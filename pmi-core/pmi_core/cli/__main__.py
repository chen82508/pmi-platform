"""Enables `python -m pmi_core.cli ...` (unchanged from the pre-package layout).

Deploy scripts (`deploy/README.md`) invoke the pipeline CLI this way inside
containers that don't have the `pmi-core` console script on PATH.
"""

from __future__ import annotations

from pmi_core.cli.pipeline import cli

if __name__ == "__main__":
    cli()
