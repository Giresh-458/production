"""Regression tests for the legacy full-pipeline entry point."""
def test_legacy_entrypoint_delegates_to_canonical_runner(monkeypatch):
    import run_full_pipeline
    import run_pipeline

    calls = []

    def fake_main():
        calls.append("main")
        return 4

    monkeypatch.setattr(run_pipeline, "main", fake_main)

    assert run_full_pipeline.run_pipeline() == 4
    assert calls == ["main"]
