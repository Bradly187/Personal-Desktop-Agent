"""Smoke test: RemoteIndexerClient -> RemoteIndexerService (must be running on :9000)."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from inference.remote_indexer_client import RemoteIndexerClient


async def main():
    c = RemoteIndexerClient("http://127.0.0.1:9000", timeout=15)
    hits = await c.query_combined("fusion engine sensor priority", n=3)
    print("combined hits:", len(hits))
    for h in hits[:3]:
        print("  ", h.get("file"), h.get("chunk_type"), "score=%.2f" % h.get("score", 0))
    # shape check: each hit is a dict with at least file + score (matches CodebaseIndexer)
    for h in hits:
        assert isinstance(h, dict) and "file" in h and "score" in h
    code = await c.query_codebase("WhisperStream transcribe", n=2)
    print("codebase hits:", len(code))
    print("REMOTE_INDEXER_SMOKE_OK")


if __name__ == "__main__":
    asyncio.run(main())
