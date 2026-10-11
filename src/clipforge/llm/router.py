"""Routeur : essaie chaque fournisseur dans l'ordre jusqu'à obtenir un JSON valide."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from clipforge.config import Settings, get_settings
from clipforge.llm.providers import LLMError, Provider, build_providers

log = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json(text: str) -> Any:
    """Parse du JSON en tolérant les blocs ```json et le texte autour."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1), default=-1)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


class LLMRouter:
    def __init__(self, providers: list[Provider]):
        self.providers = providers

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LLMRouter:
        return cls(build_providers(settings or get_settings()))

    def generate_json(self, system: str, prompt: str) -> Any:
        if not self.providers:
            raise LLMError("Aucun fournisseur IA configuré (voir les clés dans .env)")
        errors: list[str] = []
        for provider in self.providers:
            for attempt in (1, 2):  # 2e essai seulement si le JSON est invalide
                try:
                    return parse_json(provider.complete(system, prompt))
                except json.JSONDecodeError:
                    errors.append(f"{provider.name}: JSON invalide (essai {attempt})")
                    continue
                except LLMError as e:
                    log.warning("Fournisseur %s en échec : %s", provider.name, e)
                    errors.append(str(e))
                    break
        raise LLMError("Tous les fournisseurs ont échoué : " + " | ".join(errors))
