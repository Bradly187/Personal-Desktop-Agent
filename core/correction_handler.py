import logging
from typing import Callable, Optional, TYPE_CHECKING
from core.command_executor import Command
from core.async_utils import fire_and_log
from core.coordinator_state import CoordinatorState

if TYPE_CHECKING:
    from storage.db import AgentDB
    from adaptive.continuous_trainer import ContinuousTrainer
    from monitoring.audit_log import AuditLog

log = logging.getLogger(__name__)

class CorrectionHandler:
    """Handles intent drift detection and user command correction feedback loops."""
    _DRIFT_SIM_THRESHOLD = 0.3
    _DRIFT_STREAK_TRIGGER = 3
    _domain_classifier = None

    def __init__(
        self,
        *,
        state: CoordinatorState,
        agent_db: Callable[[], Optional["AgentDB"]],
        trainer: Callable[[], Optional["ContinuousTrainer"]],
        audit: Callable[[], Optional["AuditLog"]],
        tts_speak: Callable[[str], object],
        session_id: int
    ):
        self._state = state
        self._agent_db = agent_db
        self._trainer = trainer
        self._audit = audit
        self._tts_speak = tts_speak
        self._session_id = session_id

    @classmethod
    def _get_domain_classifier(cls):
        if cls._domain_classifier is None:
            from core.domain_classifier import DomainClassifier
            cls._domain_classifier = DomainClassifier()
        return cls._domain_classifier

    def note_intent_drift(self, cmd: Command) -> None:
        text = (cmd.text or "").strip()
        if not text:
            return
        if self._state.session_intent is None:
            self._state.session_intent = text
            return
        from storage.db import _tokens, _jaccard
        sim = _jaccard(_tokens(self._state.session_intent), _tokens(text))
        if sim >= self._DRIFT_SIM_THRESHOLD:
            self._state.drift_streak = 0
            return
        self._state.drift_streak += 1
        if self._state.drift_streak < self._DRIFT_STREAK_TRIGGER or self._state.drift_warned:
            return
        self._state.drift_warned = True
        log.warning(
            "HybridCoordinator: intent drift — %r diverges from opening %r (sim=%.2f)",
            text[:60], self._state.session_intent[:60], sim,
        )
        db = self._agent_db()
        if db and db.available:
            fire_and_log(
                db.logs.insert_drift(
                    self._session_id, getattr(cmd, "trace_id", None), sim,
                    self._state.session_intent, text),
                log, label="insert drift")
        audit = self._audit()
        if audit and getattr(audit, "available", False):
            fire_and_log(
                audit.log_security_event(
                    detail=f"intent drift: command diverges from session intent (sim={sim:.2f})",
                    severity="warning",
                    params={"original_intent": self._state.session_intent[:120],
                            "current_command": text[:120],
                            "drift_score": round(sim, 3)}),
                log, label="audit drift")
        nudge = f"You started by asking about {self._state.session_intent[:48]}. Are we still on track?"
        fire_and_log(self._tts_speak(nudge), log, label="drift nudge")

    def harvest_correction(self, cmd: Command, prior_action: str) -> None:
        db = self._agent_db()
        if not (db and db.available):
            return
        try:
            domain = self._get_domain_classifier().classify(cmd.text)
        except Exception:
            domain = None
        fire_and_log(
            db.logs.insert_correction(
                self._session_id, getattr(cmd, "trace_id", None),
                cmd.text or "", prior_action or "", domain),
            log, label="harvest correction")

    async def on_correction(self, cmd: Command, correct_action: str) -> None:
        try:
            self.harvest_correction(cmd, self._state.last_executed_action)
            db = self._agent_db()
            if db and db.available and self._state.last_command_id != -1:
                fire_and_log(
                    db.commands.mark_command_corrected(self._state.last_command_id, correct_action),
                    log, label="mark command corrected"
                )

            trainer = self._trainer()
            if not trainer:
                return
            await trainer.record_correction(
                cmd=cmd,
                wrong_action=self._state.last_executed_action,
                correct_action=correct_action,
                command_id=self._state.last_command_id,
            )
            log.info(
                "HybridCoordinator: correction recorded %r → %r",
                self._state.last_executed_action, correct_action,
            )
        except Exception as exc:
            log.warning("HybridCoordinator.on_correction failed: %s", exc)

    async def correct(self, cmd: Command, wrong_action: str, correct_action: str) -> dict:
        log.info(
            "Correction received: %r → was %s, should be %s",
            cmd.text, wrong_action, correct_action,
        )

        trainer = self._trainer()
        if trainer:
            await trainer.record_correction(cmd, wrong_action, correct_action)

        self.harvest_correction(cmd, wrong_action)

        return {
            "status": "ok",
            "correction": {
                "text": cmd.text,
                "wrong": wrong_action,
                "correct": correct_action,
            },
        }
