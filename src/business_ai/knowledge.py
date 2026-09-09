"""Chunking: break ingested business content into retrieval-sized pieces.

The splitting algorithm (recursive separator-based splitting with token
bounds and overlap) is adapted near-verbatim from Shri AI's proven
knowledge/chunking.py. Simplified for Business AI's sources: a business's
knowledge (website text, an uploaded FAQ/PDF, pasted text) doesn't have
PDF page numbers, so chunks are tracked by sequential index within their
source instead of page_start/page_end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_TARGET_TOKENS = 350
DEFAULT_MAX_TOKENS = 500
DEFAULT_MIN_TOKENS = 40
DEFAULT_OVERLAP_TOKENS = 50


@dataclass(frozen=True)
class TextChunk:
    text: str
    chunk_index: int


def estimate_token_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def normalize_text(text: str) -> str:
    cleaned = text.replace("\x00", "").replace("\x03", " ")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned


def _split_long_text(text: str, max_tokens: int) -> list[str]:
    """Break an oversized block into sentence- or word-level pieces."""
    parts = [text]
    for separator in ["\n", ". ", "? ", "! "]:
        next_parts: list[str] = []
        for part in parts:
            if estimate_token_count(part) <= max_tokens:
                next_parts.append(part)
                continue
            pieces = part.split(separator)
            for i, piece in enumerate(pieces):
                if not piece.strip():
                    continue
                suffix = separator if i < len(pieces) - 1 else ""
                next_parts.append(piece + suffix)
        parts = next_parts

    final: list[str] = []
    for part in parts:
        if estimate_token_count(part) <= max_tokens:
            final.append(part)
            continue
        words = part.split(" ")
        current: list[str] = []
        for word in words:
            candidate = " ".join(current + [word])
            if current and estimate_token_count(candidate) > max_tokens:
                final.append(" ".join(current) + " ")
                current = [word]
            else:
                current.append(word)
        if current:
            final.append(" ".join(current))
    return final


def chunk_text(
    text: str,
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[TextChunk]:
    """Chunk a single block of business content into retrieval-sized pieces."""
    normalized = normalize_text(text.strip())
    if not normalized:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", normalized) if p.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if estimate_token_count(paragraph) <= max_tokens:
            units.append(paragraph + "\n\n")
        else:
            units.extend(u + "\n\n" for u in _split_long_text(paragraph, max_tokens))

    if not units:
        return []

    chunks: list[TextChunk] = []
    buffer: list[str] = []
    chunk_index = 0

    def emit(active_buffer: list[str]) -> None:
        nonlocal chunk_index
        joined = "".join(active_buffer).strip()
        if not joined or estimate_token_count(joined) < min_tokens:
            return
        chunks.append(TextChunk(text=joined, chunk_index=chunk_index))
        chunk_index += 1

    def take_overlap(active_buffer: list[str]) -> list[str]:
        if overlap_tokens <= 0 or not active_buffer:
            return []
        selected: list[str] = []
        for unit in reversed(active_buffer):
            selected.insert(0, unit)
            if estimate_token_count("".join(selected)) >= overlap_tokens:
                break
        return selected

    for unit in units:
        candidate = buffer + [unit]
        if buffer and estimate_token_count("".join(candidate)) > max_tokens:
            emit(buffer)
            overlap = take_overlap(buffer)
            overlap_plus = overlap + [unit]
            buffer = overlap_plus if estimate_token_count("".join(overlap_plus)) <= max_tokens else [unit]
        else:
            buffer = candidate

    if buffer:
        joined = "".join(buffer).strip()
        if chunks and estimate_token_count(joined) < min_tokens:
            previous = chunks[-1]
            merged = f"{previous.text}\n\n{joined}".strip()
            if estimate_token_count(merged) <= max_tokens:
                chunks[-1] = TextChunk(text=merged, chunk_index=previous.chunk_index)
            else:
                emit(buffer)
        else:
            emit(buffer)

    return chunks
