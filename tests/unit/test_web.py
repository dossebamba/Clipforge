import pytest
from fastapi.testclient import TestClient

from clipforge.config import Settings
from clipforge.db.models import C_READY, V_DONE, V_FAILED, Clip, Video
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.web.app import create_app


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    factory = make_session_factory(make_engine(tmp_path / "t.sqlite"))
    with session_scope(factory) as s:
        v1 = Video(
            platform="youtube", external_id="a", url="https://y/a", title="Vidéo A",
            channel_name="Chaîne A", status=V_DONE,
        )  # fmt: skip
        v1.clips = [
            Clip(file_path="1/01.mp4", start_s=0, end_s=30, score=90, title="Clip 1",
                 description="Légende 1 #tag"),
            Clip(file_path="1/02.mp4", start_s=40, end_s=80, score=80, title="Clip 2"),
        ]  # fmt: skip
        v2 = Video(platform="twitch", external_id="b", url="https://t/b", title="VOD B",
                   status=V_FAILED, error="boom")  # fmt: skip
        s.add_all([v1, v2])
    app = create_app(settings, factory, background=False)
    return TestClient(app), factory


def _ids(factory):
    with session_scope(factory) as s:
        return [c.id for c in s.query(Clip).order_by(Clip.id)], s.query(Video).first().id


def test_pages_render(env):
    client, factory = env
    _, vid = _ids(factory)
    for url in ("/", "/published", "/sources", "/activity", f"/video/{vid}"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Vidéo A" in client.get("/").text
    assert "boom" in client.get("/activity").text
    assert client.get("/video/9999").status_code == 404


def test_full_posting_flow_moves_video_to_published(env):
    client, factory = env
    clip_ids, vid = _ids(factory)

    assert "Vidéo A" in client.get("/").text
    assert "Vidéo A" not in client.get("/published").text

    client.post(f"/clips/{clip_ids[0]}/posted", follow_redirects=False)
    # il reste un clip : la vidéo est toujours dans « À poster », pas encore publiée
    assert "Vidéo A" in client.get("/").text
    assert "Vidéo A" not in client.get("/published").text

    client.post(f"/clips/{clip_ids[1]}/posted", follow_redirects=False)
    assert "Vidéo A" not in client.get("/").text
    assert "Vidéo A" in client.get("/published").text

    # annuler un « posté » ramène la vidéo dans « À poster »
    client.post(f"/clips/{clip_ids[1]}/unposted", follow_redirects=False)
    assert "Vidéo A" in client.get("/").text
    assert "Vidéo A" not in client.get("/published").text


def test_reject_and_edit(env):
    client, factory = env
    clip_ids, _ = _ids(factory)
    client.post(f"/clips/{clip_ids[0]}/edit", data={"description": "Nouvelle", "tiktok_url": ""})
    client.post(f"/clips/{clip_ids[1]}/reject")
    with session_scope(factory) as s:
        c1, c2 = s.get(Clip, clip_ids[0]), s.get(Clip, clip_ids[1])
        assert c1.description == "Nouvelle" and c1.status == C_READY
        assert c2.status == "rejected"


def test_channel_filter_and_retry(env):
    client, factory = env
    assert "Vidéo A" in client.get("/?channel=Chaîne A").text
    assert "Vidéo A" not in client.get("/?channel=Autre").text
    with session_scope(factory) as s:
        failed_id = s.query(Video).filter_by(external_id="b").one().id
    client.post(f"/videos/{failed_id}/retry")
    with session_scope(factory) as s:
        assert s.get(Video, failed_id).status == "pending"


def test_download_missing_file_is_404(env):
    client, factory = env
    clip_ids, _ = _ids(factory)
    assert client.get(f"/clips/{clip_ids[0]}/download").status_code == 404
