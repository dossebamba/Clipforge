"""Fournisseurs LLM (Gemini, Groq, OpenRouter) derrière une interface commune."""

from __future__ import annotations

import logging
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
MAX_ATTEMPTS = 3
MAX_WAIT_S = 25
MAX_OUTPUT_TOKENS = 3000  # plafonne la réponse : sinon Groq réserve une très grosse réponse


def _raise_for(resp: httpx.Response, name: str) -> None:
    if resp.status_code >= 400:
        # Ne jamais inclure la clé : on ne garde que le code et un extrait du message
        raise LLMError(f"{name}: HTTP {resp.status_code} {resp.text[:200]}")


def _wait_time(resp: httpx.Response | None, attempt: int) -> float:
    """Délai avant un nouvel essai : Retry-After si fourni, sinon 3 s, 9 s..."""
    if resp is not None:
        try:
            return min(float(resp.headers["retry-after"]), MAX_WAIT_S)
        except (KeyError, ValueError):
            pass
    return min(3.0 * attempt**2, MAX_WAIT_S)


def _post(client: httpx.Client, url: str, name: str, **kwargs) -> httpx.Response:
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
        if resp.status_code not in RETRY_STATUS or attempt == MAX_ATTEMPTS:
            return resp
        log.info("%s : HTTP %s, nouvel essai %d/%d", name, resp.status_code, attempt, MAX_ATTEMPTS)
        time.sleep(_wait_time(resp, attempt))
    raise LLMError(f"{name}: échec")  # pragma: no cover


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None):
        self.api_key, self.model = api_key, model
        self.client = client or httpx.Client(timeout=120)

    def complete(self, system: str, prompt: str) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        )
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
            self.client, url, self.name, json=body, headers={"x-goog-api-key": self.api_key}
        )
        _raise_for(resp, self.name)
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

    def complete(self, system: str, prompt: str) -> str:
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
            out.append(GeminiProvider(settings.gemini_api_key, settings.gemini_model))
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
