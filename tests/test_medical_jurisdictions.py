"""Per-jurisdiction medical profile registry tests (Gap M1 — 13-state medical build).

The medical registry is the foundation for the Medical Office pack: every
downstream feature (M2 HIPAA, M3 clinical-drafting, M5 controlled-substance
guardrails, M6 telemedicine gate, M7 CME, M4 minor-consent) loads state rules
from these profiles instead of hardcoding them.
"""
from __future__ import annotations

import pytest
from hybridagent.jurisdictions import (
    MedicalProfile,
    get_medical_profile,
    registered_states,
)

STATES = registered_states()


# ---------------------------------------------------------------------------
# 1. Every state has a MEDICAL profile

@pytest.mark.parametrize("state", STATES)
def test_every_state_has_a_medical_profile(state):
    p = get_medical_profile(state)
    assert p is not None, f"{state} missing MEDICAL profile"
    assert isinstance(p, MedicalProfile)
    assert p.state == state.upper()


def test_registry_covers_13_states():
    assert len(STATES) == 13
    assert set(STATES) == {
        "fl", "ga", "sc", "tn", "va", "wv", "md", "pa", "oh", "nj",
        "ny", "ct", "ma",
    }


def test_unknown_state_returns_none():
    assert get_medical_profile("CA") is None
    assert get_medical_profile("tx") is None
    assert get_medical_profile("") is None


# ---------------------------------------------------------------------------
# 2. The key divergences (the things that make this a 13-state pack, not national)

def test_ma_is_the_wisp_mandate_ceiling():
    """MA 201 CMR 17.00 is the strictest data-security standard. The medical
    pack's security attestation must use the wisp_mandate tier for MA."""
    ma = get_medical_profile("MA")
    assert ma.data_security_tier == "wisp_mandate"


def test_ny_is_the_shield_obligation():
    """NY SHIELD Act — affirmative security obligation."""
    ny = get_medical_profile("NY")
    assert ny.data_security_tier == "shield_obligation"


def test_other_11_states_are_breach_notification_only():
    """The other 11 states have breach-notification laws but no proactive
    security standard (the HIPAA Security Rule is the federal floor)."""
    for st in STATES:
        if st in ("ma", "ny"):
            continue
        p = get_medical_profile(st)
        assert p.data_security_tier == "breach_notification_only", (
            f"{st} should be breach_notification_only, got {p.data_security_tier}")


def test_pa_and_ma_are_not_imlc_members():
    """PA and MA are the two non-IMLC states — the telemedicine cross-state
    gate is stricter for these (no Compact expedited licensure)."""
    pa = get_medical_profile("PA")
    ma = get_medical_profile("MA")
    assert pa.imlc_member is False
    assert ma.imlc_member is False


def test_other_11_states_are_imlc_members():
    for st in STATES:
        if st in ("pa", "ma"):
            continue
        p = get_medical_profile(st)
        assert p.imlc_member is True, f"{st} should be an IMLC member"


def test_ct_is_the_only_annual_license_cycle():
    """CT renews annually (in birth month); the other 12 are biennial. The
    credentials tracking must support per-state renewal cycles."""
    ct = get_medical_profile("CT")
    assert ct.license_cycle_years == 1
    for st in STATES:
        if st == "ct":
            continue
        p = get_medical_profile(st)
        assert p.license_cycle_years == 2, f"{st} should be biennial"


def test_ma_breach_notification_is_30_days():
    """MA has the most specific breach-notification timeline (30 days)."""
    ma = get_medical_profile("MA")
    assert ma.breach_notification_days == 30


# ---------------------------------------------------------------------------
# 3. CME — the most complex per-state variation

def test_cme_required_in_all_13():
    """All 13 states require CME (no MA-style exception like the law-firm CLE)."""
    for st in STATES:
        p = get_medical_profile(st)
        assert p.cme_required is True, f"{st} should require CME"


@pytest.mark.parametrize("state,hours", [
    ("fl", 40), ("sc", 40), ("tn", 40), ("ga", 40),
    ("wv", 50), ("md", 50), ("ct", 50),
    ("va", 60),
    ("pa", 100), ("oh", 100), ("nj", 100), ("ny", 100), ("ma", 100),
])
def test_cme_hours_per_state(state, hours):
    p = get_medical_profile(state)
    assert p.cme_hours == hours, f"{state} CME hours: expected {hours}, got {p.cme_hours}"


def test_ct_has_6_year_topic_cycles():
    """CT has the most detailed CME mandatory-topic cycles — 6-year rotating
    topics incl. cultural competency with systemic racism + transgender care."""
    ct = get_medical_profile("CT")
    assert ct.cme_topic_cycle_years == 6
    assert "cultural_competency" in ct.cme_mandatory_topics
    assert "behavioral_health" in ct.cme_mandatory_topics
    assert len(ct.cme_mandatory_topics) == 6


def test_fl_has_mandatory_topics():
    """FL requires CS, DV, HIV/AIDS, human trafficking CME."""
    fl = get_medical_profile("FL")
    assert "controlled_substance" in fl.cme_mandatory_topics
    assert "hiv_aids" in fl.cme_mandatory_topics
    assert "human_trafficking" in fl.cme_mandatory_topics


def test_pa_requires_opioid_education():
    """PA requires opioid education as a licensure prerequisite."""
    pa = get_medical_profile("PA")
    assert "opioid_education" in pa.cme_mandatory_topics


# ---------------------------------------------------------------------------
# 4. Controlled substances — PMP + initial opioid limits

def test_pmp_query_required_in_all_13():
    """All 13 states have a PMP and most mandate a query before initial opioid
    prescribing."""
    for st in STATES:
        p = get_medical_profile(st)
        assert p.pmp_query_required is True, f"{st} should require PMP query"


@pytest.mark.parametrize("state,limit", [
    ("ny", 7), ("ma", 7), ("pa", 7), ("oh", 7), ("va", 7),
    ("nj", 5),
])
def test_initial_opioid_rx_limit_per_state(state, limit):
    p = get_medical_profile(state)
    assert p.initial_opioid_rx_limit_days == limit, (
        f"{state} initial opioid limit: expected {limit}, got {p.initial_opioid_rx_limit_days}")


def test_mat_buprenorphine_permitted_in_all_13():
    """Federal X-waiver eliminated Jan 2023; all 13 permit DEA-registered
    prescribers for MAT/buprenorphine."""
    for st in STATES:
        p = get_medical_profile(st)
        assert p.mat_buprenorphine_permitted is True


# ---------------------------------------------------------------------------
# 5. Medical records retention — the minor-retention divergence

@pytest.mark.parametrize("state,adult", [
    ("fl", 5), ("sc", 7), ("tn", 7), ("wv", 7), ("md", 7),
    ("pa", 7), ("oh", 7), ("nj", 7), ("ny", 6), ("va", 6), ("ma", 7),
])
def test_record_retention_adult_per_state(state, adult):
    p = get_medical_profile(state)
    assert p.record_retention_adult_years == adult, (
        f"{state} adult retention: expected {adult}, got {p.record_retention_adult_years}")


def test_ny_patient_access_is_10_business_days():
    """NY PHL §18 — records produced within 10 business days. The fastest
    access timeline in the 13."""
    ny = get_medical_profile("NY")
    assert ny.patient_access_days == 10


# ---------------------------------------------------------------------------
# 6. Telemedicine + cross-state practice

def test_no_state_requires_prior_in_person_exam():
    """None of the 13 states require a prior in-person exam for telemedicine
    (the standard of care applies, but no prior-relationship mandate)."""
    for st in STATES:
        p = get_medical_profile(st)
        assert p.telemedicine_prior_in_person is False, (
            f"{st} should not require a prior in-person exam")


def test_fl_has_registration_requirement():
    """FL §456.47 — out-of-state providers must register for telehealth."""
    fl = get_medical_profile("FL")
    assert fl.telemedicine_requirement == "registration"


def test_cross_state_practice_not_allowed_by_default():
    """No state allows out-of-state physicians to treat in-state patients
    without a license (the telemedicine cross-state gate blocks this)."""
    for st in STATES:
        p = get_medical_profile(st)
        assert p.cross_state_practice_allowed is False, (
            f"{st} should not allow cross-state practice by default")


# ---------------------------------------------------------------------------
# 7. Minor-consent record handling

@pytest.mark.parametrize("state", STATES)
def test_every_state_has_minor_consent_services(state):
    """Every state allows minors to self-consent to at least some services
    (reproductive, STI, substance use, behavioral health)."""
    p = get_medical_profile(state)
    assert len(p.minor_consent_services) > 0
    assert p.minor_parent_access_restricted is True


# ---------------------------------------------------------------------------
# 8. Confidence tracking — the honest-sourcing protocol

def test_confidence_field_present_on_all_profiles():
    """Every profile carries a confidence field so downstream features can
    flag unverified data (8/13 primary-source verified; 5 established-knowledge)."""
    for st in STATES:
        p = get_medical_profile(st)
        assert p.confidence in ("primary_source", "established_knowledge", "mixed")


def test_established_knowledge_states_are_flagged():
    """GA, TN, MD, NJ, NY were not primary-source verified (Cloudflare/JS/
    site-reorganized). Their profiles carry confidence=established_knowledge."""
    for st in ("ga", "tn", "md", "nj", "ny"):
        p = get_medical_profile(st)
        assert p.confidence == "established_knowledge", (
            f"{st} should be established_knowledge, got {p.confidence}")


def test_primary_source_states_are_flagged():
    """FL, SC, VA, WV, PA, OH, CT, MA were primary-source verified."""
    for st in ("fl", "sc", "va", "wv", "pa", "oh", "ct", "ma"):
        p = get_medical_profile(st)
        assert p.confidence == "primary_source", (
            f"{st} should be primary_source, got {p.confidence}")