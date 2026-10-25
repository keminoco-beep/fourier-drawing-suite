"""fourier-prompt — FDSL v2.1 plan validation / deterministic repair (core scaffold).

This package currently implements the *core* of the tool chain:

* :mod:`fourier_prompt.schema` — FDSL v2.1 validation;
* :mod:`fourier_prompt.repair` — deterministic, non-guessing repairs;
* :mod:`fourier_prompt.cli` — ``schema`` / ``selftest`` / ``version`` commands.

``plan2fbs`` / ``pack`` / ``draw`` / ``eval`` are deliberately not implemented
yet (batches 2–4 in ``docs/impl/KICKOFF_v1.md``); they fail loudly instead of
pretending to work.
"""

from .repair import repair_plan
from .schema import (
    FDSL_SCHEMA,
    FDSL_SCHEMA_COMPAT,
    FDSchemaError,
    load_plan,
    validate_plan,
    validate_plan_detailed,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "FDSL_SCHEMA",
    "FDSL_SCHEMA_COMPAT",
    "FDSchemaError",
    "load_plan",
    "repair_plan",
    "validate_plan",
    "validate_plan_detailed",
]
