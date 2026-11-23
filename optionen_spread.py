"""
optionen_spread.py: Spread-Test zwischen grossen Boersen (seit 2.82.85). NUR MESSEN, kauft und verkauft nie.

Frage: Gibt es zwischen den grossen Boersen fuer BTC, ETH, SOL, XRP und DOGE Momente, in denen man auf der einen
Boerse guenstiger kaufen kann, als man auf der anderen sofort verkaufen kann, und bleibt NACH den Gebuehren etwas uebrig?

Alle 10 Sekunden je Boerse das beste Kauf- und Verkaufsangebot (oberste Stufe im Orderbuch). Verglichen wird nur
innerhalb derselben Waehrung: USD-Boersen untereinander, USDT-Boersen untereinander (ein Tausch USD gegen USDT waere
ein eigener, nicht gemessener Schritt). Annahme: Geld liegt schon auf beiden Boersen, kein Transfer, beide Seiten
gleichzeitig als Taker zum Basistarif (OPT_SPREAD_FEES, JSON, in Prozent je Boerse). Binance und Bybit sperren
Anfragen aus den USA, deshalb fehlen sie.

Gespeichert wird je Gelegenheit (Coin, Kaufboerse, Verkaufsboerse) eine Episode: Beginn, Ende, groesster Netto-
Vorsprung, Menge an der obersten Stufe. Dazu je Runde Zaehler, damit man sieht, wie oft es sich lohnt.
"""
import json
import os
import statistics
import threading
import time

import requests

from logger import log

TAKT = float(os.getenv("OPT_SPREAD_SECONDS", "10"))
COINS = ("btc", "eth", "sol", "xrp", "doge")
UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
GEBUEHR = {"coinbase": 0.60, "kraken": 0.40, "bitstamp": 0.40, "gemini": 0.40, "cryptocom": 0.50,
           "okx": 0.10, "kucoin": 0.10, "gate": 0.20, "bitget": 0.10, "mexc": 0.05,
           # seit 2.82.87: Perps (Taker, Grundtarif)
           "okx_perp": 0.05, "bitget_perp": 0.06, "mexc_perp": 0.02, "blofin_perp": 0.06,
           # seit 2.82.98, nur im Coin-Finder: weitere Boersen (Taker, Grundtarif)
           "htx": 0.20, "bitmart": 0.25, "bingx": 0.10}
try:
    GEBUEHR.update({k: float(v) for k, v in json.loads(os.getenv("OPT_SPREAD_FEES", "{}") or "{}").items()})
except Exception:
    pass
QUOTE = {"coinbase": "usd", "kraken": "usd", "bitstamp": "usd", "gemini": "usd", "cryptocom": "usd",
         "okx": "usdt", "kucoin": "usdt", "gate": "usdt", "bitget": "usdt", "mexc": "usdt",
         "okx_perp": "perp", "bitget_perp": "perp", "mexc_perp": "perp", "blofin_perp": "perp"}
_FADEN = [False]
_SCHEMA = [False]
_ST = {"rounds": 0, "ok": {}, "fail": {}, "last": 0, "error": None, "since": 0,
       "coins": {c: {"rounds": 0, "pos": 0, "gross": [], "best_net": None} for c in COINS},
       "perp_coins": {c: {"rounds": 0, "pos": 0, "gross": [], "best_net": None} for c in COINS}}
_OFFEN = {}   # (coin, kauf, verkauf) -> Episode


def _g(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=5)
    r.raise_for_status()
    return r.json()


def _f(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except Exception:
        return None


# Jede Funktion: -> {coin: (bid, bid_size_coin, ask, ask_size_coin)}; Groessen None, wenn die Boerse keine liefert
def _coinbase():
    out = {}
    for c in COINS:
        j = _g(f"https://api.exchange.coinbase.com/products/{c.upper()}-USD/book", {"level": 1})
        b, a = (j.get("bids") or [[None, None]])[0], (j.get("asks") or [[None, None]])[0]
        out[c] = (_f(b[0]), _f(b[1]), _f(a[0]), _f(a[1]))
    return out


def _kraken():
    paare = {"btc": "XBTUSD", "eth": "ETHUSD", "sol": "SOLUSD", "xrp": "XRPUSD", "doge": "XDGUSD"}
    j = _g("https://api.kraken.com/0/public/Ticker", {"pair": ",".join(paare.values())})
    res = j.get("result") or {}
    namen = {"btc": ("XXBTZUSD", "XBTUSD"), "eth": ("XETHZUSD", "ETHUSD"), "sol": ("SOLUSD",),
             "xrp": ("XXRPZUSD", "XRPUSD"), "doge": ("XDGUSD", "XXDGZUSD")}
    out = {}
    for c in paare:
        v = next((res[k] for k in namen[c] if k in res), None)
        if v:
            out[c] = (_f(v["b"][0]), _f(v["b"][2]), _f(v["a"][0]), _f(v["a"][2]))
    return out


def _bitstamp():
    out = {}
    for c in COINS:
        j = _g(f"https://www.bitstamp.net/api/v2/ticker/{c}usd/")
        out[c] = (_f(j.get("bid")), None, _f(j.get("ask")), None)
    return out


def _gemini():
    out = {}
    for c in COINS:
        j = _g(f"https://api.gemini.com/v1/pubticker/{c}usd")
        out[c] = (_f(j.get("bid")), None, _f(j.get("ask")), None)
    return out


def _cryptocom():
    j = _g("https://api.crypto.com/exchange/v1/public/get-tickers")
    d = {x.get("i"): x for x in ((j.get("result") or {}).get("data") or [])}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}_USD")
        if x:
            out[c] = (_f(x.get("b")), None, _f(x.get("k")), None)
    return out


def _okx():
    j = _g("https://www.okx.com/api/v5/market/tickers", {"instType": "SPOT"})
    d = {x.get("instId"): x for x in j.get("data") or []}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}-USDT")
        if x:
            out[c] = (_f(x.get("bidPx")), _f(x.get("bidSz")), _f(x.get("askPx")), _f(x.get("askSz")))
    return out


def _kucoin():
    out = {}
    for c in COINS:
        x = (_g("https://api.kucoin.com/api/v1/market/orderbook/level1", {"symbol": f"{c.upper()}-USDT"}) or {}).get("data") or {}
        out[c] = (_f(x.get("bestBid")), _f(x.get("bestBidSize")), _f(x.get("bestAsk")), _f(x.get("bestAskSize")))
    return out


def _gate():
    j = _g("https://api.gateio.ws/api/v4/spot/tickers")
    d = {x.get("currency_pair"): x for x in j or []}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}_USDT")
        if x:
            out[c] = (_f(x.get("highest_bid")), None, _f(x.get("lowest_ask")), None)
    return out


def _bitget():
    j = _g("https://api.bitget.com/api/v2/spot/market/tickers")
    d = {x.get("symbol"): x for x in j.get("data") or []}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}USDT")
        if x:
            out[c] = (_f(x.get("bidPr")), _f(x.get("bidSz")), _f(x.get("askPr")), _f(x.get("askSz")))
    return out


def _mexc():
    j = _g("https://api.mexc.com/api/v3/ticker/bookTicker")
    d = {x.get("symbol"): x for x in j or []}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}USDT")
        if x:
            out[c] = (_f(x.get("bidPrice")), _f(x.get("bidQty")), _f(x.get("askPrice")), _f(x.get("askQty")))
    return out


# ── Perps (seit 2.82.87). Mengen in Kontrakten statt Coins, deshalb ohne Menge (None) ──
_FUND_LIVE = {}   # (boerse, coin) -> letzte Funding-Rate aus den Tickern


def _okx_perp():
    j = _g("https://www.okx.com/api/v5/market/tickers", {"instType": "SWAP"})
    d = {x.get("instId"): x for x in j.get("data") or []}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}-USDT-SWAP")
        if x:
            out[c] = (_f(x.get("bidPx")), None, _f(x.get("askPx")), None)
    return out


def _bitget_perp():
    j = _g("https://api.bitget.com/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"})
    d = {x.get("symbol"): x for x in j.get("data") or []}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}USDT")
        if x:
            out[c] = (_f(x.get("bidPr")), _f(x.get("bidSz")), _f(x.get("askPr")), _f(x.get("askSz")))
            try:
                _FUND_LIVE[("bitget_perp", c)] = float(x.get("fundingRate"))
            except Exception:
                pass
    return out


def _mexc_perp():
    j = _g("https://contract.mexc.com/api/v1/contract/ticker")
    d = {x.get("symbol"): x for x in (j.get("data") or [])} if isinstance(j.get("data"), list) else {}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}_USDT")
        if x:
            out[c] = (_f(x.get("bid1")), None, _f(x.get("ask1")), None)
            try:
                _FUND_LIVE[("mexc_perp", c)] = float(x.get("fundingRate"))
            except Exception:
                pass
    return out


def _blofin_perp():
    j = _g("https://openapi.blofin.com/api/v1/market/tickers")
    d = {x.get("instId"): x for x in j.get("data") or []}
    out = {}
    for c in COINS:
        x = d.get(f"{c.upper()}-USDT")
        if x:
            out[c] = (_f(x.get("bidPrice")), None, _f(x.get("askPrice")), None)
    return out


BOERSEN = {"coinbase": _coinbase, "kraken": _kraken, "bitstamp": _bitstamp, "gemini": _gemini, "cryptocom": _cryptocom,
           "okx": _okx, "kucoin": _kucoin, "gate": _gate, "bitget": _bitget, "mexc": _mexc,
           "okx_perp": _okx_perp, "bitget_perp": _bitget_perp, "mexc_perp": _mexc_perp, "blofin_perp": _blofin_perp}


def _schema(pg):
    if _SCHEMA[0]:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_spread (
        id BIGSERIAL PRIMARY KEY, coin TEXT, buy_ex TEXT, sell_ex TEXT, quote TEXT, started TIMESTAMPTZ, ended TIMESTAMPTZ,
        seconds NUMERIC, best_net_pct NUMERIC, best_gross_pct NUMERIC, buy_px NUMERIC, sell_px NUMERIC,
        size_usd NUMERIC, profit_usd NUMERIC)""")
    # seit 2.82.89: alte Zeilen bereinigen, bei denen eine Seite keine Menge hatte (Gewinn war zu hoch gerechnet)
    ohne = ("bitstamp", "gemini", "cryptocom", "gate", "okx_perp", "mexc_perp", "blofin_perp")
    pg.execute("UPDATE optionen_spread SET size_usd=0, profit_usd=0 WHERE (buy_ex = ANY(%s) OR sell_ex = ANY(%s)) AND size_usd > 0",
               (list(ohne), list(ohne)))
    _SCHEMA[0] = True


def _runde(pg):
    # Alle Boersen gleichzeitig abfragen, damit die Kurse aus demselben Moment stammen (sonst Schein-Spreads)
    from concurrent.futures import ThreadPoolExecutor
    daten = {}
    with ThreadPoolExecutor(max_workers=len(BOERSEN)) as ex:
        laeufe = {name: ex.submit(fn) for name, fn in BOERSEN.items()}
        for name, f in laeufe.items():
            try:
                daten[name] = f.result(timeout=8)
                _ST["ok"][name] = _ST["ok"].get(name, 0) + 1
            except Exception:
                _ST["fail"][name] = _ST["fail"].get(name, 0) + 1
    jetzt = time.time()
    gesehen = set()
    for c in COINS:
        cs, ps = _ST["coins"][c], _ST["perp_coins"][c]
        bester, bester_p = None, None
        for q in ("usd", "usdt", "perp"):
            kurse = {ex: d[c] for ex, d in daten.items() if QUOTE[ex] == q and c in d and d[c][0] and d[c][2]}
            for kauf, (_, _, ask, ask_sz) in kurse.items():
                for verk, (bid, bid_sz, _, _) in kurse.items():
                    if kauf == verk:
                        continue
                    brutto = (bid - ask) / ask * 100
                    netto = brutto - GEBUEHR.get(kauf, 0.2) - GEBUEHR.get(verk, 0.2)
                    if q == "perp":
                        # Perps: Gewinn erst beim Schliessen beider Seiten, also Gebuehren doppelt
                        netto -= GEBUEHR.get(kauf, 0.2) + GEBUEHR.get(verk, 0.2)
                        if bester_p is None or brutto > bester_p:
                            bester_p = brutto
                    elif bester is None or brutto > bester:
                        bester = brutto
                    if netto <= 0:
                        continue
                    # 0, sobald eine Seite keine Menge nennt (bis 2.82.88 zaehlte dann die andere Seite allein: zu hoch)
                    groesse = (min(ask_sz, bid_sz) * ask) if (ask_sz and bid_sz) else 0
                    k = (c, kauf, verk)
                    gesehen.add(k)
                    e = _OFFEN.get(k)
                    if not e:
                        e = _OFFEN[k] = {"start": jetzt, "net": netto, "gross": brutto, "buy": ask, "sell": bid,
                                         "size": groesse, "quote": q}
                    if netto > e["net"]:
                        e.update(net=netto, gross=brutto, buy=ask, sell=bid, size=groesse)
                    e["last"] = jetzt
        if bester is not None:
            cs["rounds"] += 1
            cs["gross"] = (cs["gross"] + [round(bester, 4)])[-500:]
            if any(k[0] == c and QUOTE.get(k[1]) != "perp" for k in gesehen):
                cs["pos"] += 1
        if bester_p is not None:
            ps["rounds"] += 1
            ps["gross"] = (ps["gross"] + [round(bester_p, 4)])[-500:]
            if any(k[0] == c and QUOTE.get(k[1]) == "perp" for k in gesehen):
                ps["pos"] += 1
    # Episoden schliessen, die in dieser Runde nicht mehr da sind
    for k in [k for k in _OFFEN if k not in gesehen]:
        e = _OFFEN.pop(k)
        sek = max(TAKT, e.get("last", e["start"]) - e["start"] + TAKT)
        gewinn = (e["size"] or 0) * e["net"] / 100
        if pg:
            try:
                pg.execute("""INSERT INTO optionen_spread (coin, buy_ex, sell_ex, quote, started, ended, seconds, best_net_pct,
                              best_gross_pct, buy_px, sell_px, size_usd, profit_usd)
                              VALUES (%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s)""",
                           (k[0], k[1], k[2], e["quote"], e["start"], jetzt, round(sek, 1), round(e["net"], 4),
                            round(e["gross"], 4), e["buy"], e["sell"], round(e["size"] or 0, 2), round(gewinn, 4)))
            except Exception as ex:
                _ST["error"] = str(ex)[:120]
    if not _FAST["on"]:
        try:
            _sim(pg, daten, jetzt)           # seit 2.82.91: realistische Simulation mit Bestand je Boerse
        except Exception as e:
            _SIM["error"] = str(e)[:160]
    _ST["rounds"] += 1
    _ST["last"] = int(jetzt)


def faden_starten():
    """Seit 2.82.100: laeuft nur in EINEM Dienst gleichzeitig (Postgres-Sperre). Der Dienst "spread" hat Vorrang,
    jeder andere wartet erst 3 Minuten und uebernimmt nur, wenn spread nicht laeuft."""
    if _FADEN[0]:
        return
    _FADEN[0] = True

    def warten():
        import optionen
        if (os.getenv("RAILWAY_SERVICE_NAME") or os.getenv("START_AS") or "").strip().lower() != "spread":
            time.sleep(180)
        while True:
            try:
                pg = optionen._pg()
                if pg and pg.execute("SELECT pg_try_advisory_lock(728350)").fetchone()[0]:
                    log.info("spread test: lock taken, starting here")
                    _wirklich_starten()
                    return
            except Exception as e:
                log.debug(f"spread lock: {e}")
            time.sleep(60)
    threading.Thread(target=warten, daemon=True, name="spread-lock").start()


def _wirklich_starten():
    _ST["since"] = int(time.time())
    # Seit 2.82.97: Zaehler nach einem Neustart weiterfuehren (Stand aus kv), statt bei null zu beginnen
    try:
        import storage
        alt = json.loads(storage.kv_get("optionen_spread_stand") or "null") or {}
        if alt.get("since"):
            _ST["since"] = int(alt["since"])
            _ST["rounds"] = int(alt.get("rounds") or 0)
            for grp in ("coins", "perp_coins"):
                for c, v in (alt.get(grp) or {}).items():
                    if c in _ST[grp]:
                        _ST[grp][c]["rounds"] = int(v.get("rounds") or 0)
                        _ST[grp][c]["pos"] = int(v.get("pos") or 0)
            for k in ("ok", "fail"):
                _ST[k].update(alt.get(k) or {})
    except Exception:
        pass

    def lauf():
        import optionen
        while True:
            t0 = time.time()
            try:
                pg = optionen._pg()
                if pg:
                    _schema(pg)
                _runde(pg)
                if _ST["rounds"] % 6 == 0:
                    import storage
                    kurz = dict(_ST)
                    kurz["coins"] = {c: {"rounds": v["rounds"], "pos": v["pos"],
                                         "gross_median": round(statistics.median(v["gross"]), 4) if v["gross"] else None,
                                         "gross_max": max(v["gross"]) if v["gross"] else None}
                                     for c, v in _ST["coins"].items()}
                    kurz["perp_coins"] = {c: {"rounds": v["rounds"], "pos": v["pos"],
                                              "gross_median": round(statistics.median(v["gross"]), 4) if v["gross"] else None,
                                              "gross_max": max(v["gross"]) if v["gross"] else None}
                                          for c, v in _ST["perp_coins"].items()}
                    kurz["open"] = len(_OFFEN)
                    kurz["xvenue"] = dict(_XST)
                    storage.kv_set("optionen_spread_stand", json.dumps(kurz))
            except Exception as e:
                _ST["error"] = str(e)[:120]
                log.debug(f"spread: {e}")
            time.sleep(max(1.0, TAKT - (time.time() - t0)))
    threading.Thread(target=lauf, daemon=True, name="spread-test").start()
    _faden("funding-test", FUND_TAKT, _funding_runde)          # seit 2.82.87
    _faden("xvenue-test", X_TAKT, _x_runde)                    # seit 2.82.87
    try:
        import optionen_fills
        _faden("lock-test", 300, optionen_fills.lock_runde)    # seit 2.82.90: Absicherungs-Test, liest nur
        _faden("coin-finder", FIND_TAKT, _finder_lauf)           # seit 2.82.92: alle Coins durchsuchen, liest nur
        _fast_starten()                                            # seit 2.82.93: Simulation im Sekundentakt
        _gap_starten()                                             # seit 2.82.101: echte Episoden der Finder-Coins
        _m15_starten()                                             # seit 2.82.107: Kalshi 15-Minuten-Maerkte auf Papier
    except Exception as e:
        log.warning(f"lock test: {e}")


def bericht():
    out = {"fees_pct": GEBUEHR, "note": ("Measured only, nothing is traded. Best bid and ask of each exchange every "
                                         f"{int(TAKT)} s. Compared within the same quote (USD with USD, USDT with USDT). "
                                         "Assumes money already sits on both exchanges, both sides taken at once as "
                                         "taker at the base fee. Binance and Bybit block requests from the US.")}
    try:
        import storage
        out["status"] = json.loads(storage.kv_get("optionen_spread_stand") or "null")
    except Exception:
        out["status"] = None
    try:
        import optionen
        pg = optionen._pg()
        if not pg:
            return out
        _schema(pg)
        rows = pg.execute("""SELECT coin, count(*), sum(profit_usd), avg(best_net_pct), max(best_net_pct), avg(seconds),
                                    sum(CASE WHEN size_usd > 0 THEN 1 ELSE 0 END)
                             FROM optionen_spread WHERE started > now() - interval '7 days' GROUP BY coin ORDER BY 3 DESC""").fetchall()
        out["by_coin"] = [{"coin": r[0], "n": int(r[1]), "profit_usd": round(float(r[2] or 0), 2),
                           "avg_net_pct": round(float(r[3] or 0), 4), "max_net_pct": round(float(r[4] or 0), 4),
                           "avg_seconds": round(float(r[5] or 0), 1), "with_size": int(r[6] or 0)} for r in rows]
        rows = pg.execute("""SELECT coin, buy_ex, sell_ex, count(*), sum(profit_usd), max(best_net_pct), avg(seconds)
                             FROM optionen_spread WHERE started > now() - interval '7 days'
                             GROUP BY coin, buy_ex, sell_ex ORDER BY 4 DESC LIMIT 12""").fetchall()
        out["top_pairs"] = [{"coin": r[0], "buy": r[1], "sell": r[2], "n": int(r[3]), "profit_usd": round(float(r[4] or 0), 2),
                             "max_net_pct": round(float(r[5] or 0), 4), "avg_seconds": round(float(r[6] or 0), 1)} for r in rows]
        rows = pg.execute("""SELECT started, coin, buy_ex, sell_ex, best_net_pct, seconds, size_usd, profit_usd
                             FROM optionen_spread ORDER BY started DESC LIMIT 15""").fetchall()
        out["recent"] = [{"ts": int(r[0].timestamp()), "coin": r[1], "buy": r[2], "sell": r[3], "net_pct": float(r[4]),
                          "seconds": float(r[5]), "size_usd": float(r[6] or 0), "profit_usd": float(r[7] or 0)} for r in rows]
        try:
            out["funding"] = _funding_bericht(pg)
            out["funding_note"] = ("Buy the coin, short the same amount as a perp, collect funding. Rates sampled every "
                                   f"{int(FUND_TAKT / 60)} min, assuming one payment every 8 h. Net 30 days = 30 days of "
                                   "funding minus opening and closing both legs. Negative = shorts pay.")
        except Exception as e:
            out["funding_error"] = str(e)[:160]
        try:
            import storage as _sto
            out["finder"] = json.loads(_sto.kv_get("optionen_finder") or "null")
            if out["finder"] is not None:
                out["finder"]["history"] = finder_tage(pg)
        except Exception:
            pass
        try:
            out["xvenue"] = _x_bericht(pg, (out.get("status") or {}).get("xvenue"))
        except Exception as e:
            out["xvenue_error"] = str(e)[:160]
    except Exception as e:
        out["error"] = str(e)[:160]
    return out


# ══ Funding-Test (seit 2.82.87): Coin kaufen, gleiche Menge als Perp verkaufen, Funding kassieren. NUR MESSEN. ══
FUND_TAKT = float(os.getenv("OPT_FUNDING_SECONDS", "3600"))
FUND_INTERVALL_H = 8.0     # alle vier Boersen rechnen BTC und Co. in 8-Stunden-Schritten ab (Annahme, im Bericht genannt)
_FSCHEMA = [False]


def _f_schema(pg):
    if _FSCHEMA[0]:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_funding (
        id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), ex TEXT, coin TEXT, rate NUMERIC)""")
    pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_funding_ts ON optionen_funding(ts)")
    _FSCHEMA[0] = True


def _funding_holen():
    out = {}
    for c in COINS:
        try:
            x = (_g("https://www.okx.com/api/v5/public/funding-rate", {"instId": f"{c.upper()}-USDT-SWAP"}).get("data") or [{}])[0]
            out[("okx_perp", c)] = float(x["fundingRate"])
        except Exception:
            pass
        try:
            x = (_g("https://openapi.blofin.com/api/v1/market/funding-rate", {"instId": f"{c.upper()}-USDT"}).get("data") or [{}])[0]
            out[("blofin_perp", c)] = float(x["fundingRate"])
        except Exception:
            pass
    try:
        _bitget_perp()
        _mexc_perp()
    except Exception:
        pass
    out.update(_FUND_LIVE)
    return out


def _funding_runde(pg):
    werte = _funding_holen()
    _ST["funding_last"] = {f"{k[0]}:{k[1]}": v for k, v in werte.items()}
    if pg and werte:
        _f_schema(pg)
        for (ex, c), v in werte.items():
            pg.execute("INSERT INTO optionen_funding (ex, coin, rate) VALUES (%s,%s,%s)", (ex, c, v))


def _funding_bericht(pg):
    _f_schema(pg)
    rows = pg.execute("""SELECT ex, coin, avg(rate), count(*), min(rate), max(rate) FROM optionen_funding
                         WHERE ts > now() - interval '7 days' GROUP BY ex, coin ORDER BY 3 DESC""").fetchall()
    out = []
    for ex, c, avg, n, lo, hi in rows:
        tag = float(avg) * 24 / FUND_INTERVALL_H * 100              # Prozent pro Tag
        spot = min(GEBUEHR.get(x, 0.2) for x in BOERSEN if QUOTE.get(x) == "usdt")
        kosten = 2 * (spot + GEBUEHR.get(ex, 0.06))                  # rein und raus, beide Seiten
        out.append({"ex": ex, "coin": c, "samples": int(n), "rate_avg_pct": round(float(avg) * 100, 4),
                    "per_day_pct": round(tag, 4), "apr_pct": round(tag * 365, 2),
                    "net_30d_pct": round(tag * 30 - kosten, 3), "costs_pct": round(kosten, 3),
                    "min_pct": round(float(lo) * 100, 4), "max_pct": round(float(hi) * 100, 4)})
    return out


# ══ Kalshi gegen Polymarket (seit 2.82.87): gleicher Coin, gleiche Schlusszeit. NUR MESSEN. ══
# Ja bei der tieferen Schwelle auf der einen Seite plus Nein bei der gleichen oder hoeheren Schwelle auf der anderen:
# eine der beiden Wetten gewinnt immer (zwischen den Schwellen beide). Kostet beides zusammen unter $1 nach Gebuehren,
# ist der Gewinn bei der Abrechnung fest. Restrisiko: Kalshi rechnet mit dem CF-Index in USD ab, Polymarket mit
# Binance BTC/USDT. Genau deshalb wird jede Gelegenheit nach der Abrechnung mit den echten Ergebnissen nachgerechnet.
X_TAKT = float(os.getenv("OPT_XVENUE_SECONDS", "60"))
X_ABSTAND = float(os.getenv("OPT_XVENUE_MAX_GAP_PCT", "3"))     # Schwellen hoechstens so weit auseinander
_XSCHEMA = [False]
_XST = {"rounds": 0, "pairs": 0, "last": 0, "error": None, "best": {}, "pm_markets": 0, "kalshi_markets": 0}


def _x_schema(pg):
    if _XSCHEMA[0]:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_xvenue (
        id TEXT PRIMARY KEY, coin TEXT, t_resolve TIMESTAMPTZ, k_kalshi NUMERIC, k_pm NUMERIC,
        kalshi_market TEXT, kalshi_side TEXT, pm_market TEXT, pm_side TEXT,
        cost NUMERIC, fees NUMERIC, best_net NUMERIC, size NUMERIC,
        first_seen TIMESTAMPTZ DEFAULT now(), last_seen TIMESTAMPTZ DEFAULT now(), seen INT DEFAULT 1,
        kalshi_won BOOLEAN, pm_won BOOLEAN, payoff NUMERIC, real_pnl NUMERIC, settled TIMESTAMPTZ)""")
    _XSCHEMA[0] = True


def _x_runde(pg):
    import optionen
    rate_k = float(getattr(optionen, "FEE_RATE", 0.07))
    rate_p = float(os.getenv("OPT_PM_FEE_RATE", str(rate_k)))      # vorsichtig: wie Kalshi, bis bekannt
    paare = 0
    for c in ("btc", "eth"):
        try:
            pm, ks = optionen.polymarket(c), optionen.kalshi(c)
        except Exception as e:
            _XST["error"] = str(e)[:120]
            continue
        _XST["pm_markets"], _XST["kalshi_markets"] = len(pm), len(ks)
        bester = None
        # Diagnose (seit 2.82.89): naechste Schlusszeit und Schwelle, falls nie ein Paar zusammenkommt
        try:
            dts = sorted({round(min(abs(p["T"] - k["T"]) for k in ks)) for p in pm})
            _XST.setdefault("diag", {})[c] = {"min_dt_s": dts[:3], "pm_T": sorted({int(p["T"]) for p in pm})[:4],
                                              "kalshi_T_n": len({int(k["T"]) for k in ks})}
        except Exception:
            pass
        for p in pm:
            for k in ks:
                if abs(p["T"] - k["T"]) > 90 or abs(p["K"] - k["K"]) / p["K"] * 100 > X_ABSTAND:
                    continue
                paare += 1
                # Fall A: Ja auf Kalshi (tiefere oder gleiche Schwelle), Nein auf Polymarket
                # Fall B: Ja auf Polymarket (tiefere oder gleiche Schwelle), Nein auf Kalshi
                faelle = []
                if k["K"] <= p["K"] + 0.01 and k.get("yes_ask") and p.get("no_ask"):
                    faelle.append(("A", "yes", k["yes_ask"], k.get("yes_size"), "no", p["no_ask"], 1))
                if p["K"] <= k["K"] + 0.01 and k.get("no_ask") and p.get("yes_ask"):
                    faelle.append(("B", "no", k["no_ask"], k.get("no_size"), "yes", p["yes_ask"], 0))
                for fall, ks_seite, ks_px, ks_sz, pm_seite, pm_px, tok_i in faelle:
                    if not (0 < ks_px < 1 and 0 < pm_px < 1):
                        continue
                    gebuehr = rate_k * ks_px * (1 - ks_px) + rate_p * pm_px * (1 - pm_px)
                    netto = 1 - ks_px - pm_px - gebuehr
                    if bester is None or netto > bester:
                        bester = netto
                    if netto <= 0:
                        continue
                    # Polymarket-Preis am echten Orderbuch nachpruefen (Gamma kann hinterher sein)
                    pm_sz = None
                    tok = (p.get("tokens") or [None, None])[tok_i] if len(p.get("tokens") or []) == 2 else None
                    if tok:
                        echt = optionen._clob_ask(tok)
                        if echt:
                            pm_px = echt
                            buch = optionen._PM_BUCH.get(tok) or []
                            pm_sz = min((b for b in buch), key=lambda b: b[0])[1] if buch else None
                            gebuehr = rate_k * ks_px * (1 - ks_px) + rate_p * pm_px * (1 - pm_px)
                            netto = 1 - ks_px - pm_px - gebuehr
                            if netto <= 0:
                                continue
                    menge = min([x for x in (ks_sz, pm_sz) if x] or [0])
                    if not pg:
                        continue
                    _x_schema(pg)
                    pg.execute("""INSERT INTO optionen_xvenue (id, coin, t_resolve, k_kalshi, k_pm, kalshi_market, kalshi_side,
                                      pm_market, pm_side, cost, fees, best_net, size)
                                  VALUES (%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                  ON CONFLICT (id) DO UPDATE SET last_seen=now(), seen=optionen_xvenue.seen+1,
                                      cost=CASE WHEN EXCLUDED.best_net > optionen_xvenue.best_net THEN EXCLUDED.cost ELSE optionen_xvenue.cost END,
                                      fees=CASE WHEN EXCLUDED.best_net > optionen_xvenue.best_net THEN EXCLUDED.fees ELSE optionen_xvenue.fees END,
                                      size=CASE WHEN EXCLUDED.best_net > optionen_xvenue.best_net THEN EXCLUDED.size ELSE optionen_xvenue.size END,
                                      best_net=GREATEST(EXCLUDED.best_net, optionen_xvenue.best_net)""",
                               (f"{k['market']}|{p['market']}|{fall}", c, k["T"], k["K"], p["K"], k["market"], ks_seite,
                                p["market"], pm_seite, round(ks_px + pm_px, 4), round(gebuehr, 4), round(netto, 4), menge))
        if bester is not None:
            _XST["best"][c] = round(bester, 4)
    _XST["pairs"] = paare
    _XST["rounds"] += 1
    _XST["last"] = int(time.time())
    if pg:
        _x_abrechnen(pg)


def _x_abrechnen(pg):
    """Nach der Abrechnung beider Maerkte: was haette es wirklich gebracht (je Kontrakt)?"""
    import optionen
    _x_schema(pg)
    for (id_, km, ksd, pmm, pmsd, cost, fees) in pg.execute(
            """SELECT id, kalshi_market, kalshi_side, pm_market, pm_side, cost, fees FROM optionen_xvenue
               WHERE settled IS NULL AND t_resolve < now() - interval '10 minutes' ORDER BY t_resolve LIMIT 6""").fetchall():
        rk, rp = optionen._ergebnis("kalshi", km), optionen._ergebnis("polymarket", pmm)
        if rk is None or rp is None:
            continue
        kw, pw = (rk == (ksd == "yes")), (rp == (pmsd == "yes"))
        aus = int(kw) + int(pw)
        pg.execute("""UPDATE optionen_xvenue SET kalshi_won=%s, pm_won=%s, payoff=%s, real_pnl=%s, settled=now() WHERE id=%s""",
                   (kw, pw, aus, round(aus - float(cost) - float(fees), 4), id_))


def _x_bericht(pg, status=None):
    _x_schema(pg)
    r = pg.execute("""SELECT count(*), avg(best_net), max(best_net), sum(CASE WHEN settled IS NOT NULL THEN 1 ELSE 0 END),
                             avg(real_pnl), sum(CASE WHEN payoff = 0 THEN 1 ELSE 0 END), avg(size)
                      FROM optionen_xvenue WHERE first_seen > now() - interval '7 days'""").fetchone()
    out = {"status": status or dict(_XST), "found": int(r[0] or 0), "avg_net_c": round(float(r[1] or 0) * 100, 2),
           "max_net_c": round(float(r[2] or 0) * 100, 2), "settled": int(r[3] or 0),
           "real_avg_c": round(float(r[4]) * 100, 2) if r[4] is not None else None, "both_lost": int(r[5] or 0),
           "avg_size": round(float(r[6] or 0), 1)}
    out["recent"] = [{"ts": int(x[0].timestamp()), "coin": x[1], "k_kalshi": float(x[2]), "k_pm": float(x[3]),
                      "legs": f"Kalshi {x[4].upper()} + Polymarket {x[5].upper()}", "net_c": round(float(x[6]) * 100, 2),
                      "size": float(x[7] or 0), "seen": int(x[8] or 1),
                      "real_c": round(float(x[9]) * 100, 2) if x[9] is not None else None}
                     for x in pg.execute("""SELECT first_seen, coin, k_kalshi, k_pm, kalshi_side, pm_side, best_net, size, seen, real_pnl
                                            FROM optionen_xvenue ORDER BY first_seen DESC LIMIT 15""").fetchall()]
    return out


def _faden(name, takt, fn):
    def lauf():
        import optionen
        while True:
            t0 = time.time()
            try:
                fn(optionen._pg())
            except Exception as e:
                log.debug(f"{name}: {e}")
                if name == "xvenue-test":
                    _XST["error"] = str(e)[:120]
            time.sleep(max(5.0, takt - (time.time() - t0)))
    threading.Thread(target=lauf, daemon=True, name=name).start()



# ══ Simulation (seit 2.82.91): was waere WIRKLICH uebrig geblieben? NUR PAPIER. ══
# Strenger als die Spread-Zaehlung oben:
#  1. Jede Boerse zaehlt je Runde und Coin nur einmal (keine Menge doppelt fuer mehrere Paare).
#  2. Eine Luecke wird erst gehandelt, wenn sie zwei Runden hintereinander (10 s) da ist, zu den Preisen der zweiten
#     Runde. Das bildet ab, dass wir nicht in Millisekunden kaufen.
#  3. Geld liegt fest verteilt: je Boerse USDT und der Coin. Kauf braucht USDT, Verkauf braucht Coins am jeweiligen Ort.
#  4. Wird es auf einer Seite knapp, wird umgeschichtet: das kostet die Auszahlungsgebuehr und das Geld ist
#     OPT_ARB_DELAY_MIN Minuten unterwegs (nicht nutzbar).
#  5. Handelsmenge hoechstens die Menge am besten Preis (beide Seiten) und hoechstens OPT_ARB_MAX_USD.
# Gewinn = Zuwachs an USDT minus Umschichtkosten. Die Coin-Menge insgesamt bleibt gleich (gekauft = verkauft).
SIM_COINS = [c.strip() for c in os.getenv("OPT_ARB_COINS", "sol").split(",") if c.strip()]
SIM_EX = [e.strip() for e in os.getenv("OPT_ARB_EXCHANGES", "mexc,okx,kucoin,bitget").split(",") if e.strip()]
SIM_USDT = float(os.getenv("OPT_ARB_USDT_PER_EX", "1000"))
SIM_COIN_USD = float(os.getenv("OPT_ARB_COIN_USD_PER_EX", "1000"))
SIM_MAX = float(os.getenv("OPT_ARB_MAX_USD", "500"))
SIM_DELAY = float(os.getenv("OPT_ARB_DELAY_MIN", "15")) * 60
try:
    SIM_WD = json.loads(os.getenv("OPT_ARB_WITHDRAW", "") or "{}")
except Exception:
    SIM_WD = {}
SIM_WD_DEF = {"sol": 0.01, "btc": 0.0002, "eth": 0.002, "xrp": 0.25, "doge": 5.0, "usdt": 1.0}   # Schaetzwerte je Auszahlung
_SIM = {"state": None, "prev": set(), "error": None}
_SIM_SCHEMA = [False]


def _sim_schema(pg):
    if _SIM_SCHEMA[0] or not pg:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_arbsim (
        id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), kind TEXT, coin TEXT, buy_ex TEXT, sell_ex TEXT,
        qty NUMERIC, buy_px NUMERIC, sell_px NUMERIC, buy_fee_usd NUMERIC, sell_fee_usd NUMERIC, profit_usd NUMERIC, note TEXT)""")
    pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_arbsim_ts ON optionen_arbsim(ts)")
    _SIM_SCHEMA[0] = True


def _sim_start(daten):
    st = {"since": int(time.time()), "bal": {}, "transit": [], "trades": 0, "rebal": 0, "rebal_cost": 0.0,
          "profit": 0.0, "fees": 0.0, "volume": 0.0, "days": {}}
    for ex in SIM_EX:
        st["bal"][ex] = {"usdt": SIM_USDT}
        for c in SIM_COINS:
            px = None
            for e2 in SIM_EX:
                v = (daten.get(e2) or {}).get(c)
                if v and v[0] and v[2]:
                    px = (v[0] + v[2]) / 2
                    break
            st["bal"][ex][c] = SIM_COIN_USD / px if px else 0.0
    return st


def _sim(pg, daten, jetzt):
    st = _SIM["state"]
    if st is None:
        try:
            import storage
            st = json.loads(storage.kv_get("optionen_arbsim_stand") or "null")
        except Exception:
            st = None
        if not st or st.get("cfg") != [SIM_COINS, SIM_EX, SIM_USDT, SIM_COIN_USD]:
            st = _sim_start(daten)
            st["cfg"] = [SIM_COINS, SIM_EX, SIM_USDT, SIM_COIN_USD]
        _SIM["state"] = st
    # Unterwegs befindliches Geld ankommen lassen
    for t in [t for t in st["transit"] if t["at"] <= jetzt]:
        st["bal"][t["to"]][t["asset"]] = st["bal"][t["to"]].get(t["asset"], 0) + t["amount"]
        st["transit"].remove(t)
    _sim_schema(pg)
    jetzt_luecken = set()
    erst = _SIM.setdefault("first", {})
    tag = time.strftime("%Y-%m-%d", time.gmtime(jetzt))
    for c in SIM_COINS:
        kurse = {ex: daten[ex][c] for ex in SIM_EX if ex in daten and c in daten[ex]
                 and daten[ex][c][0] and daten[ex][c][2] and daten[ex][c][1] and daten[ex][c][3]}
        kand = []
        for kauf, (_, _, ask, ask_sz) in kurse.items():
            for verk, (bid, bid_sz, _, _) in kurse.items():
                if kauf == verk:
                    continue
                netto = (bid - ask) / ask * 100 - GEBUEHR.get(kauf, 0.2) - GEBUEHR.get(verk, 0.2)
                if netto > 0:
                    jetzt_luecken.add((c, kauf, verk))
                    kand.append((netto, kauf, verk, ask, ask_sz, bid, bid_sz))
        kand.sort(reverse=True)
        benutzt = set()
        for netto, kauf, verk, ask, ask_sz, bid, bid_sz in kand:
            k_ = (c, kauf, verk)
            erst.setdefault(k_, jetzt)
            # Seit 2.82.98: die Luecke muss seit mindestens WS_CONFIRM Sekunden ununterbrochen bestehen
            if jetzt - erst[k_] < WS_CONFIRM or kauf in benutzt or verk in benutzt:
                continue
            fk, fv = GEBUEHR.get(kauf, 0.2) / 100, GEBUEHR.get(verk, 0.2) / 100
            menge = min(ask_sz, bid_sz, SIM_MAX / ask,
                        st["bal"][kauf]["usdt"] / (ask * (1 + fk)), st["bal"][verk].get(c, 0))
            if menge * ask < 10:          # unter $10 lohnt keine Order
                continue
            kosten = menge * ask * (1 + fk)
            erloes = menge * bid * (1 - fv)
            st["bal"][kauf]["usdt"] -= kosten
            st["bal"][kauf][c] = st["bal"][kauf].get(c, 0) + menge
            st["bal"][verk][c] -= menge
            st["bal"][verk]["usdt"] += erloes
            gewinn = erloes - kosten
            st["profit"] += gewinn
            st["fees"] += menge * ask * fk + menge * bid * fv
            st["volume"] += menge * ask
            st["trades"] += 1
            st["days"][tag] = round(st["days"].get(tag, 0) + gewinn, 4)
            benutzt.update((kauf, verk))
            if pg:
                pg.execute("""INSERT INTO optionen_arbsim (kind, coin, buy_ex, sell_ex, qty, buy_px, sell_px, buy_fee_usd,
                              sell_fee_usd, profit_usd) VALUES ('trade',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                           (c, kauf, verk, round(menge, 6), ask, bid, round(menge * ask * fk, 4), round(menge * bid * fv, 4),
                            round(gewinn, 4)))
        # Umschichten, wenn eine Seite unter 25 % ihres Startwerts faellt
        mid = None
        for ex in SIM_EX:
            v = kurse.get(ex)
            if v:
                mid = (v[0] + v[2]) / 2
                break
        if not mid:
            continue
        start_c = SIM_COIN_USD / mid
        for ex in SIM_EX:
            for asset, start in ((c, start_c), ("usdt", SIM_USDT)):
                if st["bal"][ex].get(asset, 0) >= 0.25 * start:
                    continue
                if any(t["to"] == ex and t["asset"] == asset for t in st["transit"]):
                    continue
                quelle = max((e for e in SIM_EX if e != ex), key=lambda e: st["bal"][e].get(asset, 0))
                # Nur echte Ueberschuesse schieben, und gleich so viel, dass das Ziel wieder voll ist (wenige grosse
                # Transfers statt vieler kleiner, jeder kostet eine feste Gebuehr)
                menge = min(st["bal"][quelle].get(asset, 0) - start, start - st["bal"][ex].get(asset, 0))
                if menge < 0.5 * start:
                    continue
                gebuehr = float(SIM_WD.get(asset, SIM_WD_DEF.get(asset, 1.0)))
                st["bal"][quelle][asset] -= menge
                st["transit"].append({"to": ex, "asset": asset, "amount": menge - gebuehr, "at": jetzt + SIM_DELAY})
                kost_usd = gebuehr * (1 if asset == "usdt" else mid)
                st["rebal"] += 1
                st["rebal_cost"] += kost_usd
                st["profit"] -= kost_usd
                st["days"][tag] = round(st["days"].get(tag, 0) - kost_usd, 4)
                if pg:
                    pg.execute("""INSERT INTO optionen_arbsim (kind, coin, buy_ex, sell_ex, qty, profit_usd, note)
                                  VALUES ('transfer',%s,%s,%s,%s,%s,%s)""",
                               (asset, quelle, ex, round(menge, 6), round(-kost_usd, 4), f"withdraw fee {gebuehr} {asset}"))
    _SIM["prev"] = jetzt_luecken
    for k_ in [k_ for k_ in erst if k_ not in jetzt_luecken]:
        erst.pop(k_, None)
    if jetzt - _SIM.get("saved", 0) >= 30:
        _SIM["saved"] = jetzt
        st["fast"] = {"on": _FAST["on"], "rounds": _FAST["rounds"], "fail": _FAST["fail"], "ms": _FAST["ms"],
                      "ws": {ex: {"connected": v.get("connected"), "msgs": v.get("msgs", 0), "errors": v.get("errors", 0),
                                  "last_error": v.get("last_error"),
                                  "age_s": round(min([time.time() - x[4] for (e2, _), x in _WS_LIVE.items() if e2 == ex] or [999]), 1)}
                             for ex, v in _WS_ST.items()}}
        try:
            import storage
            storage.kv_set("optionen_arbsim_stand", json.dumps(st))
        except Exception:
            pass


def sim_bericht(pg=None):
    import optionen
    pg = pg or optionen._pg()
    try:
        import storage
        st = json.loads(storage.kv_get("optionen_arbsim_stand") or "null")
    except Exception:
        st = None
    kapital = len(SIM_EX) * (SIM_USDT + SIM_COIN_USD * len(SIM_COINS))
    out = {"capital_usd": kapital, "coins": SIM_COINS, "exchanges": SIM_EX, "max_trade_usd": SIM_MAX,
           "delay_min": SIM_DELAY / 60, "withdraw_fees": {**SIM_WD_DEF, **SIM_WD},
           "note": ("Paper only, with live prices. Each exchange is used once per round and coin, a gap must still be there "
                    + ("1 s" if (_FAST["on"] or ((st or {}).get("fast") or {}).get("on")) else "10 s") + " later, "
                    "money sits on each exchange and has to be moved back now and then (withdraw fee, funds in transit "
                    "for the delay). Profit = USDT gained minus transfer costs, coin amounts stay the same.")}
    if st:
        std = max(1.0, (time.time() - st["since"]) / 3600)
        out.update({"since": st["since"], "hours": round(std, 1), "trades": st["trades"], "profit_usd": round(st["profit"], 2),
                    "fees_usd": round(st["fees"], 2), "volume_usd": round(st["volume"], 2), "transfers": st["rebal"],
                    "transfer_cost_usd": round(st["rebal_cost"], 2), "per_day_usd": round(st["profit"] / std * 24, 2),
                    "per_day_pct": round(st["profit"] / std * 24 / kapital * 100, 3) if kapital else None,
                    "days": st["days"], "in_transit": len(st["transit"]), "fast": st.get("fast"),
                    "balances": {ex: {k: round(v, 4) for k, v in b.items()} for ex, b in st["bal"].items()}})
    if pg:
        try:
            _sim_schema(pg)
            out["recent"] = [{"ts": int(r[0].timestamp()), "kind": r[1], "coin": r[2], "buy": r[3], "sell": r[4],
                              "qty": float(r[5] or 0), "profit_usd": float(r[6] or 0)}
                             for r in pg.execute("""SELECT ts, kind, coin, buy_ex, sell_ex, qty, profit_usd FROM optionen_arbsim
                                                    ORDER BY ts DESC LIMIT 15""").fetchall()]
        except Exception as e:
            out["error"] = str(e)[:160]
    return out


def sim_csv(pg=None, tage=365):
    """Handelsliste fuer die Steuer: jede Zeile ein Tausch, im ueblichen Format (Koinly/CoinTracking lesen es ein)."""
    import csv
    import io
    import optionen
    pg = pg or optionen._pg()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date (UTC)", "Exchange", "Type", "Sent Amount", "Sent Currency", "Received Amount", "Received Currency",
                "Fee Amount", "Fee Currency", "Net Worth Amount", "Net Worth Currency", "Description"])
    if not pg:
        return buf.getvalue()
    _sim_schema(pg)
    for ts, kind, coin, kauf, verk, qty, bpx, spx, bf, sf, note in pg.execute(
            """SELECT ts, kind, coin, buy_ex, sell_ex, qty, buy_px, sell_px, buy_fee_usd, sell_fee_usd, note FROM optionen_arbsim
               WHERE ts > now() - make_interval(days => %s) ORDER BY ts""", (tage,)).fetchall():
        d = ts.strftime("%Y-%m-%d %H:%M:%S")
        q = float(qty or 0)
        if kind == "trade":
            c = coin.upper()
            w.writerow([d, kauf, "trade", round(q * float(bpx), 6), "USDT", round(q, 8), c, round(float(bf or 0), 6), "USDT",
                        round(q * float(bpx), 2), "USD", f"buy {c} on {kauf} (spread, paper)"])
            w.writerow([d, verk, "trade", round(q, 8), c, round(q * float(spx), 6), "USDT", round(float(sf or 0), 6), "USDT",
                        round(q * float(spx), 2), "USD", f"sell {c} on {verk} (spread, paper)"])
        else:
            w.writerow([d, kauf, "transfer", round(q, 8), coin.upper(), "", "", "", "", "", "USD",
                        f"move to {verk}, {note or ''} (paper)"])
    return buf.getvalue()


# ══ Coin-Finder (seit 2.82.92): welche Coins haben dauerhaft echte Luecken? NUR MESSEN. ══
# Jede Minute ALLE USDT-Paare von MEXC, OKX, Bitget und KuCoin (je eine Anfrage pro Boerse, mit Menge und 24-h-Volumen).
# Je Coin: beste Luecke nach beiden Gebuehren, Menge am besten Preis, wie oft sie da ist.
# Echtheits-Filter: Stablecoins und Hebel-Token raus, beide Seiten mit mindestens OPT_FINDER_MIN_VOL Volumen am Tag,
# und Luecken ueber 3 % gelten als verdaechtig (oft anderer Token mit gleichem Kuerzel oder gesperrte Auszahlung).
FIND_TAKT = float(os.getenv("OPT_FINDER_SECONDS", "60"))
FIND_MIN_VOL = float(os.getenv("OPT_FINDER_MIN_VOL", "200000"))
FIND_CAP = float(os.getenv("OPT_FINDER_TRADE_USD", "500"))
_STABLE = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDE", "USDD", "PYUSD", "EUR", "EURC", "USD1", "BUSD", "USDP", "XAUT", "PAXG"}
_FIND = {"rounds": 0, "since": 0, "coins": {}, "fail": {}, "last": 0}
_FIND_EX = {}     # seit 2.82.101: coin -> Boersen, auf denen er in der letzten Runde handelbar war (fuer die Episoden)


def _fz(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except Exception:
        return None


def _f_mexc():
    out = {}
    for x in _g("https://api.mexc.com/api/v3/ticker/24hr") or []:
        s = x.get("symbol") or ""
        if s.endswith("USDT"):
            out[s[:-4]] = (_fz(x.get("bidPrice")), _fz(x.get("bidQty")), _fz(x.get("askPrice")), _fz(x.get("askQty")), _fz(x.get("quoteVolume")))
    return out


def _f_okx():
    out = {}
    for x in _g("https://www.okx.com/api/v5/market/tickers", {"instType": "SPOT"}).get("data") or []:
        s = x.get("instId") or ""
        if s.endswith("-USDT"):
            out[s[:-5]] = (_fz(x.get("bidPx")), _fz(x.get("bidSz")), _fz(x.get("askPx")), _fz(x.get("askSz")), _fz(x.get("volCcy24h")))
    return out


def _f_bitget():
    out = {}
    for x in _g("https://api.bitget.com/api/v2/spot/market/tickers").get("data") or []:
        s = x.get("symbol") or ""
        if s.endswith("USDT"):
            out[s[:-4]] = (_fz(x.get("bidPr")), _fz(x.get("bidSz")), _fz(x.get("askPr")), _fz(x.get("askSz")),
                           _fz(x.get("usdtVolume") or x.get("quoteVolume")))
    return out


def _f_kucoin():
    out = {}
    for x in (_g("https://api.kucoin.com/api/v1/market/allTickers").get("data") or {}).get("ticker") or []:
        s = x.get("symbol") or ""
        if s.endswith("-USDT"):
            out[s[:-5]] = (_fz(x.get("buy")), _fz(x.get("bestBidSize")), _fz(x.get("sell")), _fz(x.get("bestAskSize")), _fz(x.get("volValue")))
    return out


def _f_htx():
    out = {}
    for x in _g("https://api.huobi.pro/market/tickers").get("data") or []:
        s_ = (x.get("symbol") or "").upper()
        if s_.endswith("USDT"):
            out[s_[:-4]] = (_fz(x.get("bid")), _fz(x.get("bidSize")), _fz(x.get("ask")), _fz(x.get("askSize")), _fz(x.get("vol")))
    return out


def _f_bitmart():
    out = {}
    for x in (_g("https://api-cloud.bitmart.com/spot/quotation/v3/tickers").get("data") or []):
        # Reihenfolge laut Doku: symbol, last, v_24h, qv_24h, open_24h, high_24h, low_24h, fluctuation, bid_px, bid_sz, ask_px, ask_sz, ts
        if isinstance(x, list) and len(x) >= 12 and str(x[0]).endswith("_USDT"):
            out[str(x[0])[:-5]] = (_fz(x[8]), _fz(x[9]), _fz(x[10]), _fz(x[11]), _fz(x[3]))
    return out


def _f_bingx():
    out = {}
    j = _g("https://open-api.bingx.com/openApi/spot/v1/ticker/24hr", {"timestamp": int(time.time() * 1000)})
    for x in j.get("data") or []:
        s_ = x.get("symbol") or ""
        if s_.endswith("-USDT"):
            out[s_[:-5]] = (_fz(x.get("bidPrice")), _fz(x.get("bidQty")), _fz(x.get("askPrice")), _fz(x.get("askQty")),
                            _fz(x.get("quoteVolume")))
    return out


FINDER = {"mexc": _f_mexc, "okx": _f_okx, "bitget": _f_bitget, "kucoin": _f_kucoin,
          "htx": _f_htx, "bitmart": _f_bitmart, "bingx": _f_bingx}


def _finder_runde():
    from concurrent.futures import ThreadPoolExecutor
    daten = {}
    with ThreadPoolExecutor(max_workers=len(FINDER)) as ex:
        laeufe = {n: ex.submit(fn) for n, fn in FINDER.items()}
        for n, f in laeufe.items():
            try:
                daten[n] = f.result(timeout=20)
            except Exception:
                _FIND["fail"][n] = _FIND["fail"].get(n, 0) + 1
    alle = set()
    for d in daten.values():
        alle.update(d.keys())
    for coin in alle:
        if coin in _STABLE or coin.endswith(("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")):
            continue
        kurse = {n: d[coin] for n, d in daten.items() if coin in d and all(d[coin][:4]) and (d[coin][4] or 0) >= FIND_MIN_VOL}
        if len(kurse) < 2:
            continue
        _FIND_EX[coin] = sorted(kurse)
        bester = None
        for kauf, (_, _, ask, asz, _) in kurse.items():
            for verk, (bid, bsz, _, _, _) in kurse.items():
                if kauf == verk:
                    continue
                brutto = (bid - ask) / ask * 100
                netto = brutto - GEBUEHR.get(kauf, 0.2) - GEBUEHR.get(verk, 0.2)
                usd = min(asz * ask, bsz * bid, FIND_CAP)
                if bester is None or netto > bester[0]:
                    bester = (netto, kauf, verk, usd, brutto)
        if not bester:
            continue
        c = _FIND["coins"].setdefault(coin, {"seen": 0, "pos": 0, "sus": 0, "profit": 0.0, "nets": [], "pairs": {}, "vol": 0})
        c["seen"] += 1
        c["vol"] = round(min(v[4] for v in kurse.values()))
        netto, kauf, verk, usd, brutto = bester
        if netto > 3:
            c["sus"] += 1               # zu schoen, um echt zu sein: nicht als Gewinn zaehlen
        elif netto > 0 and usd >= 50:
            c["pos"] += 1
            c["profit"] += usd * netto / 100
            c["nets"] = (c["nets"] + [round(netto, 3)])[-200:]
            k = f"{kauf}>{verk}"
            c["pairs"][k] = c["pairs"].get(k, 0) + 1
    _FIND["rounds"] += 1
    _FIND["last"] = int(time.time())


def finder_liste(n=30):
    std = max(FIND_TAKT, (_FIND["last"] - _FIND["since"])) / 3600 if _FIND["since"] else None
    zeilen = []
    for coin, c in _FIND["coins"].items():
        if not c["pos"] or c["seen"] < 5:
            continue
        zeilen.append({"coin": coin, "seen": c["seen"], "rounds_with_gap": c["pos"], "share_pct": round(c["pos"] / c["seen"] * 100, 1),
                       "median_net_pct": round(statistics.median(c["nets"]), 3) if c["nets"] else None,
                       "profit_usd": round(c["profit"], 2),
                       "per_day_usd": round(c["profit"] / std * 24, 2) if std else None,
                       "suspicious": c["sus"], "min_volume_24h": c["vol"],
                       "main_pair": max(c["pairs"], key=c["pairs"].get) if c["pairs"] else None})
    zeilen.sort(key=lambda z: z["profit_usd"], reverse=True)
    return {"rounds": _FIND["rounds"], "since": _FIND["since"], "hours": round(std, 1) if std else None,
            "exchanges": list(FINDER), "fail": _FIND["fail"], "min_volume": FIND_MIN_VOL, "trade_cap_usd": FIND_CAP,
            "coins": zeilen[:n],
            "note": ("Every minute all USDT pairs on MEXC, OKX, Bitget, KuCoin, HTX, BitMart and BingX. Counted only after both taker fees, with at "
                     f"least $50 at the best price and at least ${int(FIND_MIN_VOL):,} daily volume on both sides. Profit = size at the "
                     f"best price (max ${int(FIND_CAP)}) times the gap, once per minute. Gaps over 3% are counted as suspicious, "
                     "not as profit: often a different token with the same ticker or withdrawals closed. Check deposits and "
                     "withdrawals on both exchanges before trusting a coin.")}


def _finder_lauf(pg=None):
    if not _FIND["since"]:
        # Seit 2.82.96: nach einem Neustart (Deploy) mit dem alten Stand weitermachen statt bei null
        try:
            import storage
            alt = json.loads(storage.kv_get("optionen_finder_raw") or "null")
            if alt and time.time() - alt.get("last", 0) < 6 * 3600:
                _FIND.update({k: alt[k] for k in ("rounds", "since", "coins", "fail", "last") if k in alt})
        except Exception:
            pass
        if not _FIND["since"]:
            _FIND["since"] = int(time.time())
    _finder_runde()
    if _FIND["rounds"] % 5 == 0:
        try:
            import storage
            storage.kv_set("optionen_finder", json.dumps(finder_liste(40)))
            storage.kv_set("optionen_finder_raw", json.dumps(_FIND))
        except Exception:
            pass
        _finder_speichern(pg)


_FDAY = {"day": None, "base": {}}


def _finder_speichern(pg):
    """Seit 2.82.97: je Tag und Coin eine Zeile in der Datenbank (bleibt fuer immer, ueberlebt jeden Deploy)."""
    import optionen
    pg = pg or optionen._pg()
    if not pg:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_finder_day (
        day DATE, coin TEXT, seen INT, gaps INT, suspicious INT, profit_usd NUMERIC, median_net_pct NUMERIC,
        main_pair TEXT, min_volume NUMERIC, updated TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (day, coin))""")
    tag = time.strftime("%Y-%m-%d", time.gmtime())
    if _FDAY["day"] != tag:
        # Tageswechsel (oder Neustart): ab hier zaehlt der neue Tag als Differenz zum aktuellen Stand
        _FDAY["day"] = tag
        _FDAY["base"] = {c: {"seen": v["seen"], "pos": v["pos"], "sus": v["sus"], "profit": v["profit"]}
                         for c, v in _FIND["coins"].items()}
        try:
            for c, seen, gaps, sus, prof in pg.execute(
                    "SELECT coin, seen, gaps, suspicious, profit_usd FROM optionen_finder_day WHERE day=%s", (tag,)).fetchall():
                b = _FDAY["base"].setdefault(c, {"seen": 0, "pos": 0, "sus": 0, "profit": 0.0})
                b["seen"] -= int(seen or 0)
                b["pos"] -= int(gaps or 0)
                b["sus"] -= int(sus or 0)
                b["profit"] -= float(prof or 0)
        except Exception:
            pass
    for c, v in _FIND["coins"].items():
        b = _FDAY["base"].get(c, {"seen": 0, "pos": 0, "sus": 0, "profit": 0.0})
        seen, pos = v["seen"] - b["seen"], v["pos"] - b["pos"]
        if seen <= 0 or pos <= 0:
            continue
        med = round(statistics.median(v["nets"]), 4) if v["nets"] else None
        haupt = max(v["pairs"], key=v["pairs"].get) if v["pairs"] else None
        pg.execute("""INSERT INTO optionen_finder_day (day, coin, seen, gaps, suspicious, profit_usd, median_net_pct, main_pair, min_volume)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (day, coin) DO UPDATE SET seen=EXCLUDED.seen, gaps=EXCLUDED.gaps, suspicious=EXCLUDED.suspicious,
                        profit_usd=EXCLUDED.profit_usd, median_net_pct=EXCLUDED.median_net_pct, main_pair=EXCLUDED.main_pair,
                        min_volume=EXCLUDED.min_volume, updated=now()""",
                   (tag, c, seen, pos, v["sus"] - b["sus"], round(v["profit"] - b["profit"], 4), med, haupt, v["vol"]))


def finder_tage(pg=None, tage=14, n=25):
    """Gespeicherte Tageswerte: welche Coins ueber mehrere Tage echte Luecken hatten."""
    import optionen
    pg = pg or optionen._pg()
    if not pg:
        return []
    try:
        rows = pg.execute("""SELECT coin, count(*), sum(seen), sum(gaps), sum(profit_usd), avg(median_net_pct), max(day),
                                    mode() WITHIN GROUP (ORDER BY main_pair)
                             FROM optionen_finder_day WHERE day > current_date - %s
                             GROUP BY coin ORDER BY 5 DESC LIMIT %s""", (tage, n)).fetchall()
    except Exception:
        return []
    return [{"coin": r[0], "days": int(r[1]), "minutes": int(r[2] or 0), "gap_minutes": int(r[3] or 0),
             "share_pct": round(int(r[3] or 0) / max(1, int(r[2] or 0)) * 100, 1), "profit_usd": round(float(r[4] or 0), 2),
             "median_net_pct": round(float(r[5]), 3) if r[5] is not None else None, "last_day": str(r[6]), "main_pair": r[7]}
            for r in rows]



# ══ Sekundentakt fuer die Simulation (seit 2.82.93) ══
# Jede Sekunde das beste Angebot je Boerse fuer die Coins der Simulation (eine kleine Anfrage je Boerse und Coin,
# alle gleichzeitig). Eine Luecke zaehlt damit, wenn sie eine Sekunde spaeter noch da ist, statt zehn. Das entspricht
# der Zeit, die eine echte Order braucht. Echte WebSocket-Stroeme kommen mit dem Handelsteil.
FAST_TAKT = float(os.getenv("OPT_FAST_SECONDS", "1"))
_FAST = {"on": False, "rounds": 0, "fail": {}, "ms": None}


def _fx_mexc(c):
    x = _g("https://api.mexc.com/api/v3/ticker/bookTicker", {"symbol": f"{c.upper()}USDT"})
    return (_f(x.get("bidPrice")), _f(x.get("bidQty")), _f(x.get("askPrice")), _f(x.get("askQty")))


def _fx_okx(c):
    x = (_g("https://www.okx.com/api/v5/market/ticker", {"instId": f"{c.upper()}-USDT"}).get("data") or [{}])[0]
    return (_f(x.get("bidPx")), _f(x.get("bidSz")), _f(x.get("askPx")), _f(x.get("askSz")))


def _fx_kucoin(c):
    x = _g("https://api.kucoin.com/api/v1/market/orderbook/level1", {"symbol": f"{c.upper()}-USDT"}).get("data") or {}
    return (_f(x.get("bestBid")), _f(x.get("bestBidSize")), _f(x.get("bestAsk")), _f(x.get("bestAskSize")))


def _fx_bitget(c):
    x = (_g("https://api.bitget.com/api/v2/spot/market/tickers", {"symbol": f"{c.upper()}USDT"}).get("data") or [{}])[0]
    return (_f(x.get("bidPr")), _f(x.get("bidSz")), _f(x.get("askPr")), _f(x.get("askSz")))


FAST = {"mexc": _fx_mexc, "okx": _fx_okx, "kucoin": _fx_kucoin, "bitget": _fx_bitget}


def _fast_starten():
    if os.getenv("OPT_FAST_ON", "1").strip().lower() in ("0", "false", "off", "no"):
        return
    _FAST["on"] = True

    def lauf():
        import optionen
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=16)
        while True:
            t0 = time.time()
            try:
                daten = {}
                frisch = set()
                for (ex, c), v in list(_WS_LIVE.items()):
                    if ex in SIM_EX and c in SIM_COINS and time.time() - v[4] < 2 and all(v[:4]):
                        daten.setdefault(ex, {})[c] = v[:4]
                        frisch.add((ex, c))
                auftraege = {(ex, c): pool.submit(fn, c) for ex, fn in FAST.items() if ex in SIM_EX for c in SIM_COINS
                             if (ex, c) not in frisch and time.time() - _FAST.get("rest_at", {}).get(ex, 0) >= 1.0}
                for (ex, c) in auftraege:
                    _FAST.setdefault("rest_at", {})[ex] = time.time()
                for (ex, c), f in auftraege.items():
                    try:
                        v = f.result(timeout=3)
                        if all(v):
                            daten.setdefault(ex, {})[c] = v
                    except Exception:
                        _FAST["fail"][ex] = _FAST["fail"].get(ex, 0) + 1
                _FAST["ms"] = int((time.time() - t0) * 1000)
                _sim(optionen._pg(), daten, time.time())
                _FAST["rounds"] += 1
            except Exception as e:
                _SIM["error"] = str(e)[:160]
            # mit WebSocket alle 0,25 s rechnen, sonst im Sekundentakt
            takt = 0.25 if any(time.time() - v[4] < 2 for v in _WS_LIVE.values()) else FAST_TAKT
            time.sleep(max(0.1, takt - (time.time() - t0)))
    threading.Thread(target=lauf, daemon=True, name="fast-sim").start()
    _ws_starten()



# ══ WebSocket-Stroeme (seit 2.82.98): jede Preisaenderung in Millisekunden statt Abfragen ══
# OKX (bbo-tbt), Bitget (books1), KuCoin (level1, mit Token) senden JSON. MEXC sendet seit 2025 Protobuf
# (binaer): ein kleiner Leser sucht darin die Nachricht mit den vier Feldern Bid, Bid-Menge, Ask, Ask-Menge.
# Faellt ein Strom aus, nimmt die Simulation fuer diese Boerse automatisch wieder die Abfrage im Sekundentakt.
WS_CONFIRM = float(os.getenv("OPT_ARB_CONFIRM_S", "1.0"))
_WS_LIVE = {}            # (boerse, coin) -> (bid, bid_menge, ask, ask_menge, zeit)
_WS_ST = {}              # boerse -> {"connected": bool, "msgs": n, "errors": n, "last_error": str}


def _ws_setzen(ex, c, bid, bsz, ask, asz):
    v = (_f(bid), _f(bsz), _f(ask), _f(asz))
    if all(v):
        _WS_LIVE[(ex, c)] = v + (time.time(),)
        st = _WS_ST.setdefault(ex, {"msgs": 0, "errors": 0})
        st["msgs"] = st.get("msgs", 0) + 1


def _pb_felder(b):
    """Minimaler Protobuf-Leser: liefert [(feldnummer, wert)], wert = bytes bei Laengenfeldern, sonst int."""
    out, i, n = [], 0, len(b)
    while i < n:
        key, shift = 0, 0
        while True:
            x = b[i]
            i += 1
            key |= (x & 0x7F) << shift
            shift += 7
            if x < 0x80:
                break
        nr, typ = key >> 3, key & 7
        if typ == 0:
            v, shift = 0, 0
            while True:
                x = b[i]
                i += 1
                v |= (x & 0x7F) << shift
                shift += 7
                if x < 0x80:
                    break
            out.append((nr, v))
        elif typ == 2:
            ln, shift = 0, 0
            while True:
                x = b[i]
                i += 1
                ln |= (x & 0x7F) << shift
                shift += 7
                if x < 0x80:
                    break
            out.append((nr, b[i:i + ln]))
            i += ln
        elif typ == 1:
            i += 8
        elif typ == 5:
            i += 4
        else:
            raise ValueError("protobuf type")
    return out


def _mexc_pb(b):
    """Sucht im Wrapper das Symbol (Feld 3) und die Unternachricht mit vier Zahlen-Texten (Bid, Menge, Ask, Menge)."""
    felder = _pb_felder(b)
    symbol = None
    for nr, v in felder:
        if nr == 3 and isinstance(v, bytes):
            try:
                t = v.decode()
                if t.endswith("USDT"):
                    symbol = t
            except Exception:
                pass
    for nr, v in felder:
        if not isinstance(v, bytes) or len(v) < 8:
            continue
        try:
            sub = dict((k, w) for k, w in _pb_felder(v) if isinstance(w, bytes))
            werte = [float(sub[k].decode()) for k in (1, 2, 3, 4)]
            return symbol, werte
        except Exception:
            continue
    return symbol, None


async def _ws_okx():
    import websockets
    async with websockets.connect("wss://ws.okx.com:8443/ws/v5/public", ping_interval=20) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": f"{c.upper()}-USDT"} for c in SIM_COINS]}))
        _WS_ST.setdefault("okx", {})["connected"] = True
        async for m in ws:
            j = json.loads(m)
            inst = ((j.get("arg") or {}).get("instId") or "").split("-")[0].lower()
            for d in j.get("data") or []:
                b, a = (d.get("bids") or [[None, None]])[0], (d.get("asks") or [[None, None]])[0]
                _ws_setzen("okx", inst, b[0], b[1], a[0], a[1])


async def _ws_bitget():
    import asyncio
    import websockets
    async with websockets.connect("wss://ws.bitget.com/v2/ws/public", ping_interval=None) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": [{"instType": "SPOT", "channel": "books1", "instId": f"{c.upper()}USDT"} for c in SIM_COINS]}))
        _WS_ST.setdefault("bitget", {})["connected"] = True

        async def ping():
            while True:
                await asyncio.sleep(25)
                await ws.send("ping")
        t = asyncio.ensure_future(ping())
        try:
            async for m in ws:
                if m == "pong":
                    continue
                j = json.loads(m)
                inst = ((j.get("arg") or {}).get("instId") or "").replace("USDT", "").lower()
                for d in j.get("data") or []:
                    b, a = (d.get("bids") or [[None, None]])[0], (d.get("asks") or [[None, None]])[0]
                    _ws_setzen("bitget", inst, b[0], b[1], a[0], a[1])
        finally:
            t.cancel()


async def _ws_kucoin():
    import asyncio
    import websockets
    tok = requests.post("https://api.kucoin.com/api/v1/bullet-public", timeout=10).json()["data"]
    srv = tok["instanceServers"][0]
    url = f"{srv['endpoint']}?token={tok['token']}&connectId={int(time.time() * 1000)}"
    async with websockets.connect(url, ping_interval=None) as ws:
        await ws.send(json.dumps({"id": str(int(time.time() * 1000)), "type": "subscribe",
                                  "topic": "/spotMarket/level1:" + ",".join(f"{c.upper()}-USDT" for c in SIM_COINS),
                                  "privateChannel": False, "response": True}))
        _WS_ST.setdefault("kucoin", {})["connected"] = True

        async def ping():
            while True:
                await asyncio.sleep(max(5, srv.get("pingInterval", 18000) / 1000 * 0.8))
                await ws.send(json.dumps({"id": str(int(time.time() * 1000)), "type": "ping"}))
        t = asyncio.ensure_future(ping())
        try:
            async for m in ws:
                j = json.loads(m)
                if j.get("type") != "message":
                    continue
                inst = (j.get("topic") or "").split(":")[-1].split("-")[0].lower()
                d = j.get("data") or {}
                b, a = d.get("bids") or [None, None], d.get("asks") or [None, None]
                _ws_setzen("kucoin", inst, b[0], b[1], a[0], a[1])
        finally:
            t.cancel()


async def _ws_mexc():
    import asyncio
    import websockets
    async with websockets.connect("wss://wbs-api.mexc.com/ws", ping_interval=None) as ws:
        await ws.send(json.dumps({"method": "SUBSCRIPTION",
                                  "params": [f"spot@public.aggre.bookTicker.v3.api.pb@100ms@{c.upper()}USDT" for c in SIM_COINS]}))
        _WS_ST.setdefault("mexc", {})["connected"] = True

        async def ping():
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"method": "PING"}))
        t = asyncio.ensure_future(ping())
        try:
            async for m in ws:
                if isinstance(m, str):
                    continue          # Bestaetigungen und PONG kommen als Text
                sym, w = _mexc_pb(m)
                if sym and w:
                    _ws_setzen("mexc", sym[:-4].lower(), w[0], w[1], w[2], w[3])
        finally:
            t.cancel()


WS = {"okx": _ws_okx, "bitget": _ws_bitget, "kucoin": _ws_kucoin, "mexc": _ws_mexc}


def _ws_starten():
    if os.getenv("OPT_WS_ON", "1").strip().lower() in ("0", "false", "off", "no"):
        return
    try:
        import websockets  # noqa: F401
    except Exception:
        _WS_ST["_"] = {"last_error": "websockets package missing"}
        return

    def lauf(ex, fn):
        import asyncio
        while True:
            try:
                asyncio.run(fn())
            except Exception as e:
                st = _WS_ST.setdefault(ex, {})
                st["errors"] = st.get("errors", 0) + 1
                st["last_error"] = str(e)[:120]
            _WS_ST.setdefault(ex, {})["connected"] = False
            time.sleep(5)
    for ex, fn in WS.items():
        if ex in SIM_EX:
            threading.Thread(target=lauf, args=(ex, fn), daemon=True, name=f"ws-{ex}").start()



# ══ Episoden der Finder-Coins (seit 2.82.101): WIE OFT kann man wirklich handeln? NUR PAPIER. ══
# Der Finder schaut einmal pro Minute und zaehlt Minuten mit Luecke. Das sagt nicht, wie oft man handeln kann:
# eine Luecke, die eine Stunde offen steht, ist EINE Gelegenheit. Hier deshalb:
#  1. Die besten Finder-Coins (OPT_GAP_COINS, Standard 10) alle OPT_GAP_SECONDS (2 s) einzeln abfragen.
#  2. Jede Luecke ist eine Episode: geht auf, bleibt offen, geht zu. Gespeichert mit Dauer, Netto-Vorsprung, Menge.
#  3. Gehandelt wird je Episode GENAU EINMAL, und nur, wenn sie bei der naechsten Abfrage noch da ist (bestaetigt),
#     zu den Preisen dieser zweiten Abfrage, hoechstens die Menge am besten Preis und OPT_GAP_MAX_USD.
#  4. Bestand wie in echt: je Coin und Boerse OPT_GAP_USDT USDT und Coins im selben Wert. Fehlt Bestand, ist die
#     Episode verpasst (wird gezaehlt). Umschichten kostet die echte Auszahlungsgebuehr und 15 Minuten.
#  5. Ein- und Auszahlung wird bei den Boersen geprueft, die das oeffentlich zeigen (Bitget, KuCoin, HTX). Ist der
#     Weg zu, wird nicht umgeschichtet, der Bestand bleibt dann eben leer. OKX, MEXC und BingX zeigen es nur mit Key:
#     "unknown", Gebuehr geschaetzt (OPT_GAP_FEE_EST_USD, $1).
GAP_TAKT = float(os.getenv("OPT_GAP_SECONDS", "3"))
GAP_N = int(os.getenv("OPT_GAP_COINS", "6"))
# Seit 2.82.105: die grossen Coins werden IMMER beobachtet (tiefe Buecher, Auszahlung praktisch immer offen, wenig
# Kursrisiko im Bestand). Dazu die besten OPT_GAP_COINS kleinen Coins aus dem Finder zum Vergleich.
GAP_MAJORS = [c.strip().upper() for c in os.getenv(
    "OPT_GAP_MAJORS", "BTC,ETH,SOL,XRP,BNB,DOGE,ADA,TRX,LINK,AVAX,LTC,TON,SUI,DOT,AAVE,UNI,NEAR,APT,ARB,OP").split(",") if c.strip()]
GAP_USDT = float(os.getenv("OPT_GAP_USDT", "5000"))   # seit 2.82.110: 5000 statt 1000, ein Transfer kostet fest ~$1, also selten und gross schieben
GAP_MAX = float(os.getenv("OPT_GAP_MAX_USD", "500"))
GAP_MIN = float(os.getenv("OPT_GAP_MIN_USD", "10"))
GAP_FEE_EST = float(os.getenv("OPT_GAP_FEE_EST_USD", "1"))
GAP_USDT_FEE = float(os.getenv("OPT_GAP_USDT_FEE", "1"))
# Seit 2.82.110: nur Luecken ab dieser Groesse handeln, und nur nachfuellen, wenn der Coin seit dem letzten Transfer
# mindestens das X-fache der Gebuehr verdient hat. Vorher: +$215 aus Trades, -$277 fuer Transfers.
GAP_MIN_NET = float(os.getenv("OPT_GAP_MIN_NET_PCT", "0.10"))
GAP_REFILL_X = float(os.getenv("OPT_GAP_REFILL_X", "2"))
GAP_CFG = [GAP_USDT, GAP_MAX, GAP_MIN_NET, GAP_REFILL_X]
_GAP = {"on": False, "state": None, "open": {}, "watch": [], "watch_at": 0, "polls": 0, "fail": {}, "error": None,
        "route": {}, "saved": 0}
_GAP_SCHEMA = [False]


def _gx_mexc(c):
    x = _g("https://api.mexc.com/api/v3/ticker/bookTicker", {"symbol": f"{c}USDT"})
    return (_f(x.get("bidPrice")), _f(x.get("bidQty")), _f(x.get("askPrice")), _f(x.get("askQty")))


def _gx_okx(c):
    x = (_g("https://www.okx.com/api/v5/market/ticker", {"instId": f"{c}-USDT"}).get("data") or [{}])[0]
    return (_f(x.get("bidPx")), _f(x.get("bidSz")), _f(x.get("askPx")), _f(x.get("askSz")))


def _gx_kucoin(c):
    x = _g("https://api.kucoin.com/api/v1/market/orderbook/level1", {"symbol": f"{c}-USDT"}).get("data") or {}
    return (_f(x.get("bestBid")), _f(x.get("bestBidSize")), _f(x.get("bestAsk")), _f(x.get("bestAskSize")))


def _gx_bitget(c):
    x = (_g("https://api.bitget.com/api/v2/spot/market/tickers", {"symbol": f"{c}USDT"}).get("data") or [{}])[0]
    return (_f(x.get("bidPr")), _f(x.get("bidSz")), _f(x.get("askPr")), _f(x.get("askSz")))


def _gx_htx(c):
    t = _g("https://api.huobi.pro/market/detail/merged", {"symbol": f"{c.lower()}usdt"}).get("tick") or {}
    b, a = t.get("bid") or [None, None], t.get("ask") or [None, None]
    return (_f(b[0]), _f(b[1]), _f(a[0]), _f(a[1]))


def _gx_bingx(c):
    d = _g("https://open-api.bingx.com/openApi/spot/v1/ticker/bookTicker",
           {"symbol": f"{c}-USDT", "timestamp": int(time.time() * 1000)}).get("data")
    x = (d[0] if isinstance(d, list) and d else d) or {}
    return (_f(x.get("bidPrice")), _f(x.get("bidVolume")), _f(x.get("askPrice")), _f(x.get("askVolume")))


def _gx_bitmart(c):
    x = _g("https://api-cloud.bitmart.com/spot/quotation/v3/ticker", {"symbol": f"{c}_USDT"}).get("data") or {}
    return (_f(x.get("bid_px")), _f(x.get("bid_sz")), _f(x.get("ask_px")), _f(x.get("ask_sz")))


GAPX = {"mexc": _gx_mexc, "okx": _gx_okx, "kucoin": _gx_kucoin, "bitget": _gx_bitget, "htx": _gx_htx,
        "bingx": _gx_bingx, "bitmart": _gx_bitmart}


# ── Ein- und Auszahlung: nur oeffentliche Daten. -> Liste von Ketten {chain, contract, wd, dep, fee (in Coins)} oder None
def _w_bitget(c):
    d = (_g("https://api.bitget.com/api/v2/spot/public/coins", {"coin": c}).get("data") or [{}])[0]
    return [{"chain": str(k.get("chain") or "").upper(), "contract": (k.get("contractAddress") or "").lower() or None,
             "wd": str(k.get("withdrawable")).lower() == "true", "dep": str(k.get("rechargeable")).lower() == "true",
             "fee": _fz(k.get("withdrawFee"))} for k in d.get("chains") or []]


def _w_kucoin(c):
    d = _g(f"https://api.kucoin.com/api/v3/currencies/{c}").get("data") or {}
    return [{"chain": str(k.get("chainName") or k.get("chainId") or "").upper(),
             "contract": (k.get("contractAddress") or "").lower() or None,
             "wd": bool(k.get("isWithdrawEnabled")), "dep": bool(k.get("isDepositEnabled")),
             "fee": _fz(k.get("withdrawalMinFee"))} for k in d.get("chains") or []]


def _w_htx(c):
    d = (_g("https://api.huobi.pro/v2/reference/currencies", {"currency": c.lower()}).get("data") or [{}])[0]
    return [{"chain": str(k.get("displayName") or k.get("baseChain") or k.get("chain") or "").upper(), "contract": None,
             "wd": k.get("withdrawStatus") == "allowed", "dep": k.get("depositStatus") == "allowed",
             "fee": _fz(k.get("transactFeeWithdraw"))} for k in d.get("chains") or []]


WALLET = {"bitget": _w_bitget, "kucoin": _w_kucoin, "htx": _w_htx}
_WCACHE = {}     # (boerse, coin) -> (zeit, ketten oder None)


def _ketten(ex, c):
    z = _WCACHE.get((ex, c))
    if z and time.time() - z[0] < 1800:
        return z[1]
    k = None
    if ex in WALLET:
        try:
            k = WALLET[ex](c) or None
        except Exception:
            k = None
    _WCACHE[(ex, c)] = (time.time(), k)
    return k


def _norm(ch):
    ch = (ch or "").upper().replace("-", "").replace(" ", "").replace("_", "")
    for a, b in (("ERC20", "ETH"), ("ETHEREUM", "ETH"), ("BEP20", "BSC"), ("BNBSMARTCHAIN", "BSC"), ("TRC20", "TRX"),
                 ("TRON", "TRX"), ("SOLANA", "SOL"), ("SPL", "SOL"), ("ARBITRUMONE", "ARB"), ("ARBITRUM", "ARB"),
                 ("OPTIMISM", "OP"), ("POLYGON", "MATIC"), ("BASE", "BASE")):
        if ch.startswith(a):
            return b
    return ch


def _weg(c, von, nach):
    """Kann der Coin von 'von' nach 'nach'? -> (status, gebuehr_in_coins oder None). status: open, closed, unknown."""
    a, b = _ketten(von, c), _ketten(nach, c)
    raus = [k for k in a or [] if k["wd"]]
    rein = [k for k in b or [] if k["dep"]]
    if a is not None and not raus:
        return "closed", None
    if b is not None and not rein:
        return "closed", None
    if a is None or b is None:
        fees = [k["fee"] for k in raus if k["fee"] is not None]
        return "unknown", (min(fees) if a is not None and fees else None)
    passt = []
    for k in raus:
        for m in rein:
            if k["contract"] and m["contract"]:
                if k["contract"] == m["contract"]:
                    passt.append(k)
            elif _norm(k["chain"]) == _norm(m["chain"]):
                passt.append(k)
    if not passt:
        return "closed", None
    fees = [k["fee"] for k in passt if k["fee"] is not None]
    return "open", (min(fees) if fees else None)


def _gap_schema(pg):
    if _GAP_SCHEMA[0] or not pg:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_gap_ep (
        id BIGSERIAL PRIMARY KEY, coin TEXT, buy_ex TEXT, sell_ex TEXT, started TIMESTAMPTZ, ended TIMESTAMPTZ,
        seconds NUMERIC, polls INT, max_net_pct NUMERIC, avg_net_pct NUMERIC, depth_usd NUMERIC, confirmed BOOLEAN,
        action TEXT, traded_usd NUMERIC, profit_usd NUMERIC, route TEXT)""")
    pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_gap_ep ON optionen_gap_ep(started)")
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_gap_tx (
        id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), coin TEXT, asset TEXT, from_ex TEXT, to_ex TEXT,
        amount NUMERIC, fee_usd NUMERIC, fee_known BOOLEAN)""")
    pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_gap_tx ON optionen_gap_tx(ts)")
    _GAP_SCHEMA[0] = True


def _gap_liste():
    """Die Coins, die beobachtet werden: Finder-Coins mit den meisten Minuten mit Luecke, ohne Verdaechtige."""
    kand = []
    for coin, c in _FIND["coins"].items():
        exs = [e for e in _FIND_EX.get(coin, []) if e in GAPX]
        if c["pos"] < 3 or len(exs) < 2 or c["sus"] > 0.05 * max(1, c["seen"]):
            continue
        kand.append((c["profit"], coin))
    kand.sort(reverse=True)
    neu = list(GAP_MAJORS) + [coin for _, coin in kand if coin not in GAP_MAJORS][:GAP_N]
    # Coins mit offener Episode bleiben, bis sie zu ist
    for (coin, _, _) in _GAP["open"]:
        if coin not in neu:
            neu.append(coin)
    return neu


def _gap_bestand(st, coin, kurse):
    b = st["bal"].get(coin)
    mids = [(v[0] + v[2]) / 2 for v in kurse.values()]
    if not mids:
        return None
    mid = statistics.median(mids)
    if b is None:
        b = st["bal"][coin] = {}
        st["start_px"][coin] = mid
    for ex in kurse:
        if ex not in b:
            b[ex] = {"usdt": GAP_USDT, "coin": GAP_USDT / mid}
    return mid


def _gap_ep_schliessen(pg, k, e, jetzt):
    coin, kauf, verk = k
    if pg:
        nets = e["nets"]
        pg.execute("""INSERT INTO optionen_gap_ep (coin, buy_ex, sell_ex, started, ended, seconds, polls, max_net_pct, avg_net_pct,
                      depth_usd, confirmed, action, traded_usd, profit_usd, route)
                      VALUES (%s,%s,%s,to_timestamp(%s),to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (coin, kauf, verk, e["start"], jetzt, round(jetzt - e["start"], 1), len(nets), round(max(nets), 4),
                    round(sum(nets) / len(nets), 4), round(statistics.median(e["depth"]), 2), len(nets) >= 2,
                    e.get("action") or ("not_confirmed" if len(nets) < 2 else ("too_small" if e.get("small") else "busy")), round(e.get("traded", 0.0), 2), round(e.get("profit", 0.0), 4),
                    e.get("route")))


def _gap_runde(pg, pool):
    jetzt = time.time()
    st = _GAP["state"]
    if st is None:
        try:
            import storage
            st = json.loads(storage.kv_get("optionen_gap_stand") or "null")
        except Exception:
            st = None
        if not st or st.get("cfg") != GAP_CFG:
            st = {"since": int(jetzt), "bal": {}, "start_px": {}, "transit": [], "cfg": GAP_CFG, "earn": {}}
        _GAP["state"] = st
    if jetzt - _GAP["watch_at"] >= 600 or not _GAP["watch"]:
        _GAP["watch"] = _gap_liste()
        _GAP["watch_at"] = jetzt
    if not _GAP["watch"]:
        return
    for t in [t for t in st["transit"] if t["at"] <= jetzt]:
        b = st["bal"].get(t["coin"], {}).get(t["to"])
        if b is not None:
            b[t["asset"]] = b.get(t["asset"], 0) + t["amount"]
        st["transit"].remove(t)
    # Seit 2.82.105: je Boerse EINE Anfrage fuer alle Paare (statt einer je Coin und Boerse). Das haelt die Zahl der
    # Anfragen klein, auch mit 26 Coins, und trifft keine Limits mehr.
    laeufe = {ex: pool.submit(fn) for ex, fn in FINDER.items() if ex in GAPX and ex != "bitmart"}
    alle = {}
    for ex, f in laeufe.items():
        try:
            alle[ex] = f.result(timeout=6)
        except Exception:
            _GAP["fail"][ex] = _GAP["fail"].get(ex, 0) + 1
    daten = {}
    for c in _GAP["watch"]:
        for ex, d in alle.items():
            v = d.get(c)
            if v and all(v[:4]) and (v[4] or 0) >= FIND_MIN_VOL:
                daten.setdefault(c, {})[ex] = v[:4]
    _GAP["polls"] += 1
    _gap_schema(pg)
    gesehen = set()
    for c, kurse in daten.items():
        if len(kurse) < 2:
            continue
        mid = _gap_bestand(st, c, kurse)
        if not mid:
            continue
        kand = []
        for kauf, (_, _, ask, asz) in kurse.items():
            for verk, (bid, bsz, _, _) in kurse.items():
                if kauf == verk:
                    continue
                netto = (bid - ask) / ask * 100 - GEBUEHR.get(kauf, 0.2) - GEBUEHR.get(verk, 0.2)
                tiefe = min(asz * ask, bsz * bid)
                if netto > 0 and tiefe >= GAP_MIN and netto <= 3:
                    kand.append((netto, kauf, verk, ask, asz, bid, bsz, tiefe))
        kand.sort(reverse=True)
        benutzt = set()
        for netto, kauf, verk, ask, asz, bid, bsz, tiefe in kand:
            k = (c, kauf, verk)
            gesehen.add(k)
            e = _GAP["open"].get(k)
            if e is None:
                _GAP["open"][k] = {"start": jetzt, "nets": [round(netto, 4)], "depth": [tiefe]}
                continue
            e["nets"].append(round(netto, 4))
            e["depth"].append(tiefe)
            if e.get("action"):
                continue                     # in dieser Episode schon gehandelt oder verpasst: genau einmal
            if kauf in benutzt or verk in benutzt:
                continue
            if netto < GAP_MIN_NET:
                e["small"] = True            # zu klein: nicht handeln, aber offen lassen, falls sie noch waechst
                continue
            bk, bv = st["bal"][c][kauf], st["bal"][c][verk]
            fk, fv = GEBUEHR.get(kauf, 0.2) / 100, GEBUEHR.get(verk, 0.2) / 100
            menge = min(asz, bsz, GAP_MAX / ask, bk["usdt"] / (ask * (1 + fk)), bv["coin"])
            route = _GAP["route"].get((c, kauf, verk))
            if route is None or jetzt - route[2] > 1800:
                s_, fee = _weg(c, kauf, verk)
                route = _GAP["route"][(c, kauf, verk)] = (s_, fee, jetzt)
            e["route"] = route[0]
            if menge * ask < GAP_MIN:
                unterwegs = any(t["coin"] == c and t["to"] in (kauf, verk) for t in st["transit"])
                e["action"] = "in_transit" if unterwegs else "no_balance"
                continue
            kosten = menge * ask * (1 + fk)
            erloes = menge * bid * (1 - fv)
            bk["usdt"] -= kosten
            bk["coin"] += menge
            bv["coin"] -= menge
            bv["usdt"] += erloes
            e["action"] = "traded"
            e["traded"] = menge * ask
            e["profit"] = erloes - kosten
            st.setdefault("earn", {})[c] = st.setdefault("earn", {}).get(c, 0.0) + (erloes - kosten)
            benutzt.update((kauf, verk))
        # Umschichten je Coin: Seite unter 25 % -> aus dem groessten Ueberschuss auffuellen, wenn der Weg nicht zu ist
        start_c = GAP_USDT / st["start_px"][c]
        for ex in kurse:
            for asset, start in (("coin", start_c), ("usdt", GAP_USDT)):
                if st["bal"][c][ex][asset] >= 0.25 * start:
                    continue
                if any(t["coin"] == c and t["to"] == ex and t["asset"] == asset for t in st["transit"]):
                    continue
                quelle = max((x for x in st["bal"][c] if x != ex), key=lambda x: st["bal"][c][x][asset], default=None)
                if not quelle:
                    continue
                menge = min(st["bal"][c][quelle][asset] - start, start - st["bal"][c][ex][asset])
                if menge < 0.5 * start:
                    continue
                if asset == "coin":
                    s_, fee = _weg(c, quelle, ex)
                    if s_ == "closed":
                        continue
                    fee_known = fee is not None
                    fee_c = fee if fee_known else GAP_FEE_EST / mid
                    fee_usd = fee_c * mid
                else:
                    fee_known, fee_c, fee_usd = False, GAP_USDT_FEE, GAP_USDT_FEE
                verdient = st.setdefault("earn", {}).get(c, 0.0)
                if verdient < GAP_REFILL_X * fee_usd:
                    continue                 # der Coin hat die Gebuehr noch nicht eingespielt: Seite bleibt leer
                st["earn"][c] = verdient - GAP_REFILL_X * fee_usd
                st["bal"][c][quelle][asset] -= menge
                st["transit"].append({"coin": c, "to": ex, "asset": asset, "amount": menge - fee_c, "at": jetzt + SIM_DELAY})
                if pg:
                    pg.execute("""INSERT INTO optionen_gap_tx (coin, asset, from_ex, to_ex, amount, fee_usd, fee_known)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                               (c, asset, quelle, ex, round(menge, 6), round(fee_usd, 4), fee_known))
    for k in [k for k in _GAP["open"] if k not in gesehen]:
        e = _GAP["open"].pop(k)
        try:
            _gap_ep_schliessen(pg, k, e, jetzt)
        except Exception as ex_:
            _GAP["error"] = str(ex_)[:160]
    if jetzt - _GAP["saved"] >= 30:
        _GAP["saved"] = jetzt
        st["watch"] = _GAP["watch"]
        st["polls"] = _GAP["polls"]
        st["fail"] = _GAP["fail"]
        st["open_now"] = len(_GAP["open"])
        # offene Episoden mit in den Bericht (eine Luecke, die stundenlang offen steht, hat noch keine Zeile in der Tabelle)
        st["open"] = [{"coin": c, "pair": f"{a}>{b}", "seconds": round(jetzt - e["start"]), "polls": len(e["nets"]),
                       "net_pct": e["nets"][-1], "action": e.get("action"), "traded": round(e.get("traded", 0.0), 2),
                       "profit": round(e.get("profit", 0.0), 4)} for (c, a, b), e in _GAP["open"].items()]
        st["routes"] = {f"{c}|{a}|{b}": r[0] for (c, a, b), r in _GAP["route"].items()}
        st["wallet_data"] = {ex: f"{sum(1 for (e2, _), v in _WCACHE.items() if e2 == ex and v[1])}/"
                                 f"{sum(1 for (e2, _) in _WCACHE if e2 == ex)}" for ex in WALLET}
        try:
            import storage
            storage.kv_set("optionen_gap_stand", json.dumps(st))
        except Exception:
            pass


def _gap_starten():
    if os.getenv("OPT_GAP_ON", "1").strip().lower() in ("0", "false", "off", "no"):
        return
    _GAP["on"] = True

    def lauf():
        import optionen
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=24)
        time.sleep(90)            # erst den Finder eine Runde laufen lassen
        while True:
            t0 = time.time()
            try:
                _gap_runde(optionen._pg(), pool)
            except Exception as e:
                _GAP["error"] = str(e)[:160]
                log.debug(f"gap episodes: {e}")
            time.sleep(max(0.5, GAP_TAKT - (time.time() - t0)))
    threading.Thread(target=lauf, daemon=True, name="gap-episodes").start()


def gap_bericht(pg=None, tage=7):
    """Je Coin: echte Gelegenheiten pro Tag, davon mit Bestand gehandelt, netto nach Umschichten."""
    import optionen
    pg = pg or optionen._pg()
    try:
        import storage
        st = json.loads(storage.kv_get("optionen_gap_stand") or "null") or {}
    except Exception:
        st = {}
    out = {"seconds": GAP_TAKT, "coins_watched": st.get("watch") or [], "polls": st.get("polls"), "fail": st.get("fail"),
           "open_now": st.get("open_now"), "since": st.get("since"), "wallet_data": st.get("wallet_data"), "usdt_per_side": GAP_USDT, "max_trade_usd": GAP_MAX,
           "note": (f"Paper only. The big coins plus the top small coins of the finder, checked every {GAP_TAKT:g} s on each exchange. Every gap is one "
                    "episode from open to close. We trade it at most ONCE, only if it is still there at the next check, at those "
                    f"prices, max the size at the best price and ${GAP_MAX:g}. Each coin has ${GAP_USDT:g} USDT plus the same in "
                    "coins on every exchange; without stock the episode counts as missed. Moving stock back costs the real "
                    "withdraw fee (Bitget, KuCoin, HTX publish it) or an estimate of $1 (OKX, MEXC, BingX need a key), and "
                    "15 min in transit. A route marked closed is never used. Net = trade profit minus transfer costs. "
                    f"Since 2.82.110: only gaps of {GAP_MIN_NET:g} % or more after fees are traded, and a side is only refilled once the coin "
                    f"has earned {GAP_REFILL_X:g} times the transfer fee since its last transfer. Numbers count from the start of these rules.")}
    if not pg:
        return out
    try:
        _gap_schema(pg)
        std = max(1.0, (time.time() - (st.get("since") or time.time())) / 3600)
        out["hours"] = round(std, 1)
        seit = int(st.get("since") or 0)
        out["rules"] = {"min_net_pct": GAP_MIN_NET, "refill_x": GAP_REFILL_X}
        rows = pg.execute("""SELECT coin, count(*), count(*) FILTER (WHERE confirmed),
                                    count(*) FILTER (WHERE action='traded'),
                                    count(*) FILTER (WHERE action IN ('no_balance','in_transit')),
                                    percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) FILTER (WHERE confirmed),
                                    percentile_cont(0.5) WITHIN GROUP (ORDER BY avg_net_pct) FILTER (WHERE confirmed),
                                    percentile_cont(0.5) WITHIN GROUP (ORDER BY depth_usd) FILTER (WHERE confirmed),
                                    coalesce(sum(traded_usd),0), coalesce(sum(profit_usd),0),
                                    mode() WITHIN GROUP (ORDER BY buy_ex || '>' || sell_ex),
                                    min(started), count(*) FILTER (WHERE action='too_small')
                             FROM optionen_gap_ep WHERE started > now() - make_interval(days => %s) AND started >= to_timestamp(%s)
                             GROUP BY coin""", (tage, seit)).fetchall()
        tx = {r[0]: (int(r[1]), float(r[2] or 0), int(r[3])) for r in pg.execute(
            """SELECT coin, count(*), sum(fee_usd), count(*) FILTER (WHERE NOT fee_known) FROM optionen_gap_tx
               WHERE ts > now() - make_interval(days => %s) AND ts >= to_timestamp(%s) GROUP BY coin""", (tage, seit)).fetchall()}
        routes = st.get("routes") or {}
        offen = {}
        for o in st.get("open") or []:
            x = offen.setdefault(o["coin"], {"n": 0, "conf": 0, "traded": 0, "vol": 0.0, "profit": 0.0, "longest": 0})
            x["n"] += 1
            x["conf"] += 1 if o["polls"] >= 2 else 0
            x["traded"] += 1 if o["action"] == "traded" else 0
            x["vol"] += o["traded"]
            x["profit"] += o["profit"]
            x["longest"] = max(x["longest"], o["seconds"])
        out["open"] = sorted(st.get("open") or [], key=lambda o: -o["seconds"])[:20]
        rows = [list(r) for r in rows]
        da = {r[0] for r in rows}
        for coin in offen:
            if coin not in da:
                rows.append([coin, 0, 0, 0, 0, None, None, None, 0, 0, None, None, 0])
        zeilen = []
        for r in rows:
            o = offen.get(r[0])
            if o:
                r[1] = int(r[1]) + o["n"]
                r[2] = int(r[2]) + o["conf"]
                r[3] = int(r[3]) + o["traded"]
                r[8] = float(r[8]) + o["vol"]
                r[9] = float(r[9]) + o["profit"]
            coin = r[0]
            h = max(1.0, (time.time() - r[11].timestamp()) / 3600) if r[11] else std
            n_tx, kost, geschaetzt = tx.get(coin, (0, 0.0, 0))
            haupt = r[10] or next((x["pair"] for x in st.get("open") or [] if x["coin"] == coin), "")
            a, _, b = haupt.partition(">")
            netto = float(r[9]) - kost
            zeilen.append({"coin": coin, "hours": round(h, 1), "episodes": int(r[1]), "confirmed": int(r[2]),
                           "confirmed_per_day": round(int(r[2]) / h * 24, 1), "traded": int(r[3]),
                           "traded_per_day": round(int(r[3]) / h * 24, 1), "missed_no_stock": int(r[4]), "too_small": int(r[12] or 0),
                           "median_seconds": round(float(r[5]), 1) if r[5] is not None else None,
                           "median_net_pct": round(float(r[6]), 3) if r[6] is not None else None,
                           "median_depth_usd": round(float(r[7]), 0) if r[7] is not None else None,
                           "volume_usd": round(float(r[8]), 0), "trade_profit_usd": round(float(r[9]), 2),
                           "transfers": n_tx, "transfer_cost_usd": round(kost, 2), "fees_estimated": geschaetzt,
                           "net_usd": round(netto, 2), "net_per_day_usd": round(netto / h * 24, 2),
                           "capital_usd": round(2 * GAP_USDT * len((st.get("bal") or {}).get(coin) or {}), 0),
                           "open_now": (o or {}).get("n", 0), "open_longest_s": (o or {}).get("longest", 0),
                           "major": coin in GAP_MAJORS,
                           "main_pair": haupt, "route": routes.get(f"{coin}|{a}|{b}", "unknown")})
        zeilen.sort(key=lambda z: z["net_usd"], reverse=True)
        out["coins"] = zeilen
        out["total"] = {"traded": sum(z["traded"] for z in zeilen), "net_usd": round(sum(z["net_usd"] for z in zeilen), 2),
                        "transfer_cost_usd": round(sum(z["transfer_cost_usd"] for z in zeilen), 2),
                        "trade_profit_usd": round(sum(z["trade_profit_usd"] for z in zeilen), 2)}
        def _summe(zz):
            return {"coins": len(zz), "traded": sum(z["traded"] for z in zz), "net_usd": round(sum(z["net_usd"] for z in zz), 2),
                    "trade_profit_usd": round(sum(z["trade_profit_usd"] for z in zz), 2),
                    "transfer_cost_usd": round(sum(z["transfer_cost_usd"] for z in zz), 2),
                    "confirmed": sum(z["confirmed"] for z in zz)}
        out["majors"] = _summe([z for z in zeilen if z["major"]])
        out["small_caps"] = _summe([z for z in zeilen if not z["major"]])
        out["majors_list"] = GAP_MAJORS
        out["days"] = [{"day": str(r[0]), "episodes": int(r[1]), "traded": int(r[2]), "trade_profit_usd": round(float(r[3] or 0), 2)}
                       for r in pg.execute("""SELECT started::date, count(*) FILTER (WHERE confirmed), count(*) FILTER (WHERE action='traded'),
                                                     sum(profit_usd) FROM optionen_gap_ep
                                              WHERE started > now() - make_interval(days => 14) GROUP BY 1 ORDER BY 1 DESC""").fetchall()]
    except Exception as e:
        out["error"] = str(e)[:160]
    return out



# ══ Kurze Krypto-Maerkte (seit 2.82.107): NUR PAPIER. Ist dort nach Gebuehren etwas zu holen? ══
#   kalshi_15m  Kalshi, 15 Minuten, BTC und ETH (kuerzer gibt es bei Kalshi nicht)
#   poly_5m     Polymarket, 5 Minuten "Up or Down", BTC und ETH
# Alle OPT_15M_SECONDS (15 s) die laufenden Maerkte lesen und die echten Angebotspreise mitschreiben. Mehrere einfache
# Regeln kaufen parallel auf Papier, je Markt hoechstens einmal je Regel, zum echten Angebot plus Gebuehr der Boerse.
# Nach dem Schluss wird das Ergebnis von der Boerse geholt. Die Regeln sind an die Laufzeit gebunden (Anteil der Restzeit),
# damit dieselbe Regel in 5 und 15 Minuten dasselbe bedeutet.
M15_TAKT = float(os.getenv("OPT_15M_SECONDS", "15"))
M15_SERIEN = {x.split(":")[0].strip().lower(): x.split(":")[1].strip().upper() for x in os.getenv(
    "OPT_15M_SERIES", "btc:KXBTC15M,eth:KXETH15M").split(",") if ":" in x}
M5_COINS = [c.strip().lower() for c in os.getenv("OPT_5M_COINS", "").split(",") if c.strip()]
M15_KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
M5_GAMMA = "https://gamma-api.polymarket.com"
M5_CLOB = "https://clob.polymarket.com"
_M15 = {"on": False, "rounds": 0, "fail": {}, "error": None, "hist": {}, "seen": {}, "bought": 0, "last": 0, "pm": {}}
_M15_SCHEMA = [False]
M15_REGELN = {
    "always_yes": "Baseline: always YES (Up) with two thirds of the time left. Shows what the fee alone costs.",
    "favorite_early": "Follow the market: buy the side priced 60c or more, two thirds of the time left.",
    "favorite_mid": "Follow the market: buy the side priced 60c or more, one third of the time left.",
    "favorite_late": "Follow the market: buy the side priced 60c or more, one fifth of the time left.",
    "momentum": "Buy the side whose price just rose 5c or more (within about an eighth of the market's length).",
    "fade": "Buy the side whose price just fell 10c or more (within about an eighth of the market's length).",
    "spot_lead_5": "Kalshi only: spot is 0.05 % or more beyond the target, one third left, that side still costs 85c or less.",
    "spot_lead_10": "Kalshi only: spot is 0.10 % or more beyond the target, one third left, that side still costs 85c or less.",
}
M15_ARTEN = {"kalshi_15m": "Kalshi, 15 minutes", "poly_5m": "Polymarket, 5 minutes"}


def _m15_gebuehr(art, p):
    if art == "poly_5m":
        return p * 0.25 * (p * (1 - p)) ** 2          # Polymarket Krypto: hoechstens 1,56 % vom Einsatz, bei 50c
    return 0.07 * p * (1 - p)                          # Kalshi


def _m15_schema(pg):
    if _M15_SCHEMA[0] or not pg:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_15m (
        id TEXT PRIMARY KEY, ticker TEXT, coin TEXT, rule TEXT, side TEXT, price NUMERIC, fee NUMERIC,
        bought TIMESTAMPTZ DEFAULT now(), min_left NUMERIC, spot NUMERIC, target NUMERIC, closes TIMESTAMPTZ,
        won BOOLEAN, settled TIMESTAMPTZ)""")
    pg.execute("ALTER TABLE optionen_15m ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'kalshi_15m'")
    pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_15m_open ON optionen_15m(closes) WHERE settled IS NULL")
    _M15_SCHEMA[0] = True


def _m15_zeit(s):
    from datetime import datetime
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def _m15_lesen(serie):
    j = _g(M15_KALSHI + "/markets", {"series_ticker": serie, "status": "open", "limit": 50})
    out, jetzt = [], time.time()
    for m in j.get("markets", []):
        try:
            zu, auf = _m15_zeit(m.get("close_time")), _m15_zeit(m.get("open_time"))
        except Exception:
            continue
        ya, na = _f(m.get("yes_ask_dollars")), _f(m.get("no_ask_dollars"))
        if not (auf <= jetzt < zu) or not ya or not na or zu - auf > 3600:
            continue
        out.append({"ticker": m.get("ticker"), "zu": zu, "dauer": zu - auf, "ya": ya, "na": na,
                    "ziel": _f(m.get("floor_strike")), "art": m.get("strike_type")})
    return out


def _m5_lesen(coin):
    """Der laufende 5-Minuten-Markt bei Polymarket. Die Adresse haengt am Start des Fensters (volle 5 Minuten, UTC)."""
    jetzt = time.time()
    start = int(jetzt // 300) * 300
    slug = f"{coin}-updown-5m-{start}"
    m = _M15["pm"].get(slug)
    if m is None:
        e = _g(f"{M5_GAMMA}/events/slug/{slug}")
        mm = (e.get("markets") or [{}])[0]
        toks = json.loads(mm.get("clobTokenIds") or "[]")
        namen = [str(x).lower() for x in json.loads(mm.get("outcomes") or "[]")]
        if len(toks) != 2 or len(namen) != 2:
            return []
        hoch = 0 if namen[0] in ("up", "yes") else 1
        m = _M15["pm"][slug] = {"up": toks[hoch], "down": toks[1 - hoch], "zu": start + 300}
        for alt in [k for k, v in _M15["pm"].items() if v["zu"] < jetzt - 900]:
            _M15["pm"].pop(alt, None)

    def ask(tok):
        b = _g(M5_CLOB + "/book", {"token_id": tok})
        a = [float(x["price"]) for x in b.get("asks", [])]
        return min(a) if a else None
    ya, na = ask(m["up"]), ask(m["down"])
    if not ya or not na:
        return []
    return [{"ticker": slug, "zu": m["zu"], "dauer": 300.0, "ya": ya, "na": na, "ziel": None, "art": None}]


def _m5_ergebnis(slug):
    e = _g(f"{M5_GAMMA}/events/slug/{slug}")
    mm = (e.get("markets") or [{}])[0]
    if not mm.get("closed"):
        return None
    pr = [float(x) for x in json.loads(mm.get("outcomePrices") or "[]")]
    namen = [str(x).lower() for x in json.loads(mm.get("outcomes") or "[]")]
    if len(pr) != 2 or set(pr) != {0.0, 1.0}:
        return None
    sieger = namen[pr.index(1.0)]
    return "yes" if sieger in ("up", "yes") else "no"


def _m15_kauf(pg, m, coin, kind, regel, seite, rest, spot):
    preis = m["ya"] if seite == "yes" else m["na"]
    if not preis or preis <= 0.01 or preis >= 0.99:
        return
    pg.execute("""INSERT INTO optionen_15m (id, ticker, coin, kind, rule, side, price, fee, min_left, spot, target, closes)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT DO NOTHING""",
               (f"{m['ticker']}|{regel}", m["ticker"], coin, kind, regel, seite, round(preis, 4),
                round(_m15_gebuehr(kind, preis), 5), round(rest / 60, 2), spot, m["ziel"], m["zu"]))
    _M15["bought"] += 1


def _m15_runde(pg):
    jetzt = time.time()
    _m15_schema(pg)
    quellen = [("kalshi_15m", c, (lambda s_=s_: _m15_lesen(s_))) for c, s_ in M15_SERIEN.items()]
    quellen += [("poly_5m", c, (lambda c_=c: _m5_lesen(c_))) for c in M5_COINS]
    spots = {}
    for kind, coin, lesen in quellen:
        try:
            maerkte = lesen()
        except Exception as e:
            _M15["fail"][kind] = _M15["fail"].get(kind, 0) + 1
            _M15["error"] = f"{kind}: {str(e)[:120]}"
            continue
        if coin not in spots:
            try:
                b, _, a, _ = _gx_okx(coin.upper())
                spots[coin] = (b + a) / 2 if b and a else None
            except Exception:
                spots[coin] = None
        spot = spots[coin]
        for m in maerkte:
            _M15["seen"][kind] = _M15["seen"].get(kind, 0) + 1
            h = _M15["hist"].setdefault(m["ticker"], {"zu": m["zu"], "p": [], "done": set()})
            h["p"].append((jetzt, m["ya"], m["na"]))
            rest, dauer = m["zu"] - jetzt, m["dauer"]
            teil = rest / dauer
            blick = dauer * 2 / 15                                       # 15 min: 2 min, 5 min: 40 s
            alt = [x for x in h["p"] if blick * 0.8 <= jetzt - x[0] <= blick * 1.25]

            def einmal(regel, seite):
                if regel not in h["done"]:
                    h["done"].add(regel)
                    _m15_kauf(pg, m, coin, kind, regel, seite, rest, spot)
            fav = "yes" if m["ya"] >= 0.60 else ("no" if m["na"] >= 0.60 else None)
            if teil <= 2 / 3:
                einmal("always_yes", "yes")
                if fav:
                    einmal("favorite_early", fav)
            if teil <= 1 / 3 and fav:
                einmal("favorite_mid", fav)
            if teil <= 1 / 5 and fav:
                einmal("favorite_late", fav)
            if 0.2 <= teil <= 0.8 and alt:
                dy, dn = m["ya"] - alt[0][1], m["na"] - alt[0][2]
                if dy >= 0.05:
                    einmal("momentum", "yes")
                elif dn >= 0.05:
                    einmal("momentum", "no")
                if dy <= -0.10:
                    einmal("fade", "yes")
                elif dn <= -0.10:
                    einmal("fade", "no")
            if teil <= 1 / 3 and spot and m["ziel"] and str(m["art"] or "").startswith("greater"):
                ab = (spot - m["ziel"]) / m["ziel"] * 100
                for regel, schwelle in (("spot_lead_5", 0.05), ("spot_lead_10", 0.10)):
                    if ab >= schwelle and m["ya"] <= 0.85:
                        einmal(regel, "yes")
                    elif ab <= -schwelle and m["na"] <= 0.85:
                        einmal(regel, "no")
    for t in [t for t, h in _M15["hist"].items() if h["zu"] < jetzt - 120]:
        _M15["hist"].pop(t, None)
    # abrechnen: Ergebnis von der Boerse, sobald der Markt zu ist
    if jetzt - _M15["last"] >= 45:
        _M15["last"] = jetzt
        offen = pg.execute("""SELECT DISTINCT ticker, kind FROM optionen_15m WHERE settled IS NULL
                              AND closes < now() - interval '45 seconds' AND closes > now() - interval '6 hours'
                              LIMIT 16""").fetchall()
        for tk, kind in offen:
            try:
                if kind == "poly_5m":
                    r = _m5_ergebnis(tk)
                else:
                    r = str((_g(f"{M15_KALSHI}/markets/{tk}").get("market") or {}).get("result") or "").lower()
            except Exception:
                continue
            if r in ("yes", "no"):
                pg.execute("UPDATE optionen_15m SET won = (side = %s), settled = now() WHERE ticker=%s AND settled IS NULL", (r, tk))
    _M15["rounds"] += 1
    if _M15["rounds"] % 20 == 0:
        try:
            import storage
            storage.kv_set("optionen_15m_stand", json.dumps({k: _M15[k] for k in ("rounds", "fail", "error", "seen", "bought")}
                                                            | {"ts": int(jetzt), "open_markets": len(_M15["hist"])}))
        except Exception:
            pass


def _m15_starten():
    if os.getenv("OPT_15M_ON", "1").strip().lower() in ("0", "false", "off", "no"):
        return
    _M15["on"] = True

    def lauf():
        import optionen
        time.sleep(40)
        while True:
            t0 = time.time()
            try:
                _m15_runde(optionen._pg())
            except Exception as e:
                _M15["error"] = str(e)[:140]
                log.debug(f"15m test: {e}")
            time.sleep(max(2.0, M15_TAKT - (time.time() - t0)))
    threading.Thread(target=lauf, daemon=True, name="kalshi-15m").start()


def m15_bericht(pg=None):
    import optionen
    pg = pg or optionen._pg()
    try:
        import storage
        st = json.loads(storage.kv_get("optionen_15m_stand") or "null") or {}
    except Exception:
        st = {}
    out = {"status": st, "seconds": M15_TAKT, "kinds": M15_ARTEN, "rules_text": M15_REGELN,
           "note": ("Paper only, 1 contract per rule and market, bought at the real ask plus the fee of the exchange (Kalshi "
                    "7 % of p times 1 minus p, Polymarket at most 1.56 % of the stake). The two halves (older, newer) must "
                    "both be positive before a rule means anything. Break even needs a hit rate above the average price paid "
                    "plus the fee.")}
    if not pg:
        return out
    try:
        _m15_schema(pg)
        rows = pg.execute("""SELECT rule, coin, side, price, fee, won, bought, kind FROM optionen_15m
                             WHERE settled IS NOT NULL ORDER BY bought""").fetchall()

        def zus(rs):
            n = len(rs)
            if not n:
                return None
            kost = sum(float(r[3]) + float(r[4]) for r in rs)
            pnl = sum((1.0 if r[5] else 0.0) - float(r[3]) - float(r[4]) for r in rs)
            return {"n": n, "hit": round(sum(1 for r in rs if r[5]) / n, 3), "avg_price": round(sum(float(r[3]) for r in rs) / n, 3),
                    "pnl": round(pnl, 2), "roi": round(pnl / kost, 4) if kost else None}
        out["by_kind"] = {}
        for kind in M15_ARTEN:
            rk = [r for r in rows if (r[7] or "kalshi_15m") == kind]
            regeln = {}
            for regel in M15_REGELN:
                rs = [r for r in rk if r[0] == regel]
                if not rs:
                    continue
                h = len(rs) // 2
                regeln[regel] = {"all": zus(rs), "older": zus(rs[:h]), "newer": zus(rs[h:]),
                                 "by_coin": {c: zus([r for r in rs if r[1] == c]) for c in sorted({r[1] for r in rs})}}
            q = pg.execute("""SELECT count(DISTINCT ticker), count(*) FILTER (WHERE settled IS NULL) FROM optionen_15m
                              WHERE COALESCE(kind, 'kalshi_15m') = %s""", (kind,)).fetchone()
            out["by_kind"][kind] = {"name": M15_ARTEN[kind], "markets": int(q[0]), "open": int(q[1]), "settled": len(rk),
                                    "first": str(rk[0][6])[:16] if rk else None, "rules": regeln}
    except Exception as e:
        out["error"] = str(e)[:160]
    return out


def dienst():
    """Seit 2.82.99: eigener Railway-Dienst "spread". Ein Deploy der anderen Dienste beruehrt ihn nicht mehr
    (Watch Paths in Railway), und er kann in einer anderen Region laufen (EU, fuer Binance, Bybit und den Handel)."""
    log.info("spread service: start")
    faden_starten()
    while True:
        time.sleep(3600)
