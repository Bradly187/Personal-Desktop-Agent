"""A synchronous in-process pub/sub event bus.

Long edit-format eval fixture (many small methods) to stress whole-file silent
elision: re-typing all of this to add one method risks dropping a handler. The
task is to ADD a ``subscriber_count`` method without losing any existing one.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Subscription:
    topic: str
    handler: Callable
    once: bool = False
    active: bool = True


@dataclass
class EventBus:
    """Topic -> ordered list of subscriptions; publish fans out synchronously."""

    _subs: dict = field(default_factory=lambda: defaultdict(list))
    _history: list = field(default_factory=list)
    max_history: int = 100

    def subscribe(self, topic: str, handler: Callable) -> Subscription:
        sub = Subscription(topic=topic, handler=handler)
        self._subs[topic].append(sub)
        return sub

    def subscribe_once(self, topic: str, handler: Callable) -> Subscription:
        sub = Subscription(topic=topic, handler=handler, once=True)
        self._subs[topic].append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> bool:
        bucket = self._subs.get(sub.topic, [])
        if sub in bucket:
            bucket.remove(sub)
            return True
        return False

    def unsubscribe_all(self, topic: str) -> int:
        n = len(self._subs.get(topic, []))
        self._subs[topic] = []
        return n

    def publish(self, topic: str, payload=None) -> int:
        self._record(topic, payload)
        delivered = 0
        for sub in list(self._subs.get(topic, [])):
            if not sub.active:
                continue
            sub.handler(payload)
            delivered += 1
            if sub.once:
                self.unsubscribe(sub)
        return delivered

    def _record(self, topic: str, payload) -> None:
        self._history.append((topic, payload))
        if len(self._history) > self.max_history:
            self._history.pop(0)

    def pause(self, sub: Subscription) -> None:
        sub.active = False

    def resume(self, sub: Subscription) -> None:
        sub.active = True

    def topics(self) -> list:
        return sorted(t for t, subs in self._subs.items() if subs)

    def history(self) -> list:
        return list(self._history)

    def clear_history(self) -> None:
        self._history.clear()

    def has_subscribers(self, topic: str) -> bool:
        return bool(self._subs.get(topic))

    def reset(self) -> None:
        self._subs.clear()
        self._history.clear()
