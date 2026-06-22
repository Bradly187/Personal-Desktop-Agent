"""Path utilities — fixture for dev_critic eval (dc-05)."""

import os


def normalize_path(path):
    return os.path.normpath(path)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path
