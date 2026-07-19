# Medical Office Pack — Knowledge Base

This knowledge base is ingested into the `pack:medical_office` RAG namespace on pack activation. It grounds Praxis's clinical-documentation and compliance outputs across the 13 states the pack covers: FL, GA, SC, TN, VA, WV, MD, PA, OH, NJ, NY, CT, MA.

## 1. The 13-state medical quick reference

| State | Board | CME | IMLC | Retention (adult/minor) | Data-security | Telemedicine |
|---|---|---|---|---|---|---|
| FL | FL Board of Medicine | 40/biennium (CS, DV, HIV, trafficking) | Yes | 5yr / 7yr | breach-notification | **§456.47 registration** for OOS |
| GA | GA Composite Medical Board | 40/biennium (verify) | Yes | verify | breach-notification | no prior exam |
| SC | SC Board of Medical Examiners | 40/biennium Cat I (verify) | Yes | verify | breach-notification | SC license required |
| TN | TN Board of Medical Examiners | 40/2yr (verify) | Yes | 7yr | breach-notification | no prior exam |
| VA | VA Board of Medicine | 60/2yr (verify) | Yes | 6yr / 3yr | breach-notification | no prior exam |
| WV | WV Board of Medicine (MD) / Osteopathic (DO) | 50/biennium (CS CME) | Yes | verify | breach-notification | telehealth parity |
| MD | MD Board of Physicians | required (verify hours) | Yes | verify | breach-notification | parity law |
| PA | PA State Board of Medicine | **100/biennium** (20 Cat 1; opioid prereq) | **No** | 7yr / 21 | breach-notification | non-IMLC — full PA license |
| OH | State Medical Board of Ohio | verify | Yes | verify | breach-notification | no prior exam |
| NJ | NJ State Board of Medical Examiners | verify | Yes | verify | breach-notification | parity; **5-day** initial opioid |
| NY | NYSED Office of the Professions | 100/biennium (verify) | Yes | 6yr / 21+ | **SHIELD obligation** | no prior exam; 10-day access |
| CT | CT Medical Examining Board | **50/24-mo** (6-yr topic cycles) | Yes | 6yr | breach-notification | telehealth parity |
| MA | MA Board of Registration in Medicine | 100/biennium (1x Alzheimer's) | **No** | 7yr / 21+ | **WISP mandate (201 CMR 17.00)** | non-IMLC — full MA license |

## 2. Governance line (non-negotiable)

- **Never write to the chart autonomously.** Ambient documentation drafts; the physician signs (`clinical_attestation` / `ambient_documentation`).
- **Never diagnose, prescribe, or determine medical necessity.** Controlled-substance module flags; the physician + EHR prescribe.
- **Never send clinical advice to a patient without physician approval.** Portal triage holds clinical SEND.
- **PHI = minimum necessary.** `hipaa_governance` purpose-of-use + accounting-of-disclosures + breach workflow.
- **Telemedicine:** physician must be licensed in the patient's state (FL §456.47 registration exception; PA/MA non-IMLC stricter messaging).
- **Minor confidential services:** parent access restricted without minor authorization.

## 3. Controlled-substance guardrails

- All 13 states have a PMP; most mandate a query before initial opioid prescribing.
- Initial opioid day limits: NY/MA/PA/OH/VA = 7 days; NJ = 5 days; others follow CDC guidelines.
- CDC MME/day: ≥50 caution; ≥90 high-risk.
- Missing/expired DEA registration is critical (blocks).

## 4. Telemedicine cross-state gate

1. Current license in patient state → allow.
2. FL patient + current FL telehealth registration (§456.47) → allow.
3. Else → block. PA/MA: no IMLC expedite path. Other IMLC states: Compact expedites obtaining a license but does not authorize practice without one.

## 5. Minor-consent record access

Self-consented services typically include reproductive, STI, substance_use, behavioral_health. When `minor_parent_access_restricted` is True (all 13 currently), parent portal/AVS/record-release access to those encounters requires `MinorReleaseAuthorization`. Minor self-access and provider treatment access remain allowed.

## 6. Records retention + patient access

- Adult retention: 5–7 years from last visit (state-specific).
- Minor: often until age 21 or N years after last visit, whichever longer.
- Patient access deadlines: NY/MA = 10 days; FL/VA = 15; NJ = 7; MD = 21; others HIPAA 30-day floor when state encodes 0.
- Legal hold blocks disposal. Praxis never deletes autonomously (DESTRUCTIVE, dual approval).

## 7. Portal triage

- Clinical / mixed / unknown → draft for physician (SEND held).
- Administrative + exact allowlisted template body → may auto-send.
- Free-form administrative → hold for staff.

## 8. Ambient documentation workflow

1. Capture `VisitConsent` (status=captured).
2. Start `AmbientSession` → record → end → draft `ClinicalDraft`.
3. Physician `signed`/`amended` attestation unlocks chart write.
4. Audio retention follows state medical-record rules; purge is recommendation-only.
