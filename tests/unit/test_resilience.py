import httpx
import pytest
from fastapi.testclient import TestClient

from clipforge.config import Settings
from clipforge.db.models import (
    C_POSTED,
    V_DONE,
    V_FAILED,
    V_PENDING,
    V_SEEN,
    V_SKIPPED,
    Clip,
    Profile,
    Source,
    Video,
)
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.llm import providers
from clipforge.llm.providers import GeminiProvider, LLMError, OpenAICompatProvider, build_providers
from clipforge.pipeline import select as sel
from clipforge.pipeline.transcribe import Word, group_sentences
from clipforge.sources import service
from clipforge.sources.base import RemoteVideo
from clipforge.web.app import PROFILE_COOKIE, create_app

# =====================================================================================
# Fournisseurs : quotas, modèles de repli, délais
# =====================================================================================


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


OK = {"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]}


def model_of(req):
    return req.url.path.split("/models/")[1].split(":")[0]


def test_gemini_switches_to_next_model_on_quota_without_retrying():
    seen = []

    def handler(req):
        seen.append(model_of(req))
        if model_of(req) == "principal":
            return httpx.Response(429, json={"error": {"message": "You exceeded your quota"}})
        return httpx.Response(200, json=OK)

    p = GeminiProvider("k", ["principal", "lite"], client_for(handler))
    assert p.complete("s", "p") == '{"ok": 1}'
    assert seen == ["principal", "lite"]  # un seul essai sur le modèle épuisé


def test_gemini_retries_server_errors_on_same_model_before_switching():
    seen = []

    def handler(req):
        seen.append(model_of(req))
        return (
            httpx.Response(503, text="busy")
            if model_of(req) == "a"
            else httpx.Response(200, json=OK)
        )

    GeminiProvider("k", ["a", "b"], client_for(handler)).complete("s", "p")
    assert seen == ["a"] * providers.MAX_ATTEMPTS + ["b"]


def test_gemini_all_models_failing_reports_each():
    def handler(req):
        return httpx.Response(429, text="quota")

    with pytest.raises(LLMError) as exc:
        GeminiProvider("k", ["a", "b"], client_for(handler)).complete("s", "p")
    assert "gemini/a" in str(exc.value) and "gemini/b" in str(exc.value)


def test_build_providers_splits_gemini_models_and_errors_never_leak_the_key():
    s = Settings(gemini_api_key="K", gemini_model="m1, m2 ,", llm_providers="gemini")
    (p,) = build_providers(s)
    assert p.models == ["m1", "m2"]

    def handler(req):
        return httpx.Response(429, text="quota")

    with pytest.raises(LLMError) as exc:
        GeminiProvider("SECRET-KEY", ["a"], client_for(handler)).complete("s", "p")
    assert "SECRET-KEY" not in str(exc.value)


@pytest.mark.parametrize(
    ("headers", "text", "expected"),
    [
        ({"retry-after": "7"}, "", 7.0),
        ({}, "Rate limit. Please try again in 8.2s. Visit", 9.2),
        ({}, "Please try again in 350ms", 1.35),
        ({}, "Please try again in 2m", 62.0),  # plafonné à MAX_WAIT_S
        ({"retry-after": "500"}, "", 62.0),
        ({}, "pas d'indication", 3.0),
    ],
)
def test_wait_time_from_headers_or_message(headers, text, expected):
    resp = httpx.Response(429, headers=headers, text=text)
    assert providers._wait_time(resp, 1) == pytest.approx(expected)


def test_groq_waits_the_delay_given_in_the_message(monkeypatch):
    waits = []
    monkeypatch.setattr(providers.time, "sleep", waits.append)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="Rate limit ... Please try again in 12.4s.")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    OpenAICompatProvider("groq", "http://x", "k", "m", client_for(handler)).complete("s", "p")
    assert waits == [pytest.approx(13.4)]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("43.095s", 43.095),
        ("7m12s", 432.0),
        ("350ms", 0.35),
        ("2m", 120.0),
        ("1h2m", 3720.0),
        ("", 0.0),
    ],
)
def test_parse_duration(text, seconds):
    assert providers.parse_duration_s(text) == pytest.approx(seconds)


def groq_response(remaining, reset="30s", status=200):
    headers = {"x-ratelimit-remaining-tokens": str(remaining), "x-ratelimit-reset-tokens": reset}
    return httpx.Response(
        status, headers=headers, json={"choices": [{"message": {"content": "{}"}}]}
    )


def test_provider_waits_for_the_minute_window_instead_of_sending_a_doomed_request(monkeypatch):
    """Groq compte entrée + plafond de réponse : s'il reste trop peu, on attend la remise à zéro."""
    waits, sent = [], []
    monkeypatch.setattr(providers.time, "sleep", waits.append)
    clock = [1000.0]
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])

    def handler(req):
        sent.append(1)
        return groq_response(remaining=2000, reset="40s")  # il ne reste que 2000 tokens

    p = OpenAICompatProvider("groq", "http://x", "k", "m", client_for(handler))
    p.complete("s", "p" * 3000)  # 1er appel : rien n'est connu, on envoie
    assert waits == [] and len(sent) == 1
    p.complete("s", "p" * 3000)  # besoin estimé ~3 200 > 2 000 restants : on attend
    assert waits == [pytest.approx(40.5)] and len(sent) == 2


def test_no_pause_when_enough_tokens_remain_or_window_already_reset(monkeypatch):
    waits = []
    monkeypatch.setattr(providers.time, "sleep", waits.append)
    clock = [1000.0]
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    state = {"remaining": 7000}

    def handler(req):
        return groq_response(remaining=state["remaining"], reset="40s")

    p = OpenAICompatProvider("groq", "http://x", "k", "m", client_for(handler))
    p.complete("s", "p" * 100)
    p.complete("s", "p" * 100)  # il reste 7 000 tokens : largement assez
    assert waits == []
    state["remaining"] = 100
    p.complete("s", "p" * 100)  # il ne reste que 100 : la limite sera libérée dans 40 s...
    clock[0] += 120  # ...mais 2 minutes ont passé : plus d'attente nécessaire
    p.complete("s", "p" * 100)
    assert waits == []


def test_pause_is_capped_and_ignored_without_rate_limit_headers(monkeypatch):
    waits = []
    monkeypatch.setattr(providers.time, "sleep", waits.append)
    monkeypatch.setattr(providers.time, "monotonic", lambda: 0.0)
    calls = iter([groq_response(10, reset="30m"), groq_response(10, reset="30m")])
    p = OpenAICompatProvider("groq", "http://x", "k", "m", client_for(lambda req: next(calls)))
    p.complete("s", "p")
    p.complete("s", "p")
    assert waits == [providers.MAX_WAIT_S + 0.5]  # jamais plus d'une minute d'un coup

    plain = OpenAICompatProvider("or", "http://x", "k", "m", client_for(lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": "{}"}}]})))  # fmt: skip
    waits.clear()
    plain.complete("s", "p")
    plain.complete("s", "p")
    assert waits == []  # OpenRouter ne fournit pas ces en-têtes : comportement inchangé


# =====================================================================================
# Fenêtres de transcription par budget de tokens
# =====================================================================================


def sentences_for(words_per_second: float, minutes: int):
    """Transcription synthétique : mots de 6 lettres, une phrase par ~12 mots."""
    n = int(words_per_second * 60 * minutes)
    words = [
        Word("abcdef" + ("." if (i + 1) % 12 == 0 else ""), i / words_per_second,
              i / words_per_second + 0.2)
        for i in range(n)
    ]  # fmt: skip
    return group_sentences(words)


def test_short_transcript_is_a_single_window():
    assert len(sel._windows(sentences_for(2, 2))) == 1


def test_fast_talker_gets_more_windows_than_slow_one_for_same_duration():
    fast, slow = sentences_for(4, 10), sentences_for(1, 10)  # 10 minutes chacun
    assert len(sel._windows(fast)) > len(sel._windows(slow))


def test_every_window_respects_the_token_budget_and_all_text_is_covered():
    sents = sentences_for(3.5, 30)
    windows = sel._windows(sents)
    assert len(windows) > 1
    for w in windows:
        assert sum(sel._cost(s) for s in w) <= sel.WINDOW_TOKENS
    assert {s[0] for w in windows for s in w} == {s[0] for s in sents}  # aucune phrase perdue


def test_windows_overlap_slightly():
    windows = sel._windows(sentences_for(3, 20))
    assert {s[0] for s in windows[0]} & {s[0] for s in windows[1]}


def test_oversized_single_sentence_does_not_loop_forever():
    huge = [(0.0, 5.0, "x" * 20000), (6.0, 9.0, "court"), (10.0, 12.0, "fin")]
    windows = sel._windows(huge)
    assert sum(len(w) for w in windows) >= 3 and len(windows) <= 4


def test_worst_case_request_stays_under_groq_free_limit_with_all_extras():
    """Fenêtre pleine + consignes + options + réponse réservée, pour un débit très rapide."""
    sents = sentences_for(5, 30)
    style = sel.Style(language="français", niche="automobile, sportives, tuning",
                      instructions="ton direct et enthousiaste", callouts=True, teaser=True)  # fmt: skip
    rules, fields = sel.extras(style)
    worst = 0
    for w in sel._windows(sents):
        prompt = sel.PROMPT.format(
            title="Titre de vidéo assez long pour faire du volume", channel="Chaîne",
            profile_block=sel.profile_block(style), extra_rules=rules, extra_fields=fields,
            transcript=sel._fmt_transcript(w), language="français", n=4, min_s=20, max_s=60,
        )  # fmt: skip
        worst = max(worst, (len(sel.SYSTEM) + len(prompt)) // 3 + providers.MAX_OUTPUT_TOKENS)
    assert worst < 7000, worst  # marge sous les 8 000 tokens par minute


def test_prompt_asks_for_at_most_four_clips_per_window():
    captured = []

    class Rec:
        def generate_json(self, system, prompt):
            captured.append(prompt)
            return {"clips": []}

    words = [Word("mot.", float(i), i + 0.9) for i in range(60)]
    sel.select_clips(Rec(), words, 60, "T", "C", Settings(max_clips_per_video=6))
    assert "jusqu'à 4 extraits" in captured[0]


# =====================================================================================
# Suppression des erreurs de l'historique
# =====================================================================================


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    factory = make_session_factory(make_engine(tmp_path / "t.sqlite"))
    client = TestClient(create_app(settings, factory, background=False), follow_redirects=False)
    pwd = "mot-de-passe-solide"
    client.post("/register", data={"email": "a@b.co", "password": pwd, "confirm": pwd})
    return client, factory, settings


def add_video(s, ext, status, **kw):
    v = Video(platform="youtube", external_id=ext, url=f"https://y/{ext}", title=f"Vidéo {ext}",
              status=status, **kw)  # fmt: skip
    s.add(v)
    s.flush()
    return v


def statuses(factory):
    with session_scope(factory) as s:
        return {v.external_id: v.status for v in s.query(Video)}


def test_dismiss_failed_video_hides_it_and_clears_error(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        vid = add_video(s, "bad", V_FAILED, error="LLMError: quota").id
    assert "LLMError: quota" in client.get("/activity").text
    assert client.post(f"/videos/{vid}/dismiss").status_code == 303
    page = client.get("/activity").text
    assert "LLMError: quota" not in page and "Vidéo bad" not in page
    with session_scope(factory) as s:
        v = s.get(Video, vid)
        assert v.status == V_SEEN and v.error == ""


def test_dismissed_video_is_not_requeued_by_the_next_poll(env, monkeypatch):
    """Le piège : supprimer la ligne ferait revenir la vidéo au relevé suivant."""
    client, factory, _ = env
    remote = [RemoteVideo("youtube", "bad", "https://y/bad", "Vidéo bad", "C", duration_s=300)]
    monkeypatch.setattr(service, "_fetch", lambda s, st: ("C", remote))
    with session_scope(factory) as s:
        src = Source(platform="youtube", identifier="@c", initial_done=True,
                     profile_id=s.query(Profile).one().id)  # fmt: skip
        s.add(src)
        s.flush()
        vid = add_video(s, "bad", V_FAILED, error="boom", source_id=src.id).id
        sid = src.id
    client.post(f"/videos/{vid}/dismiss")
    with session_scope(factory) as s:
        assert service.poll_source(s, s.get(Source, sid), Settings()) == 0
        assert s.query(Video).filter_by(external_id="bad").count() == 1
    assert statuses(factory)["bad"] == V_SEEN


def test_dismiss_removes_partial_clips_and_their_files(env):
    client, factory, settings = env
    with session_scope(factory) as s:
        v = add_video(s, "partial", V_FAILED, error="crash en plein montage")
        v.clips = [Clip(file_path="1/01.mp4", start_s=0, end_s=30)]
        s.flush()
        vid = v.id
    folder = settings.clips_dir / str(vid)
    folder.mkdir(parents=True)
    (folder / "01.mp4").write_bytes(b"x")
    client.post(f"/videos/{vid}/dismiss")
    assert not folder.exists()
    with session_scope(factory) as s:
        assert s.query(Clip).count() == 0


def test_dismiss_never_touches_videos_that_have_clips_or_are_in_progress(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        ok = add_video(s, "ok", V_DONE)
        ok.clips = [Clip(file_path="x", start_s=0, end_s=30, status=C_POSTED)]
        wait = add_video(s, "wait", V_PENDING)
        s.flush()
        ids = [ok.id, wait.id]
    for vid in ids:
        assert client.post(f"/videos/{vid}/dismiss").status_code == 303
    assert statuses(factory) == {"ok": V_DONE, "wait": V_PENDING}
    with session_scope(factory) as s:
        assert s.query(Clip).count() == 1  # le clip posté est intact
    assert client.post("/videos/9999/dismiss").status_code == 404


def test_clear_all_failures_only_clears_that_list(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        add_video(s, "f1", V_FAILED, error="e1")
        add_video(s, "f2", V_FAILED, error="e2")
        add_video(s, "sk", V_SKIPPED, error="trop longue")
        done = add_video(s, "done", V_DONE)
        done.clips = [Clip(file_path="x", start_s=0, end_s=30)]
    assert client.post("/activity/clear", data={"kind": "failed"}).status_code == 303
    assert statuses(factory) == {"f1": V_SEEN, "f2": V_SEEN, "sk": V_SKIPPED, "done": V_DONE}


def test_clear_empty_never_deletes_a_video_with_clips(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        add_video(s, "empty", V_DONE, error="Aucun moment retenu")
        full = add_video(s, "full", V_DONE)
        full.clips = [Clip(file_path="x", start_s=0, end_s=30)]
    client.post("/activity/clear", data={"kind": "empty"})
    assert statuses(factory) == {"empty": V_SEEN, "full": V_DONE}


def test_clear_skipped_and_unknown_kind(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        add_video(s, "sk", V_SKIPPED, error="live en cours")
    client.post("/activity/clear", data={"kind": "skipped"})
    assert statuses(factory) == {"sk": V_SEEN}
    assert client.post("/activity/clear", data={"kind": "tout"}).status_code == 400


def test_clear_is_limited_to_the_active_profile(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        general = s.query(Profile).one()
        cars = Profile(name="Voiture")
        s.add(cars)
        s.flush()
        add_video(s, "g", V_FAILED, error="e", profile_id=general.id)
        add_video(s, "c", V_FAILED, error="e", profile_id=cars.id)
        cars_id = cars.id
    client.cookies.set(PROFILE_COOKIE, str(cars_id))
    client.post("/activity/clear", data={"kind": "failed"})
    assert statuses(factory) == {"g": V_FAILED, "c": V_SEEN}  # l'autre profil n'est pas touché


def test_activity_page_offers_the_delete_buttons(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        add_video(s, "bad", V_FAILED, error="boom")
    page = client.get("/activity").text
    assert "/dismiss" in page and 'value="failed"' in page and "Tout supprimer" in page
    assert "Réessayer" in page  # on peut toujours retenter avant de supprimer


def test_error_message_is_escaped_in_the_page(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        add_video(s, "bad", V_FAILED, error="<script>alert(1)</script> quota")
    page = client.get("/activity").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_new_routes_require_login(env):
    client, _, _ = env
    client.post("/logout")
    assert client.post("/videos/1/dismiss").headers["location"] == "/login"
    assert client.post("/activity/clear", data={"kind": "failed"}).headers["location"] == "/login"


# ---------- Supprimer une vidéo ne doit pas la faire revenir ----------


def test_deleting_a_source_video_keeps_a_marker_so_it_is_not_reprocessed(env, monkeypatch):
    client, factory, _ = env
    remote = [RemoteVideo("youtube", "v1", "https://y/v1", "Vidéo v1", "C", duration_s=300)]
    monkeypatch.setattr(service, "_fetch", lambda s, st: ("C", remote))
    with session_scope(factory) as s:
        src = Source(platform="youtube", identifier="@c", initial_done=True,
                     profile_id=s.query(Profile).one().id)  # fmt: skip
        s.add(src)
        s.flush()
        v = add_video(s, "v1", V_DONE, source_id=src.id)
        v.clips = [Clip(file_path="1/01.mp4", start_s=0, end_s=30)]
        s.flush()
        vid, sid = v.id, src.id
    client.post(f"/videos/{vid}/delete")
    with session_scope(factory) as s:
        assert s.query(Clip).count() == 0  # clips supprimés
        assert service.poll_source(s, s.get(Source, sid), Settings()) == 0  # et pas retraitée
    assert statuses(factory) == {"v1": V_SEEN}


def test_deleting_a_manual_video_removes_it_completely(env):
    client, factory, _ = env
    with session_scope(factory) as s:
        vid = add_video(s, "man", V_DONE).id
    client.post(f"/videos/{vid}/delete")
    assert statuses(factory) == {}


# ---------- Redemander explicitement une vidéo masquée ----------


@pytest.fixture
def manual(monkeypatch):
    def fake(url):
        return RemoteVideo("youtube", "x1", url, "Titre", "Chaîne", duration_s=300)

    monkeypatch.setattr(service.manual, "resolve_url", fake)


@pytest.mark.parametrize("status", [V_SEEN, V_SKIPPED])
def test_asking_again_for_a_hidden_video_requeues_it(manual, tmp_path, status):
    f = make_session_factory(make_engine(tmp_path / "t.sqlite"))
    with session_scope(f) as s:
        v = add_video(s, "x1", status, error="ancienne raison")
        other = Profile(name="Autre")
        s.add(other)
        s.flush()
        got = service.add_manual_url(s, "https://y/x1", other.id)
        assert got.id == v.id and got.status == V_PENDING and got.error == ""
        assert got.priority == service.MANUAL_PRIORITY and got.profile_id == other.id


@pytest.mark.parametrize("status", [V_DONE, V_PENDING, V_FAILED])
def test_asking_again_leaves_other_statuses_untouched(manual, tmp_path, status):
    f = make_session_factory(make_engine(tmp_path / "t.sqlite"))
    with session_scope(f) as s:
        add_video(s, "x1", status)
        assert service.add_manual_url(s, "https://y/x1").status == status
