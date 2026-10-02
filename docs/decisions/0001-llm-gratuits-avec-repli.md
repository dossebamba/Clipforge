# ADR 0001 : LLM gratuits avec repli automatique

**Statut :** accepté

**Contexte.** L'abonnement Claude ne donne pas accès à l'API. Le besoin est faible en volume
(quelques appels par vidéo) mais exige un grand contexte, du JSON fiable et du français correct.

**Décision.** Gemini Flash en principal, Groq (gpt-oss-120b / qwen) en secours, OpenRouter en
dernier recours, derrière une interface unique. L'ordre se règle via `LLM_PROVIDERS`.
La transcription utilise d'abord les sous-titres YouTube, puis Whisper sur Groq, puis faster-whisper en local.

**Conséquences.** Aucun coût d'API. Les quotas gratuits ne sont pas garantis, d'où le repli.
Les données envoyées (transcripts de vidéos publiques) peuvent servir à l'entraînement chez le fournisseur.
