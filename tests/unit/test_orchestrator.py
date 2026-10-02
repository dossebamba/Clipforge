import pytest

from clipforge.config import Settings
from clipforge.db.models import (
    C_READY,
    V_DONE,
    V_FAILED,
    V_PENDING,
    V_PROCESSING,
    Clip,
    Video,
)
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.pipeline import orchestrator as orch


@pytest.fixture
def factory(tmp_path):
    return make_session_factory(make_engine(tmp_path / "t.sqlite"))


def _add(s, ext, status=V_PENDING, priority=0):
    v = Video(platform="youtube", external_id=ext, url="u", status=status, priority=priority)
    s.add(v)
    s.flush()
    return v.id


def test_next_pending_orders_by_priority_then_age(factory):
    with session_scope(factory) as s:
        _add(s, "a")
        b = _add(s, "b", priority=10)
        _add(s, "c")
        assert orch.next_pending(s, Settings()) == b


def test_queue_pauses_when_too_many_clips_waiting(factory):
    with session_scope(factory) as s:
        vid = _add(s, "a", status=V_DONE)
        _add(s, "b")
        s.add_all(
            Clip(video_id=vid, file_path="x", start_s=0, end_s=30, status=C_READY) for _ in range(3)
        )
        s.flush()
        assert orch.next_pending(s, Settings(max_pending_clips=3)) is None
        assert orch.next_pending(s, Settings(max_pending_clips=4)) is not None


def test_recover_interrupted(factory):
    with session_scope(factory) as s:
        _add(s, "a", status=V_PROCESSING)
    assert orch.recover_interrupted(factory) == 1
    with session_scope(factory) as s:
        assert s.query(Video).one().status == V_PENDING


def test_process_video_failure_is_recorded_and_cleaned(factory, tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    with session_scope(factory) as s:
        vid = _add(s, "a")

    def boom(*a, **k):
        raise RuntimeError("téléchargement impossible")

    monkeypatch.setattr(orch.download, "download", boom)
    assert orch.process_video(factory, vid, settings, router=None) == 0  # type: ignore[arg-type]
    with session_scope(factory) as s:
        v = s.get(Video, vid)
        assert v.status == V_FAILED and "impossible" in v.error
