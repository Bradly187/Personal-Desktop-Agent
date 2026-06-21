"""Temperature conversion helpers.

Edit-format eval fixture with a SEEDED BUG: ``celsius_to_fahrenheit`` uses the
wrong multiplier (9/4 instead of 9/5). The task is to fix ONLY that formula and
leave every other converter intact.
"""

from __future__ import annotations


def celsius_to_fahrenheit(c: float) -> float:
    # BUG: should be c * 9 / 5 + 32
    return c * 9 / 4 + 32


def fahrenheit_to_celsius(f: float) -> float:
    return (f - 32) * 5 / 9


def celsius_to_kelvin(c: float) -> float:
    return c + 273.15


def kelvin_to_celsius(k: float) -> float:
    return k - 273.15


def fahrenheit_to_kelvin(f: float) -> float:
    return celsius_to_kelvin(fahrenheit_to_celsius(f))


def kelvin_to_fahrenheit(k: float) -> float:
    return celsius_to_fahrenheit(kelvin_to_celsius(k))
