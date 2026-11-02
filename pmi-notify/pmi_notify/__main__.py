"""Entry point for `python -m pmi_notify` (see pmi_notify/cli.py)."""

from __future__ import annotations

import sys

import structlog

from pmi_notify.cli import main

# The adapters log through structlog, which prints to stdout by default. Send it
# to stderr so the CLI's stdout stays empty and its exit code is the result.
structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))

sys.exit(main())
