"""mcp_trust_classifier — taint analysis for MCP tool results.

Scans all MCP tool outputs before they enter the next LLM reasoning step.
Flags injection-like patterns that could manipulate agent behavior.

Threat model:
  - Untrusted data flows into MCP tool results (email content, document text,
    Slack messages, web page content, file contents from user directories).
  - An attacker embeds instructions in that data hoping the LLM will follow them.
  - This classifier detects those patterns and flags them for human review.

Detection categories:
  - prompt_injection: "ignore previous instructions", "you are now", role-play triggers
  - command_injection: shell metacharacters, backticks, $() in unexpected contexts
  - data_exfiltration: URLs with encoded data, base64 blobs in outputs
  - privilege_escalation: requests to change permissions, access other users, sudo

Design:
  - Returns a TrustVerdict with risk level and matched patterns.
  - HIGH risk → block and require human approval before proceeding.
  - MEDIUM risk → log warning, proceed with caution flag in context.
  - LOW risk → pass through, log for audit trail.

Usage:
    from mcp_trust_classifier import MCPTrustClassifier, RiskLevel

    tc = MCPTrustClassifier(audit_log=audit)
    verdict = await tc.classify(tool_name="read_email", result=email_body)
    if verdict.risk == RiskLevel.HIGH:
        # Block — require human approval
        pass
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from audit_log import AuditLog

log = logging.getLogger(__name__)


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class TrustVerdict:
    """Result of trust classification on MCP tool output."""
    risk: RiskLevel
    tool_name: str
    flags: list[str] = field(default_factory=list)
    matched_patterns: list[str] = field(default_factory=list)
    recommendation: str = "proceed"  # "proceed", "caution", "block"

    @property
    def should_block(self) -> bool:
        return self.risk == RiskLevel.HIGH

    @property
    def should_warn(self) -> bool:
        return self.risk == RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

@dataclass
class _ThreatPattern:
    name: str
    category: str  # prompt_injection, command_injection, data_exfil, priv_escalation
    regex: re.Pattern
    risk: RiskLevel
    description: str


_THREAT_PATTERNS: list[_ThreatPattern] = [
    # Prompt injection
    _ThreatPattern(
        "ignore_instructions",
        "prompt_injection",
        re.compile(r'(?i)ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions'),
        RiskLevel.HIGH,
        "Attempt to override system prompt",
    ),
    _ThreatPattern(
        "new_role",
        "prompt_injection",
        re.compile(r'(?i)you\s+are\s+now\s+(?:a|an|the)\s+'),
        RiskLevel.HIGH,
        "Role reassignment attempt",
    ),
    _ThreatPattern(
        "system_prompt_override",
        "prompt_injection",
        re.compile(r'(?i)(?:system\s*prompt|new\s+instructions?|override\s+(?:your|the)\s+(?:rules|instructions))'),
        RiskLevel.HIGH,
        "System prompt manipulation attempt",
    ),
    _ThreatPattern(
        "do_not_reveal",
        "prompt_injection",
        re.compile(r'(?i)(?:do\s+not|don\'t|never)\s+(?:reveal|tell|show|disclose)\s+(?:this|these|the)\s+(?:instructions|rules|prompt)'),
        RiskLevel.MEDIUM,
        "Instruction concealment pattern (may be legitimate)",
    ),
    _ThreatPattern(
        "jailbreak_delimiter",
        "prompt_injection",
        re.compile(r'(?i)(?:\[/?INST\]|\[/?SYS\]|<\|(?:im_start|im_end|system|user)\|>|<<SYS>>)'),
        RiskLevel.HIGH,
        "LLM delimiter injection (ChatML/Llama format)",
    ),
    _ThreatPattern(
        "roleplay_trigger",
        "prompt_injection",
        re.compile(r'(?i)(?:pretend|act\s+as\s+if|imagine\s+you\s+are|from\s+now\s+on\s+you)'),
        RiskLevel.MEDIUM,
        "Roleplay/persona manipulation",
    ),

    # Command injection
    _ThreatPattern(
        "shell_metachar",
        "command_injection",
        re.compile(r'(?:;\s*(?:rm|curl|wget|nc|bash|sh|python|powershell)\s)|(?:\|\s*(?:bash|sh|python))'),
        RiskLevel.HIGH,
        "Shell command injection via metacharacters",
    ),
    _ThreatPattern(
        "backtick_exec",
        "command_injection",
        re.compile(r'`[^`]{5,}`'),
        RiskLevel.MEDIUM,
        "Backtick command execution pattern",
    ),
    _ThreatPattern(
        "subshell_exec",
        "command_injection",
        re.compile(r'\$\([^)]{5,}\)'),
        RiskLevel.MEDIUM,
        "Subshell execution pattern",
    ),

    # Data exfiltration
    _ThreatPattern(
        "encoded_url_data",
        "data_exfil",
        re.compile(r'https?://[^\s]+\?[^\s]*(?:data|payload|exfil|token)=[A-Za-z0-9+/=%]{20,}'),
        RiskLevel.HIGH,
        "URL with suspicious encoded data parameter",
    ),
    _ThreatPattern(
        "webhook_exfil",
        "data_exfil",
        re.compile(r'(?i)(?:webhook|requestbin|pipedream|hookbin|burpcollaborator)\.(?:com|net|io|site)'),
        RiskLevel.HIGH,
        "Known data exfiltration endpoint",
    ),

    # Privilege escalation
    _ThreatPattern(
        "sudo_request",
        "priv_escalation",
        re.compile(r'(?i)(?:run|execute|use)\s+(?:as\s+)?(?:sudo|admin|root|administrator)'),
        RiskLevel.MEDIUM,
        "Privilege escalation request",
    ),
    _ThreatPattern(
        "permission_change",
        "priv_escalation",
        re.compile(r'(?i)(?:chmod\s+777|chmod\s+666|icacls.*everyone.*full)'),
        RiskLevel.HIGH,
        "Dangerous permission change",
    ),
]


class MCPTrustClassifier:
    """Classifies MCP tool outputs for injection and manipulation patterns."""

    def __init__(self, audit_log: Optional["AuditLog"] = None) -> None:
        self._audit = audit_log
        self._patterns = _THREAT_PATTERNS

    async def classify(self, tool_name: str, result: str) -> TrustVerdict:
        """Analyze MCP tool output and return a trust verdict.

        Args:
            tool_name: Name of the MCP tool that produced this result.
            result: The text output from the tool.

        Returns:
            TrustVerdict with risk level, flags, and recommendation.
        """
        flags: list[str] = []
        matched_patterns: list[str] = []
        max_risk = RiskLevel.LOW

        for pattern in self._patterns:
            if pattern.regex.search(result):
                flags.append(f"{pattern.category}:{pattern.name}")
                matched_patterns.append(pattern.description)

                if pattern.risk.value == "high" or (
                    pattern.risk == RiskLevel.HIGH
                ):
                    max_risk = RiskLevel.HIGH
                elif pattern.risk == RiskLevel.MEDIUM and max_risk != RiskLevel.HIGH:
                    max_risk = RiskLevel.MEDIUM

        # Determine recommendation
        if max_risk == RiskLevel.HIGH:
            recommendation = "block"
        elif max_risk == RiskLevel.MEDIUM:
            recommendation = "caution"
        else:
            recommendation = "proceed"

        verdict = TrustVerdict(
            risk=max_risk,
            tool_name=tool_name,
            flags=flags,
            matched_patterns=matched_patterns,
            recommendation=recommendation,
        )

        # Log to audit trail
        if max_risk != RiskLevel.LOW and self._audit and self._audit.available:
            await self._audit.log_security_event(
                detail=f"MCP trust classifier: {tool_name} output flagged as {max_risk.value}",
                severity="critical" if max_risk == RiskLevel.HIGH else "warning",
                params={
                    "tool": tool_name,
                    "risk": max_risk.value,
                    "flags": flags,
                    "recommendation": recommendation,
                },
            )

        if max_risk != RiskLevel.LOW:
            log.warning(
                "MCPTrustClassifier: %s output → %s risk [%s]",
                tool_name, max_risk.value, ", ".join(flags),
            )

        return verdict

    def classify_sync(self, tool_name: str, result: str) -> TrustVerdict:
        """Synchronous version for non-async contexts. Does not log to audit."""
        flags: list[str] = []
        matched_patterns: list[str] = []
        max_risk = RiskLevel.LOW

        for pattern in self._patterns:
            if pattern.regex.search(result):
                flags.append(f"{pattern.category}:{pattern.name}")
                matched_patterns.append(pattern.description)
                if pattern.risk == RiskLevel.HIGH:
                    max_risk = RiskLevel.HIGH
                elif pattern.risk == RiskLevel.MEDIUM and max_risk != RiskLevel.HIGH:
                    max_risk = RiskLevel.MEDIUM

        if max_risk == RiskLevel.HIGH:
            recommendation = "block"
        elif max_risk == RiskLevel.MEDIUM:
            recommendation = "caution"
        else:
            recommendation = "proceed"

        return TrustVerdict(
            risk=max_risk,
            tool_name=tool_name,
            flags=flags,
            matched_patterns=matched_patterns,
            recommendation=recommendation,
        )

    @property
    def pattern_count(self) -> int:
        return len(self._patterns)
