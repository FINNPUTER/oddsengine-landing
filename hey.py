"""
hey.py: Der Builder-Eintrag von HEY Research Lab fuer Coins auf Robinhood Chain. Seit 2.80.7.

Abgesprochen mit HEY (08.11.2026): Wir zeigen ihre Daten als "What HEY read" mit Link auf ihre Seite und schicken bei
jedem Abruf den Kopf x-hey-integration: oddsengine/<version>, damit sie uns auf "Built with HEY" fuehren.
HEY ist keine Bewertung und kein Sicherheitsurteil: wir uebernehmen nur, was sie ueber den Builder wissen, und
erfinden nichts dazu. An HEY geht nichts ausser der Token-Adresse im Aufruf.

  lesen(chain, token) -> dict oder None   (1 Stunde im Speicher, Fehler 10 Minuten)
  zeilen(h)           -> Textzeilen fuer die Karten
"""
import threading
import time

import requests

from logger import log

API = "https://heyresearch.xyz/api/v1/builder"
CHAIN_ID = {"robinhood": 4663}
_CACHE = {}
_LOCK = threading.Lock()


def _version():
    try:
        import api
        return str(api.VERSION)
    except Exception:
        return "2.80.7"


def _datum(s):
    s = str(s or "")[:10]
    try:
        import datetime as dt
        d = dt.date.fromisoformat(s)
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    except Exception:
        return None


def _ohne_striche(t):
    """HEY schreibt Gedankenstriche; in unseren Karten stehen keine (Hausregel). Inhalt bleibt gleich."""
    return " ".join(str(t or "").replace(" \u2014 ", ", ").replace("\u2014", ", ").replace("\u2013", ", ").split())


def _ship(x):
    """Kurzform eines Ship- oder Change-Eintrags: (Text, Datum)."""
    if not isinstance(x, dict):
        return None
    txt = _ohne_striche(x.get("summary") or x.get("title") or "").strip()
    wann = _datum(x.get("timestamp") or x.get("occurred_at") or x.get("detected_at") or x.get("date"))
    if not txt:
        return None
    return {"text": txt[:120], "date": wann}


def lesen(chain, token):
    cid = CHAIN_ID.get(str(chain or "").lower())
    if not cid or not token:
        return None
    k = (cid, str(token).lower())
    with _LOCK:
        c = _CACHE.get(k)
        if c and time.time() - c[0] < (3600 if c[1] else 600):
            return c[1]
    out = None
    try:
        r = requests.get(API, params={"chain": cid, "token": k[1]}, timeout=5,
                         headers={"x-hey-integration": f"oddsengine/{_version()}", "User-Agent": "oddsengine"})
        if r.status_code == 200:
            d = r.json() or {}
            if d.get("hey_project_url") or d.get("project_link"):
                out = {
                    "project": d.get("hey_project_name"),
                    "url": d.get("project_link") or d.get("hey_project_url"),
                    "status": _ohne_striche(d.get("hey_status_label") or d.get("hey_status")) or None,
                    "verified_builder": bool(d.get("verified_builder")),
                    "token_verified": str(d.get("token_verification") or "").upper() == "VERIFIED",
                    "ships_30d": d.get("meaningful_ships_30d"),
                    "last_ship": _ship(d.get("latest_meaningful_ship")),
                    "last_change": _ship(d.get("latest_change")),
                    "as_of": _datum(d.get("as_of")),
                    "source": "HEY Research Lab",
                }
    except Exception as e:
        log.debug(f"hey: {e}")
    with _LOCK:
        _CACHE[k] = (time.time(), out)
    return out


def zeilen(h, html=False):
    """Der Abschnitt "What HEY read". Ohne Daten keine Zeilen."""
    if not h:
        return []
    import html as H
    e = (lambda x: H.escape(str(x), quote=False)) if html else (lambda x: str(x))
    z = []
    z.append(("Builder: verified" if h.get("verified_builder") else "Builder: not verified") +
             (f" · {e(h['status'])}" if h.get("status") else ""))
    if h.get("last_ship"):
        s = h["last_ship"]
        z.append(f"Last ship: {e(s['text'])}" + (f" · {s['date']}" if s.get("date") else ""))
    elif h.get("last_change"):
        s = h["last_change"]
        z.append(f"Last change on record: {e(s['text'])}" + (f" · {s['date']}" if s.get("date") else ""))
    if h.get("ships_30d") is not None:
        z.append(f"Ships in 30 days: {h['ships_30d']}")
    url = h.get("url")
    if url:
        z.append(f'<a href="{H.escape(url)}">Full record on HEY</a>' if html else f"Full record on HEY: {url}")
    return z
