"""
brett.py: Launch Board. Seit 2.81.0.

Jeder neue Torch-Launch mit reinen Fakten aus unserem Scan: kein Urteil, keine Ampel, keine Woerter wie safe oder
risky. Nur was on chain steht, jede Zahl mit Beleg-Link. Oeffentlich unter engine.oddsengine.live/launches,
nur mit Schalter "launchboard" (oder Admin-Vorschau).

Ablauf: ein Hintergrund-Faden holt alle 90 s die neuen Torch-Coins und scannt pro Runde hoechstens BRETT_PRO_RUNDE
noch nicht gescannte (die neuesten zuerst). Die Fakten liegen im kv (lb:<token>), die Seite liest nur von dort.
Ein Coin wird nach 15 Minuten einmal neu gescannt (dann sind Dev-Verkaeufe und Startkaeufe aussagekraeftiger).
"""
import json
import os
import threading
import time

from logger import log

PRO_RUNDE = int(os.getenv("BRETT_PRO_RUNDE", "3"))
TAKT = int(os.getenv("BRETT_TAKT_S", "90"))
NEU_NACH = 15 * 60
_LOS = {"an": False}
_LISTE = {"ts": 0, "d": None}


def _kv_get(k):
    try:
        import storage
        v = storage.kv_get(k)
        return json.loads(v) if v else None
    except Exception:
        return None


def _kv_set(k, v):
    try:
        import storage
        storage.kv_set(k, json.dumps(v))
    except Exception:
        pass


def _p(x, n=2):
    """Prozent ohne ueberfluessige Nullen: 0, 3.2, 1.12."""
    return f"{round(float(x), n):g}"


def fakten(r):
    """Aus einem Scan-Ergebnis nur die Fakten fuer die Board-Karte. Fehlt ein Wert, fehlt die Zeile (nie geraten)."""
    sp, dv, s = r.get("supply") or {}, r.get("dev") or {}, r.get("safety") or {}
    z = []
    if dv.get("holds_pct") is not None:
        d = f"Dev holds {_p(dv['holds_pct'], 2)}%"
        out = dv.get("out") or {}
        if out.get("sold_pct") is not None:
            d += f", sold {_p(out['sold_pct'], 2)}%"
        z.append({"k": "dev", "t": d, "proof": dv.get("explorer")})
    lb = sp.get("launch_buys") or {}
    if lb.get("checked"):
        n = lb.get("wallets") or lb.get("count") or 0
        z.append({"k": "block_buys", "t": (f"Launch block buys: {n} wallets bought {_p(lb.get('bought_pct') or 0, 2)}%"
                                           + (f", {_p(lb['sold_pct'], 2)}% already sold" if lb.get("sold_pct") else ""))
                  if n else "Launch block buys: none"})
    if sp.get("bundled_total_pct") is not None:
        z.append({"k": "bundles", "t": f"Bundled supply: {_p(sp['bundled_total_pct'], 2)}%"})
    if sp.get("top10_pct") is not None:
        z.append({"k": "top10", "t": f"Top 10 holders: {_p(sp['top10_pct'], 1)}%"})
    if sp.get("holders"):
        z.append({"k": "holders", "t": f"Holders: {sp['holders']}"})
    tt = r.get("torch") or {}
    if tt:
        z.append({"k": "lp", "t": "Liquidity: Torch pool after graduation" if tt.get("graduated")
                  else f"Liquidity: in the Torch curve, {tt.get('progress_to_graduation_pct')}% to graduation"})
    elif s.get("lp_locked_pct") is not None:
        z.append({"k": "lp", "t": f"LP locked or burned: {_p(s['lp_locked_pct'], 1)}%"
                  + (f", {_p(s['lp_in_contracts_pct'], 1)}% in hook or other contracts" if (s.get("lp_in_contracts_pct") or 0) >= 10 else "")})
    if dv.get("launches_seen"):
        z.append({"k": "dev_history", "t": f"Dev launched {dv['launches_seen']} coins we have seen"})
    return z


def _scan(token):
    import scan
    r = scan.analyse(token, deep=True)
    if not r or not r.get("ok", True) or r.get("error"):
        return None
    return {"token": token, "ts": int(time.time()), "facts": fakten(r),
            "explorer": (r.get("links") or {}).get("explorer") or f"https://robinhoodchain.blockscout.com/token/{token}"}


def _runde():
    import onchain
    try:
        d = onchain.torch_markt("new", 20)
    except Exception as e:
        log.debug(f"brett torch: {e}")
        return
    coins = d.get("coins") or []
    _LISTE.update(ts=time.time(), d=coins)
    jetzt, gemacht = time.time(), 0
    for c in coins:
        if gemacht >= PRO_RUNDE:
            break
        t = str(c.get("token") or "").lower()
        if not t:
            continue
        alt = _kv_get(f"lb:{t}")
        if alt and (alt.get("rescanned") or jetzt - alt.get("ts", 0) < NEU_NACH):
            continue
        try:
            f = _scan(t)
        except Exception as e:
            log.debug(f"brett scan {t}: {e}")
            f = None
        if f:
            f["rescanned"] = bool(alt)
            _kv_set(f"lb:{t}", f)
        gemacht += 1


def _schleife():
    import features
    while True:
        try:
            if features.an("launchboard"):
                _runde()
        except Exception as e:
            log.debug(f"brett: {e}")
        time.sleep(TAKT)


def starten():
    if _LOS["an"] or os.getenv("BRETT_AUS") == "1":
        return
    _LOS["an"] = True
    threading.Thread(target=_schleife, name="brett", daemon=True).start()


def board(n=20):
    coins = _LISTE["d"]
    if coins is None or time.time() - _LISTE["ts"] > 300:
        try:
            import onchain
            coins = onchain.torch_markt("new", 20).get("coins") or []
            _LISTE.update(ts=time.time(), d=coins)
        except Exception:
            coins = coins or []
    out = []
    for c in coins[:max(1, min(int(n), 30))]:
        t = str(c.get("token") or "").lower()
        f = _kv_get(f"lb:{t}") if t else None
        out.append({"symbol": c.get("symbol"), "name": c.get("name"), "token": c.get("token"),
                    "launched_utc": c.get("launched_utc"), "market_cap_usd": c.get("market_cap_usd"),
                    "graduated": c.get("graduated"), "progress_pct": c.get("progress_to_graduation_pct"),
                    "trade": c.get("trade"), "facts": (f or {}).get("facts"), "scanned_utc": (f or {}).get("ts"),
                    "explorer": (f or {}).get("explorer") or f"https://robinhoodchain.blockscout.com/token/{t}",
                    "full_scan": f"https://t.me/OddsEnginePublicBot?start=scan_{t}"})
    return {"live": True, "chain": "Robinhood Chain", "source": "Torch launches, scanned by OddsEngine",
            "note": "Facts read on chain. No rating, no advice.", "coins": out}
