"""inference.codebase_indexer.py — RAG index for Python/Swift source and PDF documents.

Maintains two ChromaDB collections:
  - "codebase"  : Python/Swift source files chunked by function/class boundary
  - "documents" : PDF pages from docs/ directory

Provides:
  query_codebase(query, n=5)  → list of {file, chunk_type, name, text, score}
  query_docs(query, n=5)      → list of {file, page, text, score}

Incremental indexing: only re-indexes files whose mtime has changed since the
last run.  State is tracked in `.codebase_index_state.json` in the project root.

Wire into DevAgent:
    from inference.codebase_indexer import get_indexer
    indexer = get_indexer()
    await indexer.start()
    await indexer.index()                     # call once at startup or on demand
    hits = await indexer.query_codebase("how does gate 1 work")
    docs_hits = await indexer.query_docs("requirements latency")

Standalone:
    python codebase_indexer.py [--reindex] [--query "text"] [--docs]
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Module-level ProcessPoolExecutor — shared by all CodebaseIndexer instances.
# Uses 4 workers, mapped to E-cores on i9-14500KF via OS scheduling.
# Handles CPU-bound AST parsing, Swift regex chunking, and PDF text extraction
# off the main event loop without GIL contention.
_CPU_POOL: Optional[concurrent.futures.ProcessPoolExecutor] = None


def _get_cpu_pool() -> concurrent.futures.ProcessPoolExecutor:
    global _CPU_POOL
    if _CPU_POOL is None or _CPU_POOL._broken:  # type: ignore[attr-defined]
        _CPU_POOL = concurrent.futures.ProcessPoolExecutor(max_workers=4)
    return _CPU_POOL

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Chunk dataclass
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """A single indexable unit from a source file or document."""
    file: str           # relative path from project root
    chunk_type: str     # "function" | "class" | "module" | "method" | "page"
    name: str           # function/class name, or "page_N"
    text: str           # content to embed
    start_line: int = 0
    page: int = 0       # PDF page number (1-indexed, 0 = not applicable)


# ---------------------------------------------------------------------------
# Python chunker (AST-based)
# ---------------------------------------------------------------------------

_MAX_CHUNK_CHARS = 4000   # truncate very long chunks before embedding


def _chunk_python(path: Path, project_root: Path) -> list[Chunk]:
    """Chunk a Python file into function/class/module-level pieces using AST."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("CodebaseIndexer: cannot read %s: %s", path, exc)
        return []

    rel = str(path.relative_to(project_root))
    lines = source.splitlines()
    chunks: list[Chunk] = []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        log.debug("CodebaseIndexer: Python parse error %s: %s", rel, exc)
        # Fall back to whole-file chunk
        snippet = source[:_MAX_CHUNK_CHARS]
        return [Chunk(file=rel, chunk_type="module", name="(file)", text=snippet)]

    # ── Module-level docstring ──
    module_doc = ast.get_docstring(tree)
    if module_doc:
        chunks.append(Chunk(
            file=rel, chunk_type="module", name="(docstring)",
            text=module_doc[:_MAX_CHUNK_CHARS], start_line=1,
        ))

    # ── Walk top-level definitions ──
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # Only top-level and one level deep (methods inside classes)
        start = node.lineno - 1
        end = getattr(node, "end_lineno", start + 1)
        chunk_text = "\n".join(lines[start:end])[:_MAX_CHUNK_CHARS]
        ctype = (
            "class" if isinstance(node, ast.ClassDef)
            else "method" if _is_method(node, tree)
            else "function"
        )
        chunks.append(Chunk(
            file=rel, chunk_type=ctype, name=node.name,
            text=chunk_text, start_line=node.lineno,
        ))

    if not chunks:
        # Fallback: whole file truncated
        chunks.append(Chunk(
            file=rel, chunk_type="module", name="(file)",
            text=source[:_MAX_CHUNK_CHARS],
        ))

    return chunks


def _is_method(node: ast.AST, tree: ast.AST) -> bool:
    """True if `node` is a method (direct child of a ClassDef)."""
    for parent in ast.walk(tree):
        if isinstance(parent, ast.ClassDef) and node in parent.body:
            return True
    return False


# ---------------------------------------------------------------------------
# Swift chunker (regex-based)
# ---------------------------------------------------------------------------

_SWIFT_TOP_DEF = re.compile(
    r"^(?:(?:public|private|internal|open|fileprivate)\s+)*"
    r"(?:final\s+)?"
    r"(class|struct|enum|protocol|actor|extension|func|var|let)\s+"
    r"(\w+)",
    re.MULTILINE,
)


def _chunk_swift(path: Path, project_root: Path) -> list[Chunk]:
    """Chunk a Swift file into definition-level pieces via regex."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("CodebaseIndexer: cannot read %s: %s", path, exc)
        return []

    rel = str(path.relative_to(project_root))
    lines = source.splitlines()
    chunks: list[Chunk] = []

    matches = list(_SWIFT_TOP_DEF.finditer(source))
    for i, m in enumerate(matches):
        start_char = m.start()
        end_char = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        snippet = source[start_char:end_char][:_MAX_CHUNK_CHARS]

        # Determine line number
        start_line = source[:start_char].count("\n") + 1
        ctype = m.group(1)   # class, struct, func, etc.
        name = m.group(2)

        chunks.append(Chunk(
            file=rel, chunk_type=ctype, name=name,
            text=snippet.strip(), start_line=start_line,
        ))

    if not chunks:
        chunks.append(Chunk(
            file=rel, chunk_type="module", name="(file)",
            text=source[:_MAX_CHUNK_CHARS],
        ))

    return chunks


# ---------------------------------------------------------------------------
# PDF chunker (page-by-page)
# ---------------------------------------------------------------------------

def _chunk_pdf(path: Path, project_root: Path) -> list[Chunk]:
    """Chunk a PDF file one page per Chunk."""
    rel = str(path.relative_to(project_root))
    chunks: list[Chunk] = []

    # Try pdfplumber first (better text extraction), fall back to pypdf
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = (page.extract_text() or "").strip()
                if text:
                    chunks.append(Chunk(
                        file=rel, chunk_type="page",
                        name=f"page_{i}", text=text[:_MAX_CHUNK_CHARS], page=i,
                    ))
        return chunks
    except ImportError:
        pass
    except Exception as exc:
        log.debug("CodebaseIndexer: pdfplumber failed for %s: %s", path, exc)

    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        for i, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                chunks.append(Chunk(
                    file=rel, chunk_type="page",
                    name=f"page_{i}", text=text[:_MAX_CHUNK_CHARS], page=i,
                ))
        return chunks
    except ImportError:
        log.debug("CodebaseIndexer: no PDF library (install pdfplumber or pypdf)")
    except Exception as exc:
        log.debug("CodebaseIndexer: PDF read failed for %s: %s", path, exc)

    return chunks


# ---------------------------------------------------------------------------
# CodebaseIndexer
# ---------------------------------------------------------------------------

class CodebaseIndexer:
    """Indexes Python/Swift source files and PDF documents into ChromaDB.

    Usage:
        indexer = CodebaseIndexer(project_root="E:/Personal_Desktop_Agent")
        await indexer.start()
        stats = await indexer.index()       # incremental by default
        hits  = await indexer.query_codebase("gate 1 confidence threshold")
        pages = await indexer.query_docs("pain day adaptation requirements")
    """

    CODEBASE_COLLECTION = "codebase"
    DOCUMENTS_COLLECTION = "documents"
    STATE_FILE = ".codebase_index_state.json"
    CHROMA_DIR = "./chroma_db"

    # Source patterns
    SOURCE_PATTERNS = ["**/*.py", "**/*.swift"]
    # Directories to exclude (generated, deps, venv, etc.)
    # NOTE: includes named venv variants — the laptop service node uses
    # ".venv-laptop"; without it the indexer would embed all of site-packages
    # (14k+ files) and never settle.
    EXCLUDE_DIRS = {
        ".git", ".claude", "__pycache__", "node_modules",
        "venv", ".venv", ".venv-laptop", ".venv-wsl", "env",
        "Pods", "build", "DerivedData", ".build", "chroma_db",
    }
    # File size limit — skip huge auto-generated files
    MAX_FILE_BYTES = 500_000

    def __init__(
        self,
        project_root: str = ".",
        chroma_dir: Optional[str] = None,
        embedder=None,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._chroma_dir = chroma_dir or str(self._root / "chroma_db")
        self._state_path = self._root / self.STATE_FILE

        # Optional custom ChromaDB embedding function. When None, start() falls
        # back to the built-in all-MiniLM-L6-v2 SentenceTransformer encoder.
        # Mirrors SemanticMemory's `encoder=` injection point. Must be a
        # ChromaDB-compatible embedding function (callable + name()); a raw
        # VLLMEmbedder needs a thin Chroma-EF adapter before it can be passed
        # here — see local_inference.VLLMEmbedder.
        self._embedder = embedder
        # The embedding function actually selected in start() (custom or the
        # MiniLM default), exposed for diagnostics / tests.
        self._embedding_fn = None

        self._client = None
        self._codebase_col = None
        self._documents_col = None
        self._available = False

        # mtime state: {rel_path: mtime}
        self._state: dict[str, float] = {}

        # File watcher state (roadmap item #5)
        self._observer = None
        self._watch_loop: Optional[asyncio.AbstractEventLoop] = None

        # ResourceGovernor pause flag — when True, new index/re-index jobs are
        # skipped. In-progress jobs run to completion.
        self._paused: bool = False

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def start(self) -> bool:
        """Initialize ChromaDB. Returns True if available."""
        try:
            import chromadb

            os.makedirs(self._chroma_dir, exist_ok=True)
            client = await asyncio.to_thread(
                chromadb.PersistentClient, path=self._chroma_dir
            )

            # Embedding function selection:
            #   * A caller-supplied embedder (e.g. a future VLLMEmbedder adapter)
            #     takes priority — mirrors SemanticMemory's `encoder=` path.
            #   * Otherwise GPU-accelerated all-MiniLM-L6-v2 (roadmap item #10):
            #     device="cuda" runs the encoder on the RTX 5090, dropping RAG
            #     retrieval latency from ~80ms (CPU) to ~8ms (GPU); falls back to
            #     CPU when CUDA is unavailable.
            if self._embedder is not None and callable(self._embedder):
                ef = self._embedder
                log.info("CodebaseIndexer: using caller-supplied embedder")
            else:
                if self._embedder is not None:
                    # Not a usable Chroma embedding function (e.g. a raw
                    # VLLMEmbedder, which exposes encode() not __call__). Fall
                    # back rather than silently breaking add()/query() later.
                    log.warning(
                        "CodebaseIndexer: supplied embedder %r is not a callable "
                        "ChromaDB embedding function — using all-MiniLM-L6-v2",
                        type(self._embedder).__name__,
                    )
                try:
                    from chromadb.utils.embedding_functions import (
                        SentenceTransformerEmbeddingFunction,
                    )
                    _device = "cpu"
                    try:
                        import torch
                        if torch.cuda.is_available():
                            _device = "cuda"
                            log.info("CodebaseIndexer: using GPU (CUDA) for embeddings")
                    except ImportError:
                        pass
                    ef = SentenceTransformerEmbeddingFunction(
                        model_name="all-MiniLM-L6-v2",
                        device=_device,
                    )
                except Exception:
                    ef = None  # fall back to ChromaDB's default
            self._embedding_fn = ef

            def _get_col(name):
                # Pin cosine space so query_codebase()'s `score = 1.0 - distance`
                # is genuine cosine similarity (ChromaDB defaults to L2). Applied
                # at creation time only — existing L2 collections keep their space
                # and must be re-indexed to switch.
                kwargs = {
                    "name": name,
                    "configuration": {"hnsw": {"space": "cosine"}},
                }
                if ef is not None:
                    kwargs["embedding_function"] = ef
                return client.get_or_create_collection(**kwargs)

            self._client = client
            self._codebase_col = await asyncio.to_thread(_get_col, self.CODEBASE_COLLECTION)
            self._documents_col = await asyncio.to_thread(_get_col, self.DOCUMENTS_COLLECTION)
            self._available = True

            # Load persisted mtime state
            self._state = self._load_state()
            n_code = await asyncio.to_thread(self._codebase_col.count)
            n_docs = await asyncio.to_thread(self._documents_col.count)
            log.info(
                "CodebaseIndexer: started — codebase=%d chunks, documents=%d chunks",
                n_code, n_docs,
            )
            return True

        except Exception as exc:
            log.warning("CodebaseIndexer.start() failed: %s — RAG unavailable", exc)
            self._available = False
            return False

    async def stop(self) -> None:
        """Release ChromaDB handles and stop file watcher."""
        self.stop_watching()
        self._codebase_col = None
        self._documents_col = None
        self._client = None
        self._available = False
        import gc
        gc.collect()

    def pause(self) -> None:
        """Signal the indexer to skip new index jobs (ResourceGovernor flare mode).
        Any in-progress indexing job runs to completion.
        """
        self._paused = True
        log.info("CodebaseIndexer: paused by ResourceGovernor")

    def resume(self) -> None:
        """Resume normal indexing after a flare ends."""
        self._paused = False
        log.info("CodebaseIndexer: resumed by ResourceGovernor")

    # ---------------------------------------------------------------------- #
    # File watcher (roadmap item #5)
    # ---------------------------------------------------------------------- #

    def start_watching(self) -> bool:
        """Start a background watchdog observer that re-indexes changed files.

        Returns True if watchdog is installed and watcher started.
        Requires: pip install watchdog
        """
        if self._observer is not None:
            return True   # already watching
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

            indexer = self
            loop = asyncio.get_event_loop()

            class _Handler(FileSystemEventHandler):
                def on_modified(self, event):
                    if not event.is_directory:
                        asyncio.run_coroutine_threadsafe(
                            indexer._on_file_changed(Path(event.src_path)),
                            loop,
                        )

                def on_created(self, event):
                    if not event.is_directory:
                        asyncio.run_coroutine_threadsafe(
                            indexer._on_file_changed(Path(event.src_path)),
                            loop,
                        )

                def on_deleted(self, event):
                    if not event.is_directory:
                        asyncio.run_coroutine_threadsafe(
                            indexer._on_file_deleted(Path(event.src_path)),
                            loop,
                        )

            self._observer = Observer()
            self._observer.schedule(_Handler(), str(self._root), recursive=True)
            self._observer.start()
            self._watch_loop = loop
            log.info("CodebaseIndexer: file watcher started on %s", self._root)
            return True
        except ImportError:
            log.info("CodebaseIndexer: watchdog not installed — file watching disabled "
                     "(pip install watchdog to enable)")
            return False
        except Exception as exc:
            log.warning("CodebaseIndexer: failed to start file watcher: %s", exc)
            return False

    def stop_watching(self) -> None:
        """Stop the background file watcher."""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
            log.info("CodebaseIndexer: file watcher stopped")

    async def _on_file_changed(self, path: Path) -> None:
        """Handle file creation/modification event from watchdog thread."""
        if self._paused:
            return
        suffix = path.suffix.lower()
        if suffix not in (".py", ".swift"):
            return
        if any(part in self.EXCLUDE_DIRS for part in path.parts):
            return
        if not path.exists():
            return
        if not self._available:
            return

        try:
            rel = str(path.relative_to(self._root))
            n = await self._index_source_file_async(path)
            mtime = path.stat().st_mtime
            self._state[rel] = mtime
            self._save_state(self._state)
            log.info("CodebaseIndexer: re-indexed %s (%d chunks)", rel, n)
        except Exception as exc:
            log.debug("CodebaseIndexer: _on_file_changed(%s) error: %s", path, exc)

    async def _on_file_deleted(self, path: Path) -> None:
        """Handle file deletion event from watchdog thread."""
        if not self._available:
            return
        try:
            rel = str(path.relative_to(self._root))
            await asyncio.to_thread(self._delete_file_chunks, rel)
            self._state.pop(rel, None)
            self._save_state(self._state)
            log.info("CodebaseIndexer: removed deleted file %s from index", rel)
        except Exception as exc:
            log.debug("CodebaseIndexer: _on_file_deleted(%s) error: %s", path, exc)

    # ---------------------------------------------------------------------- #
    # Indexing
    # ---------------------------------------------------------------------- #

    async def index(self, force: bool = False) -> dict:
        """Incrementally index source files and PDFs.

        Args:
            force: If True, re-index all files regardless of mtime.

        Returns:
            Stats dict: {indexed_files, skipped_files, total_chunks, errors}
        """
        if self._paused:
            log.debug("CodebaseIndexer.index(): skipped — paused by ResourceGovernor")
            return {"skipped": True, "reason": "paused"}
        if not self._available:
            return {"error": "ChromaDB unavailable"}

        stats = {"indexed_files": 0, "skipped_files": 0, "total_chunks": 0, "errors": 0}
        new_state: dict[str, float] = {}

        # ── Source files ──────────────────────────────────────────────────
        for pattern in self.SOURCE_PATTERNS:
            for path in self._root.rglob(pattern):
                # Skip excluded directories
                if any(part in self.EXCLUDE_DIRS for part in path.parts):
                    continue
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    log.debug("CodebaseIndexer: skipping large file %s", path)
                    continue

                rel = str(path.relative_to(self._root))
                mtime = path.stat().st_mtime
                new_state[rel] = mtime

                if not force and self._state.get(rel) == mtime:
                    stats["skipped_files"] += 1
                    continue

                try:
                    # Use ProcessPoolExecutor for CPU-bound AST/regex chunking
                    # (roadmap item #7) — keeps event loop responsive during indexing.
                    n = await self._index_source_file_async(path)
                    stats["indexed_files"] += 1
                    stats["total_chunks"] += n
                except Exception as exc:
                    log.warning("CodebaseIndexer: failed to index %s: %s", rel, exc)
                    stats["errors"] += 1

        # ── PDF documents ─────────────────────────────────────────────────
        docs_dir = self._root / "docs"
        if docs_dir.exists():
            for path in docs_dir.rglob("*.pdf"):
                rel = str(path.relative_to(self._root))
                mtime = path.stat().st_mtime
                new_state[rel] = mtime

                if not force and self._state.get(rel) == mtime:
                    stats["skipped_files"] += 1
                    continue

                try:
                    await asyncio.to_thread(self._index_pdf_file, path)
                    stats["indexed_files"] += 1
                except Exception as exc:
                    log.warning("CodebaseIndexer: failed to index %s: %s", rel, exc)
                    stats["errors"] += 1

        # ── Remove deleted files from index ───────────────────────────────
        deleted = set(self._state) - set(new_state)
        for rel in deleted:
            log.info("CodebaseIndexer: removing deleted file %s from index", rel)
            await asyncio.to_thread(self._delete_file_chunks, rel)

        # Persist state
        self._state = new_state
        self._save_state(new_state)

        log.info(
            "CodebaseIndexer.index(): indexed=%d skipped=%d errors=%d",
            stats["indexed_files"], stats["skipped_files"], stats["errors"],
        )
        return stats

    async def _index_source_file_async(self, path: Path) -> int:
        """Index one source file using the process pool for chunking, then
        run ChromaDB upsert in a thread (ChromaDB is not process-safe)."""
        loop = asyncio.get_event_loop()
        # Chunking is CPU-bound — run in process pool
        try:
            pool = _get_cpu_pool()
            if path.suffix == ".py":
                chunks = await loop.run_in_executor(
                    pool, _chunk_python_standalone, str(path), str(self._root)
                )
            elif path.suffix == ".swift":
                chunks = await loop.run_in_executor(
                    pool, _chunk_swift_standalone, str(path), str(self._root)
                )
            else:
                return 0
        except Exception:
            # Process pool failure — fall back to thread
            chunks = await asyncio.to_thread(
                _chunk_python if path.suffix == ".py" else _chunk_swift,
                path, self._root,
            )

        if not chunks:
            return 0

        # ChromaDB upsert is I/O-bound — run in default thread pool
        return await asyncio.to_thread(self._upsert_source_chunks, path, chunks)

    def _upsert_source_chunks(self, path: Path, chunks: list) -> int:
        """Write chunks to ChromaDB. Runs in thread (not process — ChromaDB not fork-safe)."""
        rel = str(path.relative_to(self._root))
        mtime = path.stat().st_mtime

        self._delete_file_chunks(rel)

        ids, documents, metadatas = [], [], []
        for chunk in chunks:
            chunk_id = _make_id(rel, chunk.name, mtime)
            ids.append(chunk_id)
            documents.append(chunk.text)
            metadatas.append({
                "file": rel,
                "chunk_type": chunk.chunk_type,
                "name": chunk.name,
                "start_line": chunk.start_line,
                "mtime": mtime,
            })

        self._codebase_col.add(documents=documents, metadatas=metadatas, ids=ids)
        log.debug("CodebaseIndexer: indexed %s → %d chunks", rel, len(chunks))
        return len(chunks)

    def _index_source_file(self, path: Path) -> int:
        """Index one .py or .swift file. Returns chunk count. Runs in thread."""
        rel = str(path.relative_to(self._root))
        mtime = path.stat().st_mtime

        # Chunk
        if path.suffix == ".py":
            chunks = _chunk_python(path, self._root)
        elif path.suffix == ".swift":
            chunks = _chunk_swift(path, self._root)
        else:
            return 0

        if not chunks:
            return 0

        # Delete any existing chunks for this file
        self._delete_file_chunks(rel)

        # Add new chunks
        ids, documents, metadatas = [], [], []
        for chunk in chunks:
            chunk_id = _make_id(rel, chunk.name, mtime)
            ids.append(chunk_id)
            documents.append(chunk.text)
            metadatas.append({
                "file": rel,
                "chunk_type": chunk.chunk_type,
                "name": chunk.name,
                "start_line": chunk.start_line,
                "mtime": mtime,
            })

        self._codebase_col.add(documents=documents, metadatas=metadatas, ids=ids)
        log.debug("CodebaseIndexer: indexed %s → %d chunks", rel, len(chunks))
        return len(chunks)

    def _index_pdf_file(self, path: Path) -> int:
        """Index one PDF file. Returns page count. Runs in thread."""
        rel = str(path.relative_to(self._root))
        mtime = path.stat().st_mtime
        chunks = _chunk_pdf(path, self._root)
        if not chunks:
            return 0

        self._delete_pdf_chunks(rel)

        ids, documents, metadatas = [], [], []
        for chunk in chunks:
            chunk_id = _make_id(rel, chunk.name, mtime)
            ids.append(chunk_id)
            documents.append(chunk.text)
            metadatas.append({
                "file": rel,
                "page": chunk.page,
                "mtime": mtime,
            })

        self._documents_col.add(documents=documents, metadatas=metadatas, ids=ids)
        log.debug("CodebaseIndexer: indexed PDF %s → %d pages", rel, len(chunks))
        return len(chunks)

    def _delete_file_chunks(self, rel: str) -> None:
        """Remove all codebase chunks for `rel`. Runs in thread."""
        try:
            if self._codebase_col is None:
                return
            result = self._codebase_col.get(where={"file": {"$eq": rel}})
            ids = result.get("ids", [])
            if ids:
                self._codebase_col.delete(ids=ids)
        except Exception as exc:
            log.debug("CodebaseIndexer: _delete_file_chunks(%s) failed: %s", rel, exc)

    def _delete_pdf_chunks(self, rel: str) -> None:
        """Remove all document chunks for `rel`. Runs in thread."""
        try:
            if self._documents_col is None:
                return
            result = self._documents_col.get(where={"file": {"$eq": rel}})
            ids = result.get("ids", [])
            if ids:
                self._documents_col.delete(ids=ids)
        except Exception as exc:
            log.debug("CodebaseIndexer: _delete_pdf_chunks(%s) failed: %s", rel, exc)

    # ---------------------------------------------------------------------- #
    # Query API
    # ---------------------------------------------------------------------- #

    async def query_codebase(self, query: str, n: int = 5) -> list[dict]:
        """Semantic search over Python/Swift source chunks.

        Returns list of:
          {file, chunk_type, name, start_line, text, score}
        where score is 1.0 - cosine_distance (higher = more similar).
        """
        if not self._available or self._codebase_col is None:
            return []
        try:
            results = await asyncio.to_thread(
                self._codebase_col.query,
                query_texts=[query],
                n_results=min(n, max(1, await asyncio.to_thread(self._codebase_col.count))),
            )
            return _unpack_results(results, score_key="score")
        except Exception as exc:
            log.warning("CodebaseIndexer.query_codebase() failed: %s", exc)
            return []

    async def query_docs(self, query: str, n: int = 5) -> list[dict]:
        """Semantic search over PDF document pages.

        Returns list of:
          {file, page, text, score}
        """
        if not self._available or self._documents_col is None:
            return []
        try:
            count = await asyncio.to_thread(self._documents_col.count)
            if count == 0:
                return []
            results = await asyncio.to_thread(
                self._documents_col.query,
                query_texts=[query],
                n_results=min(n, count),
            )
            return _unpack_results(results, score_key="score")
        except Exception as exc:
            log.warning("CodebaseIndexer.query_docs() failed: %s", exc)
            return []

    async def query_combined(self, query: str, n: int = 5) -> list[dict]:
        """Query both collections and return merged results sorted by score."""
        code_hits, doc_hits = await asyncio.gather(
            self.query_codebase(query, n),
            self.query_docs(query, n),
        )
        combined = code_hits + doc_hits
        combined.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return combined[:n]

    # ---------------------------------------------------------------------- #
    # State persistence
    # ---------------------------------------------------------------------- #

    def _load_state(self) -> dict[str, float]:
        try:
            if self._state_path.exists():
                return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.debug("CodebaseIndexer: failed to load state: %s", exc)
        return {}

    def _save_state(self, state: dict[str, float]) -> None:
        try:
            self._state_path.write_text(
                json.dumps(state, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.debug("CodebaseIndexer: failed to save state: %s", exc)

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    async def get_status(self) -> dict:
        if not self._available:
            return {"available": False}
        try:
            n_code = await asyncio.to_thread(self._codebase_col.count)
            n_docs = await asyncio.to_thread(self._documents_col.count)
            return {
                "available": True,
                "codebase_chunks": n_code,
                "document_chunks": n_docs,
                "indexed_files": len(self._state),
            }
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    @property
    def available(self) -> bool:
        return self._available


# ---------------------------------------------------------------------------
# Process-pool-safe standalone chunking functions
# These are module-level functions (not methods) so they can be pickled
# for use with ProcessPoolExecutor.
# ---------------------------------------------------------------------------

def _chunk_python_standalone(path_str: str, root_str: str) -> list:
    """Pickle-safe wrapper for _chunk_python — called in process pool workers."""
    return _chunk_python(Path(path_str), Path(root_str))


def _chunk_swift_standalone(path_str: str, root_str: str) -> list:
    """Pickle-safe wrapper for _chunk_swift — called in process pool workers."""
    return _chunk_swift(Path(path_str), Path(root_str))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_id(file: str, name: str, mtime: float) -> str:
    key = f"{file}::{name}::{mtime:.3f}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def _unpack_results(results: dict, score_key: str = "score") -> list[dict]:
    """Convert ChromaDB query result dict into flat list of hit dicts."""
    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    hits = []
    for doc, meta, dist in zip(documents, metadatas, distances):
        hit = dict(meta)   # file, chunk_type, name, start_line, etc.
        hit["text"] = doc
        hit[score_key] = round(1.0 - dist, 4)  # cosine similarity
        hits.append(hit)
    return hits


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_INDEXER: Optional[CodebaseIndexer] = None


def get_indexer(project_root: str = ".", chroma_dir: Optional[str] = None) -> CodebaseIndexer:
    """Return the process-wide CodebaseIndexer singleton."""
    global _INDEXER
    if _INDEXER is None:
        _INDEXER = CodebaseIndexer(project_root=project_root, chroma_dir=chroma_dir)
    return _INDEXER


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI: python codebase_indexer.py [--reindex] [--query TEXT] [--docs]"""
    import sys
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="CodebaseIndexer CLI")
    parser.add_argument("--reindex", action="store_true",
                        help="Force full re-index (ignore mtime cache)")
    parser.add_argument("--query", metavar="TEXT",
                        help="Query the codebase index and print top hits")
    parser.add_argument("--docs", action="store_true",
                        help="Query documents collection instead of codebase")
    parser.add_argument("--n", type=int, default=5, help="Number of results")
    parser.add_argument("--root", default=".", help="Project root path")
    args = parser.parse_args()

    async def _run():
        indexer = CodebaseIndexer(project_root=args.root)
        if not await indexer.start():
            print("ChromaDB unavailable — install: pip install chromadb sentence-transformers")
            return

        if args.query is None or args.reindex:
            print("Indexing…")
            t0 = time.monotonic()
            stats = await indexer.index(force=args.reindex)
            print(f"Done in {time.monotonic() - t0:.1f}s: {stats}")

        if args.query:
            print(f"\nQuery: {args.query!r}")
            if args.docs:
                hits = await indexer.query_docs(args.query, n=args.n)
                label = "documents"
            else:
                hits = await indexer.query_codebase(args.query, n=args.n)
                label = "codebase"
            print(f"\nTop {len(hits)} results from {label}:")
            for i, h in enumerate(hits, 1):
                if args.docs:
                    print(f"\n{i}. [{h.get('file')} p.{h.get('page')}]  score={h.get('score'):.3f}")
                else:
                    print(f"\n{i}. [{h.get('chunk_type')}] {h.get('file')}::{h.get('name')}  "
                          f"line {h.get('start_line')}  score={h.get('score'):.3f}")
                snippet = (h.get("text") or "")[:200].replace("\n", " ")
                print(f"   {snippet}…")

        await indexer.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
