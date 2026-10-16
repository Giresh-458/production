"""Backward-compatible entry point for the canonical RIF pipeline.

Historically this module contained a second pipeline implementation that used
shared output folders and continued after downstream collector failures. Keep
the old command/import stable, but route execution through run_pipeline.main(),
which owns the run-scoped output layout and stage quality gates.
"""
from __future__ import annotations


def run_pipeline() -> int:
    """Execute the canonical pipeline and return its process exit code."""
    from run_pipeline import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run_pipeline())
