"""
x_post.py: postet Signale und Ergebnisse direkt auf X.

X API v2, POST /2/tweets, angemeldet als der eigene Account (OAuth 1.0a,
User Context). Die vier Schluessel aus dem X Developer Portal liegen
ausschliesslich in Railway, nie im Repo:

    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET

Kosten (Stand Sept. 2026, Guthaben im Voraus): 0,015 $ je Post ohne Link,
0,20 $ mit Link. Deshalb enthalten die Posts bewusst KEINEN Link.
Ergebnisse gehen als Antwort unter das Signal, damit beides zusammensteht.
"""
import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse
from datetime import datetime, timezone

import requests

import storage
from config import cfg
from logger import log

URL = "https://api.twitter.com/2/tweets"


def bereit():
    return bool(cfg.x_post_enabled and cfg.x_api_key and cfg.x_api_secret
                and cfg.x_access_token and cfg.x_access_secret)


def status():
    """Ein Satz fuers Log: postet X, und wenn nicht, woran es haengt.
    Nennt nur die NAMEN fehlender Variablen, nie einen Wert."""
    if not cfg.x_post_enabled:
        return "aus (X_POST_ENABLED ist nicht true)"
    fehlt = [n for n, v in (("X_API_KEY", cfg.x_api_key), ("X_API_SECRET", cfg.x_api_secret),
                            ("X_ACCESS_TOKEN", cfg.x_access_token),
                            ("X_ACCESS_SECRET", cfg.x_access_secret)) if not v]
    if fehlt:
        return "aus (fehlt: " + ", ".join(fehlt) + ")"
    return f"an, hoechstens {cfg.x_max_per_day} Posts am Tag, heute bisher {_heute_gepostet()}"


def _q(s):
    return urllib.parse.quote(str(s), safe="~")


def _auth_header(method, url):
    o = {
        "oauth_consumer_key": cfg.x_api_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": cfg.x_access_token,
        "oauth_version": "1.0",
    }
    # JSON-Body zaehlt bei OAuth 1.0a nicht zur Signatur.
    param = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(o.items()))
    basis = "&".join([method.upper(), _q(url), _q(param)])
    schluessel = f"{_q(cfg.x_api_secret)}&{_q(cfg.x_access_secret)}"
    sig = base64.b64encode(hmac.new(schluessel.encode(), basis.encode(),
                                    hashlib.sha1).digest()).decode()
    o["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(o.items()))


def _schluessel(topf):
    heute = datetime.now(timezone.utc).date().isoformat()
    # Seit 2.69.3: die Scan-Antworten (x_scan) zaehlen in einem eigenen Topf. Vorher konnten
    # sie das Tageslimit aufbrauchen, und Wetter-Calls und Ergebnisse gingen nicht mehr raus.
    return f"x_posts_{heute}" if topf == "main" else f"x_posts_{topf}_{heute}"


def _heute_gepostet(topf="main"):
    try:
        return int(storage.kv_get(_schluessel(topf)) or 0)
    except Exception:
        return 0


def _zaehlen(topf="main"):
    try:
        storage.kv_set(_schluessel(topf), _heute_gepostet(topf) + 1)
    except Exception:
        pass


def _melden(grund):
    """Seit 2.69.3: scheitert ein X-Post, kommt sofort eine Nachricht in den privaten Telegram-Chat,
    hoechstens einmal je Fehlerart und Stunde. Vorher stand der Grund nur im Log."""
    art = grund.split(":")[0][:40]
    k = "x_alarm_" + "".join(ch for ch in art if ch.isalnum() or ch in "_ ").replace(" ", "_")
    try:
        letzt = float(storage.kv_get(k) or 0)
    except Exception:
        letzt = 0.0
    if time.time() - letzt < 3600:
        return
    try:
        storage.kv_set(k, time.time())
    except Exception:
        pass
    if not (cfg.tg_token and cfg.tg_private):
        return
    hilfe = ""
    if "402" in grund or "redit" in grund:
        hilfe = "\nLooks like the X credits are used up. Top up in the X Developer Portal under Billing."
    elif "401" in grund or "403" in grund:
        hilfe = "\nCheck the app permission (Read and write) and the four X keys on Railway."
    elif "429" in grund:
        hilfe = "\nX rate limit. It retries on the next post."
    elif "Tageslimit" in grund:
        hilfe = "\nRaise X_MAX_PER_DAY on Railway if you want more posts per day."
    try:
        requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage",
                      json={"chat_id": cfg.tg_private, "disable_web_page_preview": True,
                            "text": "X post failed: " + grund[:300] + hilfe}, timeout=15)
    except Exception as e:
        log.warning(f"x alarm: {e}")


def post(text, reply_to=None, quote=None, topf="main"):
    """Postet auf X. Gibt die Post-ID zurueck, sonst None.
    topf="scan" fuer die Scan-Antworten: eigener Zaehler, ihr Limit steht in x_scan."""
    if not bereit():
        return None
    if topf == "main" and _heute_gepostet() >= cfg.x_max_per_day:
        log.warning(f"x: Tageslimit {cfg.x_max_per_day} erreicht, nicht gepostet")
        _melden(f"Tageslimit {cfg.x_max_per_day} erreicht")
        _protokoll(topf, False, text, f"daily limit {cfg.x_max_per_day} reached")
        return None
    body = {"text": text[:280]}
    if quote:
        # Zitat statt Antwort: steht im Profil unter "Posts", mit dem Call
        # sichtbar darunter. Antworten sieht man nur im Reiter "Replies".
        body["quote_tweet_id"] = str(quote)
    elif reply_to:
        body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    try:
        r = requests.post(URL, json=body, timeout=20,
                          headers={"Authorization": _auth_header("POST", URL)})
        if r.status_code in (200, 201):
            tid = (r.json().get("data") or {}).get("id")
            _zaehlen(topf)
            log.info(f"x: gepostet {tid}" + (f" (zitiert {quote})" if quote else f" (Antwort auf {reply_to})" if reply_to else ""))
            _protokoll(topf, True, text, ref=tid)
            return tid
        log.error(f"x {r.status_code}: {r.text[:300]}")
        _melden(f"{r.status_code}: {r.text[:200]}")
        _protokoll(topf, False, text, f"{r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"x: {e}")
        _melden(f"network: {e}")
        _protokoll(topf, False, text, f"network: {e}")
    return None


def _protokoll(topf, ok, text, fehler=None, ref=None):
    if topf != "main":
        return
    try:
        import postlog
        t = str(text or "")
        art = "result" if ("HIT" in t[:30] or "MISS" in t[:30]) else "call" if "daily high" in t else "other"
        postlog.eintragen("x", ok, art, fehler, ref)
    except Exception:
        pass
