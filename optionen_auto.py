"""
optionen_auto.py: Auto-Trading der Above-X-Calls fuer interne Beta-Tester. Seit 2.82.14.

Jeder Tester hat SEIN EIGENES Kalshi-Konto (KYC, selbst eingezahlt) und erstellt dort einen API-Schluessel
(Account & security -> API Keys). Die Engine haelt nie Geld, sie kauft nur in seinem Konto, nach seinen Limits.

Einrichtung im Dienst crypto:
  KALSHI_AUTOTRADE = JSON-Liste, z. B.
    [{"name": "luka", "key_id": "abc-123", "key_env": "KALSHI_KEY_LUKA", "usd_per_call": 10, "usd_per_day": 60,
      "chat_id": "123456789", "coins": ["btc"]}]
  KALSHI_KEY_LUKA = der private Schluessel (PEM-Text, oder derselbe Text base64-kodiert)
  Schalter "options_autotrade" an.

Ablauf: Sobald optionen_calls einen Call anlegt (egal ob der Kanal-Schalter an ist), kauft die Engine fuer jeden
Tester die Seite des Calls als Limit-Order (Kalshi V2, /portfolio/events/orders) "immediate_or_cancel" zum Call-Preis plus OPT_AUTO_SLIP_CENTS (Standard 1).
Was nicht sofort zu dem Preis geht, verfaellt. Kein Nachkaufen, kein Verkaufen, die Position laeuft bis zum Schluss.
Admin: /v1/admin/options/autotrade (ohne Schluessel).

Seit 2.82.20 koennen Nutzer sich selbst eintragen (engine.oddsengine.live, Karte "Auto mode"):
  - Voraussetzung: Wallet verbunden und als $ODDS-Holder verifiziert (Stufe AUTOTRADE_TIER, Standard holder).
  - Der Nutzer gibt Key ID, privaten Schluessel und seine Limits ein. Der Schluessel wird sofort gegen Kalshi geprueft
    (Guthaben-Abfrage) und nur verschluesselt gespeichert (AUTOTRADE_SECRET im Dienst web UND crypto, gleicher Wert).
  - Freischalten: steht die Wallet auf der Einladungsliste (Admin: .../autotrade/allow?wallet=0x..&on=1), ist sie sofort
    aktiv. Sonst wartet sie auf Freigabe (Admin: .../autotrade/approve?wallet=0x..&ok=1). AUTOTRADE_OPEN=1 schaltet jeden
    verifizierten Holder ohne Freigabe frei.
  - Faellt der Nutzer unter die Holder-Stufe, kauft die Engine nicht mehr fuer ihn.
  - Obergrenzen: AUTOTRADE_MAX_CALL (Standard 50 $), AUTOTRADE_MAX_DAY (Standard 300 $).
"""
import base64
import json
import os
import time
import uuid
from datetime import datetime
import hashlib

import requests

from logger import log

BASIS = os.getenv("KALSHI_TRADE_URL", "https://external-api.kalshi.com/trade-api/v2").rstrip("/")
SLIP = int(os.getenv("OPT_AUTO_SLIP_CENTS", "1"))
# Seit 2.82.76: das Limit laesst immer mindestens so viel Gewinn uebrig (vorher 79-Cent-Call mit Limit 99 Cent).
MIN_REST_C = int(os.getenv("OPT_AUTO_MIN_PROFIT_CENTS", "10"))
# Seit 2.82.106: nur kaufen, wenn der echte Preis im Buch schon mindestens so viele Cent UEBER dem Call-Preis liegt.
# Grund (94 echte Orders, 18. bis 22.11.): stand das Angebot noch auf dem Call-Preis oder darunter, trafen 21 %
# (39 Orders, -$36.87). War es schon gestiegen, trafen 44 % (55 Orders, +$8.09). Dasselbe Bild in 998 Picks mit
# Buch: 19 % Treffer ohne Bewegung, 52 % bei mehr als 5 Cent. Der Markt bestaetigt den Call oder er tut es nicht.
# 0 schaltet die Regel ab. Uebersprungene Calls werden mitgeschrieben und spaeter ausgewertet.
MIN_MOVE_C = int(os.getenv("OPT_AUTO_MIN_MOVE_CENTS", "1"))
# Seit 2.82.76: voller Einsatz (Stueckzahl nach Call-Preis) nur mit OPT_AUTO_FULL_STAKE=1. Nachgerechnet an den
# 35 Orders von LaCabra waere es mit vollem Einsatz schlechter gewesen (-41,15 statt -32,91), weil nur 7 trafen.
# Seit 2.82.88: Kapazitaet. Alle Kunden kaufen denselben Call in derselben Sekunde; das Buch ist duenn.
CALL_CAP = int(os.getenv("OPT_AUTO_CALL_CAP", "40"))      # hoechstens so viele Kontrakte je Call ueber alle Kunden (0 = aus)
SEATS = int(os.getenv("OPT_AUTO_SEATS", "20"))            # hoechstens so viele aktive Konten, danach Warteliste (0 = aus)
VOLL = os.getenv("OPT_AUTO_FULL_STAKE", "0").strip().lower() in ("1", "true", "on", "yes")
NAMEN = {"btc": "BTC", "eth": "ETH"}
STUFE = os.getenv("AUTOTRADE_TIER", "holder").strip().lower() or "holder"
OFFEN = os.getenv("AUTOTRADE_OPEN", "0").strip() in ("1", "true", "yes", "on")
MAX_CALL = float(os.getenv("AUTOTRADE_MAX_CALL", "50"))
MAX_TAG = float(os.getenv("AUTOTRADE_MAX_DAY", "300"))
_SCHEMA = [False]
_SCHLUESSEL = {}
_HOLDER = {}


def an():
    try:
        import features
        return features.an("options_autotrade")
    except Exception:
        return False


def _aus_env():
    try:
        liste = json.loads(os.getenv("KALSHI_AUTOTRADE", "") or "[]")
        return [t for t in liste if isinstance(t, dict) and t.get("name") and t.get("key_id") and t.get("key_env")]
    except Exception as e:
        log.warning(f"autotrade: KALSHI_AUTOTRADE is not valid JSON ({e})")
        return []


# ── Selbst eingetragene Nutzer (seit 2.82.20) ──────────────────────────

def _fernet():
    geheim = os.getenv("AUTOTRADE_SECRET", "").strip()
    if not geheim:
        return None
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(geheim.encode()).digest()))


def bereit():
    return _fernet() is not None


def _pg():
    import optionen
    pg = optionen._pg()
    if pg:
        _schema(pg)
    return pg


def _norm(adr):
    return str(adr or "").strip().lower()


def erlaubt_liste():
    try:
        import storage
        return {_norm(x) for x in json.loads(storage.kv_get("autotrade_allow") or "[]")}
    except Exception:
        return set()


def erlauben(adr, an_=True):
    import storage
    l = erlaubt_liste()
    (l.add if an_ else l.discard)(_norm(adr))
    storage.kv_set("autotrade_allow", json.dumps(sorted(l)))
    pg = _pg()
    if pg and an_:
        pg.execute("UPDATE optionen_auto_user SET status='active', updated=now() WHERE wallet=%s AND status='pending'", (_norm(adr),))
    if pg and not an_:
        pg.execute("UPDATE optionen_auto_user SET status='pending', updated=now() WHERE wallet=%s AND status='active'", (_norm(adr),))
    return sorted(l)


def freigeben(adr, ok=True):
    pg = _pg()
    if not pg:
        return {"error": "no database"}
    pg.execute("UPDATE optionen_auto_user SET status=%s, updated=now() WHERE wallet=%s AND status IN ('pending','active','revoked')",
               ("active" if ok else "revoked", _norm(adr)))
    try:
        import motor
        motor.senden(_norm(adr), "Auto mode is now active. The engine buys the next Above X call in your Kalshi account, within your limits."
                     if ok else "Auto mode was switched off for your wallet.")
    except Exception:
        pass
    return nutzer_stand(adr)


def basis_ok(adr, frisch=False):
    """Seit 2.82.24: Wer ueber den Supply-Anteil geprueft wird (Plan team, holder) oder den Plan paid hat, braucht die
    Holder-Stufe nicht zusaetzlich. Der Anteil wird vor jedem Kauf frisch von der Chain gelesen (optionen_plan)."""
    try:
        import optionen_plan
        p = optionen_plan.plan(adr)
        if p in ("paid", "team") or optionen_plan.braucht_anteil(p):
            return True
    except Exception:
        pass
    return holder_ok(adr, frisch)


def holder_ok(adr, frisch=False):
    a = _norm(adr)
    c = _HOLDER.get(a)
    if c and not frisch and time.time() - c[0] < 1800:
        return c[1]
    ok = False
    try:
        import holder
        if holder.mint():
            st = holder.status(holder.norm(a), frisch=True)
            ok = holder.RANG.get(st.get("tier") or "public", 0) >= holder.RANG.get(STUFE, 1)
            _holder_merken(a, ok)
        else:
            # Seit 2.82.21: Dienst ohne Token-Variablen (z. B. crypto ohne ODDS_MINT). Dann gilt, was der
            # Dienst web zuletzt geprueft und in der Datenbank vermerkt hat, hoechstens HOLDER_ALT_TAGE alt.
            ok = _holder_gemerkt(a)
    except Exception as e:
        log.debug(f"autotrade holder {a[:8]}: {e}")
        ok = c[1] if c else False
    _HOLDER[a] = (time.time(), ok)
    return ok


HOLDER_ALT_TAGE = float(os.getenv("AUTOTRADE_HOLDER_MAX_AGE_DAYS", "7"))


def _holder_merken(a, ok):
    try:
        pg = _pg()
        if pg:
            _schema(pg)
            pg.execute("UPDATE optionen_auto_user SET holder_ok=%s, holder_ts=now() WHERE wallet=%s", (bool(ok), a))
    except Exception as e:
        log.debug(f"autotrade holder merken: {e}")


def _holder_gemerkt(a):
    pg = _pg()
    if not pg:
        return False
    _schema(pg)
    r = pg.execute("SELECT holder_ok, EXTRACT(EPOCH FROM (now() - holder_ts)) FROM optionen_auto_user WHERE wallet=%s",
                   (a,)).fetchone()
    if not r or r[0] is None or r[1] is None:
        return False
    return bool(r[0]) and float(r[1]) <= HOLDER_ALT_TAGE * 86400


def puls():
    """Aus dem Kaufdienst (crypto), bei jeder Runde: was dort gesetzt ist. Die Admin-Seite laeuft in web und saehe es sonst nicht."""
    try:
        import storage
        import holder
        alt = json.loads(storage.kv_get("autotrade_buyer") or "{}")
        if time.time() - float(alt.get("ts") or 0) < 300:
            return
        storage.kv_set("autotrade_buyer", json.dumps({
            "ts": int(time.time()), "service": (os.getenv("START_AS") or os.getenv("RAILWAY_SERVICE_NAME") or "").strip().lower(),
            "switch": an(), "secret": bereit(), "token_vars": bool(holder.mint()),
            "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())}))
    except Exception as e:
        log.debug(f"autotrade puls: {e}")


def _grenzen(b):
    try:
        pc = max(1.0, min(MAX_CALL, float(b.get("usd_per_call") or 5)))
        pt = max(pc, min(MAX_TAG, float(b.get("usd_per_day") or 20)))
    except (TypeError, ValueError):
        pc, pt = 5.0, 20.0
    coins = [c for c in [str(x).lower().strip() for x in (b.get("coins") or [])] if c in ("btc", "eth")] or ["btc", "eth"]
    return pc, pt, coins


def nutzer_speichern(adr, b):
    """Nutzer traegt Schluessel und Limits ein. Prueft den Schluessel sofort gegen Kalshi."""
    a = _norm(adr)
    if not an():
        return {"ok": False, "error": "Auto mode is not open yet."}
    f = _fernet()
    if not f:
        return {"ok": False, "error": "Auto mode is not set up on our side yet (AUTOTRADE_SECRET missing)."}
    if not basis_ok(a, frisch=True):
        return {"ok": False, "error": f"Auto mode is for verified $ODDS holders ({STUFE} tier). Verify your wallet first."}
    # Seit 2.82.24: Supply-Anteil je nach Plan (optionen_plan.py)
    import optionen_plan
    ok_e, text_e = optionen_plan.eintritt(a)
    if not ok_e:
        return {"ok": False, "error": text_e}
    pg = _pg()
    if not pg:
        return {"ok": False, "error": "No database right now, try again."}
    pc, pt, coins = _grenzen(b)
    alt = pg.execute("SELECT key_id, key_enc, status FROM optionen_auto_user WHERE wallet=%s", (a,)).fetchone()
    kid = str(b.get("key_id") or "").strip()
    pem = str(b.get("private_key") or "").strip().replace("\\n", "\n")
    if kid or pem:
        if not (kid and "BEGIN" in pem and "PRIVATE KEY" in pem):
            return {"ok": False, "error": "Paste the Key ID and the complete private key from Kalshi, including the BEGIN and END lines."}
        probe = {"name": a, "key_id": kid, "key_pem": pem, "key_env": "user:" + a}
        _SCHLUESSEL.pop(probe["key_env"], None)
        try:
            bal = guthaben(probe)
        except Exception as e:
            return {"ok": False, "error": f"That private key could not be read ({str(e)[:80]})."}
        if bal is None:
            return {"ok": False, "error": "Kalshi did not accept this key. Check the Key ID and that the key belongs to it."}
        enc = f.encrypt(pem.encode()).decode()
    elif alt:
        kid, enc = alt[0], alt[1]
    else:
        return {"ok": False, "error": "Key ID and private key are missing."}
    status = "active" if (OFFEN or a in erlaubt_liste()) else "pending"
    warteliste = False
    if status == "active" and SEATS > 0 and not (alt and alt[2] == "active"):
        belegt = int(pg.execute("SELECT count(*) FROM optionen_auto_user WHERE status='active'").fetchone()[0] or 0)
        if belegt >= SEATS:
            status, warteliste = "pending", True     # seit 2.82.88: alle Plaetze belegt, der Betreiber gibt frei
    if alt and alt[2] == "revoked":
        status = "revoked"
    pg.execute("""INSERT INTO optionen_auto_user (wallet, key_id, key_enc, usd_per_call, usd_per_day, coins, status)
                  VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (wallet) DO UPDATE SET key_id=EXCLUDED.key_id,
                  key_enc=EXCLUDED.key_enc, usd_per_call=EXCLUDED.usd_per_call, usd_per_day=EXCLUDED.usd_per_day,
                  coins=EXCLUDED.coins, status=EXCLUDED.status, paused=FALSE, updated=now()""",
               (a, kid, enc, pc, pt, ",".join(coins), status))
    _holder_merken(a, True)
    _SCHLUESSEL.pop("user:" + a, None)
    if warteliste:
        _ereignis_sicher(f"waiting list: {a[:6]}…{a[-4:]} saved a key, all {SEATS} seats are taken")
        return dict(nutzer_stand(a), ok=True, waitlist=True,
                    note=f"All {SEATS} Auto mode seats are taken right now. You are on the waiting list, your key is saved. "
                         "We open the next seat for you and tell you in Telegram.")
    return dict(nutzer_stand(a), ok=True)


def _ereignis_sicher(text):
    try:
        import optionen_plan
        optionen_plan._ereignis(text)
    except Exception:
        pass


def nutzer_pause(adr, pause):
    pg = _pg()
    if pg:
        if not pause:
            import optionen_plan
            ok_w, text_w = optionen_plan.weiter(pg, adr)
            if not ok_w:
                return dict(nutzer_stand(adr), ok=False, error=text_w)
        pg.execute("UPDATE optionen_auto_user SET paused=%s, updated=now() WHERE wallet=%s", (bool(pause), _norm(adr)))
    return nutzer_stand(adr)


def nutzer_loeschen(adr):
    pg = _pg()
    if pg:
        pg.execute("DELETE FROM optionen_auto_user WHERE wallet=%s", (_norm(adr),))
    _SCHLUESSEL.pop("user:" + _norm(adr), None)
    return nutzer_stand(adr)


def nutzer_stand(adr):
    a = _norm(adr)
    out = {"available": an(), "configured": bereit(), "tier": STUFE, "holder": basis_ok(a) if a else False,
           "invited": OFFEN or a in erlaubt_liste(), "status": "none",
           "max": {"usd_per_call": MAX_CALL, "usd_per_day": MAX_TAG}}
    pg = _pg()
    if not pg or not a:
        return out
    r = pg.execute("SELECT key_id, usd_per_call, usd_per_day, coins, status, paused FROM optionen_auto_user WHERE wallet=%s", (a,)).fetchone()
    if not r:
        # Seit 2.82.27: die Karte zeigt schon vor dem Schluessel, was fehlt (Anteil, Guthaben).
        try:
            import optionen_plan
            out["plan"] = optionen_plan.stand(pg, a, None)
        except Exception as e:
            log.debug(f"autotrade plan stand: {e}")
        return out
    out.update(status="paused" if (r[5] and r[4] == "active") else r[4], key_id=(r[0] or "")[:6] + "\u2026",
               usd_per_call=float(r[1] or 0), usd_per_day=float(r[2] or 0), coins=(r[3] or "btc,eth").split(","))
    try:
        import optionen_plan
        out["plan"] = optionen_plan.stand(pg, a, out["usd_per_day"])
    except Exception as e:
        log.debug(f"autotrade plan stand: {e}")
    rows = pg.execute("""SELECT a.filled, a.cost, a.fees, p.won, p.settled IS NOT NULL FROM optionen_auto a
                         LEFT JOIN (SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_picks UNION ALL SELECT id, won, settled, coin, strike, book_price, t_resolve, 'kalshi', created FROM optionen_live UNION ALL SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_neu_picks) p ON p.id = a.pick_id WHERE a.tester=%s""", ("w:" + a,)).fetchall()
    fertig = [x for x in rows if x[4] and x[0]]
    einsatz = sum(float(x[1]) + float(x[2] or 0) for x in fertig)
    zurueck = sum(float(x[0]) for x in fertig if x[3])
    out["stats"] = {"orders": len(rows), "filled": sum(1 for x in rows if x[0]), "settled": len(fertig),
                    "spent": round(einsatz, 2), "back": round(zurueck, 2), "pnl": round(zurueck - einsatz, 2)}
    return out


def _aus_db(pg):
    f = _fernet()
    if not f:
        return []
    out = []
    for w, kid, enc, pc, pt, coins in pg.execute(
            "SELECT wallet, key_id, key_enc, usd_per_call, usd_per_day, coins FROM optionen_auto_user "
            "WHERE status='active' AND NOT paused").fetchall():
        if not basis_ok(w):
            continue
        try:
            pem = f.decrypt(enc.encode()).decode()
        except Exception:
            log.warning(f"autotrade: key of {w[:8]} cannot be decrypted (AUTOTRADE_SECRET changed?)")
            continue
        out.append({"name": "w:" + w, "wallet": w, "key_id": kid, "key_pem": pem, "key_env": "user:" + w,
                    "usd_per_call": float(pc or 5), "usd_per_day": float(pt or 20), "coins": (coins or "btc,eth").split(",")})
    return out


def tester(pg=None):
    """Tester aus der Railway-Variable plus selbst eingetragene, freigeschaltete Holder."""
    liste = _aus_env()
    try:
        pg = pg or _pg()
        if pg:
            liste += _aus_db(pg)
    except Exception as e:
        log.warning(f"autotrade users: {e}")
    return liste


def _melden(t, text):
    if t.get("wallet"):
        try:
            import motor
            motor.senden(t["wallet"], text)
            return
        except Exception as e:
            log.debug(f"autotrade melden {t['wallet'][:8]}: {e}")
    _tg(t.get("chat_id"), text)


def _schema(pg):
    if _SCHEMA[0]:
        return
    try:   # seit 2.82.84: SOL/XRP-Picks stehen in den Abfragen mit drin, die Tabelle muss es also geben
        import optionen_neu
        optionen_neu._pg()
    except Exception as e:
        log.debug(f"autotrade neu schema: {e}")
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_auto (
        id TEXT PRIMARY KEY, pick_id TEXT, tester TEXT, ticker TEXT, side TEXT, limit_c INTEGER, wanted INTEGER,
        filled INTEGER DEFAULT 0, cost NUMERIC DEFAULT 0, fees NUMERIC DEFAULT 0, status TEXT, error TEXT,
        created TIMESTAMPTZ DEFAULT now(), reported BOOLEAN DEFAULT FALSE)""")
    pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_auto_tester ON optionen_auto(tester, created)")
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_auto_user (
        wallet TEXT PRIMARY KEY, key_id TEXT, key_enc TEXT, usd_per_call NUMERIC, usd_per_day NUMERIC, coins TEXT,
        status TEXT DEFAULT 'pending', paused BOOLEAN DEFAULT FALSE, created TIMESTAMPTZ DEFAULT now(),
        updated TIMESTAMPTZ DEFAULT now())""")
    pg.execute("ALTER TABLE optionen_auto ADD COLUMN IF NOT EXISTS ms INTEGER")
    pg.execute("ALTER TABLE optionen_auto ADD COLUMN IF NOT EXISTS info JSONB")
    pg.execute("ALTER TABLE optionen_auto_user ADD COLUMN IF NOT EXISTS holder_ok BOOLEAN")
    pg.execute("ALTER TABLE optionen_auto_user ADD COLUMN IF NOT EXISTS holder_ts TIMESTAMPTZ")
    try:  # seit 2.82.44: Live Calls liegen in optionen_live, die Abfragen hier lesen beide Tabellen
        import optionen_live
        optionen_live._schema(pg)
    except Exception as e:
        log.warning(f"autotrade live schema: {e}")
    _SCHEMA[0] = True


def _key(t):
    name = t["key_env"]
    if name in _SCHLUESSEL:
        return _SCHLUESSEL[name]
    from cryptography.hazmat.primitives import serialization
    roh = (t.get("key_pem") or os.getenv(name, "")).strip()
    if not roh:
        raise RuntimeError(f"{name} is empty")
    if "BEGIN" not in roh:
        roh = base64.b64decode(roh).decode()
    roh = roh.replace("\\n", "\n")
    k = serialization.load_pem_private_key(roh.encode(), password=None)
    _SCHLUESSEL[name] = k
    return k


def _anfrage(t, methode, pfad, body=None):
    """Signiert nach Kalshi: Zeitstempel (ms) + Methode + Pfad ab /trade-api, ohne Query. RSA-PSS SHA-256 oder Ed25519."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, ed25519
    k = _key(t)
    voll = BASIS + pfad
    pfad_sig = "/" + voll.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
    ts = str(int(time.time() * 1000))
    msg = (ts + methode + pfad_sig).encode()
    if isinstance(k, ed25519.Ed25519PrivateKey):
        sig = k.sign(msg)
    else:
        sig = k.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                     hashes.SHA256())
    h = {"KALSHI-ACCESS-KEY": t["key_id"], "KALSHI-ACCESS-TIMESTAMP": ts,
         "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "Content-Type": "application/json"}
    r = requests.request(methode, voll, headers=h, data=json.dumps(body) if body is not None else None, timeout=15)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:300]}
    return r.status_code, j


def guthaben(t):
    code, j = _anfrage(t, "GET", "/portfolio/balance")
    if code != 200:
        return None
    return round(float(j.get("balance") or 0) / 100, 2)


_SHARD = {}


def _shard_von(t, ticker):
    """exchange_index des Markts (Kalshi Sharding). Krypto liegt auf einem eigenen Shard. Je Event gemerkt."""
    ev = ticker.rsplit("-", 1)[0]
    if ev in _SHARD:
        return _SHARD[ev]
    code, j = _anfrage(t, "GET", f"/markets/{ticker}")
    idx = None
    if code == 200:
        m = j.get("market") or j
        try:
            idx = int(m.get("exchange_index")) if m.get("exchange_index") is not None else 0
        except Exception:
            idx = 0
        _SHARD[ev] = idx
    return idx


def _zahl_usd(z):
    """Ein Betrag aus balance_breakdown in Dollar. Kalshi liefert Cent (balance) oder Dollar-Text (*_dollars)."""
    for k in ("balance_dollars", "available_balance_dollars", "cash_dollars"):
        if z.get(k) is not None:
            try:
                return float(z[k])
            except Exception:
                pass
    for k in ("balance", "available_balance", "cash"):
        if z.get(k) is not None:
            try:
                return float(z[k]) / 100.0
            except Exception:
                pass
    return None


def shard_guthaben(t):
    """{shard: dollar} aus GET /portfolio/balance (balance_breakdown). Leer, wenn Kalshi keine Aufteilung liefert."""
    code, j = _anfrage(t, "GET", "/portfolio/balance")
    out = {}
    if code != 200:
        return out, j
    for z in j.get("balance_breakdown") or []:
        if not isinstance(z, dict):
            continue
        idx = z.get("exchange_index", z.get("exchange_shard", z.get("shard")))
        try:
            idx = int(idx)
        except Exception:
            continue
        v = _zahl_usd(z)
        if v is not None:
            out[idx] = out.get(idx, 0.0) + v
    return out, j


def shard_finanzieren(t, ticker, bedarf):
    """Seit 2.82.40: Kalshi haelt Geld je Shard. Ueber die Webseite schiebt Kalshi selbst, ueber die API nicht.
    Vor dem Kauf: liegt auf dem Shard des Markts weniger als noetig, den Rest vom groessten anderen Shard
    desselben Kontos herueberschieben (POST /portfolio/intra_exchange_instance_transfer, Betrag in Centicent).
    -> (ok, info). Geld verlaesst dabei nie das Kalshi-Konto des Nutzers."""
    info = {}
    idx = _shard_von(t, ticker)
    info["shard"] = idx
    if not idx:
        return True, info
    stand, roh = shard_guthaben(t)
    info["before"] = {str(k): round(v, 2) for k, v in stand.items()}
    hier = stand.get(idx, 0.0)
    if hier >= bedarf:
        return True, info
    quellen = sorted(((v, k) for k, v in stand.items() if k != idx and v > 0), reverse=True)
    quelle = quellen[0][1] if quellen else 0
    verfuegbar = quellen[0][0] if quellen else None
    menge = round(bedarf - hier + 1.0, 2)   # ein Dollar Luft fuer Gebuehren
    if verfuegbar is not None:
        menge = min(menge, round(verfuegbar, 2))
    if menge <= 0:
        return False, dict(info, why="no cash on any other index")
    body = {"source": "event_contract", "destination": "event_contract", "amount": int(round(menge * 10000)),
            "source_exchange_shard": int(quelle), "destination_exchange_shard": int(idx),
            "client_transfer_id": str(uuid.uuid4())}
    code, j = _anfrage(t, "POST", "/portfolio/intra_exchange_instance_transfer", body)
    info["transfer"] = {"usd": menge, "from": quelle, "to": idx, "http": code, "id": j.get("transfer_id"),
                        "err": None if code in (200, 201) else json.dumps(j)[:200]}
    if code not in (200, 201):
        return False, info
    # Seit 2.82.83: nicht mehr warten. Vorher bis zu 8 x 0,6 s Abfragen, das kostete jeden Kauf rund 7 Sekunden,
    # und die Orders gingen trotzdem durch. Lehnt Kalshi ab (insufficient_shard_balance), versucht kaufen() es
    # nach einer Sekunde ein zweites Mal.
    info["waited"] = False
    return True, info


def _tg(chat, text):
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not (tok and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": str(chat), "text": text, "disable_web_page_preview": True}, timeout=15)
    except Exception as e:
        log.debug(f"autotrade telegram: {e}")


def _heute(pg, name):
    r = pg.execute("SELECT COALESCE(sum(cost + fees), 0) FROM optionen_auto WHERE tester=%s "
                   "AND created > now() - interval '24 hours'", (name,)).fetchone()
    return float(r[0] or 0)


class _KeinKauf(Exception):
    pass


def move_bericht(pg, name=None):
    """Was haette die Regel gekostet oder gespart? Uebersprungene Calls zum damaligen Angebot, mit 7 % Gebuehr."""
    out = {"min_move_c": MIN_MOVE_C, "skipped": 0, "settled": 0, "would_hit": 0, "would_pnl_per_contract": 0.0}
    try:
        rows = pg.execute("""SELECT a.info, p.won, p.settled IS NOT NULL FROM optionen_auto a
                             LEFT JOIN (SELECT id, won, settled FROM optionen_picks UNION ALL SELECT id, won, settled FROM optionen_live
                                        UNION ALL SELECT id, won, settled FROM optionen_neu_picks) p ON p.id = a.pick_id
                             WHERE a.status='no_move'""" + (" AND a.tester=%s" if name else ""), ((name,) if name else ())).fetchall()
    except Exception as e:
        out["error"] = str(e)[:120]
        return out
    out["skipped"] = len(rows)
    for info, won, fertig in rows:
        if not fertig:
            continue
        try:
            j = info if isinstance(info, dict) else json.loads(info or "{}")
            ask = float((j.get("move") or {}).get("ask_c")) / 100.0
        except Exception:
            continue
        out["settled"] += 1
        gebuehr = 0.07 * ask * (1 - ask)
        if won:
            out["would_hit"] += 1
            out["would_pnl_per_contract"] += 1 - ask - gebuehr
        else:
            out["would_pnl_per_contract"] -= ask + gebuehr
    out["would_pnl_per_contract"] = round(out["would_pnl_per_contract"], 2)
    out["would_hit_rate"] = round(out["would_hit"] / out["settled"], 3) if out["settled"] else None
    return out


def kaufen(pg, call):
    """call: {id, coin, venue, side, price, K, market}. Fuer jeden Tester einmal."""
    if not an() or call.get("venue") != "kalshi" or not call.get("market"):
        return
    _schema(pg)
    leute = tester(pg)
    if len(leute) > 1:
        # seit 2.82.88: Reihenfolge je Call drehen, damit nicht immer dieselben zuerst (und billiger) kaufen
        import hashlib as _h
        k = int(_h.md5(str(call["id"]).encode()).hexdigest(), 16) % len(leute)
        leute = leute[k:] + leute[:k]
    for t in leute:
        name = t["name"]
        neu_an = [x.strip().lower() for x in os.getenv("OPT_NEW_AUTO", "").split(",") if x.strip()]   # seit 2.82.84
        if t.get("coins") and call["coin"] not in [c.lower() for c in t["coins"]] and call["coin"] not in neu_an:
            continue
        oid = f"{call['id']}|{name}"
        if pg.execute("SELECT 1 FROM optionen_auto WHERE id=%s", (oid,)).fetchone():
            continue
        # Seit 2.82.44: Live Calls bringen ihr eigenes Limit mit (Buchpreis plus wenig, nie ueber den fairen Wert)
        if call.get("limit_c"):
            limit_c = int(call["limit_c"])
        else:
            c0 = int(round(float(call["price"]) * 100))
            limit_c = min(99, c0 + SLIP, max(c0 + 1, 100 - MIN_REST_C))
        budget = min(float(t.get("usd_per_call") or 10),
                     max(0.0, float(t.get("usd_per_day") or 50) - _heute(pg, name)))
        # Seit 2.82.75: Stueckzahl nach dem erwarteten Preis (Call plus 1 Cent), nicht nach dem Limit. Vorher kaufte
        # ein Limit weit ueber dem Call nur 4 statt 7 Kontrakte, und vom Einsatz blieb ein Drittel liegen. Damit es
        # im schlechtesten Fall (alles erst am Limit gefuellt) nicht ausufert: hoechstens 10 % ueber dem Einsatz.
        if limit_c and not VOLL:
            anzahl = int(budget * 100 // limit_c)
        elif limit_c:
            erwartet_c = max(1, min(limit_c, int(round(float(call["price"]) * 100)) + 1))
            anzahl = int(budget * 100 // erwartet_c)
            anzahl = min(anzahl, int(budget * 1.10 * 100 // limit_c))
        else:
            anzahl = 0
        voll = False
        if CALL_CAP > 0 and anzahl >= 1:
            schon = int(pg.execute("SELECT COALESCE(sum(filled), 0) FROM optionen_auto WHERE pick_id=%s",
                                   (call["id"],)).fetchone()[0] or 0)
            frei = CALL_CAP - schon
            if frei < 1:
                voll = True
            anzahl = max(0, min(anzahl, frei))
        status, fehler, gefuellt, kosten, gebuehr = "skipped", None, 0, 0.0, 0.0
        ms, info = None, {"call_price": float(call["price"]), "budget": round(budget, 2)}
        darf, grund = True, None
        _t0 = time.time()
        info["t"] = {}
        if anzahl >= 1:
            # Seit 2.82.24: Bestand und Guthaben vor JEDEM Kauf, frisch von der Chain
            try:
                import optionen_plan
                darf, grund = optionen_plan.vor_kauf(pg, t)
            except Exception as e:
                darf, grund = False, f"plan check failed: {str(e)[:80]}"
            info["t"]["plan_s"] = round(time.time() - _t0, 2)
        if voll:
            fehler = f"call full: {CALL_CAP} contracts already bought for all accounts"
        elif anzahl < 1:
            fehler = "daily limit reached"
        elif not darf:
            fehler = grund
        else:
            # Seit 2.82.29: Kalshi V2 (POST /portfolio/events/orders). Der alte Weg /portfolio/orders ist bei Kalshi
            # abgeschaltet. V2 kennt nur das Ja-Buch: "bid" kauft Ja, "ask" verkauft Ja, und das ist dasselbe wie
            # Nein kaufen zu 1 - Preis. Wer Nein fuer hoechstens L kaufen will, stellt also "ask" zu 1 - L.
            ja = call["side"] == "yes"
            preis = limit_c / 100.0 if ja else 1 - limit_c / 100.0
            body = {"ticker": call["market"], "side": "bid" if ja else "ask", "count": str(anzahl),
                    "price": f"{preis:.2f}", "time_in_force": "immediate_or_cancel",
                    "self_trade_prevention_type": "taker_at_cross", "client_order_id": str(uuid.uuid4())}
            # Buch direkt vor der Order mitschreiben (seit 2.82.28). Ein Fehler hier haelt die Order nie auf.
            # Seit 2.82.80: Buch und Shard-Pruefung gleichzeitig (beides nur HTTP), vorher nacheinander.
            def _buch():
                import optionen_tiefe
                asks = optionen_tiefe.kaufseite(optionen_tiefe.buch_kalshi(
                    call["market"], lambda pf: _anfrage(t, "GET", pf)[1]), call["side"])
                return {"book": [[p_, q_] for p_, q_ in asks[:8]],
                        "depth": {"at_call": round(sum(q_ for p_, q_ in asks if p_ <= float(call["price"]) + 1e-9), 2),
                                  "at_limit": round(sum(q_ for p_, q_ in asks if p_ <= limit_c / 100.0 + 1e-9), 2)}}
            bedarf = round(anzahl * limit_c / 100.0 + anzahl * 0.02 + 0.05, 2)
            _t1 = time.time()
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as _ex:
                f_b = _ex.submit(_buch)
                f_s = _ex.submit(shard_finanzieren, t, call["market"], bedarf)
                try:
                    info.update(f_b.result(timeout=8))
                except Exception as e:
                    info["book_err"] = str(e)[:120]
                try:
                    ok_s, info["shard"] = f_s.result(timeout=10)
                except Exception as e:
                    ok_s, info["shard"] = True, {"err": str(e)[:120]}   # im Zweifel versuchen, Kalshi sagt dann nein
            info["t"]["book_shard_s"] = round(time.time() - _t1, 2)
            # Seit 2.82.106: Bestaetigung durch den Markt. Ohne Buch (Lesefehler) wird wie bisher gekauft.
            ueberspringen = False
            if MIN_MOVE_C > 0 and not call.get("limit_c") and info.get("book"):
                try:
                    ask_c = int(round(float(info["book"][0][0]) * 100))
                    call_c = int(round(float(call["price"]) * 100))
                    info["move"] = {"ask_c": ask_c, "call_c": call_c, "need_c": MIN_MOVE_C}
                    if ask_c < call_c + MIN_MOVE_C:
                        ueberspringen = True
                except Exception:
                    ueberspringen = False
            try:
                if ueberspringen:
                    raise _KeinKauf(f"price did not move: ask {info['move']['ask_c']}c, call {info['move']['call_c']}c")
                t0 = time.time()
                code, j = _anfrage(t, "POST", "/portfolio/events/orders", body)
                if code == 404 and "insufficient_shard_balance" in json.dumps(j):
                    time.sleep(1.0)   # seit 2.82.83: Umbuchung zwischen den Shards braucht einen Moment
                    info["retry"] = True
                    code, j = _anfrage(t, "POST", "/portfolio/events/orders", body)
                ms = int((time.time() - t0) * 1000)
                info["t"]["total_s"] = round(time.time() - _t0, 2)
                info["http"] = code
                info["sent"] = {k: body[k] for k in ("side", "count", "price")}
                info["order"] = {k: j.get(k) for k in ("order_id", "fill_count", "remaining_count", "average_fill_price",
                                                       "average_fee_paid", "ts_ms") if j.get(k) is not None}
                if code in (200, 201) and j.get("order_id"):
                    gefuellt = int(float(j.get("fill_count") or 0))
                    if gefuellt:
                        schnitt = float(j.get("average_fill_price") or preis)      # Ja-Preis
                        kosten = round(gefuellt * (schnitt if ja else 1 - schnitt), 4)
                        gebuehr = round(gefuellt * float(j.get("average_fee_paid") or 0), 4)
                    status = "filled" if gefuellt >= anzahl else ("partial" if gefuellt else "not_filled")
                else:
                    status, fehler = "error", f"{code}: {json.dumps(j)[:200]}"
                    if "shard" in json.dumps(j):
                        fehler = ("Kalshi keeps cash per exchange index and the crypto index had too little. "
                                  "The engine tried to move it over" + ("" if ok_s else " and Kalshi refused") + f". {code}: "
                                  + json.dumps(j)[:120])
            except _KeinKauf as e:
                status, fehler = "no_move", str(e)
            except Exception as e:
                status, fehler = "error", str(e)[:200]
        if gefuellt:
            info["avg_price"] = round(kosten / gefuellt, 4)
        pg.execute("""INSERT INTO optionen_auto (id, pick_id, tester, ticker, side, limit_c, wanted, filled, cost, fees,
                      status, error, ms, info) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                   (oid, call["id"], name, call["market"], call["side"], limit_c, anzahl, gefuellt, kosten, gebuehr,
                    status, fehler, ms, json.dumps(info, default=str)))
        log.info(f"autotrade {name} {call['market']} {call['side']} {gefuellt}/{anzahl} at <= {limit_c}c: {status} {fehler or ''}")
        if gefuellt:
            _melden(t, f"Engine bought for you: {gefuellt} {call['side'].upper()} on {NAMEN.get(call['coin'], call['coin'].upper())} "
                                  f"above ${float(call['K']):,.2f}, ${kosten + gebuehr:.2f} total.")
        elif status == "error":
            _melden(t, f"Engine could not buy the last call for you: {fehler}")


def ergebnisse(pg):
    puls()
    if not an():
        return
    _schema(pg)
    leute = {t["name"]: t for t in tester(pg)}
    rows = pg.execute("""SELECT a.id, a.tester, a.side, a.filled, a.cost, a.fees, p.won, p.coin, p.strike
                         FROM optionen_auto a JOIN (SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_picks UNION ALL SELECT id, won, settled, coin, strike, book_price, t_resolve, 'kalshi', created FROM optionen_live UNION ALL SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_neu_picks) p ON p.id = a.pick_id
                         WHERE a.filled > 0 AND NOT a.reported AND p.settled IS NOT NULL LIMIT 40""").fetchall()
    for aid, name, side, filled, cost, fees, won, coin, strike in rows:
        einsatz = float(cost) + float(fees or 0)
        zurueck = float(filled) if won else 0.0
        # erst als gemeldet markieren, dann Gebuehr: so wird nie doppelt abgerechnet
        if not pg.execute("UPDATE optionen_auto SET reported=TRUE WHERE id=%s AND NOT reported RETURNING id", (aid,)).fetchone():
            continue
        zusatz = ""
        if str(name).startswith("w:"):
            try:
                import optionen_plan
                g = optionen_plan.abrechnen(pg, name[2:], zurueck - einsatz)
                if g > 0:
                    zusatz = f" Fee ${g:.2f} from your $ODDS balance."
            except Exception as e:
                log.warning(f"autotrade fee {name}: {e}")
        _melden(leute.get(name) or ({"wallet": name[2:]} if str(name).startswith("w:") else {}), f"{'Hit' if won else 'Miss'}. {NAMEN.get(coin, coin.upper())} above ${float(strike):,.2f}, "
                             f"{side.upper()}: paid ${einsatz:.2f}, got back ${zurueck:.2f} ({zurueck - einsatz:+.2f}).{zusatz}")


def bericht():
    import optionen
    pg = optionen._pg()
    out = {"live": an(), "base_url": BASIS, "self_service": {"configured": bereit(), "open_to_all_holders": OFFEN,
                                                             "tier": STUFE, "invited": sorted(erlaubt_liste())}, "testers": []}
    if not pg:
        return out
    _schema(pg)
    out["self_service"]["users"] = [{"wallet": r[0], "status": "paused" if (r[2] and r[1] == "active") else r[1],
                                     "usd_per_call": float(r[3] or 0), "usd_per_day": float(r[4] or 0), "since": str(r[5])[:16]}
                                    for r in pg.execute("SELECT wallet, status, paused, usd_per_call, usd_per_day, created "
                                                        "FROM optionen_auto_user ORDER BY created DESC LIMIT 200").fetchall()]
    out["self_service"]["waiting_for_approval"] = [u["wallet"] for u in out["self_service"]["users"] if u["status"] == "pending"]
    try:
        import optionen_plan
        for u in out["self_service"]["users"]:
            u.update(optionen_plan.stand(pg, u["wallet"], u["usd_per_day"]))
        pl = optionen_plan.plaene()
        out["self_service"]["invited_plans"] = {w: pl.get(w, "holder") for w in out["self_service"]["invited"]}
        out["rules"] = {"min_pct": optionen_plan.MIN_PCT, "fee_pct": round(optionen_plan.FEE * 100, 1),
                        "fee_paid_pct": round(optionen_plan.FEE_PAID * 100, 1)}
        out["events"] = optionen_plan.ereignisse()
        optionen_plan._schema(pg)
        r = pg.execute("SELECT COALESCE(sum(fees), 0) FROM optionen_auto_fee").fetchone()
        out["fees_total_usd"] = round(float(r[0]), 2) if r else 0.0
    except Exception as e:
        log.debug(f"autotrade plan bericht: {e}")
    for t in tester(pg):
        name = t["name"]
        rows = pg.execute("""SELECT a.status, a.filled, a.cost, a.fees, p.won, p.settled IS NOT NULL, a.created
                             FROM optionen_auto a LEFT JOIN (SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_picks UNION ALL SELECT id, won, settled, coin, strike, book_price, t_resolve, 'kalshi', created FROM optionen_live UNION ALL SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_neu_picks) p ON p.id = a.pick_id WHERE a.tester=%s""",
                          (name,)).fetchall()
        fertig = [r for r in rows if r[5] and r[1]]
        einsatz = sum(float(r[2]) + float(r[3] or 0) for r in fertig)
        zurueck = sum(float(r[1]) for r in fertig if r[4])
        try:
            bal = guthaben(t)
            key_ok = bal is not None
        except Exception as e:
            bal, key_ok = None, f"key error: {str(e)[:80]}"
        out["testers"].append({
            "name": name, "key_ok": key_ok, "balance_usd": bal,
            "limits": {"usd_per_call": t.get("usd_per_call", 10), "usd_per_day": t.get("usd_per_day", 50),
                       "coins": t.get("coins") or ["btc", "eth"]},
            "orders": sum(1 for r in rows if r[0] != "no_move"), "filled": sum(1 for r in rows if r[1]),
            "not_filled": sum(1 for r in rows if r[0] == "not_filled"), "errors": sum(1 for r in rows if r[0] == "error"),
            "no_move": sum(1 for r in rows if r[0] == "no_move"), "move_rule": move_bericht(pg, name),
            "settled_spent": round(einsatz, 2), "settled_back": round(zurueck, 2),
            "pnl": round(zurueck - einsatz, 2),
            "since_channel": _seit_kanal([(r[6], r[1], r[2], r[3], r[4], r[4]) for r in fertig],
                                         lambda x: float(x[2]) + float(x[3] or 0),
                                         lambda x: (float(x[1]) if x[5] else 0.0) - float(x[2]) - float(x[3] or 0))})
    try:
        import storage
        b = json.loads(storage.kv_get("autotrade_buyer") or "{}")
        if b:
            b["age_s"] = int(time.time() - float(b.get("ts") or 0))
        out["buyer"] = b or None
    except Exception:
        out["buyer"] = None
    # Seit 2.82.37: jede Order einzeln fuer den Admin, offen oder abgerechnet, mit Ergebnis je Order
    try:
        rows = pg.execute("""SELECT a.created, a.tester, a.ticker, a.side, a.limit_c, a.wanted, a.filled, a.cost, a.fees,
                                    a.status, a.error, a.ms, p.coin, p.strike, p.price, p.won, p.settled, p.t_resolve, p.venue
                             FROM optionen_auto a LEFT JOIN (SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_picks UNION ALL SELECT id, won, settled, coin, strike, book_price, t_resolve, 'kalshi', created FROM optionen_live UNION ALL SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_neu_picks) p ON p.id = a.pick_id
                             ORDER BY a.created DESC LIMIT 30""").fetchall()
        liste, offen_usd, offen_n, fertig_n, ergebnis = [], 0.0, 0, 0, 0.0
        for r in rows:
            gef, kosten, geb = int(r[6] or 0), float(r[7] or 0), float(r[8] or 0)
            st = r[9] or ""
            if st == "error" or not gef:
                zustand = "error" if st == "error" else ("not filled" if st in ("not_filled", "partial", "filled") or not r[10] else "skipped")
                pnl = None
            elif r[16] is None:
                zustand, pnl = "open", None
                offen_usd += kosten + geb
                offen_n += 1
            else:
                pnl = round((gef if r[15] else 0) - kosten - geb, 2)
                zustand = "won" if r[15] else "lost"
                fertig_n += 1
                ergebnis += pnl
            liste.append({"ts": r[0].isoformat() if r[0] else None, "seat": str(r[1] or ""), "ticker": r[2],
                          "coin": (r[12] or "").upper() or None, "strike": float(r[13]) if r[13] is not None else None,
                          "side": r[3], "call_price": float(r[14]) if r[14] is not None else None,
                          "limit_c": r[4], "wanted": r[5], "filled": gef, "cost": round(kosten, 2), "fees": round(geb, 2),
                          "state": zustand, "pnl": pnl, "error": (r[10] or "")[:120] or None, "ms": r[11],
                          "resolves": r[17].isoformat() if r[17] else None, "venue": r[18],
                          # seit 2.82.45: bei nicht gekauften Orders, ob der Call selbst getroffen haette
                          "call_won": r[15] if (zustand in ("error", "not filled", "skipped") and r[16] is not None) else None})
        out["orders"] = liste
        out["orders_summary"] = {"open": offen_n, "open_usd": round(offen_usd, 2), "settled": fertig_n,
                                 "pnl": round(ergebnis, 2), "shown": len(liste)}
    except Exception as e:
        log.warning(f"autotrade orders list: {e}")
    # Seit 2.82.87: was der Kauf gegenueber dem Call-Preis kostet (alle Kaeufe seit den Channel Calls)
    try:
        from datetime import datetime as _dt, timezone as _tz
        rows = pg.execute("""SELECT a.filled, a.cost, a.fees, p.price, p.won, p.settled IS NOT NULL
                             FROM optionen_auto a LEFT JOIN (SELECT id, won, settled, price FROM optionen_picks UNION ALL SELECT id, won, settled, book_price FROM optionen_live UNION ALL SELECT id, won, settled, price FROM optionen_neu_picks) p ON p.id = a.pick_id
                             WHERE a.filled > 0 AND p.price IS NOT NULL AND a.created >= %s""",
                          (_dt.fromtimestamp(_kanal_ts(), _tz.utc),)).fetchall()
        n = sum(int(r[0]) for r in rows)
        bezahlt = sum(float(r[1]) for r in rows)
        zum_call = sum(int(r[0]) * float(r[3]) for r in rows)
        fertig = [r for r in rows if r[5]]
        echt = sum((int(r[0]) if r[4] else 0) - float(r[1]) - float(r[2] or 0) for r in fertig)
        call = sum((int(r[0]) if r[4] else 0) - int(r[0]) * float(r[3]) - float(r[2] or 0) for r in fertig)
        out["fill_gap"] = {"orders": len(rows), "contracts": n,
                           "avg_gap_c": round((bezahlt - zum_call) / n * 100, 2) if n else None,
                           "paid_extra_usd": round(bezahlt - zum_call, 2),
                           "settled_pnl_real": round(echt, 2), "settled_pnl_at_call": round(call, 2),
                           "since": int(_kanal_ts())}
    except Exception as e:
        log.debug(f"autotrade fill gap: {e}")
    # Seit 2.82.88: wie viel Menge stand beim Kauf wirklich im Buch (aus info.depth jedes Kaufs)
    try:
        import statistics as _st
        rows = pg.execute("""SELECT pick_id, sum(filled), max((info::jsonb->'depth'->>'at_call')::numeric),
                                    max((info::jsonb->'depth'->>'at_limit')::numeric),
                                    sum(CASE WHEN error LIKE 'call full%%' THEN 1 ELSE 0 END)
                             FROM optionen_auto WHERE created > now() - interval '7 days' AND info IS NOT NULL
                             GROUP BY pick_id""").fetchall()
        ac = [float(r[2]) for r in rows if r[2] is not None]
        al = [float(r[3]) for r in rows if r[3] is not None]
        out["capacity"] = {"calls": len(rows), "with_book": len(ac),
                           "at_call_median": round(_st.median(ac), 1) if ac else None,
                           "at_limit_median": round(_st.median(al), 1) if al else None,
                           "at_call_10_plus_pct": round(sum(1 for x in ac if x >= 10) / len(ac) * 100) if ac else None,
                           "at_limit_40_plus_pct": round(sum(1 for x in al if x >= 40) / len(al) * 100) if al else None,
                           "bought_median": round(_st.median([float(r[1] or 0) for r in rows]), 1) if rows else None,
                           "call_full_skips": int(sum(int(r[4] or 0) for r in rows)),
                           "cap": CALL_CAP, "seats": SEATS,
                           "active": int(pg.execute("SELECT count(*) FROM optionen_auto_user WHERE status='active'").fetchone()[0] or 0)}
    except Exception as e:
        out["capacity"] = {"error": str(e)[:120]}
    out["last_errors"] = [{"tester": r[0], "error": r[1]} for r in pg.execute(
        "SELECT tester, error FROM optionen_auto WHERE status='error' ORDER BY created DESC LIMIT 5").fetchall()]
    return out


def _zahl(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def kalshi_abgleich(pg=None, tage=3):
    """Seit 2.82.60: liest je Konto die echten Fills direkt bei Kalshi (nur lesen) und legt sie neben unsere Order-Liste.
    So sieht man, ob ein Trade im Konto von Hand oder ausserhalb der Engine kam, oder ob eine Order bei uns fehlt."""
    pg = pg or _pg()
    out = {"accounts": [], "days": tage}
    if not pg:
        return out
    _schema(pg)
    ab = time.time() - tage * 86400
    for t in tester(pg):
        konto = {"who": t["name"], "fills": [], "error": None}
        unsere = {}
        for oid, ticker, created, seite in pg.execute(
                """SELECT info->'order'->>'order_id', ticker, created, side FROM optionen_auto
                   WHERE tester=%s AND filled > 0 AND created > now() - make_interval(days => %s)""", (t["name"], tage + 1)).fetchall():
            if oid:
                unsere[oid] = {"ticker": ticker, "ts": created.isoformat() if created else None, "side": seite}
        roh, cur = [], None
        try:
            for _ in range(5):
                pfad = "/portfolio/fills?limit=200" + (f"&cursor={cur}" if cur else "")
                code, j = _anfrage(t, "GET", pfad)
                if code != 200:
                    konto["error"] = f"{code}: {json.dumps(j)[:160]}"
                    break
                roh += j.get("fills") or []
                cur = j.get("cursor")
                if not cur or not j.get("fills"):
                    break
        except Exception as e:
            konto["error"] = str(e)[:160]
        je = {}
        for f in roh:
            ts_txt = f.get("created_time") or f.get("ts") or ""
            try:
                ts = datetime.fromisoformat(str(ts_txt).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = None
            if ts is not None and ts < ab:
                continue
            oid = f.get("order_id") or f.get("fill_id")
            n = _zahl(f.get("count_fp")) or _zahl(f.get("count")) or 0
            ja = _zahl(f.get("yes_price_dollars"))
            if ja is None and _zahl(f.get("yes_price")) is not None:
                ja = _zahl(f.get("yes_price")) / 100.0
            x = je.setdefault(oid, {"order_id": oid, "ticker": f.get("ticker") or f.get("market_ticker"),
                                    "side": f.get("side"), "action": f.get("action"), "count": 0.0, "yes_sum": 0.0,
                                    "ts": ts_txt, "by_engine": oid in unsere})
            x["count"] += n
            if ja is not None:
                x["yes_sum"] += ja * n
        for x in je.values():
            x["yes_price"] = round(x["yes_sum"] / x["count"], 3) if x["count"] else None
            # Nein gekauft = Ja verkauft (V2 kennt nur das Ja-Buch)
            x["bought"] = "NO" if (x.get("action") == "sell" and x.get("side") == "yes") or \
                                  (x.get("action") == "buy" and x.get("side") == "no") else "YES"
            x.pop("yes_sum", None)
            # Seit 2.82.64: bei Engine-Orders die Seite aus unserer Order (Kalshi meldet bei V2 nur das Ja-Buch)
            if x["order_id"] in unsere and unsere[x["order_id"]].get("side"):
                x["bought"] = str(unsere[x["order_id"]]["side"]).upper()
            if x["yes_price"] is not None:
                x["paid"] = round(x["yes_price"] if x["bought"] == "YES" else 1 - x["yes_price"], 3)
        konto["fills"] = sorted(je.values(), key=lambda v: str(v["ts"]), reverse=True)
        fills_ids = set(je)
        konto["missing_at_kalshi"] = [dict(order_id=k, **{a: b for a, b in v.items() if a != "side"}) for k, v in unsere.items() if k not in fills_ids
                                      and v["ts"] and datetime.fromisoformat(v["ts"]).timestamp() >= ab]
        konto["outside_engine"] = sum(1 for v in je.values() if not v["by_engine"])
        out["accounts"].append(konto)
    return out


def _geld(v):
    v = float(v or 0)
    return ("+" if v > 0 else ("-" if v < 0 else "")) + f"${abs(v):,.2f}"


def _rest(sek):
    sek = max(0, int(sek))
    if sek < 3600:
        return f"{max(1, sek // 60)}m"
    if sek < 2 * 86400:
        return f"{sek // 3600}h {(sek % 3600) // 60}m"
    return f"{sek // 86400}d"


# Seit 2.82.86: Startpunkt "Auto mode kauft die Channel Calls" (OPT_CHANNEL_SINCE, ISO-Zeit UTC)
KANAL_SEIT = os.getenv("OPT_CHANNEL_SINCE", "2026-11-20T11:11:00+00:00").strip()


def _kanal_ts():
    try:
        from datetime import datetime
        return datetime.fromisoformat(KANAL_SEIT.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _seit_kanal(fertig, kosten, pnl):
    """Abgerechnete Kaeufe seit dem Umschalten auf Channel Calls. fertig: Zeilen mit created an Stelle 0."""
    ab = _kanal_ts()
    z = [x for x in fertig if x[0] is not None and x[0].timestamp() >= ab]
    eins = sum(kosten(x) for x in z)
    ges = sum(pnl(x) for x in z)
    return {"since": int(ab), "n": len(z), "won": sum(1 for x in z if x[5]), "pnl": round(ges, 2),
            "staked": round(eins, 2), "roi": round(ges / eins, 4) if eins else None}


_BAL = {}   # wallet -> (ts, balance), seit 2.82.82: Kalshi-Guthaben hoechstens alle 30 Sekunden lesen


def live_daten(adr):
    """Seit 2.82.82: das Live-Dashboard fuer den Nutzer selbst auf engine.oddsengine.live. Kennzahlen, offene
    Positionen, letzte Kaeufe, dazu der Live-Kurs aus optionen_kurs. Nur die eigene Wallet, nie der Schluessel."""
    a = _norm(adr)
    pg = _pg()
    out = {"ok": False}
    if not pg or not a:
        return out
    r = pg.execute("SELECT status, paused, usd_per_call, usd_per_day FROM optionen_auto_user WHERE wallet=%s", (a,)).fetchone()
    if not r:
        return out
    out.update(ok=True, status="paused" if (r[1] and r[0] == "active") else r[0],
               usd_per_call=float(r[2] or 0), usd_per_day=float(r[3] or 0))
    bar = None
    c = _BAL.get(a)
    if c and time.time() - c[0] < 30:
        bar = c[1]
    else:
        t = next((x for x in tester(pg) if x.get("wallet") == a), None)
        if t:
            try:
                code, j = _anfrage(t, "GET", "/portfolio/balance")
                if code == 200:
                    b = j.get("balance_dollars")
                    bar = float(b) if b is not None else float(j.get("balance") or 0) / 100.0
                    _BAL[a] = (time.time(), bar)
            except Exception as e:
                log.debug(f"autotrade live balance {a[:8]}: {e}")
    rows = pg.execute("""SELECT a.created, a.side, a.filled, a.cost, a.fees, p.won, p.settled IS NOT NULL, p.coin, p.strike,
                                EXTRACT(EPOCH FROM p.t_resolve), p.settled, p.price, a.ticker
                           FROM optionen_auto a
                           LEFT JOIN (SELECT id, won, settled, coin, strike, t_resolve, price FROM optionen_picks
                                      UNION ALL SELECT id, won, settled, coin, strike, t_resolve, list_price FROM optionen_live UNION ALL SELECT id, won, settled, coin, strike, t_resolve, price FROM optionen_neu_picks) p
                             ON p.id = a.pick_id
                          WHERE a.tester=%s AND a.filled > 0 ORDER BY a.created DESC""", ("w:" + a,)).fetchall()
    jetzt = time.time()

    def kosten(x):
        return float(x[3] or 0) + float(x[4] or 0)

    def pnl(x):
        return (float(x[2]) if x[5] else 0.0) - kosten(x)
    offen = [x for x in rows if not x[6]]
    fertig = [x for x in rows if x[6]]
    heute = [x for x in fertig if x[10] and (jetzt - x[10].timestamp()) < 86400]
    eins = sum(kosten(x) for x in fertig)
    ges = sum(pnl(x) for x in fertig)
    out["balance"] = round(bar, 2) if bar is not None else None
    out["in_play"] = round(sum(kosten(x) for x in offen), 2)
    out["last24"] = {"n": len(heute), "won": sum(1 for x in heute if x[5]), "pnl": round(sum(pnl(x) for x in heute), 2)}
    out["total"] = {"n": len(fertig), "won": sum(1 for x in fertig if x[5]), "pnl": round(ges, 2),
                    "staked": round(eins, 2), "roi": round(ges / eins, 4) if eins else None}
    out["since_channel"] = _seit_kanal(fertig, kosten, pnl)

    def zeile(x):
        n = float(x[2])
        return {"ts": int(x[0].timestamp()) if x[0] else None, "coin": x[7], "strike": float(x[8]) if x[8] is not None else None,
                "side": x[1], "contracts": int(n), "paid": round(kosten(x) / n, 3) if n else None,
                "call": float(x[11]) if x[11] is not None else None,
                "closes": int(float(x[9])) if x[9] else None, "settled": bool(x[6]),
                "won": (bool(x[5]) if x[6] else None), "pnl": round(pnl(x), 2) if x[6] else None}
    out["open"] = [zeile(x) for x in offen[:12]]
    out["recent"] = [zeile(x) for x in fertig[:15]]
    try:
        import json as _j
        import storage
        k = _j.loads(storage.kv_get("optionen_kurs") or "null") or {}
        out["price"] = {"ts": k.get("ts"), "coins": k.get("coins") or {}}
    except Exception:
        out["price"] = None
    return out


def ist_owner(adr):
    """Seit 2.82.81: Wallets aus OWNER_WALLETS (Railway, durch Komma getrennt) sehen alle Auto-mode-Konten."""
    a = _norm(adr)
    liste = {_norm(x) for x in os.getenv("OWNER_WALLETS", "").split(",") if x.strip()}
    return bool(a and a in liste)


def alle_text(kurz=False):
    """Seit 2.82.81: alle aktiven Auto-mode-Konten fuer den Owner, in Telegram (/autoall) und im Briefing."""
    pg = _pg()
    if not pg:
        return None
    teile = []
    for (w,) in pg.execute("""SELECT wallet FROM optionen_auto_user WHERE status='active' ORDER BY wallet""").fetchall():
        try:
            t = konto_text(w, kurz=kurz)
        except Exception as e:
            t = f"could not read: {str(e)[:60]}"
        if not t:
            continue
        kopf = f"{w[:6]}…{w[-4:]}"
        if kurz:
            t = t.replace("Auto mode\n", "", 1)
            teile.append(f"{kopf}\n{t}")
        else:
            teile.append(t.replace("Your Auto mode", f"Auto mode {kopf}", 1))
    if not teile:
        return "No active Auto mode accounts."
    kopf = "All Auto mode accounts" if kurz else f"All Auto mode accounts ({len(teile)})"
    return kopf + "\n\n" + ("\n\n".join(teile) if kurz else "\n\n────────\n\n".join(teile))


def konto_text(adr, kurz=False):
    """Seit 2.82.68: Auto mode fuer den Nutzer selbst, lesbar in Telegram: Kalshi-Guthaben, offene Positionen,
    heute, seit Start, letzte Ergebnisse. kurz=True fuer das Briefing. Liest bei Kalshi nur (Guthaben)."""
    a = _norm(adr)
    pg = _pg()
    if not pg or not a:
        return None
    r = pg.execute("SELECT status, paused, usd_per_call, usd_per_day FROM optionen_auto_user WHERE wallet=%s", (a,)).fetchone()
    if not r:
        return None
    t = next((x for x in tester(pg) if x.get("wallet") == a), None)
    bar = None
    if t:
        try:
            code, j = _anfrage(t, "GET", "/portfolio/balance")
            if code == 200:
                b = j.get("balance_dollars")
                bar = float(b) if b is not None else float(j.get("balance") or 0) / 100.0
        except Exception as e:
            log.debug(f"autotrade balance {a[:8]}: {e}")
    rows = pg.execute("""SELECT a.created, a.side, a.filled, a.cost, a.fees, p.won, p.settled IS NOT NULL, p.coin, p.strike,
                                EXTRACT(EPOCH FROM p.t_resolve), p.settled
                           FROM optionen_auto a
                           LEFT JOIN (SELECT id, won, settled, coin, strike, t_resolve FROM optionen_picks
                                      UNION ALL SELECT id, won, settled, coin, strike, t_resolve FROM optionen_live UNION ALL SELECT id, won, settled, coin, strike, t_resolve FROM optionen_neu_picks) p
                             ON p.id = a.pick_id
                          WHERE a.tester=%s AND a.filled > 0 ORDER BY a.created DESC""", ("w:" + a,)).fetchall()
    jetzt = time.time()

    def name(x):
        k = float(x[8]) if x[8] is not None else 0
        return f"{NAMEN.get(x[7], str(x[7] or '').upper())} above {k:,.2f}"

    def kosten(x):
        return float(x[3] or 0) + float(x[4] or 0)

    def pnl(x):
        return (float(x[2]) if x[5] else 0.0) - kosten(x)
    offen = [x for x in rows if not x[6]]
    fertig = [x for x in rows if x[6]]
    heute = [x for x in fertig if x[10] and (jetzt - x[10].timestamp()) < 86400]
    im_spiel = sum(kosten(x) for x in offen)
    gesamt = sum(pnl(x) for x in fertig)
    status = "paused" if (r[1] and r[0] == "active") else r[0]
    z = []
    if kurz:
        z.append("Auto mode")
        if bar is not None:
            z.append(f"Kalshi cash {_geld(bar).lstrip('+')}, {_geld(im_spiel).lstrip('+')} in {len(offen)} open position{'s' if len(offen) != 1 else ''}")
        if heute:
            g = sum(1 for x in heute if x[5])
            z.append(f"Last 24h: {g} won, {len(heute) - g} lost, {_geld(sum(pnl(x) for x in heute))}")
        else:
            z.append("Last 24h: nothing settled")
        sk = _seit_kanal(fertig, kosten, pnl)
        if sk["n"]:
            z.append(f"Since channel calls: {sk['won']} of {sk['n']} won, {_geld(sk['pnl'])}"
                     + (f" ({sk['roi'] * 100:+.1f}%)" if sk["roi"] is not None else ""))
        z.append(f"Since start: {len(fertig)} settled, {_geld(gesamt)}")
        return "\n".join(z)
    z.append(f"Your Auto mode · {status}")
    z.append(f"${float(r[2] or 0):g} per call, up to ${float(r[3] or 0):g} a day")
    z.append("")
    if bar is not None:
        z.append(f"Kalshi cash: {_geld(bar).lstrip('+')}")
    z.append(f"In play: {_geld(im_spiel).lstrip('+')} in {len(offen)} open position{'s' if len(offen) != 1 else ''}")
    if heute:
        g = sum(1 for x in heute if x[5])
        z.append(f"Last 24h: {g} won, {len(heute) - g} lost, {_geld(sum(pnl(x) for x in heute))}")
    sk = _seit_kanal(fertig, kosten, pnl)
    if sk["n"]:
        z.append(f"Since channel calls: {sk['won']} of {sk['n']} won, {_geld(sk['pnl'])}"
                 + (f", {sk['roi'] * 100:+.1f}% ROI" if sk["roi"] is not None else ""))
    z.append(f"Since start: {len(fertig)} settled, {sum(1 for x in fertig if x[5])} won, {_geld(gesamt)}")
    if offen:
        z += ["", "Open now"]
        for x in offen[:8]:
            rest = _rest(float(x[9]) - jetzt) if x[9] else "?"
            z.append(f"• {name(x)} {str(x[1]).upper()}, {int(float(x[2]))} at {round(kosten(x) / float(x[2]) * 100)}c, "
                     f"pays ${int(float(x[2]))} if right, closes in {rest}")
    if fertig:
        z += ["", "Last results"]
        for x in fertig[:5]:
            z.append(f"{'Won' if x[5] else 'Lost'} · {name(x)} {str(x[1]).upper()}: {_geld(pnl(x))}")
    z += ["", "Past results are no guarantee of future results."]
    return "\n".join(z)
