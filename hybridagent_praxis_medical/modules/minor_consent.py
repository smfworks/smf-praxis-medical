"""Minor-consent record handling (Gap M4).

Per-jurisdiction record-access gate for adolescent confidential services.

State laws vary on (a) which services a minor may consent to without a
parent (reproductive, STI, behavioral health, substance use) and (b)
whether a parent may access the records of those encounters. Across the
13-state registry every state currently encodes:

- ``minor_consent_services`` — the self-consent categories
- ``minor_parent_access_restricted=True`` — parent portal / record-release
  access to those encounters is restricted
- ``minor_consent_citation`` — the statute

This module enforces the gate:

1. **Classify the encounter.** Is the service in the state's
   ``minor_consent_services`` set? Was the minor the self-consenting
   patient?

2. **Gate parent access.** When the state's
   ``minor_parent_access_restricted`` is True and the encounter is a
   confidential minor-consent service, a parent/guardian request for that
   encounter's records (portal view, after-visit summary, record release)
   is **denied** unless the minor has authorized the release.

3. **Minor authorization.** An explicit ``MinorReleaseAuthorization`` for
   the encounter unlocks parent access. Authorizations are scoped
   (encounter- or service-level), time-bounded, and revocable.

4. **Non-confidential encounters.** Parent access to routine care (well
   visits, non-sensitive services) is unaffected — the gate only fires on
   confidential minor-consent categories.

Praxis never discloses the confidential record content itself. This
module is a **gate**: allow / deny + findings. Portal views, after-visit
summaries, and record-release workflows call it before producing PHI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hybridagent.jurisdictions import get_medical_profile

# Canonical service categories used across the 13-state MEDICAL profiles.
# Profiles store these as free-form strings in the same vocabulary.
ServiceCategory = Literal[
    "reproductive",
    "sti",
    "substance_use",
    "behavioral_health",
    "general",          # non-confidential routine care
    "other",
]

RequesterRole = Literal[
    "minor_patient",
    "parent_guardian",
    "provider",
    "payer",
    "other",
]

AccessChannel = Literal[
    "portal_view",
    "after_visit_summary",
    "record_release",
    "billing_statement",
    "other",
]


@dataclass(frozen=True)
class MinorEncounter:
    """A clinical encounter for a minor patient.

    ``service_category`` is matched against the state's
    ``minor_consent_services``. ``self_consented`` records whether the
    minor (not the parent) consented to the encounter.
    """
    encounter_id: str
    patient_id: str
    state: str                      # two-letter jurisdiction
    service_category: str           # e.g. "sti", "reproductive", "general"
    self_consented: bool = False    # True if the minor self-consented
    patient_age: int = 16           # age at encounter (years)
    encounter_at: float = 0.0


@dataclass(frozen=True)
class MinorReleaseAuthorization:
    """Explicit authorization by the minor to release a confidential
    encounter's records to a named recipient (typically a parent).

    Scoped to one encounter (or optionally one service category for the
    patient). Time-bounded and revocable.
    """
    authorization_id: str
    patient_id: str
    encounter_id: str               # specific encounter unlocked
    authorized_recipient: str       # e.g. parent id / "parent_guardian"
    authorized_at: float
    expires_at: float = 0.0         # 0 = no expiry
    revoked: bool = False
    service_category: str = ""      # optional broader scope (unused for match if empty)


@dataclass(frozen=True)
class AccessRequest:
    """A request to access a minor's encounter records."""
    request_id: str
    encounter_id: str
    requester_role: RequesterRole
    requester_id: str
    channel: AccessChannel = "portal_view"
    purpose: str = "patient_access"


@dataclass
class AccessFinding:
    """A single finding from the minor-consent access gate."""
    severity: str          # critical | high | medium | info
    code: str
    message: str
    state: str


@dataclass
class AccessReport:
    """Full minor-consent access-gate report."""
    request: AccessRequest
    encounter: MinorEncounter
    allowed: bool = False
    confidential: bool = False     # True if encounter is a confidential service
    findings: list[AccessFinding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def summary(self) -> str:
        n = len(self.findings)
        return (f"{n} finding(s); confidential={self.confidential}; "
                f"allowed={self.allowed}")


class MinorConsentError(Exception):
    """Raised when a confidential minor record is accessed without authorization."""


def is_confidential_minor_service(
    encounter: MinorEncounter,
) -> bool:
    """True if the encounter is a confidential minor-consent service in
    the patient's state AND the minor self-consented.

    Non-self-consented encounters (parent-consented) are not treated as
    confidential under this gate even if the service category matches —
    the parent already participates in care.
    """
    prof = get_medical_profile(encounter.state)
    if prof is None:
        return False
    aliases = {
        "hiv": "sti", "std": "sti", "mental_health": "behavioral_health",
        "substance": "substance_use", "sud": "substance_use",
    }
    category = encounter.service_category.lower().strip()
    category = aliases.get(category, category)
    services = {s.lower() for s in prof.minor_consent_services}
    return encounter.self_consented and (
        category in services or category not in {"general", "routine", "other", ""}
    )


def _authorization_covers(
    auth: MinorReleaseAuthorization,
    encounter: MinorEncounter,
    requester: AccessRequest,
    *,
    now: float,
) -> bool:
    """True if this authorization unlocks the request."""
    if auth.revoked:
        return False
    if (not auth.authorization_id.strip() or auth.authorized_at <= 0 or
            auth.authorized_at > now):
        return False
    if auth.patient_id != encounter.patient_id:
        return False
    if auth.encounter_id != encounter.encounter_id:
        return False
    if auth.expires_at and auth.expires_at < now:
        return False
    # Recipient match: exact id, or role-level "parent_guardian"
    if auth.authorized_recipient not in (
        requester.requester_id,
        requester.requester_role,
        "parent_guardian",
    ):
        # allow if requester is parent_guardian and auth is to any parent role
        if not (
            requester.requester_role == "parent_guardian"
            and auth.authorized_recipient in ("parent_guardian", "parent", "guardian")
        ):
            return False
    return True


def check_minor_record_access(
    request: AccessRequest,
    encounter: MinorEncounter,
    authorizations: list[MinorReleaseAuthorization] | None = None,
    *,
    now: float = 0.0,
) -> AccessReport:
    """Gate access to a minor's encounter records.

    Decision table:
    - Unknown jurisdiction → deny (fail closed)
    - Adult patient (age >= 18) → allow (gate does not apply)
    - Non-confidential encounter → allow (parent may access routine care)
    - Confidential + requester is the minor patient → allow
    - Confidential + requester is provider → allow (treatment/ops)
    - Confidential + parent/guardian + authorization covers → allow
    - Confidential + parent/guardian + no authorization → **deny**
    - Confidential + other roles → deny (fail closed)
    """
    import time as _t
    now_ts = _t.time() if now == 0.0 else now
    report = AccessReport(request=request, encounter=encounter)
    state = encounter.state.upper()

    if (request.encounter_id != encounter.encounter_id or
            not request.request_id.strip() or not request.requester_id.strip() or
            not encounter.encounter_id.strip() or not encounter.patient_id.strip()):
        report.findings.append(AccessFinding(
            "critical", "identity_mismatch",
            "request and encounter identities are missing or do not match", state,
        ))
        return report

    prof = get_medical_profile(state)
    if prof is None:
        report.findings.append(AccessFinding(
            "critical", "unknown_jurisdiction",
            f"patient state {state!r} is not in the 13-state medical "
            f"registry — cannot evaluate minor-consent rules",
            state,
        ))
        report.allowed = False
        return report

    # Adult patients — gate does not apply
    if encounter.patient_age >= 18:
        report.allowed = True
        report.confidential = False
        report.findings.append(AccessFinding(
            "info", "adult_patient",
            f"patient age {encounter.patient_age} is adult — "
            f"minor-consent gate does not apply",
            state,
        ))
        return report

    confidential = is_confidential_minor_service(encounter)
    report.confidential = confidential

    if not confidential:
        report.allowed = True
        report.findings.append(AccessFinding(
            "info", "not_confidential",
            f"encounter service {encounter.service_category!r} is not a "
            f"self-consented confidential service under {state} rules — "
            f"parent access unrestricted by this gate",
            state,
        ))
        return report

    # Confidential encounter from here
    report.findings.append(AccessFinding(
        "info", "confidential_service",
        f"encounter is a confidential minor-consent service "
        f"({encounter.service_category}) under {state} "
        f"({prof.minor_consent_citation or 'see MedicalProfile'})",
        state,
    ))

    role = request.requester_role

    # Minor patient themselves may always access their own records
    if role == "minor_patient":
        report.allowed = request.requester_id == encounter.patient_id
        report.findings.append(AccessFinding(
            "info" if report.allowed else "critical",
            "minor_self_access" if report.allowed else "minor_identity_mismatch",
            "minor patient accessing their own confidential records — allowed"
            if report.allowed else "requester identity does not match the patient",
            state,
        ))
        return report

    # Treating providers may access for treatment
    if role == "provider":
        report.allowed = True
        report.findings.append(AccessFinding(
            "info", "provider_access",
            "provider access to confidential minor-consent records "
            "(treatment purpose) — allowed",
            state,
        ))
        return report

    # Parent/guardian path
    if role == "parent_guardian":
        if not prof.minor_parent_access_restricted:
            # State does not restrict parent access (none of the 13 currently)
            report.allowed = True
            report.findings.append(AccessFinding(
                "info", "parent_access_not_restricted",
                f"{state} does not restrict parent access to minor-consent "
                f"service records — allowed",
                state,
            ))
            return report

        # Restricted — need authorization
        auths = authorizations or []
        covering = [
            a for a in auths
            if _authorization_covers(a, encounter, request, now=now_ts)
        ]
        if covering:
            report.allowed = True
            report.findings.append(AccessFinding(
                "info", "minor_authorization_present",
                f"minor authorized release to "
                f"{covering[0].authorized_recipient} "
                f"(auth {covering[0].authorization_id}) — parent access allowed",
                state,
            ))
            return report

        report.allowed = False
        report.findings.append(AccessFinding(
            "critical", "parent_access_denied",
            f"parent/guardian access to confidential "
            f"{encounter.service_category} encounter denied under {state} "
            f"minor-consent rules — requires the minor's explicit "
            f"authorization (citation: "
            f"{prof.minor_consent_citation or 'see MedicalProfile'})",
            state,
        ))
        return report

    # Other roles (payer, other) — fail closed for confidential records
    report.allowed = False
    report.findings.append(AccessFinding(
        "critical", "role_not_authorized",
        f"requester role {role!r} is not authorized to access confidential "
        f"minor-consent records without explicit release",
        state,
    ))
    return report


def assert_minor_access_allowed(report: AccessReport) -> AccessReport:
    """Fail-hard variant: raise if access is blocked."""
    if report.blocked:
        msgs = "; ".join(
            f.message for f in report.findings if f.severity == "critical"
        )
        raise MinorConsentError(
            f"access {report.request.request_id} blocked: "
            f"{msgs or report.summary()}"
        )
    return report


def render_access_report(report: AccessReport) -> str:
    """Render the minor-consent access-gate report for staff/audit review."""
    req = report.request
    enc = report.encounter
    lines = [
        "Minor-Consent Record Access Gate Report",
        (f"Request: {req.request_id} | Channel: {req.channel} | "
        f"Role: {req.requester_role} ({req.requester_id})"),
        (f"Encounter: {enc.encounter_id} | Patient: {enc.patient_id} | "
        f"Age: {enc.patient_age} | State: {enc.state}"),
        (f"Service: {enc.service_category} | Self-consented: "
        f"{enc.self_consented}"),
        (f"Decision: {'ALLOWED' if report.allowed else 'DENIED'} "
        f"(confidential={report.confidential})"),
        "=" * 60,
    ]
    if not report.findings:
        lines.append("No findings.")
    for f in report.findings:
        marker = {
            "critical": "❌", "high": "⚠️", "medium": "•", "info": "i",
        }.get(f.severity, "?")
        lines.append(
            f"  {marker} [{f.severity.upper()}] {f.code}: {f.message}"
        )
    lines.append(f"Summary: {report.summary()}")
    if report.blocked:
        lines.append("DENIED — do not disclose this encounter's records.")
    else:
        lines.append("ALLOWED — proceed under the minimum-necessary standard.")
    return "\n".join(lines)
