import pytest
from fastapi.testclient import TestClient

from clipforge.config import Settings
from clipforge.db.models import C_POSTED, V_DONE, Clip, Profile, Source, Video
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.web.app import PROFILE_COOKIE, create_app

PWD = "mot-de-passe-solide"


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    factory = make_session_factory(make_engine(tmp_path / "t.sqlite"))
    with session_scope(factory) as s:
        general = s.query(Profile).one()
        cars = Profile(name="Voiture", niche="auto")
        s.add(cars)
        s.flush()
        va = Video(platform="youtube", external_id="a", url="u", title="Vidéo GENERALE",
                   status=V_DONE, profile_id=general.id)  # fmt: skip
        va.clips = [Clip(file_path="1/01.mp4", start_s=0, end_s=30)]
        vb = Video(platform="youtube", external_id="b", url="u", title="Vidéo VOITURE",
                   status=V_DONE, profile_id=cars.id)  # fmt: skip
        vb.clips = [Clip(file_path="2/01.mp4", start_s=0, end_s=30),
                    Clip(file_path="2/02.mp4", start_s=40, end_s=70)]  # fmt: skip
        s.add_all([va, vb])
    client = TestClient(create_app(settings, factory, background=False), follow_redirects=False)
    client.post("/register", data={"email": "a@b.co", "password": PWD, "confirm": PWD})
    return client, factory


def ids(factory):
    with session_scope(factory) as s:
        return {p.name: p.id for p in s.query(Profile)}


def test_profile_pages_render(env):
    client, _ = env
    for url in ("/profiles", "/", "/published", "/sources", "/activity"):
        assert client.get(url).status_code == 200, url
    page = client.get("/profiles").text
    assert "Voiture" in page and "Général" in page


def test_switcher_filters_pages_and_counters(env):
    client, factory = env
    pid = ids(factory)
    both = client.get("/").text
    assert "Vidéo GENERALE" in both and "Vidéo VOITURE" in both

    r = client.post("/profiles/select", data={"profile": str(pid["Voiture"])})
    assert r.status_code == 303 and PROFILE_COOKIE in r.headers["set-cookie"]
    only_cars = client.get("/").text
    assert "Vidéo VOITURE" in only_cars and "Vidéo GENERALE" not in only_cars
    assert "À poster (2)" in only_cars  # compteur du profil actif seulement

    client.post("/profiles/select", data={"profile": "all"})
    assert "À poster (3)" in client.get("/").text


def test_unknown_profile_cookie_means_all(env):
    client, _ = env
    client.cookies.set(PROFILE_COOKIE, "9999")
    assert "Vidéo GENERALE" in client.get("/").text
    client.cookies.set(PROFILE_COOKIE, "pas-un-nombre")
    assert client.get("/").status_code == 200


def test_create_profile_and_errors(env):
    client, factory = env
    r = client.post("/profiles", data={"name": "Dev perso", "niche": "code",
                                       "base_hashtags": "dev, #python"})  # fmt: skip
    assert r.status_code == 303
    with session_scope(factory) as s:
        p = s.query(Profile).filter_by(name="Dev perso").one()
        assert p.base_hashtags == "#dev #python"
    dup = client.post("/profiles", data={"name": "dev PERSO"})
    assert dup.status_code == 400 and "déjà" in dup.text
    assert client.post("/profiles", data={"name": " "}).status_code == 400


def test_update_profile(env):
    client, factory = env
    pid = ids(factory)["Voiture"]
    assert (
        client.post(f"/profiles/{pid}", data={"name": "Auto", "niche": "sportives"}).status_code
        == 303
    )
    with session_scope(factory) as s:
        assert s.get(Profile, pid).name == "Auto"
    clash = client.post(f"/profiles/{pid}", data={"name": "Général"})
    assert clash.status_code == 400
    with session_scope(factory) as s:
        assert s.get(Profile, pid).name == "Auto"  # rien n'a changé
    assert client.post("/profiles/9999", data={"name": "x"}).status_code == 404


def test_delete_profile_guards_and_clears_cookie(env):
    client, factory = env
    pid = ids(factory)
    r = client.post(f"/profiles/{pid['Voiture']}/delete")
    assert r.status_code == 400 and "contient encore" in r.text
    # profil vide : suppression possible, et le cookie du profil actif est effacé
    client.post("/profiles", data={"name": "Vide"})
    vide = ids(factory)["Vide"]
    client.cookies.set(PROFILE_COOKIE, str(vide))
    r = client.post(f"/profiles/{vide}/delete")
    assert r.status_code == 303 and "clipforge_profile=" in r.headers["set-cookie"]
    assert "Vide" not in ids(factory)


def test_add_source_with_profile(env, monkeypatch):
    from clipforge.sources import service

    monkeypatch.setattr(service, "_fetch", lambda s, st: ("Chaîne", []))
    client, factory = env
    pid = ids(factory)["Voiture"]
    client.post(
        "/sources", data={"platform": "youtube", "identifier": "@gmk", "profile_id": str(pid)}
    )
    # profil inconnu -> retombe sur le profil par défaut, sans planter
    client.post(
        "/sources", data={"platform": "youtube", "identifier": "@autre", "profile_id": "9999"}
    )
    with session_scope(factory) as s:
        by = {x.identifier: x.profile_id for x in s.query(Source)}
    assert by["@gmk"] == pid
    assert by["@autre"] == ids(factory)["Général"]


def test_move_source_moves_its_unposted_videos_only(env):
    client, factory = env
    pid = ids(factory)
    with session_scope(factory) as s:
        src = Source(platform="youtube", identifier="@x", profile_id=pid["Voiture"])
        s.add(src)
        s.flush()
        for v in s.query(Video):
            v.source_id = src.id
        s.query(Clip).filter(Clip.file_path == "2/01.mp4").one().status = C_POSTED
        sid = src.id
    client.post(f"/sources/{sid}/profile", data={"profile_id": str(pid["Général"])})
    with session_scope(factory) as s:
        assert s.get(Source, sid).profile_id == pid["Général"]
        by = {v.external_id: v.profile_id for v in s.query(Video)}
    assert by["a"] == pid["Général"]
    assert by["b"] == pid["Voiture"]  # un clip y est déjà posté : il reste sur son compte


def test_move_video_endpoint_and_posted_guard(env):
    client, factory = env
    pid = ids(factory)
    with session_scope(factory) as s:
        va = s.query(Video).filter_by(external_id="a").one().id
        vb = s.query(Video).filter_by(external_id="b").one().id
    assert (
        client.post(f"/videos/{va}/profile", data={"profile_id": str(pid["Voiture"])}).status_code
        == 303
    )
    with session_scope(factory) as s:
        assert s.get(Video, va).profile_id == pid["Voiture"]
        s.query(Clip).filter(Clip.video_id == vb).first().status = C_POSTED
    r = client.post(f"/videos/{vb}/profile", data={"profile_id": str(pid["Général"])})
    assert r.status_code == 400 and "déjà posté" in r.text
    with session_scope(factory) as s:
        assert s.get(Video, vb).profile_id == pid["Voiture"]
    assert client.post(f"/videos/{vb}/profile", data={"profile_id": "9999"}).status_code == 404


def test_profile_routes_require_login(env):
    client, _ = env
    client.post("/logout")
    for url in ("/profiles", "/profiles/1", "/profiles/select", "/profiles/1/delete",
                "/videos/1/profile", "/sources/1/profile"):  # fmt: skip
        r = client.post(url, data={"name": "x", "profile_id": "1"})
        assert r.status_code == 303 and r.headers["location"] == "/login", url
    assert client.get("/profiles").status_code == 303
