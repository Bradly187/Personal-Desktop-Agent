import asyncio
import json
import logging
import time
from pathlib import Path

from inference.dev_common import _get_content_filter, _get_trust_classifier, _RAG_OPEN_FENCE, _RAG_CLOSE_FENCE
from inference.plan_parser import AgentStep, AgentResult

log = logging.getLogger(__name__)

def parse_skill_args(raw: str, body: str = "") -> tuple[str, str, dict]:
    parts = (raw or "").split(None, 2)
    skill_id = parts[0] if parts else ""
    tool = parts[1] if len(parts) > 1 else ""
    blob = parts[2] if len(parts) > 2 else (body or "")
    args: dict = {}
    if blob.strip():
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, dict):
                args = parsed
        except (ValueError, TypeError):
            args = {}
    return skill_id, tool, args

def build_skill_args(text: str, match: dict, schema: dict) -> dict:
    props = (schema or {}).get("properties", {})
    required = (schema or {}).get("required", [])
    payload = text
    kw = (match or {}).get("keyword")
    if kw:
        idx = text.lower().find(kw.lower())
        if idx >= 0:
            payload = (text[:idx] + text[idx + len(kw):]).strip()
    str_required = [p for p in required if props.get(p, {}).get("type") == "string"]
    if len(str_required) == 1:
        return {str_required[0]: payload or text}
    return {}

async def execute_skill_step(executor, step: AgentStep) -> str:
    if getattr(executor, "_skill_registry", None) is None:
        return "No skills available (registry not wired)"

    skill_id, tool, args = parse_skill_args(step.args, step.body)
    if not skill_id or not tool:
        return "SKILL step needs '<skill_id> <tool> {json args}'"

    is_send = (step.action.upper() == "SKILL_CALL"
               or executor._skill_registry.is_send_tool(skill_id, tool))

    if is_send:
        try:
            clean_blob, findings = _get_content_filter().scrub_sync(json.dumps(args))
            scrubbed = json.loads(clean_blob)
            if isinstance(scrubbed, dict):
                args = scrubbed
            if findings:
                log.info("Skill send: scrubbed %d secret(s) from %s.%s payload",
                         len(findings), skill_id, tool)
        except Exception as exc:
            log.debug("Skill send scrub failed (%s) — proceeding with raw args", exc)
        if not await executor._agent._confirm_destructive_op(
            f"Approve sending via skill {skill_id}.{tool}?"
        ):
            return f"SKILL_CALL {skill_id}.{tool} cancelled by user"

    try:
        result = await asyncio.wait_for(
            executor._skill_registry.call(skill_id, tool, args),
            timeout=getattr(executor, "SKILL_CALL_TIMEOUT_S", 60.0),
        )
    except asyncio.TimeoutError:
        timeout_s = getattr(executor, "SKILL_CALL_TIMEOUT_S", 60.0)
        log.warning("Skill %s.%s timed out after %ds",
                    skill_id, tool, timeout_s)
        return f"Skill {skill_id}.{tool} timed out after {timeout_s}s"
    text = result.get("text", "") if isinstance(result, dict) else str(result)

    if not is_send and text:
        try:
            verdict = _get_trust_classifier().classify_sync(
                f"skill:{skill_id}.{tool}", text
            )
            if verdict.should_block:
                log.warning("Skill %s.%s result quarantined (trust=HIGH)",
                            skill_id, tool)
                await audit_skill(executor, skill_id, tool, is_send, result, blocked=True)
                return "[skill result withheld — flagged as potentially unsafe]"
        except Exception as exc:
            log.debug("Skill taint check failed: %s", exc)

    await audit_skill(executor, skill_id, tool, is_send, result, blocked=False)
    if isinstance(result, dict) and result.get("status") == "error":
        return f"SKILL error: {result.get('error', 'unknown')}"
    return text or "(no output)"

async def audit_skill(executor, skill_id: str, tool: str, is_send: bool,
                       result: dict, blocked: bool) -> None:
    if getattr(executor, "_agent_db", None) is None:
        return
    try:
        summary = ""
        if isinstance(result, dict):
            summary = (result.get("text") or result.get("error") or "")[:300]
        await executor._agent_db.skills.log_skill_invocation(
            skill_id=skill_id, tool_name=tool, send=is_send,
            status=(result.get("status", "?") if isinstance(result, dict) else "?"),
            blocked=blocked, result_summary=summary,
        )
    except Exception as exc:
        log.debug("Skill audit write failed: %s", exc)

async def handle_skill(executor, text: str) -> AgentResult:
    t0 = time.monotonic()
    match = executor._skill_registry.match_intent(text)
    if match.get("plan"):
        return await executor.plan_and_run(text)
    schema = executor._skill_registry.tool_schema(match["skill_id"], match["tool"])
    args = build_skill_args(text, match, schema)
    step = AgentStep(
        action="SKILL_CALL" if match["send"] else "SKILL_QUERY",
        args=f"{match['skill_id']} {match['tool']} {json.dumps(args)}",
    )
    result_text = await execute_skill_step(executor, step)

    if (not match["send"] and match.get("summarize") and result_text
            and "withheld" not in result_text.lower()):
        try:
            r = await executor._router.infer(
                domain="general",
                user_text=f"Summarize these items concisely for the user:\n\n{result_text}",
            )
            if getattr(r, "ok", False) and getattr(r, "text", ""):
                result_text = r.text
        except Exception as exc:
            log.debug("Skill summarise failed: %s", exc)

    if not match["send"] and result_text:
        try:
            from tts.polly_stream import get_client as _get_tts
            asyncio.create_task(_get_tts().speak(result_text))
        except Exception as exc:
            log.debug("Skill TTS failed: %s", exc)

    result = AgentResult(
        goal=text,
        domain="skill",
        model_used=f"skill:{match['skill_id']}.{match['tool']}",
        response_text=result_text,
        total_latency_ms=(time.monotonic() - t0) * 1000,
    )
    executor._results_log.append(result)
    return result

async def handle_personal_query(executor, text: str) -> AgentResult:
    t0 = time.monotonic()
    hits = await executor._personal_kb.query(text, n=4)

    if not hits:
        answer = "I couldn't find anything about that in your documents."
        spoken = answer
        model_used = "personal_kb"
    else:
        lines = []
        fnames: list[str] = []
        for h in hits:
            fname = Path(h["file"]).name
            if fname not in fnames:
                fnames.append(fname)
            lines.append(f"# {fname} — {h.get('name', '')}")
            lines.append((h.get("text") or "")[:600])
            lines.append("")
        context = f"{_RAG_OPEN_FENCE}\n" + "\n".join(lines) + f"\n{_RAG_CLOSE_FENCE}"
        model_used = "personal_kb"
        answer = context
        spoken = (f"I found {len(hits)} matching passage"
                  f"{'s' if len(hits) != 1 else ''} in "
                  f"{', '.join(fnames[:3])}, but couldn't summarize them.")
        try:
            r = await executor._router.infer(
                domain="general",
                user_text=(f"Answer the user's question using ONLY the retrieved "
                           f"excerpts from their personal documents below. Quote "
                           f"the source file names. Question: {text}\n\n{context}"),
            )
            if getattr(r, "ok", False) and getattr(r, "text", ""):
                answer = r.text
                spoken = r.text
                model_used = r.model
        except Exception as exc:
            log.debug("PersonalKB synthesis failed (%s) — returning raw excerpts", exc)

    try:
        from tts.polly_stream import get_client as _get_tts
        asyncio.create_task(_get_tts().speak(spoken))
    except Exception as exc:
        log.debug("PersonalKB TTS failed: %s", exc)

    result = AgentResult(
        goal=text,
        domain="personal",
        model_used=model_used,
        response_text=answer,
        total_latency_ms=(time.monotonic() - t0) * 1000,
    )
    executor._results_log.append(result)
    return result
