# mailAgent

Agent personnel de tri de mails Gmail, assisté par LLM (Gemini par défaut), utilisable en
ligne de commande ou comme **add-on Home Assistant** avec un panneau de contrôle.

## Fonctionnement

```
Gmail ──> récupération (en-têtes + extrait, pas le corps complet)
            │
            ├─> règles (listes d'expéditeurs)  → décision immédiate, rien n'est envoyé au LLM
            │
            └─> LLM par lots de 15 mails       → catégorie + confiance + raison (JSON structuré)
                        │
                        ├─> historique SQLite (décisions, corrections, tokens)
                        └─> si appliqué : label IA/<Catégorie> + bruit marqué lu
```

## Organisation du dépôt

Le dépôt est un **dépôt d'add-ons Home Assistant** : `repository.yaml` à la racine,
l'add-on dans `mailagent/`.

| Fichier | Rôle |
|---|---|
| `mailagent/config.yaml`, `Dockerfile`, `DOCS.md` | Manifeste, image et documentation de l'add-on |
| `mail_agent/gmail_client.py` | Connexion OAuth (navigateur local ou copier-coller depuis le panneau), labels |
| `mail_agent/prefilter.py` | Règles déterministes (`@domaine`, `adresse`, `name:Nom affiché`) |
| `mail_agent/llm/base.py` | **Le prompt** + interface commune aux fournisseurs |
| `mail_agent/llm/gemini.py` | Appel Gemini : sortie structurée, comptage des tokens, gestion du quota |
| `mail_agent/classifier.py` | Orchestration : règles → LLM → décisions |
| `mail_agent/engine.py` | Analyses en arrière-plan, application, corrections, état global |
| `mail_agent/store.py` | Historique SQLite |
| `mail_agent/scheduler.py` | Analyse automatique quotidienne |
| `mail_agent/ha.py` | Capteurs/boutons MQTT et notifications Home Assistant |
| `mail_agent/web.py`, `web/` | Panneau : API FastAPI + page HTML/JS sans build |
| `mail_agent/main.py` | Ligne de commande |

(`mail_agent/` = `mailagent/mail_agent/`)

## Add-on Home Assistant

Voir [`mailagent/DOCS.md`](mailagent/DOCS.md) : installation, entités créées, exemple de carte.

En bref : Paramètres → Modules complémentaires → Boutique → ⋮ → **Dépôts** → ajouter
`https://github.com/PyT/mailAgent`, puis installer **Mail Agent**.

## Développement en local (Mac)

Les données locales sont à la racine du dépôt, toutes ignorées par git : `.env`,
`credentials.json`, `config/profile.yaml`, `tokens/`, `data/`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r mailagent/requirements.txt
cp .env.example .env                                           # GEMINI_API_KEY=...
cp mailagent/mail_agent/profile.example.yaml config/profile.yaml

cd mailagent
../.venv/bin/python -m mail_agent.main            # ligne de commande, lecture seule
../.venv/bin/python -m mail_agent.main --apply    # labels + bruit marqué lu
../.venv/bin/python -m mail_agent.server          # panneau sur http://localhost:8099
```

### Accès Gmail (OAuth), à faire une seule fois pour tous les comptes

1. https://console.cloud.google.com → crée un projet
2. **APIs & Services → Library** → active **Gmail API**
3. **OAuth consent screen** : type **External**, ajoute tes adresses en **Test users**, puis
   passe l'app **In production** pour que les jetons n'expirent pas au bout de 7 jours.
   L'app reste privée et non validée.
4. **Credentials → Create credentials → OAuth client ID** → type **Desktop app** (pas « Web
   application »)
5. Télécharge le JSON et enregistre-le sous `credentials.json`

Au premier lancement de la ligne de commande, un navigateur s'ouvre pour chaque compte.
Le programme vérifie que tu as choisi le bon compte. Les jetons sont dans `tokens/`.

## Confidentialité

- Seuls l'expéditeur, l'objet et l'extrait (~200 caractères) sont envoyés au LLM.
- Les `private_senders` ne sont **jamais** envoyés au LLM.
- **Offre gratuite de Gemini : Google peut utiliser les contenus pour améliorer ses produits.**
  Le palier payant l'exclut.
- La permission OAuth `gmail.modify` n'autorise ni l'envoi ni la suppression définitive.
- Le contenu des mails est traité comme des **données** : le prompt interdit au LLM d'obéir
  aux instructions qu'un mail contiendrait, et le panneau n'insère jamais ce contenu comme HTML.
