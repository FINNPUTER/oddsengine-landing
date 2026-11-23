"""Above X: haette man den Call Preis wirklich bekommen? (seit 2.82.39)

Fuer jeden alten Kalshi-Pick liest dieses Modul die echten Trades von Kalshi (oeffentlich, ohne Schluessel) im
Fenster kurz nach dem Pick. Gezaehlt werden nur Trades, bei denen ein Kaeufer UNSERE Seite genommen hat
(taker_side = unsere Seite). Nur das beweist, dass dort jemand zu diesem Preis verkauft hat.

Ergebnis je Pick (Tabelle optionen_fill):
  n_60, n_300        Kontrakte, die auf unserer Seite zum Call Preis oder billiger gehandelt wurden (60 s, 300 s)
  min_60, min_300    billigster Preis auf unserer Seite im Fenster
  first_s            Sekunden bis zum ersten solchen Trade
  avg_60             Durchschnittspreis aller Kaeufe unserer Seite in 60 s (der "Marktpreis" danach)
Laeuft im Hintergrund, setzt nach einem Neustart dort fort, wo es war. Admin: /v1/admin/options/fills.
"""
import csv
import io
import json
import os
import threading
import time
from datetime import datetime, timezone

from logger import log

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ABSTAND = float(os.getenv("OPT_FILL_PAUSE", "0.15"))
_LAUF = {"aktiv": False, "start": 0, "fertig": 0, "fehler": 0, "letzter_fehler": None}
_LOCK = threading.Lock()


def _pg():
    import optionen
    return optionen._pg()


def _schema(pg):
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_fill (
        pick_id TEXT PRIMARY KEY, checked TIMESTAMPTZ DEFAULT now(), trades INTEGER,
        n_60 NUMERIC, n_300 NUMERIC, min_60 NUMERIC, min_300 NUMERIC, first_s NUMERIC, avg_60 NUMERIC, note TEXT)""")


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _seite_preis(t, seite):
    """Preis unserer Seite in Dollar. Neue Felder (_dollars) zuerst, alte (Cent) als Rueckfall."""
    p = _f(t.get(f"{seite}_price_dollars"))
    if p is None:
        c = _f(t.get(f"{seite}_price"))
        p = c / 100.0 if c is not None and c > 1 else c
    return p


def _menge(t):
    return _f(t.get("count_fp")) or _f(t.get("count")) or 0.0


def _zeit(t):
    s = t.get("created_time") or ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def trades(ticker, von, bis):
    """Alle Trades eines Markts im Zeitfenster. Erst der normale Pfad, fuer alte Maerkte /historical."""
    import requests
    out = []
    for basis in ("/markets/trades", "/historical/trades"):
        cur = None
        for _ in range(10):
            p = {"ticker": ticker, "min_ts": int(von), "max_ts": int(bis), "limit": 1000}
            if cur:
                p["cursor"] = cur
            time.sleep(ABSTAND)
            r = requests.get(KALSHI + basis, params=p, timeout=15)
            if r.status_code == 429:
                time.sleep(3)
                r = requests.get(KALSHI + basis, params=p, timeout=15)
            if r.status_code in (400, 404) and basis != "/markets/trades":
                break   # den historischen Pfad gibt es fuer diesen Markt nicht: dann eben keine Trades
            r.raise_for_status()
            j = r.json()
            out += j.get("trades") or []
            cur = j.get("cursor")
            if not cur:
                break
        if out:
            break
    return out


def pruefen(pick):
    """pick: (id, market, side, price, created_ts) -> Zeile fuer optionen_fill."""
    pid, ticker, seite, preis, t0 = pick
    t0, preis = float(t0), float(preis)   # aus Postgres kommen Decimal-Werte (seit 2.82.41)
    tr = trades(ticker, t0 - 5, t0 + 300)
    eigen = []
    for t in tr:
        if str(t.get("taker_side") or "").lower() != seite:
            continue
        p, ts = _seite_preis(t, seite), _zeit(t)
        if p is None or ts is None:
            continue
        eigen.append((ts - t0, p, _menge(t)))
    grenze = float(preis) + 1e-9

    def bis(sek):
        return [e for e in eigen if -5 <= e[0] <= sek]
    e60, e300 = bis(60), bis(300)
    billig60 = [e for e in e60 if e[1] <= grenze]
    billig300 = [e for e in e300 if e[1] <= grenze]
    menge60 = sum(e[2] for e in e60)
    return {"pick_id": pid, "trades": len(tr),
            "n_60": round(sum(e[2] for e in billig60), 2), "n_300": round(sum(e[2] for e in billig300), 2),
            "min_60": min((e[1] for e in e60), default=None), "min_300": min((e[1] for e in e300), default=None),
            "first_s": round(min((e[0] for e in billig300), default=-1), 1) if billig300 else None,
            "avg_60": round(sum(e[1] * e[2] for e in e60) / menge60, 4) if menge60 else None,
            "note": None if tr else "no trades in the window"}


def _offen(pg, tage):
    # Die Calls, die Auto mode wirklich kauft, zuerst. Danach alle anderen Picks, neueste zuerst.
    try:
        return pg.execute("""SELECT p.id, p.market, p.side, p.price, EXTRACT(EPOCH FROM p.created)
                             FROM optionen_picks p LEFT JOIN optionen_fill f ON f.pick_id = p.id
                             LEFT JOIN optionen_calls c ON c.pick_id = p.id
                             WHERE p.venue = 'kalshi' AND f.pick_id IS NULL AND p.settled IS NOT NULL
                               AND p.created > now() - make_interval(days => %s)
                             ORDER BY (c.pick_id IS NULL), p.created DESC""", (tage,)).fetchall()
    except Exception:
        return pg.execute("""SELECT p.id, p.market, p.side, p.price, EXTRACT(EPOCH FROM p.created)
                             FROM optionen_picks p LEFT JOIN optionen_fill f ON f.pick_id = p.id
                             WHERE p.venue = 'kalshi' AND f.pick_id IS NULL AND p.settled IS NOT NULL
                               AND p.created > now() - make_interval(days => %s)
                             ORDER BY p.created DESC""", (tage,)).fetchall()


def _lauf(tage):
    pg = _pg()
    try:
        _schema(pg)
        liste = _offen(pg, tage)
        _LAUF.update(start=int(time.time()), fertig=0, offen=len(liste), fehler=0)
        for pick in liste:
            try:
                z = pruefen(pick)
                pg.execute("""INSERT INTO optionen_fill (pick_id, trades, n_60, n_300, min_60, min_300, first_s, avg_60, note)
                              VALUES (%(pick_id)s,%(trades)s,%(n_60)s,%(n_300)s,%(min_60)s,%(min_300)s,%(first_s)s,%(avg_60)s,%(note)s)
                              ON CONFLICT (pick_id) DO NOTHING""", z)
                _LAUF["fertig"] += 1
            except Exception as e:
                _LAUF["fehler"] += 1
                _LAUF["letzter_fehler"] = str(e)[:160]
                if _LAUF["fehler"] > 50 and _LAUF["fehler"] > _LAUF["fertig"]:
                    log.warning(f"optionen fills: too many errors, stop: {e}")
                    break
                time.sleep(2)
    except Exception as e:
        _LAUF["letzter_fehler"] = str(e)[:160]
        log.warning(f"optionen fills: {e}")
    finally:
        _LAUF["aktiv"] = False
        _LAUF["ende"] = int(time.time())


def starten(tage=60):
    with _LOCK:
        if _LAUF["aktiv"]:
            return dict(_LAUF)
        _LAUF.update(aktiv=True, letzter_fehler=None)
    threading.Thread(target=_lauf, args=(tage,), daemon=True).start()
    return dict(_LAUF)


def _zeilen(pg, tage):
    import optionen
    sql = """SELECT p.created, p.coin, p.side, p.price, p.fee, p.won, p.pnl, f.trades, f.n_60, f.n_300,
                    f.min_60, f.min_300, f.first_s, f.avg_60, f.note, p.market, p.strike, {call}, p.p_model, p.t_resolve
             FROM optionen_picks p JOIN optionen_fill f ON f.pick_id = p.id {join}
             WHERE p.venue = 'kalshi' AND p.settled IS NOT NULL
               AND p.created > now() - make_interval(days => %s)
             ORDER BY p.created"""
    try:
        return pg.execute(sql.format(call="c.pick_id IS NOT NULL",
                                     join="LEFT JOIN optionen_calls c ON c.pick_id = p.id"), (tage,)).fetchall()
    except Exception:
        return pg.execute(sql.format(call="FALSE", join=""), (tage,)).fetchall()


def bericht(tage=60):
    """Je Tag: alle Picks gegen die, bei denen auf unserer Seite wirklich zum Call Preis gehandelt wurde."""
    import optionen
    pg = _pg()
    if not pg:
        return {"error": "no database"}
    _schema(pg)
    sh = optionen.SHARES
    rows = _zeilen(pg, tage)
    gesamt = pg.execute("SELECT count(*) FROM optionen_picks WHERE venue='kalshi' AND settled IS NOT NULL "
                        "AND created > now() - make_interval(days => %s)", (tage,)).fetchone()[0]
    out = {"job": dict(_LAUF), "kalshi_picks": int(gesamt), "checked": len(rows), "shares": sh}

    def summe(zs, preis_fn=None):
        n = len(zs)
        if not n:
            return {"n": 0}
        pnl = kosten = 0.0
        gew = 0
        for z in zs:
            p = preis_fn(z) if preis_fn else float(z[3])
            f = optionen.gebuehr(p)
            kosten += sh * (p + f)
            pnl += sh * ((1.0 if z[5] else 0.0) - p - f)
            gew += 1 if z[5] else 0
        return {"n": n, "pnl": round(pnl, 2), "roi": round(pnl / kosten, 4) if kosten else None, "hit": round(gew / n, 4)}
    tage_d = {}
    for z in rows:
        tage_d.setdefault(z[0].strftime("%Y-%m-%d"), []).append(z)
    out["by_day"] = []
    alle, f60, f300, markt = [], [], [], []
    for tag, zs in sorted(tage_d.items()):
        a = [z for z in zs]
        b60 = [z for z in zs if float(z[8] or 0) >= sh]
        b300 = [z for z in zs if float(z[9] or 0) >= sh]
        mk = [z for z in zs if z[13] is not None]
        alle += a
        f60 += b60
        f300 += b300
        markt += mk
        out["by_day"].append({"day": tag, "all": summe(a), "fillable_60s": summe(b60), "fillable_300s": summe(b300),
                              "at_market_60s": summe(mk, lambda z: float(z[13]))})
    out["total"] = {"all": summe(alle), "fillable_60s": summe(f60), "fillable_300s": summe(f300),
                    "at_market_60s": summe(markt, lambda z: float(z[13])),
                    "share_fillable_60s": round(len(f60) / len(alle), 4) if alle else None,
                    "no_trades": sum(1 for z in rows if z[14])}
    # Seit 2.82.41: nur die Calls, die Auto mode wirklich kauft (Filter des Call-Kanals)
    cs = [z for z in rows if len(z) > 17 and z[17]]
    out["calls_only"] = {"all": summe(cs), "fillable_60s": summe([z for z in cs if float(z[8] or 0) >= sh]),
                         "fillable_300s": summe([z for z in cs if float(z[9] or 0) >= sh]),
                         "at_market_60s": summe([z for z in cs if z[13] is not None], lambda z: float(z[13]))}
    # Seit 2.82.42: den Call-Filter rueckwirkend auf ALLE Picks anwenden. So sieht man ueber alle Tage,
    # ob der Filter zum echten Preis traegt, nicht nur seit es den Kanal gibt.
    try:
        import optionen_calls as oc
        gesehen, tag_n, retro = set(), {}, []
        for z in rows:   # nach Zeit sortiert
            p_, preis = float(z[18] or 0), float(z[3])
            edge = p_ - preis - optionen.gebuehr(preis)
            if edge < oc.EDGE or not (oc.MIN_P <= preis <= oc.MAX_P) or z[1] in oc.SKIP:
                continue
            key = (z[1], z[19])          # ein Call je Coin und Termin
            if key in gesehen:
                continue
            tag = z[0].strftime("%Y-%m-%d")
            if tag_n.get(tag, 0) >= oc.PRO_TAG:
                continue
            gesehen.add(key)
            tag_n[tag] = tag_n.get(tag, 0) + 1
            retro.append(z)
        out["call_filter_all_days"] = {
            "all": summe(retro), "fillable_60s": summe([z for z in retro if float(z[8] or 0) >= sh]),
            "at_market_60s": summe([z for z in retro if z[13] is not None], lambda z: float(z[13])),
            "by_day": [{"day": d, "all": summe([z for z in retro if z[0].strftime("%Y-%m-%d") == d]),
                        "at_market_60s": summe([z for z in retro if z[0].strftime("%Y-%m-%d") == d and z[13] is not None],
                                               lambda z: float(z[13]))}
                       for d in sorted({z[0].strftime("%Y-%m-%d") for z in retro})]}
    except Exception as e:
        out["call_filter_all_days"] = {"error": str(e)[:120]}
    out["how"] = ("fillable = at least the call size traded on our side at the call price or cheaper within the window. "
                  "at_market_60s = the same pick bought at the average price buyers on our side actually paid in the 60 s after.")
    return out


def csv_text(tage=60):
    pg = _pg()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["created_utc", "coin", "market", "strike", "side", "call_price", "won", "record_pnl", "trades_in_window",
                "our_side_at_or_below_call_60s", "our_side_at_or_below_call_300s", "cheapest_60s", "cheapest_300s",
                "seconds_to_first_fill", "avg_price_paid_60s", "note"])
    if not pg:
        return buf.getvalue()
    _schema(pg)
    for z in _zeilen(pg, tage):
        w.writerow([z[0].isoformat(), z[1], z[15], z[16], z[2], z[3], "" if z[5] is None else int(z[5]), z[6], z[7],
                    z[8], z[9], z[10], z[11], z[12], z[13], z[14] or ""])
    return buf.getvalue()


# ══ Absicherungs-Test (seit 2.82.90): Gewinn festmachen, wenn die Gegenseite billig wird. NUR LESEN. ══
# Fuer jeden abgerechneten Channel Call: alle echten Kalshi-Trades vom Call bis zum Schluss. Gemerkt werden nur
# Kaeufe der GEGENSEITE (taker_side = Gegenseite), und davon jeder neue Tiefstpreis mit Zeitpunkt. Damit laesst sich
# fuer jede Schwelle L nachrechnen: sobald Call-Preis + Gegenseite + Gebuehr <= L, haette man die Gegenseite gekauft
# und $1 sicher gehabt (Gewinn = 1 - Summe). Sonst bleibt es beim normalen Ergebnis. Verglichen mit "einfach halten".
# Das Gleiche fuer die echten Auto-mode-Kaeufe mit dem wirklich bezahlten Preis.
LOCK_STUFEN = (0.70, 0.75, 0.80, 0.85, 0.90)
_LOCK_SCHEMA = [False]
_LOCK_ST = {"done": 0, "errors": 0, "last": 0, "last_error": None}


def _lock_schema(pg):
    if _LOCK_SCHEMA[0]:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_lock (
        pick_id TEXT PRIMARY KEY, coin TEXT, side TEXT, price NUMERIC, won BOOLEAN, created TIMESTAMPTZ,
        t_resolve TIMESTAMPTZ, trades INTEGER, mins JSONB, checked TIMESTAMPTZ DEFAULT now())""")
    _LOCK_SCHEMA[0] = True


def _gebuehr(p):
    return 0.07 * p * (1 - p)


def lock_pruefen(ticker, seite, t0, t1):
    """Neue Tiefstpreise der Gegenseite zwischen Kauf und Schluss: [[sekunden_nach_kauf, preis], ...]."""
    gegen = "no" if seite == "yes" else "yes"
    tr = trades(ticker, t0, t1)
    pts = []
    for t in tr:
        if str(t.get("taker_side") or "").lower() != gegen:
            continue
        p, ts = _seite_preis(t, gegen), _zeit(t)
        if p is not None and ts is not None and ts >= t0:
            pts.append((ts - t0, p))
    pts.sort()
    mins, tief = [], 9.0
    for s, p in pts:
        if p < tief - 1e-9:
            tief = p
            mins.append([round(s, 1), round(p, 4)])
    return len(tr), mins


def lock_runde(pg=None, menge=25):
    pg = pg or _pg()
    if not pg:
        return
    _lock_schema(pg)
    rows = pg.execute("""SELECT p.id, p.market, p.side, p.price, p.won, p.coin, p.created, p.t_resolve
                         FROM optionen_calls c JOIN optionen_picks p ON p.id = c.pick_id
                         LEFT JOIN optionen_lock l ON l.pick_id = p.id
                         WHERE p.venue = 'kalshi' AND p.settled IS NOT NULL AND l.pick_id IS NULL
                         ORDER BY p.created DESC LIMIT %s""", (menge,)).fetchall()
    for pid, mk, seite, preis, won, coin, cr, tr_ in rows:
        try:
            n, mins = lock_pruefen(mk, seite, cr.timestamp(), tr_.timestamp())
            pg.execute("""INSERT INTO optionen_lock (pick_id, coin, side, price, won, created, t_resolve, trades, mins)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                       (pid, coin, seite, preis, won, cr, tr_, n, json.dumps(mins)))
            _LOCK_ST["done"] += 1
        except Exception as e:
            _LOCK_ST["errors"] += 1
            _LOCK_ST["last_error"] = str(e)[:160]
            time.sleep(2)
    _LOCK_ST["last"] = int(time.time())
    try:
        import storage
        storage.kv_set("optionen_lock_stand", json.dumps(_LOCK_ST))
    except Exception:
        pass


def _lock_wert(bezahlt, won, mins, stufe):
    """(pnl je Kontrakt mit Absicherung, abgesichert?, Sekunden bis dahin). bezahlt inkl. Gebuehr."""
    for s, p in mins or []:
        if bezahlt + p + _gebuehr(p) <= stufe + 1e-9:
            return 1 - bezahlt - p - _gebuehr(p), True, s
    return (1 - bezahlt) if won else -bezahlt, False, None


def lock_bericht(pg=None):
    pg = pg or _pg()
    try:
        import storage
        st = json.loads(storage.kv_get("optionen_lock_stand") or "null") or dict(_LOCK_ST)
    except Exception:
        st = dict(_LOCK_ST)
    out = {"status": st, "levels": [], "auto": []}
    if not pg:
        return out
    _lock_schema(pg)
    rows = pg.execute("SELECT pick_id, price, won, mins, created FROM optionen_lock ORDER BY created").fetchall()
    out["checked"] = len(rows)
    if not rows:
        return out
    halten = []
    for pid, preis, won, mins, cr in rows:
        b = float(preis) + _gebuehr(float(preis))
        halten.append(((1 - b) if won else -b, b, cr.strftime("%Y-%m-%d")))
    h_sum = sum(x[0] for x in halten)
    h_cost = sum(x[1] for x in halten)
    tage_h = {}
    for x in halten:
        tage_h[x[2]] = tage_h.get(x[2], 0) + x[0]
    out["hold"] = {"n": len(rows), "pnl_per_contract": round(h_sum, 3), "roi": round(h_sum / h_cost, 4) if h_cost else None,
                   "days": len(tage_h), "green_days": sum(1 for v in tage_h.values() if v > 0),
                   "worst_day": round(min(tage_h.values()), 3)}
    for stufe in LOCK_STUFEN:
        summe, n_lock, n_lock_won, sek, tage = 0.0, 0, 0, [], {}
        for pid, preis, won, mins, cr in rows:
            b = float(preis) + _gebuehr(float(preis))
            v, ab, s = _lock_wert(b, won, mins if isinstance(mins, list) else json.loads(mins or "[]"), stufe)
            summe += v
            d = cr.strftime("%Y-%m-%d")
            tage[d] = tage.get(d, 0) + v
            if ab:
                n_lock += 1
                n_lock_won += 1 if won else 0
                sek.append(s)
        out["levels"].append({"level_c": int(round(stufe * 100)), "locked": n_lock,
                              "locked_would_have_won": n_lock_won,
                              "pnl_per_contract": round(summe, 3), "roi": round(summe / h_cost, 4) if h_cost else None,
                              "vs_hold": round(summe - h_sum, 3),
                              "green_days": sum(1 for v in tage.values() if v > 0), "days": len(tage),
                              "worst_day": round(min(tage.values()), 3),
                              "median_min": round(sorted(sek)[len(sek) // 2] / 60, 1) if sek else None})
    # Echte Auto-mode-Kaeufe mit wirklich bezahltem Preis (nur die, deren Call schon geprueft ist)
    try:
        ar = pg.execute("""SELECT a.filled, a.cost, a.fees, l.won, l.mins FROM optionen_auto a
                           JOIN optionen_lock l ON l.pick_id = a.pick_id WHERE a.filled > 0""").fetchall()
        if ar:
            h = sum((float(f) if w else 0) - float(c) - float(g or 0) for f, c, g, w, m in ar)
            out["auto_hold_usd"] = round(h, 2)
            out["auto_orders"] = len(ar)
            for stufe in LOCK_STUFEN:
                s = 0.0
                for f, c, g, w, m in ar:
                    b = (float(c) + float(g or 0)) / float(f)
                    v, _, _ = _lock_wert(b, w, m if isinstance(m, list) else json.loads(m or "[]"), stufe)
                    s += v * float(f)
                out["auto"].append({"level_c": int(round(stufe * 100)), "pnl_usd": round(s, 2), "vs_hold": round(s - h, 2)})
    except Exception as e:
        out["auto_error"] = str(e)[:160]
    return out
