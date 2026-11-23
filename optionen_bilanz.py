"""
optionen_bilanz.py: die komplette Bilanz von "Above X" (optionen.py) seit dem ersten Pick. Seit 2.82.11.

Die Admin-Seite rechnet nur die letzten 2.000 Picks. Hier wird alles gelesen, was in optionen_picks abgerechnet ist,
und aufgeteilt nach Tag, Woche, Boerse, Coin, Seite, Vorsprung (Optionen minus Preis), Preis und Restlaufzeit.
Dazu die Kalibrierung (optionen_cal) ueber die ganze Zeit. Nur Admin: /v1/admin/options/record, als CSV mit format=csv.
"""
import csv
import io
import time
from datetime import timezone

SHARES = 10.0


def _rows():
    import optionen
    pg = optionen._pg()
    if not pg:
        return [], []
    try:
        sh = float(getattr(optionen, "SHARES", SHARES))
    except Exception:
        sh = SHARES
    picks = pg.execute("""SELECT venue, coin, strike, side, price, fee, p_model, won, pnl, t_resolve, created, settled, iv, fwd
                          FROM optionen_picks WHERE settled IS NOT NULL ORDER BY t_resolve""").fetchall()
    out = []
    for r in picks:
        preis, fee = float(r[4] or 0), float(r[5] or 0)
        tr, cr = r[9], r[10]
        out.append({"venue": r[0], "coin": r[1], "K": float(r[2] or 0), "side": r[3], "price": preis, "fee": fee,
                    "p": float(r[6] or 0), "won": bool(r[7]), "pnl": float(r[8] or 0), "cost": sh * (preis + fee),
                    "resolve": tr, "created": cr, "iv": float(r[12]) if r[12] is not None else None,
                    "fwd": float(r[13]) if r[13] is not None else None,
                    "hours_left": round((tr - cr).total_seconds() / 3600, 1) if tr and cr else None})
    cal = pg.execute("""SELECT venue, coin, chk, p_model, p_market, yes FROM optionen_cal
                        WHERE settled IS NOT NULL AND yes IS NOT NULL""").fetchall()
    return out, [{"venue": c[0], "coin": c[1], "chk": float(c[2] or 0), "pm": float(c[3] or 0),
                  "pk": float(c[4] or 0), "yes": bool(c[5])} for c in cal]


def _sum(rows):
    n = len(rows)
    if not n:
        return None
    cost = sum(r["cost"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    return {"n": n, "wins": sum(1 for r in rows if r["won"]), "win_rate": round(sum(1 for r in rows if r["won"]) / n, 4),
            "cost": round(cost, 2), "pnl": round(pnl, 2), "roi": round(pnl / cost, 4) if cost else None,
            "avg_price": round(sum(r["price"] for r in rows) / n, 3)}


def _gruppe(rows, key):
    g = {}
    for r in rows:
        g.setdefault(key(r), []).append(r)
    return {k: _sum(v) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}


def _bucket(x, grenzen):
    for lo, hi in zip(grenzen, grenzen[1:]):
        if lo <= x < hi:
            return f"{lo:g}-{hi:g}"
    return f">={grenzen[-1]:g}"


def bericht():
    rows, cal = _rows()
    if not rows:
        return {"picks": 0}
    tag = lambda r: r["resolve"].astimezone(timezone.utc).strftime("%Y-%m-%d") if r["resolve"] else "?"
    woche = lambda r: r["resolve"].astimezone(timezone.utc).strftime("%G-W%V") if r["resolve"] else "?"
    tage = _gruppe(rows, tag)
    gruen = sum(1 for v in tage.values() if v and v["pnl"] > 0)
    rot = sum(1 for v in tage.values() if v and v["pnl"] < 0)
    # Verlauf und groesster Rueckgang (nach Abrechnungstag)
    kum, hoch, dd, verlauf = 0.0, 0.0, 0.0, []
    for d, v in tage.items():
        kum += v["pnl"]
        hoch = max(hoch, kum)
        dd = min(dd, kum - hoch)
        verlauf.append({"day": d, "pnl": v["pnl"], "cum": round(kum, 2), "n": v["n"]})
    # Kalibrierung ueber die ganze Zeit
    kal = {}
    for c in cal:
        k = f"{c['chk']:g}h"
        a = kal.setdefault(k, {"n": 0, "options": 0.0, "market": 0.0})
        y = 1.0 if c["yes"] else 0.0
        a["n"] += 1
        a["options"] += (c["pm"] - y) ** 2
        a["market"] += (c["pk"] - y) ** 2
    for a in kal.values():
        a["options"] = round(a["options"] / a["n"], 4)
        a["market"] = round(a["market"] / a["n"], 4)
    erst, letzt = rows[0]["resolve"], rows[-1]["resolve"]
    return {
        "picks": len(rows), "from": erst.isoformat() if erst else None, "to": letzt.isoformat() if letzt else None,
        "all": _sum(rows),
        "days": {"count": len(tage), "green": gruen, "red": rot, "max_drawdown": round(dd, 2)},
        "by_venue": _gruppe(rows, lambda r: r["venue"]),
        "by_coin": _gruppe(rows, lambda r: r["coin"]),
        "by_venue_coin": _gruppe(rows, lambda r: f"{r['venue']} {r['coin']}"),
        "by_side": _gruppe(rows, lambda r: r["side"]),
        "by_edge": _gruppe(rows, lambda r: _bucket(r["p"] - r["price"], [0.05, 0.08, 0.12, 0.2, 0.3])),
        "by_price": _gruppe(rows, lambda r: _bucket(r["price"], [0, 0.2, 0.4, 0.6, 0.8, 1.0])),
        "by_hours_left": _gruppe(rows, lambda r: _bucket(r["hours_left"] or 0, [0, 1, 6, 24, 48, 96])),
        "by_week": _gruppe(rows, woche),
        "by_day": verlauf,
        "calibration": kal,
    }


def csv_text():
    rows, _ = _rows()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["resolve_utc", "venue", "coin", "strike", "side", "price", "fee", "p_options", "won", "pnl", "cost",
                "hours_left", "created_utc", "iv", "fwd"])
    for r in rows:
        w.writerow([r["resolve"].isoformat() if r["resolve"] else "", r["venue"], r["coin"], r["K"], r["side"],
                    r["price"], r["fee"], r["p"], int(r["won"]), round(r["pnl"], 4), round(r["cost"], 4), r["hours_left"],
                    r["created"].isoformat() if r["created"] else "", r["iv"] if r["iv"] is not None else "",
                    r["fwd"] if r["fwd"] is not None else ""])
    return buf.getvalue()


def kapital():
    """Startgeld und Stand heute fuer die oeffentliche Bilanz (seit 2.82.31).

    Gerechnet wie ein Konto: jeder Pick kostet beim Entstehen seinen Einsatz samt Gebuehr, jeder Treffer zahlt beim
    Marktschluss aus. Der tiefste Kontostand auf diesem Weg ist das Geld, das mindestens noetig war ("needed").
    Das Startgeld ist OPT_START_CAPITAL (Standard 700). Reicht das nicht mehr, steigt es auf den naechsten vollen
    Hunderter ueber "needed", damit die Angabe nie kleiner ist als das, was wirklich gebraucht wurde."""
    import math
    import os
    import optionen
    pg = optionen._pg()
    if not pg:
        return None
    sh = optionen.SHARES
    r = pg.execute("""
        WITH ev AS (
          SELECT created AS ts, -(%s * (price + COALESCE(fee, 0))) AS d FROM optionen_picks
          UNION ALL
          SELECT COALESCE(t_resolve, settled), pnl + %s * (price + COALESCE(fee, 0)) FROM optionen_picks
          WHERE settled IS NOT NULL AND pnl IS NOT NULL)
        SELECT COALESCE(min(s), 0) FROM (SELECT sum(d) OVER (ORDER BY ts, d) AS s FROM ev) x""", (sh, sh)).fetchone()
    noetig = round(max(0.0, -float(r[0] or 0)), 2)
    pnl = float(pg.execute("SELECT COALESCE(sum(pnl), 0) FROM optionen_picks WHERE settled IS NOT NULL").fetchone()[0])
    try:
        start = float(os.getenv("OPT_START_CAPITAL", "700"))
    except ValueError:
        start = 700.0
    if noetig > start:
        start = float(math.ceil(noetig / 100.0) * 100)
    return {"start": round(start, 2), "now": round(start + pnl, 2), "needed": noetig,
            "return": round(pnl / start, 4) if start else None}

