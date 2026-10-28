from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text


def test_audio_only_ui_worker_uses_existing_pipeline_without_browser(runtime, monkeypatch):
    from video_action_agent import ui_processing

    def no_browser(*args, **kwargs):
        raise AssertionError("Audio-only setup must not create a browser adapter")

    monkeypatch.setattr(ui_processing, "LocalOpenCli", no_browser)
    monkeypatch.setattr(ui_processing, "LocalOpenCliSessionProbe", no_browser)
    provider = object()
    worker = ui_processing.build_ui_worker(
        runtime, "alice", ("audio_transcription",), provider_factory=lambda: provider,
    )
    assert worker.source_modes == ("audio_transcription",)
    assert worker.pipeline.adapter is None and worker.pipeline.session_probe is None
    assert worker.pipeline.audio_session_factory is ui_processing.AudioSession
    assert worker.pipeline.draft_provider is provider


def test_audio_ui_refuses_old_schema_before_provider_setup():
    from video_action_agent.ui_processing import UIProcessingError, build_ui_worker

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0010')"))

    def no_provider():
        raise AssertionError("Old schema must fail before provider setup")

    try:
        with pytest.raises(UIProcessingError, match="audio_schema_upgrade_required"):
            build_ui_worker(SimpleNamespace(engine=engine), "alice", ("audio_transcription",),
                            provider_factory=no_provider)
    finally:
        engine.dispose()
