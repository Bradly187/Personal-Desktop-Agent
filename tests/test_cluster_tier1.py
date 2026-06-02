"""Quick functional checks for Tier 1 cluster offload.

Run from project root:  python tests/test_cluster_tier1.py
Exercises the real laptop Ollama (must be reachable) for the offload path.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cluster_config import ClusterConfig, _clean_url
from core.cluster_health import ClusterHealthMonitor
from inference.model_router import ModelRouter


def test_config_load_and_helpers():
    # disabled when file missing
    cfg = ClusterConfig.load(os.path.join(tempfile.gettempdir(), "no_such_cluster.json"))
    assert cfg.enabled is False
    assert cfg.offload_lightweight is False

    # real project config
    cfg2 = ClusterConfig.load()  # project-root cluster_config.json
    assert cfg2.enabled is True, "expected project cluster_config.json to load"
    assert cfg2.lightweight_host == "laptop"
    assert cfg2.offload_lightweight is True
    assert cfg2.laptop_ollama_url.endswith(":11434")

    assert _clean_url("  http://x:1/  ") == "http://x:1"
    assert _clean_url(None) is None
    print("OK  config load + helpers")


def test_should_offload_logic():
    cfg = ClusterConfig.load()
    router = ModelRouter()

    # No cluster wired → never offload
    assert router._should_offload("command") is False

    class FakeHealth:
        def __init__(self, up): self.up = up
        def is_healthy(self, svc): return self.up

    # Healthy laptop → offload command, but not specialist domains
    router.set_cluster(cfg, FakeHealth(True))
    assert router._should_offload("command") is True
    assert router._should_offload("code") is False

    # Unhealthy laptop → no offload
    router.set_cluster(cfg, FakeHealth(False))
    assert router._should_offload("command") is False

    # Offload disabled in config → no offload even if healthy
    disabled = ClusterConfig(enabled=True, lightweight_host="desktop",
                             laptop_ollama_url="http://x:11434")
    router.set_cluster(disabled, FakeHealth(True))
    assert router._should_offload("command") is False
    print("OK  _should_offload logic")


async def test_health_monitor_real():
    cfg = ClusterConfig.load()
    mon = ClusterHealthMonitor(cfg, interval=999, timeout=3)
    await mon._check_all()
    st = mon.status()
    print("    health snapshot:", st)
    assert st.get("laptop_ollama") is True, "laptop Ollama should be reachable"
    print("OK  health monitor sees laptop Ollama")
    return cfg, mon


async def test_offload_end_to_end():
    cfg = ClusterConfig.load()
    mon = ClusterHealthMonitor(cfg, interval=999, timeout=3)
    await mon._check_all()
    router = ModelRouter()
    router.set_cluster(cfg, mon)
    res = await router.infer("command", "open the web browser")
    print("    backend=%s model=%s text=%r" % (res.backend, res.model, res.text[:80]))
    assert res.backend == "ollama-laptop", "command infer should offload to laptop"
    assert res.model == "llama3.1:8b"
    assert res.error is None
    print("OK  end-to-end command offload to laptop")


def main():
    test_config_load_and_helpers()
    test_should_offload_logic()
    asyncio.run(test_health_monitor_real())
    asyncio.run(test_offload_end_to_end())
    print("\nALL TIER1 CHECKS PASSED")


if __name__ == "__main__":
    main()
