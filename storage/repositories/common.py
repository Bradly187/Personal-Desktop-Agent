import os
_GOAL_LEASE_TTL_S: float = 1800.0  # 30 minutes

def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return True
    return True
