"""Configuration centrale, lue depuis les variables d'environnement / le fichier .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Fournisseurs IA (clés optionnelles : un fournisseur sans clé est ignoré)
    gemini_api_key: str = ""
    groq_api_key: str = ""
    openrouter_api_key: str = ""
    llm_providers: str = "gemini,groq,openrouter"

    # Twitch
    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    # Stockage
    data_dir: Path = ROOT / "data"

    # Pipeline
    max_video_height: int = 720
    max_source_duration_s: int = 3 * 3600
    initial_import_count: int = 2  # nb de vidéos prises à l'ajout d'une chaîne
    poll_interval_minutes: int = 30

    @property
    def provider_order(self) -> list[str]:
        return [p.strip() for p in self.llm_providers.split(",") if p.strip()]

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "clipforge.sqlite"

    def ensure_dirs(self) -> None:
        for d in (self.downloads_dir, self.work_dir, self.clips_dir, self.db_path.parent):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
