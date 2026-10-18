"""Canonical writable-root and terminal working-directory policy.

One :class:`ExecutionScope` is shared by every side-effect executor in a
production pipeline.  Policy failures are represented as quiet, typed denials;
callers retain ownership of their public response shapes.
"""

from __future__ import annotations

import json
import ntpath
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE_CONFIG_PATH = Path.home() / ".claude" / "ipad_bridge" / "config.json"


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    canonical_path: Path | None
    root: Path | None
    reason: str | None


class ExecutionScope:
    """Own canonical writable roots and active-directory state.

    Mutation is intentionally synchronous and in-memory.  Production constructs
    one instance on the asyncio event-loop thread and injects it into all file
    and process side-effect paths.
    """

    def __init__(
        self,
        repository_root: str | os.PathLike[str] | None = None,
        writable_roots: Iterable[str | os.PathLike[str]] | None = None,
    ) -> None:
        repository = repository_root if repository_root is not None else _REPOSITORY_ROOT
        repo = self._canonicalize(repository, base=None)
        if repo is None:
            raise ValueError(f"invalid repository root: {repository!r}")
        self._repository_root = repo
        self._active_root: Path | None = None
        configured = list(writable_roots) if writable_roots is not None else load_writable_roots()
        self._roots: tuple[Path, ...] = self._canonical_roots(configured)

    def roots(self) -> tuple[Path, ...]:
        return self._roots

    def active_root(self) -> Path | None:
        return self._active_root

    @property
    def repository_root(self) -> Path:
        return self._repository_root

    def add_root(self, path: str, *, human_confirmed: bool) -> ScopeDecision:
        if not human_confirmed:
            return self._deny("adding a writable root requires human confirmation")
        canonical = self._canonicalize(path, base=self._active_root or self._repository_root)
        if canonical is None:
            return self._deny("path could not be canonicalized")
        try:
            if not canonical.is_dir():
                return self._deny("writable root is not an existing directory", canonical)
        except OSError:
            return self._deny("writable root could not be verified", canonical)
        if not any(self._same_path(canonical, root) for root in self._roots):
            self._roots = (*self._roots, canonical)
        return ScopeDecision(True, canonical, canonical, None)

    def set_active_root(self, path: str, *, human_confirmed: bool) -> ScopeDecision:
        canonical = self._canonicalize(path, base=self._active_root or self._repository_root)
        if canonical is None:
            return self._deny("path could not be canonicalized")
        try:
            if not canonical.is_dir():
                return self._deny("active root is not an existing directory", canonical)
        except OSError:
            return self._deny("active root could not be verified", canonical)

        existing = self._containing_root(canonical)
        if existing is None and not human_confirmed:
            return self._deny("activating a new writable root requires human confirmation", canonical)
        # Preserve the existing picker contract: the selected directory itself is
        # visible in the root list, even when an ancestor was already allowed.
        if not any(self._same_path(canonical, root) for root in self._roots):
            self._roots = (*self._roots, canonical)
        self._active_root = canonical
        return ScopeDecision(True, canonical, canonical, None)

    def authorize_path(self, path: str, *, operation: str) -> ScopeDecision:
        canonical = self._canonicalize(path, base=self._active_root or self._repository_root)
        if canonical is None:
            return self._deny(f"{operation} path could not be canonicalized")
        root = self._containing_root(canonical)
        if root is None:
            return self._deny(f"{operation} path is outside the writable-root allowlist", canonical)
        return ScopeDecision(True, canonical, root, None)

    def authorize_cwd(self, cwd: str | None) -> ScopeDecision:
        selected: str | os.PathLike[str] = (
            cwd if cwd is not None else self._active_root or self._repository_root
        )
        decision = self.authorize_path(str(selected), operation="RUN_TERMINAL cwd")
        if not decision.allowed:
            return decision
        try:
            if decision.canonical_path is None or not decision.canonical_path.is_dir():
                return self._deny(
                    "RUN_TERMINAL cwd is not an existing directory",
                    decision.canonical_path,
                )
        except OSError:
            return self._deny("RUN_TERMINAL cwd could not be verified", decision.canonical_path)
        return decision

    def contains(self, path: str) -> ScopeDecision:
        """Compatibility/query helper used by active-directory confirmation."""
        canonical = self._canonicalize(path, base=self._active_root or self._repository_root)
        if canonical is None:
            return self._deny("path could not be canonicalized")
        root = self._containing_root(canonical)
        if root is None:
            return self._deny("path is outside the writable-root allowlist", canonical)
        return ScopeDecision(True, canonical, root, None)

    def _replace_roots_for_compat(self, roots: Iterable[str | os.PathLike[str]]) -> None:
        """Adapter for legacy tests/callers that assigned ``_writable_roots``."""
        self._roots = self._canonical_roots(roots)

    def _canonical_roots(
        self, roots: Iterable[str | os.PathLike[str]]
    ) -> tuple[Path, ...]:
        result: list[Path] = []
        for value in roots:
            canonical = self._canonicalize(value, base=self._repository_root)
            if canonical is None:
                continue
            if not any(self._same_path(canonical, prior) for prior in result):
                result.append(canonical)
        return tuple(result)

    def _containing_root(self, path: Path) -> Path | None:
        candidate = self._comparison_text(path)
        for root in self._roots:
            root_text = self._comparison_text(root)
            try:
                if os.path.commonpath((candidate, root_text)) == root_text:
                    return root
            except (OSError, ValueError):
                # Different drives or uncertain path semantics deny this root.
                continue
        return None

    @classmethod
    def _canonicalize(
        cls,
        value: str | os.PathLike[str],
        *,
        base: Path | None,
    ) -> Path | None:
        try:
            raw = os.fspath(value)
            if not isinstance(raw, str):
                return None
            raw = raw.strip()
            if not raw or "\x00" in raw:
                return None
            if raw[:1] in ("'", '"') or raw[-1:] in ("'", '"'):
                if len(raw) < 2 or raw[0] != raw[-1] or raw[0] not in ("'", '"'):
                    return None
                raw = raw[1:-1].strip()
                if not raw:
                    return None

            expanded = os.path.expanduser(raw)
            drive, tail = ntpath.splitdrive(expanded)
            if drive and len(drive) == 2 and drive[1] == ":" and (
                not tail or not tail.startswith(("\\", "/"))
            ):
                return None

            candidate = Path(expanded)
            if not candidate.is_absolute():
                if base is None:
                    return None
                candidate = base / candidate
            candidate = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))

            # Resolve the nearest existing ancestor strictly, then append the
            # normalized missing suffix.  Broken links/junctions are uncertain.
            suffix: list[str] = []
            probe = candidate
            while not os.path.lexists(probe):
                if probe.parent == probe:
                    return None
                if probe.name in ("", ".", ".."):
                    return None
                suffix.append(probe.name)
                probe = probe.parent
            resolved = probe.resolve(strict=True)
            for part in reversed(suffix):
                resolved /= part
            normalized = os.path.normpath(os.fspath(resolved))
            return Path(normalized)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    @staticmethod
    def _comparison_text(path: Path) -> str:
        text = os.path.normpath(os.fspath(path))
        return os.path.normcase(text) if os.name == "nt" else text

    @classmethod
    def _same_path(cls, left: Path, right: Path) -> bool:
        return cls._comparison_text(left) == cls._comparison_text(right)

    @staticmethod
    def _deny(reason: str, path: Path | None = None) -> ScopeDecision:
        return ScopeDecision(False, path, None, reason)


def load_writable_roots() -> list[str]:
    """Load configured roots while preserving the legacy defaults exactly."""
    default = [str(_REPOSITORY_ROOT), tempfile.gettempdir()]
    try:
        if _BRIDGE_CONFIG_PATH.exists():
            config = json.loads(_BRIDGE_CONFIG_PATH.read_text(encoding="utf-8"))
            roots = config.get("writable_roots")
            if isinstance(roots, list) and roots:
                return [os.path.expanduser(str(root)) for root in roots]
    except (OSError, ValueError):
        pass
    return default
