from clipforge.config import Settings
from clipforge.pipeline.render import ass_time, build_ass, build_filtergraph
from clipforge.pipeline.transcribe import Word

S = Settings()


def test_ass_time():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(61.5) == "0:01:01.50"
    assert ass_time(3661.04) == "1:01:01.04"
    assert ass_time(-3) == "0:00:00.00"


def _words():
    return [
        Word(t, i * 0.5, i * 0.5 + 0.4)
        for i, t in enumerate(["un", "deux", "trois", "quatre", "cinq"])
    ]


def test_ass_has_hook_and_one_event_per_word_relative_to_clip():
    ass = build_ass(_words(), clip_start=0.5, clip_end=2.5, hook="Regarde ça", settings=S)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert any(",Hook," in e and "REGARDE ÇA" in e for e in events)
    subs = [e for e in events if ",Sub," in e]
    assert len(subs) == 4  # mots 1 à 4 (deux, trois, quatre, cinq) dans [0.5, 2.5]
    assert subs[0].split(",")[1] == "0:00:00.00"  # relatif au début du clip
    assert "UN" not in " ".join(subs)  # mot avant le clip exclu


def test_ass_escapes_braces():
    ass = build_ass([Word("a{b}", 0, 1)], 0, 2, "", S)
    assert "{b}" not in ass.split("[Events]")[1]


def test_filtergraph_variants():
    blur = build_filtergraph(S, "blur_fit")
    assert "boxblur" in blur and "overlay" in blur and "subtitles=sub.ass" in blur
    assert "C\\:/Windows/Fonts" in blur
    crop = build_filtergraph(S, "crop")
    assert "crop=ih*9/16:ih" in crop and "boxblur" not in crop
