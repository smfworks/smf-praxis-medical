"""Medical records retention + patient-access workflow (Gap M9).

Per-jurisdiction retention periods and the patient right-of-access timeline
from the MedicalProfile registry:

- ``record_retention_adult_years`` / ``record_retention_minor_years``
- ``record_retention_minor_rule`` (e.g. until age 21 or N years after last
  visit, whichever longer)
- ``patient_access_days`` — business-day deadline to produce records on
  request (NY=10, MA=10, FL=15, NJ=7, HIPAA floor ~30 when state is 0)

Design:
1. **Retention clock.** For each encounter/record set, compute the
   earliest eligible disposal date from the state's adult or minor rule.
   Disposal is blocked while a legal hold is active.

2. **Patient-access workflow.** A patient (or authorized representative)
   requests records; the practice must produce them within
   ``patient_access_days`` (or the HIPAA 30-day floor when the state
   field is 0). This module tracks the request, the deadline, and
   completion / overdue status.

3. **No autonomous destruction.** Praxis never deletes medical records.
   Disposal recommendations surface as DRAFT findings for staff; actual
   deletion remains DESTRUCTIVE (held, dual-approval).

Builds on the ``data_policy`` retention primitive without modifying the
governance spine — this is a medical-vertical workflow surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hybridagent.jurisdictions import get_medical_profile

# Approximate year length for retention math (leap-year aware enough for policy)
_SECONDS_PER_YEAR = 365.25 * 24 * 3600
_SECONDS_PER_DAY = 24 * 3600
# HIPAA floor for patient access when state encodes 0
HIPAA_ACCESS_DAYS_FLOOR = 30

RecordStatus = Literal[
    "active",              # within retention period
    "eligible_for_disposal",  # past retention, no hold
    "legal_hold",          # hold blocks disposal
    "unknown_jurisdiction",
]

AccessRequestStatus = Literal[
    "open",
    "fulfilled",
    "overdue",
    "cancelled",
]


@dataclass(frozen=True)
class MedicalRecordSet:
    """A set of medical records for one patient encounter / chart segment.

    Retention is evaluated against the state's adult or minor rule based
    on ``patient_was_minor`` and ``patient_dob`` when available.
    """
    record_id: str
    patient_id: str
    state: str
    last_visit_at: float           # timestamp of last encounter in the set
    patient_was_minor: bool = False
    patient_dob: float = 0.0       # timestamp; 0 if unknown
    legal_hold: bool = False
    legal_hold_reason: str = ""


@dataclass
class RetentionAssessment:
    """Retention status for a medical record set."""
    record: MedicalRecordSet
    status: RecordStatus
    retention_years: int
    dispose_after: float           # timestamp; 0 if unknown
    findings: list[str] = field(default_factory=list)
    citation: str = ""

    @property
    def eligible_for_disposal(self) -> bool:
        return self.status == "eligible_for_disposal"


@dataclass(frozen=True)
class PatientAccessRequest:
    """A patient (or authorized representative) request for medical records."""
    request_id: str
    patient_id: str
    state: str
    requested_at: float
    requester_role: str = "patient"   # patient | authorized_representative | parent
    scope: str = "full_record"        # full_record | encounter | date_range
    channel: str = "portal"           # portal | mail | in_person | email


@dataclass
class AccessWorkflow:
    """Tracked patient-access request with per-state deadline."""
    request: PatientAccessRequest
    deadline_at: float
    access_days: int
    citation: str
    status: AccessRequestStatus = "open"
    fulfilled_at: float = 0.0
    findings: list[str] = field(default_factory=list)

    def refresh_status(self, *, now: float) -> AccessRequestStatus:
        if self.status in ("fulfilled", "cancelled"):
            return self.status
        if now > self.deadline_at:
            self.status = "overdue"
        else:
            self.status = "open"
        return self.status


class RecordsRetentionError(Exception):
    """Raised on hard policy violations (e.g. dispose under legal hold)."""


def _years_to_seconds(years: int) -> float:
    return float(years) * _SECONDS_PER_YEAR


def assess_retention(
    record: MedicalRecordSet,
    *,
    now: float = 0.0,
) -> RetentionAssessment:
    """Compute retention status for a medical record set.

    Adult rule: last_visit_at + adult_years.
    Minor rule: max(last_visit_at + minor_years, dob + 21 years) when DOB
    is known; otherwise last_visit_at + minor_years. The registry's
    ``record_retention_minor_rule`` is surfaced in findings for humans.
    """
    import time as _t
    now_ts = _t.time() if now == 0.0 else now
    state = record.state.upper()
    prof = get_medical_profile(state)

    if prof is None:
        return RetentionAssessment(
            record=record,
            status="unknown_jurisdiction",
            retention_years=0,
            dispose_after=0.0,
            findings=[
                (f"state {state!r} not in the 13-state medical registry — "
                f"cannot compute retention")
            ],
        )

    if record.patient_was_minor:
        years = prof.record_retention_minor_years
        dispose_after = record.last_visit_at + _years_to_seconds(years)
        # "until age 21" style rules: if DOB known, keep until max(dispose, age 21)
        if record.patient_dob > 0:
            age_21 = record.patient_dob + _years_to_seconds(21)
            dispose_after = max(dispose_after, age_21)
        citation = prof.record_retention_citation
        rule_note = prof.record_retention_minor_rule
    else:
        years = prof.record_retention_adult_years
        dispose_after = record.last_visit_at + _years_to_seconds(years)
        citation = prof.record_retention_citation
        rule_note = f"{years} years from last visit (adult)"

    findings: list[str] = [
        f"{state} retention rule: {rule_note}",
        f"citation: {citation}",
    ]

    if record.legal_hold:
        findings.append(
            "LEGAL HOLD active"
            + (f" ({record.legal_hold_reason})" if record.legal_hold_reason else "")
            + " — disposal blocked"
        )
        return RetentionAssessment(
            record=record,
            status="legal_hold",
            retention_years=years,
            dispose_after=dispose_after,
            findings=findings,
            citation=citation,
        )

    if now_ts >= dispose_after:
        findings.append("retention period elapsed — eligible for disposal review")
        status: RecordStatus = "eligible_for_disposal"
    else:
        remaining = dispose_after - now_ts
        remaining_years = remaining / _SECONDS_PER_YEAR
        findings.append(
            f"retention active — ~{remaining_years:.1f} years remaining "
            f"before disposal eligibility"
        )
        status = "active"

    return RetentionAssessment(
        record=record,
        status=status,
        retention_years=years,
        dispose_after=dispose_after,
        findings=findings,
        citation=citation,
    )


def assert_disposal_allowed(assessment: RetentionAssessment) -> RetentionAssessment:
    """Fail-hard: raise if disposal is not allowed."""
    if assessment.status == "legal_hold":
        raise RecordsRetentionError(
            f"record {assessment.record.record_id} is under legal hold — "
            f"disposal blocked"
        )
    if assessment.status != "eligible_for_disposal":
        raise RecordsRetentionError(
            f"record {assessment.record.record_id} is not eligible for "
            f"disposal (status={assessment.status})"
        )
    return assessment


def open_patient_access_request(
    request: PatientAccessRequest,
) -> AccessWorkflow:
    """Open a patient-access request and compute the per-state deadline.

    When ``patient_access_days`` is 0 (state encodes HIPAA floor only),
    the HIPAA 30-day floor is applied.
    """
    state = request.state.upper()
    prof = get_medical_profile(state)
    findings: list[str] = []

    if prof is None:
        # Fail closed with HIPAA floor so the workflow still tracks
        days = HIPAA_ACCESS_DAYS_FLOOR
        citation = "HIPAA 45 CFR §164.524 (30-day floor) — unknown jurisdiction"
        findings.append(
            f"state {state!r} not in registry — applying HIPAA "
            f"{HIPAA_ACCESS_DAYS_FLOOR}-day floor"
        )
    else:
        days = prof.patient_access_days
        if days <= 0:
            days = HIPAA_ACCESS_DAYS_FLOOR
            findings.append(
                f"{state} encodes no specific access deadline — applying "
                f"HIPAA {HIPAA_ACCESS_DAYS_FLOOR}-day floor"
            )
        citation = prof.patient_access_citation or (
            f"{state} patient-access rule"
        )
        findings.append(
            f"{state} patient-access deadline: {days} business days "
            f"({citation})"
        )

    # Approximate business days as calendar days * 7/5 for deadline math;
    # practices convert to true business days in ops. Policy surface uses
    # the stated day count as calendar-day budget for deterministic tests.
    deadline = request.requested_at + (days * _SECONDS_PER_DAY)

    return AccessWorkflow(
        request=request,
        deadline_at=deadline,
        access_days=days,
        citation=citation,
        status="open",
        findings=findings,
    )


def fulfill_access_request(
    workflow: AccessWorkflow,
    *,
    fulfilled_at: float,
) -> AccessWorkflow:
    """Mark a patient-access request fulfilled."""
    workflow.fulfilled_at = fulfilled_at
    if fulfilled_at > workflow.deadline_at:
        workflow.status = "overdue"
        workflow.findings.append(
            f"fulfilled after deadline "
            f"(deadline_at={workflow.deadline_at}, fulfilled_at={fulfilled_at})"
        )
    else:
        workflow.status = "fulfilled"
        workflow.findings.append("fulfilled within deadline")
    return workflow


def access_request_status(
    workflow: AccessWorkflow,
    *,
    now: float = 0.0,
) -> AccessRequestStatus:
    """Return the current status, refreshing overdue when still open."""
    import time as _t
    now_ts = _t.time() if now == 0.0 else now
    return workflow.refresh_status(now=now_ts)


def approaching_retention_expiry(
    records: list[MedicalRecordSet],
    *,
    within_years: float = 1.0,
    now: float = 0.0,
) -> list[RetentionAssessment]:
    """Return assessments for records entering the final ``within_years``
    of their retention window (or already eligible). Useful for the
    dashboard retention summary card.
    """
    import time as _t
    now_ts = _t.time() if now == 0.0 else now
    window = within_years * _SECONDS_PER_YEAR
    out: list[RetentionAssessment] = []
    for rec in records:
        a = assess_retention(rec, now=now_ts)
        if a.status in ("eligible_for_disposal", "legal_hold"):
            out.append(a)
        elif a.status == "active" and a.dispose_after > 0:
            if a.dispose_after - now_ts <= window:
                out.append(a)
    return out


def render_retention_assessment(assessment: RetentionAssessment) -> str:
    rec = assessment.record
    lines = [
        "Medical Records Retention Assessment",
        (f"Record: {rec.record_id} | Patient: {rec.patient_id} | "
        f"State: {rec.state}"),
        (f"Last visit: {rec.last_visit_at} | Minor at care: "
        f"{rec.patient_was_minor}"),
        (f"Status: {assessment.status} | Retention years: "
        f"{assessment.retention_years}"),
        f"Dispose after: {assessment.dispose_after}",
        "=" * 60,
    ]
    for f in assessment.findings:
        lines.append(f"  • {f}")
    if assessment.eligible_for_disposal:
        lines.append(
            "ELIGIBLE — route disposal as DESTRUCTIVE (dual approval); "
            "Praxis never deletes autonomously."
        )
    elif assessment.status == "legal_hold":
        lines.append("HOLD — do not dispose.")
    else:
        lines.append("ACTIVE — retain.")
    return "\n".join(lines)


def render_access_workflow(workflow: AccessWorkflow) -> str:
    req = workflow.request
    lines = [
        "Patient Access Request Workflow",
        (f"Request: {req.request_id} | Patient: {req.patient_id} | "
        f"State: {req.state}"),
        (f"Requested at: {req.requested_at} | Scope: {req.scope} | "
        f"Channel: {req.channel}"),
        (f"Deadline: {workflow.deadline_at} ({workflow.access_days} days) | "
        f"Status: {workflow.status}"),
        f"Citation: {workflow.citation}",
        "=" * 60,
    ]
    for f in workflow.findings:
        lines.append(f"  • {f}")
    if workflow.status == "overdue":
        lines.append("OVERDUE — escalate; patient right-of-access deadline missed.")
    elif workflow.status == "fulfilled":
        lines.append("FULFILLED — within or after deadline (see findings).")
    else:
        lines.append("OPEN — produce records before the deadline.")
    return "\n".join(lines)
