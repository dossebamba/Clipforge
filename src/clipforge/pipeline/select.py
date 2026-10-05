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

# Fenêtres de ~10 min : une requête reste sous la limite de l'offre gratuite Groq (8 000 tokens/min)
WINDOW_S = 10 * 60
OVERLAP_S = 60

SYSTEM = """Tu es un monteur expert de contenus courts viraux pour TikTok.
À partir d'une transcription horodatée, tu repères les moments qui fonctionnent seuls, hors contexte :
une accroche forte dès la première phrase, une histoire ou une idée complète, une émotion, un rire,
une révélation, une punchline, un conseil concret, une polémique. Tu évites les passages d'introduction,
de remerciements, de transition ou qui dépendent de ce qui précède.
Tu réponds uniquement en JSON valide."""

PROMPT = """Vidéo : « {title} » (chaîne : {channel}).
{profile_block}Transcription, une ligne par phrase au format [secondes] texte :

{transcript}

Propose jusqu'à {n} extraits, chacun entre {min_s} et {max_s} secondes, qui commencent au début
d'une phrase et finissent à la fin d'une phrase. Les extraits ne doivent pas se chevaucher.
Écris l'accroche, le titre, la description et les hashtags en {language}, quelle que soit la langue
de la transcription : tous les champs dans la même langue. Mets les hashtags uniquement dans
le champ "hashtags", jamais dans la description.
{extra_rules}
Réponds avec ce JSON exact :
{{"clips": [{{
  "start": <secondes>, "end": <secondes>, "score": <0-100, potentiel viral>,
  "hook": "<accroche affichée à l'écran, 8 mots max>",
  "title": "<titre court>",
  "description": "<légende TikTok engageante, 1 à 2 phrases>",
  "hashtags": ["#exemple", "..."],
  "reason": "<pourquoi ce moment fonctionne, 1 phrase>"{extra_fields}
}}]}}"""

# Contraintes de validation des transformations demandées à l'IA
MAX_CALLOUTS = 2
CALLOUT_MAX_CHARS = 45
CALLOUT_AFTER_START_S = 4.0  # pas de remarque pendant l'accroche
CALLOUT_BEFORE_END_S = 3.0
CALLOUT_SPACING_S = 5.0
TEASER_MIN_S, TEASER_MAX_S = 2.5, 7.0
TEASER_AFTER_START_S = 8.0  # le passage rejoué doit venir d'ailleurs que de l'ouverture

CALLOUT_RULES = (
    'Pour chaque extrait, ajoute "callouts" : 1 ou 2 courtes remarques affichées à l\'écran '
    f"({CALLOUT_MAX_CHARS} caractères max chacune) qui apportent un plus au spectateur : réaction, "
    "contexte, précision utile ou question. Ne répète pas mot pour mot ce qui est dit. "
    '"at" = instant en secondes dans la vidéo (même repère que la transcription), au moins '
    f"{CALLOUT_AFTER_START_S:.0f} s après le début de l'extrait et {CALLOUT_BEFORE_END_S:.0f} s "
    f"avant sa fin, et {CALLOUT_SPACING_S:.0f} s d'écart entre deux remarques.\n"
)
TEASER_RULES = (
    'Pour chaque extrait, ajoute "teaser" : le passage le plus fort de l\'extrait '
    f"({TEASER_MIN_S:g} à {TEASER_MAX_S:g} secondes, de début à fin de phrase), qui sera rejoué en "
    f"ouverture comme bande-annonce. Il doit commencer au moins {TEASER_AFTER_START_S:.0f} secondes "
    "après le début de l'extrait.\n"
)


@dataclass
class Style:
    """Ce que le profil (compte TikTok) impose à l'IA et au montage."""

    language: str = ""
    niche: str = ""
    instructions: str = ""
    hashtags: list[str] = field(default_factory=list)  # ajoutés par le code, pas par l'IA
    # transformations du montage
    layout: str = ""
    callouts: bool = False
    teaser: bool = False
    brand_text: str = ""


def extras(style: Style) -> tuple[str, str]:
    """(règles ajoutées au prompt, champs ajoutés au JSON demandé) selon les options du profil."""
    rules, fields = "", ""
    if style.callouts:
        rules += CALLOUT_RULES
        # accolades simples : ces chaînes sont des valeurs de .format(), pas le modèle lui-même
        fields += ',\n  "callouts": [{"at": <secondes>, "text": "<remarque>"}]'
    if style.teaser:
        rules += TEASER_RULES
        fields += ',\n  "teaser": {"start": <secondes>, "end": <secondes>}'
    return rules, fields


def profile_block(style: Style) -> str:
    lines = []
    if style.niche:
        lines.append(
            f"Compte TikTok cible : thème « {style.niche} ». Privilégie les moments qui intéressent "
            "ce public et ignore ceux qui sont hors sujet."
        )
    if style.instructions:
        lines.append(f"Consignes de ton pour les textes : {style.instructions}")
    return "\n".join(lines) + "\n" if lines else ""


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
    callouts: list[tuple[float, str]] = field(default_factory=list)  # (instant absolu, texte)
    teaser: tuple[float, float] | None = None  # passage (absolu) rejoué en ouverture


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


def _parse_callouts(raw: Any) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            text = re.sub(r"\s+", " ", _strip_hashtags(str(item["text"]))).strip()
            out.append((float(item["at"]), text))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _parse_teaser(raw: Any) -> tuple[float, float] | None:
    try:
        return float(raw["start"]), float(raw["end"])
    except (KeyError, TypeError, ValueError):
        return None


def _to_candidate(raw: dict[str, Any]) -> Candidate | None:
    try:
        return Candidate(
            callouts=_parse_callouts(raw.get("callouts")),
            teaser=_parse_teaser(raw.get("teaser")),
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


def validate_callouts(
    callouts: list[tuple[float, str]], start: float, end: float
) -> list[tuple[float, str]]:
    """Garde au plus 2 remarques courtes, bien placées dans le clip et espacées."""
    kept: list[tuple[float, str]] = []
    for at, text in sorted(callouts):
        if not text or len(text) > CALLOUT_MAX_CHARS:
            continue
        if not (start + CALLOUT_AFTER_START_S <= at <= end - CALLOUT_BEFORE_END_S):
            continue
        if kept and at - kept[-1][0] < CALLOUT_SPACING_S:
            continue
        kept.append((round(at, 2), text))
    return kept[:MAX_CALLOUTS]


def validate_teaser(
    teaser: tuple[float, float] | None,
    start: float,
    end: float,
    starts: list[float],
    ends: list[float],
    word_starts: list[float] | None = None,
    word_ends: list[float] | None = None,
) -> tuple[float, float] | None:
    """Cale la bande-annonce sur des bornes de phrase (à défaut de mot), impose 2,5 à 7 s,
    et la tire hors de l'ouverture du clip."""
    if not teaser or not starts or not ends:
        return None
    t0, t1 = teaser
    # début : début de phrase proche (2 s), sinon début de mot le plus proche
    near = min(starts, key=lambda s: abs(s - t0))
    if abs(near - t0) <= 2:
        t0 = near
    elif word_starts:
        t0 = min(word_starts, key=lambda s: abs(s - t0))
    if t0 < start + TEASER_AFTER_START_S or t0 >= end:
        return None
    # fin : fin de phrase dans la fenêtre autorisée, sinon fin de mot la plus proche de la demande
    limit = min(t0 + TEASER_MAX_S, end)
    lo = t0 + TEASER_MIN_S
    sentence_fits = [e for e in ends if lo <= e <= min(t1 + 0.5, limit)]
    if sentence_fits:
        t1 = max(sentence_fits)
    else:
        word_fits = [e for e in (word_ends or []) if lo <= e <= limit]
        if not word_fits:
            return None
        t1 = min(word_fits, key=lambda e: abs(e - t1))
    return (round(t0, 2), round(t1, 2))


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
        c.callouts = validate_callouts(c.callouts, c.start, c.end)
        c.teaser = validate_teaser(
            c.teaser, c.start, c.end, starts, ends, [w.start for w in words], [w.end for w in words]
        )
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
    style: Style | None = None,
) -> list[Candidate]:
    style = style or Style()
    sentences = group_sentences(words)
    per_window = max(2, settings.max_clips_per_video)
    cands: list[Candidate] = []
    extra_rules, extra_fields = extras(style)
    for part in _windows(sentences, duration):
        prompt = PROMPT.format(
            title=title,
            channel=channel or "inconnue",
            profile_block=profile_block(style),
            extra_rules=extra_rules,
            extra_fields=extra_fields,
            transcript=_fmt_transcript(part),
            language=style.language or settings.content_language,
            n=per_window,
            min_s=settings.clip_min_s,
            max_s=settings.clip_max_s,
        )
        data = router.generate_json(SYSTEM, prompt)
        raw_clips = data.get("clips", []) if isinstance(data, dict) else data
        cands += [c for r in raw_clips if isinstance(r, dict) and (c := _to_candidate(r))]
    refined = refine(cands, words, duration, settings)
    for c in refined:  # on n'applique jamais une transformation que le profil n'a pas demandée
        if not style.callouts:
            c.callouts = []
        if not style.teaser:
            c.teaser = None
    return refined
