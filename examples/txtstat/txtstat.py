"""Small text statistics used by the h2oc demo repository."""

from __future__ import annotations

import re

_PUNCT = ".,!?;:\"'()[]{}"


def _clean(word: str) -> str:
    return word.lower().strip(_PUNCT)


def summarize(text: str) -> dict:
    words = text.split()
    sentences = [part for part in re.split(r"\.", text) if part.strip()]
    unique = {word.lower() for word in words}
    if words:
        avg = sum(len(word) for word in words) / len(words)
    else:
        avg = 0.0
    return {
        "words": len(words),
        "sentences": len(sentences),
        "unique": len(unique),
        "avg_word_length": round(avg, 2),
    }
