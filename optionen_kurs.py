"""
optionen_kurs.py: Live-Kurs BTC und ETH fuer die Admin-Seite (seit 2.82.78).

Kalshi rechnet die stuendlichen BTC- und ETH-Maerkte mit dem Index von CF Benchmarks ab (Mittel der letzten
60 Sekunden vor Schluss). Diesen Index gibt es nicht frei. Wir nehmen den Median der grossen Boersen, aus denen er
gebaut wird (Coinbase, Kraken, Bitstamp, Gemini). Das liegt nahe am Index, ist aber nicht derselbe Wert.

Eigener Faden im Dienst crypto: alle 5 Sekunden ein Kurs je Coin, im Speicher die letzten 3 Stunden, alle 10 Sekunden
die letzten 90 Minuten (10-Sekunden-Raster) in kv "optionen_kurs" fuer die Admin-Seite. Kauft nichts.
"""
import json
import statistics
import threading
import time

import requests

from logger import log

TAKT = 5
FENSTER_S = 3 * 3600
ZEIGEN_S = 90 * 60
UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
_FADEN = [False]
_REIHE = {"btc": [], "eth": []}          # [(ts, kurs)]
_ST = {"last": 0, "ok": {}, "fail": {}, "error": None}


def _coinbase(c):
    r = requests.get(f"https://api.exchange.coinbase.com/products/{c.upper()}-USD/ticker", headers=UA, timeout=4)
    return float(r.json()["price"]) if r.ok else None


def _kraken(c):
    paar = {"btc": "XBTUSD", "eth": "ETHUSD"}[c]
    r = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": paar}, headers=UA, timeout=4)
    if not r.ok:
        return None
    res = (r.json() or {}).get("result") or {}
    for v in res.values():
        return float(v["c"][0])
    return None


def _bitstamp(c):
    r = requests.get(f"https://www.bitstamp.net/api/v2/ticker/{c}usd/", headers=UA, timeout=4)
    return float(r.json()["last"]) if r.ok else None


def _gemini(c):
    r = requests.get(f"https://api.gemini.com/v1/pubticker/{c}usd", headers=UA, timeout=4)
    return float(r.json()["last"]) if r.ok else None


QUELLEN = {"coinbase": _coinbase, "kraken": _kraken, "bitstamp": _bitstamp, "gemini": _gemini}


def _kurs(c):
    werte = []
    for name, fn in QUELLEN.items():
        try:
            v = fn(c)
            if v and v > 0:
                werte.append(v)
                _ST["ok"][name] = _ST["ok"].get(name, 0) + 1
                continue
        except Exception:
            pass
        _ST["fail"][name] = _ST["fail"].get(name, 0) + 1
    return statistics.median(werte) if werte else None


def _speichern():
    jetzt = time.time()
    out = {"ts": int(jetzt), "step_s": 10, "coins": {}}
    for c, reihe in _REIHE.items():
        punkte, letzte = [], None
        for ts, v in reihe:
            if ts < jetzt - ZEIGEN_S:
                continue
            raster = int(ts) - int(ts) % 10
            if raster != letzte:
                punkte.append([raster, round(v, 2)])
                letzte = raster
            else:
                punkte[-1][1] = round(v, 2)
        letzte60 = [v for ts, v in reihe if ts >= jetzt - 60]
        out["coins"][c] = {"points": punkte, "now": round(reihe[-1][1], 2) if reihe else None,
                           "avg_60s": round(sum(letzte60) / len(letzte60), 2) if letzte60 else None}
    out["sources"] = {"ok": _ST["ok"], "fail": _ST["fail"]}
    try:
        import storage
        storage.kv_set("optionen_kurs", json.dumps(out))
    except Exception as e:
        _ST["error"] = str(e)[:120]


def faden_starten():
    """Eigener Faden im Dienst crypto. Mehrfacher Aufruf ist harmlos."""
    if _FADEN[0]:
        return
    _FADEN[0] = True

    def lauf():
        n = 0
        while True:
            t0 = time.time()
            try:
                for c in _REIHE:
                    v = _kurs(c)
                    if v:
                        _REIHE[c].append((time.time(), v))
                        while _REIHE[c] and _REIHE[c][0][0] < time.time() - FENSTER_S:
                            _REIHE[c].pop(0)
                n += 1
                if n % 2 == 0:
                    _speichern()
                _ST["last"] = int(time.time())
            except Exception as e:
                _ST["error"] = str(e)[:120]
                log.debug(f"kurs: {e}")
            time.sleep(max(1.0, TAKT - (time.time() - t0)))
    threading.Thread(target=lauf, daemon=True, name="options-price").start()


def bericht():
    """Fuer die Admin-Seite: Kursreihe plus offene Auto-mode-Positionen und offene Channel Calls (Schwelle, Seite)."""
    try:
        import storage
        d = json.loads(storage.kv_get("optionen_kurs") or "null") or {}
    except Exception:
        d = {}
    pos = []
    try:
        import optionen
        pg = optionen._pg()
        if pg:
            for ticker, side, preis, menge, t_res in pg.execute(
                    """SELECT a.ticker, a.side, a.cost / NULLIF(a.filled, 0), a.filled, COALESCE(p.t_resolve, l.t_resolve, n.t_resolve)
                         FROM optionen_auto a
                         LEFT JOIN optionen_picks p ON p.id = a.pick_id
                         LEFT JOIN optionen_live l ON l.id = a.pick_id
                         LEFT JOIN optionen_neu_picks n ON n.id = a.pick_id
                        WHERE a.filled > 0 AND a.created > now() - interval '2 days'
                          AND COALESCE(p.t_resolve, l.t_resolve, n.t_resolve) > now() - interval '2 minutes'
                        ORDER BY 5 LIMIT 20""").fetchall():
                tu = str(ticker).upper()
                coin = "btc" if "BTC" in tu else ("eth" if "ETH" in tu else ("sol" if "SOL" in tu else ("xrp" if "XRP" in tu else "?")))
                try:
                    k = float(str(ticker).rsplit("-T", 1)[1])
                except Exception:
                    k = None
                pos.append({"coin": coin, "ticker": ticker, "side": side, "strike": k, "paid": round(float(preis or 0), 2),
                            "contracts": int(menge or 0),
                            "closes": int(t_res.timestamp()) if t_res is not None else None})
    except Exception as e:
        d["pos_error"] = str(e)[:120]
    d["positions"] = pos
    d["note"] = ("Median of Coinbase, Kraken, Bitstamp and Gemini. Kalshi settles on the CF Benchmarks index, "
                 "averaged over the last 60 seconds before the close. Close to this line, not the same number.")
    return d
