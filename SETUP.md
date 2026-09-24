# Mise en route — alerte prix carburants

Tout est prêt dans ce dossier : `alerte_carburants.py` (le script), `.github/workflows/alerte-carburants.yml`
(le cron GitHub Actions), et ce guide. Il reste 4 étapes, toutes de ton côté (je n'ai pas d'accès à ton
compte GitHub ni à ton Gmail depuis cette session).

## 1. Créer le mot de passe d'application Gmail

Gmail refuse désormais la connexion SMTP avec ton mot de passe habituel — il faut un **mot de passe
d'application** dédié :

1. Vérifie que la validation en deux étapes est activée sur ton compte Google (sinon, active-la d'abord).
2. Va sur **myaccount.google.com/apppasswords**.
3. Crée un mot de passe pour « Mail » / « Autre (nom personnalisé) » → note-le (16 caractères, sans espaces).
   C'est lui qui ira dans le secret `SMTP_PASS`, **pas** ton mot de passe Gmail normal.

## 2. Créer le dépôt GitHub et y pousser ce dossier

Depuis ce dossier (`D:\prix_carburants`), en Git Bash, **avec le VPN VDN actif** si tu pousses depuis le
réseau pro :

```bash
git init
git add alerte_carburants.py .github/workflows/alerte-carburants.yml SETUP.md .gitignore
git commit -m "Alerte prix carburants"
```

Puis crée un dépôt **privé** sur github.com (bouton "New repository", ne coche aucune case d'init), et :

```bash
git remote add origin https://github.com/<ton-compte>/<nom-du-depot>.git
git branch -M main
git push -u origin main
```

> Je n'ai volontairement pas ajouté au commit les fichiers `historique_prix_carburants.csv` et
> `alert_state.json` générés par mes tests locaux — le premier run sur GitHub Actions les recréera proprement.

## 3. Ajouter les secrets du dépôt

Sur GitHub : **Settings → Secrets and variables → Actions → New repository secret**. Ajoute :

| Secret | Valeur |
|---|---|
| `SMTP_USER` | ton adresse Gmail |
| `SMTP_PASS` | le mot de passe d'application (étape 1) |
| `MAIL_TO` | l'adresse qui doit recevoir le digest (peut être la même que `SMTP_USER`) |
| `SLACK_WEBHOOK_URL` | l'URL du webhook entrant Slack pour Marcel *(optionnel, voir ci-dessous)* |

## 4. Autoriser Actions à committer le journal

**Settings → Actions → General → Workflow permissions** → coche **"Read and write permissions"** → Save.
(Sinon le job plantera à l'étape "Committe le journal".)

## 5. Tester avant de laisser tourner le cron

Onglet **Actions** du dépôt → workflow *Alerte prix carburants* → bouton **Run workflow** (déclenchement
manuel, simule un run "digest"). Vérifie que le mail arrive et que le job se termine en vert.

---

## Pour le webhook Slack de Marcel

Si "Marcel" a déjà un webhook entrant existant utilisé ailleurs à la rédaction, donne-le-moi (ou colle-le
directement dans le secret `SLACK_WEBHOOK_URL`) — je n'ai pas besoin de le connaître, c'est un secret GitHub.
S'il n'existe pas encore : **api.slack.com/apps → Create New App → From scratch** → active
**Incoming Webhooks** → "Add New Webhook to Workspace" → choisis le canal cible → copie l'URL générée
(`https://hooks.slack.com/services/...`).

## Déclencheur externe (recommandé : ponctuel et fiable)

Les crons planifiés de GitHub sont « au mieux » (retardés ou sautés). On utilise donc un **service de cron externe**
(cron-job.org ou équivalent) qui appelle l'API GitHub à heure fixe pour lancer le workflow — démarrage en quelques
secondes. Les crons GitHub restent en simple filet de sécurité.

**1. Créer un jeton GitHub limité à ce dépôt** : GitHub → *Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token* :
- Nom : `cron-veille-carburants` · Expiration : la plus longue proposée (à renouveler à l'échéance)
- *Repository access* : **Only select repositories** → `veille-carburants`
- *Repository permissions* → **Actions : Read and write** (rien d'autre)
- Copier le jeton (`github_pat_...`), il ne s'affiche qu'une fois. **Ne le colle jamais dans un fichier du dépôt.**

**2. Créer la tâche sur le service de cron** :

| Champ | Valeur |
|---|---|
| URL | `https://api.github.com/repos/JulienDepelchin/veille-carburants/actions/workflows/alerte-carburants.yml/dispatches` |
| Méthode | `POST` |
| En-têtes | `Accept: application/vnd.github+json` · `Authorization: Bearer <ton jeton>` · `X-GitHub-Api-Version: 2022-11-28` · `Content-Type: application/json` |
| Corps | `{"ref":"main","inputs":{"mode":"verification"}}` |
| Fuseau | Europe/Paris |
| Fréquence | toutes les 30 minutes (`*/30 * * * *`) |
| Succès attendu | HTTP **204** (aucun contenu) |

Le digest part au premier appel à partir de 9h00 (donc à 9h00 pile si l'appel passe), les alertes au premier appel qui
voit une station atteindre le seuil. Ne mets **pas** `"mode":"digest"` dans la tâche récurrente : ça forcerait un digest
à chaque appel.

**3. Tester** : bouton « exécuter maintenant » du service → doit renvoyer 204, et un run apparaît dans l'onglet
*Actions* du dépôt quelques secondes plus tard.

## Rythme actuel (modifiable dans le fichier `.yml`)

GitHub Actions ne garantit **pas** l'heure d'exécution des crons : ils sont retardés, voire sautés, quand la
plateforme est chargée (surtout pile à l'heure ronde). Le script est donc conçu pour ne pas dépendre d'un run précis :

- **Un run toutes les 30 min** via le déclencheur externe (+ filet de sécurité : cron GitHub toutes les 4 h et à ~9h47).
  Chaque run recalcule les prix.
- **Digest quotidien** : envoyé par le **premier run qui tourne après 9h (heure de Paris)** si le digest du jour n'est pas
  encore parti. Si le run de 9h saute, il part au run suivant plutôt que jamais (pas de doublon : la date d'envoi est
  mémorisée dans `alert_state.json`). Insensible au changement d'heure.
- **Alerte de seuil** : dès qu'une station atteint 2,99 € ou 3,00 € (gazole) pour la première fois en France ou dans le
  NPDC — puis silence tant que la situation persiste.
- **Si un envoi échoue** (ex. Gmail refuse la connexion), le run passe en rouge dans l'onglet Actions ET l'envoi est
  retenté au run suivant (le digest/l'alerte n'est marqué « envoyé » qu'une fois réellement livré).

Consommation : ~48 runs/jour externes + ~7 GitHub ≈ 1 650 minutes/mois, sous le quota gratuit de 2 000 min/mois d'un
dépôt privé (passe à toutes les heures côté service externe si tu dois réduire).
