"""Above X: alles mitschreiben, was der Markt im Moment eines Picks hergibt (seit 2.82.28).

Der Rekord bucht jeden Pick zum besten Ask. Ob 10 Stueck zu dem Preis wirklich da waren, stand bisher nirgends.
Dieses Modul schreibt je Pick in optionen_picks.data:
  mkt   Bid, Ask, Spanne, Groesse am besten Ask, Volumen, Open Interest, letzter Preis, Edge, Restzeit
  book  die naechsten Preisstufen der Kaufseite (Kalshi: /markets/<ticker>/orderbook, Polymarket: CLOB)
  depth Stueck, die bis zum Ask, +1c, +2c und +5c zu haben waren
  path  Bid, Ask, Groesse und Edge in den Runden nach dem Pick (bis zu 12 Eintraege, etwa eine Stunde)
Und je Order im Auto-Modus (optionen_auto.info): Buch vor der Order, Dauer in ms, die Antwort von Kalshi.
Nichts hier entscheidet etwas: es wird nur geschrieben. Ein Fehler beim Lesen kostet nie einen Pick.
Admin: /v1/admin/options/depth (format=csv fuer jeden Pick), /v1/admin/options/autotrade/orders (format=csv).
"""
import csv
import hashlib
import io
import json
import time

from logger import log

STUFEN = (0, 1, 2, 5)          # Cent ueber dem Ask
PFAD_MAX = 12
PFAD_MIN = 65                  # Minuten nach dem Pick


def _f(x, d=None):
    try:
        return float(x)
    except Exception:
        return d


def _preis(x):
    """Kalshi liefert Dollar als Text ('0.4200') oder Cent als Zahl (42)."""
    v = _f(x)
    if v is None:
        return None
    return round(v / 100.0, 4) if (v > 1.0 and not isinstance(x, str)) else round(v, 4)


def _stufen(liste):
    out = []
    for z in liste or []:
        try:
            p, q = _preis(z[0]), _f(z[1])
        except Exception:
            continue
        if p is not None and q:
            out.append((p, q))
    return out


def buch_kalshi(ticker, holen):
    """holen(pfad) -> dict. Gibt {'yes': [(preis, stueck)], 'no': [...]} zurueck: die GEBOTE je Seite."""
    j = holen(f"/markets/{ticker}/orderbook") or {}
    ob = j.get("orderbook_fp") or j.get("orderbook") or j
    return {"yes": _stufen(ob.get("yes_dollars") or ob.get("yes")),
            "no": _stufen(ob.get("no_dollars") or ob.get("no"))}


def kaufseite(buch, side):
    """Wer Ja kauft, trifft auf die Nein-Gebote: Ask = 1 - Gebot der Gegenseite. Aufsteigend nach Preis."""
    gegen = buch.get("no" if side == "yes" else "yes") or []
    return sorted(((round(1 - p, 4), q) for p, q in gegen), key=lambda z: z[0])


def tiefe(asks, ask):
    """Stueck bis Ask + n Cent, kumuliert."""
    out = {}
    for c in STUFEN:
        grenze = ask + c / 100.0 + 1e-9
        out[f"plus{c}c"] = round(sum(q for p, q in asks if p <= grenze), 2)
    return out


def markt(m, side, ask, edge):
    """Was die Marktliste ohnehin schon liefert, kostet keine Anfrage."""
    bid = m.get("yes_bid") if side == "yes" else m.get("no_bid")
    d = {"ts": int(time.time()), "ask": ask, "bid": bid,
         "spread": round(ask - bid, 4) if bid is not None else None,
         "ask_size": m.get("yes_size") if side == "yes" else m.get("no_size"),
         "edge": edge, "tau_h": m.get("tau_h"), "index": m.get("index"), "mid": m.get("mid"),
         "vol": m.get("vol"), "vol24": m.get("vol24"), "oi": m.get("oi"), "liq": m.get("liq"), "last": m.get("last")}
    return {k: v for k, v in d.items() if v is not None}


def nach_pick(pg, pid, m, side, ask, holen, pm_buch=None):
    """Direkt nach einem neuen Pick: Orderbuch holen und dazuschreiben."""
    extra = {}
    try:
        if m.get("venue") == "kalshi":
            asks = kaufseite(buch_kalshi(m["market"], holen), side)
        else:
            asks = sorted(pm_buch or [], key=lambda z: z[0])
        extra["book"] = [[p, q] for p, q in asks[:10]]
        extra["depth"] = tiefe(asks, ask)
    except Exception as e:
        extra["book_err"] = str(e)[:120]
    try:
        pg.execute("UPDATE optionen_picks SET data = COALESCE(data, '{}'::jsonb) || %s::jsonb WHERE id=%s",
                   (json.dumps(extra), pid))
    except Exception as e:
        log.debug(f"optionen tiefe nach_pick: {e}")


def junge(pg):
    """Offene Picks der letzten Stunde: id -> [Minuten seit Pick, Zahl der Pfadeintraege]."""
    out = {}
    try:
        for pid, alter, n in pg.execute(
                """SELECT id, EXTRACT(EPOCH FROM (now() - created)) / 60.0,
                          jsonb_array_length(COALESCE(data->'path', '[]'::jsonb))
                   FROM optionen_picks WHERE settled IS NULL AND created > now() - make_interval(mins => %s)""",
                (PFAD_MIN,)).fetchall():
            out[pid] = [float(alter), int(n)]
    except Exception as e:
        log.debug(f"optionen tiefe junge: {e}")
    return out


def pfad(pg, m, jung):
    """Jede Runde: wie steht der Markt jetzt bei den Picks der letzten Stunde."""
    if not jung:
        return
    for side in ("yes", "no"):
        pid = f"{m['venue']}|{m['market']}|{side}"
        z = jung.get(pid)
        if not z or z[1] >= PFAD_MAX or z[0] < 1:
            continue
        ask = m["yes_ask"] if side == "yes" else m["no_ask"]
        e = {"m": round(z[0], 1), "ask": ask, "bid": m.get("yes_bid") if side == "yes" else m.get("no_bid"),
             "size": m.get("yes_size") if side == "yes" else m.get("no_size"),
             "edge": m.get("edge_yes") if side == "yes" else m.get("edge_no"),
             "p": m.get("p") if side == "yes" else (round(1 - m["p"], 4) if m.get("p") is not None else None)}
        try:
            pg.execute("""UPDATE optionen_picks SET data = jsonb_set(COALESCE(data, '{}'::jsonb), '{path}',
                          COALESCE(data->'path', '[]'::jsonb) || %s::jsonb) WHERE id=%s""",
                       (json.dumps([{k: v for k, v in e.items() if v is not None}]), pid))
            z[1] += 1
        except Exception as ex:
            log.debug(f"optionen tiefe pfad: {ex}")


# ── Auswertung ───────────────────────────────────────────────────────────
def _zeilen(pg, tage):
    out = []
    for r in pg.execute("""SELECT id, venue, coin, strike, side, price, fee, p_model, won, pnl, created, t_resolve,
                                  settled, data FROM optionen_picks
                           WHERE created > now() - make_interval(days => %s) ORDER BY created""", (tage,)).fetchall():
        d = r[13] if isinstance(r[13], dict) else (json.loads(r[13]) if r[13] else {})
        mk, dp = d.get("mkt") or {}, d.get("depth") or {}
        out.append({"id": r[0], "venue": r[1], "coin": r[2], "K": _f(r[3]), "side": r[4], "price": _f(r[5]),
                    "fee": _f(r[6], 0.0), "p": _f(r[7]), "won": r[8], "pnl": _f(r[9]), "created": r[10],
                    "resolve": r[11], "settled": r[12], "mkt": mk, "depth": dp, "book": d.get("book"),
                    "path": d.get("path") or [], "book_err": d.get("book_err")})
    return out


def _summe(rows, stueck):
    fertig = [r for r in rows if r["settled"] is not None and r["pnl"] is not None]
    kosten = sum(stueck * (r["price"] + r["fee"]) for r in fertig)
    pnl = sum(r["pnl"] for r in fertig)
    return {"n": len(fertig), "pnl": round(pnl, 2), "cost": round(kosten, 2),
            "roi": round(pnl / kosten, 4) if kosten else None,
            "hit": round(sum(1 for r in fertig if r["won"]) / len(fertig), 4) if fertig else None}


def bericht(tage=30):
    import optionen
    pg = optionen._pg()
    if not pg:
        return {"error": "no database"}
    sh = optionen.SHARES
    rows = _zeilen(pg, tage)
    mit = [r for r in rows if r["depth"]]
    out = {"shares": sh, "days": tage, "picks": len(rows), "with_book": len(mit),
           "book_errors": sum(1 for r in rows if r["book_err"]),
           "note": "Depth is written from 2.82.28 on. Older picks have none and are left out of the depth rows."}
    if not mit:
        return out
    out["all_with_book"] = _summe(mit, sh)
    for c in STUFEN:
        k = f"plus{c}c"
        out[f"fillable_{k}"] = _summe([r for r in mit if (r["depth"].get(k) or 0) >= sh], sh)
        out[f"share_fillable_{k}"] = round(sum(1 for r in mit if (r["depth"].get(k) or 0) >= sh) / len(mit), 4)
    gr = sorted((r["depth"].get("plus0c") or 0) for r in mit)
    out["size_at_ask"] = {"median": gr[len(gr) // 2], "p10": gr[len(gr) // 10], "p90": gr[(len(gr) * 9) // 10]}
    # Wie lange bleibt die Luecke offen: Edge beim ersten Pfadeintrag danach
    nach = [r["path"][0] for r in rows if r["path"] and r["path"][0].get("edge") is not None]
    if nach:
        out["after_first_round"] = {"n": len(nach),
                                    "edge_still_positive": round(sum(1 for e in nach if e["edge"] > 0) / len(nach), 4),
                                    "avg_minutes": round(sum(e.get("m") or 0 for e in nach) / len(nach), 1)}
    return out


def csv_picks(tage=30):
    import optionen
    pg = optionen._pg()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["created_utc", "resolve_utc", "venue", "coin", "strike", "side", "price", "fee", "p_engine", "edge",
                "bid", "spread", "ask_size", "depth_at_ask", "depth_plus1c", "depth_plus2c", "depth_plus5c",
                "volume", "volume_24h", "open_interest", "last_price", "hours_left", "index", "settled", "won", "pnl",
                "book", "path"])
    for r in (_zeilen(pg, tage) if pg else []):
        mk, dp = r["mkt"], r["depth"]
        w.writerow([r["created"].isoformat() if r["created"] else "", r["resolve"].isoformat() if r["resolve"] else "",
                    r["venue"], r["coin"], r["K"], r["side"], r["price"], r["fee"], r["p"], mk.get("edge"),
                    mk.get("bid"), mk.get("spread"), mk.get("ask_size"), dp.get("plus0c"), dp.get("plus1c"),
                    dp.get("plus2c"), dp.get("plus5c"), mk.get("vol"), mk.get("vol24"), mk.get("oi"), mk.get("last"),
                    mk.get("tau_h"), mk.get("index"), int(r["settled"] is not None),
                    "" if r["won"] is None else int(r["won"]), "" if r["pnl"] is None else round(r["pnl"], 4),
                    json.dumps(r["book"]) if r["book"] else "", json.dumps(r["path"]) if r["path"] else ""])
    return buf.getvalue()


def csv_orders(tage=30):
    import optionen
    pg = optionen._pg()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["created_utc", "seat", "ticker", "side", "call_price", "limit_c", "wanted", "filled", "cost", "fees",
                "status", "error", "ms", "seconds_after_pick", "depth_at_call", "depth_at_limit", "won", "info"])
    if not pg:
        return buf.getvalue()
    try:
        rows = pg.execute("""SELECT a.created, a.tester, a.ticker, a.side, p.price, a.limit_c, a.wanted, a.filled, a.cost,
                                    a.fees, a.status, a.error, a.ms, EXTRACT(EPOCH FROM (a.created - p.created)), a.info,
                                    p.won FROM optionen_auto a LEFT JOIN (SELECT id, won, settled, coin, strike, price, t_resolve, venue, created FROM optionen_picks UNION ALL SELECT id, won, settled, coin, strike, book_price, t_resolve, 'kalshi', created FROM optionen_live) p ON p.id = a.pick_id
                             WHERE a.created > now() - make_interval(days => %s) ORDER BY a.created""", (tage,)).fetchall()
    except Exception as e:
        log.debug(f"optionen tiefe csv_orders: {e}")
        rows = []
    for r in rows:
        info = r[14] if isinstance(r[14], dict) else (json.loads(r[14]) if r[14] else {})
        # Sitze ohne Wallet im Klartext: nur eine laufende Kennung
        seat = "seat-" + hashlib.sha256(str(r[1]).encode()).hexdigest()[:6]
        w.writerow([r[0].isoformat() if r[0] else "", seat, r[2], r[3], _f(r[4]), r[5], r[6], r[7], _f(r[8]), _f(r[9]),
                    r[10], r[11] or "", r[12] if r[12] is not None else "", round(float(r[13]), 1) if r[13] is not None else "",
                    (info.get("depth") or {}).get("at_call", ""), (info.get("depth") or {}).get("at_limit", ""),
                    "" if r[15] is None else int(r[15]), json.dumps(info) if info else ""])
    return buf.getvalue()
