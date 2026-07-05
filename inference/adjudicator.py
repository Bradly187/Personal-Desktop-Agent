"""Deny-Only Local Adjudicator for DevAgent (CG-4)."""

import logging
import os

log = logging.getLogger(__name__)

PROMPT = """You are evaluating a failed agent task that was rolled back.
We want to filter out useless tasks (e.g., hallucinations, impossible constraints) so the human doesn't have to review them.

Goal: {goal}
Reason: {reason}
Failed Action: {failed_action}
Detail (Trajectory): {detail}

Is this failure due to a hallucination, an impossible constraint, or a trivial error that can be safely discarded?
Reply exactly DISMISS if it should be discarded, or ESCALATE if a human MUST see it.
"""

class LocalAdjudicator:
    def __init__(self, db, router):
        self._db = db
        self._router = router
        self._evaluated_ids: set[int] = set()
        
    def is_enabled(self) -> bool:
        return os.environ.get("DA_AUTO_ADJUDICATE", "0").strip().lower() in ("1", "true", "yes", "on")

    async def adjudicate_pending(self) -> int:
        """Evaluate pending escalations. Returns number dismissed."""
        if not self.is_enabled():
            return 0
            
        try:
            pending = await self._db.get_pending_escalations(limit=20)
        except Exception as exc:
            log.warning("LocalAdjudicator failed to fetch: %s", exc)
            return 0

        dismissed = 0
        for esc in pending:
            eid = esc["id"]
            if eid in self._evaluated_ids:
                continue
            
            self._evaluated_ids.add(eid)
            
            prompt = PROMPT.format(
                goal=esc.get("goal", ""),
                reason=esc.get("reason", ""),
                failed_action=esc.get("failed_action", ""),
                detail=esc.get("detail", "")
            )
            
            try:
                # Use the fast plan domain
                res = await self._router.infer(domain="plan", user_text=prompt, context=None)
                if res and res.ok and res.text:
                    decision = res.text.strip().upper()
                    if "DISMISS" in decision and "ESCALATE" not in decision:
                        await self._db.resolve_escalations(status="auto_dismissed", escalation_id=eid)
                        dismissed += 1
                        log.info("LocalAdjudicator auto-dismissed escalation %d", eid)
                    else:
                        log.debug("LocalAdjudicator escalated %d", eid)
            except Exception as exc:
                log.warning("LocalAdjudicator evaluation failed for %d: %s", eid, exc)
                
        return dismissed
