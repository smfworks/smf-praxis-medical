"""Controlled-substance prescribing guardrails (Gap M5).

Praxis is a **guardrail**, never the prescriber. Prescribing is always
physician-held — the physician + the EHR's e-prescribing module prescribe.
This module flags risk before a controlled-substance Rx reaches the
physician for approval:

1. **PMP query gate.** All 13 states have a Prescription Monitoring Program
   and most mandate a query before initial opioid prescribing. This module
   records the PMP query result and flags if a query wasn't run before the
   Rx draft.

2. **Initial opioid Rx limit.** NY, MA, PA, OH, VA have a 7-day initial
   supply limit for acute opioid prescriptions; NJ has a 5-day limit. The
   limit comes from the MedicalProfile.initial_opioid_rx_limit_days field.
   This module flags a draft that exceeds the state's initial limit.

3. **Dosing flag.** A draft outside the guideline dose range (e.g. an
   opioid MME/day above the CDC 50 MME/day caution threshold) is flagged
   for physician review.

4. **DEA + state authority verification.** The physician must have a
   current DEA registration and (where required) state controlled-substance
   authority to prescribe the schedule. This module checks the asserted
   credentials and flags if either is missing or expired.

The governance line is non-negotiable: Praxis never prescribes. Every
finding routes to the physician (SEND held); the physician decides.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hybridagent.jurisdictions import get_medical_profile

# DEA Schedules
Schedule = Literal["II", "III", "IV", "V"]

# The CDC MME/day caution threshold for opioid dosing
CDC_MME_CAUTION_THRESHOLD = 50.0
# The CDC MME/day high-risk threshold (avoid or justify)
CDC_MME_HIGH_RISK_THRESHOLD = 90.0


@dataclass(frozen=True)
class PmpQueryResult:
    """The result of a Prescription Monitoring Program query. Praxis records
    this; the physician interprets it (prior opioid history, multiple
    prescribers, etc.). Praxis flags whether a query was run at all."""
    query_id: str
    queried_at: float
    patient_id: str
    state: str
    prior_opioid_scripts: int = 0    # count of prior opioid Rx in the PMP
    multiple_prescribers: bool = False
    queried: bool = True             # False = the query was NOT run (the flag)


@dataclass(frozen=True)
class RxDraft:
    """A drafted controlled-substance prescription. Praxis drafts this; the
    physician approves + signs (the governance broker holds SEND). The
    guardrail module flags risk before the physician sees the draft."""
    draft_id: str
    patient_id: str
    state: str                       # the jurisdiction (for the limit + PMP)
    physician_id: str
    drug_name: str
    schedule: Schedule
    days_supply: int                 # the initial supply (days)
    mme_per_day: float = 0.0         # morphine milligram equivalents per day (opioids)
    is_opioid: bool = False
    is_initial: bool = True          # initial Rx (vs refill)


@dataclass
class PrescriberAuthority:
    """The physician's asserted controlled-substance prescribing authority.
    Praxis records this; the practice verifies (no board-API verification)."""
    physician_id: str
    state: str
    dea_number: str
    dea_expires: str                 # ISO date
    state_cs_authority: bool = True  # state controlled-substance registration
    state_cs_expires: str = ""       # ISO date (if separate)


class ControlledSubstanceError(Exception):
    """Raised when a controlled-substance Rx draft is blocked entirely (e.g.
    the prescriber has no DEA registration). Soft flags return findings
    without raising."""


@dataclass
class GuardrailFinding:
    """A single guardrail finding on a controlled-substance Rx draft."""
    severity: str          # critical | high | medium | info
    code: str              # pmp_not_queried | initial_limit_exceeded | high_mme | ...
    message: str
    state: str             # the jurisdiction whose rule triggered this


@dataclass
class GuardrailReport:
    """The full guardrail report for a controlled-substance Rx draft."""
    draft: RxDraft
    findings: list[GuardrailFinding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """A critical finding blocks the draft from reaching the physician
        (e.g. no DEA registration — the physician can't prescribe anyway)."""
        return any(f.severity == "critical" for f in self.findings)

    @property
    def flags_for_physician(self) -> list[GuardrailFinding]:
        """Findings the physician must review before approving (high/medium).
        The draft proceeds to the physician; these are the risk flags."""
        return [f for f in self.findings if f.severity in ("high", "medium")]

    def summary(self) -> str:
        n = len(self.findings)
        crit = sum(1 for f in self.findings if f.severity == "critical")
        high = sum(1 for f in self.findings if f.severity == "high")
        return (f"{n} finding(s): {crit} critical, {high} high; "
                f"blocked={self.blocked}")


def check_prescriber_authority(
    rx: RxDraft, authority: PrescriberAuthority, *, now: float = 0.0
) -> list[GuardrailFinding]:
    """Check the physician's DEA + state controlled-substance authority.
    A missing/expired DEA registration is critical (the physician can't
    prescribe). A missing state authority is high (the physician may have
    it through the DEA in some states)."""
    findings: list[GuardrailFinding] = []
    import time as _t
    now_dt = _t.time() if now == 0.0 else now
    from datetime import datetime
    if authority.physician_id != rx.physician_id:
        findings.append(GuardrailFinding(
            "critical", "authority_physician_mismatch",
            "prescriber authority belongs to a different physician", rx.state,
        ))
    if authority.state.upper() != rx.state.upper():
        findings.append(GuardrailFinding(
            "critical", "authority_state_mismatch",
            "prescriber authority is for a different state", rx.state,
        ))
    try:
        dea_exp = datetime.fromisoformat(authority.dea_expires).timestamp()
    except (ValueError, TypeError):
        dea_exp = 0.0
    if not authority.dea_number.strip():
        findings.append(GuardrailFinding(
            "critical", "no_dea_registration",
            "physician has no DEA registration — cannot prescribe controlled substances",
            rx.state))
    elif dea_exp <= 0.0:
        findings.append(GuardrailFinding(
            "critical", "dea_expiry_invalid",
            "physician's DEA expiration date is missing or invalid", rx.state,
        ))
    elif dea_exp < now_dt:
        findings.append(GuardrailFinding(
            "critical", "dea_expired",
            f"physician's DEA registration expired {authority.dea_expires}",
            rx.state))
    if not authority.state_cs_authority:
        findings.append(GuardrailFinding(
            "high", "no_state_cs_authority",
            "physician lacks state controlled-substance authority",
            rx.state))
    elif authority.state_cs_expires:
        try:
            cs_exp = datetime.fromisoformat(authority.state_cs_expires).timestamp()
            if cs_exp < now_dt:
                findings.append(GuardrailFinding(
                    "high", "state_cs_expired",
                    f"state controlled-substance authority expired {authority.state_cs_expires}",
                    rx.state))
        except (ValueError, TypeError):
            findings.append(GuardrailFinding(
                "high", "state_cs_expiry_invalid",
                "state controlled-substance authority expiration is invalid",
                rx.state,
            ))
    return findings


def check_pmp_query(rx: RxDraft, pmp: PmpQueryResult | None) -> list[GuardrailFinding]:
    """Check that a PMP query was run before the Rx draft. All 13 states
    have a PMP; most mandate a query before initial opioid prescribing.
    A missing query is a high finding (the physician must review)."""
    findings: list[GuardrailFinding] = []
    prof = get_medical_profile(rx.state)
    if prof is None:
        return [GuardrailFinding(
            "critical", "unknown_jurisdiction",
            "cannot evaluate PMP requirement for an unsupported jurisdiction", rx.state,
        )]
    if not prof.pmp_query_required:
        return findings
    if pmp is None or not pmp.queried:
        findings.append(GuardrailFinding(
            "high", "pmp_not_queried",
            f"PMP query not run before this {rx.state} controlled-substance Rx "
            f"(required by state)",
            rx.state))
    elif (pmp.patient_id != rx.patient_id or
          pmp.state.upper() != rx.state.upper() or
          not pmp.query_id.strip() or
          pmp.queried_at <= 0):
        findings.append(GuardrailFinding(
            "high", "pmp_query_mismatch",
            "PMP query is not bound to this patient, state, and request", rx.state,
        ))
    elif pmp.multiple_prescribers or pmp.prior_opioid_scripts > 3:
        findings.append(GuardrailFinding(
            "medium", "pmp_history_flag",
            f"PMP flags prior history: {pmp.prior_opioid_scripts} prior opioid "
            f"scripts, multiple_prescribers={pmp.multiple_prescribers} — "
            f"physician to review",
            rx.state))
    return findings


def check_initial_opioid_limit(rx: RxDraft) -> list[GuardrailFinding]:
    """Check the initial opioid Rx supply against the state's day limit.
    NY/MA/PA/OH/VA = 7 days; NJ = 5 days; others = no state limit (the
    CDC guideline applies). A draft exceeding the state's initial limit
    is a high finding."""
    findings: list[GuardrailFinding] = []
    if not rx.is_opioid or not rx.is_initial:
        return findings  # limit applies to initial opioid Rx only
    prof = get_medical_profile(rx.state)
    if prof is None:
        return [GuardrailFinding(
            "critical", "unknown_jurisdiction",
            "cannot evaluate opioid limits for an unsupported jurisdiction", rx.state,
        )]
    limit = prof.initial_opioid_rx_limit_days
    if limit > 0 and rx.days_supply > limit:
        findings.append(GuardrailFinding(
            "high", "initial_limit_exceeded",
            f"initial opioid Rx for {rx.days_supply} days exceeds {rx.state}'s "
            f"{limit}-day initial supply limit",
            rx.state))
    return findings


def check_dosing(rx: RxDraft) -> list[GuardrailFinding]:
    """Check the opioid MME/day against CDC thresholds. >50 MME/day is a
    caution; >90 MME/day is high-risk (avoid or justify)."""
    findings: list[GuardrailFinding] = []
    if not rx.is_opioid or rx.mme_per_day <= 0:
        return findings
    if rx.mme_per_day >= CDC_MME_HIGH_RISK_THRESHOLD:
        findings.append(GuardrailFinding(
            "high", "high_mme",
            f"opioid dose {rx.mme_per_day} MME/day >= {CDC_MME_HIGH_RISK_THRESHOLD} "
            f"(CDC high-risk threshold) — avoid or justify",
            rx.state))
    elif rx.mme_per_day >= CDC_MME_CAUTION_THRESHOLD:
        findings.append(GuardrailFinding(
            "medium", "caution_mme",
            f"opioid dose {rx.mme_per_day} MME/day >= {CDC_MME_CAUTION_THRESHOLD} "
            f"(CDC caution threshold) — review",
            rx.state))
    return findings


def check_controlled_substance_rx(
    rx: RxDraft,
    authority: PrescriberAuthority,
    pmp: PmpQueryResult | None = None,
    *,
    now: float = 0.0,
) -> GuardrailReport:
    """The full guardrail check for a controlled-substance Rx draft. Runs
    all four checks (prescriber authority, PMP query, initial limit, dosing)
    and returns the report. The governance broker holds the Rx as SEND
    (physician approval); this report is the risk surface the physician
    reviews before signing."""
    report = GuardrailReport(draft=rx)
    report.findings.extend(check_prescriber_authority(rx, authority, now=now))
    report.findings.extend(check_pmp_query(rx, pmp))
    report.findings.extend(check_initial_opioid_limit(rx))
    report.findings.extend(check_dosing(rx))
    return report


def render_guardrail_report(report: GuardrailReport) -> str:
    """Render the guardrail report as the physician-facing review document."""
    rx = report.draft
    lines = ["Controlled-Substance Rx Guardrail Report",
             f"Draft: {rx.draft_id} | Patient: {rx.patient_id} | State: {rx.state}",
             f"Drug: {rx.drug_name} (Schedule {rx.schedule}) | "
             f"Days: {rx.days_supply} | MME/day: {rx.mme_per_day}",
             "=" * 60]
    if not report.findings:
        lines.append("No guardrail findings — within state limits + CDC guidelines.")
    for f in report.findings:
        marker = {"critical": "❌", "high": "⚠️",
                  "medium": "•", "info": "i"}.get(f.severity, "?")
        lines.append(f"  {marker} [{f.severity.upper()}] {f.code}: {f.message}")
    lines.append(f"Summary: {report.summary()}")
    if report.blocked:
        lines.append("BLOCKED — physician cannot prescribe (critical finding).")
    else:
        lines.append("Proceed to physician review (SEND held for approval).")
    return "\n".join(lines)