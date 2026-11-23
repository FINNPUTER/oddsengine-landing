"""
anruf.py: Your Engine ruft an (seit 2.52). Ueber Twilio, mit der Stimme aus stimme.py (ElevenLabs, Gideon).

Was es kann:
  - Bei Gefahr anrufen: Wallet Guard (🚨 Tokens werden abgezogen, gefaehrliche Freigabe, Besitzer gewechselt)
    und Bag Watch (💧 LP gezogen, 💸 Dev verkauft, 🐋 Top-Halter dumpt, 👥 Wallet-Gruppe verkauft).
  - Auf Wunsch anrufen: "call me and read me the briefing" (call_me).
Der Anruf spricht die Meldung, mit 1 hoert man sie nochmal. Die Nachricht in Telegram kommt wie immer zusaetzlich.

Nummer: nur bestaetigte Nummern. Beim Eintragen ruft Your Engine einmal an und sagt einen 4-stelligen Code,
den der Nutzer in den Chat schreibt. So kann niemand fremde Nummern anrufen lassen.

Kosten: CALL_USD_PER_MIN (Standard 0.50) je angefangene Minute vom Guthaben, kein weiterer Aufschlag.
Vor dem Anruf wird die Hoechstdauer reserviert (CALL_MAX_SEC, Standard 90), nach dem Anruf auf die echte
Dauer ausgeglichen. Nicht angenommen = 0. Team-Wallets (ENGINE_FREE) zahlen nichts.

Schalter: Feature 'calls'. Railway (web und worker):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM (+E.164)
  PUBLIC_API_URL      oeffentliche Adresse des Web-Services (Standard https://api.oddsengine.live)
  CALL_USD_PER_MIN    Preis je Minute (0.50)
  CALL_MAX_SEC        Hoechstdauer eines Anrufs (90)
  CALL_MAX_DAY        Anrufe je Nutzer und Tag (10)
  CALL_COOLDOWN_MIN   Mindestabstand zwischen zwei Alarm-Anrufen (10)
"""
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from xml.sax.saxutils import escape

import requests

import storage
from logger import log

GEFAHR_BAG = ("💧", "💸", "🐋", "👥")


def _f(name, std):
    try:
        return float(os.getenv(name, str(std)).strip() or std)
    except Exception:
        return std


def bereit():
    return all(os.getenv(k, "").strip() for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))


def basis():
    return (os.getenv("PUBLIC_API_URL", "").strip() or "https://api.oddsengine.live").rstrip("/")


def preis_min():
    return round(_f("CALL_USD_PER_MIN", 0.50), 4)


def _j(k, std=None):
    try:
        v = storage.kv_get(k)
        return json.loads(v) if v else std
    except Exception:
        return std


def _set(k, v):
    storage.kv_set(k, json.dumps(v) if not isinstance(v, str) else v)


def nummer_norm(n):
    t = re.sub(r"[\s().-]", "", str(n or ""))
    if t.startswith("00"):
        t = "+" + t[2:]
    return t if re.fullmatch(r"\+[1-9]\d{7,14}", t) else None


def _kurz(n):
    return n[:4] + "…" + n[-3:] if n else n


def _cfg(adr):
    import motor
    z = motor.zustand(adr)
    return z, z["prefs"].setdefault("call", {})


# ── Twilio ───────────────────────────────────────────────────────────────

def _twiml(cid, text=None, nochmal=0):
    """TwiML fuer einen Anruf: Stimme abspielen (oder Twilio-Stimme als Ersatz), dann 1 = nochmal."""
    c = _j("call_" + cid, {}) or {}
    teile = ['<?xml version="1.0" encoding="UTF-8"?><Response><Pause length="1"/>']
    if c.get("audio"):
        teile.append(f"<Play>{escape(basis())}/v1/call/audio/{cid}.mp3</Play>")
    else:
        teile.append(f'<Say voice="Polly.Matthew-Neural">{escape(text or c.get("text") or "")}</Say>')
    if nochmal < 2:
        teile.append(f'<Gather numDigits="1" timeout="6" action="{escape(basis())}/v1/call/twiml/{cid}?n={nochmal + 1}" method="POST">'
                     '<Say voice="Polly.Matthew-Neural">Press 1 to hear it again.</Say></Gather>')
    teile.append("</Response>")
    return "".join(teile)


def signatur_ok(url, params, sig):
    """X-Twilio-Signature pruefen: Base64(HMAC-SHA1(Token, URL + sortierte Schluessel+Werte))."""
    tok = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not tok or not sig:
        return False
    daten = url + "".join(k + str(params[k]) for k in sorted(params))
    soll = base64.b64encode(hmac.new(tok.encode(), daten.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(soll, sig)


def _waehlen(nummer, cid):
    sid, tok = os.getenv("TWILIO_ACCOUNT_SID").strip(), os.getenv("TWILIO_AUTH_TOKEN").strip()
    r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", auth=(sid, tok), timeout=20,
                      data={"To": nummer, "From": os.getenv("TWILIO_FROM").strip(), "Twiml": _twiml(cid),
                            "TimeLimit": str(int(_f("CALL_MAX_SEC", 90))),
                            "StatusCallback": f"{basis()}/v1/call/status/{cid}", "StatusCallbackMethod": "POST"})
    if r.status_code >= 300:
        raise RuntimeError(f"twilio {r.status_code}: {r.text[:200]}")
    return r.json().get("sid")


def anrufen(adr, text, grund="manual", nummer=None):
    """Einen Anruf starten. Reserviert die Hoechstdauer vom Guthaben. -> {"ok", ...}"""
    import ai
    if not bereit():
        return {"ok": False, "error": "Phone calls are not switched on yet."}
    z, c = _cfg(adr)
    ziel = nummer or (c.get("num") if c.get("ok") else None)
    if not ziel:
        return {"ok": False, "error": "No confirmed phone number yet. Give me your number with country code, e.g. +39 333 1234567."}
    tag = time.strftime("%Y%m%d", time.gmtime())
    zahl = c.get("n", 0) if c.get("day") == tag else 0
    if zahl >= int(_f("CALL_MAX_DAY", 10)):
        return {"ok": False, "error": "Daily call limit reached."}
    reserviert = round(math.ceil(_f("CALL_MAX_SEC", 90) / 60) * preis_min(), 4)
    if not ai._reservieren(adr, reserviert):
        return {"ok": False, "error": f"Not enough balance for a call ({reserviert:.2f} $ needed). Top up with $ODDS on engine.oddsengine.live."}
    cid = secrets.token_urlsafe(12)
    sprechtext = str(text or "").strip()
    audio = None
    try:
        import stimme
        if stimme.anbieter():
            mp3, info = stimme.sprechen(sprechtext)
            if mp3:
                audio = base64.b64encode(mp3).decode()
    except Exception as e:
        log.warning(f"anruf stimme: {e}")
    try:
        import stimme
        sprechtext = stimme.vorlesbar(sprechtext) or sprechtext
    except Exception:
        pass
    _set("call_" + cid, {"adr": adr, "text": sprechtext[:1500], "audio": bool(audio), "reserved": reserviert,
                         "ts": int(time.time()), "why": grund, "settled": False})
    if audio:
        _set("call_audio_" + cid, audio)
    try:
        tsid = _waehlen(ziel, cid)
    except Exception as e:
        ai._ausgleichen(adr, reserviert, 0)
        log.warning(f"anruf {adr[:8]}: {e}")
        return {"ok": False, "error": "The call could not be placed. Check that the number is right."}
    c["day"], c["n"] = tag, zahl + 1
    c["last"] = int(time.time())
    import motor
    motor._zustand_speichern(adr, z)
    k = _j("call_" + cid, {})
    k["twilio"] = tsid
    _set("call_" + cid, k)
    return {"ok": True, "calling": _kurz(ziel), "price_per_min": preis_min(),
            "note": f"Calling now. {preis_min():.2f} $ per started minute from the balance, nothing if not answered."}


def abrechnen(cid, dauer_s, status):
    """Aus dem Twilio-Status-Rueckruf: Reservierung auf die echte Dauer bringen (einmal)."""
    import ai
    c = _j("call_" + cid)
    if not c or c.get("settled"):
        return False
    try:
        d = int(float(dauer_s or 0))
    except Exception:
        d = 0
    minuten = math.ceil(d / 60) if d > 0 and status == "completed" else 0
    echt = round(min(minuten * preis_min(), c["reserved"]), 4)
    ai._ausgleichen(c["adr"], c["reserved"], echt)
    c.update(settled=True, seconds=d, charged=echt, status=status)
    _set("call_" + cid, c)
    _set("call_audio_" + cid, "")
    return True


def audio(cid):
    a = storage.kv_get("call_audio_" + cid)
    try:
        return base64.b64decode(a) if a else None
    except Exception:
        return None


def twiml_nochmal(cid, n):
    c = _j("call_" + cid)
    if not c:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    return _twiml(cid, c.get("text"), n)


# ── Nummer einrichten ────────────────────────────────────────────────────

def nummer_setzen(adr, nummer):
    n = nummer_norm(nummer)
    if not n:
        return {"ok": False, "error": "Send the number with country code, e.g. +39 333 1234567."}
    z, c = _cfg(adr)
    code = f"{secrets.randbelow(9000) + 1000}"
    c["pending"] = {"num": n, "code": code, "ts": int(time.time()), "tries": 0}
    import motor
    motor._zustand_speichern(adr, z)
    ziffern = " ".join(code)
    r = anrufen(adr, f"Hi, this is Your Engine. Your code is {ziffern}. Again: {ziffern}. Type it in the chat.",
                "verify", nummer=n)
    if not r.get("ok"):
        return r
    return {"ok": True, "calling": _kurz(n),
            "note": "Your Engine is calling that number now and says a 4 digit code. The user types it here, then call confirm_phone."}


def bestaetigen(adr, code):
    z, c = _cfg(adr)
    p = c.get("pending")
    if not p or time.time() - p["ts"] > 900:
        return {"ok": False, "error": "No code waiting. Give me the number again."}
    p["tries"] = p.get("tries", 0) + 1
    import motor
    if re.sub(r"\D", "", str(code or "")) != p["code"]:
        if p["tries"] >= 3:
            c.pop("pending", None)
        motor._zustand_speichern(adr, z)
        return {"ok": False, "error": "That code is not right." + (" Give me the number again." if p["tries"] >= 3 else "")}
    c.update(num=p["num"], ok=True, on=True, since=int(time.time()))
    c.pop("pending", None)
    motor._zustand_speichern(adr, z)
    return {"ok": True, "number": _kurz(c["num"]), "alerts_call": True,
            "note": f"Confirmed. From now on Your Engine calls this number when Wallet Guard sees a drain or a dangerous approval, "
                    f"and when a coin you hold gets rugged (LP pulled, dev sells, top holder dumps). {preis_min():.2f} $ per started minute. "
                    "Bag alerts stay quiet from 23 to 8 your time, wallet drains always call."}


def einstellen(adr, on=None, quiet=None, forget=False):
    z, c = _cfg(adr)
    import motor
    if forget:
        z["prefs"]["call"] = {}
        motor._zustand_speichern(adr, z)
        return {"ok": True, "removed": True}
    if on is not None:
        c["on"] = bool(on)
    if quiet is not None:
        m = re.fullmatch(r"\s*(\d{1,2})\s*[-:to ]+\s*(\d{1,2})\s*", str(quiet))
        if str(quiet).lower() in ("off", "none", "0"):
            c["quiet"] = None
        elif m:
            c["quiet"] = [int(m.group(1)) % 24, int(m.group(2)) % 24]
        else:
            return {"ok": False, "error": "Quiet hours look like 23-8, or 'off'."}
    motor._zustand_speichern(adr, z)
    return status(adr)


def status(adr):
    z, c = _cfg(adr)
    return {"ok": True, "number": _kurz(c.get("num")) if c.get("ok") else None, "alert_calls": bool(c.get("ok") and c.get("on")),
            "quiet_hours": c.get("quiet", [23, 8]), "price_per_min": preis_min(), "available": bereit()}


# ── Alarm-Anrufe ─────────────────────────────────────────────────────────

def _ruhe(adr, c):
    q = c.get("quiet", [23, 8])
    if not q:
        return False
    import motor
    h = (time.gmtime().tm_hour + motor._tz(adr)) % 24
    a, b = q
    return (a <= h or h < b) if a > b else (a <= h < b)


def wichtig(adr, text, quelle):
    """Aus Wallet Guard (quelle 'guard') und Bag Watch ('bag'). Ruft an, wenn eingerichtet und es wirklich brennt."""
    try:
        import motor
        if not bereit() or not motor._schalter("calls", adr):
            return False
        z, c = _cfg(adr)
        if not (c.get("ok") and c.get("on")):
            return False
        t = str(text or "")
        if quelle == "guard":
            if not t.lstrip().startswith("🚨"):
                return False
        elif quelle == "bag":
            if not any(e in t for e in GEFAHR_BAG) or _ruhe(adr, c):
                return False
        else:
            return False
        if time.time() - (c.get("last") or 0) < _f("CALL_COOLDOWN_MIN", 10) * 60:
            return False
        c["last"] = int(time.time())
        motor._zustand_speichern(adr, z)
        zeilen = [x for x in t.splitlines() if x.strip()]
        kern = " ".join(zeilen[:3])[:500]
        gesagt = f"This is Your Engine with an urgent alert. {kern} Details are in your Telegram."
        threading.Thread(target=anrufen, args=(adr, gesagt, quelle), daemon=True).start()
        return True
    except Exception as e:
        log.warning(f"anruf wichtig {adr[:8]}: {e}")
        return False
