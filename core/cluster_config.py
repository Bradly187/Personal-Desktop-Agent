"""ClusterConfig — static configuration for the multi-machine compute cluster.

The Personal Desktop Agent can offload work from the desktop orchestrator
(RTX 5090) to a laptop service node (RTX 4070) over the LAN:

  - lightweight Ollama inference (command domain → llama3.1:8b on the laptop)
  - faster-whisper transcription (Tier 2)
  - CodebaseIndexer RAG queries (Tier 2)

This module loads `cluster_config.json` (project root by default) into a frozen
dataclass.  Every field is optional: a missing file or missing key means "use
the desktop default", so the agent runs identically with no cluster configured.

Example cluster_config.json:
    {
      "laptop": {
        "hostname": "Brad_Laptop",
        "ollama_url":  "http://192.168.18.12:11434",
        "whisper_url": "http://192.168.18.12:8888",
        "indexer_url": "http://192.168.18.12:9000"
      },
      "routing": { "lightweight_host": "laptop" },
      "smb_share": null
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Default location: <project root>/cluster_config.json
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "cluster_config.json"


@dataclass(frozen=True)
class ClusterConfig:
    """Immutable cluster configuration.

    `enabled` is False when no config file was found — in that case every URL is
    None and `offload_lightweight` is False, so the agent behaves as a
    single-machine deployment.
    """

    enabled: bool = False
    laptop_hostname: Optional[str] = None
    laptop_ollama_url: Optional[str] = None
    laptop_whisper_url: Optional[str] = None
    laptop_indexer_url: Optional[str] = None
    # "laptop" → offload the lightweight (command-domain) inference to the laptop.
    # "desktop" (or anything else) → keep it local.
    lightweight_host: str = "desktop"
    smb_share: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, path: "str | Path | None" = None) -> "ClusterConfig":
        """Load cluster config from JSON.

        Returns a disabled config (all defaults) when the file is absent or
        unreadable — never raises, so startup is never blocked by cluster config.
        """
        p = Path(path) if path is not None else _DEFAULT_PATH
        if not p.is_file():
            log.info("ClusterConfig: no config at %s — cluster offload disabled", p)
            return cls()

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ClusterConfig: failed to read %s (%s) — cluster disabled", p, exc)
            return cls()

        laptop = data.get("laptop", {}) or {}
        routing = data.get("routing", {}) or {}

        cfg = cls(
            enabled=True,
            laptop_hostname=laptop.get("hostname"),
            laptop_ollama_url=_clean_url(laptop.get("ollama_url")),
            laptop_whisper_url=_clean_url(laptop.get("whisper_url")),
            laptop_indexer_url=_clean_url(laptop.get("indexer_url")),
            lightweight_host=(routing.get("lightweight_host") or "desktop"),
            smb_share=data.get("smb_share"),
        )
        log.info(
            "ClusterConfig: loaded from %s — lightweight_host=%s ollama=%s whisper=%s indexer=%s",
            p, cfg.lightweight_host, cfg.laptop_ollama_url,
            cfg.laptop_whisper_url, cfg.laptop_indexer_url,
        )
        return cfg

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #

    @property
    def offload_lightweight(self) -> bool:
        """True when lightweight (command-domain) inference should go to the laptop."""
        return (
            self.enabled
            and self.lightweight_host == "laptop"
            and bool(self.laptop_ollama_url)
        )

    @property
    def has_remote_whisper(self) -> bool:
        return self.enabled and bool(self.laptop_whisper_url)

    @property
    def has_remote_indexer(self) -> bool:
        return self.enabled and bool(self.laptop_indexer_url)


def _clean_url(url: Optional[str]) -> Optional[str]:
    """Normalise a URL: strip whitespace and any trailing slash. None stays None."""
    if not url:
        return None
    return url.strip().rstrip("/")
