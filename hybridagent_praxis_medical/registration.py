"""Registration — wire the medical vertical into the Praxis base registry.

Called once on import of :mod:`hybridagent_praxis_medical`. Registers:

  * the ``medical_office`` and ``medical`` :class:`VerticalSpec` rows,
  * a factory returning the 5 manual medical-office eval cases.

The manual eval cases import the vertical-specific modules lazily (inside
the factory's runnables), so simply installing this package does not
import the compliance modules — they load only when an eval case or
dashboard route actually executes.
"""

from __future__ import annotations

from hybridagent.broker import RiskClass
from hybridagent.evals import EvalCase
from hybridagent.verticals.registry import (
    VerticalSpec,
    register_vertical_eval_cases,
    register_vertical_spec,
)


_MEDICAL_OFFICE_SPEC = VerticalSpec(
    name="medical_office",
    persona_keyword="medical office",
    compliance_mode="enforced",
    autonomous={RiskClass.READ, RiskClass.DRAFT},
    held={RiskClass.SEND, RiskClass.DESTRUCTIVE},
    version="0.1.0",
)

_MEDICAL_CLINICAL_SPEC = VerticalSpec(
    name="medical",
    persona_keyword="clinical",
    compliance_mode="enforced",
    autonomous={RiskClass.READ},
    held={RiskClass.SEND, RiskClass.DESTRUCTIVE},
    version="0.1.0",
)


def _never_write_chart_case():
    def run() -> tuple[bool, str]:
        from .modules.clinical_attestation import (
            AttestationError,
            AttestationLedger,
            ClinicalDraft,
            require_attestation,
        )
        ledger = AttestationLedger()
        draft = ClinicalDraft(
            "d1", "c1", "p1", "soap_note", "hash", drafted_at=1.0,
        )
        ledger.register_draft(draft)
        blocked = False
        try:
            require_attestation(ledger, "d1")
        except AttestationError:
            blocked = True
        from hybridagent import vertical_templates as vt
        from hybridagent.pack import VerticalPack
        t = vt.get_template("medical_office") or {}
        pk = VerticalPack.from_manifest({**t, "name": "medical_office"})
        sp = pk.system_prompt.lower()
        persona = ("never write to the chart" in sp and "do not diagnose" in sp)
        return blocked and persona, f"blocked={blocked} persona={persona}"
    return run


def _telemedicine_case():
    def run() -> tuple[bool, str]:
        from .modules.telemedicine_gate import (
            PhysicianLicense,
            TeleVisit,
            check_telemedicine_license,
        )
        report = check_telemedicine_license(
            TeleVisit("tv", "dr-1", "p1", "PA"),
            [PhysicianLicense("dr-1", "NY", "MD-1", "2027-01-01")],
            now=1_780_000_000.0,
            require_consent_documented=False,
        )
        blocked = report.blocked and any(
            f.code == "not_licensed" for f in report.findings
        )
        non_imlc = any(f.code == "non_imlc_jurisdiction" for f in report.findings)
        return blocked and non_imlc, f"blocked={blocked} non_imlc={non_imlc}"
    return run


def _controlled_substance_case():
    def run() -> tuple[bool, str]:
        from .modules.controlled_substances import (
            PrescriberAuthority,
            RxDraft,
            check_controlled_substance_rx,
        )
        rx = RxDraft(
            "rx1", "p1", "NY", "dr-1", "oxycodone", "II",
            days_supply=7, mme_per_day=40.0, is_opioid=True, is_initial=True,
        )
        auth = PrescriberAuthority("dr-1", "NY", "AB1", "2027-01-01")
        report = check_controlled_substance_rx(rx, auth, None, now=1_780_000_000.0)
        flagged = any(f.code == "pmp_not_queried" for f in report.findings)
        return flagged, f"pmp_flag={flagged}"
    return run


def _minor_consent_case():
    def run() -> tuple[bool, str]:
        from .modules.minor_consent import (
            AccessRequest,
            MinorEncounter,
            check_minor_record_access,
        )
        report = check_minor_record_access(
            AccessRequest("r1", "enc-1", "parent_guardian", "parent-1"),
            MinorEncounter(
                "enc-1", "p1", "NY", "sti", self_consented=True, patient_age=16,
            ),
            None,
            now=1_780_000_000.0,
        )
        return report.blocked and report.confidential, report.summary()
    return run


def _portal_triage_case():
    def run() -> tuple[bool, str]:
        from .modules.portal_triage import (
            AUTONOMOUS_ADMIN_TEMPLATES,
            PortalMessage,
            PortalReplyDraft,
            triage_portal_message,
        )
        clinical = triage_portal_message(
            PortalMessage("m1", "p1", "Pain", "I have chest pain"),
        )
        tid = "office_hours"
        admin = triage_portal_message(
            PortalMessage("m2", "p1", "Hours", "office hours of operation?"),
            PortalReplyDraft(
                "d1", "m2", AUTONOMOUS_ADMIN_TEMPLATES[tid], tid,
            ),
        )
        ok = clinical.requires_physician and admin.autonomous_allowed
        return ok, f"clin_phys={clinical.requires_physician} admin_auto={admin.autonomous_allowed}"
    return run


def _manual_cases() -> list[EvalCase]:
    """Factory: return the 5 manual medical-office eval cases."""

    return [
        EvalCase("vertical.medical_office.never_write_chart", "vertical",
                 "Chart write without physician attestation is blocked.",
                 _never_write_chart_case()),
        EvalCase("vertical.medical_office.telemedicine_gate", "vertical",
                 "Tele-visit blocked when physician lacks patient-state license.",
                 _telemedicine_case()),
        EvalCase("vertical.medical_office.controlled_substance", "vertical",
                 "Controlled-substance Rx without PMP query is flagged high.",
                 _controlled_substance_case()),
        EvalCase("vertical.medical_office.minor_consent", "vertical",
                 "Parent access to minor self-consented STI record is denied.",
                 _minor_consent_case()),
        EvalCase("vertical.medical_office.portal_triage", "vertical",
                 "Clinical portal reply is not autonomous; allowlisted admin is.",
                 _portal_triage_case()),
    ]


def register() -> None:
    """Register the medical vertical with the Praxis base registry.

    Idempotent: safe to call multiple times (the registry deduplicates specs
    by name and factories by identity).
    """

    register_vertical_spec(_MEDICAL_OFFICE_SPEC)
    register_vertical_spec(_MEDICAL_CLINICAL_SPEC)
    register_vertical_eval_cases(_manual_cases)