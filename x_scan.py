"""
x_scan.py: Your Engine antwortet auf X auf gepostete Contracts (seit 2.49.3).

Wer unter irgendeinen unserer Posts eine CA schreibt (oder uns irgendwo mit einer CA erwaehnt), bekommt den
Kurz-Scan als Antwort. Gelesen wird die eigene Erwaehnungs-Liste, darin stehen alle Antworten auf unsere Posts.
Begrenzt, damit der Account nicht nach Spam aussieht und nichts unerwartet kostet:
  X_SCAN_ENABLED    true schaltet es ein (Standard aus)
  X_SCAN_PER_POST   hoechstens so viele Antworten unter einem Post (Standard 5)
  X_SCAN_PER_DAY    hoechstens so viele Antworten am Tag insgesamt (Standard 20)
  X_SCAN_POLL_SEC   wie oft nachgesehen wird (Standard 60)
Je Person eine Antwort am Tag, jede CA einmal am Tag. Antworten ohne Link (ein Link kostet bei X 0,20 $
statt 0,015 $). Beim Einschalten wird nichts Altes nachgeholt, nur was danach kommt. Gelesen wird nur Neues
(since_id). Nutzt dieselben vier X-Schluessel wie x_post.py. Scheitert ein Scan, wird nichts gepostet.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timezone

import requests

import storage
from config import cfg
from logger import log

API = "https://api.twitter.com/2"
_MENTION = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z0-9_]{1,15}")


def _zahl(name, std):
    try:
        return max(0, int(os.getenv(name, str(std)).strip() or std))
    except Exception:
        return std


def bereit(frisch=False):
    """Seit 2.69.3 entscheidet der Schalter 'xscan' auf der Admin-Seite, nicht mehr die Variable allein.
    X_SCAN_ENABLED ist nur noch der Startwert, solange der Schalter nie umgelegt wurde.
    X_SCAN_OFF=true sperrt es zusaetzlich hart, egal was der Schalter sagt."""
    if os.getenv("X_SCAN_OFF", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    try:
        import features
        if not (features.alle(frisch=True).get("xscan") if frisch else features.an("xscan")):
            return False
    except Exception:
        return False
    try:
        import x_post
        return x_post.bereit()
    except Exception:
        return False


def _q(s):
    return urllib.parse.quote(str(s), safe="~")


def _get(url, params):
    """GET mit OAuth 1.0a (User Context); die Abfrage-Parameter gehoeren hier zur Signatur."""
    o = {"oauth_consumer_key": cfg.x_api_key, "oauth_nonce": secrets.token_hex(16),
         "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
         "oauth_token": cfg.x_access_token, "oauth_version": "1.0"}
    alle = dict(o, **{k: str(v) for k, v in params.items()})
    param = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(alle.items()))
    basis = "&".join(["GET", _q(url), _q(param)])
    sig = base64.b64encode(hmac.new(f"{_q(cfg.x_api_secret)}&{_q(cfg.x_access_secret)}".encode(),
                                    basis.encode(), hashlib.sha1).digest()).decode()
    o["oauth_signature"] = sig
    kopf = "OAuth " + ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(o.items()))
    r = requests.get(url, params=params, headers={"Authorization": kopf}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"x {r.status_code}: {r.text[:200]}")
    return r.json()


def _stand():
    try:
        return json.loads(storage.kv_get("xscan_state") or "null") or {}
    except Exception:
        return {}


def _merken(st):
    storage.kv_set("xscan_state", json.dumps(st))


EIGENE = {"0x9c87b4cfa21266d1638f437c96dbe1012cb81111"}  # $ODDS: wir scannen uns nie selbst als Antwort


def ca_aus(text):
    """Die CA aus einem Post, ohne @Erwaehnungen. None, wenn keine drin ist.
    Seit 2.62.8: nur ausdrueckliche Contract-Adressen. Ein Cashtag ($ODDS, $SIL) loest nichts mehr aus, vorher wurde
    aus einem Post mit drei Cashtags der erste aufgeloest (und das war unser eigener Coin). Stehen mehrere
    verschiedene Adressen im Post, antworten wir gar nicht, statt zu raten. Eigene Coins nie."""
    import scan
    t = _MENTION.sub(" ", str(text or ""))
    t = re.sub(r"https?://t\.co/\S+", " ", t)
    gefunden = []
    for roh in re.findall(r"\S+", t):
        w = roh.strip(".,;:!?()[]{}\"'")
        hinweis = None
        if ":" in w and not w.lower().startswith("0x"):
            hinweis, w = w.split(":", 1)
        if not scan.ist_adresse(w):
            continue
        if w.lower() in EIGENE:
            continue
        k = (hinweis + ":" + w) if hinweis else w
        if k not in gefunden:
            gefunden.append(k)
    return gefunden[0] if len(gefunden) == 1 else None


def _sauber(s):
    return str(s or "").replace("—", ",").replace("–", "-").strip()


def antwort(r):
    """Kurz-Scan fuer X: hoechstens 280 Zeichen, kein Link, als Emoji nur das gruene Licht."""
    v = r.get("verdict") or {}
    sym = r.get("symbol") or ""
    kopf = f"${sym}" if sym else "This coin"
    if r.get("chain_name"):
        kopf += f" on {r['chain_name']}"
    # Seit 2.81: nur Fakten, kein Urteil (kein Titel wie clean oder high risk, kein Ampel-Emoji)
    z = [kopf, "What the chain shows:"]
    schlecht = [_sauber(x) for x in (v.get("bad") or [])][:3]
    gut = [_sauber(x) for x in (v.get("good") or [])][:3]
    z += ["· " + x for x in schlecht + gut[:max(1, 4 - len(schlecht))]]
    fuss = os.getenv("X_SCAN_FOOTER", "").strip() or "Full report: Your Engine on Telegram"
    while len("\n".join(z + ["", fuss])) > 280 and len(z) > 2:
        z.pop()
    text = "\n".join(z + ["", fuss])
    return text if len(text) <= 280 else text[:279]


def runde():
    """Neue Erwaehnungen lesen und innerhalb der Grenzen beantworten. -> Zahl der Antworten."""
    if not bereit():
        return 0
    st = _stand()
    heute = datetime.now(timezone.utc).date().isoformat()
    if st.get("day") != heute:
        st.update(day=heute, n=0, authors=[], cas=[], posts={})
    if not st.get("me"):
        st["me"] = (_get(API + "/users/me", {}).get("data") or {}).get("id")
        if not st["me"]:
            return 0
    p = {"max_results": 100, "tweet.fields": "author_id,created_at,conversation_id"}
    if st.get("since"):
        p["since_id"] = st["since"]
    d = _get(f"{API}/users/{st['me']}/mentions", p)
    daten = sorted(d.get("data") or [], key=lambda x: int(x["id"]))
    if not st.get("since"):
        # Erster Lauf: nur die Stelle merken, nichts Altes beantworten
        st["since"] = (daten[-1]["id"] if daten else (d.get("meta") or {}).get("newest_id")) or "1"
        _merken(st)
        return 0
    if not daten:
        return 0
    import scan
    import x_post
    pro_post, pro_tag = _zahl("X_SCAN_PER_POST", 5), _zahl("X_SCAN_PER_DAY", 20)
    gepostet = 0
    for tw in daten:
        st["since"] = tw["id"]
        if st["n"] >= pro_tag:
            continue
        wer, faden = tw.get("author_id"), tw.get("conversation_id") or tw["id"]
        if not wer or wer == st["me"] or wer in st["authors"] or st["posts"].get(faden, 0) >= pro_post:
            continue
        q = ca_aus(tw.get("text"))
        if not q or q.split(":")[-1].lower() in st["cas"]:
            continue
        try:
            # Seit 2.50.13: auf X nur mit fertigem Deep Scan antworten (Bundles, Weiterleitungen, Gruppen), nie mit dem Kurz-Scan
            r = scan.analyse(q, deep=True)
            ende = time.time() + _zahl("X_SCAN_DEEP_WAIT", 150)
            while (r.get("deep") or {}).get("state") == "running" and time.time() < ende:
                time.sleep(8)
                r = scan.analyse(q, deep=True)
        except Exception as e:
            log.warning(f"x_scan analyse {q}: {e}")
            continue
        if not r or r.get("error") or not r.get("ok", True) or not r.get("verdict"):
            continue
        # Der Deep Scan kann bis 150 s dauern: direkt vor dem Posten den Schalter frisch lesen.
        if not bereit(frisch=True):
            log.info(f"x_scan: switched off, reply to {tw['id']} not posted")
            _merken(st)
            return gepostet
        tid = x_post.post(antwort(r), reply_to=tw["id"], topf="scan")
        if tid:
            st["n"] += 1
            st["authors"].append(wer)
            st["cas"].append(q.split(":")[-1].lower())
            st["posts"][faden] = st["posts"].get(faden, 0) + 1
            gepostet += 1
            log.info(f"x_scan: answered {tw['id']} with ${r.get('symbol')} ({st['n']}/{pro_tag} today)")
        _merken(st)
    _merken(st)
    return gepostet


def stand():
    st = _stand()
    return {"on": bereit(), "answered_today": st.get("n", 0), "per_day": _zahl("X_SCAN_PER_DAY", 20),
            "per_post": _zahl("X_SCAN_PER_POST", 5)}


def schleife():
    time.sleep(60)
    vorher = None
    while True:
        try:
            jetzt = bereit()
            if jetzt and vorher is False:
                # Wieder eingeschaltet: nichts nachholen, was waehrend der Pause kam.
                st = _stand()
                st.pop("since", None)
                _merken(st)
                log.info("x_scan: switched on again, old mentions are skipped")
            vorher = jetzt
            if jetzt:
                runde()
        except Exception as e:
            log.warning(f"x_scan: {e}")
        time.sleep(max(30, _zahl("X_SCAN_POLL_SEC", 60)))
