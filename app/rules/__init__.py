"""Deterministic business-rule modules (eligibility, deadlines, quotas, prerequisites).

The Deterministic Verifier invokes pure functions from this package; no LLM or
agent code may perform official-rule calculations itself. Empty in Phase 1 --
rule modules are added starting Phase 2.
"""
