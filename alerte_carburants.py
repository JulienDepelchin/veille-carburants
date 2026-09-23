#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alerte prix carburants — France + Nord/Pas-de-Calais
======================================================

Calcule un prix moyen fiable (methode AFP, cf. memoire projet) a partir du flux
instantane officiel, national et regional (NPDC = 59+62), et alerte quand des
stations franchissent 2,99 EUR ou 3,00 EUR le litre de gazole.

Concu pour tourner plusieurs fois par jour (cron / GitHub Actions) sans etat
partage entre executions autre que le fichier alert_state.json (deduplication
des alertes) et historique_prix_carburants.csv (journal, append-only).

Variables d'environnement (toutes optionnelles — sans elles, le script
calcule et affiche/enregistre mais n'envoie rien) :
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, MAIL_TO
  SLACK_WEBHOOK_URL
  SEND_EMAIL=1        -> envoie le mail (sinon: calcule seulement)
  SEND_SLACK=1        -> envoie sur Slack (sinon: calcule seulement)

Usage :
  python alerte_carburants.py                 # calcule, affiche, log, PAS d'envoi
  SEND_EMAIL=1 SEND_SLACK=1 python alerte_carburants.py   # calcule + envoie
"""
import os, sys, json, csv, gzip, smtplib, ssl, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "alert_state.json"
LOG_FILE = HERE / "historique_prix_carburants.csv"
NPDC_DEPTS = {"59", "62"}
SEUILS_GAZOLE = [2.99, 3.00]   # seuils psychologiques a surveiller
FUELS = {"gazole": "1", "e10": "5"}
API_BASE = "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/prix-des-carburants-en-france-flux-instantane-v2/exports/json"


# ---------------------------------------------------------------- fetch ----
def fetch_flux(retries=3):
    sel = "id,ville,cp,code_departement,adresse,prix,rupture"
    url = API_BASE + "?select=" + urllib.parse.quote(sel)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "vdn-alerte-carburants/1.0"})
            raw = urllib.request.urlopen(req, timeout=120).read()
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
            return json.loads(raw)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"echec recuperation flux apres {retries} tentatives: {last_err}")


# ------------------------------------------------------------- filtrage ----
def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def extract(records, now):
    """Renvoie une liste de lignes {id, ville, cp, dep, adresse, fuel, prix, age_j}
    apres application des filtres AFP (bornes, silence<=120j, hors rupture J-1/J-2,
    maj<=31j)."""
    out = []
    for r in records:
        prix_arr, rupt_arr = {}, {}
        pv = r.get("prix")
        if pv:
            try:
                arr = json.loads(pv) if isinstance(pv, str) else pv
                for p in arr:
                    if isinstance(p, dict):
                        prix_arr[p.get("@id")] = p
            except Exception:
                pass
        rv = r.get("rupture")
        if rv:
            try:
                arr = json.loads(rv) if isinstance(rv, str) else rv
                for x in arr:
                    if isinstance(x, dict):
                        rupt_arr[x.get("@id")] = x
            except Exception:
                pass

        for fuel, fid in FUELS.items():
            p = prix_arr.get(fid)
            if not p or p.get("@valeur") in (None, ""):
                continue
            try:
                val = float(p["@valeur"])
            except (TypeError, ValueError):
                continue
            if not (0.50 <= val <= 5.00):
                continue
            maj = parse_dt(p.get("@maj"))
            if not maj:
                continue
            age = (now - maj).total_seconds() / 86400
            if age > 120:
                continue
            x = rupt_arr.get(fid)
            if x and not x.get("@fin"):
                d = parse_dt(x.get("@debut"))
                if d and (now - d).days >= 1:
                    continue  # en rupture depuis J-1/J-2 -> exclu
            if age > 31:
                continue  # cf. memoire projet : plafond TotalEnergies deja couvert par maj frequent
            out.append({
                "id": r["id"], "ville": r.get("ville", ""), "cp": r.get("cp", ""),
                "dep": r.get("code_departement", ""), "adresse": (r.get("adresse") or "").strip(),
                "fuel": fuel, "prix": round(val, 4), "age_j": round(age, 2),
            })
    return out


# -------------------------------------------------------------- stats -----
def stats_for(rows, fuel, dep_filter=None):
    sub = [r for r in rows if r["fuel"] == fuel and (dep_filter is None or r["dep"] in dep_filter)]
    if not sub:
        return None
    prices = [r["prix"] for r in sub]
    mx = max(sub, key=lambda r: r["prix"])
    mn = min(sub, key=lambda r: r["prix"])
    return {
        "n": len(sub), "moy": round(sum(prices) / len(prices), 4),
        "max": mx, "min": mn,
    }


def stations_au_dessus(rows, fuel, seuil, dep_filter=None):
    return sorted(
        (r for r in rows if r["fuel"] == fuel and r["prix"] >= seuil and (dep_filter is None or r["dep"] in dep_filter)),
        key=lambda r: -r["prix"],
    )


# --------------------------------------------------------- presentation --
def fmt_station(r):
    dep_nom = "Nord" if r["dep"] == "59" else ("Pas-de-Calais" if r["dep"] == "62" else f"dept. {r['dep']}")
    return f"{r['prix']:.3f} EUR - {r['ville']} ({r['cp']}, {dep_nom}) - {r['adresse']}"


def build_report(rows, run_dt):
    lines = []
    lines.append(f"PRIX DES CARBURANTS — {run_dt.strftime('%A %d %B %Y, %Hh%M')} (heure UTC)")
    lines.append("=" * 60)

    for scope_name, dep_filter in [("FRANCE ENTIERE", None), ("NORD / PAS-DE-CALAIS", NPDC_DEPTS)]:
        lines.append(f"\n## {scope_name}")
        for fuel, label in [("gazole", "Gazole"), ("e10", "SP95-E10")]:
            s = stats_for(rows, fuel, dep_filter)
            if not s:
                lines.append(f"  {label} : pas de donnee valide")
                continue
            lines.append(f"  {label} (n={s['n']}) — moyenne {s['moy']:.3f} EUR/L")
            lines.append(f"    + cher : {fmt_station(s['max'])}")
            lines.append(f"    - cher : {fmt_station(s['min'])}")

    lines.append(f"\n## SEUILS GAZOLE")
    for seuil in SEUILS_GAZOLE:
        st = stations_au_dessus(rows, "gazole", seuil)
        st_npdc = [r for r in st if r["dep"] in NPDC_DEPTS]
        lines.append(f"  >= {seuil:.2f} EUR : {len(st)} stations en France, dont {len(st_npdc)} dans le NPDC")
    return "\n".join(lines)


def build_slack_message(rows, run_dt, new_crossings):
    npdc_g = stats_for(rows, "gazole", NPDC_DEPTS)
    npdc_e = stats_for(rows, "e10", NPDC_DEPTS)
    fr_g = stats_for(rows, "gazole", None)
    txt = [f"*Prix carburants — {run_dt.strftime('%d/%m %Hh%M')}*"]
    if fr_g:
        txt.append(f"Gazole France : moy *{fr_g['moy']:.3f} EUR* (max {fr_g['max']['prix']:.3f} — {fr_g['max']['ville']})")
    if npdc_g:
        txt.append(f"Gazole NPDC : moy *{npdc_g['moy']:.3f} EUR* (max {npdc_g['max']['prix']:.3f} — {npdc_g['max']['ville']})")
    if npdc_e:
        txt.append(f"SP95-E10 NPDC : moy *{npdc_e['moy']:.3f} EUR*")
    if new_crossings:
        txt.append(f"\n:rotating_light: *{len(new_crossings)} nouvelle(s) station(s) au-dessus d'un seuil :*")
        for r in new_crossings[:15]:
            txt.append(f"  • {fmt_station(r)}")
        if len(new_crossings) > 15:
            txt.append(f"  … et {len(new_crossings) - 15} de plus")
    return "\n".join(txt)


# ------------------------------------------------------------ etat/log ----
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"alerted_ids": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def compute_new_crossings(rows, state):
    """Renvoie les stations qui viennent de franchir un seuil (pas deja signalees),
    et met a jour l'etat. Cle = id station + seuil, pour ne pas re-notifier."""
    new = []
    alerted = state.setdefault("alerted_ids", {})
    for seuil in SEUILS_GAZOLE:
        key = f"gazole_{seuil:.2f}"
        seen = set(alerted.get(key, []))
        above = stations_au_dessus(rows, "gazole", seuil)
        for r in above:
            if str(r["id"]) not in seen:
                new.append(r)
                seen.add(str(r["id"]))
        alerted[key] = sorted(seen)
    return new


def append_log(rows, run_dt):
    is_new = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["horodatage_utc", "perimetre", "carburant", "n", "prix_moyen",
                        "prix_max", "station_max", "prix_min", "station_min",
                        "n_ge_2_99", "n_ge_3_00"])
        for scope_name, dep_filter in [("france", None), ("npdc", NPDC_DEPTS)]:
            for fuel in FUELS:
                s = stats_for(rows, fuel, dep_filter)
                if not s:
                    continue
                n299 = len(stations_au_dessus(rows, fuel, 2.99, dep_filter)) if fuel == "gazole" else ""
                n300 = len(stations_au_dessus(rows, fuel, 3.00, dep_filter)) if fuel == "gazole" else ""
                w.writerow([
                    run_dt.isoformat(), scope_name, fuel, s["n"], s["moy"],
                    s["max"]["prix"], f"{s['max']['ville']} ({s['max']['cp']})",
                    s["min"]["prix"], f"{s['min']['ville']} ({s['min']['cp']})",
                    n299, n300,
                ])


# -------------------------------------------------------------- envoi -----
def send_email(subject, body):
    host, port = os.environ.get("SMTP_HOST"), int(os.environ.get("SMTP_PORT", "587"))
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    mfrom, mto = os.environ.get("MAIL_FROM", user), os.environ.get("MAIL_TO")
    if not all([host, user, pw, mfrom, mto]):
        print("[mail] variables SMTP manquantes, envoi ignore", file=sys.stderr)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, mfrom, mto
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(user, pw)
        s.sendmail(mfrom, [mto], msg.as_string())
    print(f"[mail] envoye a {mto}")


def send_slack(text):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        print("[slack] SLACK_WEBHOOK_URL manquant, envoi ignore", file=sys.stderr)
        return
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode("utf-8"),
                                  headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)
    print("[slack] envoye")


# ---------------------------------------------------------------- main ----
def main():
    now = datetime.now(timezone.utc)
    print(f"[run] {now.isoformat()} — recuperation du flux...")
    records = fetch_flux()
    rows = extract(records, now)
    print(f"[run] {len(rows)} lignes station x carburant apres filtres AFP")

    state = load_state()
    new_crossings = compute_new_crossings(rows, state)

    report = build_report(rows, now)
    print("\n" + report + "\n")

    append_log(rows, now)
    save_state(state)

    if os.environ.get("SEND_EMAIL") == "1":
        send_email(f"Prix carburants — {now.strftime('%d/%m %Hh%M')}", report)
    if os.environ.get("SEND_SLACK") == "1":
        # SLACK_ONLY_ON_ALERT=1 : ne poste que s'il y a du nouveau (utile pour les
        # passages frequents "verification seuil") ; sinon poste a chaque run
        # (utile pour les 2 runs "digest" du matin/soir).
        if os.environ.get("SLACK_ONLY_ON_ALERT") != "1" or new_crossings:
            send_slack(build_slack_message(rows, now, new_crossings))

    if new_crossings:
        print(f"[alerte] {len(new_crossings)} nouvelle(s) station(s) au-dessus d'un seuil gazole")


if __name__ == "__main__":
    main()
