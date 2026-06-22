"""Shell execution helpers — fixture for dev_critic eval (dc-07)."""

import subprocess


def safe_run(args, *, timeout=30):
    """Run a command with an explicit arg list (no shell=True, no injection risk)."""
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    result.check_returncode()
    return result.stdout
