# Clipforge

Surveille des chaînes YouTube et Twitch, découpe automatiquement les meilleurs moments,
les monte en vertical 9:16 avec sous-titres animés, génère la description, puis les range
dans un dashboard de validation. La publication TikTok est manuelle pour l'instant.

> N'utilise que des contenus dont tu as l'autorisation de réutiliser.

## Démarrage rapide (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env      # puis remplis les clés
pre-commit install          # hooks qualité + anti-fuite de secrets
pytest
```

## Structure

| Dossier | Rôle |
|---|---|
| `src/clipforge/sources/` | Détection des vidéos : chaînes YouTube (RSS), Twitch (Helix), liens manuels |
| `src/clipforge/pipeline/` | Téléchargement, transcription, sélection, découpe, montage, description |
| `src/clipforge/llm/` | Fournisseurs IA (Gemini, Groq, OpenRouter) avec repli automatique |
| `src/clipforge/db/` | Modèles et accès SQLite |
| `src/clipforge/scheduler/` | Surveillance périodique des chaînes |
| `src/clipforge/web/` | Dashboard (FastAPI + templates) |
| `config/` | Modèles de configuration |
| `data/` | Vidéos, clips, base SQLite (non versionné) |
| `docs/` | Architecture et décisions (ADR) |
| `tests/` | `unit/` rapides, `integration/` (réseau, FFmpeg) |

## Qualité

- `ruff check . && ruff format --check .` : lint et format
- `mypy` : typage
- `pytest -m "not integration"` : tests rapides
- La CI GitHub lance tout cela, plus un scan de secrets (gitleaks), à chaque push et PR.

## Secrets

Les clés vivent uniquement dans `.env` (ignoré par git). Modèle : `.env.example`.

## Connexion

Le dashboard est protégé par un compte. Au premier lancement, ouvre http://127.0.0.1:8000 :
tu es redirigé vers la page d'inscription, et ce premier compte devient le propriétaire.
Les inscriptions se ferment ensuite (réglable avec `ALLOW_REGISTRATION=true` dans `.env`).

Mot de passe oublié (il n'y a pas d'e-mail de récupération) :

```powershell
.\.venv\Scripts\python.exe -m clipforge.cli reset-password ton@email.com
```

Sécurité : mots de passe hachés (scrypt), sessions côté serveur révocables, cookie HttpOnly,
limitation des tentatives de connexion, contrôle d'origine anti-CSRF, clips servis uniquement aux
utilisateurs connectés. Si tu exposes Clipforge sur Internet, place-le derrière HTTPS
(et mets `SECURE_COOKIES=true`).
