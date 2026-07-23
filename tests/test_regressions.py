"""Regression coverage for release-blocking medical governance failures."""
from dataclasses import replace
from datetime import datetime

import pytest

from hybridagent_praxis_medical.modules.clinical_attestation import (
    AttestationError,
    AttestationLedger,
    ClinicalAttestation,
    ClinicalDraft,
)
from hybridagent_praxis_medical.modules.controlled_substances import (
    PmpQueryResult,
    PrescriberAuthority,
    RxDraft,
    check_controlled_substance_rx,
)
from hybridagent_praxis_medical.modules.hipaa_governance import (
    AccountingOfDisclosures,
    MinimumNecessaryError,
    MinimumNecessaryRequest,
    PhiDisclosure,
    PhiField,
    check_minimum_necessary,
)
from hybridagent_praxis_medical.modules.minor_consent import (
    AccessRequest,
    MinorEncounter,
    check_minor_record_access,
)
from hybridagent_praxis_medical.modules.telemedicine_gate import (
    FLTelehealthRegistration,
    PhysicianLicense,
    TeleVisit,
    check_telemedicine_license,
)

NOW = datetime(2026, 1, 1).timestamp()


def test_invalid_expiry_never_authorizes_telemedicine():
    visit = TeleVisit("v", "doc", "p", "NY")
    bad = PhysicianLicense("doc", "NY", "NY-1", "not-a-date")
    assert check_telemedicine_license(visit, [bad], now=NOW).blocked

    florida = replace(visit, patient_state="FL")
    registration = FLTelehealthRegistration("doc", "FL-1", "not-a-date")
    assert check_telemedicine_license(
        florida, [], fl_registration=registration, now=NOW
    ).blocked


def test_controlled_substance_evidence_is_bound_to_request():
    rx = RxDraft("r", "patient", "NY", "doc", "drug", "II", 3)
    authority = PrescriberAuthority("other-doc", "PA", "DEA", "not-a-date")
    pmp = PmpQueryResult("q", NOW, "other-patient", "PA")
    report = check_controlled_substance_rx(rx, authority, pmp, now=NOW)
    codes = {item.code for item in report.findings}
    assert report.blocked
    assert {"authority_physician_mismatch", "authority_state_mismatch", "dea_expiry_invalid"} <= codes
    assert "pmp_query_mismatch" in codes


def _draft(content_hash="hash-1"):
    return ClinicalDraft("d", "chart", "patient", "soap_note", content_hash)


def _signed(kind="signed"):
    return ClinicalAttestation("a", "d", "doc", kind, NOW)


def test_clinical_draft_is_immutable_and_attestation_is_terminal():
    ledger = AttestationLedger()
    ledger.register_draft(_draft())
    with pytest.raises(AttestationError, match="immutable"):
        ledger.register_draft(_draft("hash-2"))
    ledger.attest(_signed())
    with pytest.raises(AttestationError, match="terminal"):
        ledger.attest(replace(_signed(), attestation_id="reject", attestation_type="rejected"))
    assert ledger.can_write_chart("d")


def test_confidential_minor_self_access_requires_matching_identity():
    encounter = MinorEncounter("e", "minor-1", "NY", "hiv", True, 16)
    request = AccessRequest("r", "e", "minor_patient", "minor-2")
    assert check_minor_record_access(request, encounter, now=NOW).blocked


def test_hiv_self_consented_record_is_confidential_to_billing():
    encounter = MinorEncounter("e", "minor", "NY", "hiv", True, 16)
    request = AccessRequest("r", "e", "payer", "billing", purpose="treatment")
    report = check_minor_record_access(request, encounter, now=NOW)
    assert report.confidential and report.blocked


def test_minimum_necessary_rejects_role_purpose_spoofing_and_nonroutine_scope():
    billing = MinimumNecessaryRequest(
        "treatment", (PhiField("hiv_status", "highly_sensitive"),), "billing"
    )
    assert any("role_purpose_mismatch" in item for item in check_minimum_necessary(billing))

    law_enforcement = MinimumNecessaryRequest(
        "law_enforcement", (PhiField("clinical_notes"),), "law_enforcement"
    )
    findings = check_minimum_necessary(law_enforcement)
    assert any("non_routine_authorization_required" in item for item in findings)

    sensitive = MinimumNecessaryRequest(
        "law_enforcement", (PhiField("hiv_status", "highly_sensitive"),), "law_enforcement"
    )
    with pytest.raises(MinimumNecessaryError):
        check_minimum_necessary(sensitive)


def test_accounting_rejects_invalid_disclosure_evidence():
    ledger = AccountingOfDisclosures()
    invalid = PhiDisclosure("", "patient", ("diagnosis",), "", "invalid", 0, "")
    with pytest.raises(ValueError):
        ledger.record(invalid)
