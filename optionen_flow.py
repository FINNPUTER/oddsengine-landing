"""
optionen_flow.py: wo das grosse Geld steht (seit 2.82.72). Phase 1: NUR MITSCHREIBEN, beeinflusst keinen Call.

Eigener Faden im Dienst crypto. Alles kostenlos und oeffentlich, jede Quelle einzeln abgesichert: faellt eine Boerse
aus oder sperrt sie (Binance und Bybit sperren US-Server), wird ohne sie gerechnet.

1. Orderbuch-Ungleichgewicht BTC und ETH, jede Minute: Kaufseite gegen Verkaufsseite bis 1 % und bis 2 % um die
   Mitte, zusammengerechnet ueber Coinbase, OKX, Kraken, Bitstamp, Binance, Bybit. imb = (Kauf - Verkauf) / Summe,
   also +1 nur Kaeufer, -1 nur Verkaeufer.  Tabelle optionen_flow.
2. Perps, alle 5 Minuten: Funding und Open Interest (USD) bei OKX und Bybit.  Tabelle optionen_perp.
3. Deribit-Optionen, alle 15 Minuten, fuer die naechsten Verfallstermine: gesamtes Open Interest, Strike mit dem
   meisten Open Interest und "Max Pain" (der Kurs, bei dem die Optionskaeufer insgesamt am wenigsten bekommen).
   Tabelle optionen_oi. Daraus der Pinning-Test: lag BTC beim Verfall (08:00 UTC) naeher an Max Pain als 24 h vorher?

Phase 2 (nach 1 bis 2 Wochen Daten): die Kalibrierung bekommt diese Werte als weitere Signale und nutzt sie nur,
wenn sie auf ungesehenen Calls zum echten Preis gewinnen (gleiche Regel wie die Chart-Signale).
"""
import threading
import time
from datetime import datetime, timezone

import requests

from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
T = 8
_ST = {"started": 0, "book_ok": {}, "book_fail": {}, "perp_ok": {}, "oi_ok": 0, "errors": 0, "last": {}}
_FADEN = [False]
_LETZT = {"perp": 0, "oi": 0}


def _schema(pg):
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_flow (
        coin TEXT, ts BIGINT, mid NUMERIC, bid1 NUMERIC, ask1 NUMERIC, bid2 NUMERIC, ask2 NUMERIC,
        imb1 NUMERIC, imb2 NUMERIC, sources TEXT, PRIMARY KEY (coin, ts))""")
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_perp (
        coin TEXT, ts BIGINT, src TEXT, funding NUMERIC, oi_usd NUMERIC, PRIMARY KEY (coin, ts, src))""")
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_oi (
        coin TEXT, ts BIGINT, expiry BIGINT, total_oi NUMERIC, max_oi_strike NUMERIC, max_pain NUMERIC,
        spot NUMERIC, PRIMARY KEY (coin, ts, expiry))""")


def _get(url, **params):
    r = requests.get(url, params=params, headers=UA, timeout=T)
    r.raise_for_status()
    return r.json()


# ── 1. Orderbuch ─────────────────────────────────────────────────────────

def _book(src, coin):
    """-> (bids, asks) als Listen (preis, menge in Coin)."""
    c = coin.upper()
    if src == "coinbase":
        j = _get(f"https://api.exchange.coinbase.com/products/{c}-USD/book", level=2)
        return [(float(p), float(q)) for p, q, *_ in j["bids"]], [(float(p), float(q)) for p, q, *_ in j["asks"]]
    if src == "okx":
        j = _get("https://www.okx.com/api/v5/market/books", instId=f"{c}-USDT", sz=400)["data"][0]
        return [(float(x[0]), float(x[1])) for x in j["bids"]], [(float(x[0]), float(x[1])) for x in j["asks"]]
    if src == "kraken":
        paar = "XBTUSD" if c == "BTC" else f"{c}USD"
        j = _get("https://api.kraken.com/0/public/Depth", pair=paar, count=500)["result"]
        v = next(iter(j.values()))
        return [(float(x[0]), float(x[1])) for x in v["bids"]], [(float(x[0]), float(x[1])) for x in v["asks"]]
    if src == "bitstamp":
        j = _get(f"https://www.bitstamp.net/api/v2/order_book/{coin.lower()}usd/")
        return [(float(p), float(q)) for p, q in j["bids"]], [(float(p), float(q)) for p, q in j["asks"]]
    if src == "binance":
        j = _get("https://api.binance.com/api/v3/depth", symbol=f"{c}USDT", limit=5000)
        return [(float(p), float(q)) for p, q in j["bids"]], [(float(p), float(q)) for p, q in j["asks"]]
    if src == "bybit":
        j = _get("https://api.bybit.com/v5/market/orderbook", category="spot", symbol=f"{c}USDT", limit=200)["result"]
        return [(float(p), float(q)) for p, q in j["b"]], [(float(p), float(q)) for p, q in j["a"]]
    return [], []


QUELLEN = ("coinbase", "okx", "kraken", "bitstamp", "binance", "bybit")


def buch_runde(pg):
    jetzt = int(time.time())
    for coin in ("btc", "eth"):
        bid1 = ask1 = bid2 = ask2 = 0.0
        mids, ok = [], []
        for src in QUELLEN:
            try:
                b, a = _book(src, coin)
                if not b or not a:
                    raise ValueError("empty")
                mid = (max(p for p, _ in b) + min(p for p, _ in a)) / 2
                mids.append(mid)
                bid1 += sum(p * q for p, q in b if p >= mid * 0.99)
                bid2 += sum(p * q for p, q in b if p >= mid * 0.98)
                ask1 += sum(p * q for p, q in a if p <= mid * 1.01)
                ask2 += sum(p * q for p, q in a if p <= mid * 1.02)
                ok.append(src)
                _ST["book_ok"][src] = _ST["book_ok"].get(src, 0) + 1
            except Exception:
                _ST["book_fail"][src] = _ST["book_fail"].get(src, 0) + 1
        if not ok:
            continue
        mid = sorted(mids)[len(mids) // 2]
        imb1 = (bid1 - ask1) / (bid1 + ask1) if bid1 + ask1 else 0
        imb2 = (bid2 - ask2) / (bid2 + ask2) if bid2 + ask2 else 0
        pg.execute("""INSERT INTO optionen_flow (coin, ts, mid, bid1, ask1, bid2, ask2, imb1, imb2, sources)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                   (coin, jetzt, round(mid, 2), round(bid1), round(ask1), round(bid2), round(ask2),
                    round(imb1, 4), round(imb2, 4), ",".join(ok)))
        _ST["last"][coin] = {"ts": jetzt, "bid2_musd": round(bid2 / 1e6, 1), "ask2_musd": round(ask2 / 1e6, 1),
                             "imb1": round(imb1, 3), "imb2": round(imb2, 3), "sources": ok}


# ── 2. Perps ─────────────────────────────────────────────────────────────

def perp_runde(pg):
    jetzt = int(time.time())
    for coin in ("btc", "eth"):
        c = coin.upper()
        try:
            f = float(_get("https://www.okx.com/api/v5/public/funding-rate", instId=f"{c}-USDT-SWAP")["data"][0]["fundingRate"])
            o = _get("https://www.okx.com/api/v5/public/open-interest", instType="SWAP", instId=f"{c}-USDT-SWAP")["data"][0]
            oi = float(o.get("oiUsd") or 0) or None
            pg.execute("INSERT INTO optionen_perp (coin, ts, src, funding, oi_usd) VALUES (%s,%s,'okx',%s,%s) ON CONFLICT DO NOTHING",
                       (coin, jetzt, f, oi))
            _ST["perp_ok"]["okx"] = _ST["perp_ok"].get("okx", 0) + 1
        except Exception:
            pass
        try:
            t = _get("https://api.bybit.com/v5/market/tickers", category="linear", symbol=f"{c}USDT")["result"]["list"][0]
            pg.execute("INSERT INTO optionen_perp (coin, ts, src, funding, oi_usd) VALUES (%s,%s,'bybit',%s,%s) ON CONFLICT DO NOTHING",
                       (coin, jetzt, float(t["fundingRate"]), float(t.get("openInterestValue") or 0) or None))
            _ST["perp_ok"]["bybit"] = _ST["perp_ok"].get("bybit", 0) + 1
        except Exception:
            pass


# ── 3. Deribit Open Interest, Max Pain ───────────────────────────────────

_MON = {m: i + 1 for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}


def _verfall(s):
    """'27NOV26' -> Unix-Zeit 08:00 UTC."""
    tag, mon, jahr = int(s[:-5]), _MON[s[-5:-2]], 2000 + int(s[-2:])
    return int(datetime(jahr, mon, tag, 8, 0, tzinfo=timezone.utc).timestamp())


def oi_runde(pg):
    jetzt = int(time.time())
    for coin in ("btc", "eth"):
        c = coin.upper()
        try:
            rows = _get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency", currency=c, kind="option")["result"]
            spot = _get("https://www.deribit.com/api/v2/public/get_index_price", index_name=f"{coin}_usd")["result"]["index_price"]
        except Exception:
            continue
        je = {}
        for r in rows:
            try:
                _, exp, k, art = r["instrument_name"].split("-")
                je.setdefault(exp, []).append((float(k), art, float(r.get("open_interest") or 0)))
            except Exception:
                continue
        for exp, opts in je.items():
            try:
                ts = _verfall(exp)
            except Exception:
                continue
            if ts < jetzt or ts > jetzt + 9 * 86400:
                continue                     # nur Verfaelle der naechsten 9 Tage
            ges = sum(o for _, _, o in opts)
            if ges <= 0:
                continue
            je_k = {}
            for k, art, o in opts:
                je_k[k] = je_k.get(k, 0) + o
            max_oi = max(je_k, key=je_k.get)
            strikes = sorted(je_k)
            best, mp = None, None
            for s in strikes:
                zahl = sum(o * max(0.0, s - k) for k, art, o in opts if art == "C") + \
                       sum(o * max(0.0, k - s) for k, art, o in opts if art == "P")
                if best is None or zahl < best:
                    best, mp = zahl, s
            pg.execute("""INSERT INTO optionen_oi (coin, ts, expiry, total_oi, max_oi_strike, max_pain, spot)
                          VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                       (coin, jetzt, ts, round(ges, 2), max_oi, mp, round(float(spot), 2)))
            _ST["oi_ok"] += 1


def faden_starten():
    if _FADEN[0]:
        return
    _FADEN[0] = True
    _ST["started"] = int(time.time())

    def lauf():
        import optionen
        while True:
            t0 = time.time()
            try:
                pg = optionen._pg()
                if pg:
                    _schema(pg)
                    buch_runde(pg)
                    if time.time() - _LETZT["perp"] > 300:
                        _LETZT["perp"] = time.time()
                        perp_runde(pg)
                    if time.time() - _LETZT["oi"] > 900:
                        _LETZT["oi"] = time.time()
                        oi_runde(pg)
                    try:
                        import json
                        import storage
                        storage.kv_set("optionen_flow_stand", json.dumps(_ST))
                    except Exception:
                        pass
            except Exception as e:
                _ST["errors"] += 1
                log.debug(f"flow: {e}")
            time.sleep(max(5, 60 - (time.time() - t0)))
    threading.Thread(target=lauf, daemon=True, name="options-flow").start()


def bericht():
    import json
    import optionen
    import storage
    out = {"status": None, "expiries": [], "pinning": [], "rows": {}}
    try:
        out["status"] = json.loads(storage.kv_get("optionen_flow_stand") or "null")
    except Exception:
        pass
    pg = optionen._pg()
    if not pg:
        return out
    try:
        _schema(pg)
        for t in ("optionen_flow", "optionen_perp", "optionen_oi"):
            out["rows"][t] = int(pg.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
        jetzt = int(time.time())
        for coin, exp, tot, mo, mp, spot, ts in pg.execute(
                """SELECT DISTINCT ON (coin, expiry) coin, expiry, total_oi, max_oi_strike, max_pain, spot, ts
                   FROM optionen_oi WHERE expiry > %s ORDER BY coin, expiry, ts DESC""", (jetzt,)).fetchall():
            out["expiries"].append({"coin": coin, "expiry": int(exp), "total_oi": float(tot), "max_oi_strike": float(mo),
                                    "max_pain": float(mp), "spot": float(spot), "ts": int(ts)})
        out["expiries"].sort(key=lambda x: (x["expiry"], x["coin"]))
        # Pinning-Test: vergangene Verfaelle mit Werten 24 h vorher und Kurs beim Verfall (Deribit spot der Zeile kurz danach)
        for coin, exp in pg.execute("SELECT DISTINCT coin, expiry FROM optionen_oi WHERE expiry <= %s ORDER BY expiry DESC LIMIT 20",
                                    (jetzt - 600,)).fetchall():
            vor = pg.execute("""SELECT max_pain, spot FROM optionen_oi WHERE coin=%s AND expiry=%s AND ts <= %s
                                ORDER BY ts DESC LIMIT 1""", (coin, exp, int(exp) - 86400)).fetchone()
            nach = pg.execute("""SELECT mid FROM optionen_flow WHERE coin=%s AND ts BETWEEN %s AND %s ORDER BY ts LIMIT 1""",
                              (coin, int(exp), int(exp) + 300)).fetchone()
            if vor and nach:
                mp, s24, s_end = float(vor[0]), float(vor[1]), float(nach[0])
                out["pinning"].append({"coin": coin, "expiry": int(exp), "max_pain": mp, "spot_24h_before": s24,
                                       "spot_at_expiry": s_end, "gap_before_pct": round(abs(s24 / mp - 1) * 100, 2),
                                       "gap_at_expiry_pct": round(abs(s_end / mp - 1) * 100, 2)})
        # Seit 2.82.79: sagt das Orderbuch die Richtung voraus? Jede Messung gegen den Kurs 60 Minuten spaeter.
        out["book_test"] = {}
        for coin in ("btc", "eth"):
            rows = [(int(t), float(m), float(i)) for t, m, i in pg.execute(
                "SELECT ts, mid, imb2 FROM optionen_flow WHERE coin=%s AND mid IS NOT NULL AND imb2 IS NOT NULL ORDER BY ts",
                (coin,)).fetchall()]
            paare, j = [], 0
            for t, m, i in rows:
                while j < len(rows) and rows[j][0] < t + 3600:
                    j += 1
                if j < len(rows) and rows[j][0] <= t + 3900:
                    paare.append((i, rows[j][1] / m - 1))
            if not paare:
                out["book_test"][coin] = {"n": 0}
                continue

            def gruppe(z):
                if not z:
                    return {"n": 0}
                return {"n": len(z), "up_share": round(sum(1 for _, r in z if r > 0) / len(z), 3),
                        "avg_move_pct": round(sum(r for _, r in z) / len(z) * 100, 3)}
            richtig = sum(1 for i, r in paare if (i > 0) == (r > 0) and r != 0)
            out["book_test"][coin] = {"n": len(paare), "direction_right": round(richtig / len(paare), 3),
                                      "more_buy": gruppe([x for x in paare if x[0] > 0]),
                                      "strong_buy": gruppe([x for x in paare if x[0] > 0.2]),
                                      "more_sell": gruppe([x for x in paare if x[0] < 0]),
                                      "since": rows[0][0]}
    except Exception as e:
        out["error"] = str(e)[:200]
    return out
