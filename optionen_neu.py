"""
optionen_neu.py: Above X Testspur fuer weitere Coins (SOL, XRP). Seit 2.82.23.

Gleiche Idee wie optionen.py (BTC/ETH): aus den Deribit-Optionen folgt eine Wahrscheinlichkeit fuer
"Coin ueber X zum Zeitpunkt T", daneben steht der Kalshi-Preis (KXSOLD, KXXRPD, Schnitt der letzten
60 Sekunden des CF Benchmarks RTI). Liegt die Zahl um mehr als OPT_EDGE ueber dem Ask (Gebuehr abgezogen),
wird ein Pick mitgeschrieben: 10 Anteile zum echten Ask, abgerechnet am echten Ergebnis.

Bewusst GETRENNT von optionen.py:
  - eigene Tabellen optionen_neu_picks und optionen_neu_cal, nichts landet in optionen_picks,
  - keine Calls in den Kanal, kein Auto-Modus, nichts oeffentlich,
  - nur Admin: /v1/admin/options/new.
So bleibt die Bilanz von BTC/ETH unberuehrt, bis die neue Spur sich ueber Wochen bewiesen hat.

Deribit: SOL und XRP gibt es nur als lineare USDC-Optionen (SOL_USDC-17NOV26-71-C, XRP_USDC-17NOV26-0d95-C,
"d" steht fuer das Komma). Ein Abruf (currency=USDC) liefert alle Coins. Der Index steht als
estimated_delivery_price in jeder Zeile.

Einstellung: OPT_NEW_COINS (Standard "sol,xrp", leer schaltet die Spur ab). Laeuft im Dienst crypto aus optionen.tick.
"""
import json
import math
import os
import time
from datetime import datetime, timezone

from logger import log

BEKANNT = {"sol": ("SOL", "KXSOLD"), "xrp": ("XRP", "KXXRPD")}
COINS = {c: BEKANNT[c] for c in [x.strip().lower() for x in os.getenv("OPT_NEW_COINS", "sol,xrp").split(",")] if c in BEKANNT}
CALL_EDGE = float(os.getenv("OPT_CALLS_EDGE", "0.08"))
CALL_MIN = float(os.getenv("OPT_CALLS_MIN_PRICE", "0.20"))
CALL_MAX = float(os.getenv("OPT_CALLS_MAX_PRICE", "0.80"))
_SCHEMA = [False]
_STAND = {"ts": 0, "error": None, "markets": {}, "expiries": {}}


def _pg():
    import optionen
    pg = optionen._pg()
    if pg and not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS optionen_neu_picks (
            id TEXT PRIMARY KEY, venue TEXT, coin TEXT, event TEXT, market TEXT, strike NUMERIC,
            side TEXT, price NUMERIC, fee NUMERIC, p_model NUMERIC, iv NUMERIC, fwd NUMERIC, edge NUMERIC,
            ask_size NUMERIC, tau_h NUMERIC, t_resolve TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now(),
            won BOOLEAN, pnl NUMERIC, settled TIMESTAMPTZ)""")
        pg.execute("""CREATE TABLE IF NOT EXISTS optionen_neu_cal (
            id TEXT PRIMARY KEY, coin TEXT, market TEXT, chk NUMERIC, p_model NUMERIC, p_market NUMERIC,
            t_resolve TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now(), yes BOOLEAN, settled TIMESTAMPTZ)""")
        pg.execute("""CREATE TABLE IF NOT EXISTS optionen_neu_calls (
            event TEXT PRIMARY KEY, pick_id TEXT, coin TEXT, market TEXT, side TEXT, price NUMERIC, edge NUMERIC,
            strike NUMERIC, t_resolve TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now(), bought BOOLEAN DEFAULT FALSE,
            won BOOLEAN, pnl NUMERIC, settled TIMESTAMPTZ, real_price NUMERIC, real_pnl NUMERIC, real_checked BOOLEAN DEFAULT FALSE)""")
        _SCHEMA[0] = True
    return pg


def _strike(code):
    return float(code.replace("d", "."))


def flaechen():
    """{SYM: {"index": .., "exp": {verfall_ts: {"F": forward, "pts": [(ln(K/F), iv), ...]}}}} fuer alle USDC-Coins."""
    import optionen
    d = optionen._get(optionen.DERIBIT + "/get_book_summary_by_currency", currency="USDC", kind="option")["result"]
    roh = {}
    for x in d:
        teile = x["instrument_name"].split("-")
        if len(teile) != 4 or not teile[0].endswith("_USDC"):
            continue
        sym = teile[0][:-5]
        try:
            t = optionen._verfall(teile[1])
            K = _strike(teile[2])
        except Exception:
            continue
        iv = optionen._f(x.get("mark_iv"))
        F = optionen._f(x.get("underlying_price"))
        if not iv or iv <= 0 or not F or K <= 0:
            continue
        c = roh.setdefault(sym, {"idx": [], "exp": {}})
        ix = optionen._f(x.get("estimated_delivery_price"))
        if ix:
            c["idx"].append(ix)
        # aus dem Geld: Calls ueber F, Puts unter F (die liquideren Seiten)
        if (teile[3] == "C" and K < F * 0.995) or (teile[3] == "P" and K > F * 1.005):
            continue
        e = c["exp"].setdefault(t, {"F": F, "pts": {}})
        e["pts"].setdefault(round(math.log(K / F), 5), []).append(iv / 100.0)
    out = {}
    for sym, c in roh.items():
        exp = {}
        for t, e in c["exp"].items():
            pts = sorted((k, sum(v) / len(v)) for k, v in e["pts"].items())
            if len(pts) >= 3:
                exp[t] = {"F": e["F"], "pts": pts}
        if exp and c["idx"]:
            idx = sorted(c["idx"])[len(c["idx"]) // 2]
            out[sym] = {"index": idx, "exp": exp}
    return out


def kalshi(coin):
    import optionen
    serie = COINS[coin][1]
    out, cur = [], None
    for _ in range(4):
        p = {"series_ticker": serie, "status": "open", "limit": 1000}
        if cur:
            p["cursor"] = cur
        j = optionen._get(optionen.KALSHI + "/markets", **p)
        for m in j.get("markets", []):
            if m.get("strike_type") != "greater":
                continue
            K = optionen._f(m.get("floor_strike"))
            ya, yb = optionen._f(m.get("yes_ask_dollars")), optionen._f(m.get("yes_bid_dollars"))
            na, nb = optionen._f(m.get("no_ask_dollars")), optionen._f(m.get("no_bid_dollars"))
            T = m.get("close_time")
            if not K or not T or ya is None or na is None or (ya >= 1 and na >= 1):
                continue
            # Kalshi nennt nur die Groesse am Ja-Ask und am Ja-Bid. Nein kaufen heisst gegen den Ja-Bid handeln.
            out.append({"venue": "kalshi", "coin": coin, "event": m.get("event_ticker"), "title": m.get("title"),
                        "market": m.get("ticker"), "K": K,
                        "T": datetime.fromisoformat(T.replace("Z", "+00:00")).timestamp(),
                        "yes_bid": yb, "yes_ask": ya, "no_ask": na, "no_bid": nb,
                        "yes_size": optionen._f(m.get("yes_ask_size_fp")), "no_size": optionen._f(m.get("yes_bid_size_fp"))})
        cur = j.get("cursor")
        if not cur:
            break
    return out


def tick():
    """Aus optionen.tick, im selben Takt. Fehler hier duerfen BTC/ETH nie stoeren."""
    if not COINS:
        return None
    import optionen
    pg = _pg()
    if not pg:
        return None
    try:
        fl = flaechen()
    except Exception as e:
        _STAND.update(ts=time.time(), error=f"deribit: {str(e)[:120]}")
        log.warning(f"optionen neu deribit: {e}")
        return None
    _STAND.update(ts=time.time(), error=None)
    neue = []
    for coin, (sym, _) in COINS.items():
        f = fl.get(sym)
        if not f:
            _STAND["markets"][coin] = 0
            _STAND["expiries"][coin] = 0
            continue
        _STAND["expiries"][coin] = len(f["exp"])
        try:
            maerkte = kalshi(coin)
        except Exception as e:
            _STAND["error"] = f"kalshi {coin}: {str(e)[:120]}"
            log.debug(f"optionen neu kalshi {coin}: {e}")
            continue
        _STAND["markets"][coin] = len(maerkte)
        for m in maerkte:
            w = optionen.wahrscheinlichkeit(f, m["K"], m["T"], 1.0)
            if not w:
                continue
            p, tau = w["p"], w["tau_h"]
            mid = round((m["yes_bid"] + m["yes_ask"]) / 2, 4) if m["yes_bid"] is not None else m["yes_ask"]
            try:
                for chk in optionen.CHECKS_H:
                    if chk - 0.5 <= tau <= chk + 0.5 and mid is not None and 0.02 <= mid <= 0.98:
                        pg.execute("""INSERT INTO optionen_neu_cal (id, coin, market, chk, p_model, p_market, t_resolve)
                                      VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT DO NOTHING""",
                                   (f"{m['market']}|{chk}", coin, m["market"], chk, round(p, 4), mid, m["T"]))
                if not (optionen.MIN_TAU_H <= tau <= optionen.MAX_TAU_H):
                    continue
                for side, ps, ask, size in (("yes", p, m["yes_ask"], m.get("yes_size")),
                                            ("no", 1 - p, m["no_ask"], m.get("no_size"))):
                    if ask is None or not (0.04 <= ask <= 0.96):
                        continue
                    edge = ps - ask - optionen.gebuehr(ask)
                    if edge < optionen.EDGE:
                        continue
                    cur = pg.execute("""INSERT INTO optionen_neu_picks (id, venue, coin, event, market, strike, side, price, fee,
                                    p_model, iv, fwd, edge, ask_size, tau_h, t_resolve)
                                  VALUES (%s,'kalshi',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s))
                                  ON CONFLICT DO NOTHING""",
                               (f"{m['market']}|{side}", coin, m["event"], m["market"], m["K"], side, ask,
                                optionen.gebuehr(ask), round(ps, 4), round(w["iv"], 4), w["fwd"], round(edge, 4), size,
                                round(tau, 2), m["T"]))
                    if getattr(cur, "rowcount", 0) == 1:
                        neue.append({"id": f"{m['market']}|{side}", "coin": coin, "venue": "kalshi", "side": side,
                                     "price": ask, "edge": edge, "K": m["K"], "T": m["T"], "market": m["market"], "p": ps})
            except Exception as e:
                log.debug(f"optionen neu merken: {e}")
        if neue:   # seit 2.82.84: Calls je Coin sofort, damit ein Kauf nicht auf den naechsten Coin wartet
            try:
                calls(pg, [c for c in neue if c["coin"] == coin])
            except Exception as e:
                log.warning(f"optionen neu calls: {e}")
    try:
        _abrechnen(pg)
    except Exception as e:
        log.debug(f"optionen neu abrechnen: {e}")
    try:
        _calls_abrechnen(pg)
    except Exception as e:
        log.debug(f"optionen neu calls abrechnen: {e}")
    try:
        import storage
        storage.kv_set("optionen_neu_stand", json.dumps(_STAND))
    except Exception:
        pass
    return _STAND


CALLS_TAG = int(os.getenv("OPT_NEW_CALLS_DAY", "12"))


def auto_coins():
    return [x.strip().lower() for x in os.getenv("OPT_NEW_AUTO", "").split(",") if x.strip()]


def calls(pg, neue):
    """Seit 2.82.84: dieselbe Call-Logik wie bei BTC/ETH (der staerkste Pick je Termin, Vorsprung ab 8 Cent, Preis
    20 bis 80 Cent), eigenes Tageslimit. Mit OPT_NEW_AUTO=sol (oder sol,xrp) kauft Auto mode diese Calls auch."""
    best = {}
    for c in neue:
        if c["edge"] >= CALL_EDGE and CALL_MIN <= float(c["price"]) <= CALL_MAX:
            key = f"{c['coin']}|{int(c['T'])}"
            if key not in best or c["edge"] > best[key]["edge"]:
                best[key] = c
    if not best:
        return
    heute = pg.execute("SELECT count(*) FROM optionen_neu_calls WHERE created > now() - interval '24 hours'").fetchone()[0]
    an = auto_coins()
    for key, c in sorted(best.items(), key=lambda kv: -kv[1]["edge"]):
        if heute >= CALLS_TAG:
            break
        if pg.execute("SELECT 1 FROM optionen_neu_calls WHERE event=%s", (key,)).fetchone():
            continue
        gekauft = False
        if c["coin"] in an:
            try:
                import optionen_auto
                optionen_auto.kaufen(pg, c)
                gekauft = True
            except Exception as e:
                log.warning(f"optionen neu autotrade: {e}")
        pg.execute("""INSERT INTO optionen_neu_calls (event, pick_id, coin, market, side, price, edge, strike, t_resolve, bought)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s) ON CONFLICT DO NOTHING""",
                   (key, c["id"], c["coin"], c["market"], c["side"], c["price"], round(c["edge"], 4), c["K"], c["T"], gekauft))
        heute += 1


def _calls_abrechnen(pg):
    import optionen
    sh = optionen.SHARES
    for ev, pid, side, price in pg.execute(
            """SELECT c.event, c.pick_id, c.side, c.price FROM optionen_neu_calls c
               WHERE c.settled IS NULL AND c.t_resolve < now() - interval '5 minutes' LIMIT 40""").fetchall():
        r = pg.execute("SELECT won FROM optionen_neu_picks WHERE id=%s AND settled IS NOT NULL", (pid,)).fetchone()
        if not r:
            continue
        won = bool(r[0])
        pr = float(price)
        pnl = (sh if won else 0.0) - sh * (pr + optionen.gebuehr(pr))
        pg.execute("UPDATE optionen_neu_calls SET won=%s, pnl=%s, settled=now() WHERE event=%s", (won, round(pnl, 4), ev))
    # echter Preis: was Kaeufer auf unserer Seite in den 60 Sekunden danach wirklich gezahlt haben
    for ev, mkt, side, price, won, t0 in pg.execute(
            """SELECT event, market, side, price, won, EXTRACT(EPOCH FROM created) FROM optionen_neu_calls
               WHERE settled IS NOT NULL AND NOT real_checked ORDER BY created LIMIT 1""").fetchall():
        try:
            import optionen_fills
            z = optionen_fills.pruefen((ev, mkt, side, float(price), float(t0)))
            rp = z.get("avg_60")
            rpnl = None
            if rp is not None:
                rp = float(rp)
                rpnl = round((sh if won else 0.0) - sh * (rp + optionen.gebuehr(rp)), 4)
            pg.execute("UPDATE optionen_neu_calls SET real_checked=TRUE, real_price=%s, real_pnl=%s WHERE event=%s",
                       (rp, rpnl, ev))
        except Exception as e:
            log.debug(f"optionen neu real price: {e}")
            break


def calls_bericht(pg):
    out = {"auto": auto_coins(), "per_day": CALLS_TAG, "coins": {}}
    for coin in sorted(COINS):
        rows = pg.execute("""SELECT price, won, pnl, real_price, real_pnl, settled IS NOT NULL, bought, created
                             FROM optionen_neu_calls WHERE coin=%s ORDER BY created""", (coin,)).fetchall()
        fertig = [r for r in rows if r[5]]
        real = [r for r in fertig if r[4] is not None]

        def zs(z, i_pnl, i_pr):
            if not z:
                return {"n": 0}
            import optionen
            k = sum(10 * (float(r[i_pr]) + optionen.gebuehr(float(r[i_pr]))) for r in z)
            p = sum(float(r[i_pnl]) for r in z)
            return {"n": len(z), "hit": round(sum(1 for r in z if r[1]) / len(z), 3), "pnl": round(p, 2),
                    "roi": round(p / k, 4) if k else None}
        out["coins"][coin] = {"calls": len(rows), "open": len(rows) - len(fertig), "bought": sum(1 for r in rows if r[6]),
                              "at_call": zs(fertig, 2, 0), "at_real": zs(real, 4, 3),
                              "real_pending": len(fertig) - len(real)}
    return out


def _abrechnen(pg):
    import optionen
    sh = optionen.SHARES
    for (pid, market, side, price, fee) in pg.execute(
            "SELECT id, market, side, price, fee FROM optionen_neu_picks "
            "WHERE settled IS NULL AND t_resolve < now() - interval '5 minutes' LIMIT 80").fetchall():
        ja = optionen._ergebnis("kalshi", market)
        if ja is None:
            continue
        won = ja if side == "yes" else not ja
        pnl = (sh if won else 0.0) - sh * (float(price) + float(fee))
        pg.execute("UPDATE optionen_neu_picks SET won=%s, pnl=%s, settled=now() WHERE id=%s", (won, pnl, pid))
    gesehen = {}
    for (cid, market) in pg.execute("SELECT id, market FROM optionen_neu_cal WHERE settled IS NULL "
                                    "AND t_resolve < now() - interval '5 minutes' LIMIT 300").fetchall():
        if market not in gesehen:
            gesehen[market] = optionen._ergebnis("kalshi", market)
        if gesehen[market] is None:
            continue
        pg.execute("UPDATE optionen_neu_cal SET yes=%s, settled=now() WHERE id=%s", (gesehen[market], cid))


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
            "days": len(tage), "green_days": sum(1 for v in tage.values() if v > 0),
            "by_day": [{"day": d, "pnl": round(v, 2)} for d, v in sorted(tage.items())][-21:]}


def bericht():
    """Admin: jede neue Spur fuer sich, dazu derselbe Call-Filter wie bei BTC/ETH und die Kalibrierung."""
    import optionen
    sh = optionen.SHARES
    out = {"coins": {}, "tracked": sorted(COINS), "status": None,
           "note": "Separate test track. Nothing here goes into the Above X record, the channel or Auto mode."}
    try:
        import storage
        st = json.loads(storage.kv_get("optionen_neu_stand") or "{}")
        if st:
            st["age_s"] = int(time.time() - float(st.get("ts") or 0))
        out["status"] = st or None
    except Exception:
        pass
    pg = _pg()
    if not pg:
        return out
    for coin in sorted(set(COINS) | {r[0] for r in pg.execute("SELECT DISTINCT coin FROM optionen_neu_picks").fetchall()}):
        rows = [{"side": r[0], "price": float(r[1]), "fee": float(r[2] or 0), "edge": float(r[3] or 0),
                 "size": None if r[4] is None else float(r[4]), "won": r[5], "pnl": float(r[6] or 0), "end": r[7],
                 "settled": r[8] is not None}
                for r in pg.execute("""SELECT side, price, fee, edge, ask_size, won, pnl, t_resolve, settled
                                       FROM optionen_neu_picks WHERE coin=%s ORDER BY t_resolve""", (coin,)).fetchall()]
        fertig = [r for r in rows if r["settled"]]
        # derselbe Filter wie die Calls bei BTC/ETH: grosser Vorsprung, Preis 20 bis 80 Cent, der staerkste je Termin
        best = {}
        for r in fertig:
            if r["edge"] >= CALL_EDGE and CALL_MIN <= r["price"] <= CALL_MAX:
                k = r["end"]
                if k not in best or r["edge"] > best[k]["edge"]:
                    best[k] = r
        fuellbar = [r for r in fertig if r["size"] is not None and r["size"] >= sh]
        cal = [{"hours_before": float(c[0]), "n": int(c[1]), "options_brier": round(float(c[2]), 4),
                "market_brier": round(float(c[3]), 4)}
               for c in pg.execute("""SELECT chk, count(*), avg(power(p_model - (CASE WHEN yes THEN 1 ELSE 0 END), 2)),
                                             avg(power(p_market - (CASE WHEN yes THEN 1 ELSE 0 END), 2))
                                      FROM optionen_neu_cal WHERE coin=%s AND settled IS NOT NULL
                                      GROUP BY chk ORDER BY chk DESC""", (coin,)).fetchall()]
        out["coins"][coin] = {"name": BEKANNT.get(coin, (coin.upper(),))[0], "open": len(rows) - len(fertig),
                              "all": _sum(fertig, sh), "calls_filter": _sum(list(best.values()), sh),
                              "fillable": _sum(fuellbar, sh), "calibration": cal,
                              "since": rows[0]["end"].isoformat() if rows else None}
    try:
        out["calls"] = calls_bericht(pg)
    except Exception as e:
        out["calls_error"] = str(e)[:160]
    return out
