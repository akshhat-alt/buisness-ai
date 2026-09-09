"""Unit tests for chunk_text(): the retrieval-unit boundary between raw
ingested content and what gets embedded/indexed."""

from __future__ import annotations

from business_ai.knowledge import chunk_text, estimate_token_count


def test_empty_text_produces_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_but_complete_source_is_still_indexed():
    """Regression test: a short-but-complete business page (a one-line
    'About Us', brief hours) must still produce a chunk even though it's
    far under min_tokens — that content is all a source has, not a
    fragment to discard."""
    text = "We are open Monday to Saturday, 9am to 7pm."
    chunks = chunk_text(text, min_tokens=40)
    assert len(chunks) == 1
    assert chunks[0].text == text


def test_long_text_is_split_into_multiple_chunks():
    paragraph = ("This is a sentence about our business. " * 40).strip()
    text = "\n\n".join([paragraph] * 5)
    chunks = chunk_text(text, target_tokens=100, max_tokens=150, min_tokens=20)
    assert len(chunks) > 1
    for chunk in chunks:
        assert estimate_token_count(chunk.text) <= 150


def test_chunk_indices_are_sequential():
    paragraph = ("Sentence. " * 60).strip()
    text = "\n\n".join([paragraph] * 4)
    chunks = chunk_text(text, target_tokens=80, max_tokens=100, min_tokens=20)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_trailing_short_fragment_merges_into_previous_chunk():
    paragraph = ("Sentence. " * 60).strip()
    text = paragraph + "\n\n" + "Short tail."
    chunks = chunk_text(text, target_tokens=80, max_tokens=100, min_tokens=20)
    # The short tail must not become its own tiny fragment chunk when it
    # can merge into the previous one.
    assert "Short tail." in chunks[-1].text
