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

## Profils (comptes TikTok)

Un profil = un compte TikTok / un thème (ex. « Voiture », « Divertissement »). Chaque chaîne et
chaque vidéo appartient à un seul profil, donc un clip n'est destiné qu'à un seul compte.
Le sélecteur en haut du dashboard filtre toutes les pages et les compteurs par profil.
Le thème, la langue et les consignes de ton d'un profil guident l'IA ; ses hashtags fixes sont
ajoutés à chaque légende. Une base existante est migrée automatiquement (profil « Général »).

## Transformations du montage

TikTok écarte du fil « Pour toi » les contenus non originaux, et ajouter des sous-titres ne suffit pas.
Chaque profil règle donc des transformations, pour que le clip apporte quelque chose en plus :

| Option | Effet |
|---|---|
| Accroche permanente | Le titre accrocheur reste affiché en haut pendant tout le clip (toujours actif). |
| Bande-annonce d'ouverture | Le passage le plus fort (2,5 à 7 s) est rejoué au tout début, puis le clip se déroule. |
| Remarques à l'écran | 1 à 2 courts commentaires écrits par l'IA (réaction, contexte), jamais pendant l'accroche. |
| Pseudo | Ton @compte en haut à droite. |
| Cadrage | Vidéo entière sur fond flouté, ou plein écran recadré. |

Le crédit du créateur (« Source : nom de la chaîne ») n'est pas affiché sur la vidéo : il est ajouté
à la description de chaque clip.

Les sorties de l'IA sont validées par le code (durées, positions, longueur des textes) avant d'être
appliquées. Les changements d'un profil s'appliquent aux prochains clips, pas à ceux déjà montés.
Rien ne garantit l'éligibilité au fil « Pour toi » : mesure la part des vues « Pour toi » dans les
statistiques TikTok de tes premiers clips.

## Limites des offres gratuites

- Gemini : le quota est compté par modèle. `GEMINI_MODEL` accepte une liste (par défaut
  `gemini-flash-latest,gemini-flash-lite-latest`) : si le premier est épuisé, le suivant est essayé.
- Groq (8 000 tokens/minute) : la transcription est découpée par budget de tokens, et les requêtes
  sont espacées d'après les en-têtes de limite du fournisseur, au lieu d'échouer puis de réessayer.
  Conséquence : une vidéo longue et dense se traite plus lentement (environ 25 s de pause entre deux
  requêtes quand Gemini est indisponible), mais elle aboutit.
- Page Activité : les erreurs, vidéos ignorées et vidéos sans clip peuvent être supprimées une par
  une ou en bloc. Elles sont masquées (pas effacées de la base) pour ne pas être retraitées au
  relevé suivant ; redonner leur lien manuellement les remet en file.
