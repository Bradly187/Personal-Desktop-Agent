"""An in-memory inventory store — the longest fixture, to stress whole-file
silent elision (a weak model re-typing all of this is prone to dropping a method).

Task: ADD a ``total_value`` method that returns the summed quantity*unit_price
across all items, without dropping any existing method.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    name: str
    quantity: int
    unit_price: float


class Inventory:
    """Keyed by SKU; supports add/remove/restock/lookup and a low-stock report."""

    def __init__(self, low_stock_threshold: int = 5) -> None:
        self._items: dict[str, Item] = {}
        self.low_stock_threshold = low_stock_threshold

    def add_item(self, item: Item) -> None:
        if item.sku in self._items:
            raise KeyError(f"SKU already exists: {item.sku}")
        self._items[item.sku] = item

    def remove_item(self, sku: str) -> Item:
        if sku not in self._items:
            raise KeyError(f"no such SKU: {sku}")
        return self._items.pop(sku)

    def restock(self, sku: str, amount: int) -> int:
        if amount <= 0:
            raise ValueError("restock amount must be positive")
        item = self._items[sku]
        item.quantity += amount
        return item.quantity

    def sell(self, sku: str, amount: int) -> int:
        item = self._items[sku]
        if amount > item.quantity:
            raise ValueError(f"not enough stock for {sku}")
        item.quantity -= amount
        return item.quantity

    def get(self, sku: str) -> Item:
        return self._items[sku]

    def count(self) -> int:
        return len(self._items)

    def low_stock(self) -> list[Item]:
        return [it for it in self._items.values()
                if it.quantity <= self.low_stock_threshold]

    def skus(self) -> list[str]:
        return sorted(self._items)
