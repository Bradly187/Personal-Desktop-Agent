"""storage/personal_kb.py — PersonalKB: semantic search over the user's own files.

Fills the personal-knowledge gap: the codebase indexer covers source + project
PDFs, but "what did I write about X in my notes?" needs the user's documents.
PersonalKB indexes configured roots (default: ~/Documents and ~/Notes when they
exist) into its OWN ChromaDB store under ~/.claude/personal_kb/ — deliberately
outside the repo so personal content never rides along with project backups.

Pure-local: no auth, no network, no cloud. Markdown/.txt files are chunked by
paragraph (coalesced to ~1500 chars, heading-prefixed for context); PDFs by page.
Re-index skips unchanged files via an mtime state file and prunes deleted ones.

Config (optional): ~/.claude/personal_kb/config.json
    {"roots": ["C:/Users/me/Documents", "D:/notes"], "extensions": [".md", ".txt", ".pdf"]}

Wire-up: constructed in main.py, indexed in a background task at startup, queried
by the DevAgent (personal-query intent + the SEARCH_PERSONAL plan verb).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_KB_DIR = Path.home() / ".claude" / "personal_kb"
_CONFIG_PATH = _KB_DIR / "config.json"
_STATE_PATH = _KB_DIR / "state.json"
_CHROMA_DIR = _KB_DIR / "chroma"

_DEFAULT_EXTENSIONS = (".md", ".txt", ".pdf")
_MAX_FILE_BYTES = 2 * 1024 * 1024      # skip text files larger than 2 MB
_MAX_FILES_PER_ROOT = 5000             # hard scan bound per root
_MAX_CHUNK_CHARS = 1500                # coalesce paragraphs up to this size
_SKIP_DIR_NAMES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".obsidian",
    "AppData", "Library", ".cache", ".Trash", "$RECYCLE.BIN",
}


# Phrases that signal a question about the user's OWN documents. Every marker
# is anchored on a DOCUMENT NOUN — bare verb forms like "find my"/"search my"
# hijacked dev queries ("find my bug in the parser", "search my codebase").
# Shared by the DevAgent (routing to the KB) and the HybridCoordinator (forcing
# the query LOCAL — personal-document questions must never reach the cloud).
PERSONAL_QUERY_MARKERS: tuple[str, ...] = (
    "my notes", "my note", "my journal", "my documents", "my docs",
    "my personal files",
)


def is_personal_query(text: str) -> bool:
    tl = (text or "").lower()
    return any(m in tl for m in PERSONAL_QUERY_MARKERS)


@dataclass
class DocChunk:
    file: str          # absolute path (personal files live outside any one root)
    name: str          # heading / "page N" / "part i/N"
    text: str
    mtime: float


# ---------------------------------------------------------------------------
# Chunkers (pure functions — unit-testable without ChromaDB)
# ---------------------------------------------------------------------------

def chunk_text_document(path: Path, text: str, mtime: float) -> list[DocChunk]:
    """Paragraph-chunk a markdown/plain-text document.

    Splits on blank lines, coalesces consecutive paragraphs up to
    _MAX_CHUNK_CHARS, and carries the most recent markdown heading into each
    chunk name so retrieval hits keep their context.
    """
    chunks: list[DocChunk] = []
    heading = "(start)"
    buf: list[str] = []
    buf_len = 0

    def _flush() -> None:
        nonlocal buf, buf_len
        body = "\n\n".join(buf).strip()
        if body:
            chunks.append(DocChunk(file=str(path), name=heading, text=body, mtime=mtime))
        buf, buf_len = [], 0

    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        first = para.lstrip().splitlines()[0] if para.strip() else ""
        if first.startswith("#"):
            _flush()
            heading = first.lstrip("# ").strip() or heading
        if buf_len + len(para) > _MAX_CHUNK_CHARS and buf:
            _flush()
        # An oversized single paragraph is split hard so no chunk explodes.
        while len(para) > _MAX_CHUNK_CHARS:
            chunks.append(DocChunk(file=str(path), name=heading,
                                   text=para[:_MAX_CHUNK_CHARS], mtime=mtime))
            para = para[_MAX_CHUNK_CHARS:]
        if para:
            buf.append(para)
            buf_len += len(para)
    _flush()
    return chunks


def chunk_pdf_document(path: Path, mtime: float) -> list[DocChunk]:
    """Page-chunk a PDF (pdfplumber preferred, pypdf fallback, [] if neither)."""
    pages: list[str] = []
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
    except Exception:
        try:
            from pypdf import PdfReader
            pages = [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
        except Exception as exc:
            log.debug("PersonalKB: PDF extract failed for %s: %s", path, exc)
            return []
    return [
        DocChunk(file=str(path), name=f"page {i + 1}",
                 text=text.strip()[: _MAX_CHUNK_CHARS * 2], mtime=mtime)
        for i, text in enumerate(pages) if text.strip()
    ]


def _default_roots() -> list[str]:
    home = Path.home()
    return [str(p) for p in (home / "Documents", home / "Notes") if p.is_dir()]


# ---------------------------------------------------------------------------
# PersonalKB
# ---------------------------------------------------------------------------

class PersonalKB:
    """ChromaDB-backed semantic index over the user's personal documents."""

    COLLECTION = "personal"

    def __init__(
        self,
        roots: Optional[list[str]] = None,
        chroma_dir: "str | Path | None" = None,
        embedder=None,
        config_path: "str | Path | None" = None,
        state_path: "str | Path | None" = None,
    ) -> None:
        self._config_path = Path(config_path) if config_path else _CONFIG_PATH
        self._state_path = Path(state_path) if state_path else _STATE_PATH
        cfg = self._load_config()
        self._roots = [Path(r) for r in (roots or cfg.get("roots") or _default_roots())]
        self._extensions = tuple(cfg.get("extensions") or _DEFAULT_EXTENSIONS)
        self._chroma_dir = str(chroma_dir) if chroma_dir else str(_CHROMA_DIR)
        self._embedder = embedder
        import typing
        self._client: typing.Any = None
        self._col: typing.Any = None
        self.available = False
        self._state: dict[str, float] = {}   # abs path -> mtime at last index
        self._paused = False
        self._index_lock = asyncio.Lock()
        # Cooperative cancel for the indexing worker thread: task.cancel() can't
        # interrupt asyncio.to_thread, so stop() sets this and _index_sync exits
        # at the next file boundary instead of pinning shutdown for minutes.
        self._stop_event = threading.Event()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Initialize the ChromaDB collection. False (graceful) when unavailable."""
        try:
            import chromadb

            def _open():
                client = chromadb.PersistentClient(path=self._chroma_dir)
                ef = self._embedder
                if ef is None:
                    from chromadb.utils.embedding_functions import (
                        SentenceTransformerEmbeddingFunction,
                    )
                    device = "cpu"
                    try:
                        import torch
                        if torch.cuda.is_available():
                            device = "cuda"
                    except Exception:
                        pass
                    ef = SentenceTransformerEmbeddingFunction(
                        model_name="all-MiniLM-L6-v2", device=device)
                col = client.get_or_create_collection(
                    name=self.COLLECTION,
                    embedding_function=ef,
                    configuration={"hnsw": {"space": "cosine"}},
                )
                return client, col

            self._client, self._col = await asyncio.to_thread(_open)
            self._state = self._load_state()
            self.available = True
            n = await asyncio.to_thread(self._col.count)
            log.info("PersonalKB ready: %d chunks, roots=%s",
                     n, [str(r) for r in self._roots])
            return True
        except Exception as exc:
            log.warning("PersonalKB.start() failed: %s — personal search unavailable", exc)
            self.available = False
            return False

    async def stop(self) -> None:
        # Signal the worker FIRST so an in-flight _index_sync exits at the next
        # file; it holds its own local collection ref, so nulling ours is safe.
        self._stop_event.set()
        self._client = None
        self._col = None
        self.available = False

    def pause(self) -> None:
        """ResourceGovernor hook: skip new index jobs during a flare."""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    # ── Indexing ───────────────────────────────────────────────────────────

    def _scan(self) -> list[Path]:
        """All indexable files under the configured roots, bounded.

        Tracks resolved real paths of visited directories so symlink/junction
        loops (os.walk's followlinks=False does NOT stop Windows junctions)
        can't recurse forever.
        """
        out: list[Path] = []
        seen_real: set[str] = set()
        for root in self._roots:
            if not root.is_dir():
                continue
            count = 0
            for dirpath, dirnames, filenames in os.walk(root):
                if self._stop_event.is_set():
                    return out
                pruned = []
                for d in dirnames:
                    if d in _SKIP_DIR_NAMES or d.startswith("."):
                        continue
                    real = os.path.normcase(os.path.realpath(os.path.join(dirpath, d)))
                    if real in seen_real:
                        continue   # junction/symlink cycle — already visited
                    seen_real.add(real)
                    pruned.append(d)
                dirnames[:] = pruned
                for fn in filenames:
                    if count >= _MAX_FILES_PER_ROOT:
                        log.warning("PersonalKB: scan bound (%d) hit under %s — "
                                    "remaining files skipped", _MAX_FILES_PER_ROOT, root)
                        break
                    p = Path(dirpath) / fn
                    if p.suffix.lower() in self._extensions:
                        out.append(p)
                        count += 1
                if count >= _MAX_FILES_PER_ROOT:
                    break
        return out

    def _chunk_file(self, path: Path) -> list[DocChunk]:
        try:
            mtime = path.stat().st_mtime
            if path.suffix.lower() == ".pdf":
                return chunk_pdf_document(path, mtime)
            if path.stat().st_size > _MAX_FILE_BYTES:
                log.debug("PersonalKB: %s exceeds size bound — skipped", path)
                return []
            text = path.read_text(encoding="utf-8", errors="replace")
            return chunk_text_document(path, text, mtime)
        except OSError as exc:
            log.debug("PersonalKB: cannot read %s: %s", path, exc)
            return []

    async def index(self, force: bool = False) -> dict:
        """Index changed files; prune deleted ones. Returns counts.

        Serialized (one index job at a time); a paused KB (flare) is a no-op.
        """
        if not self.available:
            return {"error": "unavailable"}
        if self._paused:
            return {"skipped": "paused"}
        async with self._index_lock:
            return await asyncio.to_thread(self._index_sync, force)

    def _index_sync(self, force: bool) -> dict:
        # Hold a LOCAL collection ref for the whole run: stop() nulls self._col
        # under us, but the live object stays valid for this thread.
        col = self._col
        if col is None:
            return {"error": "unavailable"}
        files = self._scan()
        on_disk = {str(p) for p in files}
        indexed, skipped = 0, 0

        # Prune chunks whose source file no longer exists.
        pruned = 0
        for known in list(self._state):
            if self._stop_event.is_set():
                break
            if known not in on_disk:
                try:
                    got = col.get(where={"file": {"$eq": known}})
                    ids = got.get("ids", [])
                    if ids:
                        col.delete(ids=ids)
                except Exception as exc:
                    log.debug("PersonalKB: prune of %s failed: %s", known, exc)
                self._state.pop(known, None)
                pruned += 1

        for path in files:
            if self._stop_event.is_set():
                break   # cooperative cancel — return partial stats fast
            key = str(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if not force and self._state.get(key) == mtime:
                skipped += 1
                continue
            chunks = self._chunk_file(path)
            try:
                # Replace this file's chunks wholesale (stale-id-proof).
                got = col.get(where={"file": {"$eq": key}})
                if got.get("ids"):
                    col.delete(ids=got["ids"])
                if chunks:
                    col.add(
                        documents=[c.text for c in chunks],
                        metadatas=[{"file": c.file, "name": c.name, "mtime": c.mtime}
                                   for c in chunks],
                        ids=[f"{key}::{i}" for i in range(len(chunks))],
                    )
                self._state[key] = mtime
                indexed += 1
            except Exception as exc:
                log.warning("PersonalKB: indexing %s failed: %s", path, exc)
        self._save_state()
        stats = {"indexed": indexed, "skipped": skipped, "pruned": pruned,
                 "stopped": self._stop_event.is_set()}
        try:
            stats["total_chunks"] = col.count()
        except Exception:
            pass
        log.info("PersonalKB index: %s", stats)
        return stats

    # ── Query ──────────────────────────────────────────────────────────────

    async def query(self, query: str, n: int = 4) -> list[dict]:
        """Semantic search. Returns [{file, name, text, score}] (cosine score)."""
        if not self.available or self._col is None:
            return []
        try:
            count = await asyncio.to_thread(self._col.count)
            if count == 0:
                return []
            res = await asyncio.to_thread(
                self._col.query, query_texts=[query], n_results=min(n, count))
            out: list[dict] = []
            for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0],
                                       res["distances"][0]):
                out.append({"file": meta.get("file", "?"), "name": meta.get("name", ""),
                            "text": doc, "score": 1.0 - dist})
            return out
        except Exception as exc:
            log.warning("PersonalKB.query failed: %s", exc)
            return []

    def get_status(self) -> dict:
        return {
            "available": self.available,
            "roots": [str(r) for r in self._roots],
            "files_indexed": len(self._state),
            "paused": self._paused,
        }

    # ── Config / state persistence ─────────────────────────────────────────

    def _load_config(self) -> dict:
        try:
            if self._config_path.exists():
                cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    return cfg
        except (OSError, ValueError) as exc:
            log.warning("PersonalKB: bad config %s: %s", self._config_path, exc)
        return {}

    def _load_state(self) -> dict:
        try:
            if self._state_path.exists():
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict):
                    return {str(k): float(v) for k, v in state.items()}
        except (OSError, ValueError):
            pass
        return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            log.debug("PersonalKB: state save failed: %s", exc)
