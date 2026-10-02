# Architecture

```
Sources (chaînes surveillées + liens manuels)
   -> File de traitement (une vidéo à la fois)
   -> Téléchargement (720p max)
   -> Transcription (sous-titres YouTube > Whisper Groq > faster-whisper CPU)
   -> Sélection des moments (LLM + signaux : chat Twitch, clips existants)
   -> Découpe + recadrage 9:16 + sous-titres animés (FFmpeg, fichiers ASS)
   -> Description + hashtags (LLM)
   -> Suppression de la source, clips enregistrés
   -> Dashboard : À poster / Publiés
```

## Modèle de données

```
Source (plateforme, identifiant, actif, filtres, dernière_vérif)
Video  (source_id | null si manuel, url, titre, statut_traitement)
  └── Clip (mp4, score, description, statut: prêt|posté|rejeté, date_posté, lien_tiktok)
```

Une vidéo est « publiée » quand elle n'a plus aucun clip au statut « prêt ».

## Contraintes de la machine cible

CPU i7-1255U, 16 Go de RAM, aucun GPU NVIDIA, ~28 Go de disque libre : une seule vidéo à la fois,
transcription déportée quand possible, source supprimée dès les clips rendus.

## Règles d'import

À l'ajout d'une chaîne : 2 dernières vidéos, puis uniquement les nouvelles publications.
Les liens manuels passent en priorité.
