"""Medical-practice data-security attestation for MA WISP and NY SHIELD."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hybridagent.jurisdictions import get_medical_profile

Severity = Literal["critical", "high", "medium", "low", "info"]


@dataclass(frozen=True)
class SecurityControls:
    wisp_on_file: bool = False
    wisp_last_reviewed: str = ""
    encryption_at_rest: bool = False
    encryption_in_transit: bool = False
    employee_training_current: bool = False
    training_last_completed: str = ""
    breach_notification_procedure: bool = False
    data_classification_enforced: bool = False
    egress_allowlist_enforced: bool = False
    redaction_required_on_export: bool = False
    sandbox_isolation_enabled: bool = False
    audit_trail_enabled: bool = False
    last_incident_test: str = ""


@dataclass(frozen=True)
class SecurityFinding:
    severity: Severity
    control: str
    message: str
    requirement: str = ""


@dataclass
class SecurityAttestation:
    jurisdiction: str
    tier: str
    passed: bool
    findings: list[SecurityFinding] = field(default_factory=list)
    controls: SecurityControls | None = None
    profile_citation: str = ""

    def summary(self) -> str:
        result = "PASS" if self.passed else "FAIL"
        return f"{result}: data-security controls for {self.jurisdiction} ({self.tier})."


def _wisp(controls: SecurityControls) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    required: tuple[tuple[bool, Severity, str, str], ...] = (
        (controls.wisp_on_file, "critical", "wisp_on_file", "Written Information Security Program missing"),
        (controls.encryption_at_rest, "critical", "encryption_at_rest", "encryption at rest missing"),
        (controls.encryption_in_transit, "critical", "encryption_in_transit", "encryption in transit missing"),
        (controls.employee_training_current, "high", "employee_training_current", "security training is not current"),
        (controls.breach_notification_procedure, "high", "breach_notification_procedure", "breach procedure missing"),
    )
    for present, severity, control, message in required:
        if not present:
            findings.append(SecurityFinding(severity, control, message, "201 CMR 17.00"))
    return findings


def _shield(controls: SecurityControls) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    if not controls.wisp_on_file and not controls.data_classification_enforced:
        findings.append(SecurityFinding("high", "wisp_on_file", "administrative safeguards missing", "GBL §899-bb"))
    if not controls.encryption_at_rest and not controls.encryption_in_transit:
        findings.append(SecurityFinding("high", "encryption", "technical safeguards missing", "GBL §899-bb"))
    if not controls.breach_notification_procedure:
        findings.append(SecurityFinding("high", "breach_notification_procedure", "breach procedure missing", "GBL §899-aa"))
    return findings


def _breach_only(controls: SecurityControls) -> list[SecurityFinding]:
    if controls.breach_notification_procedure:
        return []
    return [SecurityFinding("high", "breach_notification_procedure", "breach procedure missing")]


_CHECKERS = {
    "wisp_mandate": _wisp,
    "shield_obligation": _shield,
    "breach_notification_only": _breach_only,
}


def attest(state: str, controls: SecurityControls) -> SecurityAttestation:
    profile = get_medical_profile(state)
    if profile is None:
        return SecurityAttestation(
            state.upper(),
            "unknown",
            False,
            [SecurityFinding("critical", "jurisdiction", "unsupported jurisdiction")],
            controls,
        )
    tier = profile.data_security_tier
    checker = _CHECKERS.get(tier)
    if checker is None:
        return SecurityAttestation(
            state.upper(),
            tier,
            False,
            [SecurityFinding("critical", "data_security_tier", f"unsupported tier: {tier}")],
            controls,
            profile.data_security_citation,
        )
    findings = checker(controls)
    passed = not any(item.severity in {"critical", "high"} for item in findings)
    return SecurityAttestation(
        state.upper(), tier, passed, findings, controls, profile.data_security_citation
    )


def render(attestation: SecurityAttestation) -> str:
    lines = [
        f"Data-Security Attestation — {attestation.jurisdiction}",
        f"Tier: {attestation.tier}",
        attestation.summary(),
    ]
    lines.extend(
        f"[{finding.severity}] {finding.control}: {finding.message}"
        for finding in attestation.findings
    )
    return "\n".join(lines)
