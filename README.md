# SMF Praxis Medical vertical

Medical Office compliance pack for the [Praxis](https://github.com/smfworks/smf-praxis) autonomous agent platform.

## Status

Extracted from `smf-praxis` base on 2026-07-19 during the vertical-extraction cutover. Depends on the open-core base (`praxis-agent`) as a runtime dependency.

## What's inside

- **8 compliance modules** — `ambient_documentation`, `clinical_attestation`, `controlled_substances`, `hipaa_governance`, `minor_consent`, `portal_triage`, `records_retention`, `telemedicine_gate`
- **Medical Office pack** — `packs/medical_office/` (manifest + knowledge base), persona + authority policy for `verticals/medical/`
- **5 vertical eval cases** — never-write-chart, telemedicine gate, controlled substance, minor consent, portal triage
- **13-state coverage** — FL, GA, SC, TN, VA, WV, MD, PA, OH, NJ, NY, CT, MA
- **HIPAA federal floor** + state-specific telemedicine licensure, controlled-substance PMP, minor-consent, records-retention

## Installation

```bash
pip install praxis-agent          # open-core base (public, MIT)
pip install praxis-medical        # SMF Praxis medical compliance pack
```

Importing `hybridagent_praxis_medical` auto-registers the Medical Office vertical with the base's plugin registry.

## Compliance posture

**Enforced.** READ + DRAFT autonomous; SEND + DESTRUCTIVE held for human approval. "Never write to the chart" + "do not diagnose" guardrails. HIPAA minimum-necessary + accounting-of-disclosures + breach-response workflow.

## License

This pack is MIT-licensed. See `LICENSE`.

This pack is informational tooling and is not legal or medical-compliance advice. Users should verify requirements with qualified counsel.
