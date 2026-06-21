"""A tiny in-memory tabular data structure (a poor man's dataframe).

Long edit-format eval fixture (many methods) to stress whole-file elision. The
task is to ADD a ``column_sum`` method that totals a numeric column, without
dropping any of the existing row/column/aggregate helpers.
"""

from __future__ import annotations

from typing import Any, Callable


class Table:
    """Columns are named; rows are dicts keyed by column name."""

    def __init__(self, columns: list) -> None:
        self.columns = list(columns)
        self._rows: list = []

    def add_row(self, row: dict) -> None:
        missing = [c for c in self.columns if c not in row]
        if missing:
            raise KeyError(f"row missing columns: {missing}")
        self._rows.append({c: row[c] for c in self.columns})

    def add_rows(self, rows: list) -> None:
        for r in rows:
            self.add_row(r)

    def row_count(self) -> int:
        return len(self._rows)

    def column(self, name: str) -> list:
        self._check_column(name)
        return [r[name] for r in self._rows]

    def _check_column(self, name: str) -> None:
        if name not in self.columns:
            raise KeyError(f"no such column: {name}")

    def select(self, names: list) -> "Table":
        for n in names:
            self._check_column(n)
        out = Table(names)
        for r in self._rows:
            out.add_row({n: r[n] for n in names})
        return out

    def filter(self, predicate: Callable) -> "Table":
        out = Table(self.columns)
        out._rows = [r for r in self._rows if predicate(r)]
        return out

    def sort_by(self, name: str, reverse: bool = False) -> "Table":
        self._check_column(name)
        out = Table(self.columns)
        out._rows = sorted(self._rows, key=lambda r: r[name], reverse=reverse)
        return out

    def map_column(self, name: str, fn: Callable) -> "Table":
        self._check_column(name)
        out = Table(self.columns)
        for r in self._rows:
            nr = dict(r)
            nr[name] = fn(r[name])
            out.add_row(nr)
        return out

    def distinct(self, name: str) -> list:
        self._check_column(name)
        seen: list = []
        for v in self.column(name):
            if v not in seen:
                seen.append(v)
        return seen

    def aggregate(self, name: str, fn: Callable) -> Any:
        return fn(self.column(name))

    def head(self, n: int = 5) -> list:
        return self._rows[:n]

    def to_dicts(self) -> list:
        return [dict(r) for r in self._rows]
