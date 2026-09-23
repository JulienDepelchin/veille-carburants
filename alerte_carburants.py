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
des alertes) et historique_prix_carburants.csv (journal, append-only — sert
aussi a calculer la tendance depuis la verification precedente).

Variables d'environnement (toutes optionnelles — sans elles, le script
calcule et affiche/enregistre mais n'envoie rien) :
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, MAIL_TO
  SLACK_WEBHOOK_URL
  SEND_EMAIL=1        -> envoie le mail (sinon: calcule seulement)
  SEND_SLACK=1        -> envoie sur Slack (sinon: calcule seulement)
  SLACK_ONLY_ON_ALERT=1 -> ne poste sur Slack que s'il y a un nouveau franchissement

Usage :
  python alerte_carburants.py                              # calcule, affiche, log, PAS d'envoi
  SEND_EMAIL=1 SEND_SLACK=1 python alerte_carburants.py     # calcule + envoie
"""
import os, sys, json, csv, gzip, smtplib, ssl, urllib.request, urllib.parse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Console Windows (cp1252) recrache une erreur sur les fleches/emoji unicode des
# messages ; GitHub Actions (Ubuntu, UTF-8) n'en a pas besoin mais ca ne genera pas.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "alert_state.json"
LOG_FILE = HERE / "historique_prix_carburants.csv"
NPDC_DEPTS = {"59", "62"}
SEUILS_GAZOLE = [2.99, 3.00]   # seuils psychologiques a surveiller
FUELS = {"gazole": "1", "e10": "5"}
API_BASE = "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/prix-des-carburants-en-france-flux-instantane-v2/exports/json"

# Couleurs pompe a essence francaise : gazole = jaune, SP95 = vert.
FUEL_META = {
    "gazole": {"label": "Gazole", "emoji": "\U0001F7E1", "hex": "#F2C94C", "hex_text": "#5c4a00"},
    "e10":    {"label": "SP95-E10", "emoji": "\U0001F7E2", "hex": "#27AE60", "hex_text": "#ffffff"},
}
SCOPE_META = {
    "france": {"label": "France entière", "filter": None},
    "npdc":   {"label": "Nord / Pas-de-Calais", "filter": NPDC_DEPTS},
}


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
    return {
        "n": len(sub), "moy": round(sum(prices) / len(prices), 4),
        "max": max(sub, key=lambda r: r["prix"]),
        "min": min(sub, key=lambda r: r["prix"]),
    }


def all_stats(rows):
    """{(scope, fuel): stats_for(...)} pour les 4 combinaisons."""
    out = {}
    for scope, meta in SCOPE_META.items():
        for fuel in FUELS:
            out[(scope, fuel)] = stats_for(rows, fuel, meta["filter"])
    return out


def stations_au_dessus(rows, fuel, seuil, dep_filter=None):
    return sorted(
        (r for r in rows if r["fuel"] == fuel and r["prix"] >= seuil and (dep_filter is None or r["dep"] in dep_filter)),
        key=lambda r: -r["prix"],
    )


# --------------------------------------------------------- tendance -------
def load_previous_averages():
    """Dernier prix_moyen connu par (perimetre, carburant), lu dans le journal
    AVANT que ce run n'y ajoute ses propres lignes."""
    prev = {}
    if not LOG_FILE.exists():
        return prev
    with open(LOG_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                prev[(row["perimetre"], row["carburant"])] = float(row["prix_moyen"])
            except (KeyError, ValueError):
                continue
    return prev


def trend_arrow(delta):
    if delta is None:
        return ""
    if delta > 0.0015:
        return f" ▲ +{delta:.3f} €"
    if delta < -0.0015:
        return f" ▼ {delta:.3f} €"
    return " ▬ stable"


# --------------------------------------------------------- presentation --
def fmt_station(r):
    dep_nom = "Nord" if r["dep"] == "59" else ("Pas-de-Calais" if r["dep"] == "62" else f"dept. {r['dep']}")
    return f"{r['prix']:.3f} EUR - {r['ville']} ({r['cp']}, {dep_nom}) - {r['adresse']}"


def build_report_text(rows, run_dt, stats, prev):
    """Version texte brut — log console + secours mail texte."""
    lines = [f"PRIX DES CARBURANTS — {run_dt.strftime('%A %d %B %Y, %Hh%M')} (heure UTC)", "=" * 60]
    for scope, smeta in SCOPE_META.items():
        lines.append(f"\n## {smeta['label'].upper()}")
        for fuel, fmeta in FUEL_META.items():
            s = stats[(scope, fuel)]
            if not s:
                lines.append(f"  {fmeta['label']} : pas de donnee valide")
                continue
            d = None
            pv = prev.get((scope, fuel))
            if pv is not None:
                d = s["moy"] - pv
            lines.append(f"  {fmeta['label']} (n={s['n']}) — moyenne {s['moy']:.3f} EUR/L{trend_arrow(d)}")
            lines.append(f"    + cher : {fmt_station(s['max'])}")
            lines.append(f"    - cher : {fmt_station(s['min'])}")
    lines.append("\n## SEUILS GAZOLE")
    for seuil in SEUILS_GAZOLE:
        st = stations_au_dessus(rows, "gazole", seuil)
        st_npdc = [r for r in st if r["dep"] in NPDC_DEPTS]
        lines.append(f"  >= {seuil:.2f} EUR : {len(st)} stations en France, dont {len(st_npdc)} dans le NPDC")
    return "\n".join(lines)


def build_report_html(rows, run_dt, stats, prev, new_crossings):
    """Mail joli — tableau HTML, compatible clients mail (styles inline)."""
    def badge(fuel):
        m = FUEL_META[fuel]
        return (f'<span style="display:inline-block;background:{m["hex"]};color:{m["hex_text"]};'
                f'padding:2px 9px;border-radius:12px;font-weight:700;font-size:13px;">'
                f'{m["emoji"]} {m["label"]}</span>')

    def row_html(scope, fuel):
        s = stats[(scope, fuel)]
        if not s:
            return ""
        d = None
        pv = prev.get((scope, fuel))
        if pv is not None:
            d = s["moy"] - pv
        trend = trend_arrow(d).strip()
        trend_color = "#c0392b" if trend.startswith("▲") else ("#1e7e46" if trend.startswith("▼") else "#8a8a8a")
        return f'''
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #eee;">{badge(fuel)}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:20px;font-weight:700;color:#111;">
            {s["moy"]:.3f} €<span style="font-size:12px;font-weight:600;color:{trend_color};margin-left:8px;">{trend}</span>
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #eee;font-size:12.5px;color:#444;">
            <b>+ cher</b> {s["max"]["prix"]:.3f} € — {s["max"]["ville"]} ({s["max"]["cp"]})<br>
            <b>- cher</b> {s["min"]["prix"]:.3f} € — {s["min"]["ville"]} ({s["min"]["cp"]})
          </td>
        </tr>'''

    def scope_block(scope):
        smeta = SCOPE_META[scope]
        rows_html = "".join(row_html(scope, fuel) for fuel in FUEL_META)
        return f'''
        <div style="margin-bottom:22px;">
          <div style="font-size:14px;font-weight:700;color:#111;letter-spacing:.02em;
                      text-transform:uppercase;margin:0 0 8px 2px;">{smeta["label"]}</div>
          <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eee;border-radius:8px;overflow:hidden;">
            {rows_html}
          </table>
        </div>'''

    seuils_html = ""
    for seuil in SEUILS_GAZOLE:
        st = stations_au_dessus(rows, "gazole", seuil)
        st_npdc = [r for r in st if r["dep"] in NPDC_DEPTS]
        seuils_html += (f'<div style="padding:6px 0;">🟡 <b>≥ {seuil:.2f} €</b> : '
                         f'{len(st)} station(s) en France, dont <b>{len(st_npdc)}</b> dans le NPDC</div>')

    alert_html = ""
    if new_crossings:
        items = "".join(f'<li style="margin:4px 0;">{fmt_station(r)}</li>' for r in new_crossings[:20])
        alert_html = f'''
        <div style="background:#fff4e5;border:1px solid #f0b429;border-radius:8px;padding:14px 16px;margin-bottom:22px;">
          <div style="font-weight:700;color:#8a5a00;margin-bottom:6px;">🚨 {len(new_crossings)} nouvelle(s) station(s) au-dessus d'un seuil</div>
          <ul style="margin:0;padding-left:18px;color:#5c4a00;font-size:13px;">{items}</ul>
        </div>'''

    return f'''<!doctype html>
<html><body style="margin:0;padding:24px;background:#f6f6f4;font-family:-apple-system,Segoe UI,Arial,sans-serif;">
  <div style="max-width:640px;margin:0 auto;">
    <div style="font-size:20px;font-weight:800;color:#111;margin-bottom:2px;">⛽ Prix des carburants</div>
    <div style="font-size:12.5px;color:#888;margin-bottom:20px;">{run_dt.strftime('%A %d %B %Y — %Hh%M')} (heure UTC)</div>
    {alert_html}
    {scope_block("france")}
    {scope_block("npdc")}
    <div style="background:#f0f0ee;border-radius:8px;padding:12px 16px;font-size:13px;color:#333;">
      <div style="font-weight:700;margin-bottom:4px;">Seuils gazole surveillés</div>
      {seuils_html}
    </div>
    <div style="font-size:11px;color:#aaa;margin-top:18px;">
      Source : flux instantané officiel des prix des carburants — méthode de filtrage AFP.
    </div>
  </div>
</body></html>'''


def build_slack_blocks(rows, run_dt, stats, prev, new_crossings):
    def field(scope, fuel):
        s = stats[(scope, fuel)]
        if not s:
            return None
        m = FUEL_META[fuel]
        d = None
        pv = prev.get((scope, fuel))
        if pv is not None:
            d = s["moy"] - pv
        txt = f"{m['emoji']} *{m['label']} — {SCOPE_META[scope]['label']}*\n*{s['moy']:.3f} €*{trend_arrow(d)}"
        txt += f"\n{s['max']['prix']:.3f} € {s['max']['ville']} ↔ {s['min']['prix']:.3f} € {s['min']['ville']}"
        return {"type": "mrkdwn", "text": txt}

    # Meme regroupement que le mail : un bloc par perimetre (France, puis NPDC),
    # gazole et SP95-E10 cote a cote dans chaque bloc.
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"⛽ Prix carburants — {run_dt.strftime('%d/%m %Hh%M')}", "emoji": True}},
    ]
    for scope in SCOPE_META:
        blocks.append({"type": "section", "fields": [field(scope, "gazole"), field(scope, "e10")]})
    n299 = len(stations_au_dessus(rows, "gazole", 2.99))
    n300 = len(stations_au_dessus(rows, "gazole", 3.00))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"Seuils gazole (France) : *{n299}* station(s) ≥ 2,99 € · *{n300}* ≥ 3,00 €"}]})

    if new_crossings:
        blocks.append({"type": "divider"})
        txt = f":rotating_light: *{len(new_crossings)} nouvelle(s) station(s) au-dessus d'un seuil*\n"
        txt += "\n".join(f"• {fmt_station(r)}" for r in new_crossings[:10])
        if len(new_crossings) > 10:
            txt += f"\n… et {len(new_crossings) - 10} de plus"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})

    return blocks


# ------------------------------------------------------------ etat/log ----
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"alerted_ids": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def compute_new_crossings(rows, state):
    new = []
    alerted = state.setdefault("alerted_ids", {})
    for seuil in SEUILS_GAZOLE:
        key = f"gazole_{seuil:.2f}"
        seen = set(alerted.get(key, []))
        for r in stations_au_dessus(rows, "gazole", seuil):
            if str(r["id"]) not in seen:
                new.append(r)
                seen.add(str(r["id"]))
        alerted[key] = sorted(seen)
    return new


def append_log(stats, run_dt):
    is_new = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["horodatage_utc", "perimetre", "carburant", "n", "prix_moyen",
                        "prix_max", "station_max", "prix_min", "station_min"])
        for (scope, fuel), s in stats.items():
            if not s:
                continue
            w.writerow([
                run_dt.isoformat(), scope, fuel, s["n"], s["moy"],
                s["max"]["prix"], f"{s['max']['ville']} ({s['max']['cp']})",
                s["min"]["prix"], f"{s['min']['ville']} ({s['min']['cp']})",
            ])


# -------------------------------------------------------------- envoi -----
def send_email(subject, text_body, html_body):
    host, port = os.environ.get("SMTP_HOST"), int(os.environ.get("SMTP_PORT", "587"))
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    mfrom, mto = os.environ.get("MAIL_FROM", user), os.environ.get("MAIL_TO")
    if not all([host, user, pw, mfrom, mto]):
        print("[mail] variables SMTP manquantes, envoi ignore", file=sys.stderr)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, mfrom, mto
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(user, pw)
        s.sendmail(mfrom, [mto], msg.as_string())
    print(f"[mail] envoye a {mto}")


def send_slack(blocks, fallback_text):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        print("[slack] SLACK_WEBHOOK_URL manquant, envoi ignore", file=sys.stderr)
        return
    payload = {"text": fallback_text, "blocks": blocks}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                  headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)
    print("[slack] envoye")


# ---------------------------------------------------------------- main ----
def main():
    now = datetime.now(timezone.utc)
    print(f"[run] {now.isoformat()} — recuperation du flux...")
    records = fetch_flux()
    rows = extract(records, now)
    stats = all_stats(rows)
    prev = load_previous_averages()  # AVANT d'ecrire le journal de ce run
    print(f"[run] {len(rows)} lignes station x carburant apres filtres AFP")

    state = load_state()
    new_crossings = compute_new_crossings(rows, state)

    text_report = build_report_text(rows, now, stats, prev)
    print("\n" + text_report + "\n")

    append_log(stats, now)
    save_state(state)

    if os.environ.get("SEND_EMAIL") == "1":
        html_report = build_report_html(rows, now, stats, prev, new_crossings)
        send_email(f"⛽ Prix carburants — {now.strftime('%d/%m %Hh%M')}", text_report, html_report)
    if os.environ.get("SEND_SLACK") == "1":
        if os.environ.get("SLACK_ONLY_ON_ALERT") != "1" or new_crossings:
            blocks = build_slack_blocks(rows, now, stats, prev, new_crossings)
            fallback = f"Prix carburants {now.strftime('%d/%m %Hh%M')} : voir details."
            send_slack(blocks, fallback)

    if new_crossings:
        print(f"[alerte] {len(new_crossings)} nouvelle(s) station(s) au-dessus d'un seuil gazole")


if __name__ == "__main__":
    main()
