import pytest

from clipforge.config import Settings
from clipforge.db.models import V_PENDING, V_SEEN, V_SKIPPED, Video
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.sources import service
from clipforge.sources.base import RemoteVideo
from clipforge.sources.twitch import normalize_login, parse_duration
from clipforge.sources.youtube import entry_to_video, normalize_channel_url


def test_twitch_duration_and_login():
    assert parse_duration("3h2m1s") == 10921
    assert parse_duration("45m10s") == 2710
    assert parse_duration("12s") == 12
    assert parse_duration("") == 0
    assert normalize_login("https://www.twitch.tv/Ninja/videos") == "ninja"
    assert normalize_login("@Gotaga") == "gotaga"


def test_youtube_url_normalisation():
    assert normalize_channel_url("@abc") == "https://www.youtube.com/@abc/videos"
    assert normalize_channel_url("https://www.youtube.com/@abc/") == (
        "https://www.youtube.com/@abc/videos"
    )
    assert normalize_channel_url("https://www.youtube.com/@abc/streams") == (
        "https://www.youtube.com/@abc/videos"
    )


def test_entry_to_video():
    v = entry_to_video({"id": "x1", "title": "T", "duration": 61.0, "live_status": "is_live"}, "C")
    assert v and v.external_id == "x1" and v.duration_s == 61 and v.is_live
    assert entry_to_video({}) is None


@pytest.fixture
def factory(tmp_path):
    return make_session_factory(make_engine(tmp_path / "t.sqlite"))


def _remote(n, **kw):
    return (
        [
            RemoteVideo(
                "youtube", f"id{i}", f"https://y/{i}", f"Titre {i}", "Chaîne", duration_s=600
            )
            for i in range(n)
        ]
        if not kw
        else [RemoteVideo("youtube", "idx", "u", **kw)]
    )


def test_initial_import_takes_two_latest_then_only_new(factory, monkeypatch):
    settings = Settings(initial_import_count=2)
    state = {"vids": _remote(5)}  # id0 = la plus récente
    monkeypatch.setattr(service, "_fetch", lambda s, st: ("Chaîne", state["vids"]))

    with session_scope(factory) as s:
        src = service.add_source(s, "youtube", "@abc", settings)
        s.flush()
        by_id = {v.external_id: v.status for v in s.query(Video)}
        assert by_id["id0"] == V_PENDING and by_id["id1"] == V_PENDING
        assert all(by_id[f"id{i}"] == V_SEEN for i in (2, 3, 4))

        # relevé suivant : une vidéo toute neuve apparaît en tête
        new = RemoteVideo("youtube", "idNEW", "u", "Nouvelle", "Chaîne", duration_s=900)
        state["vids"] = [new, *state["vids"]]
        assert service.poll_source(s, src, settings) == 1
        assert s.query(Video).filter_by(external_id="idNEW").one().status == V_PENDING
        # un relevé de plus ne ramène rien
        assert service.poll_source(s, src, settings) == 0


def test_skip_rules(factory, monkeypatch):
    settings = Settings(initial_import_count=10, max_source_duration_s=1000)
    vids = [
        RemoteVideo("youtube", "live", "u", "x", duration_s=100, is_live=True),
        RemoteVideo("youtube", "long", "u", "x", duration_s=5000),
        RemoteVideo("youtube", "kw", "u", "Rediffusion du soir", duration_s=100),
        RemoteVideo("youtube", "ok", "u", "Super vidéo", duration_s=100),
    ]
    monkeypatch.setattr(service, "_fetch", lambda s, st: ("C", vids))
    with session_scope(factory) as s:
        service.add_source(s, "youtube", "@abc", settings, ignore_keywords="rediffusion")
        s.flush()
        st = {v.external_id: v.status for v in s.query(Video)}
    assert st == {"live": V_SKIPPED, "long": V_SKIPPED, "kw": V_SKIPPED, "ok": V_PENDING}


def test_manual_url_has_priority_and_no_duplicate(factory, monkeypatch):
    rv = RemoteVideo("youtube", "m1", "https://y/m1", "Manuel", "Chaîne", duration_s=300)
    monkeypatch.setattr(service.manual, "resolve_url", lambda url: rv)
    with session_scope(factory) as s:
        a = service.add_manual_url(s, "https://y/m1")
        b = service.add_manual_url(s, "https://y/m1")
        assert a.id == b.id and a.priority == service.MANUAL_PRIORITY
        assert a.source_id is None


def test_poll_error_is_recorded_not_raised(factory, monkeypatch):
    def boom(s, st):
        raise RuntimeError("réseau")

    monkeypatch.setattr(service, "_fetch", boom)
    with session_scope(factory) as s:
        src = service.add_source(s, "youtube", "@abc", Settings())
        assert "réseau" in src.last_error
