# Mail Agent

Trie tes mails Gmail avec un LLM (Gemini) : les mails importants ressortent, le bruit
(promo, newsletter, notification, spam) est labellisé et marqué comme lu.

## Installation

### 1. Prérequis

- **Broker MQTT** : installe l'add-on officiel **Mosquitto broker**, démarre-le, puis accepte
  l'intégration MQTT que Home Assistant propose (Paramètres → Appareils et services).
  Sans lui, le panneau fonctionne mais aucune entité n'est créée dans HA.
- **Application OAuth Google** (celle déjà utilisée sur ton ordinateur) :
  - type d'identifiant **Desktop app** (fichier `credentials.json`) ;
  - écran de consentement en **In production**. L'app reste privée et non validée, mais sans
    ça les jetons expirent au bout de 7 jours et l'add-on s'arrêterait de fonctionner.
- **Clé API Gemini** : https://aistudio.google.com/apikey

### 2. Configuration de l'add-on

Onglet **Configuration** : colle ta clé Gemini, enregistre, puis démarre l'add-on et active
**Afficher dans la barre latérale**.

### 3. Dans le panneau « Mail Agent »

1. **Réglages → Avancé : éditer le YAML complet** : colle ton `config/profile.yaml`
   (profils, règles, comptes) et enregistre. Ou remplis le formulaire.
2. **Comptes** :
   - dépose ton `credentials.json` ;
   - pour chaque compte, clique sur **Connecter** et suis les étapes. Tu peux aussi importer
     le jeton `tokens/<adresse>.json` de ton ordinateur.
3. **Analyser (aperçu)** : vérifie le tri dans l'onglet **Aperçu du tri**, corrige si besoin,
   puis **Appliquer**.
4. Quand le tri te convient : **Réglages → Analyse automatique** (heure + récap sur le
   téléphone).

## Ce que fait l'analyse

| Action | Aperçu | Appliquer / auto |
|---|---|---|
| Classer les nouveaux mails (règles, puis Gemini) | ✓ | ✓ |
| Poser le label `IA/<Catégorie>` | | ✓ |
| Marquer comme lus promo, newsletter, notification, spam | | ✓ |
| Archiver ou supprimer | jamais | jamais |

Un mail déjà appliqué n'est jamais renvoyé au LLM.

## Entités créées (via MQTT)

| Entité | Rôle |
|---|---|
| `sensor.mail_agent_important_unread` | Nombre d'importants non lus (liste dans les attributs) |
| `sensor.mail_agent_last_run` / `last_status` | Dernière analyse |
| `sensor.mail_agent_next_run` | Prochaine analyse automatique |
| `sensor.mail_agent_tokens_today` | Tokens consommés aujourd'hui |
| `sensor.mail_agent_cost_30d` | Coût équivalent au palier payant sur 30 jours |
| `binary_sensor.mail_agent_quota_alert` | Quota Gemini atteint ou seuil quotidien dépassé |
| `switch.mail_agent_auto` | Active/désactive l'analyse automatique |
| `button.mail_agent_analyze` / `analyze_apply` | Lance une analyse (aperçu / appliquée) |

Exemple de carte pour un tableau de bord :

```yaml
type: entities
title: Mail Agent
entities:
  - sensor.mail_agent_important_unread
  - switch.mail_agent_auto
  - sensor.mail_agent_next_run
  - button.mail_agent_analyze
  - sensor.mail_agent_tokens_today
  - binary_sensor.mail_agent_quota_alert
```

## Données et confidentialité

- Tout est stocké dans le dossier privé de l'add-on (`/data`) : réglages, jetons Gmail,
  historique. Rien n'est dans le dépôt GitHub.
- Seuls l'expéditeur, l'objet et un extrait (~200 caractères) sont envoyés à Gemini. Les
  expéditeurs « privés » ne le sont jamais.
- Offre gratuite de Gemini : Google peut utiliser ces contenus pour améliorer ses produits.
  Le palier payant l'exclut (voir l'onglet Consommation pour le coût estimé).
- Le panneau n'est accessible qu'à travers Home Assistant (utilisateurs connectés).
