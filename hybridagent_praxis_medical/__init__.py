"""SMF Praxis Medical vertical — registration module.

This package is the private paid Medical Office vertical build for Praxis.
It depends on the open-core ``smf-praxis`` base and registers the medical
vertical's spec and eval cases with the base's
:mod:`hybridagent.verticals.registry` on import.

Installation::

    pip install smf-praxis            # open-core base (public, MIT)
    pip install praxis-medical        # this vertical (private, commercial)

Activating the vertical lights up:

  * the ``medical_office`` vertical pack (persona + knowledge + HIPAA +
    controlled-substance + telemedicine + minor-consent + records-retention
    + portal-triage + ambient-documentation modules),
  * the ``vertical.medical_office.*`` eval cases (never-write-chart,
    telemedicine gate, controlled substance, minor consent, portal triage),
  * the ``vertical.medical.*`` clinical persona eval cases.

The vertical-specific modules are imported lazily by the eval cases and
daemon routes that need them.

Compliance mode: ``enforced``. READ + DRAFT autonomous; SEND + DESTRUCTIVE
held for human approval. The Medical Office persona carries
"never write to the chart" + "do not diagnose" guardrails across all
13 states, with HIPAA as the federal floor.
"""

from __future__ import annotations

from .registration import register

__version__ = "0.1.1"

__all__ = ["register", "__version__"]

# Auto-register on import so ``import hybridagent_praxis_medical`` lights up
# the vertical for the whole process lifetime.
register()