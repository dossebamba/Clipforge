from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from clipforge.config import Settings
from clipforge.db.models import AuthSession, Clip, User, Video, utcnow
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.web import auth
from clipforge.web.app import create_app

PWD = "mot-de-passe-solide"
NEW_PWD = PWD + "-v2"  # dérivé, pour ne pas multiplier les mots de passe en dur dans les tests
CREDS = {"email": "moi@example.com", "password": PWD, "confirm": PWD}


def make(tmp_path, **settings_kw):
    settings = Settings(data_dir=tmp_path / "data", **settings_kw)
    factory = make_session_factory(make_engine(tmp_path / "t.sqlite"))
    app = create_app(settings, factory, background=False)
    return TestClient(app, follow_redirects=False), factory, settings


@pytest.fixture
def anon(tmp_path):
    return make(tmp_path)


@pytest.fixture
def logged(tmp_path):
    client, factory, settings = make(tmp_path)
    assert client.post("/register", data=CREDS).status_code == 303
    return client, factory, settings


# ---------- Fonctions pures ----------


def test_password_hash_roundtrip():
    h = auth.hash_password(PWD)
    assert h.startswith("scrypt$") and PWD not in h
    assert auth.verify_password(PWD, h)
    assert not auth.verify_password("autre", h)
    assert auth.hash_password(PWD) != h  # sel aléatoire


@pytest.mark.parametrize(
    "bad", ["", "texte", "scrypt$1$2", "md5$1$2$3$a$b", "scrypt$x$8$1$AA==$AA=="]
)
def test_verify_rejects_malformed_hash(bad):
    assert auth.verify_password(PWD, bad) is False


def test_safe_next_blocks_open_redirect():
    assert auth.safe_next("/video/3?x=1") == "/video/3?x=1"
    for evil in ("//evil.com", "https://evil.com", "/\\evil.com", "", None, "javascript:alert(1)"):
        assert auth.safe_next(evil) == "/"


def test_validate_registration():
    s = Settings()
    ok = auth.validate_registration
    assert ok("a@b.co", PWD, PWD, s) is None
    assert ok("pas-un-mail", PWD, PWD, s)
    assert ok("a@b.co", "court", "court", s)
    assert ok("a@b.co", PWD, PWD + "x", s)
    assert ok("a@b.co", "x" * 129, "x" * 129, s)
    assert ok("longmotdepasse@b.co", "longmotdepasse@b.co", "longmotdepasse@b.co", s)


def test_rate_limiter():
    rl = auth.RateLimiter(max_failures=3, window_s=600)
    for _ in range(3):
        assert not rl.blocked("ip")
        rl.fail("ip")
    assert rl.blocked("ip") and not rl.blocked("autre")
    rl.reset("ip")
    assert not rl.blocked("ip")


# ---------- Accès et redirections ----------


def test_first_visit_redirects_to_register(anon):
    client, _, _ = anon
    assert client.get("/").headers["location"] == "/register"
    assert client.get("/login").headers["location"] == "/register"
    assert client.get("/register").status_code == 200


def test_anonymous_is_sent_to_login_with_next(logged):
    client, _, _ = logged
    client.post("/logout")
    r = client.get("/video/1?a=b")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fvideo%2F1%3Fa%3Db"
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200  # ressources publiques


def test_all_protected_routes_require_login(logged):
    client, _, _ = logged
    client.post("/logout")
    gets = [
        "/",
        "/published",
        "/sources",
        "/activity",
        "/video/1",
        "/clips/1/download",
        "/media/1/01.mp4",
    ]
    posts = [
        "/clips/1/posted", "/clips/1/unposted", "/clips/1/reject", "/clips/1/restore",
        "/clips/1/edit", "/videos/1/retry", "/videos/1/delete", "/sources",
        "/sources/manual", "/sources/1/toggle", "/sources/1/delete", "/poll",
    ]  # fmt: skip
    for url in gets:
        r = client.get(url)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url
    for url in posts:
        r = client.post(url, data={"platform": "youtube", "identifier": "x", "urls": "x"})
        assert r.status_code == 303 and r.headers["location"] == "/login", url


def test_anonymous_post_does_not_modify_data(logged):
    client, factory, _ = logged
    with session_scope(factory) as s:
        v = Video(platform="youtube", external_id="a", url="u", status="done")
        v.clips = [Clip(file_path="1/01.mp4", start_s=0, end_s=30)]
        s.add(v)
    client.post("/logout")
    client.post("/clips/1/posted")
    client.post("/videos/1/delete")
    with session_scope(factory) as s:
        assert s.get(Clip, 1).status == "ready"
        assert s.get(Video, 1) is not None


# ---------- Inscription ----------


def test_registration_closes_after_first_account(logged):
    client, factory, _ = logged
    client.post("/logout")
    assert client.get("/register").status_code == 403
    r = client.post("/register", data={**CREDS, "email": "autre@example.com"})
    assert r.status_code == 403
    with session_scope(factory) as s:
        assert s.query(User).count() == 1


def test_registration_can_stay_open_via_setting(tmp_path):
    client, factory, _ = make(tmp_path, allow_registration=True)
    client.post("/register", data=CREDS)
    client.post("/logout")
    assert client.get("/register").status_code == 200
    assert client.post("/register", data={**CREDS, "email": "autre@example.com"}).status_code == 303
    with session_scope(factory) as s:
        assert s.query(User).count() == 2


def test_register_validation_errors_and_duplicates(tmp_path):
    client, factory, _ = make(tmp_path, allow_registration=True)
    r = client.post("/register", data={**CREDS, "password": "court", "confirm": "court"})
    assert r.status_code == 400 and "au moins" in r.text
    r = client.post("/register", data={**CREDS, "confirm": "different-different"})
    assert r.status_code == 400
    assert client.post("/register", data=CREDS).status_code == 303
    r = client.post("/register", data={**CREDS, "email": "MOI@example.com "})  # même e-mail
    assert r.status_code == 400 and "Impossible" in r.text
    with session_scope(factory) as s:
        assert s.query(User).count() == 1


def test_password_never_stored_in_clear(logged):
    _, factory, _ = logged
    with session_scope(factory) as s:
        u = s.query(User).one()
        assert PWD not in u.password_hash and u.email == "moi@example.com"


# ---------- Connexion ----------


def test_login_success_sets_secure_cookie_and_redirects_to_next(logged):
    client, _, _ = logged
    client.post("/logout")
    r = client.post(
        "/login", data={"email": "MOI@example.com", "password": PWD, "next": "/sources"}
    )
    assert r.status_code == 303 and r.headers["location"] == "/sources"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/sources").status_code == 200


def test_login_ignores_external_next(logged):
    client, _, _ = logged
    client.post("/logout")
    r = client.post(
        "/login", data={"email": "moi@example.com", "password": PWD, "next": "//evil.com"}
    )
    assert r.headers["location"] == "/"


def test_login_errors_are_identical_for_unknown_email_and_bad_password(logged):
    client, _, _ = logged
    client.post("/logout")
    a = client.post("/login", data={"email": "moi@example.com", "password": "mauvais-mot-de-passe"})
    b = client.post(
        "/login", data={"email": "inconnu@example.com", "password": "mauvais-mot-de-passe"}
    )
    assert a.status_code == b.status_code == 401
    assert auth.BAD_CREDENTIALS in a.text and auth.BAD_CREDENTIALS in b.text


def test_login_is_rate_limited(logged):
    client, _, _ = logged
    client.post("/logout")
    for _ in range(5):
        assert (
            client.post(
                "/login", data={"email": "moi@example.com", "password": "mauvais-mot-de-passe"}
            ).status_code
            == 401
        )
    # même avec le bon mot de passe, la source est bloquée
    assert (
        client.post("/login", data={"email": "moi@example.com", "password": PWD}).status_code == 429
    )


def test_logout_revokes_session_server_side(logged):
    client, factory, _ = logged
    token = client.cookies.get(auth.COOKIE_NAME)
    assert token and client.get("/").status_code == 200
    client.post("/logout")
    with session_scope(factory) as s:
        assert s.query(AuthSession).count() == 0
    client.cookies.set(auth.COOKIE_NAME, token)  # rejouer l'ancien cookie
    assert client.get("/").status_code == 303


def test_expired_session_is_rejected(logged):
    client, factory, _ = logged
    with session_scope(factory) as s:
        s.query(AuthSession).update({"expires_at": utcnow() - timedelta(seconds=1)})
    assert client.get("/").status_code == 303


def test_token_is_stored_hashed(logged):
    client, factory, _ = logged
    token = client.cookies.get(auth.COOKIE_NAME)
    with session_scope(factory) as s:
        assert s.query(AuthSession).one().token_hash != token


def test_forged_cookie_is_rejected(logged):
    client, _, _ = logged
    client.cookies.set(auth.COOKIE_NAME, "n-importe-quoi")
    assert client.get("/").status_code == 303


# ---------- Protection des clips ----------


def test_media_requires_login_and_blocks_path_traversal(logged):
    client, _, settings = logged
    f = settings.clips_dir / "1" / "01.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"fake-video")
    assert client.get("/media/1/01.mp4").content == b"fake-video"
    (settings.data_dir / "secret.txt").write_text("secret")
    assert client.get("/media/../secret.txt").status_code == 404
    assert client.get("/media/..%2Fsecret.txt").status_code == 404
    client.post("/logout")
    assert client.get("/media/1/01.mp4").status_code == 303


# ---------- CSRF et en-têtes ----------


def test_cross_site_post_is_refused(logged):
    client, _, _ = logged
    r = client.post("/logout", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/").status_code == 200  # toujours connecté


def test_same_origin_post_is_allowed(logged):
    client, _, _ = logged
    r = client.post("/poll", headers={"origin": "http://testserver", "host": "testserver"})
    assert r.status_code == 303


def test_security_headers_present(logged):
    client, _, _ = logged
    h = client.get("/").headers
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    client.post("/logout")
    assert client.get("/login").headers["cache-control"] == "no-store"


# ---------- Réinitialisation du mot de passe ----------


def test_reset_password_changes_hash_and_revokes_sessions(logged):
    client, factory, _ = logged
    with session_scope(factory) as s:
        assert auth.set_password(s, "moi@example.com", NEW_PWD) is True
        assert auth.set_password(s, "inconnu@example.com", "x" * 12) is False
    assert client.get("/").status_code == 303  # ancienne session fermée
    r = client.post("/login", data={"email": "moi@example.com", "password": NEW_PWD})
    assert r.status_code == 303
    client.post("/logout")
    r = client.post("/login", data={"email": "moi@example.com", "password": PWD})
    assert r.status_code == 401
