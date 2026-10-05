"""Fournisseurs LLM (Gemini, Groq, OpenRouter) derrière une interface commune."""

from __future__ import annotations

import logging
import re
import time
from typing import Protocol

import httpx

from clipforge.config import Settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Erreur d'un fournisseur (quota, réseau, réponse invalide)."""


class Provider(Protocol):
    name: str

    def complete(self, system: str, prompt: str) -> str:
        """Retourne le texte brut (censé être du JSON) de la réponse."""
        ...


RETRY_STATUS = {429, 500, 502, 503, 504}  # surcharge ou quota par minute : ça passe en réessayant
SERVER_RETRY_STATUS = {
    500,
    502,
    503,
    504,
}  # sans 429 : un quota épuisé ne se règle pas en attendant
MAX_ATTEMPTS = 3
MAX_WAIT_S = 62  # une limite « par minute » se libère en moins d'une minute
MAX_OUTPUT_TOKENS = 2200  # plafonne la réponse : sinon Groq réserve une très grosse réponse
RETRY_IN = re.compile(r"try again in (\d+(?:\.\d+)?)\s*(ms|s|m)\b", re.IGNORECASE)


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration_s(text: str) -> float:
    """'43.095s' -> 43.095 ; '7m12s' -> 432 ; '350ms' -> 0.35 ; '' -> 0."""
    return sum(float(v) * _UNIT_S[u] for v, u in _DURATION.findall(text or ""))


def _raise_for(resp: httpx.Response, name: str) -> None:
    if resp.status_code >= 400:
        # Ne jamais inclure la clé : on ne garde que le code et un extrait du message
        raise LLMError(f"{name}: HTTP {resp.status_code} {resp.text[:200]}")


def _wait_time(resp: httpx.Response | None, attempt: int) -> float:
    """Délai avant un nouvel essai : Retry-After, sinon le délai écrit dans le message
    (« Please try again in 8.2s »), sinon 3 s, 12 s..."""
    if resp is not None:
        try:
            return min(float(resp.headers["retry-after"]), MAX_WAIT_S)
        except (KeyError, ValueError):
            pass
        if m := RETRY_IN.search(resp.text):
            value, unit = float(m.group(1)), m.group(2).lower()
            seconds = value / 1000 if unit == "ms" else value * 60 if unit == "m" else value
            return min(seconds + 1.0, MAX_WAIT_S)
    return min(3.0 * attempt**2, MAX_WAIT_S)


def _post(
    client: httpx.Client,
    url: str,
    name: str,
    retry_statuses: set[int] = RETRY_STATUS,
    **kwargs,
) -> httpx.Response:
    """POST avec nouvelles tentatives sur les erreurs temporaires (503, 429...)."""
    resp: httpx.Response | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.post(url, **kwargs)
        except httpx.HTTPError as e:
            if attempt == MAX_ATTEMPTS:
                raise LLMError(f"{name}: {type(e).__name__}") from e
            time.sleep(_wait_time(None, attempt))
            continue
        if resp.status_code not in retry_statuses or attempt == MAX_ATTEMPTS:
            return resp
        log.info("%s : HTTP %s, nouvel essai %d/%d", name, resp.status_code, attempt, MAX_ATTEMPTS)
        time.sleep(_wait_time(resp, attempt))
    raise LLMError(f"{name}: échec")  # pragma: no cover


class GeminiProvider:
    """Essaie plusieurs modèles dans l'ordre : le quota gratuit est compté PAR MODÈLE, donc quand
    le modèle principal est épuisé, un modèle « lite » prend le relais."""

    name = "gemini"

    def __init__(self, api_key: str, models: str | list[str], client: httpx.Client | None = None):
        self.api_key = api_key
        self.models = [models] if isinstance(models, str) else list(models)
        self.client = client or httpx.Client(timeout=120)

    def complete(self, system: str, prompt: str) -> str:
        errors: list[str] = []
        for model in self.models:
            try:
                return self._complete_with(model, system, prompt)
            except LLMError as e:
                log.warning("Gemini (%s) en échec : %s", model, e)
                errors.append(str(e))
        raise LLMError(" | ".join(errors))

    def _complete_with(self, model: str, system: str, prompt: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.4,
                "maxOutputTokens": 8192,
            },
        }
        resp = _post(
            self.client,
            url,
            f"gemini/{model}",
            SERVER_RETRY_STATUS,
            json=body,
            headers={"x-goog-api-key": self.api_key},
        )
        _raise_for(resp, f"gemini/{model}")
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError("gemini: réponse inattendue") from e


class OpenAICompatProvider:
    """Groq et OpenRouter exposent l'API chat/completions d'OpenAI."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
    ):
        self.name, self.base_url, self.api_key, self.model = name, base_url, api_key, model
        self.client = client or httpx.Client(timeout=120)
        # Limite « tokens par minute » telle que le fournisseur la décrit dans ses en-têtes
        self._tokens_left: int | None = None
        self._resets_at = 0.0

    def _pace(self, system: str, prompt: str) -> None:
        """Attend la remise à zéro de la limite par minute plutôt que d'envoyer une requête
        qui sera refusée. Le fournisseur compte l'entrée PLUS le plafond de réponse réservé,
        même si la réponse réelle est bien plus courte."""
        if self._tokens_left is None:
            return
        needed = (len(system) + len(prompt)) // 3 + MAX_OUTPUT_TOKENS  # estimation prudente
        now = time.monotonic()
        if needed > self._tokens_left and now < self._resets_at:
            wait = min(self._resets_at - now, MAX_WAIT_S) + 0.5
            log.info("%s : pause de %.0f s (limite de tokens par minute)", self.name, wait)
            time.sleep(wait)
        self._tokens_left = None  # les chiffres sont périmés : la réponse suivante les remplace

    def _learn(self, resp: httpx.Response) -> None:
        try:
            self._tokens_left = int(resp.headers["x-ratelimit-remaining-tokens"])
        except (KeyError, ValueError):
            return
        reset = parse_duration_s(resp.headers.get("x-ratelimit-reset-tokens", ""))
        self._resets_at = time.monotonic() + reset

    def complete(self, system: str, prompt: str) -> str:
        self._pace(system, prompt)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        if "gpt-oss" in self.model:
            body["reasoning_effort"] = "low"  # moins de tokens « de réflexion » consommés
        resp = _post(
            self.client,
            f"{self.base_url}/chat/completions",
            self.name,
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        self._learn(resp)
        _raise_for(resp, self.name)
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{self.name}: réponse inattendue") from e


def build_providers(settings: Settings) -> list[Provider]:
    """Construit les fournisseurs dans l'ordre configuré, en ignorant ceux sans clé."""
    out: list[Provider] = []
    for name in settings.provider_order:
        if name == "gemini" and settings.gemini_api_key:
            models = [m.strip() for m in settings.gemini_model.split(",") if m.strip()]
            out.append(GeminiProvider(settings.gemini_api_key, models))
        elif name == "groq" and settings.groq_api_key:
            out.append(
                OpenAICompatProvider(
                    "groq",
                    "https://api.groq.com/openai/v1",
                    settings.groq_api_key,
                    settings.groq_model,
                )
            )
        elif name == "openrouter" and settings.openrouter_api_key:
            out.append(
                OpenAICompatProvider(
                    "openrouter",
                    "https://openrouter.ai/api/v1",
                    settings.openrouter_api_key,
                    settings.openrouter_model,
                )
            )
    return out
