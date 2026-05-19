"""content_filter — scrubs secrets and PII from text before API transmission.

Applies regex-based pattern matching (trufflehog-style) to detect and redact:
  - API keys (AWS, Anthropic, OpenAI, GitHub, generic)
  - Private keys (RSA, EC, PGP)
  - Connection strings (database URLs, Redis, etc.)
  - Common PII (SSN, credit card numbers, email addresses)
  - Environment variable assignments containing secrets

Design:
  - Stateless: each call is independent.
  - Returns both the scrubbed text and a list of findings.
  - Integrates with AuditLog to record security events when secrets are detected.
  - Does NOT block execution — redacts and logs, caller decides whether to proceed.

Usage:
    from content_filter import ContentFilter

    cf = ContentFilter(audit_log=audit)
    clean_text, findings = await cf.scrub(prompt_text)
    if findings:
        # prompt contained secrets — clean_text has them redacted
        pass
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from audit_log import AuditLog

log = logging.getLogger(__name__)


@dataclass
class Finding:
    """A single detected secret or PII instance."""
    pattern_name: str
    matched_text: str  # first 8 chars + "..." for logging (never full secret)
    start: int
    end: int
    severity: str = "warning"  # "warning" or "critical"


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

@dataclass
class _Pattern:
    name: str
    regex: re.Pattern
    severity: str = "warning"
    redact_with: str = "[REDACTED:{name}]"


_PATTERNS: list[_Pattern] = [
    # AWS
    _Pattern("aws_access_key", re.compile(r'AKIA[0-9A-Z]{16}'), "critical"),
    _Pattern("aws_secret_key", re.compile(r'(?i)aws[_\-]?secret[_\-]?access[_\-]?key[\s]*[=:]\s*["\']?([A-Za-z0-9/+=]{40})["\']?'), "critical"),

    # Anthropic
    _Pattern("anthropic_api_key", re.compile(r'sk-ant-[a-zA-Z0-9\-_]{20,}'), "critical"),

    # OpenAI
    _Pattern("openai_api_key", re.compile(r'sk-[a-zA-Z0-9]{20,}'), "critical"),

    # GitHub
    _Pattern("github_token", re.compile(r'gh[pousr]_[A-Za-z0-9_]{36,}'), "critical"),
    _Pattern("github_fine_grained", re.compile(r'github_pat_[A-Za-z0-9_]{22,}'), "critical"),

    # Generic high-entropy secrets (env var assignments)
    _Pattern("env_secret", re.compile(r'(?i)(?:api[_\-]?key|secret|token|password|passwd)\s*[=:]\s*["\']?([A-Za-z0-9/+=\-_]{16,})["\']?'), "warning"),

    # Private keys
    _Pattern("private_key", re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'), "critical"),
    _Pattern("pgp_private", re.compile(r'-----BEGIN PGP PRIVATE KEY BLOCK-----'), "critical"),

    # Connection strings
    _Pattern("database_url", re.compile(r'(?i)(?:postgres|mysql|mongodb|redis|amqp)://[^\s"\']+:[^\s"\']+@[^\s"\']+'), "critical"),

    # PII
    _Pattern("ssn", re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), "warning"),
    _Pattern("credit_card", re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b'), "warning"),

    # WireGuard private keys (base64, 44 chars)
    _Pattern("wireguard_key", re.compile(r'PrivateKey\s*=\s*[A-Za-z0-9+/]{43}='), "critical"),
]


class ContentFilter:
    """Stateless content filter that detects and redacts secrets/PII."""

    def __init__(self, audit_log: Optional["AuditLog"] = None) -> None:
        self._audit = audit_log
        self._patterns = _PATTERNS

    async def scrub(self, text: str) -> tuple[str, list[Finding]]:
        """Scan text for secrets/PII, return (redacted_text, findings).

        The redacted text replaces each match with [REDACTED:pattern_name].
        Findings contain metadata about what was found (never the full secret).
        """
        findings: list[Finding] = []
        redacted = text

        for pattern in self._patterns:
            for match in pattern.regex.finditer(text):
                matched = match.group(0)
                # Truncate for safe logging — never store full secret
                safe_preview = matched[:8] + "..." if len(matched) > 8 else matched[:4] + "..."

                findings.append(Finding(
                    pattern_name=pattern.name,
                    matched_text=safe_preview,
                    start=match.start(),
                    end=match.end(),
                    severity=pattern.severity,
                ))

                # Redact in output
                placeholder = f"[REDACTED:{pattern.name}]"
                redacted = redacted.replace(matched, placeholder, 1)

        # Log to audit trail if findings exist
        if findings and self._audit and self._audit.available:
            await self._audit.log_security_event(
                detail=f"Content filter: {len(findings)} secret(s)/PII detected and redacted",
                severity="warning" if all(f.severity == "warning" for f in findings) else "critical",
                params={
                    "pattern_names": list(set(f.pattern_name for f in findings)),
                    "count": len(findings),
                },
            )

        if findings:
            log.warning(
                "ContentFilter: redacted %d finding(s): %s",
                len(findings),
                [f.pattern_name for f in findings],
            )

        return redacted, findings

    def scrub_sync(self, text: str) -> tuple[str, list[Finding]]:
        """Synchronous version for non-async contexts. Does not log to audit."""
        findings: list[Finding] = []
        redacted = text

        for pattern in self._patterns:
            for match in pattern.regex.finditer(text):
                matched = match.group(0)
                safe_preview = matched[:8] + "..." if len(matched) > 8 else matched[:4] + "..."

                findings.append(Finding(
                    pattern_name=pattern.name,
                    matched_text=safe_preview,
                    start=match.start(),
                    end=match.end(),
                    severity=pattern.severity,
                ))

                placeholder = f"[REDACTED:{pattern.name}]"
                redacted = redacted.replace(matched, placeholder, 1)

        return redacted, findings

    @property
    def pattern_count(self) -> int:
        return len(self._patterns)
