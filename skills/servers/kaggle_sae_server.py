"""kaggle_sae_server — MCP skill wrapping the Kaggle SAE (Simulated Agent Exam) API.

GAP-8 (external calibration / standardized benchmarks). The whitepaper notes
Kaggle SAE deploys agents via a SKILL.md file — our MCP-client skill model
supports this exact shape. This server exposes the four SAE lifecycle tools so
the agent can register, fetch an exam, submit answers, and read its score,
giving an external calibration signal alongside the in-house evals/ suite.

Configuration (env):
  KAGGLE_SAE_BASE_URL   API base (default https://www.kaggle.com/api/v1/sae)
  KAGGLE_SAE_API_KEY    bearer token; without it every tool returns a clear
                        "not configured" message rather than erroring.

Disabled by default (skills/manifests/kaggle_sae.json `enabled: false`) — it
makes outbound network calls, so it is opt-in. Stdlib-only (urllib), no new dep.

Run standalone:  python -m skills.servers.kaggle_sae_server
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kaggle-sae-skill")

_DEFAULT_BASE = "https://www.kaggle.com/api/v1/sae"
_TIMEOUT_S = 15.0


def _base_url() -> str:
    return os.environ.get("KAGGLE_SAE_BASE_URL", _DEFAULT_BASE).rstrip("/")


def _request(method: str, path: str, payload: dict | None = None) -> str:
    """Issue an authenticated SAE API call; return a result/diagnostic string."""
    key = os.environ.get("KAGGLE_SAE_API_KEY", "").strip()
    if not key:
        return ("Kaggle SAE not configured — set KAGGLE_SAE_API_KEY (and "
                "optionally KAGGLE_SAE_BASE_URL) to enable this skill.")
    url = f"{_base_url()}/{path.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": "DesktopAgent-SAE/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return body or "(empty response)"
    except urllib.error.HTTPError as exc:
        return f"SAE API error {exc.code}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return f"SAE API request failed: {exc}"


@mcp.tool()
def register_agent(name: str, description: str = "") -> str:
    """Register this agent with the Kaggle SAE service. Returns the agent record."""
    return _request("POST", "/agents",
                    {"name": name, "description": description})


@mcp.tool()
def fetch_exam(exam_id: str) -> str:
    """Fetch the questions/tasks for a Kaggle SAE exam by id (read-only)."""
    return _request("GET", f"/exams/{exam_id}")


@mcp.tool()
def submit_answer(exam_id: str, question_id: str, answer: str) -> str:
    """Submit an answer for one exam question (egress/'send')."""
    return _request("POST", f"/exams/{exam_id}/answers",
                    {"question_id": question_id, "answer": answer})


@mcp.tool()
def get_score(exam_id: str) -> str:
    """Read the current score for a submitted Kaggle SAE exam (read-only)."""
    return _request("GET", f"/exams/{exam_id}/score")


if __name__ == "__main__":
    mcp.run(transport="stdio")
