"""
optionen_maker.py: Maker-Test auf Papier fuer Above X (seit 2.82.67). Es wird NICHTS gekauft und keine Order gelegt.

Frage: Was waere, wenn die Engine nicht sofort zum Ask kauft, sondern selbst eine kleine Kauforder 2 Cent unter dem
Listenpreis ins Buch legt und 5 Minuten wartet? Dafuer braucht es kein grosses Kapital: eine Order ueber 10 Kontrakte
kostet so viel wie jeder normale Kauf, nur der Preis ist unserer.

Ablauf:
  1. merken(): je Feld (Coin und Schlusszeit) der beste Kandidat, bei dem der Vorsprung zum Maker-Preis
     (Listen-Ask minus 2 Cent, Maker-Gebuehr geschaetzt als ein Viertel der normalen) mindestens so gross ist wie die
     Live-Schwelle. Gleiche Zahl wie die Live Engine (kalibriert, wenn die Kalibrierung aktiv ist).
  2. pruefen(): nach 6 Minuten die echten Kalshi-Trades der 5 Minuten danach. Gefuellt gilt nur vorsichtig:
     ein Verkaeufer hat UNTER unserem Preis verkauft (dann waren alle Gebote auf unserem Preis schon weg, auch
     unseres), oder auf unserem Preis oder darunter wurden mindestens 30 Kontrakte gehandelt.
  3. Abrechnung am echten Ergebnis, nur die gefuellten zaehlen.
Ergebnis je Hälfte (aeltere, neuere), damit Zufall auffaellt. Eigene Tabelle optionen_maker.
"""
import os
import time

from logger import log

ABSTAND = float(os.getenv("OPT_MAKER_GAP", "0.02"))
WARTEN_S = int(os.getenv("OPT_MAKER_WAIT_S", "300"))
MAKER_ANTEIL = float(os.getenv("OPT_MAKER_FEE_SHARE", "0.25"))
STUECK = 10
_SCHEMA = [False]
_ZULETZT = [0.0]


def _schema(pg):
    if _SCHEMA[0]:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_maker (
        id TEXT PRIMARY KEY, coin TEXT, event TEXT, market TEXT, side TEXT, price NUMERIC, fee NUMERIC, p NUMERIC,
        edge NUMERIC, list_ask NUMERIC, created TIMESTAMPTZ DEFAULT now(), t_resolve TIMESTAMPTZ,
        checked TIMESTAMPTZ, filled BOOLEAN, fill_s NUMERIC, won BOOLEAN, pnl NUMERIC, settled TIMESTAMPTZ)""")
    _SCHEMA[0] = True


def gebuehr(p):
    import optionen
    return optionen.gebuehr(p) * MAKER_ANTEIL


def merken(pg, coin, kand, regeln):
    """kand: Liste wie in optionen_live.runde (edge_l, m, w, side, ask, p_side, ...), schon nach Vorsprung sortiert."""
    try:
        _schema(pg)
        gesehen = set()
        for x in kand:
            m, side, ask, p_side = x[1], x[3], x[4], x[5]
            preis = round(ask - ABSTAND, 2)
            if preis < regeln["min_p"] or preis > regeln["max_p"]:
                continue
            event = f"kalshi|{coin}|{int(m['T'])}"
            if event in gesehen:
                continue
            edge = p_side - preis - gebuehr(preis)
            if edge < regeln["edge"]:
                continue
            gesehen.add(event)
            pg.execute("""INSERT INTO optionen_maker (id, coin, event, market, side, price, fee, p, edge, list_ask, t_resolve)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT DO NOTHING""",
                       (event, coin, event, m["market"], side, preis, round(gebuehr(preis), 4), round(p_side, 4),
                        round(edge, 4), ask, m["T"]))
    except Exception as e:
        log.debug(f"maker merken: {e}")


def _menge(t):
    for k in ("count_fp", "count"):
        try:
            v = float(t.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 1.0


def pruefen(pg):
    """Hoechstens alle 60 s: Fuellung pruefen und abrechnen."""
    if time.time() - _ZULETZT[0] < 60:
        return
    _ZULETZT[0] = time.time()
    try:
        import optionen
        import optionen_fills as of
        _schema(pg)
        for mid, market, side, preis, created in pg.execute(
                """SELECT id, market, side, price, EXTRACT(EPOCH FROM created) FROM optionen_maker
                   WHERE checked IS NULL AND created < now() - make_interval(secs => %s) LIMIT 20""",
                (WARTEN_S + 60,)).fetchall():
            preis, c = float(preis), float(created)
            try:
                tr = of.trades(market, c, c + WARTEN_S)
            except Exception:
                continue
            gegen = "no" if side == "yes" else "yes"     # wer an uns verkauft, kauft als Taker die Gegenseite
            unter, menge, erst = False, 0.0, None
            for t in sorted(tr, key=lambda x: of._zeit(x) or 0):
                z = of._zeit(t)
                if z is None or not (c < z <= c + WARTEN_S) or str(t.get("taker_side") or "").lower() != gegen:
                    continue
                p = of._seite_preis(t, side)
                if p is None:
                    continue
                if p < preis - 1e-9:
                    unter = True
                if p <= preis + 1e-9:
                    menge += _menge(t)
                if (unter or menge >= 30) and erst is None:
                    erst = z - c
            filled = unter or menge >= 30
            pg.execute("UPDATE optionen_maker SET checked=now(), filled=%s, fill_s=%s WHERE id=%s",
                       (filled, round(erst, 1) if erst is not None else None, mid))
        for mid, market, side, preis, fee in pg.execute(
                """SELECT id, market, side, price, fee FROM optionen_maker
                   WHERE filled AND settled IS NULL AND t_resolve < now() - interval '2 minutes' LIMIT 40""").fetchall():
            ja = optionen._ergebnis("kalshi", market)
            if ja is None:
                continue
            won = ja if side == "yes" else not ja
            pnl = round(STUECK * ((1.0 if won else 0.0) - float(preis) - float(fee or 0)), 4)
            pg.execute("UPDATE optionen_maker SET won=%s, pnl=%s, settled=now() WHERE id=%s", (won, pnl, mid))
    except Exception as e:
        log.debug(f"maker pruefen: {e}")


def _sum(rows):
    if not rows:
        return {"n": 0}
    kosten = sum(STUECK * (r[0] + r[1]) for r in rows)
    pnl = sum(r[3] for r in rows)
    return {"n": len(rows), "hit": round(sum(1 for r in rows if r[2]) / len(rows), 3), "pnl": round(pnl, 2),
            "roi": round(pnl / kosten, 4) if kosten else None,
            "avg_price": round(sum(r[0] for r in rows) / len(rows), 3)}


def bericht():
    import optionen
    pg = optionen._pg()
    if not pg:
        return {"error": "no database"}
    _schema(pg)
    alle = pg.execute("SELECT count(*), count(checked), count(*) FILTER (WHERE filled) FROM optionen_maker").fetchone()
    rows = [(float(a), float(b or 0), bool(c), float(d or 0)) for a, b, c, d in pg.execute(
        """SELECT price, fee, won, pnl FROM optionen_maker WHERE filled AND settled IS NOT NULL ORDER BY created""").fetchall()]
    h = len(rows) // 2
    return {"orders": int(alle[0]), "checked": int(alle[1]), "filled": int(alle[2]),
            "fill_rate": round(alle[2] / alle[1], 3) if alle[1] else None,
            "all": _sum(rows), "older": _sum(rows[:h]), "newer": _sum(rows[h:]),
            "rules": {"gap_c": int(ABSTAND * 100), "wait_s": WARTEN_S, "fee_share": MAKER_ANTEIL}}
