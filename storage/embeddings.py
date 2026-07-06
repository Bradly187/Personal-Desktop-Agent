import asyncio
import logging
import math
from typing import Optional

log = logging.getLogger(__name__)

_ENCODER = None
_ENCODER_FAILED = False
_ENCODER_LOCK = asyncio.Lock()

def _load_encoder_sync():
    """Load all-MiniLM-L6-v2 synchronously (called via asyncio.to_thread)."""
    from sentence_transformers import SentenceTransformer
    enc = SentenceTransformer("all-MiniLM-L6-v2")
    log.info("MiniLM loaded — semantic few-shot retrieval enabled (384-dim cosine)")
    return enc


async def _get_encoder() -> Optional[object]:
    """Return the cached encoder, loading it on first call (non-blocking).

    Uses a double-checked lock so only one coroutine pays the model-load cost
    even when multiple callers race on the first call (#13).
    """
    global _ENCODER, _ENCODER_FAILED
    # Fast path: already loaded or permanently failed.
    if _ENCODER is not None:
        return _ENCODER
    if _ENCODER_FAILED:
        return None
    async with _ENCODER_LOCK:
        # Re-check inside the lock: another coroutine may have loaded it.
        if _ENCODER is not None:
            return _ENCODER
        if _ENCODER_FAILED:
            return None
        try:
            _ENCODER = await asyncio.to_thread(_load_encoder_sync)
            return _ENCODER
        except Exception as exc:
            _ENCODER_FAILED = True
            log.debug("MiniLM unavailable — falling back to Jaccard scoring: %s", exc)
            return None


def _encode_sync(text: str, encoder) -> bytes:
    """Encode text to normalised float32 bytes (384-dim)."""
    import numpy as np
    vec = encoder.encode(text, normalize_embeddings=True)
    return vec.astype(np.float32).tobytes()


def _cosine(a: bytes, b: bytes) -> float:
    """Cosine similarity between two normalised float32 BLOBs. Already unit-length → dot product."""
    import numpy as np
    va = np.frombuffer(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    return float(np.dot(va, vb))
def _tokens(text: str) -> set[str]:
    return set(text.lower().split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _recency_weight(ts: float, now: float, half_life_days: float = 30.0) -> float:
    age_days = (now - ts) / 86400.0
    return math.exp(-age_days * math.log(2) / half_life_days)


def _fse_score(row: dict, query_tokens: set[str], now: float) -> float:
    overlap = _jaccard(query_tokens, _tokens(row["text"]))
    recency = _recency_weight(row["ts"], now)
    usage = math.log1p(row["usage_count"])
    return overlap * recency * usage
