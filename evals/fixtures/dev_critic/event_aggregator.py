"""Event accumulator — fixture for dev_critic eval (dc-04)."""


class EventAggregator:
    def __init__(self):
        self._events = []

    def record(self, event):
        self._events.append(event)

    def count(self):
        return len(self._events)

    def peek_all(self):
        """Return events without clearing."""
        return list(self._events)
