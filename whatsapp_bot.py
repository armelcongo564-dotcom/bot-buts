"""
Bot WhatsApp : "Moins de 4 buts ?" (Under 3.5) -> OUI / NON
Tu écris sur WhatsApp :  Arsenal - Chelsea   (équipe domicile - équipe extérieur)
Le bot cherche les stats (football-data.org), calcule, et te répond.

Variables d'environnement nécessaires :
  FOOTBALL_DATA_KEY       clé football-data.org
  TWILIO_ACCOUNT_SID      Account SID Twilio
  TWILIO_AUTH_TOKEN       Auth Token Twilio
  TWILIO_WHATSAPP_FROM    ex: whatsapp:+14155238886 (numéro du sandbox Twilio)
"""
import base64
import difflib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, request

app = Flask(__name__)

FD_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")
TW_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TW_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TW_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

API = "https://api.football-data.org/v4"
COMPETITIONS = ["PL", "PD", "SA", "BL1", "FL1", "DED", "PPL", "ELC", "BSA"]
NOMS = {"PL": "Premier League", "PD": "Liga", "SA": "Serie A", "BL1": "Bundesliga",
        "FL1": "Ligue 1", "DED": "Eredivisie", "PPL": "Liga Portugal",
        "ELC": "Championship", "BSA": "Brasileirão"}
SEUIL_OUI = 0.60
RAFRAICHIR_APRES = 6 * 3600   # on recharge les stats toutes les 6 heures

DATA = {"equipes": {}, "ligues": {}, "pret": False, "maj": 0}
VERROU = threading.Lock()


# ---------- Récupération des stats ----------
def appel_api(chemin):
    req = urllib.request.Request(API + chemin, headers={"X-Auth-Token": FD_KEY})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(30)
                continue
            return None
        except Exception:
            return None
    return None


def charger_donnees():
    """Télécharge les classements (total / domicile / extérieur) de chaque ligue."""
    equipes, ligues = {}, {}
    for code in COMPETITIONS:
        d = appel_api(f"/competitions/{code}/standings")
        time.sleep(7)  # respect de la limite gratuite (10 requêtes/minute)
        if not d:
            continue
        tables = {b["type"]: b["table"] for b in d.get("standings", [])}
        total = tables.get("TOTAL", [])
        buts = sum(l["goalsFor"] for l in total)
        matchs = sum(l["playedGames"] for l in total)
        ligues[code] = buts / matchs if matchs else 1.35
        for typ in ("TOTAL", "HOME", "AWAY"):
            for l in tables.get(typ, []):
                tid = l["team"]["id"]
                e = equipes.setdefault(tid, {"team": l["team"], "code": code})
                j = max(l["playedGames"], 1)
                e[typ] = (l["goalsFor"] / j, l["goalsAgainst"] / j, l["playedGames"])
    with VERROU:
        if equipes:
            DATA["equipes"], DATA["ligues"] = equipes, ligues
            DATA["pret"], DATA["maj"] = True, time.time()


def boucle_maj():
    while True:
        if time.time() - DATA["maj"] > RAFRAICHIR_APRES:
            try:
                charger_donnees()
            except Exception:
                pass
        time.sleep(60)


threading.Thread(target=boucle_maj, daemon=True).start()


# ---------- Calcul ----------
def trouver(nom):
    nom = nom.lower().strip()
    meilleur = (0.0, None)
    for tid, e in DATA["equipes"].items():
        for cand in (e["team"].get("name", ""), e["team"].get("shortName", ""),
                     e["team"].get("tla", "")):
            cand = cand.lower()
            if not cand:
                continue
            s = difflib.SequenceMatcher(None, nom, cand).ratio()
            if len(nom) >= 3 and (nom in cand or cand in nom):
                s = max(s, 0.9)
            if s > meilleur[0]:
                meilleur = (s, tid)
    return meilleur[1] if meilleur[0] >= 0.6 else None


def stats(tid, cote):
    e = DATA["equipes"][tid]
    s = e.get(cote)
    if not s or s[2] < 3:   # début de saison : on prend le total
        s = e["TOTAL"]
    return s[0], s[1]


def poisson_cdf(k_max, lam):
    return sum(math.exp(-lam) * lam**k / math.factorial(k) for k in range(k_max + 1))


def analyser(texte):
    parts = re.split(r"\s+(?:-|–|vs|contre)\s+", texte.strip(), flags=re.I)
    if len(parts) != 2:
        return ("Écris les deux équipes comme ça :\n"
                "Arsenal - Chelsea\n(domicile - extérieur, avec des espaces autour du tiret)")
    if not DATA["pret"]:
        return "⏳ Je charge les données, réessaie dans 1 à 2 minutes."
    a, b = trouver(parts[0]), trouver(parts[1])
    if a is None or b is None:
        manque = parts[0] if a is None else parts[1]
        return (f"❌ Je ne trouve pas « {manque.strip()} ».\n"
                "Championnats couverts : " + ", ".join(NOMS.values()))
    dm, de = stats(a, "HOME")
    em, ee = stats(b, "AWAY")
    moy = DATA["ligues"].get(DATA["equipes"][a]["code"], 1.35)
    lam = dm * ee / moy + em * de / moy
    p = poisson_cdf(3, lam)
    na = DATA["equipes"][a]["team"]["name"]
    nb = DATA["equipes"][b]["team"]["name"]
    rep = "OUI ✅" if p >= SEUIL_OUI else "NON ❌"
    return (f"⚽ {na} 🆚 {nb}\n"
            f"Buts attendus : {lam:.2f}\n"
            f"Proba de moins de 4 buts : {p*100:.0f}%\n"
            f"👉 Moins de 4 buts : {rep}\n\n"
            "⚠️ Estimation basée sur les moyennes de buts seulement, pas une certitude.")


# ---------- WhatsApp (Twilio) ----------
def envoyer(destinataire, texte):
    data = urllib.parse.urlencode({"To": destinataire, "From": TW_FROM, "Body": texte}).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{TW_SID}/Messages.json", data=data)
    auth = base64.b64encode(f"{TW_SID}:{TW_TOKEN}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print("Erreur envoi Twilio :", e)


def traiter(destinataire, texte):
    try:
        reponse = analyser(texte)
    except Exception as e:
        print("Erreur analyse :", e)
        reponse = "😕 Une erreur est survenue, réessaie."
    envoyer(destinataire, reponse)


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    texte = request.form.get("Body", "")
    de = request.form.get("From", "")
    # On répond tout de suite à Twilio, et on envoie le résultat ensuite
    threading.Thread(target=traiter, args=(de, texte), daemon=True).start()
    return Response("<Response></Response>", mimetype="text/xml")


@app.route("/")
def accueil():
    return "Bot moins de 4 buts : en ligne ✅"
