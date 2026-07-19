"""Patient portal message triage governance (Gap M10).

Classify inbound patient-portal messages as clinical vs administrative and
route them under the medical governance line:

- **Clinical** messages (symptoms, medication questions, test results,
  clinical advice requests) → DRAFT a reply, hold SEND for physician
  approval. Never auto-send clinical content to a patient.
- **Administrative** messages (appointment confirmations, hours, billing
  address updates, directions) → may be answered autonomously **only** if
  the reply template is on a tight allowlist.

This module is the triage classifier + autonomous-allowlist gate. The
governance broker already holds SEND for clinical content; the gap this
closes is (1) classifying the inbound message and (2) deciding whether an
administrative reply may leave without physician approval.

Design principles:
- Fail closed: ambiguous / mixed messages are treated as clinical.
- Allowlist is explicit and small — free-form administrative replies are
  not autonomous.
- Praxis never gives clinical advice autonomously.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MessageClass = Literal["clinical", "administrative", "mixed", "unknown"]
TriageAction = Literal[
    "draft_for_physician",   # clinical → draft reply, SEND held
    "autonomous_reply",      # administrative + allowlisted template
    "hold_for_staff",        # administrative but not allowlisted / needs human
]


# Clinical keyword signals (lowercase). Any hit → clinical (or mixed).
_CLINICAL_SIGNALS: tuple[str, ...] = (
    "symptom", "pain", "fever", "nausea", "vomit", "bleed", "dizzy",
    "shortness of breath", "chest pain", "medication", "prescription",
    "refill", "dose", "side effect", "allergic", "allergy", "lab result",
    "test result", "diagnosis", "treatment", "therapy", "worsening",
    "emergency", "urgent care", "er ", " er", "hospital", "suicide",
    "self-harm", "depressed", "anxiety attack", "rash", "infection",
    "antibiotic", "opioid", "blood pressure", "glucose", "diabetes",
    "pregnant", "pregnancy", "miscarriage", "sti", "hiv", "positive test",
    "negative test", "biopsy", "mri", "ct scan", "x-ray", "imaging",
    "what does this mean", "is this normal", "should i take",
    "should i stop", "clinical", "doctor advice", "medical advice",
)

# Administrative keyword signals.
_ADMIN_SIGNALS: tuple[str, ...] = (
    "appointment", "reschedule", "cancel my visit", "office hours",
    "hours of operation", "parking", "directions", "address",
    "insurance card", "billing", "invoice", "statement", "copay",
    "co-pay", "forms", "new patient paperwork", "fax", "records request",
    "portal password", "login", "username", "reset password",
    "confirm my appointment", "running late", "wait time",
)

# Templates that may be sent autonomously for pure-administrative messages.
# Keys are stable template ids; values are the exact reply body (or a
# short identifier the pack uses). Free-form text is never autonomous.
AUTONOMOUS_ADMIN_TEMPLATES: dict[str, str] = {
    "appt_confirm": (
        "Your appointment is confirmed. If you need to reschedule, "
        "please call the office or reply here."
    ),
    "office_hours": (
        "Our office hours are Monday–Friday, 8:00 AM – 5:00 PM. "
        "We are closed on major holidays."
    ),
    "parking_directions": (
        "Parking is available in the lot adjacent to the building. "
        "Enter through the main lobby and check in at the front desk."
    ),
    "portal_password_reset": (
        "To reset your patient-portal password, use the 'Forgot Password' "
        "link on the portal login page. Contact the office if you need help."
    ),
    "billing_statement_ack": (
        "We received your billing question. A billing specialist will "
        "follow up within 2 business days."
    ),
    "forms_location": (
        "New-patient and intake forms are available on our website under "
        "Patient Forms, and at the front desk."
    ),
}


@dataclass(frozen=True)
class PortalMessage:
    """An inbound patient-portal message awaiting triage."""
    message_id: str
    patient_id: str
    subject: str
    body: str
    received_at: float = 0.0
    state: str = ""                 # optional jurisdiction context


@dataclass(frozen=True)
class PortalReplyDraft:
    """A drafted reply to a portal message.

    Autonomous send is permitted only when ``template_id`` is in
    ``AUTONOMOUS_ADMIN_TEMPLATES`` and triage classified the inbound
    message as pure administrative.
    """
    draft_id: str
    message_id: str
    body: str
    template_id: str = ""           # empty = free-form
    drafted_by: str = "praxis"


@dataclass
class TriageFinding:
    severity: str
    code: str
    message: str


@dataclass
class TriageReport:
    """Triage result for an inbound portal message (+ optional reply draft)."""
    message: PortalMessage
    classification: MessageClass
    action: TriageAction
    clinical_hits: list[str] = field(default_factory=list)
    admin_hits: list[str] = field(default_factory=list)
    findings: list[TriageFinding] = field(default_factory=list)
    autonomous_allowed: bool = False

    @property
    def requires_physician(self) -> bool:
        return self.action == "draft_for_physician"


def _hits(text: str, signals: tuple[str, ...]) -> list[str]:
    """Return signals found in text using word-boundary-aware matching.

    Multi-word signals (e.g. \"chest pain\") match as literal phrases.
    Single-token signals use word boundaries so short tokens like \"sti\"
    do not fire inside \"question\".
    """
    import re
    lower = text.lower()
    found: list[str] = []
    for s in signals:
        if " " in s:
            if s in lower:
                found.append(s)
        else:
            # word-boundary match; escape for regex safety
            if re.search(rf"\b{re.escape(s)}\b", lower):
                found.append(s)
    return found


def classify_portal_message(message: PortalMessage) -> tuple[MessageClass, list[str], list[str]]:
    """Classify an inbound portal message.

    - clinical hits only → clinical
    - admin hits only → administrative
    - both → mixed (treated as clinical for routing)
    - neither → unknown (fail closed → clinical routing)
    """
    text = f"{message.subject}\n{message.body}"
    clinical = _hits(text, _CLINICAL_SIGNALS)
    admin = _hits(text, _ADMIN_SIGNALS)
    if clinical and admin:
        return "mixed", clinical, admin
    if clinical:
        return "clinical", clinical, admin
    if admin:
        return "administrative", clinical, admin
    return "unknown", clinical, admin


def triage_portal_message(
    message: PortalMessage,
    reply: PortalReplyDraft | None = None,
) -> TriageReport:
    """Triage an inbound portal message and optionally gate a reply draft.

    Routing:
    - clinical / mixed / unknown → ``draft_for_physician`` (SEND held)
    - administrative + allowlisted template reply → ``autonomous_reply``
    - administrative + free-form / non-allowlisted reply → ``hold_for_staff``
    - administrative + no reply yet → ``hold_for_staff`` (staff picks template)
    """
    classification, clinical, admin = classify_portal_message(message)
    report = TriageReport(
        message=message,
        classification=classification,
        action="draft_for_physician",
        clinical_hits=clinical,
        admin_hits=admin,
    )

    if classification in ("clinical", "mixed", "unknown"):
        report.action = "draft_for_physician"
        report.autonomous_allowed = False
        if classification == "unknown":
            report.findings.append(TriageFinding(
                "high", "unclassified_fail_closed",
                "message matched no clinical or administrative signals — "
                "fail closed to physician review",
            ))
        elif classification == "mixed":
            report.findings.append(TriageFinding(
                "high", "mixed_content",
                "message contains both clinical and administrative signals — "
                "route as clinical (physician holds SEND)",
            ))
        else:
            report.findings.append(TriageFinding(
                "info", "clinical_content",
                f"clinical signals: {', '.join(clinical[:5])}"
                + ("…" if len(clinical) > 5 else ""),
            ))
        report.findings.append(TriageFinding(
            "info", "physician_hold",
            "clinical portal replies are SEND-held for physician approval — "
            "Praxis never sends clinical advice autonomously",
        ))
        # Even if a template_id is set, clinical never goes autonomous
        if reply and reply.template_id:
            report.findings.append(TriageFinding(
                "medium", "template_ignored_for_clinical",
                f"template {reply.template_id!r} cannot auto-send for "
                f"clinical/mixed/unknown messages",
            ))
        return report

    # Pure administrative
    report.findings.append(TriageFinding(
        "info", "administrative_content",
        f"administrative signals: {', '.join(admin[:5])}"
        + ("…" if len(admin) > 5 else ""),
    ))

    if reply is None:
        report.action = "hold_for_staff"
        report.autonomous_allowed = False
        report.findings.append(TriageFinding(
            "info", "awaiting_template",
            "administrative message — select an allowlisted template for "
            "autonomous reply, or draft free-form for staff approval",
        ))
        return report

    if reply.template_id and reply.template_id in AUTONOMOUS_ADMIN_TEMPLATES:
        expected = AUTONOMOUS_ADMIN_TEMPLATES[reply.template_id]
        # Body must match the allowlisted template (exact) to prevent
        # free-form content riding an allowlisted id.
        if reply.body.strip() == expected.strip():
            report.action = "autonomous_reply"
            report.autonomous_allowed = True
            report.findings.append(TriageFinding(
                "info", "autonomous_allowlist_match",
                f"template {reply.template_id!r} is on the autonomous "
                f"administrative allowlist — reply may send without "
                f"physician approval",
            ))
            return report
        report.action = "hold_for_staff"
        report.autonomous_allowed = False
        report.findings.append(TriageFinding(
            "high", "template_body_mismatch",
            f"template id {reply.template_id!r} is allowlisted but the "
            f"reply body does not match the approved template text — "
            f"hold for staff",
        ))
        return report

    # Free-form administrative reply
    report.action = "hold_for_staff"
    report.autonomous_allowed = False
    if reply.template_id:
        report.findings.append(TriageFinding(
            "medium", "template_not_allowlisted",
            f"template {reply.template_id!r} is not on the autonomous "
            f"allowlist — hold for staff",
        ))
    else:
        report.findings.append(TriageFinding(
            "medium", "freeform_admin_reply",
            "free-form administrative reply is not autonomous — hold for staff",
        ))
    return report


def can_send_autonomously(report: TriageReport) -> bool:
    """True only when triage authorized an autonomous administrative reply."""
    return report.autonomous_allowed and report.action == "autonomous_reply"


def list_autonomous_templates() -> dict[str, str]:
    """Return a copy of the autonomous administrative template allowlist."""
    return dict(AUTONOMOUS_ADMIN_TEMPLATES)


def render_triage_report(report: TriageReport) -> str:
    msg = report.message
    lines = [
        "Portal Message Triage Report",
        f"Message: {msg.message_id} | Patient: {msg.patient_id}",
        f"Subject: {msg.subject}",
        f"Classification: {report.classification} | Action: {report.action}",
        f"Autonomous send: {'YES' if report.autonomous_allowed else 'NO'}",
        "=" * 60,
    ]
    if report.clinical_hits:
        lines.append(f"Clinical hits: {', '.join(report.clinical_hits)}")
    if report.admin_hits:
        lines.append(f"Admin hits: {', '.join(report.admin_hits)}")
    for f in report.findings:
        marker = {
            "critical": "❌", "high": "⚠️", "medium": "•", "info": "i",
        }.get(f.severity, "?")
        lines.append(
            f"  {marker} [{f.severity.upper()}] {f.code}: {f.message}"
        )
    if report.requires_physician:
        lines.append(
            "ROUTE: draft reply → physician approval (SEND held)."
        )
    elif report.autonomous_allowed:
        lines.append("ROUTE: autonomous administrative reply (allowlisted).")
    else:
        lines.append("ROUTE: hold for staff review.")
    return "\n".join(lines)
