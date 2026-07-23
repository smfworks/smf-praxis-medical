"""Clinical-drafting governance (Gap M3 — never write to the chart autonomously).

The medical vertical's version of the law-firm UPL guardrail, and it's
stricter because the chart is the legal medical record. Praxis **never
writes to the EHR autonomously** — ambient documentation drafts the SOAP
note, the physician reviews and signs, and only then does the draft enter
the chart. The governance broker already holds SEND-class actions for
physician approval; this module is the attestation surface that records
*who signed, when, which draft, which chart* — the evidence that the
physician reviewed the AI-drafted content before it became part of the
legal medical record.

Design:
- ``ClinicalDraft`` — a drafted clinical artifact (SOAP note, after-visit
  summary, order, prescription draft, results letter) with the chart id,
  the patient id, the draft content, and the draft's provenance.
- ``ClinicalAttestation`` — the physician's recorded review-and-sign: the
  draft id, the physician id, the timestamp, any edits the physician made,
  and the attestation type (signed / amended / rejected).
- ``AttestationLedger`` — the append-only ledger of attestations. A chart
  write is blocked unless ``has_attestation(draft_id)`` returns True.

The governance line is non-negotiable: the chart is the legal medical
record, and an AI writing to it without physician sign-off is the
unauthorized practice of medicine. This module is the evidence surface
that the sign-off happened.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AttestationType = Literal["signed", "amended", "rejected"]


@dataclass(frozen=True)
class ClinicalDraft:
    """A drafted clinical artifact awaiting physician review.

    Praxis drafts this; the physician reviews + signs (or amends, or
    rejects) via a ``ClinicalAttestation``. The draft never enters the
    chart until an attestation of type ``signed`` or ``amended`` exists.
    """
    draft_id: str               # unique id for this draft
    chart_id: str               # the EHR chart/encounter id it targets
    patient_id: str             # the patient (PHI reference)
    artifact_type: str          # "soap_note", "after_visit_summary", "order", "rx_draft", "results_letter"
    content_hash: str           # hash of the draft content (integrity)
    drafted_by: str = "praxis"  # provenance — "praxis" or a staff id
    drafted_at: float = 0.0     # timestamp
    visit_audio_retained: bool = False  # for ambient documentation: is the visit audio kept?


@dataclass(frozen=True)
class ClinicalAttestation:
    """A physician's recorded review-and-sign on a clinical draft.

    This is the evidence that the physician reviewed the AI-drafted content
    before it entered the chart. ``signed`` = approved as-is; ``amended`` =
    approved with edits (the edits are recorded); ``rejected`` = the draft
    was not entered into the chart.
    """
    attestation_id: str
    draft_id: str               # the ClinicalDraft this attests
    physician_id: str           # the licensed physician who signed
    attestation_type: AttestationType
    attested_at: float          # timestamp
    edits_summary: str = ""     # for "amended": summary of the physician's edits
    edit_hash: str = ""         # for "amended": hash of the final (post-edit) content


class AttestationError(Exception):
    """Raised when a chart write is attempted without a valid attestation,
    or when an attestation is recorded for a draft that doesn't exist."""


class AttestationLedger:
    """Append-only ledger of clinical attestations.

    The chart-write gate: ``can_write_chart(draft_id)`` returns True only
    if a ``signed`` or ``amended`` attestation exists for that draft. This
    is the enforcement surface for the never-write-to-chart rule — the
    governance broker holds the SEND-class chart-write action, and this
    ledger is the evidence that the physician sign-off happened.
    """

    def __init__(self) -> None:
        self._drafts: dict[str, ClinicalDraft] = {}
        self._attestations: list[ClinicalAttestation] = []

    def register_draft(self, draft: ClinicalDraft) -> None:
        """Register a draft so it can be attested. A draft must be registered
        before an attestation can be recorded for it."""
        if not all((draft.draft_id.strip(), draft.chart_id.strip(),
                    draft.patient_id.strip(), draft.content_hash.strip())):
            raise AttestationError("draft identity and content_hash are required")
        existing = self._drafts.get(draft.draft_id)
        if existing is not None and existing != draft:
            raise AttestationError(
                f"draft {draft.draft_id} is immutable and cannot be overwritten")
        self._drafts[draft.draft_id] = draft

    def get_draft(self, draft_id: str) -> ClinicalDraft | None:
        return self._drafts.get(draft_id)

    def attest(self, attestation: ClinicalAttestation) -> ClinicalAttestation:
        """Record a physician's review-and-sign. The draft must be registered.
        Returns the recorded attestation. Raises if the draft doesn't exist."""
        if attestation.draft_id not in self._drafts:
            raise AttestationError(
                f"cannot attest draft {attestation.draft_id} — not registered")
        if not all((attestation.attestation_id.strip(),
                    attestation.physician_id.strip())) or attestation.attested_at <= 0:
            raise AttestationError("attestation identity, physician, and timestamp are required")
        if attestation.attestation_type not in {"signed", "amended", "rejected"}:
            raise AttestationError("unsupported attestation type")
        if attestation.attestation_type == "amended" and not attestation.edit_hash.strip():
            raise AttestationError("amended attestation requires final edit_hash")
        existing = [a for a in self._attestations if a.draft_id == attestation.draft_id]
        if existing:
            if attestation in existing:
                return attestation
            raise AttestationError(
                f"draft {attestation.draft_id} already has a terminal attestation")
        self._attestations.append(attestation)
        return attestation

    def can_write_chart(self, draft_id: str) -> bool:
        """The chart-write gate. Returns True only if a ``signed`` or
        ``amended`` attestation exists for this draft. This is what the
        governance broker's SEND-hold for chart writes checks before
        releasing the draft to the EHR."""
        return any(
            a.draft_id == draft_id and a.attestation_type in ("signed", "amended")
            for a in self._attestations
        )

    def has_attestation(self, draft_id: str) -> bool:
        """Whether any attestation (incl. rejected) exists for this draft."""
        return any(a.draft_id == draft_id for a in self._attestations)

    def latest_attestation(self, draft_id: str) -> ClinicalAttestation | None:
        """The most recent attestation for a draft, or None."""
        hits = [a for a in self._attestations if a.draft_id == draft_id]
        if not hits:
            return None
        return max(hits, key=lambda a: a.attested_at)

    def all_attestations(self) -> list[ClinicalAttestation]:
        """All attestations, in record order (for audit export)."""
        return list(self._attestations)

    def pending_drafts(self) -> list[ClinicalDraft]:
        """Drafts that are registered but not yet signed or amended (still
        awaiting physician review)."""
        signed = {a.draft_id for a in self._attestations
                  if a.attestation_type in ("signed", "amended")}
        rejected = {a.draft_id for a in self._attestations
                    if a.attestation_type == "rejected"}
        return [d for d in self._drafts.values()
                if d.draft_id not in signed and d.draft_id not in rejected]


def require_attestation(ledger: AttestationLedger, draft_id: str) -> None:
    """Raise ``AttestationError`` if the draft has no signed/amended
    attestation. Call this before any chart-write path to enforce the
    never-write-to-chart rule."""
    if not ledger.can_write_chart(draft_id):
        raise AttestationError(
            f"chart write blocked for draft {draft_id} — no physician "
            f"attestation (signed or amended). The chart is the legal "
            f"medical record; Praxis never writes to it autonomously.")


def render_attestation_log(ledger: AttestationLedger) -> str:
    """Render the attestation ledger as an audit-ready text log."""
    lines = ["Clinical Attestation Log", "=" * 60]
    for a in ledger.all_attestations():
        d = ledger.get_draft(a.draft_id)
        chart = d.chart_id if d else "?"
        patient = d.patient_id if d else "?"
        atype = a.attestation_type
        edits = f" | edits: {a.edits_summary}" if a.edits_summary else ""
        lines.append(
            f"  [{a.attested_at}] draft={a.draft_id} chart={chart} "
            f"patient={patient} physician={a.physician_id} "
            f"type={atype}{edits}")
    lines.append(f"Total: {len(ledger.all_attestations())} attestation(s)")
    return "\n".join(lines)