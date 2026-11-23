"""Above X live (seit 2.82.43): derselbe Gedanke wie Above X, aber schnell und zum echten Buchpreis.

Warum: Die normale Runde schaut alle 5 Minuten auf die Kalshi-Liste. Die Pruefung gegen echte Trades
(optionen_fills) hat gezeigt: zum echten Preis bleibt davon kaum etwas, weil der Preis beim Call oft schon weg ist.

Was hier passiert, alle OPT_LIVE_TICK Sekunden (Standard 3):
  1. Kurs von BTC und ETH frisch von Deribit (Index), die Optionsflaeche alle 2 Minuten neu.
  2. Alle offenen "above"-Maerkte von Kalshi lesen, fuer jeden die Wahrscheinlichkeit zum Kurs von JETZT.
  3. Sieht ein Markt in der Liste billig genug aus, sofort das echte Orderbuch holen und den Preis fuer
     10 Kontrakte daraus rechnen (das, was eine Order jetzt wirklich zahlen wuerde).
  4. Nur wenn auch dieser echte Preis noch Vorsprung hat: ein "Live Call", mit Buchpreis, Zeitstempel und Latenz.
Gleiche Regeln wie der Call-Kanal (Vorsprung, Preisband, ein Call je Coin und Termin, Tagesgrenze).
Abgerechnet wird nach dem offiziellen Kalshi-Ergebnis. Ohne OPT_LIVE_AUTO=1 wird nichts gekauft, dann misst es nur. Mit OPT_LIVE_AUTO=1 kauft Auto mode genau diese Calls.
Laeuft im Dienst crypto. Aus: OPT_LIVE=0. Admin: /v1/admin/options/live.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

from logger import log

TAKT = float(os.getenv("OPT_LIVE_TICK", "3"))
FLAECHE_S = float(os.getenv("OPT_LIVE_SURFACE_S", "120"))
STUECK = 10
_ST = {"rounds": 0, "candidates": 0, "book_checked": 0, "book_ok": 0, "calls": 0, "errors": 0,
       "list_said_yes": 0, "list_yes_book_yes": 0, "book_only": 0, "gap_median_c": None,
       "last_round": 0, "last_error": None, "round_ms": None, "started": None}
_FL = {}
PRE = float(os.getenv("OPT_LIVE_PRE", "0.02"))
_LUECKE = []   # Cent, die das Buch ueber dem Listenpreis lag (letzte 400 Abfragen)
LIVE_SLIP = int(os.getenv("OPT_LIVE_SLIP_CENTS", "3"))
_KUEHL = {}   # Markt -> Zeit der letzten Buchabfrage, damit ein Kandidat nicht jede Runde neu abgefragt wird
_SCHEMA = [False]
_BESTE = []   # (ts, Buch-Vorsprung in Cent, Markt, Seite, Buchpreis) der letzten Stunde


def an():
    return os.getenv("OPT_LIVE", "1").strip() not in ("0", "false", "off", "")


def auto_an():
    """Seit 2.82.44: OPT_LIVE_AUTO=1 laesst Auto mode die Live Calls kaufen statt der 5-Minuten-Calls."""
    return os.getenv("OPT_LIVE_AUTO", "0").strip() in ("1", "true", "on")


def _regeln():
    import optionen_calls as oc
    edge = os.getenv("OPT_LIVE_EDGE", "").strip()
    if edge:
        e = float(edge)
    else:
        # Seit 2.82.65: ohne Variable waehlt die Engine die Schwelle selbst (optionen_kalib.auto_regel)
        e = oc.EDGE
        try:
            import optionen_kalib as ok_
            if os.getenv("OPT_LIVE_CAL", "auto").strip().lower() == "auto":
                r = ok_.auto_regel()
                if r.get("cal") and r.get("edge"):
                    e = r["edge"]
        except Exception:
            pass
    return {"edge": e, "min_p": oc.MIN_P, "max_p": oc.MAX_P,
            "per_day": int(os.getenv("OPT_LIVE_DAY", str(oc.PRO_TAG)))}


def _schema(pg):
    if _SCHEMA[0]:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_live (
        id TEXT PRIMARY KEY, coin TEXT, event TEXT, market TEXT, strike NUMERIC, side TEXT, t_resolve TIMESTAMPTZ,
        created TIMESTAMPTZ DEFAULT now(), list_price NUMERIC, book_price NUMERIC, n NUMERIC, p_model NUMERIC,
        edge_list NUMERIC, edge_book NUMERIC, spot NUMERIC, ms NUMERIC, tau_h NUMERIC,
        won BOOLEAN, pnl NUMERIC, settled TIMESTAMPTZ, data JSONB)""")
    pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_live_open ON optionen_live(settled)")
    _SCHEMA[0] = True


def _flaeche(sym):
    import optionen
    c = _FL.get(sym)
    if c and time.time() - c["_ts"] < FLAECHE_S:
        return c
    fl = optionen.flaeche(sym)
    fl["_ts"] = time.time()
    _FL[sym] = fl
    return fl


def _jetzt_flaeche(fl, spot):
    """Flaeche auf den Kurs von jetzt schieben: Index und alle Forwards im selben Verhaeltnis."""
    if not spot or not fl.get("index"):
        return fl
    f = spot / fl["index"]
    return {"index": spot, "exp": {t: {"F": e["F"] * f, "pts": e["pts"]} for t, e in fl["exp"].items()}}


def _spot(sym):
    import optionen
    return float(optionen._get(optionen.DERIBIT + "/get_index_price", index_name=sym.lower() + "_usd")["result"]["index_price"])


def _buchpreis(market, side):
    """Durchschnittspreis fuer STUECK Kontrakte aus dem echten Buch, oder None wenn nicht genug da ist."""
    import optionen
    import optionen_tiefe
    asks = optionen_tiefe.kaufseite(optionen_tiefe.buch_kalshi(market, lambda pf: optionen._get(optionen.KALSHI + pf)), side)
    brauch, kosten = STUECK, 0.0
    for p, q in asks:
        t = min(brauch, q)
        kosten += t * p
        brauch -= t
        if brauch <= 0:
            break
    if brauch > 0:
        return None, asks[:5]
    return round(kosten / STUECK, 4), asks[:5]


def _schon(pg, event, tag):
    if pg.execute("SELECT 1 FROM optionen_live WHERE event=%s", (event,)).fetchone():
        return True
    # Seit 2.82.66: BTC und ETH laufen fast immer zusammen. Zwei Calls auf denselben Schluss waeren dieselbe Wette
    # doppelt. Auf Wunsch standardmaessig AUS (beide duerfen kaufen), OPT_LIVE_ONE_PER_CLOSE=1 schaltet die Sperre ein.
    if os.getenv("OPT_LIVE_ONE_PER_CLOSE", "0").strip() in ("1", "on", "true"):
        schluss = event.rsplit("|", 1)[-1]
        if pg.execute("SELECT 1 FROM optionen_live WHERE event LIKE %s", (f"kalshi|%|{schluss}",)).fetchone():
            return True
    n = pg.execute("SELECT count(*) FROM optionen_live WHERE created > now() - interval '24 hours'").fetchone()[0]
    return n >= tag


def runde(pg):
    import optionen
    import optionen_kalib
    r = _regeln()
    km = optionen_kalib.modell(pg)
    try:
        ext = optionen_kalib.ext_aktiv(km)    # seit 2.82.68: Chart-Merkmale nur, wenn sie den Test gewonnen haben
    except Exception:
        ext = False
    budget = [int(os.getenv("OPT_LIVE_BOOKS", "12"))]   # hoechstens so viele Buchabfragen je Runde
    for coin, (sym, _, _) in optionen.COINS.items():
        fl0 = _flaeche(sym)
        spot = _spot(sym)
        fl = _jetzt_flaeche(fl0, spot)
        t_liste = time.time()
        maerkte = optionen.kalshi(coin)
        # Seit 2.82.54: erst alle Kandidaten sammeln, dann je Feld (Coin und Schlusszeit) den mit dem groessten
        # Vorsprung zuerst ins Buch pruefen. Vorher gewann die erste Schwelle in der Liste, nicht die beste.
        # Die Wahrscheinlichkeit ist kalibriert (optionen_kalib), wenn die Pruefung auf ungesehenen Daten besser war.
        kand = []
        for m in maerkte:
            w = optionen.wahrscheinlichkeit(fl, m["K"], m["T"], 1.0)
            if not w or not (optionen.MIN_TAU_H <= w["tau_h"] <= optionen.MAX_TAU_H):
                continue
            for side, ask in (("yes", m["yes_ask"]), ("no", m["no_ask"])):
                if ask is None or not (r["min_p"] <= ask <= r["max_p"]):
                    continue
                p_roh = w["p"] if side == "yes" else 1 - w["p"]
                feat = None
                if ext:
                    try:
                        import optionen_merkmale as om
                        feat = om.vektor(om.merkmale(pg, coin, time.time(), side, w.get("iv")))
                    except Exception:
                        feat = None
                p_side = optionen_kalib.p(p_roh, ask, w["tau_h"], km, feat)
                p_kal = optionen_kalib.p_immer(p_roh, ask, w["tau_h"], km)
                edge_l = p_side - ask - optionen.gebuehr(ask)
                kand.append((edge_l, m, w, side, ask, p_side, p_roh, p_kal))
        kand.sort(key=lambda x: -x[0])
        # Seit 2.82.67: Maker-Test auf Papier (eigene Tabelle, kauft nichts)
        try:
            import optionen_maker
            optionen_maker.merken(pg, coin, kand, r)
        except Exception:
            pass
        for edge_l, m, w, side, ask, p_side, p_roh, p_kal in kand:
            if True:
                # Seit 2.82.47: Die Liste von Kalshi hinkt dem Buch hinterher. Deshalb schon ab einem kleinen
                # Listen-Vorsprung (OPT_LIVE_PRE, Standard 2 Cent) ins echte Buch schauen. Entschieden wird nur am Buch.
                if edge_l < PRE:
                    continue
                _ST["candidates"] += 1
                event = f"kalshi|{coin}|{int(m['T'])}"
                if _schon(pg, event, r["per_day"]):
                    continue
                if time.time() - _KUEHL.get(m["market"] + side, 0) < 15 or budget[0] <= 0:
                    continue
                budget[0] -= 1
                _KUEHL[m["market"] + side] = time.time()
                t_b = time.time()
                try:
                    bp, buch = _buchpreis(m["market"], side)
                except Exception as e:
                    _ST["errors"] += 1
                    _ST["last_error_ts"] = int(time.time())
                    _ST["last_error"] = f"book {m['market']}: {str(e)[:80]}"
                    continue
                _ST["book_checked"] += 1
                ms = int((time.time() - t_liste) * 1000)
                if bp is None:
                    continue
                _LUECKE.append(round((bp - ask) * 100, 1))
                del _LUECKE[:-400]
                edge_b = p_side - bp - optionen.gebuehr(bp)
                # Seit 2.82.62: bester Buch-Vorsprung der letzten Stunde, damit man sieht, wie knapp es war
                jetzt_ = time.time()
                _BESTE.append((jetzt_, round(edge_b * 100, 1), m["market"], side, round(bp, 2)))
                while _BESTE and jetzt_ - _BESTE[0][0] > 3600:
                    _BESTE.pop(0)
                del _BESTE[:-3000]
                b_ = max(_BESTE, key=lambda x: x[1])
                _ST["best_book_edge_1h"] = {"edge_c": b_[1], "market": b_[2], "side": b_[3], "book": b_[4],
                                            "need_c": round(r["edge"] * 100, 1), "books": len(_BESTE)}
                if edge_l >= r["edge"]:
                    _ST["list_said_yes"] += 1
                    if edge_b >= r["edge"]:
                        _ST["list_yes_book_yes"] += 1
                elif edge_b >= r["edge"]:
                    _ST["book_only"] += 1
                if edge_b < r["edge"]:
                    continue
                _ST["book_ok"] += 1
                pid = f"{event}|{side}|{m['market']}"
                cur = pg.execute("""INSERT INTO optionen_live (id, coin, event, market, strike, side, t_resolve, list_price,
                                    book_price, n, p_model, edge_list, edge_book, spot, ms, tau_h, data)
                                    VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                    ON CONFLICT DO NOTHING""",
                                 (pid, coin, event, m["market"], m["K"], side, m["T"], ask, bp, STUECK, round(p_side, 4),
                                  round(edge_l, 4), round(edge_b, 4), spot, ms, round(w["tau_h"], 2),
                                  json.dumps({"book": buch, "book_ms": int((time.time() - t_b) * 1000),
                                              "iv": round(w["iv"], 4), "title": m.get("title"),
                                              "p_raw": round(p_roh, 4), "calibrated": p_roh != p_side,
                                              "p_cal": round(p_kal, 4) if p_kal is not None else None,
                                              "cal_would_buy": (p_kal - bp - optionen.gebuehr(bp) >= r["edge"]) if p_kal is not None else None})))
                if getattr(cur, "rowcount", 0) == 1:
                    _ST["calls"] += 1
                    _ST["last_call_ts"] = int(time.time())
                    log.info(f"options live call {m['market']} {side} book {bp} (list {ask}) edge {edge_b:.3f} {ms} ms")
                    if auto_an():
                        # Limit: Buchpreis plus ein paar Cent fuer die Sekunden bis zur Order, aber nie so hoch,
                        # dass vom Vorsprung weniger als die Haelfte bleibt
                        grenze = p_side - optionen.gebuehr(bp) - r["edge"] / 2
                        limit_c = max(int(round(bp * 100)), min(int(round(bp * 100)) + LIVE_SLIP, int(grenze * 100)))
                        try:
                            import optionen_auto
                            optionen_auto.kaufen(pg, {"id": pid, "venue": "kalshi", "coin": coin, "event": event,
                                                      "market": m["market"], "K": m["K"], "T": m["T"], "side": side,
                                                      "price": bp, "p": p_side, "edge": edge_b, "limit_c": min(99, limit_c)})
                        except Exception as e:
                            log.warning(f"options live autotrade: {e}")


def abrechnen(pg):
    import optionen
    for pid, market, side, bp, n in pg.execute(
            """SELECT id, market, side, book_price, n FROM optionen_live
               WHERE settled IS NULL AND t_resolve < now() - interval '2 minutes' LIMIT 60""").fetchall():
        ja = optionen._ergebnis("kalshi", market)
        if ja is None:
            continue
        won = ja if side == "yes" else not ja
        bp, n = float(bp), float(n)
        pnl = round(n * ((1.0 if won else 0.0) - bp - optionen.gebuehr(bp)), 4)
        pg.execute("UPDATE optionen_live SET won=%s, pnl=%s, settled=now() WHERE id=%s", (won, pnl, pid))


def schleife():
    """Eigener Faden im Dienst crypto."""
    if not an():
        log.info("options live: off (OPT_LIVE=0)")
        return
    import optionen
    _ST["started"] = int(time.time())
    try:
        import optionen_merkmale
        optionen_merkmale.faden_starten()      # seit 2.82.68: Kerzen fuer die Chart-Merkmale
    except Exception as e:
        log.warning(f"options candles: {e}")
    try:
        import optionen_kalib
        optionen_kalib.lern_faden_starten()    # seit 2.82.71: Kalibrierung lernt im eigenen Faden
    except Exception as e:
        log.warning(f"options calibration thread: {e}")
    try:
        import optionen_flow
        optionen_flow.faden_starten()          # seit 2.82.72: Orderbuch, Perps, Open Interest mitschreiben
    except Exception as e:
        log.warning(f"options flow: {e}")
    try:
        import optionen_kurs
        optionen_kurs.faden_starten()          # seit 2.82.78: Live-Kurs fuer die Admin-Seite
    except Exception as e:
        log.warning(f"options price: {e}")
    try:
        # Seit 2.82.99: laeuft im eigenen Dienst "spread", sobald OPT_SPREAD_HERE=0 im crypto-Dienst steht
        if os.getenv("OPT_SPREAD_HERE", "1").strip().lower() not in ("0", "false", "off", "no"):
            import optionen_spread
            optionen_spread.faden_starten()        # seit 2.82.85: Spread-Test zwischen Boersen, misst nur
    except Exception as e:
        log.warning(f"spread test: {e}")
    letzte_abr = 0.0
    while True:
        t0 = time.time()
        try:
            pg = optionen._pg()
            if pg:
                _schema(pg)
                runde(pg)
                try:
                    import optionen_live_neu     # seit 2.82.68: SOL und XRP live, nur auf Papier
                    optionen_live_neu.runde(pg)
                except Exception as e:
                    log.debug(f"options live new: {e}")
                if time.time() - letzte_abr > 60:
                    abrechnen(pg)
                    try:
                        import optionen_maker
                        optionen_maker.pruefen(pg)
                    except Exception:
                        pass
                    try:
                        import optionen_live_neu
                        optionen_live_neu.abrechnen(pg)
                    except Exception:
                        pass
                    try:
                        import optionen_calls     # seit 2.82.87: "While you slept" einmal am Morgen
                        optionen_calls.nacht(pg)
                        optionen_calls.lab_runde()        # seit 2.82.94: Lab-Stand an den Owner
                    except Exception as e:
                        log.debug(f"night recap: {e}")
                    letzte_abr = time.time()
            _ST["rounds"] += 1
            if _LUECKE:
                g = sorted(_LUECKE)
                _ST["gap_median_c"] = g[len(g) // 2]
            _ST["last_round"] = int(time.time())
            _ST["round_ms"] = int((time.time() - t0) * 1000)
        except Exception as e:
            _ST["errors"] += 1
            _ST["last_error"] = str(e)[:160]
            _ST["last_error_ts"] = int(time.time())
            log.warning(f"options live: {e}")
            time.sleep(5)
        try:
            if _ST["rounds"] % 20 == 0:
                import storage
                _ST["auto_live"] = auto_an()   # seit 2.82.73: der Dienst crypto sagt, welche Calls Auto mode kauft
                storage.kv_set("optionen_live_stand", json.dumps(_ST))
        except Exception:
            pass
        time.sleep(max(0.5, TAKT - (time.time() - t0)))


def bericht(tage=30):
    import optionen
    pg = optionen._pg()
    if not pg:
        return {"error": "no database"}
    _schema(pg)
    try:
        import storage
        stand = json.loads(storage.kv_get("optionen_live_stand") or "{}")
    except Exception:
        stand = {}
    out = {"status": stand, "rules": _regeln(), "tick_s": TAKT, "contracts": STUECK,
           "autotrade": bool(stand["auto_live"]) if "auto_live" in stand else auto_an(),
           "running": bool(stand.get("last_round")) and time.time() - float(stand.get("last_round") or 0) < 300}
    try:
        import optionen_kalib
        out["calibration"] = optionen_kalib.stand()
    except Exception as e:
        out["calibration"] = {"error": str(e)[:120]}
    rows = pg.execute("""SELECT created, coin, market, strike, side, list_price, book_price, n, p_model, edge_book, ms,
                                won, pnl, settled, tau_h, data
                         FROM optionen_live WHERE created > now() - make_interval(days => %s) ORDER BY created DESC""",
                      (tage,)).fetchall()

    def summe(zs):
        f = [z for z in zs if z[13] is not None]
        if not f:
            return {"n": 0, "open": len(zs)}
        pnl = sum(float(z[12]) for z in f)
        kosten = sum(float(z[7]) * (float(z[6]) + optionen.gebuehr(float(z[6]))) for z in f)
        return {"n": len(f), "open": len(zs) - len(f), "pnl": round(pnl, 2), "roi": round(pnl / kosten, 4) if kosten else None,
                "hit": round(sum(1 for z in f if z[11]) / len(f), 4)}
    out["total"] = summe(rows)
    tage_d = {}
    for z in rows:
        tage_d.setdefault(z[0].strftime("%Y-%m-%d"), []).append(z)
    out["by_day"] = [{"day": d, **summe(zs)} for d, zs in sorted(tage_d.items(), reverse=True)]
    ms = sorted(int(z[10]) for z in rows if z[10] is not None)
    out["median_ms"] = ms[len(ms) // 2] if ms else None
    if stand.get("book_checked"):
        ja = stand.get("list_said_yes") or 0
        out["list_vs_book"] = {"candidates": stand.get("candidates"), "book_checked": stand.get("book_checked"),
                               "list_said_edge": ja, "still_good_in_book": stand.get("list_yes_book_yes") or 0,
                               "share": round((stand.get("list_yes_book_yes") or 0) / ja, 4) if ja else None,
                               "edge_only_in_book": stand.get("book_only") or 0,
                               "book_above_list_median_c": stand.get("gap_median_c")}
    out["last_call_ts"] = int(rows[0][0].timestamp()) if rows else None
    out["recent"] = [{"ts": z[0].isoformat(), "coin": z[1], "market": z[2], "strike": float(z[3]), "side": z[4],
                      "list": float(z[5]), "book": float(z[6]), "p": float(z[8]), "edge": float(z[9]),
                      "ms": z[10], "won": z[11], "pnl": float(z[12]) if z[12] is not None else None,
                      "p_cal": _d(z).get("p_cal"), "cal_would_buy": _d(z).get("cal_would_buy")} for z in rows[:25]]
    # Seit 2.82.55: was die kalibrierte Zahl mit denselben Calls gemacht haette (nur Calls mit Schattenwert)
    mit = [z for z in rows if _d(z).get("cal_would_buy") is not None]
    out["calibration_shadow"] = {"would_buy": summe([z for z in mit if _d(z).get("cal_would_buy")]),
                                 "would_skip": summe([z for z in mit if not _d(z).get("cal_would_buy")])}
    return out


def _d(z):
    try:
        d = z[15]
        return d if isinstance(d, dict) else (json.loads(d) if d else {})
    except Exception:
        return {}
