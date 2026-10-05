import sqlite3

import pytest
from fastapi.testclient import TestClient

from clipforge import profiles as ps
from clipforge.config import Settings
from clipforge.db.models import V_PENDING, Clip, Profile, Video
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.pipeline import orchestrator as orch
from clipforge.pipeline.download import Downloaded
from clipforge.pipeline.render import Transform, build_ass, build_filtergraph
from clipforge.pipeline.select import (
    Candidate,
    Style,
    _parse_callouts,
    _parse_teaser,
    _to_candidate,
    extras,
    refine,
    select_clips,
    validate_callouts,
    validate_teaser,
)
from clipforge.pipeline.transcribe import Word
from clipforge.web.app import create_app

S = Settings()


def _words(n=200):
    """Un mot par seconde, une phrase toutes les 10 secondes (fins à 9.9, 19.9, ...)."""
    return [Word("mot." if (i + 1) % 10 == 0 else "mot", float(i), i + 0.9) for i in range(n)]


STARTS = [float(i) for i in range(0, 200, 10)]
ENDS = [i + 9.9 for i in range(0, 200, 10)]

# ---------- Validation des remarques ----------


def test_callouts_are_filtered_spaced_and_capped():
    raw = [
        (21.0, "Trop tôt"),  # < début + 4 s (début = 20)
        (25.0, "Première remarque"),
        (27.0, "Trop proche de la précédente"),
        (35.0, "Seconde"),
        (45.0, "Troisième ignorée (max 2)"),
        (38.0, "x" * 60),  # trop longue
        (49.0, "Trop tard"),  # > fin - 3 s (fin = 50)
        (30.0, ""),  # vide
    ]
    assert validate_callouts(raw, 20.0, 50.0) == [(25.0, "Première remarque"), (35.0, "Seconde")]


def test_callouts_parsing_is_tolerant():
    got = _parse_callouts(
        [{"at": "25", "text": "Salut #foot  toi"}, {"at": "x"}, "bizarre", {"text": "a"}]
    )
    assert got == [(25.0, "Salut toi")]
    assert _parse_callouts(None) == [] and _parse_callouts("texte") == []


# ---------- Validation de la bande-annonce ----------


WSTARTS = [float(i) for i in range(200)]
WENDS = [i + 0.9 for i in range(200)]


def test_teaser_uses_sentence_end_when_one_fits():
    # phrases courtes (4 s) : la fin se cale sur une fin de phrase
    starts = [float(i) for i in range(0, 200, 4)]
    ends = [i + 3.9 for i in range(0, 200, 4)]
    t = validate_teaser((41.0, 46.0), 20.0, 80.0, starts, ends, WSTARTS, WENDS)
    # fins de phrase à 43.9 et 47.9 : seule la première tient dans la demande (46 s + 0,5 s)
    assert t == (40.0, 43.9)


def test_teaser_falls_back_to_word_boundaries_when_sentences_are_long():
    # phrases de 10 s : aucune fin de phrase ne tient dans 2,5 à 7 s -> on coupe à une fin de mot
    t = validate_teaser((41.0, 46.0), 20.0, 80.0, STARTS, ENDS, WSTARTS, WENDS)
    assert t == (40.0, 45.9)
    # sans liste de mots : on ne peut pas caler, on rejette plutôt que de couper n'importe où
    assert validate_teaser((41.0, 46.0), 20.0, 80.0, STARTS, ENDS) is None


def test_teaser_duration_is_always_within_bounds():
    for asked in [(41.0, 90.0), (55.0, 56.0), (30.0, 45.0)]:
        t = validate_teaser(asked, 20.0, 80.0, STARTS, ENDS, WSTARTS, WENDS)
        assert t is None or 2.5 <= t[1] - t[0] <= 7.0


def test_teaser_rejected_when_in_the_opening_or_out_of_bounds():
    args = (STARTS, ENDS, WSTARTS, WENDS)
    assert validate_teaser((22.0, 26.0), 20.0, 80.0, *args) is None  # < début + 8 s
    assert validate_teaser((78.0, 90.0), 20.0, 80.0, *args) is None  # pas la place avant la fin
    assert validate_teaser(None, 20.0, 80.0, *args) is None
    short = validate_teaser((50.0, 50.5), 20.0, 80.0, *args)  # trop court : étendu au minimum
    assert short is not None and short[1] - short[0] >= 2.5


def test_refine_validates_extras_against_final_bounds():
    c = Candidate(
        start=20,
        end=80,
        score=90,
        callouts=[(25.0, "Ok"), (21.0, "Trop tôt")],
        teaser=(45.0, 50.0),
    )
    out = refine([c], _words(), 200, S)[0]
    assert out.callouts == [(25.0, "Ok")]
    assert out.teaser is not None and out.teaser[0] >= out.start + 8


# ---------- Prompt ----------


class Rec:
    def __init__(self, payload):
        self.payload, self.prompts = payload, []

    def generate_json(self, system, prompt):
        self.prompts.append(prompt)
        return self.payload


PAYLOAD = {
    "clips": [
        {
            "start": 20, "end": 60, "score": 90, "hook": "H", "title": "T", "description": "D",
            "hashtags": [], "reason": "r",
            "callouts": [{"at": 30, "text": "Wow"}], "teaser": {"start": 41, "end": 46},
        }
    ]
}  # fmt: skip


def test_extras_only_when_requested_and_braces_not_doubled():
    assert extras(Style()) == ("", "")
    rules, fields = extras(Style(callouts=True, teaser=True))
    assert "callouts" in rules and "teaser" in rules
    assert '"callouts": [{"at"' in fields and '"teaser": {"start"' in fields
    assert "{{" not in fields  # ce sont des valeurs de .format(), pas le modèle


def test_prompt_asks_for_extras_only_if_profile_enabled():
    plain, rich = Rec(PAYLOAD), Rec(PAYLOAD)
    select_clips(plain, _words(), 200, "T", "C", S, Style())
    select_clips(rich, _words(), 200, "T", "C", S, Style(callouts=True, teaser=True))
    assert '"callouts"' not in plain.prompts[0] and '"teaser"' not in plain.prompts[0]
    assert '"callouts"' in rich.prompts[0] and '"teaser"' in rich.prompts[0]
    assert "{{" not in rich.prompts[0]


def test_unrequested_extras_returned_by_the_model_are_discarded():
    out = select_clips(Rec(PAYLOAD), _words(), 200, "T", "C", S, Style())
    assert out[0].callouts == [] and out[0].teaser is None
    kept = select_clips(Rec(PAYLOAD), _words(), 200, "T", "C", S, Style(callouts=True, teaser=True))
    assert kept[0].callouts and kept[0].teaser


def test_candidate_parsing_survives_garbage_extras():
    c = _to_candidate(
        {"start": 0, "end": 30, "score": 80, "callouts": "n'importe quoi", "teaser": 5}
    )
    assert c is not None and c.callouts == [] and c.teaser is None
    assert _parse_teaser({"start": "a", "end": 3}) is None


# ---------- Sous-titres et graphe FFmpeg ----------


def _events(ass, style):
    return [
        line for line in ass.splitlines() if line.startswith("Dialogue") and f",{style}," in line
    ]


def test_ass_overlays_and_teaser_offsets():
    words = [Word(f"w{i}", 10 + i * 0.5, 10.4 + i * 0.5) for i in range(40)]
    t = Transform(
        teaser=(20.0, 23.0),
        callouts=[(16.0, "Info"), (12.0, "Tôt")],
        credit="Source : GMK",
        brand="@moi",
    )
    ass = build_ass(words, 10.0, 30.0, "Accroche", S, t)
    assert _events(ass, "Credit") and "Source : GMK" in _events(ass, "Credit")[0]
    assert "@moi" in _events(ass, "Brand")[0]
    # la remarque à 16 s (clip commence à 10 s) apparaît à 6 + 3 (bande-annonce) = 9 s ; celle de 12 s
    # tomberait pendant l'accroche (2 + 3 = 5 s > 3 s : OK) -> vérifions les horaires réels
    starts = sorted(e.split(",")[1] for e in _events(ass, "Callout"))
    assert starts == ["0:00:05.00", "0:00:09.00"]
    # le clip entier est décalé de la durée de la bande-annonce
    subs = _events(ass, "Sub")
    assert (
        subs[0].split(",")[1] == "0:00:00.00"
    )  # 1re ligne = bande-annonce (mots à partir de 20 s)
    assert any(e.split(",")[1] == "0:00:03.00" for e in subs)  # début du clip proprement dit
    # la fin globale = bande-annonce + durée du clip
    assert max(e.split(",")[2] for e in _events(ass, "Credit")) == "0:00:23.00"


def test_callout_during_hook_is_skipped():
    ass = build_ass(_words(30), 0.0, 20.0, "H", S, Transform(callouts=[(1.0, "Trop tôt")]))
    assert _events(ass, "Callout") == []


def test_no_transform_means_no_extra_events():
    ass = build_ass(_words(30), 0.0, 20.0, "H", S)
    assert not _events(ass, "Callout") and not _events(ass, "Credit") and not _events(ass, "Brand")


def test_filtergraph_with_teaser_builds_audio_and_video_concat():
    g = build_filtergraph(S, "blur_fit", True, True)
    # deux entrées lues séparément (0 = bande-annonce, 1 = clip), jamais de split/trim sur une seule
    assert "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[cv][ca]" in g
    assert "trim" not in g and "asplit" not in g
    assert "[ca]loudnorm" in g and g.rstrip().endswith("[a]")
    assert "[cv]split=2" in g  # le cadrage s'applique au flux assemblé
    mute = build_filtergraph(S, "crop", True, False)
    assert "[0:v][1:v]concat=n=2:v=1:a=0[cv]" in mute and "[ca]" not in mute
    assert "[cv]crop=" in mute
    plain = build_filtergraph(S, "blur_fit")
    assert "concat" not in plain and plain.rstrip().endswith("[v]")


# ---------- Options du profil ----------


@pytest.fixture
def factory(tmp_path):
    return make_session_factory(make_engine(tmp_path / "t.sqlite"))


def test_profile_options_validation_and_partial_update(factory):
    with session_scope(factory) as s:
        p = ps.create_profile(s, "A", brand_text="  @{mon}\\compte \n")
        assert p.brand_text == "@moncompte" and p.layout == "blur_fit"
        assert p.callouts and p.credit and not p.teaser  # valeurs par défaut
        with pytest.raises(ps.ProfileError, match="Cadrage"):
            ps.create_profile(s, "B", layout="zoom")
        with pytest.raises(ps.ProfileError, match="pseudo"):
            ps.create_profile(s, "C", brand_text="x" * 41)
        # mise à jour sans options : elles restent inchangées
        ps.update_profile(s, p, "A2", "", "", "", "")
        assert p.name == "A2" and p.brand_text == "@moncompte" and p.callouts
        ps.update_profile(s, p, "A2", "", "", "", "", teaser=True, callouts=False, layout="crop")
        assert p.teaser and not p.callouts and p.layout == "crop"


LEGACY_PROFILES = """
CREATE TABLE profiles (id INTEGER PRIMARY KEY, name VARCHAR(40) NOT NULL UNIQUE, niche VARCHAR(200) NOT NULL,
  language VARCHAR(40) NOT NULL, base_hashtags VARCHAR(200) NOT NULL, style VARCHAR(500) NOT NULL,
  created_at DATETIME NOT NULL);
INSERT INTO profiles VALUES (1,'Voiture','auto','français','#auto','ton direct','2026-01-01');
"""


def test_profiles_table_from_previous_version_gets_new_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_PROFILES)
    con.commit()
    con.close()
    f = make_session_factory(make_engine(path))
    with session_scope(f) as s:
        p = s.query(Profile).one()
        assert (p.name, p.base_hashtags) == ("Voiture", "#auto")  # données conservées
        assert (p.layout, p.teaser, p.callouts, p.credit, p.brand_text) == (
            "blur_fit", False, True, True, "",
        )  # fmt: skip


# ---------- Interface ----------


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    f = make_session_factory(make_engine(tmp_path / "t.sqlite"))
    c = TestClient(create_app(settings, f, background=False), follow_redirects=False)
    pwd = "mot-de-passe-solide"
    c.post("/register", data={"email": "a@b.co", "password": pwd, "confirm": pwd})
    return c, f


def test_profile_form_saves_checkboxes_and_layout(client):
    c, f = client
    r = c.post("/profiles", data={"name": "Divertissement", "layout": "crop", "teaser": "on",
                                  "credit": "on", "brand_text": "@moi"})  # fmt: skip
    assert r.status_code == 303
    with session_scope(f) as s:
        p = s.query(Profile).filter_by(name="Divertissement").one()
        assert (p.layout, p.teaser, p.callouts, p.credit, p.brand_text) == (
            "crop",
            True,
            False,
            True,
            "@moi",
        )
        pid = p.id
    c.post(
        f"/profiles/{pid}", data={"name": "Divertissement", "callouts": "on"}
    )  # tout le reste décoché
    with session_scope(f) as s:
        p = s.get(Profile, pid)
        assert (p.teaser, p.callouts, p.credit) == (False, True, False)
    assert c.post("/profiles", data={"name": "X", "layout": "zoom"}).status_code == 400
    page = c.get("/profiles").text
    assert "Bande-annonce d'ouverture" in page and "Remarques à l'écran" in page


# ---------- Orchestration ----------


def test_process_video_applies_profile_transformations(factory, tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    with session_scope(factory) as s:
        p = ps.create_profile(
            s, "Voiture", "auto", layout="crop", teaser=True, callouts=True, credit=True,
            brand_text="@moncompte",
        )  # fmt: skip
        v = Video(platform="youtube", external_id="a", url="u", title="T", status=V_PENDING,
                  channel_name="GMK", profile_id=p.id)  # fmt: skip
        s.add(v)
        s.flush()
        vid = v.id

    video_file = tmp_path / "source.mp4"
    video_file.write_bytes(b"x")
    monkeypatch.setattr(
        orch.download, "download", lambda *a, **k: Downloaded(video_file, None, 200.0)
    )
    monkeypatch.setattr(orch.transcribe, "transcribe", lambda *a, **k: (_words(), "fake"))
    monkeypatch.setattr(orch.render, "make_poster", lambda *a, **k: None)
    seen = {}

    def fake_render(
        src, out, start, end, words, hook, settings, work, layout="blur_fit", transform=None
    ):
        seen.update(layout=layout, transform=transform, start=start, end=end)

    monkeypatch.setattr(orch.render, "render_clip", fake_render)
    router = Rec(PAYLOAD)
    assert orch.process_video(factory, vid, settings, router) == 1  # type: ignore[arg-type]
    t = seen["transform"]
    assert seen["layout"] == "crop"
    assert t.credit == "Source : GMK" and t.brand == "@moncompte"
    assert t.callouts == [(30.0, "Wow")] and t.teaser is not None
    assert '"teaser"' in router.prompts[0]
    with session_scope(factory) as s:
        assert s.query(Clip).count() == 1


def test_process_video_without_options_applies_nothing(factory, tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    with session_scope(factory) as s:
        p = ps.create_profile(s, "Brut", callouts=False, credit=False)
        v = Video(platform="youtube", external_id="a", url="u", title="T", status=V_PENDING,
                  channel_name="GMK", profile_id=p.id)  # fmt: skip
        s.add(v)
        s.flush()
        vid = v.id
    video_file = tmp_path / "source.mp4"
    video_file.write_bytes(b"x")
    monkeypatch.setattr(
        orch.download, "download", lambda *a, **k: Downloaded(video_file, None, 200.0)
    )
    monkeypatch.setattr(orch.transcribe, "transcribe", lambda *a, **k: (_words(), "fake"))
    monkeypatch.setattr(orch.render, "make_poster", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(orch.render, "render_clip",
                        lambda *a, **k: seen.update(t=a[9] if len(a) > 9 else k.get("transform")))  # fmt: skip
    orch.process_video(factory, vid, settings, Rec(PAYLOAD))  # type: ignore[arg-type]
    t = seen["t"]
    assert t.credit == "" and t.brand == "" and t.callouts == [] and t.teaser is None
