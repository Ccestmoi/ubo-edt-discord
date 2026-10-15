# ubo-edt-discord

Envoi quotidien automatique des emplois du temps **M1 Info S7 (UBO)** vers un serveur
Discord, avec un embed compact et lisible sur téléphone — un embed par salon :
`edt1A`, `edt1B`, `edt2A`, `edt2B`.

Chaque salon affiche :

- **📘 Cours <groupe>** — les cours du groupe TP principal du salon ;
- **📢 Événements communs** — événements partagés par plusieurs formations (AG, Nuit de l'Info…) ;
- **🚀 Projet — TIIL-A** — les créneaux du projet TIIL-A ;
- **🤝 Projet PVP** — les créneaux PVP, étiquetés « Tous PVP » ou par groupe (PVP1, PVP2, PVP3).

Les événements présents dans plusieurs calendriers sont **dédupliqués**, et les
créneaux consécutifs identiques (même cours, même salle, écart ≤ 30 min) sont
**fusionnés**. La salle et l'enseignant sont extraits automatiquement de
l'export iCal UBO.

Le script n'utilise **que la bibliothèque standard Python** : aucune installation
de dépendance n'est nécessaire.

## 1. Créer les webhooks Discord

Pour chacun des 4 salons (`edt1A`, `edt1B`, `edt2A`, `edt2B`) :

1. Sur le salon : ⚙️ **Paramètres du salon** → **Intégrations** → **Webhooks**
   → **Nouveau webhook** ;
2. Nommez-le par ex. « EDT M1 Info » ;
3. **Copier l'URL du webhook** (format
   `https://discord.com/api/webhooks/<id>/<token>`).

## 2. Enregistrer les secrets GitHub

Dans le dépôt : **Settings → Secrets and variables → Actions → New repository
secret**, ajouter 4 secrets :

| Nom du secret     | Contenu                          |
|-------------------|----------------------------------|
| `WEBHOOK_EDT1A`   | URL du webhook du salon `edt1A`  |
| `WEBHOOK_EDT1B`   | URL du webhook du salon `edt1B`  |
| `WEBHOOK_EDT2A`   | URL du webhook du salon `edt2A`  |
| `WEBHOOK_EDT2B`   | URL du webhook du salon `edt2B`  |

## 3. Tester

Onglet **Actions** → **EDT Discord quotidien** → **Run workflow** :

- cochez **dry_run** pour une simulation (les embeds s'affichent dans le log
  du job, rien n'est envoyé à Discord) ;
- le champ **date** permet de tester sur une journée précise (`2026-10-19` par ex.).

En local, c'est identique :

```bash
EDT_DRY_RUN=1 EDT_DATE=2026-10-19 python3 edt_discord.py   # simulation
python3 edt_discord.py                                     # envoi réel (nécessite les webhooks)
```

## 4. Fonctionnement automatique

Le workflow s'exécute tous les jours du lundi au samedi à **06:00 UTC**
(07:00 à Paris en hiver, 08:00 en été). Pour modifier l'horaire, ajustez le
`cron` dans `.github/workflows/edt-daily.yml`.

## Configuration

Tout se règle dans **`config.json`** :

- `calendars` : nom → URL iCal UBO. Le portail UBO génère une URL par
  sélection de groupes ; pour en ajouter/remplacer un, récupérez le nouvel
  export iCal sur le portail UBO et mettez l'URL ici ;
- `channels` : un salon = un embed. `main` = calendrier(s) du groupe TP du
  salon ; `combined` = calendriers affichés en sections additionnelles ;
  `color` = couleur de l'embed (hexadécimal) ; `webhook_env` = nom du secret
  GitHub contenant l'URL du webhook.

### Calendrier TP2A indisponible

Le lien iCal du groupe **TP2A** renvoie actuellement une erreur 404 côté UBO
(l'export a expiré). Le script le gère proprement : le salon `edt2A` affiche un
avertissement et les sections communes/PVP/TIIL fonctionnent quand même.

**Pour le corriger** : sur le portail UBO, sélectionnez le groupe TP2A seul →
générez un nouvel export iCal → remplacez l'URL `TP2A` dans `config.json` par
la nouvelle URL (format `https://m.univ-brest.fr/ical/<uuid>.ics`).

## Comportements notables

- **Calendrier inaccessible** : le run ne s'arrête pas ; l'embed affiche
  un avertissement et les autres salons partent normalement ;
- **Journée sans cours** : « Aucun cours aujourd'hui 🎉 » ;
- **Rate limit Discord (429)** : nouvelle tentative automatique après le
  délai indiqué par Discord ;
- **Code de sortie** : 0 si tout est OK, 1 si au moins un salon a échoué
  (visible en rouge dans l'onglet Actions).
