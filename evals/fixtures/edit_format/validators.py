"""A grab-bag of input validators.

Edit-format eval fixture: the task is to ADD an ``is_valid_port`` function that
returns True for an int in the inclusive range 1..65535, without disturbing the
existing validators.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value or ""))


def is_non_empty(value: str) -> bool:
    return bool(value and value.strip())


def is_in_range(value: float, low: float, high: float) -> bool:
    return low <= value <= high


def is_valid_username(value: str) -> bool:
    return bool(value) and value.isidentifier() and len(value) <= 32
