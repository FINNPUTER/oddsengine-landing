"""
postlog.py: Protokoll jedes oeffentlichen Posts (Telegram-Kanal und X), seit 2.70.2.

Vorher stand ein gescheiterter Post nur im Railway-Log. Jetzt: die letzten 80 Versuche in der Datenbank
(Admin-Seite, Abschnitt "Public posts"), und scheitert der Telegram-Kanal, kommt sofort eine Nachricht in
den privaten Chat, hoechstens einmal je Fehlerart und Stunde (wie bei X seit 2.69.3).
"""
import json
import time

import requests

import storage
from config import cfg
from logger import log

KV = "post_log"


def eintragen(wo, ok, art="", fehler=None, ref=None):
    try:
        liste = json.loads(storage.kv_get(KV) or "[]")
    except Exception:
        liste = []
    liste.append({"ts": int(time.time()), "where": wo, "ok": bool(ok), "kind": art[:40],
                  "error": (str(fehler)[:220] if fehler else None), "ref": ref})
    try:
        storage.kv_set(KV, json.dumps(liste[-80:]))
    except Exception as e:
        log.debug(f"postlog: {e}")


def lesen():
    try:
        return list(reversed(json.loads(storage.kv_get(KV) or "[]")))
    except Exception:
        return []


def alarm(quelle, grund, hilfe=""):
    art = (quelle + "_" + str(grund).split(":")[0][:30])
    k = "post_alarm_" + "".join(ch for ch in art if ch.isalnum() or ch == "_")
    try:
        if time.time() - float(storage.kv_get(k) or 0) < 3600:
            return
        storage.kv_set(k, time.time())
    except Exception:
        pass
    if not (cfg.tg_token and cfg.tg_private):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage",
                      json={"chat_id": cfg.tg_private, "disable_web_page_preview": True,
                            "text": f"{quelle} post failed: {str(grund)[:300]}{hilfe}"}, timeout=15)
    except Exception as e:
        log.warning(f"post alarm: {e}")
