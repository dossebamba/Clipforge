from clipforge.db.models import C_POSTED, C_READY, V_DONE, Clip, Video
from clipforge.db.session import make_engine, make_session_factory, session_scope


def _video(clips):
    v = Video(platform="youtube", external_id="abc", url="u", status=V_DONE)
    v.clips = [Clip(file_path="x.mp4", start_s=0, end_s=30, status=s) for s in clips]
    return v


def test_video_published_only_when_no_ready_clip_left():
    assert not _video([C_READY, C_POSTED]).is_published
    assert _video([C_POSTED, C_POSTED]).is_published
    assert not _video([]).is_published


def test_roundtrip_in_sqlite(tmp_path):
    factory = make_session_factory(make_engine(tmp_path / "t.sqlite"))
    with session_scope(factory) as s:
        s.add(_video([C_READY]))
    with session_scope(factory) as s:
        v = s.query(Video).one()
        assert v.origin == "manuel"
        assert v.clips[0].duration_s == 30
