"""Stack data structure — fixture for dev_critic eval (dc-01)."""


class Stack:
    def __init__(self):
        self._items = []

    def push(self, item):
        self._items.append(item)

    def peek(self):
        if not self._items:
            raise IndexError("peek from empty stack")
        return self._items[-1]

    def __len__(self):
        return len(self._items)

    def is_empty(self):
        return len(self._items) == 0
