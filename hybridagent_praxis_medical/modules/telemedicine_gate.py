"""Telemedicine cross-state-practice gate (Gap M6).

Block a tele-visit workflow if the physician is not licensed in the
patient's state. The MedicalProfile registry supplies the per-state rules:

1. **License match.** The physician must hold a current medical license
   in the patient's location state. No multi-state automatic authority —
   IMLC membership expedites *obtaining* a license; it does not authorize
   practice without one.

2. **FL out-of-state telehealth registration (§456.47).** Florida's
   ``telemedicine_requirement=\"registration\"`` path allows an out-of-state
   physician who holds a valid Florida telehealth provider registration
   to treat Florida patients without a full FL medical license. The gate
   accepts that registration as the license substitute for FL only.

3. **PA + MA non-IMLC.** Pennsylvania and Massachusetts are **not**
   Interstate Medical Licensure Compact members. For patients in those
   states the gate is stricter in messaging (no Compact expedited path)
   and still requires a full PA/MA license — there is no registration
   exception equivalent to Florida's.

Praxis never conducts the tele-visit. This module is a **gate**: it
returns an allow/block decision with findings. The visit is blocked when
the physician lacks authority in the patient's state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hybridagent.jurisdictions import get_medical_profile

AuthorityPath = Literal[
    "in_state_license",
    "fl_telehealth_registration",
    "none",
]


@dataclass(frozen=True)
class PhysicianLicense:
    """An asserted medical license held by a physician in one state.

    Praxis records asserted credentials; the practice verifies with the
    board (no board-API verification in the base pack).
    """
    physician_id: str
    state: str                 # two-letter code where licensed
    license_number: str
    expires: str               # ISO date YYYY-MM-DD
    active: bool = True


@dataclass(frozen=True)
class FLTelehealthRegistration:
    """Florida out-of-state telehealth provider registration (§456.47).

    Acceptable only when the patient is located in Florida and the
    physician does not hold a full FL medical license.
    """
    physician_id: str
    registration_number: str
    expires: str               # ISO date
    active: bool = True


@dataclass(frozen=True)
class TeleVisit:
    """A proposed telemedicine visit awaiting the cross-state license gate.

    ``patient_state`` is the patient's location at the time of the visit —
    that is the jurisdiction whose medical-practice rules apply.
    """
    visit_id: str
    physician_id: str
    patient_id: str
    patient_state: str         # two-letter code — the governing jurisdiction
    visit_at: float = 0.0
    modality: str = "synchronous_video"  # informational


@dataclass
class GateFinding:
    """A single finding from the telemedicine license gate."""
    severity: str          # critical | high | medium | info
    code: str              # not_licensed | fl_registration_ok | non_imlc | ...
    message: str
    state: str             # the patient-location jurisdiction


@dataclass
class GateReport:
    """Full telemedicine license-gate report for a proposed tele-visit."""
    visit: TeleVisit
    allowed: bool = False
    authority_path: AuthorityPath = "none"
    findings: list[GateFinding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """Inverse of ``allowed`` — convenience for the broker surface."""
        return not self.allowed

    def summary(self) -> str:
        n = len(self.findings)
        crit = sum(1 for f in self.findings if f.severity == "critical")
        return (f"{n} finding(s), {crit} critical; "
                f"allowed={self.allowed}, path={self.authority_path}")


class TelemedicineGateError(Exception):
    """Raised when a tele-visit is blocked and the caller requested fail-hard."""


def _parse_expiry(iso_date: str) -> float:
    """Parse an ISO date to a timestamp; return 0.0 on failure."""
    if not iso_date:
        return 0.0
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso_date).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _license_current(lic: PhysicianLicense, *, now: float) -> bool:
    if not lic.active or not lic.license_number:
        return False
    exp = _parse_expiry(lic.expires)
    if exp and exp < now:
        return False
    return True


def _fl_registration_current(reg: FLTelehealthRegistration, *, now: float) -> bool:
    if not reg.active or not reg.registration_number:
        return False
    exp = _parse_expiry(reg.expires)
    if exp and exp < now:
        return False
    return True


def check_telemedicine_license(
    visit: TeleVisit,
    licenses: list[PhysicianLicense],
    *,
    fl_registration: FLTelehealthRegistration | None = None,
    now: float = 0.0,
    require_consent_documented: bool = True,
) -> GateReport:
    """Gate a tele-visit against the patient's-state licensing rules.

    Order of evaluation:
    1. Patient state must be in the MedicalProfile registry.
    2. Physician holds a current license in the patient's state → allow
       (``in_state_license``).
    3. Else, patient is in FL and physician has a current FL telehealth
       registration → allow (``fl_telehealth_registration``).
    4. Else → block. Annotate with non-IMLC messaging when patient is in
       PA or MA, and with Compact-expedite info when the state is an
       IMLC member (expedite does not authorize practice without a license).

    Optional: if the state's profile requires documented telemedicine
    consent, emit an info finding (does not block — consent capture is a
    separate workflow surface).
    """
    import time as _t
    now_ts = _t.time() if now == 0.0 else now
    report = GateReport(visit=visit)
    patient_state = visit.patient_state.upper()

    prof = get_medical_profile(patient_state)
    if prof is None:
        report.findings.append(GateFinding(
            "critical", "unknown_jurisdiction",
            f"patient state {patient_state!r} is not in the 13-state "
            f"medical registry — cannot evaluate telemedicine authority",
            patient_state,
        ))
        report.allowed = False
        report.authority_path = "none"
        return report

    # Collect this physician's current licenses
    physician_licenses = [
        lic for lic in licenses
        if lic.physician_id == visit.physician_id
        and _license_current(lic, now=now_ts)
    ]
    licensed_states = {lic.state.upper() for lic in physician_licenses}

    # Path 1: in-state license
    if patient_state in licensed_states:
        report.allowed = True
        report.authority_path = "in_state_license"
        report.findings.append(GateFinding(
            "info", "in_state_license",
            f"physician holds a current {patient_state} medical license — "
            f"tele-visit authorized",
            patient_state,
        ))
    else:
        # Path 2: FL telehealth registration exception
        fl_ok = False
        if (
            patient_state == "FL"
            and prof.telemedicine_requirement == "registration"
            and fl_registration is not None
            and fl_registration.physician_id == visit.physician_id
            and _fl_registration_current(fl_registration, now=now_ts)
        ):
            fl_ok = True
            report.allowed = True
            report.authority_path = "fl_telehealth_registration"
            report.findings.append(GateFinding(
                "info", "fl_registration_ok",
                f"physician holds current FL telehealth registration "
                f"{fl_registration.registration_number} (§456.47) — "
                f"tele-visit authorized without full FL medical license",
                "FL",
            ))
        elif patient_state == "FL" and fl_registration is not None:
            # Registration present but not current / wrong physician
            if fl_registration.physician_id != visit.physician_id:
                report.findings.append(GateFinding(
                    "high", "fl_registration_mismatch",
                    "FL telehealth registration is for a different physician",
                    "FL",
                ))
            elif not _fl_registration_current(fl_registration, now=now_ts):
                report.findings.append(GateFinding(
                    "high", "fl_registration_expired",
                    f"FL telehealth registration expired "
                    f"{fl_registration.expires} or inactive",
                    "FL",
                ))

        if not fl_ok:
            report.allowed = False
            report.authority_path = "none"
            # Core block finding
            report.findings.append(GateFinding(
                "critical", "not_licensed",
                f"physician is not licensed in patient state {patient_state} "
                f"(licensed in: {sorted(licensed_states) or 'none'}) — "
                f"tele-visit blocked",
                patient_state,
            ))

            # Non-IMLC vs IMLC messaging
            if not prof.imlc_member:
                report.findings.append(GateFinding(
                    "high", "non_imlc_jurisdiction",
                    f"{patient_state} is NOT an IMLC member — no Compact "
                    f"expedited licensure path. Physician must obtain a full "
                    f"{patient_state} medical license before treating "
                    f"{patient_state} patients via telemedicine",
                    patient_state,
                ))
            else:
                report.findings.append(GateFinding(
                    "info", "imlc_expedite_available",
                    f"{patient_state} is an IMLC member — Compact expedites "
                    f"obtaining a {patient_state} license but does NOT "
                    f"authorize practice without one. Obtain the license "
                    f"(or, for FL, §456.47 registration) before the visit",
                    patient_state,
                ))

            # FL-specific: registration path exists but wasn't used
            if (
                patient_state == "FL"
                and prof.telemedicine_requirement == "registration"
                and fl_registration is None
            ):
                report.findings.append(GateFinding(
                    "medium", "fl_registration_available",
                    "FL §456.47 out-of-state telehealth registration is an "
                    "alternative to a full FL medical license — register or "
                    "obtain a FL license",
                    "FL",
                ))

    # Prior in-person exam requirement (none of the 13 currently require it,
    # but the gate surfaces the rule if a future profile flips the flag).
    if prof.telemedicine_prior_in_person:
        report.findings.append(GateFinding(
            "high", "prior_in_person_required",
            f"{patient_state} requires a prior in-person exam before "
            f"telemedicine — verify established relationship exists",
            patient_state,
        ))
        # Does not auto-block (relationship status is external), but elevates
        # the surface for the physician to confirm.

    # Consent documentation reminder (info only — capture is a separate surface)
    if require_consent_documented and prof.telemedicine_consent_documented:
        report.findings.append(GateFinding(
            "info", "consent_must_be_documented",
            f"{patient_state} requires documented consent for telehealth — "
            f"capture consent before the visit proceeds",
            patient_state,
        ))

    return report


def assert_telemedicine_allowed(report: GateReport) -> GateReport:
    """Fail-hard variant: raise if the tele-visit is blocked."""
    if report.blocked:
        msgs = "; ".join(
            f.message for f in report.findings if f.severity == "critical"
        )
        raise TelemedicineGateError(
            f"tele-visit {report.visit.visit_id} blocked: {msgs or report.summary()}"
        )
    return report


def render_gate_report(report: GateReport) -> str:
    """Render the telemedicine license-gate report for physician/staff review."""
    v = report.visit
    lines = [
        "Telemedicine Cross-State License Gate Report",
        f"Visit: {v.visit_id} | Physician: {v.physician_id} | "
        f"Patient: {v.patient_id}",
        f"Patient location: {v.patient_state} | Modality: {v.modality}",
        f"Decision: {'ALLOWED' if report.allowed else 'BLOCKED'} "
        f"(path={report.authority_path})",
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
        lines.append("BLOCKED — do not proceed with the tele-visit.")
    else:
        lines.append("ALLOWED — proceed (physician remains responsible for "
                     "standard of care).")
    return "\n".join(lines)
