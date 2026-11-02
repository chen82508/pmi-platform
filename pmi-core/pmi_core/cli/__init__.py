"""`pmi_core.cli` package — the `pmi-core` pipeline CLI.

Re-exported as `cli` so the `pmi-core = pmi_core.cli:cli` console-script entry
point and `python -m pmi_core.cli ...` both work.
"""

from __future__ import annotations

from pmi_core.cli.pipeline import cli

__all__ = ["cli"]
