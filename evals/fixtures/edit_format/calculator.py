"""A small arithmetic calculator with a running memory register.

Used as an edit-format eval fixture: the task is to ADD a `divide` method
without disturbing the existing operations or the memory register.
"""

from __future__ import annotations


class Calculator:
    """Stateful calculator: every operation also updates ``self.memory``."""

    def __init__(self, memory: float = 0.0) -> None:
        self.memory = memory
        self.history: list[str] = []

    def _record(self, op: str, result: float) -> float:
        self.memory = result
        self.history.append(f"{op} -> {result}")
        return result

    def add(self, a: float, b: float) -> float:
        return self._record(f"add({a}, {b})", a + b)

    def subtract(self, a: float, b: float) -> float:
        return self._record(f"subtract({a}, {b})", a - b)

    def multiply(self, a: float, b: float) -> float:
        return self._record(f"multiply({a}, {b})", a * b)

    def clear(self) -> None:
        self.memory = 0.0
        self.history.clear()

    def last(self) -> str | None:
        return self.history[-1] if self.history else None
