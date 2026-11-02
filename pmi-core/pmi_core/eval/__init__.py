"""Classification evaluation harness — the measuring stick for membership changes.

Why this exists
---------------
Membership today is decided by a factor LLM, and the same factor bound to a
different model produces wildly different judgments on the same market
population (measured on the live corpus: ``armed_conflict`` fires on 17.3 % of
markets under ``gpt-4o-mini-2024-07-18`` and 54.6 % under ``ollama/gemma3:4b``).
Both answers are cached as ground truth and both feed live scores. Without a
pinned reference set there is no way to say which is right, so every prompt
tweak / model promote / selector broadening is a *displacement* rather than an
improvement — see the v4→v13 comment archaeology in
``index_defs/war-index-2026.yaml``.

This package supplies the missing reference:

``schema``      the gold-set file format (JSONL + strata sidecar), versioned in
                git next to the prompts whose sha256 it is bound to.
``sampling``    stratified sampling — a uniform sample of 2.2 M markets contains
                approximately zero positives, and (crucially) never surfaces the
                false NEGATIVES that a selector-scoped sample cannot see.
``labeling``    the model-only labelling protocol: independent labellers ×
                self-consistency × a deterministic structural vote, with
                disagreement recorded rather than averaged away.
``metrics``     population-corrected precision / recall, plus a liquidity-weighted
                variant — an error on an untraded strike and an error on a deep
                book are not the same error.

A gold set answers "does this match the definition we wrote down". It does not
answer "is the definition good" — that stays with index-level regression
(``engine.backfill_scores`` replay) and, ultimately, outcome calibration
(``engine.brier``).
"""
