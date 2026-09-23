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

## Rythme actuel du cron (modifiable dans le fichier `.yml`)

- **07h00 et 19h00 (heure de Paris)** : digest complet (mail + Slack) — moyenne/plus cher/moins cher,
  France + NPDC, gazole et SP95-E10.
- **Toutes les 2 heures entre 6h et 22h (Paris)** : vérification silencieuse ; un message Slack part
  **seulement** si une station franchit 2,99 € ou 3,00 € pour la première fois (pas de répétition).

⚠️ Les horaires sont écrits en UTC dans le fichier cron. Paris passe en heure d'hiver (UTC+1) fin octobre
2026 — les runs se décaleront alors d'une heure (08h/20h au lieu de 07h/19h) tant que le fichier n'est
pas ajusté.
