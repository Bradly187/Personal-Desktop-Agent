"""Simple text statistics.

Edit-format eval fixture with a SEEDED BUG: ``word_count`` splits on a single
space, so runs of whitespace and leading/trailing spaces produce empty "words"
and an inflated count. The task is to fix it to use ``str.split()`` (which
collapses runs of whitespace) without touching the other stats.
"""

from __future__ import annotations


def word_count(text: str) -> int:
    # BUG: split(" ") counts empty strings from repeated/edge whitespace.
    return len(text.split(" "))


def char_count(text: str, include_spaces: bool = True) -> int:
    if include_spaces:
        return len(text)
    return len(text.replace(" ", ""))


def line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def longest_word(text: str) -> str:
    words = text.split()
    return max(words, key=len) if words else ""
