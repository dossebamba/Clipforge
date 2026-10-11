from clipforge.config import Settings
from clipforge.pipeline.describe import build_caption
from clipforge.pipeline.select import Candidate, _clean_hashtags, _to_candidate, refine
from clipforge.pipeline.transcribe import Word, group_sentences


def _words(total=200):
    """Un mot par seconde, une phrase (point) toutes les 10 secondes."""
    return [Word("mot." if (i + 1) % 10 == 0 else "mot", float(i), i + 0.9) for i in range(total)]


S = Settings(clip_min_s=20, clip_max_s=60, min_clip_score=60, max_clips_per_video=3)


def test_group_sentences_on_punctuation():
    sents = group_sentences(_words(30))
    assert len(sents) == 3
    assert sents[0][0] == 0.0 and sents[0][1] == 9.9


def test_refine_snaps_to_sentence_bounds():
    out = refine([Candidate(start=21.5, end=52.0, score=90)], _words(), 200, S)
    assert len(out) == 1
    assert out[0].start == 20.0  # début de phrase le plus proche
    assert out[0].end == 49.9  # fin de phrase la plus proche


def test_refine_enforces_max_and_min_duration():
    long = refine([Candidate(start=0, end=150, score=90)], _words(), 200, S)[0]
    assert long.end - long.start <= 60
    short = refine([Candidate(start=100, end=105, score=90)], _words(), 200, S)[0]
    assert short.end - short.start >= 20


def test_refine_drops_low_score_and_overlaps_and_caps():
    cands = [
        Candidate(start=0, end=40, score=95),
        Candidate(start=10, end=45, score=80),  # chevauche le meilleur
        Candidate(start=100, end=130, score=50),  # score trop bas
        Candidate(start=60, end=90, score=70),
    ]
    out = refine(cands, _words(), 200, S)
    assert [(c.start, c.score) for c in out] == [(0.0, 95), (60.0, 70)]


def test_refine_without_words_returns_nothing():
    assert refine([Candidate(0, 30, 90)], [], 100, S) == []


def test_candidate_parsing_is_tolerant():
    assert _to_candidate({"start": "5", "end": "40", "score": "88.0"}).score == 88
    assert _to_candidate({"nope": 1}) is None
    assert _clean_hashtags(["foot", "#But!", "foot", "a b"]) == ["#foot", "#But", "#ab"]


def test_hashtags_removed_from_description():
    c = _to_candidate(
        {
            "start": 0,
            "end": 30,
            "score": 80,
            "description": "Super moment #foot #but",
            "hashtags": ["#foot"],
        }
    )
    assert c.description == "Super moment"
    assert c.hashtags == ["#foot"]


def test_caption():
    txt = build_caption("Incroyable.", ["#a", "#b"], "Chaîne")
    assert txt.splitlines()[0] == "Incroyable."
    assert "#a #b" in txt and "Source : Chaîne" in txt
