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

    # Modèles
    gemini_model: str = "gemini-flash-latest"  # alias : suit le modèle Flash courant
    groq_model: str = "openai/gpt-oss-120b"
    openrouter_model: str = "openai/gpt-oss-120b:free"
    groq_whisper_model: str = "whisper-large-v3-turbo"
    local_whisper_model: str = "small"

    # Authentification
    allow_registration: bool = False  # True : inscriptions ouvertes même après le 1er compte
    secure_cookies: bool = False  # à mettre à True derrière HTTPS
    session_days: int = 14
    min_password_length: int = 10

    # Stockage
    data_dir: Path = ROOT / "data"

    # Pipeline
    max_video_height: int = 720
    max_source_duration_s: int = 3 * 3600
    initial_import_count: int = 2  # nb de vidéos prises à l'ajout d'une chaîne
    watch_window: int = 15  # nb de vidéos récentes examinées à chaque relevé
    poll_interval_minutes: int = 30
    max_pending_clips: int = 50  # au-delà, la file de traitement se met en pause

    # Clips
    clip_min_s: int = 20
    clip_max_s: int = 60
    max_clips_per_video: int = 6
    min_clip_score: int = 60
    content_language: str = "français"  # langue des accroches, titres et descriptions
    layout: str = "blur_fit"  # blur_fit | crop
    out_width: int = 1080
    out_height: int = 1920
    font_name: str = "Arial"
    fonts_dir: str = "C:/Windows/Fonts"

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
