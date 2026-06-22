"""Config file loader — fixture for dev_critic eval (dc-08)."""

import json
import os


def write_config(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
