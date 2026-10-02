"""Fournisseurs LLM (Gemini, Groq, OpenRouter) derrière une interface commune."""

from __future__ import annotations

from typing import Protocol

import httpx

from clipforge.config import Settings


class LLMError(Exception):
    """Erreur d'un fournisseur (quota, réseau, réponse invalide)."""


class Provider(Protocol):
    name: str

    def complete(self, system: str, prompt: str) -> str:
        """Retourne le texte brut (censé être du JSON) de la réponse."""
        ...


def _raise_for(resp: httpx.Response, name: str) -> None:
    if resp.status_code >= 400:
        # Ne jamais inclure la clé : on ne garde que le code et un extrait du message
        raise LLMError(f"{name}: HTTP {resp.status_code} {resp.text[:200]}")


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
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
        }
        try:
            resp = self.client.post(url, json=body, headers={"x-goog-api-key": self.api_key})
        except httpx.HTTPError as e:
            raise LLMError(f"gemini: {type(e).__name__}") from e
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
        }
        try:
            resp = self.client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name}: {type(e).__name__}") from e
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
