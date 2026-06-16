"""agents/ — Observer agents (R-1, choreography).

Observer agents subscribe directly to EventBus topics (core/events.py) and react,
instead of running through the central DevAgent orchestration thread. They are
supervised background tasks (Supervisor-compatible: is_healthy/restart) and must
never do blocking work on the event-handling path.

See agents/observer_base.py for the reusable base and agents/fatigue_monitor.py
for the reference implementation.
"""
