"""
optionen_range.py: Testspur fuer Kalshi Range-Maerkte auf BTC und ETH (seit 2.82.60).

Ein Range-Markt fragt: "Liegt BTC zum Zeitpunkt T zwischen A und B?" (KXBTC, KXETH). Aus derselben
Deribit-Flaeche wie Above X folgt:  P(A <= Kurs < B) = P(Kurs > A) - P(Kurs > B).
Die Randfenster (nur "ueber A" oder "unter B") werden genauso gerechnet.

Bewusst GETRENNT von allem anderen:
  - eigene Tabellen optionen_range_picks und optionen_range_cal,
  - keine Calls in den Kanal, kein Auto-Modus, nichts oeffentlich, nichts im Above-X-Rekord,
  - nur Admin: /v1/admin/options/range.
Es wird nichts gekauft. Mitgeschrieben wird ein Pick (10 Kontrakte zum echten Ask aus der Liste) immer dann, wenn die
Zahl mindestens OPT_EDGE ueber Ask plus Gebuehr liegt. Abgerechnet am echten Kalshi-Ergebnis.

Weil Range-Maerkte vor allem aus billigen Fenstern bestehen und die Engine dort laut Kalibrierung zu optimistisch ist,
zeigt der Bericht das Ergebnis zusaetzlich nach Preisband (unter 20, 20 bis 35, 35 bis 65, ueber 65 Cent).

Nutzt die Flaeche, die optionen.tick gerade gelesen hat (optionen._FL), also keine zusaetzlichen Deribit-Abfragen.
Einstellung: OPT_RANGE (Standard "1", "0" schaltet die Spur ab).
"""
import json
import os
import time
from datetime import datetime, timezone

from logger import log

AN = os.getenv("OPT_RANGE", "1").strip().lower() not in ("0", "off", "false", "no")
SERIEN = {"btc": ("BTC", "KXBTC"), "eth": ("ETH", "KXETH")}
CALL_EDGE = float(os.getenv("OPT_CALLS_EDGE", "0.08"))
CALL_MIN = float(os.getenv("OPT_CALLS_MIN_PRICE", "0.20"))
CALL_MAX = float(os.getenv("OPT_CALLS_MAX_PRICE", "0.80"))
TAKT = float(os.getenv("OPT_RANGE_INTERVAL_SEC", "300"))   # seit 2.82.70: 300 statt 120 s, Kalshi-Limit
BAENDER = (("under_20c", 0.0, 0.20), ("20_to_35c", 0.20, 0.35), ("35_to_65c", 0.35, 0.65), ("over_65c", 0.65, 1.0))
_SCHEMA = [False]
_LETZT = [0.0]
_STAND = {"ts": 0, "error": None, "markets": {}}


def _pg():
    import optionen
    pg = optionen._pg()
    if pg and not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS optionen_range_picks (
            id TEXT PRIMARY KEY, coin TEXT, event TEXT, market TEXT, lo NUMERIC, hi NUMERIC,
            side TEXT, price NUMERIC, fee NUMERIC, p_model NUMERIC, edge NUMERIC, ask_size NUMERIC, tau_h NUMERIC,
            t_resolve TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now(), won BOOLEAN, pnl NUMERIC, settled TIMESTAMPTZ)""")
        pg.execute("""CREATE TABLE IF NOT EXISTS optionen_range_cal (
            id TEXT PRIMARY KEY, coin TEXT, market TEXT, chk NUMERIC, p_model NUMERIC, p_market NUMERIC,
            t_resolve TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now(), yes BOOLEAN, settled TIMESTAMPTZ)""")
        _SCHEMA[0] = True
    return pg


def kalshi(coin):
    """Offene Range-Maerkte: {lo, hi} mit None fuer einen offenen Rand."""
    import optionen
    serie = SERIEN[coin][1]
    out, cur = [], None
    for _ in range(5):
        p = {"series_ticker": serie, "status": "open", "limit": 1000}
        if cur:
            p["cursor"] = cur
        j = optionen._get(optionen.KALSHI + "/markets", **p)
        for m in j.get("markets", []):
            art = m.get("strike_type")
            lo, hi = optionen._f(m.get("floor_strike")), optionen._f(m.get("cap_strike"))
            if art == "between" and lo and hi:
                pass
            elif art == "greater" and lo:
                hi = None
            elif art == "less" and hi:
                lo = None
            else:
                continue
            ya, yb = optionen._f(m.get("yes_ask_dollars")), optionen._f(m.get("yes_bid_dollars"))
            na = optionen._f(m.get("no_ask_dollars"))
            T = m.get("close_time")
            if not T or ya is None or na is None or (ya >= 1 and na >= 1):
                continue
            out.append({"coin": coin, "event": m.get("event_ticker"), "market": m.get("ticker"), "lo": lo, "hi": hi,
                        "T": datetime.fromisoformat(T.replace("Z", "+00:00")).timestamp(),
                        "yes_ask": ya, "yes_bid": yb, "no_ask": na,
                        "yes_size": optionen._f(m.get("yes_ask_size_fp")), "no_size": optionen._f(m.get("yes_bid_size_fp"))})
        cur = j.get("cursor")
        if not cur:
            break
    return out


def _p_fenster(fl, lo, hi, T):
    import optionen
    oben = optionen.wahrscheinlichkeit(fl, lo, T) if lo else None
    unten = optionen.wahrscheinlichkeit(fl, hi, T) if hi else None
    if (lo and not oben) or (hi and not unten):
        return None
    p_lo = oben["p"] if oben else 1.0
    p_hi = unten["p"] if unten else 0.0
    tau = (oben or unten)["tau_h"]
    return {"p": max(0.0, min(1.0, p_lo - p_hi)), "tau_h": tau}


def tick():
    """Aus optionen.tick, hoechstens alle TAKT Sekunden. Fehler hier stoeren Above X nie."""
    if not AN or time.time() - _LETZT[0] < TAKT:
        return None
    _LETZT[0] = time.time()
    import optionen
    pg = _pg()
    if not pg:
        return None
    _STAND.update(ts=time.time(), error=None, best_edge_c=None, best=None)
    for coin, (sym, _) in SERIEN.items():
        fl = optionen._FL.get(sym)
        if not fl or time.time() - float(fl.get("_ts") or 0) > 900:
            _STAND["markets"][coin] = 0
            continue
        try:
            maerkte = kalshi(coin)
        except Exception as e:
            _STAND["error"] = f"kalshi {coin}: {str(e)[:120]}"
            continue
        _STAND["markets"][coin] = len(maerkte)
        for m in maerkte:
            w = _p_fenster(fl, m["lo"], m["hi"], m["T"])
            if not w:
                continue
            p, tau = w["p"], w["tau_h"]
            mid = round((m["yes_bid"] + m["yes_ask"]) / 2, 4) if m["yes_bid"] is not None else m["yes_ask"]
            try:
                for chk in optionen.CHECKS_H:
                    if chk - 0.5 <= tau <= chk + 0.5 and mid is not None and 0.02 <= mid <= 0.98:
                        pg.execute("""INSERT INTO optionen_range_cal (id, coin, market, chk, p_model, p_market, t_resolve)
                                      VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT DO NOTHING""",
                                   (f"{m['market']}|{chk}", coin, m["market"], chk, round(p, 4), mid, m["T"]))
                if not (optionen.MIN_TAU_H <= tau <= optionen.MAX_TAU_H):
                    continue
                for side, ps, ask, size in (("yes", p, m["yes_ask"], m["yes_size"]), ("no", 1 - p, m["no_ask"], m["no_size"])):
                    if ask is None or not (0.04 <= ask <= 0.96):
                        continue
                    edge = ps - ask - optionen.gebuehr(ask)
                    if _STAND.get("best_edge_c") is None or edge * 100 > _STAND["best_edge_c"]:
                        _STAND["best_edge_c"] = round(edge * 100, 1)
                        _STAND["best"] = {"market": m["market"], "side": side, "ask": ask, "p": round(ps, 3)}
                    if edge < optionen.EDGE:
                        continue
                    pg.execute("""INSERT INTO optionen_range_picks (id, coin, event, market, lo, hi, side, price, fee,
                                    p_model, edge, ask_size, tau_h, t_resolve)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT DO NOTHING""",
                               (f"{m['market']}|{side}", coin, m["event"], m["market"], m["lo"], m["hi"], side, ask,
                                optionen.gebuehr(ask), round(ps, 4), round(edge, 4), size, round(tau, 2), m["T"]))
            except Exception as e:
                _STAND["error"] = f"pick: {str(e)[:120]}"
                log.debug(f"optionen range merken: {e}")
    try:
        _abrechnen(pg)
    except Exception as e:
        log.debug(f"optionen range abrechnen: {e}")
    try:
        import storage
        storage.kv_set("optionen_range_stand", json.dumps(_STAND))
    except Exception:
        pass
    return _STAND


def _abrechnen(pg):
    import optionen
    sh = optionen.SHARES
    gesehen = {}

    def erg(market):
        if market not in gesehen:
            gesehen[market] = optionen._ergebnis("kalshi", market)
        return gesehen[market]

    for (pid, market, side, price, fee) in pg.execute(
            "SELECT id, market, side, price, fee FROM optionen_range_picks "
            "WHERE settled IS NULL AND t_resolve < now() - interval '5 minutes' LIMIT 150").fetchall():
        ja = erg(market)
        if ja is None:
            continue
        won = ja if side == "yes" else not ja
        pnl = (sh if won else 0.0) - sh * (float(price) + float(fee))
        pg.execute("UPDATE optionen_range_picks SET won=%s, pnl=%s, settled=now() WHERE id=%s", (won, pnl, pid))
    for (cid, market) in pg.execute("SELECT id, market FROM optionen_range_cal WHERE settled IS NULL "
                                    "AND t_resolve < now() - interval '5 minutes' LIMIT 300").fetchall():
        ja = erg(market)
        if ja is None:
            continue
        pg.execute("UPDATE optionen_range_cal SET yes=%s, settled=now() WHERE id=%s", (ja, cid))


def _sum(rows, sh):
    if not rows:
        return None
    cost = sum(sh * (r["price"] + r["fee"]) for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    tage = {}
    for r in rows:
        d = r["end"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        tage[d] = tage.get(d, 0.0) + r["pnl"]
    return {"n": len(rows), "wins": sum(1 for r in rows if r["won"]),
            "win_rate": round(sum(1 for r in rows if r["won"]) / len(rows), 4), "cost": round(cost, 2),
            "pnl": round(pnl, 2), "roi": round(pnl / cost, 4) if cost else None,
            "avg_price": round(sum(r["price"] for r in rows) / len(rows), 3),
            "days": len(tage), "green_days": sum(1 for v in tage.values() if v > 0)}


def bericht():
    import optionen
    sh = optionen.SHARES
    out = {"on": AN, "coins": {}, "status": None,
           "note": "Range markets test track. Nothing is bought and nothing goes into the Above X record, the channel or Auto mode."}
    try:
        import storage
        st = json.loads(storage.kv_get("optionen_range_stand") or "{}")
        if st:
            st["age_s"] = int(time.time() - float(st.get("ts") or 0))
        out["status"] = st or None
    except Exception:
        pass
    pg = _pg()
    if not pg:
        return out
    for coin in SERIEN:
        rows = [{"side": r[0], "price": float(r[1]), "fee": float(r[2] or 0), "edge": float(r[3] or 0),
                 "size": None if r[4] is None else float(r[4]), "won": r[5], "pnl": float(r[6] or 0), "end": r[7],
                 "settled": r[8] is not None}
                for r in pg.execute("""SELECT side, price, fee, edge, ask_size, won, pnl, t_resolve, settled
                                       FROM optionen_range_picks WHERE coin=%s ORDER BY t_resolve""", (coin,)).fetchall()]
        fertig = [r for r in rows if r["settled"]]
        best = {}
        for r in fertig:
            if r["edge"] >= CALL_EDGE and CALL_MIN <= r["price"] <= CALL_MAX:
                if r["end"] not in best or r["edge"] > best[r["end"]]["edge"]:
                    best[r["end"]] = r
        fuellbar = [r for r in fertig if r["size"] is not None and r["size"] >= sh]
        baender = {name: _sum([r for r in fertig if lo <= r["price"] < hi], sh) for name, lo, hi in BAENDER}
        cal = [{"hours_before": float(c[0]), "n": int(c[1]), "engine": round(float(c[2]), 4), "market": round(float(c[3]), 4)}
               for c in pg.execute("""SELECT chk, count(*), avg(power(p_model - (CASE WHEN yes THEN 1 ELSE 0 END), 2)),
                                             avg(power(p_market - (CASE WHEN yes THEN 1 ELSE 0 END), 2))
                                      FROM optionen_range_cal WHERE coin=%s AND settled IS NOT NULL
                                      GROUP BY chk ORDER BY chk DESC""", (coin,)).fetchall()]
        out["coins"][coin] = {"name": SERIEN[coin][0], "open": len(rows) - len(fertig), "all": _sum(fertig, sh),
                              "calls_filter": _sum(list(best.values()), sh), "fillable": _sum(fuellbar, sh),
                              "by_price": baender, "accuracy": cal,
                              "since": rows[0]["end"].isoformat() if rows else None}
    return out
