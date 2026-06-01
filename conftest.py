# Root conftest.py
# Standalone integration scripts are meant to be run directly via
# `python tests/test_*.py`, not collected by pytest. They use a non-pytest
# test harness (asyncio.run + sys.exit) and pass mock objects as function args,
# so pytest cannot resolve their parameter fixtures.
collect_ignore_glob = [
    "tests/test_touch_scroll_e2e.py",
    "tests/test_tilt_navigation.py",
    "tests/test_gesture_bridge_e2e.py",
]
