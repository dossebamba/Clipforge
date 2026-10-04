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
Profile (nom, thème, langue, hashtags fixes, consignes de ton)  = un compte TikTok
Source (profile_id, plateforme, identifiant, actif, filtres, dernière_vérif)
Video  (profile_id, source_id | null si manuel, url, titre, statut_traitement)
  └── Clip (mp4, score, description, statut: prêt|posté|rejeté, date_posté, lien_tiktok)
```

Une vidéo est « publiée » quand elle n'a plus aucun clip au statut « prêt ».

## Contraintes de la machine cible

CPU i7-1255U, 16 Go de RAM, aucun GPU NVIDIA, ~28 Go de disque libre : une seule vidéo à la fois,
transcription déportée quand possible, source supprimée dès les clips rendus.

## Règles d'import

À l'ajout d'une chaîne : 2 dernières vidéos, puis uniquement les nouvelles publications.
Les liens manuels passent en priorité.

Un clip n'appartient qu'à un profil (celui de sa vidéo) : il n'est destiné qu'à un seul compte.
Le thème, la langue et le ton du profil sont injectés dans le prompt de sélection ; ses hashtags fixes
sont placés avant ceux de l'IA dans la légende.
