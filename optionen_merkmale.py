"""
optionen_merkmale.py: was ein Trader auf dem Chart sieht, als Zahlen fuer die Kalibrierung (seit 2.82.68).

Eigene Kerzen-Ablage optionen_kerzen (5-Minuten-Schlusskurse BTC, ETH, SOL, XRP, 62 Tage), gefuellt von Bitstamp,
Coinbase als Rueckfall. Ein eigener Faden im Dienst crypto haelt sie aktuell (alle 5 Minuten).

Merkmale zu einem Zeitpunkt ts, nur aus Kerzen, die zu ts schon ABGESCHLOSSEN waren (kein Blick in die Zukunft):
  mom60   Bewegung der letzten 60 Minuten (log), mit Vorzeichen fuer unsere Seite (+ = laeuft fuer uns)
  mom15   dasselbe fuer 15 Minuten
  lrv     ln(echte Schwankung der letzten 2 Stunden / Schwankung laut Optionen). Negativ = ruhiger als erwartet
  sin_h, cos_h  Tageszeit (UTC) als Kreis
Fehlen Kerzen, gibt es None. Dann rechnet die Kalibrierung ohne Merkmale (wie bisher).
"""
import math
import threading
import time

import requests

from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
PAARE = {"btc": ("btcusd", "BTC-USD"), "eth": ("ethusd", "ETH-USD"), "sol": ("solusd", "SOL-USD"),
         "xrp": ("xrpusd", "XRP-USD")}
SEK = 300
TAGE = 62
_CACHE = {}      # coin -> {"ts": geladen, "c": {start_ts: close}}
_FADEN = [False]
_STAND = {"last": 0, "rows": {}, "error": None}


def _schema(pg):
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_kerzen (
        coin TEXT, ts BIGINT, c NUMERIC, PRIMARY KEY (coin, ts))""")


def _bitstamp(paar, start, ende):
    r = requests.get(f"https://www.bitstamp.net/api/v2/ohlc/{paar}/", headers=UA, timeout=12,
                     params={"step": SEK, "limit": 1000, "start": int(start), "end": int(ende)})
    if not r.ok:
        return {}
    d = ((r.json() or {}).get("data") or {}).get("ohlc") or []
    return {int(x["timestamp"]): float(x["close"]) for x in d if x.get("timestamp") and x.get("close")}


def _coinbase(prod, start, ende):
    from datetime import datetime, timezone
    out, t = {}, int(start)
    while t < ende:
        e = min(int(ende), t + 290 * SEK)
        r = requests.get(f"https://api.exchange.coinbase.com/products/{prod}/candles", headers=UA, timeout=10,
                         params={"granularity": SEK, "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                                 "end": datetime.fromtimestamp(e, timezone.utc).isoformat()})
        if r.ok:
            for x in r.json() or []:
                if isinstance(x, list) and len(x) > 4:
                    out[int(x[0])] = float(x[4])
        t = e
        time.sleep(0.3)
    return out


def fuellen(pg):
    """Fehlende Kerzen nachladen. Beim ersten Mal 62 Tage, danach nur das Neue."""
    _schema(pg)
    jetzt = int(time.time())
    for coin, (bs, cb) in PAARE.items():
        try:
            letzte = pg.execute("SELECT max(ts) FROM optionen_kerzen WHERE coin=%s", (coin,)).fetchone()[0]
            start = int(letzte) + SEK if letzte else jetzt - TAGE * 86400
            neu = {}
            t = start
            while t < jetzt - SEK:
                ende = min(jetzt, t + 999 * SEK)
                try:
                    d = _bitstamp(bs, t, ende)
                except Exception:
                    d = {}
                if not d:
                    try:
                        d = _coinbase(cb, t, ende)
                    except Exception:
                        d = {}
                neu.update({k: v for k, v in d.items() if t <= k <= ende})
                t = ende + SEK
                time.sleep(0.4)
            for k, v in neu.items():
                pg.execute("INSERT INTO optionen_kerzen (coin, ts, c) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                           (coin, k, v))
            pg.execute("DELETE FROM optionen_kerzen WHERE coin=%s AND ts < %s", (coin, jetzt - (TAGE + 3) * 86400))
            _STAND["rows"][coin] = int(pg.execute("SELECT count(*) FROM optionen_kerzen WHERE coin=%s", (coin,)).fetchone()[0])
            _CACHE.pop(coin, None)
        except Exception as e:
            _STAND["error"] = f"{coin}: {str(e)[:120]}"
            log.debug(f"merkmale fuellen {coin}: {e}")
    _STAND["last"] = int(time.time())


def faden_starten():
    """Eigener Faden im Dienst crypto. Mehrfacher Aufruf ist harmlos."""
    if _FADEN[0]:
        return
    _FADEN[0] = True

    def lauf():
        import optionen
        while True:
            try:
                pg = optionen._pg()
                if pg:
                    fuellen(pg)
            except Exception as e:
                _STAND["error"] = str(e)[:120]
            time.sleep(SEK)
    threading.Thread(target=lauf, daemon=True, name="options-candles").start()


def _reihe(pg, coin):
    c = _CACHE.get(coin)
    if c and time.time() - c["ts"] < 240:
        return c["c"]
    d = {}
    try:
        for ts, v in pg.execute("SELECT ts, c FROM optionen_kerzen WHERE coin=%s", (coin,)).fetchall():
            d[int(ts)] = float(v)
    except Exception:
        pass
    _CACHE[coin] = {"ts": time.time(), "c": d}
    return d


def _schluss(reihe, ts):
    """Schlusskurs der letzten Kerze, die zu ts schon abgeschlossen war."""
    b = int(ts) - int(ts) % SEK - SEK
    for d in (0, -SEK, -2 * SEK):
        if b + d in reihe:
            return reihe[b + d]
    return None


def merkmale(pg, coin, ts, side, iv):
    """-> dict oder None. side 'yes' = ueber der Schwelle, 'no' = darunter. iv als Anteil (0.45 = 45 %)."""
    r = _reihe(pg, coin)
    if not r:
        return None
    s0 = _schluss(r, ts)
    s60 = _schluss(r, ts - 3600)
    s15 = _schluss(r, ts - 900)
    if not (s0 and s60 and s15):
        return None
    rets = []
    for k in range(24):
        a, b = _schluss(r, ts - (k + 1) * SEK), _schluss(r, ts - k * SEK)
        if a and b:
            rets.append(math.log(b / a))
    if len(rets) < 12 or not iv or iv <= 0:
        return None
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1))
    rv = sd * math.sqrt(365.25 * 86400 / SEK)
    vz = 1.0 if side == "yes" else -1.0
    stunde = (int(ts) % 86400) / 3600.0
    return {"mom60": vz * math.log(s0 / s60) * 100, "mom15": vz * math.log(s0 / s15) * 100,
            "lrv": math.log(max(rv, 1e-4) / iv), "sin_h": math.sin(2 * math.pi * stunde / 24),
            "cos_h": math.cos(2 * math.pi * stunde / 24)}


NAMEN = ("mom60", "mom15", "lrv", "sin_h", "cos_h")


def vektor(f):
    return [float(f[k]) for k in NAMEN] if f else None


def stand():
    return dict(_STAND)
