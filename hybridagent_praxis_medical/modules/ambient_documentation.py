"""Ambient clinical documentation governance (Gap M8).

The #1 medical-office use case: ambient capture of a visit → draft SOAP
note + after-visit summary → physician sign-off → chart write.

This module is the medical-specific extension of clinical-drafting
governance (M3) focused on the documentation workflow:

1. **Visit-consent capture.** The patient consented to AI listening /
   ambient capture before recording begins. No consent → no ambient
   session.

2. **Draft-to-chart attestation.** The ambient session produces a
   ``ClinicalDraft``; the chart write is blocked until a physician
   ``ClinicalAttestation`` (signed/amended) exists — reuses M3.

3. **Audio retention policy.** Visit audio is itself a medical record
   segment. Retention follows the state's adult/minor medical-record
   rules (M9). Default posture: retain only as long as the clinical
   note needs the audio for amendment; purge recommendation surfaces
   after the note is signed (or at the state's retention ceiling).

Praxis never writes to the chart autonomously and never diagnoses.
Ambient documentation drafts; the physician signs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .clinical_attestation import (
    AttestationLedger,
    ClinicalAttestation,
    ClinicalDraft,
)
from .records_retention import (
    MedicalRecordSet,
    assess_retention,
)

ConsentStatus = Literal["captured", "declined", "revoked", "missing"]
SessionStatus = Literal[
    "pending_consent",
    "recording",
    "drafting",
    "awaiting_attestation",
    "signed",
    "rejected",
    "cancelled",
]


@dataclass(frozen=True)
class VisitConsent:
    """Patient consent for ambient AI listening during a visit."""
    consent_id: str
    patient_id: str
    visit_id: str
    state: str
    status: ConsentStatus
    captured_at: float = 0.0
    captured_by: str = ""          # staff id who recorded consent
    method: str = "verbal"         # verbal | written | portal
    notes: str = ""


@dataclass
class AmbientSession:
    """An ambient documentation session for one visit."""
    session_id: str
    visit_id: str
    patient_id: str
    physician_id: str
    state: str
    consent_id: str = ""
    status: SessionStatus = "pending_consent"
    started_at: float = 0.0
    ended_at: float = 0.0
    audio_retained: bool = False
    audio_record_id: str = ""      # links to MedicalRecordSet for retention
    draft_id: str = ""             # ClinicalDraft produced from the audio
    patient_was_minor: bool = False
    findings: list[str] = field(default_factory=list)


class AmbientDocumentationError(Exception):
    """Raised when ambient workflow is blocked (no consent, no attestation)."""


def capture_consent(consent: VisitConsent) -> VisitConsent:
    """Validate a visit-consent record. Declined/missing/revoked cannot
    authorize an ambient session."""
    if consent.status not in ("captured", "declined", "revoked", "missing"):
        raise AmbientDocumentationError(
            f"invalid consent status: {consent.status!r}"
        )
    return consent


def start_ambient_session(
    session: AmbientSession,
    consent: VisitConsent,
    *,
    now: float = 0.0,
) -> AmbientSession:
    """Start ambient recording only with captured consent for this visit."""
    import time as _t
    now_ts = _t.time() if now == 0.0 else now

    if consent.patient_id != session.patient_id:
        raise AmbientDocumentationError(
            "consent patient_id does not match ambient session"
        )
    if consent.visit_id != session.visit_id:
        raise AmbientDocumentationError(
            "consent visit_id does not match ambient session"
        )
    if consent.status != "captured":
        raise AmbientDocumentationError(
            f"cannot start ambient session without captured consent "
            f"(status={consent.status})"
        )

    session.consent_id = consent.consent_id
    session.status = "recording"
    session.started_at = now_ts
    session.audio_retained = True
    session.audio_record_id = session.audio_record_id or f"audio-{session.session_id}"
    session.findings.append(
        f"consent {consent.consent_id} captured via {consent.method} — "
        f"ambient recording authorized"
    )
    return session


def end_recording(
    session: AmbientSession,
    *,
    now: float = 0.0,
) -> AmbientSession:
    """Mark recording ended; session moves to drafting."""
    import time as _t
    now_ts = _t.time() if now == 0.0 else now
    if session.status != "recording":
        raise AmbientDocumentationError(
            f"cannot end recording from status={session.status}"
        )
    session.ended_at = now_ts
    session.status = "drafting"
    session.findings.append("recording ended — drafting clinical note")
    return session


def attach_draft(
    session: AmbientSession,
    draft: ClinicalDraft,
    ledger: AttestationLedger,
) -> AmbientSession:
    """Register the ambient-produced clinical draft with the attestation
    ledger and mark the session awaiting physician sign-off."""
    if session.status not in ("drafting", "awaiting_attestation"):
        raise AmbientDocumentationError(
            f"cannot attach draft from status={session.status}"
        )
    if draft.patient_id != session.patient_id:
        raise AmbientDocumentationError("draft patient_id mismatch")
    # Mark ambient provenance
    if not draft.visit_audio_retained and session.audio_retained:
        # ClinicalDraft is frozen — we record the link via session only.
        session.findings.append(
            "draft produced with visit audio retained (see audio_record_id)"
        )
    ledger.register_draft(draft)
    session.draft_id = draft.draft_id
    session.status = "awaiting_attestation"
    session.findings.append(
        f"draft {draft.draft_id} registered — awaiting physician attestation"
    )
    return session


def complete_with_attestation(
    session: AmbientSession,
    attestation: ClinicalAttestation,
    ledger: AttestationLedger,
) -> AmbientSession:
    """Record physician attestation; only signed/amended unlocks chart write."""
    if session.status != "awaiting_attestation":
        raise AmbientDocumentationError(
            f"cannot complete from status={session.status}"
        )
    if attestation.draft_id != session.draft_id:
        raise AmbientDocumentationError("attestation draft_id mismatch")
    ledger.attest(attestation)
    if attestation.attestation_type == "rejected":
        session.status = "rejected"
        session.findings.append(
            f"physician rejected draft {session.draft_id}"
        )
    elif attestation.attestation_type in ("signed", "amended"):
        session.status = "signed"
        session.findings.append(
            f"physician {attestation.attestation_type} draft "
            f"{session.draft_id} — chart write authorized"
        )
    else:
        raise AmbientDocumentationError(
            f"unknown attestation type: {attestation.attestation_type}"
        )
    return session


def can_write_chart(session: AmbientSession, ledger: AttestationLedger) -> bool:
    """True only when the ambient session is signed and the ledger permits
    the chart write for the attached draft."""
    if session.status != "signed" or not session.draft_id:
        return False
    return ledger.can_write_chart(session.draft_id)


def assert_chart_write_allowed(
    session: AmbientSession,
    ledger: AttestationLedger,
) -> None:
    if not can_write_chart(session, ledger):
        raise AmbientDocumentationError(
            f"chart write blocked for session {session.session_id} "
            f"(status={session.status}, draft={session.draft_id or 'none'})"
        )


def audio_retention_assessment(
    session: AmbientSession,
    *,
    now: float = 0.0,
):
    """Assess retention of the visit audio under the state's medical-record
    rules. Audio is treated as a medical record set segment."""
    if not session.audio_retained or not session.audio_record_id:
        return None
    last_visit = session.ended_at or session.started_at or 0.0
    rec = MedicalRecordSet(
        record_id=session.audio_record_id,
        patient_id=session.patient_id,
        state=session.state,
        last_visit_at=last_visit,
        patient_was_minor=session.patient_was_minor,
    )
    return assess_retention(rec, now=now)


def recommend_audio_purge_after_signoff(
    session: AmbientSession,
    *,
    keep_days_after_signoff: int = 30,
    now: float = 0.0,
) -> list[str]:
    """Recommend purging visit audio after the note is signed, subject to
    the state's retention floor. Default: keep 30 days post-signoff for
    amendment support, then surface a purge recommendation (never auto-
    delete — DESTRUCTIVE held).
    """
    import time as _t
    now_ts = _t.time() if now == 0.0 else now
    findings: list[str] = []
    if session.status != "signed":
        findings.append(
            "audio purge not recommended until the clinical note is signed"
        )
        return findings
    if not session.audio_retained:
        findings.append("no audio retained")
        return findings

    assessment = audio_retention_assessment(session, now=now_ts)
    if assessment is None:
        findings.append("no audio record to assess")
        return findings

    # State floor: cannot purge before dispose_after if still active
    if assessment.status == "active":
        findings.append(
            f"state retention still active "
            f"(~{assessment.retention_years}y rule) — keep audio; "
            f"dispose_after={assessment.dispose_after}"
        )
        # Still note the operational preference for short retention post-signoff
        findings.append(
            f"operational preference: re-evaluate purge "
            f"{keep_days_after_signoff} days after sign-off, subject to "
            f"state floor"
        )
        return findings

    if assessment.status == "legal_hold":
        findings.append("LEGAL HOLD — do not purge audio")
        return findings

    findings.append(
        "state retention elapsed or not blocking — recommend purge "
        "review (DESTRUCTIVE, dual approval; Praxis never deletes)"
    )
    return findings


def render_ambient_session(session: AmbientSession) -> str:
    lines = [
        "Ambient Documentation Session",
        f"Session: {session.session_id} | Visit: {session.visit_id}",
        f"Patient: {session.patient_id} | Physician: {session.physician_id}",
        f"State: {session.state} | Status: {session.status}",
        f"Consent: {session.consent_id or 'none'} | "
        f"Draft: {session.draft_id or 'none'}",
        f"Audio retained: {session.audio_retained} "
        f"({session.audio_record_id or 'n/a'})",
        "=" * 60,
    ]
    for f in session.findings:
        lines.append(f"  • {f}")
    return "\n".join(lines)
