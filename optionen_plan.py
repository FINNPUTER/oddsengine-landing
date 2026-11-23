"""
optionen_plan.py: Auto-Modus, wer rein darf und was es kostet. Seit 2.82.24.

Drei Plaene je Wallet (der Betreiber setzt ihn im Admin, Standard "holder"):
  team    keine Gebuehr, kein Anteil noetig (seit 2.82.36, vorher AUTOTRADE_MIN_PCT)
  holder  Gebuehr AUTOTRADE_FEE vom Gewinn, muss AUTOTRADE_MIN_PCT halten
  paid    Gebuehr AUTOTRADE_FEE_PAID vom Gewinn, ohne Supply-Anteil (fuer alle, die den Anteil nicht halten)

Bestand: vor JEDEM Kauf frisch von der Chain (Wallet plus verknuepfte Wallets). Faellt jemand unter den Anteil,
weil er verkauft oder verschickt hat, wird sofort pausiert, er und der Betreiber bekommen eine Nachricht.
Weiter geht es erst, wenn der Anteil wieder da ist und der Nutzer in der Karte auf Resume klickt.
Ist die Chain nicht erreichbar, wird dieser eine Kauf ausgelassen, aber nicht pausiert.

Gebuehr: vom Netto-Gewinn ueber dem bisherigen Hoechststand (High-water mark). Jede abgerechnete Order
aendert den Netto-Stand, liegt er ueber dem Hoechststand, geht der Anteil sofort vom $ODDS-Guthaben ab
(ai_accounts.credit, dasselbe Guthaben wie Your Engine und AI, aufgeladen mit $ODDS). In roten Phasen faellt
nichts an, Verluste werden erst aufgeholt.
Damit nie eine Gebuehr offen bleibt: gekauft wird nur, solange das Guthaben mindestens
Gebuehrensatz x Tageslimit betraegt (bei 20 % und $60 am Tag also $12).

Burn: jede Gebuehr steht als Zeile 'auto-mode-fee' in ai_usage und zaehlt damit voll als Marge. Davon wird der
uebliche Anteil (MARGIN_BURN_PCT, Standard 50 %) im Admin unter "Burn" faellig und vom Betreiber signiert.

Einstellung: AUTOTRADE_MIN_PCT (1.0), AUTOTRADE_FEE (0.20), AUTOTRADE_FEE_PAID (0.35), ODDS_TOTAL_SUPPLY (nur Rueckfall).
"""
import json
import os
import time
from decimal import Decimal

from logger import log

MIN_PCT = float(os.getenv("AUTOTRADE_MIN_PCT", "1.0"))
FEE = float(os.getenv("AUTOTRADE_FEE", "0.20"))
FEE_PAID = float(os.getenv("AUTOTRADE_FEE_PAID", "0.35"))
PLAENE = ("team", "holder", "paid")
_SCHEMA = [False]
_BESTAND = {}
_SUPPLY = {"ts": 0.0, "v": None}


def _norm(a):
    return str(a or "").strip().lower()


def _schema(pg):
    if _SCHEMA[0]:
        return
    pg.execute("ALTER TABLE optionen_auto_user ADD COLUMN IF NOT EXISTS hold_pause BOOLEAN DEFAULT FALSE")
    pg.execute("ALTER TABLE optionen_auto_user ADD COLUMN IF NOT EXISTS notice_ts TIMESTAMPTZ")
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_auto_fee (
        wallet TEXT PRIMARY KEY, net NUMERIC DEFAULT 0, hwm NUMERIC DEFAULT 0, fees NUMERIC DEFAULT 0,
        updated TIMESTAMPTZ DEFAULT now())""")
    pg.execute("""CREATE TABLE IF NOT EXISTS ai_accounts (
        address TEXT PRIMARY KEY, credit NUMERIC(20,8) NOT NULL DEFAULT 0, deposited NUMERIC(20,8) NOT NULL DEFAULT 0,
        spent NUMERIC(20,8) NOT NULL DEFAULT 0, created TIMESTAMPTZ DEFAULT now())""")
    _SCHEMA[0] = True


# ── Plan ─────────────────────────────────────────────────────────────────

def plaene():
    try:
        import storage
        d = json.loads(storage.kv_get("autotrade_plan") or "{}")
        return {_norm(k): v for k, v in d.items() if v in PLAENE}
    except Exception:
        return {}


def plan(adr):
    return plaene().get(_norm(adr), "holder")


def plan_setzen(adr, p):
    if p not in PLAENE:
        return {"ok": False, "error": "plan must be team, holder or paid"}
    import storage
    d = plaene()
    d[_norm(adr)] = p
    storage.kv_set("autotrade_plan", json.dumps(d))
    return {"ok": True, "wallet": _norm(adr), "plan": p}


def satz(p):
    return 0.0 if p == "team" else (FEE_PAID if p == "paid" else FEE)


def braucht_anteil(p):
    # Seit 2.82.36: nur der Plan holder muss einen Anteil halten. Team ist vom Admin eingeladen und haelt nichts.
    return p == "holder" and MIN_PCT > 0


# ── Bestand ──────────────────────────────────────────────────────────────

def supply():
    if _SUPPLY["v"] and time.time() - _SUPPLY["ts"] < 6 * 3600:
        return _SUPPLY["v"]
    v = None
    try:
        import onchain
        m = onchain._meta([onchain.ODDS_CA]).get(onchain.ODDS_CA) or {}
        roh = int(m.get("supply_raw") or 0)
        if roh > 0:
            v = roh / 10 ** int(m.get("decimals") or 18)
    except Exception as e:
        log.debug(f"autotrade supply: {e}")
    if not v:
        v = float(os.getenv("ODDS_TOTAL_SUPPLY", "1000000000") or 1e9)
    _SUPPLY.update(ts=time.time(), v=v)
    return v


def bestand(adr, frisch=False):
    """{"tokens": .., "pct": ..} direkt von der Chain, Wallet plus verknuepfte Wallets. None: Chain nicht erreichbar."""
    a = _norm(adr)
    c = _BESTAND.get(a)
    if c and time.time() - c[0] < (20 if frisch else 300):
        return c[1]
    try:
        import onchain
        wallets = [a]
        try:
            import holder
            wallets += [_norm(x) for x in (holder._kv("links_" + a) or []) if str(x).startswith("0x") and len(str(x)) == 42]
        except Exception:
            pass
        m = onchain._meta([onchain.ODDS_CA]).get(onchain.ODDS_CA) or {}
        dez = int(m.get("decimals") or 18)
        r = onchain._multicall([(onchain.ODDS_CA, "0x70a08231" + onchain._pad(w)) for w in wallets])
        werte = [onchain._u(x) for x in r]
        if not werte or werte[0] is None:
            return None
        tokens = sum((x or 0) for x in werte) / 10 ** dez
        out = {"tokens": round(tokens, 2), "pct": round(tokens / supply() * 100, 4)}
    except Exception as e:
        log.debug(f"autotrade bestand {a[:8]}: {e}")
        return None
    _BESTAND[a] = (time.time(), out)
    return out


# ── Guthaben und Gebuehr ─────────────────────────────────────────────────

def guthaben(pg, adr):
    _schema(pg)
    r = pg.execute("SELECT credit FROM ai_accounts WHERE address=%s", (_norm(adr),)).fetchone()
    return float(r[0]) if r else 0.0


def mindest(p, usd_pro_tag):
    return round(satz(p) * float(usd_pro_tag or 0), 2)


def abrechnen(pg, adr, pnl):
    """Nach jeder abgerechneten Order. Gibt die abgezogene Gebuehr zurueck."""
    a = _norm(adr)
    _schema(pg)
    pg.execute("INSERT INTO optionen_auto_fee (wallet) VALUES (%s) ON CONFLICT DO NOTHING", (a,))
    r = pg.execute("UPDATE optionen_auto_fee SET net = net + %s, updated=now() WHERE wallet=%s RETURNING net, hwm",
                   (Decimal(str(round(pnl, 4))), a)).fetchone()
    net, hwm = float(r[0]), float(r[1])
    s = satz(plan(a))
    if net <= hwm:
        return 0.0
    gebuehr = round(s * (net - hwm), 4)
    pg.execute("UPDATE optionen_auto_fee SET hwm=%s, fees = fees + %s WHERE wallet=%s",
               (Decimal(str(net)), Decimal(str(gebuehr)), a))
    if gebuehr > 0:
        pg.execute("INSERT INTO ai_accounts (address) VALUES (%s) ON CONFLICT DO NOTHING", (a,))
        pg.execute("UPDATE ai_accounts SET credit = credit - %s, spent = spent + %s WHERE address=%s",
                   (Decimal(str(gebuehr)), Decimal(str(gebuehr)), a))
        # Seit 2.82.25: die Gebuehr laeuft als Marge in den Burn (margenburn.py, MARGIN_BURN_PCT, Standard 50 %).
        # Sie steht dafuer als eigene Zeile im Verbrauch, ohne Modellkosten, also zaehlt sie ganz als Marge.
        try:
            pg.execute("""INSERT INTO ai_usage (address, model, tokens_in, tokens_out, cost_or, charged, status)
                          VALUES (%s, 'auto-mode-fee', 0, 0, 0, %s, 'ok')""", (a, Decimal(str(gebuehr))))
        except Exception as e:
            log.warning(f"autotrade fee usage row: {e}")
    return gebuehr


# ── Vor dem Kauf ─────────────────────────────────────────────────────────

def _ereignis(text):
    try:
        import storage
        l = json.loads(storage.kv_get("autotrade_events") or "[]")
        l.append({"ts": int(time.time()), "text": text})
        storage.kv_set("autotrade_events", json.dumps(l[-40:]))
    except Exception:
        pass
    try:
        import requests
        tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = (os.getenv("AI_ALERT_CHAT_ID", "") or os.getenv("TELEGRAM_PRIVATE_CHAT_ID", "")).strip()
        if tok and chat:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": "Auto mode: " + text, "disable_web_page_preview": True}, timeout=15)
    except Exception:
        pass


def einladung_text(p):
    """Begruessung nach der Einladung, je Plan (seit 2.82.33). Nur Dinge, die der Code auch so macht."""
    kopf = "You are in. 🟢\n\n"
    was = "the engine buys its BTC and ETH Above X calls in your own Kalshi account. Your key, your money, your limits."
    if p == "team":
        mitte = "Your wallet has a team seat for Auto mode: " + was + "\n\nTeam seat means no fee."
    elif p == "paid":
        mitte = ("Your wallet has a seat for Auto mode: " + was + f"\n\nThe fee is {FEE_PAID * 100:.0f}% of new profit, "
                 "taken from your $ODDS balance. No new profit, no fee. You do not need to hold a share of the supply.")
    else:
        mitte = ("Your wallet has a holder seat for Auto mode: " + was + f"\n\nThe fee is {FEE * 100:.0f}% of new profit, "
                 "taken from your $ODDS balance. No new profit, no fee.")
    if braucht_anteil(p):
        mitte += f" Keep at least {MIN_PCT:g}% of the $ODDS supply in this wallet."
    ende = ("\n\nOne step left: open engine.oddsengine.live, connect this wallet and enter your Kalshi key in the Auto mode "
            "card. The card shows what is still missing. Once everything is green it runs, and you can pause it any time.")
    return kopf + mitte + ende


def einladung_melden(adr):
    """Schickt die Begruessung. -> 'telegram', 'inbox' oder 'failed'."""
    a = _norm(adr)
    p = plan(a)
    weg = "failed"
    try:
        import motor
        ok = motor.senden(a, einladung_text(p), [[("Set up Auto mode", "https://engine.oddsengine.live")]])
        weg = "telegram" if ok else "inbox"
    except Exception as e:
        log.warning(f"autotrade invite message {a[:8]}: {e}")
    _ereignis(f"invite message for {a[:6]}…{a[-4:]} ({p}): " + {"telegram": "sent in Telegram", "inbox": "Telegram not linked, waiting in the engine inbox",
                                                              "failed": "could not be sent"}[weg])
    return weg


def _nutzer_melden(adr, text):
    try:
        import motor
        motor.senden(_norm(adr), text)
    except Exception as e:
        log.debug(f"autotrade plan melden: {e}")


def vor_kauf(pg, t):
    """(True, None) oder (False, Grund). t: Eintrag aus optionen_auto.tester mit wallet, usd_per_day."""
    a = _norm(t.get("wallet"))
    if not a:
        return True, None          # Tester aus der Railway-Variable laufen wie bisher
    _schema(pg)
    p = plan(a)
    if braucht_anteil(p):
        b = bestand(a, frisch=True)
        if b is None:
            return False, "holding check not possible right now"
        if b["pct"] < MIN_PCT:
            pg.execute("UPDATE optionen_auto_user SET paused=TRUE, hold_pause=TRUE, updated=now() WHERE wallet=%s", (a,))
            _nutzer_melden(a, f"Auto mode is paused. Your wallet holds {b['pct']:.2f}% of the $ODDS supply, "
                              f"it needs {MIN_PCT:g}%. Top up and press Resume in the Auto mode card.")
            _ereignis(f"{a} paused, holds {b['pct']:.2f}% (needs {MIN_PCT:g}%).")
            return False, f"holds {b['pct']:.2f}%, needs {MIN_PCT:g}%"
    s = satz(p)
    if s > 0:
        brauch = mindest(p, t.get("usd_per_day"))
        hat = guthaben(pg, a)
        if hat < brauch:
            r = pg.execute("""UPDATE optionen_auto_user SET notice_ts=now() WHERE wallet=%s
                              AND (notice_ts IS NULL OR notice_ts < now() - interval '24 hours') RETURNING wallet""", (a,)).fetchone()
            if r:
                _nutzer_melden(a, f"Auto mode is waiting. Your $ODDS balance is ${hat:.2f}, it needs ${brauch:.2f} "
                                  f"for your daily limit. Top up in Your Engine and it continues by itself.")
                _ereignis(f"{a} waiting, balance ${hat:.2f} (needs ${brauch:.2f}).")
            return False, f"balance ${hat:.2f}, needs ${brauch:.2f}"
    return True, None


def weiter(pg, adr):
    """Nutzer klickt Resume nach einer Bestands-Pause. Nur wenn der Anteil wieder da ist."""
    a = _norm(adr)
    _schema(pg)
    r = pg.execute("SELECT hold_pause FROM optionen_auto_user WHERE wallet=%s", (a,)).fetchone()
    if not r or not r[0]:
        return True, None
    p = plan(a)
    if braucht_anteil(p):
        b = bestand(a, frisch=True)
        if b is None:
            return False, "The holding check is not possible right now. Try again in a minute."
        if b["pct"] < MIN_PCT:
            return False, f"Your wallet holds {b['pct']:.2f}% of the $ODDS supply. Auto mode needs {MIN_PCT:g}%."
    pg.execute("UPDATE optionen_auto_user SET hold_pause=FALSE WHERE wallet=%s", (a,))
    _ereignis(f"{a} resumed after a holding pause.")
    return True, None


def eintritt(adr):
    """Beim Eintragen des Schluessels. (True, None) oder (False, Text fuer den Nutzer)."""
    a = _norm(adr)
    p = plan(a)
    if not braucht_anteil(p):
        return True, None
    b = bestand(a, frisch=True)
    if b is None:
        return False, "The holding check is not possible right now. Try again in a minute."
    if b["pct"] < MIN_PCT:
        return False, (f"Auto mode needs {MIN_PCT:g}% of the $ODDS supply in your wallet. You hold {b['pct']:.2f}%. "
                       f"If you want in without that, message us.")
    return True, None


def stand(pg, adr, usd_pro_tag=None):
    """Fuer die Karte und den Admin."""
    a = _norm(adr)
    p = plan(a)
    out = {"plan": p, "fee_pct": round(satz(p) * 100, 1), "min_pct": MIN_PCT if braucht_anteil(p) else 0.0}
    b = bestand(a)
    out["holding_pct"] = None if b is None else b["pct"]
    if pg:
        _schema(pg)
        out["credit_usd"] = round(guthaben(pg, a), 2)
        out["credit_needed_usd"] = mindest(p, usd_pro_tag)
        r = pg.execute("SELECT net, hwm, fees FROM optionen_auto_fee WHERE wallet=%s", (a,)).fetchone()
        out["net_usd"] = round(float(r[0]), 2) if r else 0.0
        out["fees_usd"] = round(float(r[2]), 2) if r else 0.0
        r = pg.execute("SELECT hold_pause FROM optionen_auto_user WHERE wallet=%s", (a,)).fetchone()
        out["hold_pause"] = bool(r and r[0])
    return out


def ereignisse():
    try:
        import storage
        return list(reversed(json.loads(storage.kv_get("autotrade_events") or "[]")))[:15]
    except Exception:
        return []
