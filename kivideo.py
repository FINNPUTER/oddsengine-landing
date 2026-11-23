"""
kivideo.py: KI-Videos in Your Engine ueber die Higgsfield-API, seit 2.64.0. Neben den Text- und Bild-Modellen.

Was es kann:
  Text zu Video und Foto zu Video (das letzte Foto, das der Nutzer geschickt hat, ist das erste Bild).
  Modelle: Kling 4.0, Kling 3.0 Pro, Kling 3.0 Turbo, Seedance 2.5, Hailuo 2.3, Wan 3.0.
  Kling 4.0 (seit 2.64.1): Higgsfield nennt den API-Pfad nicht oeffentlich. Die Engine probiert die moeglichen Pfade
  ueber die kostenlose Preisabfrage (/estimate) und merkt sich den, der antwortet. Feste Vorgabe moeglich mit
  AIVIDEO_KLING4_TEXT und AIVIDEO_KLING4_IMAGE (Pfad aus der Console, ohne https://api.higgsfield.ai/).

Ablauf:
  1. angebot(): Higgsfield nennt den exakten Preis (POST /estimate/...). Your Engine zeigt ihn mit Aufschlag
     (dieselbe Holder-Staffel wie bei den KI-Modellen, ai.markup) und fragt nach.
  2. starten(): erst nach "ja". Betrag wird reserviert, die Anfrage geht raus (mit Idempotency-Key).
  3. Im Hintergrund: Status abfragen (2 bis 10 s), fertig -> Video als Telegram-Video, sonst Link im Web-Posteingang.
     failed / nsfw / canceled -> Betrag zurueck. Higgsfield berechnet in dem Fall auch nichts.
  Jede Abrechnung steht in ai_usage (model "video:<id>", cost_or = Higgsfield-Preis, charged = mit Aufschlag),
  wie bei den KI-Modellen, damit Umsatz, Marge und Rueckkauf mitlaufen.

Railway (web):
  HIGGSFIELD_API_KEY  der API-Key aus open.higgsfield.ai (API keys, "Copy API key"), so wie er angezeigt wird.
                      Alternativ getrennt: HIGGSFIELD_API_KEY_ID und HIGGSFIELD_API_KEY_SECRET
  AIVIDEO_MARKUP      eigener Aufschlag fuer Videos (0.2 = 20 %), ohne: Holder-Staffel der KI-Modelle
  AIVIDEO_MAX_USD     hoechster Preis je Video fuer den Nutzer, Standard 15
  AIVIDEO_DAY_MAX     Videos je Nutzer und Tag, Standard 10
  AIVIDEO_TIMEOUT_MIN nach so vielen Minuten ohne Ergebnis aufgeben und erstatten, Standard 25
Schalter auf admin.html: "Your Engine: AI video (Higgsfield)".
"""
import base64
import json
import os
import re
import secrets
import threading
import time

import requests

import storage
from logger import log

API = "https://api.higgsfield.ai"
ENDE = {"completed", "failed", "nsfw", "canceled"}

# Modell -> Name, Endpunkte (Text, Bild), Dauer (min, max oder feste Werte), Zusatzfelder
MODELLE = {
    "kling-4": {
        "name": "Kling 4.0", "info": "newest Kling, most realistic, up to 30 s, 1080p",
        "text": None, "bild": None, "suche": "kling4",
        "dauer": (3, 30), "std": 5, "seiten": ("16:9", "9:16", "1:1", "21:9"), "aufloesung": ("720p", "1080p"),
    },
    "kling-3-standard": {
        "name": "Kling 3.0 Standard", "info": "Kling 3.0 at a lower price",
        "text": None, "bild": None, "suche": "kling3std",
        "dauer": (3, 15), "std": 5, "seiten": ("16:9", "9:16", "1:1"),
    },
    "kling-2.6": {
        "name": "Kling 2.6 Pro", "info": "cinematic motion, strong physics, 5 or 10 s",
        "text": None, "bild": None, "suche": "kling26",
        "dauer": (5, 10), "fest": (5, 10), "std": 5, "seiten": ("16:9", "9:16", "1:1"),
    },
    "kling-2.5": {
        "name": "Kling 2.5 Turbo", "info": "cheap and quick, 5 or 10 s",
        "text": None, "bild": None, "suche": "kling25",
        "dauer": (5, 10), "fest": (5, 10), "std": 5, "seiten": ("16:9", "9:16", "1:1"),
    },
    "seedance-2.0": {
        "name": "Seedance 2.0", "info": "ByteDance, the only one up to 4K, 4 to 15 s",
        "text": None, "bild": None, "suche": "seedance20",
        "dauer": (4, 15), "std": 5, "seiten": ("16:9", "9:16", "1:1"), "aufloesung": ("720p", "1080p", "4k"),
        "seite_nur_text": True,
    },
    "wan-3-prime": {
        "name": "Wan 3.0 Prime", "info": "Alibaba, sharper Wan, up to 30 s with audio",
        "text": None, "bild": None, "suche": "wan3prime",
        "dauer": (2, 30), "std": 5, "seiten": ("16:9", "9:16", "1:1"), "aufloesung": ("720p", "1080p"),
        "ton": "generate_audio",
    },
    "minimax-h3": {
        "name": "MiniMax H3", "info": "newest MiniMax, follow-up to Hailuo",
        "text": None, "bild": None, "suche": "minimaxh3",
        "dauer": (6, 10), "fest": (6, 10), "std": 6, "seiten": (),
    },
    "veo-3.1": {
        "name": "Veo 3.1", "info": "Google, ultra realistic, native sound",
        "text": None, "bild": None, "suche": "veo31",
        "dauer": (4, 8), "fest": (4, 6, 8), "std": 8, "seiten": ("16:9", "9:16"), "ton": "generate_audio",
    },
    "kling-3-pro": {
        "name": "Kling 3.0 Pro", "info": "best quality, cinematic motion, native sound",
        "text": "kling-video/v3.0/pro/text-to-video", "bild": "kling-video/v3.0/pro/image-to-video",
        "dauer": (3, 15), "std": 5, "seiten": ("16:9", "9:16", "1:1"), "ton": "sound",
    },
    "kling-3-turbo": {
        "name": "Kling 3.0 Turbo", "info": "fast and cheaper, up to 1080p",
        "text": "kling-video/v3.0-turbo/text-to-video", "bild": "kling-video/v3.0-turbo/image-to-video",
        "dauer": (3, 15), "std": 5, "seiten": ("16:9", "9:16", "1:1"), "aufloesung": ("720p", "1080p"),
    },
    "seedance-2.5": {
        "name": "Seedance 2.5", "info": "ByteDance, long clips up to 30 s, native audio",
        "text": "bytedance/seedance-2.5/text-to-video", "bild": "bytedance/seedance-2.5/image-to-video",
        "dauer": (4, 30), "std": 5, "seiten": ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"),
        "aufloesung": ("480p", "720p", "1080p"), "ton": "generate_audio", "seite_nur_text": True,
    },
    "hailuo-2.3": {
        "name": "Hailuo 2.3", "info": "MiniMax, cheapest, 6 or 10 s, no sound",
        "text": "minimax/hailuo-2.3/standard/text-to-video", "bild": "minimax/hailuo-2.3/standard/image-to-video",
        "dauer": (6, 10), "fest": (6, 10), "std": 6, "seiten": (),
    },
    "wan-3": {
        "name": "Wan 3.0", "info": "Alibaba, up to 30 s, 1080p, native audio",
        "text": "alibaba/wan-3.0/text-to-video", "bild": "alibaba/wan-3.0/image-to-video",
        "dauer": (2, 30), "std": 5, "seiten": ("16:9", "9:16", "1:1", "4:3", "3:4"),
        "aufloesung": ("480p", "720p", "1080p"), "ton": "generate_audio",
    },
}
STANDARD = "kling-3-turbo"
_LETZTES_BILD = {}      # adr -> (ts, data-URL), das letzte Foto aus dem Chat, 30 Minuten gueltig
_ANGEBOTE = {}          # adr -> (ts, angebot), damit "ja" genau das bestaetigte Video startet
_LAEUFT = {}
_PFAD = {}              # (modell, art) -> gefundener Endpunkt

# moegliche API-Pfade, wenn Higgsfield sie nicht dokumentiert (Reihenfolge = Wahrscheinlichkeit)
KANDIDATEN = {
    "kling3std": ["kling-video/v3.0/standard/{art}", "kling-video/v3.0-standard/{art}"],
    "kling26": ["kling-video/v2.6/pro/{art}", "kling-video/v2.6-pro/{art}", "kling-video/v2.6/{art}"],
    "kling25": ["kling-video/v2.5-turbo/pro/{art}", "kling-video/v2.5-turbo/{art}", "kling-video/v2.5/turbo/pro/{art}",
                "kling-video/v2.5-turbo/standard/{art}"],
    "seedance20": ["bytedance/seedance-2.0/{art}", "bytedance/seedance-2.0/pro/{art}", "bytedance/seedance-2/{art}"],
    "wan3prime": ["alibaba/wan-3.0-prime/{art}", "alibaba/wan-3.0/prime/{art}", "alibaba/wan-3-prime/{art}"],
    "minimaxh3": ["minimax/h3/{art}", "minimax/h3/standard/{art}", "minimax/hailuo-h3/{art}", "minimax/minimax-h3/{art}"],
    "veo31": ["google/veo-3.1/{art}", "google/veo3.1/{art}", "google/veo-3.1/standard/{art}", "google/veo-3-1/{art}"],
    "kling4": [
        "kling-video/v4.0/{art}", "kling-video/v4.0/pro/{art}", "kling-video/v4.0/standard/{art}",
        "kling-video/v4/{art}", "kling-video/v4-pro/{art}", "kling-video/v4.0-pro/{art}", "kling/v4.0/{art}",
        "kling-video/4.0/{art}",
    ],
}


def _f(k, d):
    try:
        return float(os.getenv(k, "") or d)
    except Exception:
        return float(d)


def _schluessel():
    """Der Wert hinter "Key " im Authorization-Kopf. Die Console zeigt einen einzigen API-Key (meist "id:secret"),
    alternativ getrennt als HIGGSFIELD_API_KEY_ID und HIGGSFIELD_API_KEY_SECRET."""
    kid = os.getenv("HIGGSFIELD_API_KEY_ID", "").strip()
    sec = os.getenv("HIGGSFIELD_API_KEY_SECRET", "").strip()
    if kid and sec:
        return f"{kid}:{sec}"
    roh = os.getenv("HIGGSFIELD_API_KEY", "").strip()
    if roh.lower().startswith("key "):
        roh = roh[4:].strip()
    return roh or None


def _jobs_key(adr):
    return "aivideo_jobs_" + str(adr).lower()


def jobs(adr):
    """Die letzten Videos des Nutzers (fuer die Video-Seite), neueste zuerst."""
    try:
        j = json.loads(storage.kv_get(_jobs_key(adr)) or "[]")
    except Exception:
        j = []
    jetzt = time.time()
    for x in j:  # haengengebliebene Auftraege nach dem Timeout als fehlgeschlagen zeigen
        if x.get("status") == "rendering" and jetzt - float(x.get("ts") or 0) > _f("AIVIDEO_TIMEOUT_MIN", 25) * 60 + 300:
            x["status"] = "failed"
    return j


def _job_setzen(adr, jid, **felder):
    try:
        j = jobs(adr)
        for x in j:
            if x.get("id") == jid:
                x.update(felder)
                break
        else:
            j.insert(0, dict(id=jid, **felder))
        storage.kv_set(_jobs_key(adr), json.dumps(j[:30]))
    except Exception as e:
        log.warning(f"kivideo job: {e}")


def bereit():
    return _schluessel() is not None


def an(adr=None):
    try:
        import features
        import motor
        return bereit() and (features.an("aivideo") or (adr is not None and motor.beta(adr)))
    except Exception:
        return False


def _kopf(idem=None):
    k = {"Authorization": f"Key {_schluessel()}", "Content-Type": "application/json"}
    if idem:
        k["Idempotency-Key"] = idem
    return k


def bild_merken(adr, dataurl):
    """Aus motor.frage: das letzte Foto des Nutzers, fuer 'mach daraus ein Video'."""
    if isinstance(dataurl, str) and dataurl.startswith("data:image/"):
        _LETZTES_BILD[str(adr).lower()] = (time.time(), dataurl)


def _letztes_bild(adr):
    v = _LETZTES_BILD.get(str(adr).lower())
    if v and time.time() - v[0] < 1800:
        return v[1]
    return None


_TMP = {}              # token -> (ts, bytes, mime): Foto fuer Higgsfield ueber unsere eigene URL (Rueckfall)

ABSICHT = re.compile(r"\b(video|clip|animat\w*|make (it|this|him|her|them) (move|alive)|bring (it|this) to life|"
                     r"motion|bewegen|beweg\w*|animier\w*|lebendig|film)\b", re.I)


def will_video(text):
    return bool(ABSICHT.search(str(text or "")))


def tmp_bild(token):
    v = _TMP.get(token)
    if v and time.time() - v[0] < 3600:
        return v[1], v[2]
    return None


def _eigene_url(daten, mime):
    for t in [t for t, v in _TMP.items() if time.time() - v[0] > 3600]:
        _TMP.pop(t, None)
    token = secrets.token_urlsafe(18)
    _TMP[token] = (time.time(), daten, mime)
    basis = (os.getenv("PUBLIC_API_URL", "").strip() or "https://api.oddsengine.live").rstrip("/")
    return f"{basis}/v1/engine/vidimg/{token}." + ("png" if mime == "image/png" else "webp" if mime == "image/webp" else "jpg")


def _hochladen(dataurl):
    """Foto zu Higgsfield (presigned Upload) -> oeffentliche URL fuer image_url.
    Klappt der Upload bei Higgsfield nicht, bekommt Higgsfield das Foto ueber unsere eigene URL (1 Stunde gueltig)."""
    m = re.match(r"data:(image/[a-z+.-]+);base64,(.*)", dataurl, re.S)
    if not m:
        raise ValueError("photo not readable")
    mime = {"image/jpg": "image/jpeg"}.get(m.group(1), m.group(1))
    if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        mime = "image/jpeg"
    daten = base64.b64decode(re.sub(r"\s", "", m.group(2)))
    try:
        r = requests.post(API + "/files/generate-upload-url", headers=_kopf(), json={"content_type": mime}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"upload-url {r.status_code}: {r.text[:200]}")
        d = r.json()
        put = requests.put(d["upload_url"], data=daten, headers=d.get("upload_headers") or {"Content-Type": mime}, timeout=60)
        if put.status_code >= 400:
            raise RuntimeError(f"put {put.status_code}: {put.text[:200]}")
        return d["public_url"]
    except Exception as e:
        log.warning(f"kivideo higgsfield-upload, nehme eigene URL: {e}")
        return _eigene_url(daten, mime)


def _modell(name):
    n = re.sub(r"[^a-z0-9.]", "", str(name or "").lower())
    if not n:
        return STANDARD
    if "kling4" in n or "klingv4" in n or n in ("kling4.0", "4.0", "4"):
        return "kling-4"
    if "kling2.6" in n or "kling26" in n:
        return "kling-2.6"
    if "kling2.5" in n or "kling25" in n:
        return "kling-2.5"
    if "kling" in n and ("standard" in n or "std" in n):
        return "kling-3-standard"
    if "kling" in n:
        return "kling-3-pro" if "pro" in n else "kling-3-turbo"
    if "veo" in n:
        return "veo-3.1"
    if "h3" in n or ("minimax" in n and "hailuo" not in n):
        return "minimax-h3"
    if "seedance2.0" in n or "seedance20" in n or n == "seedance2":
        return "seedance-2.0"
    if "wan" in n and "prime" in n:
        return "wan-3-prime"
    for k, m in MODELLE.items():
        if n in re.sub(r"[^a-z0-9.]", "", k) or n in re.sub(r"[^a-z0-9.]", "", m["name"].lower()):
            return k
    if "kling" in n:
        return "kling-3-pro" if "pro" in n else "kling-3-turbo"
    return None


def _body(k, prompt, dauer, seite, aufl, ton, bild_url):
    m = MODELLE[k]
    lo, hi = m["dauer"]
    d = int(dauer or m["std"])
    if m.get("fest"):
        d = min(m["fest"], key=lambda x: abs(x - d))
    d = max(lo, min(hi, d))
    b = {"prompt": str(prompt or "")[:2400], "duration": d}
    if bild_url:
        b["image_url"] = bild_url
    if m["seiten"] and seite in m["seiten"] and not (bild_url and m.get("seite_nur_text")) and not (bild_url and k.startswith("kling")):
        b["aspect_ratio"] = seite
    if m.get("aufloesung"):
        b["resolution"] = aufl if aufl in m["aufloesung"] else ("1080p" if "1080p" in m["aufloesung"] else m["aufloesung"][-1])
    if m.get("ton") == "sound":
        b["sound"] = "on" if ton is not False else "off"
    elif m.get("ton") == "generate_audio":
        b["generate_audio"] = ton is not False
    return b


def _aufschlag(adr):
    """AIVIDEO_MARKUP (z. B. 0.2 = 20 %) fuer Videos, sonst dieselbe Holder-Staffel wie bei den KI-Modellen.
    Higgsfield-Rabatte (Max-Discount, Aktionen) stecken schon im geschaetzten Einkaufspreis."""
    import ai
    roh = os.getenv("AIVIDEO_MARKUP", "").strip()
    if roh:
        try:
            return max(0.0, min(2.0, float(roh)))
        except Exception:
            pass
    return ai.markup(adr)


def _schaetzen(endpunkt, body):
    r = requests.post(f"{API}/estimate/{endpunkt}", headers=_kopf(), json=body, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"estimate {r.status_code}: {r.text[:200]}")
    return float(r.json().get("usd"))


def _endpunkt(k, mit_bild, body):
    """Endpunkt fuer Modell k. Bei Modellen ohne dokumentierten Pfad: Env-Vorgabe, gemerkter Pfad oder Suche
    ueber /estimate (kostet nichts). Gibt (endpunkt, usd) zurueck, usd None wenn noch nicht geschaetzt."""
    m = MODELLE[k]
    art = "image-to-video" if mit_bild else "text-to-video"
    fest = m["bild" if mit_bild else "text"]
    if fest:
        return fest, None
    name_env = ("AIVIDEO_KLING4_IMAGE" if mit_bild else "AIVIDEO_KLING4_TEXT") if k == "kling-4" else \
        f"AIVIDEO_PATH_{re.sub(r'[^A-Z0-9]', '_', k.upper())}_{'IMAGE' if mit_bild else 'TEXT'}"
    env = os.getenv(name_env, "").strip().strip("/")
    if env:
        return env.replace("https://api.higgsfield.ai/", ""), None
    schl = f"kivideo_pfad_{k}_{art}"
    gemerkt = _PFAD.get((k, art))
    if not gemerkt:
        try:
            gemerkt = storage.kv_get(schl)
        except Exception:
            gemerkt = None
    if gemerkt:
        return gemerkt, None
    minimal = {"prompt": body["prompt"], "duration": body["duration"]}
    if body.get("image_url"):
        minimal["image_url"] = body["image_url"]
    for muster in KANDIDATEN.get(m["suche"], []):
        pfad = muster.format(art=art)
        for b in (body, minimal):
            try:
                r = requests.post(f"{API}/estimate/{pfad}", headers=_kopf(), json=b, timeout=20)
            except Exception:
                break
            if r.status_code in (404, 405):
                break
            if r.status_code < 400:
                try:
                    usd = float(r.json().get("usd"))
                except Exception:
                    break
                _PFAD[(k, art)] = pfad
                try:
                    storage.kv_set(schl, pfad)
                except Exception:
                    pass
                log.info(f"kivideo: {m['name']} {art} gefunden unter {pfad}")
                return pfad, (usd, b)
    return None, None


def modelle():
    """Fuer die Antwort 'welche Video-Modelle gibt es' (nur die, deren API-Weg bestaetigt ist), mit Preis."""
    return [{"id": x["id"], "name": x["name"], "about": x["about"],
             "seconds": " or ".join(str(v) for v in x["fixed"]) if x["fixed"] else f"{x['min']} to {x['max']}",
             "price": f"about ${x['price_usd']:.2f} for {x['price_seconds']} s" if x.get("price_usd") else "shown before start",
             "photo_to_video": True} for x in modelle_seite()]


def angebot(adr, prompt, model=None, seconds=None, aspect=None, resolution=None, sound=None, from_photo=False):
    """Preis holen, noch nichts starten. from_photo=None: automatisch, wenn ein frisches Foto da ist."""
    import ai
    if not bereit():
        return {"error": "AI video is not set up yet."}
    if not str(prompt or "").strip() and not from_photo:
        return {"error": "Describe the video you want."}
    k = _modell(model)
    if not k:
        return {"error": f"No video model matches '{model}'.", "video_models": modelle()}
    bild = _letztes_bild(adr) if from_photo else None
    if not prompt or not str(prompt).strip():
        prompt = "Bring this picture to life with natural, cinematic motion and a slow camera push in."
    if from_photo and not bild:
        return {"error": "Send the photo first (in the last 30 minutes), then ask for the video."}
    try:
        bild_url = _hochladen(bild) if bild else None
    except Exception as e:
        log.warning(f"kivideo foto: {e}")
        return {"error": "Could not read that photo. Send it again as a normal photo (jpg or png)."}
    body = _body(k, prompt, seconds, aspect or "16:9", resolution, sound, bild_url)
    endpunkt, gefunden = _endpunkt(k, bool(bild), body)
    if not endpunkt:
        return {"error": f"{MODELLE[k]['name']} is not open on our video API yet. Kling 3.0 Pro is the best one "
                         "available right now. Want that instead?", "video_models": modelle()}
    try:
        if gefunden:
            usd, body = gefunden
        else:
            try:
                usd = _schaetzen(endpunkt, body)
            except Exception:
                if not MODELLE[k].get("suche"):
                    raise
                body = {x: body[x] for x in ("prompt", "duration", "image_url") if x in body}
                usd = _schaetzen(endpunkt, body)
    except Exception as e:
        log.warning(f"kivideo schaetzen: {e}")
        return {"error": "Could not get a price for this video right now. Try again in a moment or pick another model."}
    preis = round(usd * (1 + _aufschlag(adr)), 2)
    if preis > _f("AIVIDEO_MAX_USD", 15):
        return {"error": f"That video would cost ${preis:.2f}, above the limit of ${_f('AIVIDEO_MAX_USD', 15):.0f}. "
                         "Make it shorter, use a lower resolution or a cheaper model."}
    a = {"model": k, "endpunkt": endpunkt, "body": body, "usd": usd, "preis": preis, "foto": bool(bild)}
    _ANGEBOTE[str(adr).lower()] = (time.time(), a)
    kt = ai.konto(adr) or {}
    return {"ok": True, "model": MODELLE[k]["name"], "seconds": body["duration"],
            "aspect": body.get("aspect_ratio") or ("from the photo" if bild else "16:9"),
            "resolution": body.get("resolution"), "sound": body.get("sound", body.get("generate_audio")),
            "from_photo": bool(bild), "price_usd": preis, "balance_usd": round(float(kt.get("credit") or 0), 2),
            "how_to_answer": f"Tell the user: {MODELLE[k]['name']}, {body['duration']} s, price ${preis:.2f} from the balance. "
                             "Ask if you should start it. Only after a clear yes call make_ai_video again with confirm=true. "
                             "Paid only if it works, a failed video costs nothing."}


def angebot_seite(adr, b):
    """Von der Video-Seite: optional ein Foto (data-URL) mitschicken."""
    bild = b.get("image")
    if isinstance(bild, str) and bild.startswith("data:image/") and len(bild) < 9_000_000:
        bild_merken(adr, bild)
        mit = True
    else:
        mit = False
    r = angebot(adr, b.get("prompt"), b.get("model"), b.get("seconds"), b.get("aspect"), b.get("resolution"),
                b.get("sound"), mit)
    r.pop("how_to_answer", None)
    return r


_PREISE = {"ts": 0.0, "laeuft": False, "d": {}}


def _preise_holen():
    """Je Modell: Einkaufspreis fuer den Standard-Clip (Text zu Video) ueber /estimate, kostet nichts.
    Modelle ohne gefundenen Pfad gelten als nicht verfuegbar. Laeuft im Hintergrund, alle 6 Stunden."""
    try:
        d = {}
        for k, m in MODELLE.items():
            # Ab-Preis wie bei Higgsfield: niedrigste Aufloesung, Standardlaenge
            body = _body(k, "A calm city street at night, slow camera move.", m["std"], "16:9",
                         (m.get("aufloesung") or (None,))[0], None, None)
            try:
                pfad, gef = _endpunkt(k, False, body)
                if not pfad:
                    d[k] = {"ok": False}
                    continue
                usd = gef[0] if gef else None
                if usd is None:
                    try:
                        usd = _schaetzen(pfad, body)
                    except Exception:
                        usd = _schaetzen(pfad, {x: body[x] for x in ("prompt", "duration")})
                d[k] = {"ok": True, "usd": usd, "sec": body["duration"], "res": body.get("resolution")}
            except Exception as e:
                log.debug(f"kivideo preis {k}: {e}")
                d[k] = {"ok": None}
        if any(v.get("ok") for v in d.values()):
            _PREISE.update(d=d, ts=time.time())
            try:
                storage.kv_set("kivideo_preise", json.dumps({"ts": _PREISE["ts"], "d": d}))
            except Exception:
                pass
    finally:
        _PREISE["laeuft"] = False


def _preise():
    if not _PREISE["d"]:
        try:
            alt = json.loads(storage.kv_get("kivideo_preise") or "null")
            if alt:
                _PREISE.update(d=alt["d"], ts=float(alt["ts"]))
        except Exception:
            pass
    if bereit() and time.time() - _PREISE["ts"] > 6 * 3600 and not _PREISE["laeuft"]:
        _PREISE["laeuft"] = True
        threading.Thread(target=_preise_holen, daemon=True).start()
    return _PREISE["d"]


def modelle_seite(adr=None):
    """Modelle fuer die Seite, mit Preis fuer den Nutzer (inkl. Aufschlag). Nicht gefundene Modelle fehlen."""
    pr = _preise()
    try:
        auf = _aufschlag(adr) if adr else _aufschlag(None)
    except Exception:
        auf = 0.08
    out = []
    for k, m in MODELLE.items():
        p = pr.get(k) or {}
        if m.get("suche") and p.get("ok") is not True:
            continue  # Pfad noch nicht bestaetigt: lieber nicht zeigen
        usd = p.get("usd")
        preis = round(usd * (1 + auf), 2) if usd else None
        out.append({"id": k, "name": m["name"], "about": m["info"], "min": m["dauer"][0], "max": m["dauer"][1],
                    "fixed": list(m["fest"]) if m.get("fest") else None, "default": m["std"],
                    "aspects": list(m["seiten"]), "resolutions": list(m.get("aufloesung") or ()),
                    "sound": bool(m.get("ton")), "default_model": k == STANDARD,
                    "price_usd": preis, "price_seconds": p.get("sec"), "price_res": p.get("res"),
                    "per_second_usd": round(preis / p["sec"], 3) if preis and p.get("sec") else None})
    return out


def starten(adr, chat_id=None, quelle="tg"):
    """Das zuletzt angebotene Video starten (nach 'ja'). quelle="page": nur auf der Video-Seite zeigen."""
    import ai
    v = _ANGEBOTE.get(str(adr).lower())
    if not v or time.time() - v[0] > 900:
        return {"error": "No video waiting for confirmation. Ask for the video again."}
    a = v[1]
    tag = time.strftime("%Y-%m-%d", time.gmtime())
    zkey = f"aivideo_{str(adr).lower()}_{tag}"
    try:
        n = int(storage.kv_get(zkey) or 0)
    except Exception:
        n = 0
    if n >= int(_f("AIVIDEO_DAY_MAX", 10)):
        return {"error": "Daily limit for AI videos reached. Try again tomorrow."}
    if time.time() - _LAEUFT.get(str(adr).lower(), 0) < 30:
        return {"error": "A video was just started. Give it a moment."}
    if not ai._reservieren(adr, a["preis"]):
        return {"error": f"Your balance is too low for this video (${a['preis']:.2f}). Top up on engine.oddsengine.live."}
    _ANGEBOTE.pop(str(adr).lower(), None)
    _LAEUFT[str(adr).lower()] = time.time()
    try:
        storage.kv_set(zkey, str(n + 1))
    except Exception:
        pass
    body = dict(a["body"])
    if chat_id is None and quelle != "page":
        try:
            import motor
            chat_id = (motor._tg_von(adr) or {}).get("tg")
        except Exception:
            chat_id = None
    try:
        idem = "oe-" + secrets.token_hex(12)
        r = requests.post(f"{API}/{a['endpunkt']}", headers=_kopf(idem), json=body, timeout=60)
        d = r.json() if r.content else {}
        if r.status_code >= 400 or not d.get("request_id"):
            raise RuntimeError(f"{r.status_code}: {str(d)[:200]}")
    except Exception as e:
        log.warning(f"kivideo start {adr}: {e}")
        ai._ausgleichen(adr, a["preis"], 0.0)
        return {"error": "The video could not be started. Nothing was charged. Try again in a moment."}
    _job_setzen(adr, d["request_id"], model=MODELLE[a["model"]]["name"], seconds=a["body"]["duration"],
                price_usd=a["preis"], status="rendering", url=None, ts=time.time(),
                prompt=str(a["body"].get("prompt") or "")[:300], photo=bool(a.get("foto")))
    threading.Thread(target=_warten, args=(adr, chat_id, a, d, quelle), daemon=True).start()
    return {"started": True, "id": d["request_id"], "model": MODELLE[a["model"]]["name"], "price_usd": a["preis"],
            "how_to_answer": "Say it is rendering now, usually 1 to 5 minutes. It arrives as its own message in Telegram and "
                             "under Video on engine.oddsengine.live. Do not invent anything about the result."}


def _senden(adr, chat_id, url, text):
    import motor
    from config import cfg
    if chat_id:
        try:
            r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendVideo",
                              data={"chat_id": chat_id, "video": url, "caption": text[:1000], "supports_streaming": "true"},
                              timeout=120)
            if r.ok and r.json().get("ok"):
                return
            daten = requests.get(url, timeout=120).content
            r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendVideo",
                              data={"chat_id": chat_id, "caption": text[:1000], "supports_streaming": "true"},
                              files={"video": ("video.mp4", daten, "video/mp4")}, timeout=300)
            if r.ok and r.json().get("ok"):
                return
        except Exception as e:
            log.warning(f"kivideo telegram: {e}")
    motor.senden(adr, f"{text}\n\nDownload (link works for 7 days): {url}")


def _warten(adr, chat_id, a, d, quelle="tg"):
    import ai
    import motor
    url = d.get("status_url") or f"{API}/requests/{d['request_id']}/status"
    t0, pause, st = time.time(), 2.0, {}
    grenze = _f("AIVIDEO_TIMEOUT_MIN", 25) * 60
    while time.time() - t0 < grenze:
        time.sleep(pause)
        pause = min(10.0, pause * 1.5)
        try:
            r = requests.get(url, headers=_kopf(), timeout=30)
            if r.status_code in (401, 404):
                st = {"status": "failed", "error": f"status {r.status_code}"}
                break
            st = r.json()
        except Exception:
            continue
        if st.get("status") in ENDE:
            break
    name = MODELLE[a["model"]]["name"]
    video = ((st or {}).get("video") or {}).get("url")
    if st.get("status") == "completed" and video:
        try:
            ai._ausgleichen(adr, a["preis"], a["preis"])
            from decimal import Decimal
            import holder
            ai._pg().execute("""INSERT INTO ai_usage (address, key_id, model, tokens_in, tokens_out, cost_or, charged,
                                gen_id, status) VALUES (%s,%s,%s,0,0,%s,%s,%s,'ok')""",
                             (holder.norm(adr), None, "video:" + a["model"], Decimal(str(round(a["usd"], 6))),
                              Decimal(str(round(a["preis"], 6))), d.get("request_id")))
        except Exception as e:
            log.warning(f"kivideo abrechnen: {e}")
        _job_setzen(adr, d["request_id"], status="completed", url=video, done=time.time())
        if quelle != "page":
            _senden(adr, chat_id, video, f"🎬 {name} · {a['body']['duration']} s · ${a['preis']:.2f}")
        return
    # nicht fertig geworden: alles zurueck
    ai._ausgleichen(adr, a["preis"], 0.0)
    if st.get("status") == "completed":
        grund = "it finished without a video file"
    else:
        grund = {"nsfw": "the content filter blocked it", "canceled": "it was canceled",
                 "failed": "the model could not make it"}.get(st.get("status"), "it took too long")
    if st.get("status") not in ENDE:
        try:
            requests.post(f"{API}/requests/{d['request_id']}/cancel", headers=_kopf(), timeout=20)
        except Exception:
            pass
    _job_setzen(adr, d["request_id"], status="failed", reason=grund, done=time.time())
    if quelle != "page":
        motor.senden(adr, f"The {name} video did not work: {grund}. Nothing was charged, your ${a['preis']:.2f} are back.")
