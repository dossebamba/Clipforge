"""Sélection des meilleurs moments par le LLM, puis validation et recadrage sur les mots."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from clipforge.config import Settings
from clipforge.llm.router import LLMRouter
from clipforge.pipeline.transcribe import Word, group_sentences

log = logging.getLogger(__name__)

WINDOW_S = 25 * 60
OVERLAP_S = 2 * 60

SYSTEM = """Tu es un monteur expert de contenus courts viraux pour TikTok.
À partir d'une transcription horodatée, tu repères les moments qui fonctionnent seuls, hors contexte :
une accroche forte dès la première phrase, une histoire ou une idée complète, une émotion, un rire,
une révélation, une punchline, un conseil concret, une polémique. Tu évites les passages d'introduction,
de remerciements, de transition ou qui dépendent de ce qui précède.
Tu réponds uniquement en JSON valide."""

PROMPT = """Vidéo : « {title} » (chaîne : {channel}).
Transcription, une ligne par phrase au format [secondes] texte :

{transcript}

Propose jusqu'à {n} extraits, chacun entre {min_s} et {max_s} secondes, qui commencent au début
d'une phrase et finissent à la fin d'une phrase. Les extraits ne doivent pas se chevaucher.
Écris l'accroche, le titre, la description et les hashtags en {language}, quelle que soit la langue
de la transcription : tous les champs dans la même langue. Mets les hashtags uniquement dans
le champ "hashtags", jamais dans la description.

Réponds avec ce JSON exact :
{{"clips": [{{
  "start": <secondes>, "end": <secondes>, "score": <0-100, potentiel viral>,
  "hook": "<accroche affichée à l'écran, 8 mots max>",
  "title": "<titre court>",
  "description": "<légende TikTok engageante, 1 à 2 phrases>",
  "hashtags": ["#exemple", "..."],
  "reason": "<pourquoi ce moment fonctionne, 1 phrase>"
}}]}}"""


@dataclass
class Candidate:
    start: float
    end: float
    score: int
    hook: str = ""
    title: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    reason: str = ""


def _fmt_transcript(sentences: list[tuple[float, float, str]]) -> str:
    return "\n".join(f"[{s:.1f}] {t}" for s, _e, t in sentences)


def _windows(sentences: list[tuple[float, float, str]], duration: float):
    """Fenêtres de ~25 min avec recouvrement, pour les longues vidéos."""
    if duration <= WINDOW_S + OVERLAP_S:
        yield sentences
        return
    t = 0.0
    while t < duration:
        part = [s for s in sentences if s[0] >= t and s[0] < t + WINDOW_S + OVERLAP_S]
        if part:
            yield part
        t += WINDOW_S


def _clean_hashtags(tags: Any) -> list[str]:
    out: list[str] = []
    for t in tags if isinstance(tags, list) else []:
        tag = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"), flags=re.UNICODE)
        if len(tag) > 1 and tag.lower() not in [x.lower() for x in out]:
            out.append(tag)
    return out[:6]


def _strip_hashtags(text: str) -> str:
    """Retire les #hashtags collés dans la description (ils sont gérés à part)."""
    return re.sub(r"\s*#\w+", "", text, flags=re.UNICODE).strip()


def _to_candidate(raw: dict[str, Any]) -> Candidate | None:
    try:
        return Candidate(
            start=float(raw["start"]),
            end=float(raw["end"]),
            score=int(float(raw.get("score", 0))),
            hook=str(raw.get("hook", "")).strip(),
            title=str(raw.get("title", "")).strip(),
            description=_strip_hashtags(str(raw.get("description", ""))),
            hashtags=_clean_hashtags(raw.get("hashtags")),
            reason=str(raw.get("reason", "")).strip(),
        )
    except (KeyError, TypeError, ValueError):
        return None


def refine(
    cands: list[Candidate], words: list[Word], duration: float, settings: Settings
) -> list[Candidate]:
    """Cale les bornes sur les mots, impose la durée, retire les chevauchements, garde les meilleurs."""
    if not words:
        return []
    sentences = group_sentences(words)
    starts = [s for s, _e, _t in sentences]
    ends = [e for _s, e, _t in sentences]
    lo, hi = settings.clip_min_s, settings.clip_max_s

    kept: list[Candidate] = []
    for c in sorted(cands, key=lambda c: -c.score):
        if c.score < settings.min_clip_score or c.end <= c.start:
            continue
        # début : début de phrase le plus proche (tolérance 4 s)
        near = min(starts, key=lambda s: abs(s - c.start))
        start = near if abs(near - c.start) <= 4 else c.start
        # fin : fin de phrase la plus proche respectant la durée max
        end = c.end
        near_e = min(ends, key=lambda e: abs(e - c.end))
        if abs(near_e - c.end) <= 4 and near_e > start:
            end = near_e
        if end - start > hi:  # on coupe à la dernière fin de phrase qui tient
            fits = [e for e in ends if start + lo <= e <= start + hi]
            end = max(fits) if fits else start + hi
        if end - start < lo:
            end = start + lo
        end = min(end, duration) if duration else end
        if end - start < lo * 0.8:
            continue
        # chevauchement avec un clip déjà retenu
        if any(min(end, k.end) - max(start, k.start) > 0.2 * (end - start) for k in kept):
            continue
        c.start, c.end = round(start, 2), round(end, 2)
        kept.append(c)
        if len(kept) >= settings.max_clips_per_video:
            break
    return sorted(kept, key=lambda c: c.start)


def select_clips(
    router: LLMRouter,
    words: list[Word],
    duration: float,
    title: str,
    channel: str,
    settings: Settings,
) -> list[Candidate]:
    sentences = group_sentences(words)
    per_window = max(2, settings.max_clips_per_video)
    cands: list[Candidate] = []
    for part in _windows(sentences, duration):
        prompt = PROMPT.format(
            title=title,
            channel=channel or "inconnue",
            transcript=_fmt_transcript(part),
            language=settings.content_language,
            n=per_window,
            min_s=settings.clip_min_s,
            max_s=settings.clip_max_s,
        )
        data = router.generate_json(SYSTEM, prompt)
        raw_clips = data.get("clips", []) if isinstance(data, dict) else data
        cands += [c for r in raw_clips if isinstance(r, dict) and (c := _to_candidate(r))]
    return refine(cands, words, duration, settings)
