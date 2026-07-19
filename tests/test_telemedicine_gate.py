"""Telemedicine cross-state-practice gate tests (Gap M6).

Verifies: in-state license allow, FL §456.47 registration exception,
PA/MA non-IMLC block messaging, IMLC-member block-with-expedite-info,
expired/missing credentials, unknown jurisdiction, render/assert helpers.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from hybridagent.jurisdictions import get_medical_profile, registered_states
from hybridagent.telemedicine_gate import (
    FLTelehealthRegistration,
    PhysicianLicense,
    TelemedicineGateError,
    TeleVisit,
    assert_telemedicine_allowed,
    check_telemedicine_license,
    render_gate_report,
)

NOW = datetime(2026, 6, 1).timestamp()  # 2026-06-01


def _lic(
    state: str = "NY",
    physician_id: str = "dr-1",
    number: str = "MD-123",
    expires: str = "2027-01-01",
    active: bool = True,
) -> PhysicianLicense:
    return PhysicianLicense(
        physician_id=physician_id,
        state=state,
        license_number=number,
        expires=expires,
        active=active,
    )


def _visit(
    patient_state: str = "NY",
    physician_id: str = "dr-1",
    visit_id: str = "tv-1",
) -> TeleVisit:
    return TeleVisit(
        visit_id=visit_id,
        physician_id=physician_id,
        patient_id="p1",
        patient_state=patient_state,
        visit_at=NOW,
    )


def _fl_reg(
    physician_id: str = "dr-1",
    number: str = "FL-TH-999",
    expires: str = "2027-01-01",
    active: bool = True,
) -> FLTelehealthRegistration:
    return FLTelehealthRegistration(
        physician_id=physician_id,
        registration_number=number,
        expires=expires,
        active=active,
    )


# ---------------------------------------------------------------------------
# 1. In-state license → allow
# ---------------------------------------------------------------------------

def test_in_state_license_allows():
    """Physician licensed in the patient's state → allowed via in_state_license."""
    report = check_telemedicine_license(
        _visit("NY"), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    assert report.allowed
    assert not report.blocked
    assert report.authority_path == "in_state_license"
    assert any(f.code == "in_state_license" for f in report.findings)


def test_multi_state_license_matches_patient():
    """Physician licensed in GA + SC; patient in SC → allow."""
    licenses = [_lic("GA", number="GA-1"), _lic("SC", number="SC-1")]
    report = check_telemedicine_license(
        _visit("SC"), licenses, now=NOW, require_consent_documented=False,
    )
    assert report.allowed
    assert report.authority_path == "in_state_license"


@pytest.mark.parametrize("state", ["PA", "MA"])
def test_non_imlc_in_state_license_still_allows(state):
    """Even in non-IMLC states, a full in-state license authorizes the visit."""
    report = check_telemedicine_license(
        _visit(state), [_lic(state)], now=NOW, require_consent_documented=False,
    )
    assert report.allowed
    assert report.authority_path == "in_state_license"
    # no non_imlc block when licensed
    assert not any(f.code == "non_imlc_jurisdiction" for f in report.findings)


# ---------------------------------------------------------------------------
# 2. Not licensed → block
# ---------------------------------------------------------------------------

def test_not_licensed_blocks():
    """Physician licensed only in NY; patient in GA → blocked."""
    report = check_telemedicine_license(
        _visit("GA"), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    assert report.blocked
    assert report.authority_path == "none"
    assert any(f.code == "not_licensed" and f.severity == "critical"
               for f in report.findings)


def test_no_licenses_at_all_blocks():
    report = check_telemedicine_license(
        _visit("OH"), [], now=NOW, require_consent_documented=False,
    )
    assert report.blocked
    assert any(f.code == "not_licensed" for f in report.findings)


def test_expired_license_does_not_authorize():
    """An expired license is treated as not licensed."""
    report = check_telemedicine_license(
        _visit("NY"),
        [_lic("NY", expires="2020-01-01")],
        now=NOW,
        require_consent_documented=False,
    )
    assert report.blocked
    assert any(f.code == "not_licensed" for f in report.findings)


def test_inactive_license_does_not_authorize():
    report = check_telemedicine_license(
        _visit("NY"),
        [_lic("NY", active=False)],
        now=NOW,
        require_consent_documented=False,
    )
    assert report.blocked


def test_other_physician_license_ignored():
    """A license for a different physician does not authorize this visit."""
    report = check_telemedicine_license(
        _visit("NY", physician_id="dr-1"),
        [_lic("NY", physician_id="dr-other")],
        now=NOW,
        require_consent_documented=False,
    )
    assert report.blocked


# ---------------------------------------------------------------------------
# 3. PA + MA non-IMLC messaging
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["PA", "MA"])
def test_non_imlc_block_messaging(state):
    """Patients in PA/MA without a local license get the non-IMLC finding."""
    # confirm registry agrees
    prof = get_medical_profile(state)
    assert prof is not None and prof.imlc_member is False

    report = check_telemedicine_license(
        _visit(state), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    assert report.blocked
    assert any(f.code == "not_licensed" for f in report.findings)
    assert any(
        f.code == "non_imlc_jurisdiction" and f.severity == "high"
        for f in report.findings
    )
    # IMLC expedite message must NOT appear for non-members
    assert not any(f.code == "imlc_expedite_available" for f in report.findings)


def test_imlc_member_block_includes_expedite_info():
    """For IMLC states, a block still notes Compact expedites (but does not authorize)."""
    prof = get_medical_profile("GA")
    assert prof is not None and prof.imlc_member is True

    report = check_telemedicine_license(
        _visit("GA"), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    assert report.blocked
    assert any(f.code == "imlc_expedite_available" and f.severity == "info"
               for f in report.findings)
    assert not any(f.code == "non_imlc_jurisdiction" for f in report.findings)


# ---------------------------------------------------------------------------
# 4. FL §456.47 telehealth registration exception
# ---------------------------------------------------------------------------

def test_fl_registration_allows_without_fl_license():
    """Out-of-state physician with current FL telehealth registration → allow."""
    report = check_telemedicine_license(
        _visit("FL"),
        [_lic("NY")],  # licensed in NY only
        fl_registration=_fl_reg(),
        now=NOW,
        require_consent_documented=False,
    )
    assert report.allowed
    assert report.authority_path == "fl_telehealth_registration"
    assert any(f.code == "fl_registration_ok" for f in report.findings)
    assert not any(f.code == "not_licensed" for f in report.findings)


def test_fl_full_license_preferred_over_registration():
    """A full FL medical license authorizes via in_state_license (not registration)."""
    report = check_telemedicine_license(
        _visit("FL"),
        [_lic("FL")],
        fl_registration=_fl_reg(),
        now=NOW,
        require_consent_documented=False,
    )
    assert report.allowed
    assert report.authority_path == "in_state_license"


def test_fl_expired_registration_blocks():
    report = check_telemedicine_license(
        _visit("FL"),
        [_lic("NY")],
        fl_registration=_fl_reg(expires="2020-01-01"),
        now=NOW,
        require_consent_documented=False,
    )
    assert report.blocked
    assert any(f.code == "fl_registration_expired" for f in report.findings)
    assert any(f.code == "not_licensed" for f in report.findings)


def test_fl_registration_mismatch_blocks():
    report = check_telemedicine_license(
        _visit("FL", physician_id="dr-1"),
        [_lic("NY")],
        fl_registration=_fl_reg(physician_id="dr-other"),
        now=NOW,
        require_consent_documented=False,
    )
    assert report.blocked
    assert any(f.code == "fl_registration_mismatch" for f in report.findings)


def test_fl_no_registration_suggests_path():
    """FL patient, no FL license, no registration → block + registration-available hint."""
    report = check_telemedicine_license(
        _visit("FL"),
        [_lic("NY")],
        fl_registration=None,
        now=NOW,
        require_consent_documented=False,
    )
    assert report.blocked
    assert any(f.code == "fl_registration_available" and f.severity == "medium"
               for f in report.findings)


def test_fl_registration_does_not_authorize_other_states():
    """A FL telehealth registration is FL-only — does not authorize a GA visit."""
    report = check_telemedicine_license(
        _visit("GA"),
        [_lic("NY")],
        fl_registration=_fl_reg(),
        now=NOW,
        require_consent_documented=False,
    )
    assert report.blocked
    assert report.authority_path == "none"


# ---------------------------------------------------------------------------
# 5. Registry integration across all 13 states
# ---------------------------------------------------------------------------

def test_all_13_states_have_telemedicine_fields():
    """Every registered state has IMLC + telemedicine fields the gate consumes."""
    for st in registered_states():
        p = get_medical_profile(st)
        assert p is not None, f"{st} missing MEDICAL profile"
        assert isinstance(p.imlc_member, bool)
        assert p.telemedicine_requirement in (
            "no_prior_exam", "prior_exam_required", "registration",
        )
        assert isinstance(p.telemedicine_prior_in_person, bool)
        assert isinstance(p.telemedicine_consent_documented, bool)


def test_only_pa_and_ma_are_non_imlc():
    """Across the 13, only PA and MA are non-IMLC members."""
    non = [
        st.upper() for st in registered_states()
        if (p := get_medical_profile(st)) is not None and not p.imlc_member
    ]
    assert sorted(non) == ["MA", "PA"]


def test_only_fl_uses_registration_requirement():
    """FL is the only state with telemedicine_requirement='registration'."""
    reg_states = [
        st.upper() for st in registered_states()
        if (p := get_medical_profile(st)) is not None
        and p.telemedicine_requirement == "registration"
    ]
    assert reg_states == ["FL"]


def test_gate_evaluates_every_state_with_local_license():
    """A physician licensed in each of the 13 can tele-visit a patient there."""
    for st in registered_states():
        code = st.upper()
        report = check_telemedicine_license(
            _visit(code),
            [_lic(code)],
            now=NOW,
            require_consent_documented=False,
        )
        assert report.allowed, f"{code} in-state license should authorize"
        assert report.authority_path == "in_state_license"


def test_gate_blocks_every_state_without_license():
    """A NY-only physician is blocked for every non-NY patient state."""
    for st in registered_states():
        code = st.upper()
        if code == "NY":
            continue
        report = check_telemedicine_license(
            _visit(code),
            [_lic("NY")],
            now=NOW,
            require_consent_documented=False,
        )
        assert report.blocked, f"{code} without local license should block"


# ---------------------------------------------------------------------------
# 6. Consent documentation + unknown jurisdiction + helpers
# ---------------------------------------------------------------------------

def test_consent_documented_info_finding():
    """When the state requires documented telehealth consent, emit an info finding."""
    # All 13 currently have telemedicine_consent_documented=True
    report = check_telemedicine_license(
        _visit("NY"), [_lic("NY")], now=NOW, require_consent_documented=True,
    )
    assert report.allowed
    assert any(f.code == "consent_must_be_documented" and f.severity == "info"
               for f in report.findings)


def test_consent_check_can_be_suppressed():
    report = check_telemedicine_license(
        _visit("NY"), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    assert not any(f.code == "consent_must_be_documented" for f in report.findings)


def test_unknown_jurisdiction_blocks():
    report = check_telemedicine_license(
        _visit("XX"), [_lic("XX")], now=NOW, require_consent_documented=False,
    )
    assert report.blocked
    assert any(f.code == "unknown_jurisdiction" and f.severity == "critical"
               for f in report.findings)


def test_assert_telemedicine_allowed_raises_when_blocked():
    report = check_telemedicine_license(
        _visit("GA"), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    with pytest.raises(TelemedicineGateError):
        assert_telemedicine_allowed(report)


def test_assert_telemedicine_allowed_passthrough_when_allowed():
    report = check_telemedicine_license(
        _visit("NY"), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    out = assert_telemedicine_allowed(report)
    assert out is report


def test_render_gate_report_allowed():
    report = check_telemedicine_license(
        _visit("NY"), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    text = render_gate_report(report)
    assert "ALLOWED" in text
    assert "tv-1" in text
    assert "in_state_license" in text


def test_render_gate_report_blocked():
    report = check_telemedicine_license(
        _visit("PA"), [_lic("NY")], now=NOW, require_consent_documented=False,
    )
    text = render_gate_report(report)
    assert "BLOCKED" in text
    assert "not_licensed" in text
    assert "non_imlc" in text.lower() or "NOT an IMLC" in text


def test_case_insensitive_patient_state():
    """patient_state is normalized to upper-case."""
    report = check_telemedicine_license(
        TeleVisit(
            visit_id="tv-2", physician_id="dr-1", patient_id="p1",
            patient_state="ny",
        ),
        [_lic("NY")],
        now=NOW,
        require_consent_documented=False,
    )
    assert report.allowed
