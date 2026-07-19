"""HIPAA governance wrapper (Gap M2 — minimum-necessary + accounting-of-
disclosures + breach-response workflow).

HIPAA is the federal floor for medical-practice data handling. Three pieces
the medical vertical needs that the existing substrate doesn't provide:

1. **Minimum-necessary enforcement.** HIPAA's "minimum necessary" standard
   (45 CFR §164.502(b)) requires that PHI access + disclosure be limited to
   the minimum needed for the purpose. Praxis's ``authz.py`` already has
   purpose-of-use enforcement; this module wraps it with a minimum-
   necessary check that records the purpose + the fields requested and
   flags over-broad access.

2. **Accounting of disclosures.** HIPAA (45 CFR §164.528) gives patients
   the right to an accounting of non-routine disclosures of their PHI over
   the prior 6 years. This module is the append-only ledger that records
   every PHI disclosure (who, what, to whom, purpose, when) and produces
   the accounting on request.

3. **Breach-response workflow.** The existing ``security_attestation.py``
   has the per-jurisdiction tier (MA WISP / NY SHIELD / breach-notification-
   only). This module is the workflow: detect a suspected breach → assess
   (is it a reportable breach under the 4-factor risk assessment?) →
   notify within the state's timeline (MA 30 days, HIPAA floor 60 days,
   NY "expeditiously") → document. The state timeline comes from the
   MedicalProfile.breach_notification_days field.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hybridagent.jurisdictions import get_medical_profile

# HIPAA disclosure purposes (45 CFR §164.506 — treatment/payment/operations
# are routine and don't require an accounting entry; the non-routine ones do)
DisclosurePurpose = Literal[
    "treatment",            # routine — no accounting entry
    "payment",              # routine — no accounting entry
    "healthcare_operations",  # routine — no accounting entry
    "legal_subpoena",       # non-routine — accounting entry required
    "public_health",        # non-routine
    "research",             # non-routine
    "law_enforcement",      # non-routine
    "decedent",             # non-routine
    "patient_request",      # non-routine (disclosed at patient's written request)
    "other",                # non-routine — detail required
]

# The non-routine purposes that require an accounting-of-disclosures entry
NON_ROUTINE_PURPOSES = frozenset({
    "legal_subpoena", "public_health", "research", "law_enforcement",
    "decedent", "patient_request", "other",
})


@dataclass(frozen=True)
class PhiField:
    """A PHI field requested for access or disclosure (for minimum-necessary
    checks). e.g. 'diagnosis', 'medication_list', 'demographics'."""
    name: str
    sensitivity: str = "standard"   # standard | highly_sensitive (HIV, MH, SU, reproductive)


@dataclass(frozen=True)
class MinimumNecessaryRequest:
    """A request to access PHI for a purpose. The minimum-necessary check
    verifies the requested fields are appropriate for the purpose and flags
    over-broad access."""
    purpose: DisclosurePurpose
    requested_fields: tuple[PhiField, ...]
    requester_role: str          # "physician", "nurse", "billing", "admin", "praxis"


# The fields appropriate for each routine purpose (minimum-necessary defaults)
# — a physician treating a patient may access the full clinical record; billing
# may access charges + diagnoses but not the full clinical narrative; admin
# may access demographics but not clinical content.
_PURPOSE_FIELDS: dict[str, frozenset[str]] = {
    "treatment": frozenset({"demographics", "diagnosis", "medication_list",
                            "allergies", "lab_results", "clinical_notes",
                            "imaging", "vital_signs", "problem_list",
                            # a treating physician may access the full clinical
                            # record, incl. highly-sensitive fields
                            "hiv_status", "mental_health", "substance_use",
                            "reproductive_health", "genetic_info"}),
    "payment": frozenset({"demographics", "diagnosis_codes",
                          "charges", "insurance", "procedure_codes"}),
    "healthcare_operations": frozenset({"demographics", "diagnosis_codes",
                                        "quality_metrics", "procedure_codes",
                                        "utilization"}),
}

# Highly-sensitive fields require a tighter purpose — they may not be accessed
# for payment or operations without explicit authorization
_HIGHLY_SENSITIVE = frozenset({"hiv_status", "mental_health", "substance_use",
                               "reproductive_health", "genetic_info"})


class MinimumNecessaryError(Exception):
    """Raised when a PHI access request is over-broad for its purpose."""


def check_minimum_necessary(req: MinimumNecessaryRequest) -> list[str]:
    """Check a PHI access request against the minimum-necessary standard.
    Returns a list of findings (empty = compliant). Raises only on
    hard violations (highly-sensitive fields for an inappropriate purpose);
    soft over-broad access returns findings but doesn't raise."""
    findings: list[str] = []
    allowed = _PURPOSE_FIELDS.get(req.purpose, frozenset())
    requested = {f.name for f in req.requested_fields}

    # over-broad: requested fields not in the allowed set for this purpose
    extra = requested - allowed
    if extra and req.purpose in _PURPOSE_FIELDS:
        findings.append(
            f"over_broad: fields {sorted(extra)} not appropriate for "
            f"purpose '{req.purpose}' (minimum necessary = {sorted(allowed)})")

    # highly-sensitive fields require treatment purpose (not payment/operations)
    if req.purpose in ("payment", "healthcare_operations"):
        sensitive = [f.name for f in req.requested_fields
                     if f.sensitivity == "highly_sensitive"
                     or f.name in _HIGHLY_SENSITIVE]
        if sensitive:
            raise MinimumNecessaryError(
                f"highly-sensitive fields {sensitive} may not be accessed for "
                f"purpose '{req.purpose}' without explicit patient authorization "
                f"(HIPAA 42 CFR Part 2 / state heightened protections)")

    # Praxis (the AI) may only access PHI for treatment support or operations,
    # never for payment or unsupervised access
    if req.requester_role == "praxis" and req.purpose not in ("treatment", "healthcare_operations"):
        findings.append(
            f"praxis may access PHI only for treatment or operations, not "
            f"'{req.purpose}'")

    return findings


# ---------------------------------------------------------------------------
# Accounting of disclosures (45 CFR §164.528)

@dataclass(frozen=True)
class PhiDisclosure:
    """A recorded disclosure of PHI (for the accounting of disclosures).
    Non-routine disclosures (legal_subpoena, public_health, research, law
    enforcement, etc.) require an accounting entry; routine TPO disclosures
    (treatment/payment/operations) do not."""
    disclosure_id: str
    patient_id: str
    phi_fields: tuple[str, ...]    # what PHI was disclosed
    disclosed_to: str              # recipient
    purpose: DisclosurePurpose
    disclosed_at: float            # timestamp
    disclosed_by: str             # who in the practice made the disclosure
    detail: str = ""               # for "other" purpose: the detail


class AccountingOfDisclosures:
    """Append-only ledger of non-routine PHI disclosures. HIPAA gives
    patients the right to an accounting of these over the prior 6 years."""

    def __init__(self) -> None:
        self._entries: list[PhiDisclosure] = []

    def record(self, disclosure: PhiDisclosure) -> PhiDisclosure | None:
        """Record a disclosure. Returns the recorded entry if it's non-routine
        (and thus requires an accounting entry), or None if it's a routine
        TPO disclosure (treatment/payment/operations — no accounting entry)."""
        if disclosure.purpose in NON_ROUTINE_PURPOSES:
            self._entries.append(disclosure)
            return disclosure
        return None  # routine TPO — no accounting entry

    def for_patient(self, patient_id: str,
                    since: float = 0.0) -> list[PhiDisclosure]:
        """Produce the accounting for a patient (optionally since a timestamp).
        This is what the practice produces when a patient requests their
        accounting of disclosures under 45 CFR §164.528."""
        return [e for e in self._entries
                if e.patient_id == patient_id and e.disclosed_at >= since]

    def all_entries(self) -> list[PhiDisclosure]:
        """All entries (for audit)."""
        return list(self._entries)

    def count(self, patient_id: str | None = None) -> int:
        if patient_id is None:
            return len(self._entries)
        return sum(1 for e in self._entries if e.patient_id == patient_id)


def render_accounting(ledger: AccountingOfDisclosures,
                      patient_id: str, since: float = 0.0) -> str:
    """Render the accounting of disclosures as the patient-facing document
    required by 45 CFR §164.528."""
    entries = ledger.for_patient(patient_id, since)
    lines = ["Accounting of Disclosures of Protected Health Information",
             f"Patient: {patient_id}",
             "=" * 60]
    if not entries:
        lines.append("No non-routine disclosures recorded in the requested period.")
    for e in entries:
        lines.append(
            f"  Date: {e.disclosed_at} | To: {e.disclosed_to} | "
            f"Purpose: {e.purpose} | Fields: {', '.join(e.phi_fields)}"
            + (f" | Detail: {e.detail}" if e.detail else ""))
    lines.append(f"Total: {len(entries)} disclosure(s)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Breach-response workflow

BreachStatus = Literal[
    "suspected",      # a potential breach has been detected
    "under_assessment",  # the 4-factor risk assessment is in progress
    "not_reportable", # assessed; below the reportable threshold
    "reportable",     # assessed; a reportable breach — notify
    "notified",       # notifications sent within the state's timeline
    "documented",     # breach response fully documented (closed)
]


@dataclass
class BreachIncident:
    """A suspected or confirmed PHI breach. The workflow:
    suspected → under_assessment → (not_reportable | reportable) → notified → documented.
    The state's notification timeline comes from the MedicalProfile."""
    incident_id: str
    detected_at: float
    state: str                    # the jurisdiction (for the notification timeline)
    phi_fields: tuple[str, ...]
    patients_affected: int
    description: str
    status: BreachStatus = "suspected"
    assessment_findings: str = ""   # the 4-factor risk assessment result
    assessment_at: float = 0.0
    notified_at: float = 0.0
    notification_deadline: float = 0.0   # detected_at + breach_notification_days
    documented_at: float = 0.0


class BreachResponseError(Exception):
    """Raised when a breach-response transition is invalid (e.g. notifying
    before assessing, or missing the state's timeline)."""


class BreachResponseLedger:
    """The breach-response workflow ledger. Tracks each incident through
    the state machine and enforces the state's notification timeline."""

    def __init__(self) -> None:
        self._incidents: dict[str, BreachIncident] = {}

    def report(self, incident: BreachIncident) -> BreachIncident:
        """Register a suspected breach. Sets the notification deadline from
        the state's MedicalProfile.breach_notification_days."""
        prof = get_medical_profile(incident.state)
        days = prof.breach_notification_days if prof else 60  # HIPAA floor
        if days <= 0:
            days = 60  # "without unreasonable delay" → use the HIPAA floor
        incident.notification_deadline = incident.detected_at + (days * 86400)
        self._incidents[incident.incident_id] = incident
        return incident

    def assess(self, incident_id: str, findings: str,
               reportable: bool, assessed_at: float) -> BreachIncident:
        """Complete the 4-factor risk assessment. Transitions to
        not_reportable or reportable."""
        inc = self._incidents.get(incident_id)
        if inc is None:
            raise BreachResponseError(f"incident {incident_id} not found")
        if inc.status not in ("suspected", "under_assessment"):
            raise BreachResponseError(
                f"cannot assess incident in status '{inc.status}'")
        inc.assessment_findings = findings
        inc.assessment_at = assessed_at
        inc.status = "not_reportable" if not reportable else "reportable"
        return inc

    def notify(self, incident_id: str, notified_at: float) -> BreachIncident:
        """Record that breach notifications were sent. Must be called only
        after a 'reportable' assessment, and before the notification deadline."""
        inc = self._incidents.get(incident_id)
        if inc is None:
            raise BreachResponseError(f"incident {incident_id} not found")
        if inc.status != "reportable":
            raise BreachResponseError(
                f"cannot notify incident in status '{inc.status}' — must be 'reportable'")
        if notified_at > inc.notification_deadline:
            raise BreachResponseError(
                f"notification at {notified_at} is PAST the deadline "
                f"{inc.notification_deadline} — the state's timeline was missed")
        inc.notified_at = notified_at
        inc.status = "notified"
        return inc

    def document(self, incident_id: str, documented_at: float) -> BreachIncident:
        """Close the incident — the breach response is fully documented."""
        inc = self._incidents.get(incident_id)
        if inc is None:
            raise BreachResponseError(f"incident {incident_id} not found")
        # may document after notified, or after not_reportable (no notification needed)
        if inc.status not in ("notified", "not_reportable"):
            raise BreachResponseError(
                f"cannot document incident in status '{inc.status}'")
        inc.documented_at = documented_at
        inc.status = "documented"
        return inc

    def get(self, incident_id: str) -> BreachIncident | None:
        return self._incidents.get(incident_id)

    def all_incidents(self) -> list[BreachIncident]:
        return list(self._incidents.values())

    def open_incidents(self) -> list[BreachIncident]:
        """Incidents not yet documented (still in the workflow)."""
        return [i for i in self._incidents.values() if i.status != "documented"]


def render_breach_log(ledger: BreachResponseLedger) -> str:
    """Render the breach-response ledger as an audit-ready log."""
    lines = ["Breach Response Log", "=" * 60]
    for inc in ledger.all_incidents():
        lines.append(
            f"  [{inc.incident_id}] state={inc.state} status={inc.status} "
            f"patients={inc.patients_affected} "
            f"deadline={inc.notification_deadline}")
    lines.append(f"Total: {len(ledger.all_incidents())} incident(s)")
    return "\n".join(lines)