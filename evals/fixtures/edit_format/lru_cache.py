"""A bounded LRU cache built on an ordered dict.

Long edit-format eval fixture where the edit target sits near the END of the file
(a common whole-file elision spot — models drop trailing methods). The task is to
ADD a ``peek`` method that returns a key's value WITHOUT marking it recently used,
leaving every other method intact.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class LRUCache:
    """Evicts the least-recently-used entry when capacity is exceeded."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self._data:
            self.misses += 1
            return default
        self.hits += 1
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: Any, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def contains(self, key: Any) -> bool:
        return key in self._data

    def invalidate(self, key: Any) -> bool:
        if key in self._data:
            del self._data[key]
            return True
        return False

    def clear(self) -> None:
        self._data.clear()

    def resize(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def keys(self) -> list:
        return list(self._data.keys())

    def values(self) -> list:
        return list(self._data.values())

    def __len__(self) -> int:
        return len(self._data)

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def reset_stats(self) -> None:
        self.hits = 0
        self.misses = 0
