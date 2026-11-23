"""
optionen_live_neu.py: SOL und XRP in der Live Engine, NUR AUF PAPIER (seit 2.82.68). Kauft nie.

Gleiche Regeln wie BTC und ETH live: Zahl aus der Deribit-Optionsflaeche (USDC-Optionen, optionen_neu.flaechen),
auf den Kurs von jetzt geschoben, kalibriert wie die Live Engine, dann das ECHTE Orderbuch fuer 10 Kontrakte. Ein Call
wird nur geschrieben, wenn zum Buchpreis noch die Live-Schwelle uebrig bleibt. Je Feld (Coin und Schlusszeit) einer.
Hoechstens alle 20 Sekunden und 4 Buchabfragen je Runde, damit BTC und ETH nie gebremst werden.
Eigene Tabelle optionen_live_neu. Abrechnung am echten Kalshi-Ergebnis.
"""
import json
import time

from logger import log

TAKT = 90     # seit 2.82.70: seltener, Kalshi meldete 429 (zu viele Abfragen)
_SCHEMA = [False]
_ZULETZT = [0.0]
_FL = {"ts": 0, "f": {}}
_ST = {"rounds": 0, "books": 0, "calls": 0, "last_round": 0, "error": None}


def _schema(pg):
    if _SCHEMA[0]:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_live_neu (
        id TEXT PRIMARY KEY, coin TEXT, event TEXT, market TEXT, side TEXT, t_resolve TIMESTAMPTZ,
        created TIMESTAMPTZ DEFAULT now(), list_price NUMERIC, book_price NUMERIC, p NUMERIC, edge NUMERIC,
        won BOOLEAN, pnl NUMERIC, settled TIMESTAMPTZ)""")
    _SCHEMA[0] = True


def runde(pg):
    if time.time() - _ZULETZT[0] < TAKT:
        return
    _ZULETZT[0] = time.time()
    import optionen
    import optionen_kalib
    import optionen_live as ol
    import optionen_neu as on
    _schema(pg)
    r = ol._regeln()
    km = optionen_kalib.modell(pg)
    if time.time() - _FL["ts"] > 120:
        try:
            _FL.update(ts=time.time(), f=on.flaechen())
        except Exception as e:
            _ST["error"] = f"surface: {str(e)[:100]}"
            return
    budget = 4
    for coin, (sym, _) in on.COINS.items():
        fl = _FL["f"].get(sym)
        if not fl:
            continue
        try:
            maerkte = on.kalshi(coin)
        except Exception as e:
            _ST["error"] = f"kalshi {coin}: {str(e)[:100]}"
            continue
        kand = []
        for m in maerkte:
            w = optionen.wahrscheinlichkeit(fl, m["K"], m["T"], 1.0)
            if not w or not (optionen.MIN_TAU_H <= w["tau_h"] <= optionen.MAX_TAU_H):
                continue
            for side, ask in (("yes", m["yes_ask"]), ("no", m["no_ask"])):
                if ask is None or not (r["min_p"] <= ask <= r["max_p"]):
                    continue
                p_roh = w["p"] if side == "yes" else 1 - w["p"]
                p_side = optionen_kalib.p(p_roh, ask, w["tau_h"], km)
                edge_l = p_side - ask - optionen.gebuehr(ask)
                if edge_l >= ol.PRE:
                    kand.append((edge_l, m, side, ask, p_side))
        kand.sort(key=lambda x: -x[0])
        for edge_l, m, side, ask, p_side in kand:
            if budget <= 0:
                break
            event = f"kalshi|{coin}|{int(m['T'])}"
            if pg.execute("SELECT 1 FROM optionen_live_neu WHERE event=%s", (event,)).fetchone():
                continue
            budget -= 1
            try:
                bp, _b = ol._buchpreis(m["market"], side)
            except Exception:
                continue
            _ST["books"] += 1
            if bp is None:
                continue
            edge_b = p_side - bp - optionen.gebuehr(bp)
            if edge_b < r["edge"]:
                continue
            pg.execute("""INSERT INTO optionen_live_neu (id, coin, event, market, side, t_resolve, list_price, book_price, p, edge)
                          VALUES (%s,%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                       (event, coin, event, m["market"], side, m["T"], ask, bp, round(p_side, 4), round(edge_b, 4)))
            _ST["calls"] += 1
    _ST["rounds"] += 1
    _ST["last_round"] = int(time.time())
    try:
        import storage
        storage.kv_set("optionen_live_neu_stand", json.dumps(_ST))
    except Exception:
        pass


def abrechnen(pg):
    import optionen
    _schema(pg)
    for pid, market, side, bp in pg.execute(
            """SELECT id, market, side, book_price FROM optionen_live_neu
               WHERE settled IS NULL AND t_resolve < now() - interval '2 minutes' LIMIT 40""").fetchall():
        ja = optionen._ergebnis("kalshi", market)
        if ja is None:
            continue
        won = ja if side == "yes" else not ja
        bp = float(bp)
        pnl = round(10 * ((1.0 if won else 0.0) - bp - optionen.gebuehr(bp)), 4)
        pg.execute("UPDATE optionen_live_neu SET won=%s, pnl=%s, settled=now() WHERE id=%s", (won, pnl, pid))


def bericht():
    import optionen
    pg = optionen._pg()
    if not pg:
        return {"error": "no database"}
    _schema(pg)
    out = {"coins": {}}
    try:
        import storage
        out["status"] = json.loads(storage.kv_get("optionen_live_neu_stand") or "null")
    except Exception:
        out["status"] = None
    for coin in ("sol", "xrp"):
        rows = [(float(a), bool(b), float(c or 0)) for a, b, c in pg.execute(
            """SELECT book_price, won, pnl FROM optionen_live_neu WHERE coin=%s AND settled IS NOT NULL
               ORDER BY created""", (coin,)).fetchall()]
        offen = pg.execute("SELECT count(*) FROM optionen_live_neu WHERE coin=%s AND settled IS NULL", (coin,)).fetchone()[0]

        def zs(z):
            if not z:
                return {"n": 0}
            k = sum(10 * (p + optionen.gebuehr(p)) for p, _, _ in z)
            pnl = sum(x for _, _, x in z)
            return {"n": len(z), "hit": round(sum(1 for _, w, _ in z if w) / len(z), 3), "pnl": round(pnl, 2),
                    "roi": round(pnl / k, 4) if k else None, "avg_price": round(sum(p for p, _, _ in z) / len(z), 3)}
        h = len(rows) // 2
        out["coins"][coin] = {"open": int(offen), "all": zs(rows), "older": zs(rows[:h]), "newer": zs(rows[h:])}
    return out
