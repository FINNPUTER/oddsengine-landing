"""
optionen_test.py: zwei Tests fuer Above X, bevor irgendetwas live geht (seit 2.82.58).

1. Trend. Jeder abgerechnete Kalshi-Pick bekommt die Bewegung von BTC/ETH in der Stunde davor (Coinbase, 5-Minuten-
   Kerzen). Dann getrennt: Picks MIT dem Trend (YES bei steigendem Kurs, NO bei fallendem), GEGEN den Trend und
   seitwaerts (unter 0,15 %). Je Gruppe Trefferquote und Ergebnis, einmal zum Pick-Preis plus 1 Cent und, wo die
   Fills-Pruefung (optionen_fill) echte Trades hat, zum Preis, den Kaeufer in den 60 s danach wirklich zahlten.
   Nur die Calls mit Preis 20 bis 80 Cent (wie Auto mode) und alle Picks getrennt.

2. Endspurt. Die Picks entstehen erst ab 30 Minuten vor Schluss, die letzten Minuten fehlen dort. Deshalb direkt aus
   Kalshi: alle abgerechneten Stundenmaerkte der letzten TAGE Tage, je Markt die echten Trades 10, 5 und 2 Minuten vor
   Schluss. Fuer jeden Zeitpunkt: Abstand des Kurses zur Schwelle in Schwankungen (z, aus der realisierten
   1-Minuten-Volatilitaet der Stunde davor), die wahrscheinliche Seite, der Preis, den jemand dafuer wirklich gezahlt
   hat, und ob sie gewann. Ergebnis je Restzeit und z-Stufe, nach Gebuehr.

Stabilitaet: jede Gruppe zusaetzlich getrennt nach aeltere Haelfte und neuere Haelfte. Was nur in einer Haelfte
traegt, ist Zufall.

Laeuft im Hintergrund (Admin-Knopf), Ergebnis in kv optionen_test. Es wird nichts gehandelt.
Seit 2.82.60: Endspurt zuerst, Fortschritt sichtbar, Frist 45 Minuten, Bitstamp als zweite Kursquelle.
Seit 2.82.62: kein Blick in die Zukunft mehr (Kurs nur aus abgeschlossenen Kerzen, Kauf erst danach).
"""
import json
import math
import threading
import time
from datetime import datetime, timezone

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
CB = "https://api.exchange.coinbase.com/products/{}/candles"
_JOB = {"aktiv": False}
_KERZEN = {}


_FRIST = {"bis": 0}


def _bitstamp(prod, t, ende, sek):
    """Zweite Quelle (seit 2.82.60), wenn Coinbase nicht antwortet. -> {ts: close}"""
    paar = prod.replace("-", "").lower()
    try:
        r = requests.get(f"https://www.bitstamp.net/api/v2/ohlc/{paar}/", headers=UA, timeout=10,
                         params={"step": sek, "limit": 1000, "start": int(t), "end": int(ende)})
        d = ((r.json() or {}).get("data") or {}).get("ohlc") or [] if r.ok else []
        return {int(x["timestamp"]): float(x["close"]) for x in d if x.get("timestamp") and x.get("close")}
    except Exception:
        return {}


def _kerzen(prod, von, bis, sek, stand=None, name=""):
    """Kerzen {ts: close} von..bis, in Stuecken zu 290. Coinbase, sonst Bitstamp.
    Seit 2.82.60 mit Fortschritt im Stand und fester Frist (_FRIST), damit der Test nicht ewig haengt."""
    out = {}
    t0 = int(von) - int(von) % sek
    stuecke = max(1, int((bis - t0) // (290 * sek)) + 1)
    t, i = t0, 0
    while t < bis:
        if _FRIST["bis"] and time.time() > _FRIST["bis"]:
            raise TimeoutError("time limit reached, price source too slow")
        i += 1
        if stand is not None:
            stand.update(phase=f"{name} prices {i} of {stuecke}")
        ende = min(int(bis), t + 290 * sek)
        k = (prod, sek, t)
        if k not in _KERZEN:
            d = None
            for v in range(2):
                try:
                    r = requests.get(CB.format(prod), headers=UA, timeout=8, params={
                        "granularity": sek, "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                        "end": datetime.fromtimestamp(ende, timezone.utc).isoformat()})
                    if r.status_code == 429:
                        time.sleep(1.5 * (v + 1))
                        continue
                    d = r.json() if r.ok else None
                    break
                except Exception:
                    time.sleep(0.5)
            werte = {int(x[0]): float(x[4]) for x in d or [] if isinstance(x, list) and len(x) > 4}
            if not werte:
                werte = _bitstamp(prod, t, ende, sek)
            _KERZEN[k] = werte
            time.sleep(0.2)
        out.update(_KERZEN[k])
        t = ende
    return out


def _preis(reihe, ts, sek):
    """Letzter Schlusskurs, der zum Zeitpunkt ts schon bekannt war (Kerze ganz abgeschlossen).
    Seit 2.82.62: vorher wurde die Kerze genommen, die bei ts BEGINNT. Deren Schluss liegt bis zu einer Kerze in der
    Zukunft, im Endspurt also 60 s Blick nach vorn. Das hat die 2-Minuten-Werte stark geschoent."""
    b = int(ts) - int(ts) % sek - sek          # Kerze, die spaetestens bei ts endet
    for d in (0, -sek, -2 * sek):
        if b + d in reihe:
            return reihe[b + d]
    return None


def _gebuehr(p):
    import optionen
    return optionen.gebuehr(p)


def _ergebnis(zeilen):
    """zeilen: (preis, won) -> n, Treffer, Durchschnittspreis, Ergebnis je 10 Kontrakte, Rendite."""
    z = [(p, w) for p, w in zeilen if p is not None and 0 < p < 1]
    if not z:
        return {"n": 0}
    kosten = sum(10 * (p + _gebuehr(p)) for p, _ in z)
    pnl = sum((10 if w else 0) - 10 * (p + _gebuehr(p)) for p, w in z)
    return {"n": len(z), "hit": round(sum(1 for _, w in z if w) / len(z), 3),
            "avg_price": round(sum(p for p, _ in z) / len(z), 3), "pnl": round(pnl, 2),
            "roi": round(pnl / kosten, 4) if kosten else None}


def _mit_haelften(gruppe):
    """gruppe: [(ts, preis, won)] -> gesamt, aeltere Haelfte, neuere Haelfte."""
    g = sorted(gruppe)
    h = len(g) // 2
    return {"all": _ergebnis([(p, w) for _, p, w in g]), "older": _ergebnis([(p, w) for _, p, w in g[:h]]),
            "newer": _ergebnis([(p, w) for _, p, w in g[h:]])}


# ── 1. Trend ─────────────────────────────────────────────────────────────

def trend_test(pg, tage=60, stand=None):
    rows = pg.execute("""SELECT p.coin, p.side, p.price, p.won, EXTRACT(EPOCH FROM p.created), f.avg_60
                           FROM optionen_picks p LEFT JOIN optionen_fill f ON f.pick_id = p.id
                          WHERE p.venue='kalshi' AND p.won IS NOT NULL AND p.created > now() - make_interval(days => %s)
                          ORDER BY p.created""", (tage,)).fetchall()
    if not rows:
        return {"n": 0}
    reihen = {}
    for coin in {r[0] for r in rows}:
        ts = [float(r[4]) for r in rows if r[0] == coin]
        reihen[coin] = _kerzen(f"{coin.upper()}-USD", min(ts) - 7200, max(ts) + 600, 300, stand, f"trend {coin.upper()}")
    gruppen = {}
    for coin, side, preis, won, ts, avg60 in rows:
        ts, preis = float(ts), float(preis)
        r = reihen.get(coin) or {}
        jetzt, vorher = _preis(r, ts, 300), _preis(r, ts - 3600, 300)
        if not jetzt or not vorher:
            continue
        mom = jetzt / vorher - 1
        if abs(mom) < 0.0015:
            art = "flat"
        else:
            art = "with_trend" if (mom > 0) == (side == "yes") else "against_trend"
        call = 0.20 <= preis <= 0.80
        echt = float(avg60) if avg60 is not None else None
        for kreis in ("all_picks", "calls_20_80") if call else ("all_picks",):
            g = gruppen.setdefault(kreis, {}).setdefault(art, {"list": [], "real": []})
            g["list"].append((ts, min(0.99, preis + 0.01), bool(won)))
            if echt:
                g["real"].append((ts, echt, bool(won)))
    out = {}
    for kreis, arten in gruppen.items():
        out[kreis] = {art: {"at_pick_price_plus_1c": _mit_haelften(g["list"]),
                            "at_real_price_60s": _mit_haelften(g["real"])} for art, g in arten.items()}
    return {"n": len(rows), "days": tage, "groups": out}


# ── 2. Endspurt ──────────────────────────────────────────────────────────

def _maerkte(serie, von, bis):
    out, cur = [], None
    for _ in range(40):
        p = {"series_ticker": serie, "status": "settled", "limit": 1000, "min_close_ts": int(von), "max_close_ts": int(bis)}
        if cur:
            p["cursor"] = cur
        try:
            r = requests.get("https://api.elections.kalshi.com/trade-api/v2/markets", params=p, headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(3)
                continue
            j = r.json()
        except Exception as e:
            log.debug(f"optionen_test maerkte: {e}")
            break
        for m in j.get("markets") or []:
            if m.get("strike_type") != "greater" or m.get("result") not in ("yes", "no"):
                continue
            try:
                out.append({"ticker": m["ticker"], "K": float(m["floor_strike"]), "yes": m["result"] == "yes",
                            "T": datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()})
            except Exception:
                continue
        cur = j.get("cursor")
        if not cur:
            break
        time.sleep(0.3)
    return out


def endspurt_test(tage=7, stand=None):
    import optionen
    import optionen_fills as of
    jetzt = time.time()
    zeilen = []
    for coin, (sym, _, serie) in optionen.COINS.items():
        maerkte = _maerkte(serie, jetzt - tage * 86400, jetzt - 600)
        if not maerkte:
            continue
        reihe = _kerzen(f"{sym}-USD", min(m["T"] for m in maerkte) - 4200, max(m["T"] for m in maerkte) + 60, 60,
                        stand, f"final minutes {sym}")
        for i, m in enumerate(maerkte):
            if _FRIST["bis"] and time.time() > _FRIST["bis"]:
                break
            if stand is not None:
                stand.update(phase=f"final minutes: {sym} {i + 1} of {len(maerkte)}")
            T = m["T"]
            s10 = _preis(reihe, T - 600, 60)
            if not s10 or abs(math.log(s10 / m["K"])) > 0.008:
                continue          # weit weg von der Schwelle: dort handelt kaum jemand, Abfrage sparen
            rets = []
            for t in range(int(T - 4200), int(T - 600), 60):
                a, b = reihe.get(t - t % 60), reihe.get(t - t % 60 + 60)
                if a and b:
                    rets.append(math.log(b / a))
            if len(rets) < 30:
                continue
            mu = sum(rets) / len(rets)
            sig = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) or 1e-9
            try:
                tr = of.trades(m["ticker"], T - 660, T - 60)
            except Exception as e:
                log.debug(f"optionen_test trades {m['ticker']}: {e}")
                continue
            for minuten in (10, 5, 2):
                t0 = T - minuten * 60
                s = _preis(reihe, t0, 60)
                if not s:
                    continue
                z = math.log(s / m["K"]) / (sig * math.sqrt(minuten))
                seite = "yes" if z > 0 else "no"
                # erster echter Kauf dieser Seite NACH t0 (der Kurs davor war da schon bekannt), in der Minute danach
                preis = None
                for t in sorted(tr, key=lambda x: of._zeit(x) or 0):
                    ts = of._zeit(t)
                    if ts is None or not (t0 < ts < t0 + 60) or str(t.get("taker_side") or "").lower() != seite:
                        continue
                    preis = of._seite_preis(t, seite)
                    break
                if preis is None:
                    continue
                zeilen.append({"ts": t0, "coin": coin, "min": minuten, "z": abs(z), "price": preis,
                               "won": m["yes"] == (seite == "yes")})
    gruppen = {}
    for z in zeilen:
        stufe = "z_under_1" if z["z"] < 1 else ("z_1_to_2" if z["z"] < 2 else ("z_2_to_3" if z["z"] < 3 else "z_3_plus"))
        gruppen.setdefault(f"{z['min']}_min_left", {}).setdefault(stufe, []).append((z["ts"], z["price"], z["won"]))
        if z["price"] <= 0.95:
            gruppen.setdefault(f"{z['min']}_min_left_price_max_95c", {}).setdefault(stufe, []).append((z["ts"], z["price"], z["won"]))
    out = {k: {s: _mit_haelften(v) for s, v in sorted(st.items())} for k, st in gruppen.items()}
    return {"n": len(zeilen), "days": tage, "groups": out}


# ── 3. Range-Rueckblick (seit 2.82.62) ───────────────────────────────────

def _range_maerkte(serie, von, bis):
    out, cur = [], None
    for _ in range(60):
        if _FRIST["bis"] and time.time() > _FRIST["bis"]:
            break
        p = {"series_ticker": serie, "status": "settled", "limit": 1000, "min_close_ts": int(von), "max_close_ts": int(bis)}
        if cur:
            p["cursor"] = cur
        try:
            r = requests.get("https://api.elections.kalshi.com/trade-api/v2/markets", params=p, headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(3)
                continue
            j = r.json()
        except Exception as e:
            log.debug(f"optionen_test range maerkte: {e}")
            break
        for m in j.get("markets") or []:
            if m.get("strike_type") != "between" or m.get("result") not in ("yes", "no"):
                continue
            try:
                out.append({"ticker": m["ticker"], "lo": float(m["floor_strike"]), "hi": float(m["cap_strike"]),
                            "yes": m["result"] == "yes",
                            "T": int(datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())})
            except Exception:
                continue
        cur = j.get("cursor")
        if not cur:
            break
        time.sleep(0.3)
    return out


def _n(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _p_ueber(F, K, iv, tau_j):
    sd = iv * math.sqrt(max(tau_j, 1e-9))
    if sd <= 0 or F <= 0 or K <= 0:
        return None
    return _n((math.log(F / K) - 0.5 * sd * sd) / sd)


def range_test(pg, tage=14, stand=None):
    """Rueckblick fuer Range-Maerkte mit Daten, die wir schon haben: je Coin und Stundenschluss der Above-X-Pick, der
    am naechsten an 1 h vor Schluss lag, mit seiner Optionszahl (Forward und Volatilitaet). Damit wird jedes Fenster
    in der Naehe (bis 1,5 % vom Kurs) gerechnet. Preis = erster echter Kauf unserer Seite in den 2 Minuten danach.
    Vereinfachung: eine Volatilitaet fuer alle Fenster (die Schiefe fehlt), deshalb nur Fenster nahe am Kurs."""
    import optionen
    import optionen_fills as of
    rows = pg.execute("""SELECT coin, EXTRACT(EPOCH FROM created), EXTRACT(EPOCH FROM t_resolve), iv, fwd
                           FROM optionen_picks WHERE venue='kalshi' AND won IS NOT NULL AND iv IS NOT NULL AND fwd IS NOT NULL
                            AND created > now() - make_interval(days => %s)""", (tage,)).fetchall()
    beste = {}
    for coin, c, T, iv, F in rows:
        c, T = float(c), int(float(T))
        tau_h = (T - c) / 3600
        if not 0.5 <= tau_h <= 3:
            continue
        k = (coin, T)
        if k not in beste or abs(tau_h - 1) < abs(beste[k][3] - 1):
            beste[k] = (c, float(iv), float(F), tau_h)
    serien = {"btc": "KXBTC", "eth": "KXETH"}
    zeilen = []
    for coin, serie in serien.items():
        ts = [T for (cc, T) in beste if cc == coin]
        if not ts:
            continue
        maerkte = _range_maerkte(serie, min(ts) - 60, max(ts) + 60)
        je_T = {}
        for m in maerkte:
            je_T.setdefault(m["T"], []).append(m)
        schluesse = sorted(T for (cc, T) in beste if cc == coin and T in je_T)
        for i, T in enumerate(schluesse):
            if _FRIST["bis"] and time.time() > _FRIST["bis"]:
                break
            if stand is not None:
                stand.update(phase=f"range {coin.upper()} {i + 1} of {len(schluesse)}")
            c, iv, F, tau_h = beste[(coin, T)]
            tau_j = (T - c) / (365.25 * 86400)
            for m in je_T[T]:
                mitte = (m["lo"] + m["hi"]) / 2
                if abs(math.log(mitte / F)) > 0.015:
                    continue
                a, b = _p_ueber(F, m["lo"], iv, tau_j), _p_ueber(F, m["hi"], iv, tau_j)
                if a is None or b is None:
                    continue
                p_ja = max(0.0, min(1.0, a - b))
                try:
                    tr = of.trades(m["ticker"], c, c + 120)
                except Exception:
                    continue
                for seite, p_s in (("yes", p_ja), ("no", 1 - p_ja)):
                    preis = None
                    for t in sorted(tr, key=lambda x: of._zeit(x) or 0):
                        z = of._zeit(t)
                        if z is None or not (c < z <= c + 120) or str(t.get("taker_side") or "").lower() != seite:
                            continue
                        preis = of._seite_preis(t, seite)
                        break
                    if preis is None or not (0.02 <= preis <= 0.98):
                        continue
                    edge = p_s - preis - optionen.gebuehr(preis)
                    zeilen.append({"ts": c, "coin": coin, "price": preis, "edge": edge,
                                   "won": m["yes"] == (seite == "yes")})
    gruppen = {}
    for z in zeilen:
        for schwelle, name in ((0.05, "edge_5c"), (0.08, "edge_8c")):
            if z["edge"] < schwelle:
                continue
            gruppen.setdefault(name, {}).setdefault("all", []).append((z["ts"], z["price"], z["won"]))
            band = "under_20c" if z["price"] < 0.20 else ("20_to_35c" if z["price"] < 0.35 else
                                                         ("35_to_65c" if z["price"] < 0.65 else "over_65c"))
            gruppen[name].setdefault(band, []).append((z["ts"], z["price"], z["won"]))
    out = {k: {b: _mit_haelften(v) for b, v in st.items()} for k, st in gruppen.items()}
    return {"n_windows_priced": len(zeilen), "closes": len(beste), "days": tage, "groups": out}


# ── Job ──────────────────────────────────────────────────────────────────

FRIST_MIN = 45


def _lauf(tage_trend, tage_end):
    """Seit 2.82.60: Endspurt zuerst (der wichtige Teil), jedes Teilergebnis wird sofort gespeichert,
    beide Teile zusammen hoechstens FRIST_MIN Minuten."""
    import optionen
    st = {"start": int(time.time()), "phase": "final minutes", "done": False}
    _JOB.update(aktiv=True, stand=st)
    _FRIST["bis"] = time.time() + FRIST_MIN * 60
    erg = {"ts": int(time.time())}
    try:
        try:
            erg["final_minutes"] = endspurt_test(tage_end, st)
        except Exception as e:
            erg["final_minutes"] = {"error": str(e)[:200]}
        erg["ts"] = int(time.time())
        storage.kv_set("optionen_test", json.dumps(erg))
        st["phase"] = "trend"
        _FRIST["bis"] = time.time() + 25 * 60      # seit 2.82.62: eigenes Zeitfenster, der Endspurt brauchte alles auf
        try:
            pg = optionen._pg()
            erg["trend"] = trend_test(pg, tage_trend, st) if pg else {"error": "no database"}
        except Exception as e:
            erg["trend"] = {"error": str(e)[:200]}
        erg["ts"] = int(time.time())
        storage.kv_set("optionen_test", json.dumps(erg))
        st["phase"] = "range"
        _FRIST["bis"] = time.time() + 30 * 60      # eigenes Zeitfenster fuer den Range-Rueckblick
        try:
            pg = optionen._pg()
            erg["range"] = range_test(pg, 14, st) if pg else {"error": "no database"}
        except Exception as e:
            erg["range"] = {"error": str(e)[:200]}
        erg["ts"] = int(time.time())
        storage.kv_set("optionen_test", json.dumps(erg))
        st.update(done=True, phase="done")
    except Exception as e:
        st.update(error=str(e)[:200])
        log.warning(f"optionen_test: {e}")
    finally:
        _FRIST["bis"] = 0
        _JOB["aktiv"] = False


def starten(tage_trend=60, tage_end=7):
    if _JOB.get("aktiv"):
        return False
    threading.Thread(target=_lauf, args=(tage_trend, tage_end), daemon=True, name="options-test").start()
    return True


def bericht():
    try:
        erg = json.loads(storage.kv_get("optionen_test") or "null")
    except Exception:
        erg = None
    return {"running": bool(_JOB.get("aktiv")), "job": _JOB.get("stand"), "result": erg}
