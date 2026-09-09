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
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
