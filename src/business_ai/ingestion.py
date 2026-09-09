"""Ingestion: turn a business's raw content (website, PDF, pasted text) into
indexed, retrievable knowledge segments.

The website fetcher's SSRF protection (assert_public_http_url, validated on
the initial request AND every redirect hop) is the security-critical piece,
carried over from Shri AI's connectors/website/fetcher.py. HTML-to-text
extraction uses the stdlib html.parser instead of Shri AI's lxml-based
parser — a business FAQ/about page doesn't need DOM-level sophistication,
and this keeps the dependency footprint smaller.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Generator
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel
from pypdf import PdfReader

from business_ai.knowledge import chunk_text
from business_ai.retrieval import EmbeddingProvider, VectorStore
from business_ai.security import MAX_HTTP_RESPONSE_BYTES, UnsafeUrlError, assert_public_http_url, read_limited_response

USER_AGENT = "BusinessAI-Ingest/1.0 (+https://business-ai.example)"


class IngestionError(Exception):
    """Raised when a source cannot be fetched or parsed."""


@dataclass(frozen=True)
class IngestResult:
    source_id: str
    chunks_indexed: int


# ==============================================================================
# Website fetching (SSRF-safe)
# ==============================================================================


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Re-validates every redirect target against SSRF rules — a URL that
    passes the initial check can still redirect to an internal address."""

    def __init__(self, max_redirects: int = 5) -> None:
        super().__init__()
        self.max_redirects = max_redirects
        self.redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirect_count += 1
        if self.redirect_count > self.max_redirects:
            raise IngestionError("Too many redirects.")
        assert_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _TextExtractingParser(HTMLParser):
    """Extract visible text from HTML, skipping script/style content."""

    _SKIP_TAGS = {"script", "style", "noscript", "head", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.text_parts.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtractingParser()
    parser.feed(html)
    return "\n\n".join(parser.text_parts)


def fetch_website_text(url: str) -> str:
    """Fetch a single URL and extract its visible text. SSRF-guarded on the
    initial request and every redirect hop."""
    assert_public_http_url(url)
    opener = build_opener(_SafeRedirectHandler())
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=15) as response:
            raw = read_limited_response(response, max_bytes=MAX_HTTP_RESPONSE_BYTES)
    except UnsafeUrlError:
        raise
    except (HTTPError, URLError) as exc:
        raise IngestionError(f"Failed to fetch {url}: {exc}") from exc

    charset = "utf-8"
    content_type = response.headers.get_content_charset() if hasattr(response, "headers") else None
    if content_type:
        charset = content_type
    try:
        html = raw.decode(charset, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")

    text = html_to_text(html)
    if not text.strip():
        raise IngestionError(f"No extractable text found at {url}.")
    return text


# ==============================================================================
# PDF text extraction
# ==============================================================================


def extract_pdf_text(file_bytes: bytes) -> str:
    import io

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception as exc:
        raise IngestionError(f"Could not read PDF: {exc}") from exc
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n\n".join(pages).strip()
    if not text:
        raise IngestionError("No extractable text found in this PDF.")
    return text


# ==============================================================================
# Orchestration: text -> chunks -> embeddings -> vector store
# ==============================================================================


def ingest_text(
    *,
    text: str,
    tenant_id: str,
    source_id: str,
    source_label: str,
    source_url: str | None,
    embeddings: EmbeddingProvider,
    store: VectorStore,
) -> IngestResult:
    chunks = chunk_text(text)
    if not chunks:
        raise IngestionError("No content could be extracted from this source.")

    chunk_texts = [c.text for c in chunks]
    vectors = embeddings.embed_texts(chunk_texts)

    ids = [f"seg_{source_id}_{c.chunk_index}_{secrets.token_hex(3)}" for c in chunks]
    metadatas = [
        {
            "tenant_id": tenant_id,
            "source_id": source_id,
            "source_label": source_label,
            **({"source_url": source_url} if source_url else {}),
        }
        for _ in chunks
    ]
    store.upsert(ids=ids, embeddings=vectors, metadatas=metadatas, documents=chunk_texts)
    return IngestResult(source_id=source_id, chunks_indexed=len(chunks))


# ==============================================================================
# Source tracking — so a business owner can see what they've added
# ==============================================================================


class SourceRecord(BaseModel):
    source_id: str
    tenant_id: str
    label: str
    source_type: str  # "website" | "pdf" | "text"
    chunks_indexed: int
    created_at: str


class SourceStore:
    """Thread-safe SQLite store recording what's been ingested per tenant."""

    def __init__(self, db_path: Path | str = "data/sources.db") -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    chunks_indexed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_tenant ON sources(tenant_id)")
            conn.commit()

    def record(self, *, tenant_id: str, source_id: str, label: str, source_type: str, chunks_indexed: int) -> SourceRecord:
        rec = SourceRecord(
            source_id=source_id, tenant_id=tenant_id, label=label, source_type=source_type,
            chunks_indexed=chunks_indexed, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO sources (source_id, tenant_id, label, source_type, chunks_indexed, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rec.source_id, rec.tenant_id, rec.label, rec.source_type, rec.chunks_indexed, rec.created_at),
            )
            conn.commit()
        return rec

    def list_for_tenant(self, tenant_id: str) -> list[SourceRecord]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
            ).fetchall()
            return [SourceRecord(**dict(r)) for r in rows]
