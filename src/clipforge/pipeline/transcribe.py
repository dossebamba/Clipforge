"""Transcription avec timing mot par mot.

Ordre : sous-titres YouTube (json3) > Whisper sur Groq (gratuit, cloud) > faster-whisper local (CPU).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from clipforge.config import Settings
from clipforge.pipeline.media import run_ffmpeg

log = logging.getLogger(__name__)

CHUNK_S = 15 * 60  # morceaux audio envoyés à Groq (limite de taille par fichier)


@dataclass
class Word:
    text: str
    start: float
    end: float


def parse_json3(path: Path) -> list[Word]:
    """Sous-titres automatiques YouTube : chaque événement porte des segments avec décalage en ms."""
    data = json.loads(path.read_text(encoding="utf-8"))
    words: list[Word] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        base = ev.get("tStartMs", 0)
        end_ev = base + ev.get("dDurationMs", 0)
        for i, seg in enumerate(segs):
            text = (seg.get("utf8") or "").strip()
            if not text or text == "\n":
                continue
            start = (base + seg.get("tOffsetMs", 0)) / 1000
            nxt = segs[i + 1].get("tOffsetMs") if i + 1 < len(segs) else None
            end = (base + nxt) / 1000 if nxt is not None else end_ev / 1000
            words.append(Word(text, start, max(end, start + 0.05)))
    # les sous-titres auto se chevauchent : on borne chaque mot au début du suivant
    for a, b in zip(words, words[1:], strict=False):
        if a.end > b.start:
            a.end = max(b.start, a.start + 0.05)
    return words


def _extract_audio_chunks(video: Path, work: Path) -> list[tuple[Path, float]]:
    pattern = work / "audio_%03d.mp3"
    run_ffmpeg(
        [
            "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
            "-f", "segment", "-segment_time", str(CHUNK_S), "-reset_timestamps", "1",
            str(pattern),
        ]
    )  # fmt: skip
    return [(p, i * CHUNK_S) for i, p in enumerate(sorted(work.glob("audio_*.mp3")))]


def transcribe_groq(
    video: Path, work: Path, settings: Settings, client: httpx.Client | None = None
) -> list[Word]:
    client = client or httpx.Client(timeout=300)
    words: list[Word] = []
    for chunk, offset in _extract_audio_chunks(video, work):
        with chunk.open("rb") as f:
            resp = client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                data={
                    "model": settings.groq_whisper_model,
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": "word",
                },
                files={"file": (chunk.name, f, "audio/mpeg")},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Groq Whisper : HTTP {resp.status_code} {resp.text[:200]}")
        for w in resp.json().get("words", []):
            words.append(Word(w["word"].strip(), w["start"] + offset, w["end"] + offset))
    return words


def transcribe_local(video: Path, settings: Settings) -> list[Word]:
    from faster_whisper import WhisperModel

    model = WhisperModel(settings.local_whisper_model, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video), word_timestamps=True, vad_filter=True)
    return [
        Word(w.word.strip(), w.start, w.end)
        for seg in segments
        for w in (seg.words or [])
        if w.word.strip()
    ]


def transcribe(
    video: Path, subtitle: Path | None, work: Path, settings: Settings
) -> tuple[list[Word], str]:
    """Renvoie (mots, méthode utilisée)."""
    if subtitle:
        try:
            words = parse_json3(subtitle)
            if len(words) > 20:
                return words, "youtube-subs"
        except (OSError, ValueError, KeyError) as e:
            log.warning("Sous-titres YouTube inutilisables : %s", e)
    if settings.groq_api_key:
        try:
            words = transcribe_groq(video, work, settings)
            if words:
                return words, "groq-whisper"
        except Exception as e:
            log.warning("Whisper Groq en échec, repli sur le local : %s", e)
    return transcribe_local(video, settings), "local-whisper"


def group_sentences(
    words: list[Word], gap_s: float = 0.8, max_len_s: float = 12.0
) -> list[tuple[float, float, str]]:
    """Regroupe les mots en phrases (pause longue ou ponctuation finale) : [(start, end, texte)]."""
    out: list[tuple[float, float, str]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (
            w.start - cur[-1].end > gap_s
            or cur[-1].text.endswith((".", "?", "!"))
            or w.end - cur[0].start > max_len_s
        ):
            out.append((cur[0].start, cur[-1].end, " ".join(x.text for x in cur)))
            cur = []
        cur.append(w)
    if cur:
        out.append((cur[0].start, cur[-1].end, " ".join(x.text for x in cur)))
    return out
