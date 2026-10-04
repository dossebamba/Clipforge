import sqlite3

import pytest

from clipforge import profiles as ps
from clipforge.config import Settings
from clipforge.db.models import (
    C_POSTED,
    C_READY,
    V_DONE,
    V_PENDING,
    Clip,
    Profile,
    Source,
    Video,
)
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.pipeline import orchestrator as orch
from clipforge.pipeline.download import Downloaded
from clipforge.pipeline.select import Style, profile_block, select_clips
from clipforge.pipeline.transcribe import Word
from clipforge.sources import service
from clipforge.sources.base import RemoteVideo


@pytest.fixture
def factory(tmp_path):
    return make_session_factory(make_engine(tmp_path / "t.sqlite"))


# ---------- Hashtags ----------


def test_parse_and_merge_hashtags():
    assert ps.parse_hashtags("voiture, #Tuning auto;  #voiture") == ["#voiture", "#Tuning", "#auto"]
    assert ps.parse_hashtags("a b c d e f g", limit=3) == ["#a", "#b", "#c"]
    assert ps.parse_hashtags("###  !!") == []
    assert ps.merge_hashtags(["#a", "#b"], ["#B", "#c", "#d"], limit=4) == ["#a", "#b", "#c", "#d"]


# ---------- Règles métier ----------


def test_fresh_database_has_default_profile(factory):
    with session_scope(factory) as s:
        assert [p.name for p in s.query(Profile)] == ["Général"]


def test_create_update_validation(factory):
    with session_scope(factory) as s:
        p = ps.create_profile(
            s, "Voiture", "automobile", "français", "voiture, #auto", "ton direct"
        )
        assert p.base_hashtags == "#voiture #auto" and p.hashtag_list == ["#voiture", "#auto"]
        with pytest.raises(ps.ProfileError, match="obligatoire"):
            ps.create_profile(s, "   ")
        with pytest.raises(ps.ProfileError, match="déjà"):
            ps.create_profile(s, "voiture")  # même nom, casse différente
        with pytest.raises(ps.ProfileError, match="trop long"):
            ps.create_profile(s, "x" * 41)
        other = ps.create_profile(s, "Dev")
        with pytest.raises(ps.ProfileError, match="déjà"):
            ps.update_profile(s, other, "VOITURE", "", "français", "", "")
        ps.update_profile(s, other, "Dev perso", "code", "", "", "")  # langue vide -> défaut
        assert other.name == "Dev perso" and other.language == "français"


def test_delete_rules(factory):
    with session_scope(factory) as s:
        general = s.query(Profile).one()
        with pytest.raises(ps.ProfileError, match="dernier"):
            ps.delete_profile(s, general)
        cars = ps.create_profile(s, "Voiture")
        s.add(Video(platform="youtube", external_id="a", url="u", profile_id=cars.id))
        s.flush()
        with pytest.raises(ps.ProfileError, match="contient encore"):
            ps.delete_profile(s, cars)
        empty = ps.create_profile(s, "Vide")
        ps.delete_profile(s, empty)
        s.flush()
        assert s.query(Profile).filter_by(name="Vide").count() == 0


def test_move_video_refused_once_a_clip_is_posted(factory):
    with session_scope(factory) as s:
        a, b = s.query(Profile).one(), ps.create_profile(s, "Autre")
        v = Video(platform="youtube", external_id="a", url="u", status=V_DONE, profile_id=a.id)
        v.clips = [Clip(file_path="x", start_s=0, end_s=30, status=C_READY)]
        s.add(v)
        s.flush()
        ps.move_video(s, v, b)
        assert v.profile_id == b.id
        v.clips[0].status = C_POSTED
        with pytest.raises(ps.ProfileError, match="déjà posté"):
            ps.move_video(s, v, a)
        assert v.profile_id == b.id


# ---------- Migration d'une base existante ----------

LEGACY_SQL = """
CREATE TABLE sources (
  id INTEGER PRIMARY KEY, platform VARCHAR(16) NOT NULL, identifier VARCHAR(300) NOT NULL,
  display_name VARCHAR(200) NOT NULL, enabled BOOLEAN NOT NULL, initial_done BOOLEAN NOT NULL,
  min_duration_s INTEGER NOT NULL, max_duration_s INTEGER NOT NULL, ignore_keywords TEXT NOT NULL,
  last_checked DATETIME, last_error TEXT NOT NULL, created_at DATETIME NOT NULL);
CREATE TABLE videos (
  id INTEGER PRIMARY KEY, source_id INTEGER, platform VARCHAR(16) NOT NULL,
  external_id VARCHAR(64) NOT NULL UNIQUE, url VARCHAR(500) NOT NULL, title VARCHAR(500) NOT NULL,
  channel_name VARCHAR(200) NOT NULL, thumbnail VARCHAR(500) NOT NULL, duration_s INTEGER NOT NULL,
  priority INTEGER NOT NULL, status VARCHAR(16) NOT NULL, stage VARCHAR(32) NOT NULL,
  error TEXT NOT NULL, created_at DATETIME NOT NULL, processed_at DATETIME);
INSERT INTO sources VALUES (1,'youtube','@abc','ABC',1,1,0,0,'',NULL,'','2026-01-01');
INSERT INTO videos VALUES (1,1,'youtube','v1','u','Titre','ABC','',60,0,'done','','','2026-01-01',NULL);
INSERT INTO videos VALUES (2,NULL,'twitch','v2','u','Manuel','','',60,10,'pending','','','2026-01-02',NULL);
"""


def test_legacy_database_is_migrated_without_losing_data(tmp_path):
    path = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SQL)
    con.commit()
    con.close()

    for _ in range(2):  # la 2e ouverture doit être sans effet (idempotence)
        factory = make_session_factory(make_engine(path))
        with session_scope(factory) as s:
            profiles = s.query(Profile).all()
            assert [p.name for p in profiles] == ["Général"]
            gid = profiles[0].id
            assert s.get(Source, 1).profile_id == gid and s.get(Source, 1).display_name == "ABC"
            assert {v.profile_id for v in s.query(Video)} == {gid}
            assert s.query(Video).count() == 2


# ---------- Sources et profils ----------


def test_sources_and_manual_links_carry_their_profile(factory, monkeypatch):
    settings = Settings(initial_import_count=5)
    vids = [RemoteVideo("youtube", f"id{i}", "u", f"T{i}", "C", duration_s=300) for i in range(2)]
    monkeypatch.setattr(service, "_fetch", lambda s, st: ("C", vids))
    monkeypatch.setattr(
        service.manual,
        "resolve_url",
        lambda url: RemoteVideo("youtube", "man", url, "M", "C", duration_s=300),
    )
    with session_scope(factory) as s:
        general = s.query(Profile).one()
        cars = ps.create_profile(s, "Voiture")
        service.add_source(s, "youtube", "@a", settings, profile_id=cars.id)
        s.flush()
        assert {v.profile_id for v in s.query(Video)} == {cars.id}
        # sans profil précisé : profil par défaut (le premier)
        src2 = service.add_source(s, "youtube", "@b", settings)
        assert src2.profile_id == general.id
        assert service.add_manual_url(s, "https://y/man", cars.id).profile_id == cars.id


# ---------- Le profil guide l'IA et la légende ----------


class RecordingRouter:
    def __init__(self, payload):
        self.payload, self.prompts = payload, []

    def generate_json(self, system, prompt):
        self.prompts.append(prompt)
        return self.payload


def _words(n=120):
    return [Word("mot." if (i + 1) % 10 == 0 else "mot", float(i), i + 0.9) for i in range(n)]


CLIP = {"start": 10, "end": 45, "score": 90, "hook": "H", "title": "T", "description": "D",
        "hashtags": ["#ia", "#voiture"], "reason": "r"}  # fmt: skip


def test_prompt_contains_profile_theme_style_and_language():
    router = RecordingRouter({"clips": [CLIP]})
    style = Style(language="anglais", niche="automobile, tuning", instructions="ton direct")
    select_clips(router, _words(), 120, "Titre", "Chaîne", Settings(), style)
    prompt = router.prompts[0]
    assert "automobile, tuning" in prompt and "ton direct" in prompt and "anglais" in prompt


def test_empty_profile_adds_no_block():
    assert profile_block(Style()) == ""
    router = RecordingRouter({"clips": [CLIP]})
    select_clips(router, _words(), 120, "Titre", "Chaîne", Settings(content_language="français"))
    assert "français" in router.prompts[0] and "Compte TikTok cible" not in router.prompts[0]


def test_process_video_uses_profile_for_prompt_and_caption(factory, tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    with session_scope(factory) as s:
        p = ps.create_profile(
            s, "Voiture", "automobile", "français", "#voiture #auto", "ton direct"
        )
        v = Video(platform="youtube", external_id="a", url="u", title="T", status=V_PENDING,
                  channel_name="GMK", profile_id=p.id)  # fmt: skip
        s.add(v)
        s.flush()
        vid = v.id

    fake_video = tmp_path / "source.mp4"
    fake_video.write_bytes(b"x")
    monkeypatch.setattr(
        orch.download, "download", lambda *a, **k: Downloaded(fake_video, None, 120.0)
    )
    monkeypatch.setattr(orch.transcribe, "transcribe", lambda *a, **k: (_words(), "fake"))
    monkeypatch.setattr(orch.render, "render_clip", lambda *a, **k: None)
    monkeypatch.setattr(orch.render, "make_poster", lambda *a, **k: None)

    router = RecordingRouter({"clips": [CLIP]})
    assert orch.process_video(factory, vid, settings, router) == 1  # type: ignore[arg-type]
    assert "automobile" in router.prompts[0]
    with session_scope(factory) as s:
        caption = s.query(Clip).one().description
    # hashtags du profil d'abord, puis ceux de l'IA, sans doublon (#voiture n'apparaît qu'une fois)
    assert "#voiture #auto #ia" in caption
    assert caption.count("#voiture") == 1
    assert "Source : GMK" in caption
