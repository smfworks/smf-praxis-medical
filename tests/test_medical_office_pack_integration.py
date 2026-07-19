"""Medical Office pack — 13-state integration test (Slice 10).

Proves the pack works across all 13 states: MEDICAL profiles load,
telemedicine IMLC/FL-registration behavior, minor-consent parent gate,
retention/access deadlines, controlled-substance limits, security tiers,
and pack persona/knowledge coverage.
"""
from __future__ import annotations

import pytest

from hybridagent import config as cfg
from hybridagent import pack
from hybridagent.clinical_attestation import (
    AttestationError,
    AttestationLedger,
    ClinicalAttestation,
    ClinicalDraft,
    require_attestation,
)
from hybridagent.controlled_substances import (
    PmpQueryResult,
    PrescriberAuthority,
    RxDraft,
    check_controlled_substance_rx,
)
from hybridagent.jurisdictions import get_medical_profile, registered_states
from hybridagent.minor_consent import (
    AccessRequest,
    MinorEncounter,
    check_minor_record_access,
)
from hybridagent.portal_triage import (
    AUTONOMOUS_ADMIN_TEMPLATES,
    PortalMessage,
    PortalReplyDraft,
    triage_portal_message,
)
from hybridagent.records_retention import (
    HIPAA_ACCESS_DAYS_FLOOR,
    MedicalRecordSet,
    PatientAccessRequest,
    assess_retention,
    open_patient_access_request,
)
from hybridagent.security_attestation import SecurityControls, attest
from hybridagent.telemedicine_gate import (
    FLTelehealthRegistration,
    PhysicianLicense,
    TeleVisit,
    check_telemedicine_license,
)


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


STATES = registered_states()
NOW = 1_780_000_000.0  # fixed epoch for deterministic tests


# ---------------------------------------------------------------------------
# Pack activation + profiles
# ---------------------------------------------------------------------------

def test_medical_office_pack_activates(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.activate("medical_office")
    assert p is not None
    assert p.name == "medical_office"
    assert cfg.get_active_pack_name() == "medical_office"
    pack.deactivate()


@pytest.mark.parametrize("state", STATES)
def test_every_state_has_medical_profile(state):
    p = get_medical_profile(state)
    assert p is not None
    assert p.state == state.upper()
    assert p.board_name
    assert isinstance(p.imlc_member, bool)


# ---------------------------------------------------------------------------
# Telemedicine across 13 states
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", STATES)
def test_telemedicine_in_state_license_allows(state):
    code = state.upper()
    report = check_telemedicine_license(
        TeleVisit("tv", "dr-1", "p1", code),
        [PhysicianLicense("dr-1", code, "MD-1", "2027-01-01")],
        now=NOW,
        require_consent_documented=False,
    )
    assert report.allowed


@pytest.mark.parametrize("state", [s for s in STATES if s.upper() != "NY"])
def test_telemedicine_blocks_without_local_license(state):
    code = state.upper()
    report = check_telemedicine_license(
        TeleVisit("tv", "dr-1", "p1", code),
        [PhysicianLicense("dr-1", "NY", "MD-1", "2027-01-01")],
        now=NOW,
        require_consent_documented=False,
    )
    assert report.blocked


def test_only_pa_ma_non_imlc():
    non = [
        s.upper() for s in STATES
        if (p := get_medical_profile(s)) and not p.imlc_member
    ]
    assert sorted(non) == ["MA", "PA"]


def test_fl_registration_exception():
    report = check_telemedicine_license(
        TeleVisit("tv", "dr-1", "p1", "FL"),
        [PhysicianLicense("dr-1", "NY", "MD-1", "2027-01-01")],
        fl_registration=FLTelehealthRegistration(
            "dr-1", "FL-TH-1", "2027-01-01",
        ),
        now=NOW,
        require_consent_documented=False,
    )
    assert report.allowed
    assert report.authority_path == "fl_telehealth_registration"


# ---------------------------------------------------------------------------
# Minor consent across 13 states
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", STATES)
def test_parent_denied_sti_every_state(state):
    code = state.upper()
    report = check_minor_record_access(
        AccessRequest("r1", "enc-1", "parent_guardian", "parent-1"),
        MinorEncounter(
            "enc-1", "p1", code, "sti", self_consented=True, patient_age=16,
        ),
        None,
        now=NOW,
    )
    assert report.blocked
    assert report.confidential


# ---------------------------------------------------------------------------
# Retention + access
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", STATES)
def test_recent_record_active_every_state(state):
    a = assess_retention(
        MedicalRecordSet(
            "rec-1", "p1", state.upper(), last_visit_at=NOW - 86400 * 30,
        ),
        now=NOW,
    )
    assert a.status == "active"


@pytest.mark.parametrize("state", STATES)
def test_patient_access_deadline_positive(state):
    wf = open_patient_access_request(
        PatientAccessRequest("a1", "p1", state.upper(), NOW),
    )
    assert wf.access_days >= 1
    assert wf.deadline_at > NOW


def test_hipaa_floor_applied_for_zero_day_states():
    zeros = [
        s for s in STATES
        if (p := get_medical_profile(s)) and p.patient_access_days == 0
    ]
    assert zeros, "expected at least one state with access_days=0"
    wf = open_patient_access_request(
        PatientAccessRequest("a2", "p1", zeros[0].upper(), NOW),
    )
    assert wf.access_days == HIPAA_ACCESS_DAYS_FLOOR


# ---------------------------------------------------------------------------
# Controlled substances
# ---------------------------------------------------------------------------

def test_ny_opioid_over_limit_flagged():
    rx = RxDraft(
        "rx1", "p1", "NY", "dr-1", "oxycodone", "II",
        days_supply=14, mme_per_day=40.0, is_opioid=True, is_initial=True,
    )
    auth = PrescriberAuthority("dr-1", "NY", "AB1", "2027-01-01")
    pmp = PmpQueryResult("q1", NOW, "p1", "NY", queried=True)
    report = check_controlled_substance_rx(rx, auth, pmp, now=NOW)
    assert any(f.code == "initial_limit_exceeded" for f in report.findings)


def test_nj_5_day_limit():
    rx = RxDraft(
        "rx2", "p1", "NJ", "dr-1", "oxycodone", "II",
        days_supply=6, mme_per_day=30.0, is_opioid=True, is_initial=True,
    )
    findings = check_controlled_substance_rx(
        rx,
        PrescriberAuthority("dr-1", "NJ", "AB1", "2027-01-01"),
        PmpQueryResult("q1", NOW, "p1", "NJ"),
        now=NOW,
    ).findings
    assert any(f.code == "initial_limit_exceeded" for f in findings)


# ---------------------------------------------------------------------------
# Security tiers (reuse law-firm security_attestation)
# ---------------------------------------------------------------------------

def test_ma_wisp_fails_without_controls():
    att = attest("MA", SecurityControls())
    assert not att.passed


def test_ny_shield_tier():
    att = attest("NY", SecurityControls())
    assert att.tier == "shield_obligation"


# ---------------------------------------------------------------------------
# Clinical attestation never-write-to-chart
# ---------------------------------------------------------------------------

def test_chart_write_blocked_without_attestation():
    ledger = AttestationLedger()
    draft = ClinicalDraft(
        "d1", "c1", "p1", "soap_note", "hash", drafted_at=NOW,
    )
    ledger.register_draft(draft)
    with pytest.raises(AttestationError):
        require_attestation(ledger, "d1")
    ledger.attest(ClinicalAttestation(
        "a1", "d1", "dr-1", "signed", NOW + 1,
    ))
    require_attestation(ledger, "d1")  # no raise


# ---------------------------------------------------------------------------
# Portal triage
# ---------------------------------------------------------------------------

def test_clinical_portal_not_autonomous():
    report = triage_portal_message(
        PortalMessage("m1", "p1", "Pain", "I have chest pain"),
    )
    assert report.requires_physician
    assert not report.autonomous_allowed


def test_admin_template_autonomous():
    tid = "office_hours"
    report = triage_portal_message(
        PortalMessage("m2", "p1", "Hours", "What are office hours of operation?"),
        PortalReplyDraft("d1", "m2", AUTONOMOUS_ADMIN_TEMPLATES[tid], tid),
    )
    assert report.autonomous_allowed


# ---------------------------------------------------------------------------
# Pack persona + knowledge coverage
# ---------------------------------------------------------------------------

def test_medical_office_persona_guardrails(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.load_pack("medical_office")
    assert p is not None
    sp = p.system_prompt.lower()
    assert "do not diagnose" in sp
    assert "never write to the chart" in sp or "never write to the chart autonomously" in sp
    assert "do not prescribe" in sp or "never prescribe" in sp
    assert "phi" in sp
    assert "imlc" in sp or "telemedicine" in sp
    assert "wisp" in sp
    assert "shield" in sp


def test_medical_office_knowledge_covers_13_states(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from hybridagent.pack import bundled_packs_dir
    kb = (bundled_packs_dir() / "medical_office" / "knowledge.md").read_text()
    for state in ("FL", "GA", "SC", "TN", "VA", "WV", "MD", "PA",
                  "OH", "NJ", "NY", "CT", "MA"):
        assert state in kb, f"{state} missing from knowledge base"


def test_medical_office_skills_present(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.load_pack("medical_office")
    assert p is not None
    names = {s["name"] if isinstance(s, dict) else s.name for s in p.skills}
    for required in (
        "ambient-documentation", "controlled-substance-guardrail",
        "telemedicine-license-check", "minor-consent-gate",
        "portal-triage", "cme-status",
    ):
        assert required in names, f"missing skill {required}"


def test_medical_office_risk_policy_holds_send(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.load_pack("medical_office")
    assert p is not None
    rp = p.risk_policy
    # dualApprovalRisks should include send + destructive
    dual = {x.lower() for x in rp.get("dualApprovalRisks", [])}
    assert "send" in dual
    assert "destructive" in dual
