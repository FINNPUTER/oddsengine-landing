"""
optionen_calls.py: Above X Calls. Seit 2.82.13.

Aus den Above-X-Picks (optionen.py) gehen nur die staerksten als Call raus: Boerse OPT_CALLS_VENUES (Standard kalshi),
Vorsprung mindestens OPT_CALLS_EDGE (Standard 0.08), Preis zwischen OPT_CALLS_MIN_PRICE und OPT_CALLS_MAX_PRICE
(Standard 0.20 bis 0.80), hoechstens ein Call je Markt-Termin (Coin und Schlusszeit) und hoechstens OPT_CALLS_DAY am Tag.

Schalter "options_calls" an: der Call geht in den Kanal OPT_CALLS_CHAT (Standard TELEGRAM_PAID_CHANNEL_ID),
das Ergebnis als Antwort darauf, sobald der Markt abgerechnet ist. Schalter aus: die Calls werden nur still
mitgeschrieben (Admin: /v1/admin/options/calls). Die oeffentliche Bilanz (/v1/options/calls/record) zaehlt nur
Calls, die wirklich verschickt wurden, ab dem ersten verschickten Call.

Laeuft im Dienst crypto (aus optionen.tick), braucht dort TELEGRAM_BOT_TOKEN und den Kanal.
"""
import json
import os
import time
from datetime import datetime, timezone

import requests

from logger import log

VENUES = {v.strip() for v in os.getenv("OPT_CALLS_VENUES", "kalshi").lower().split(",") if v.strip()}
EDGE = float(os.getenv("OPT_CALLS_EDGE", "0.08"))
MIN_P = float(os.getenv("OPT_CALLS_MIN_PRICE", "0.20"))
MAX_P = float(os.getenv("OPT_CALLS_MAX_PRICE", "0.80"))
PRO_TAG = int(os.getenv("OPT_CALLS_DAY", "24"))
SKIP = {c.strip() for c in os.getenv("OPT_CALLS_SKIP", "").lower().split(",") if c.strip()}
SHARES = 10.0
NAMEN = {"btc": "BTC", "eth": "ETH"}
_SCHEMA = [False]


def an():
    try:
        import features
        return features.an("options_calls")
    except Exception:
        return False


def _schema(pg):
    if _SCHEMA[0]:
        return
    pg.execute("""CREATE TABLE IF NOT EXISTS optionen_calls (
        pick_id TEXT PRIMARY KEY, event TEXT, coin TEXT, venue TEXT, side TEXT, price NUMERIC, fair NUMERIC,
        strike NUMERIC, t_resolve TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now(),
        sent BOOLEAN DEFAULT FALSE, msg_id BIGINT, result_sent BOOLEAN DEFAULT FALSE)""")
    pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_calls_event ON optionen_calls(event)")
    _SCHEMA[0] = True


def _kanal():
    return (os.getenv("OPT_CALLS_CHAT", "") or os.getenv("TELEGRAM_PAID_CHANNEL_ID", "")).strip()


def _tg(text, url=None, reply_to=None):
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = _kanal()
    if not (tok and chat):
        log.warning("options calls: TELEGRAM_BOT_TOKEN or OPT_CALLS_CHAT / TELEGRAM_PAID_CHANNEL_ID missing in this service")
        return None
    d = {"chat_id": chat, "text": text.replace("\u2014", ",").replace("\u2013", ","), "disable_web_page_preview": True}
    if url:
        d["reply_markup"] = {"inline_keyboard": [[{"text": "Open on Kalshi" if "kalshi" in url else "Open market", "url": url}]]}
    if reply_to:
        d["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=d, timeout=15)
        if r.status_code != 200:
            log.warning(f"options calls telegram {r.status_code}: {r.text[:160]}")
            return None
        return r.json()["result"]["message_id"]
    except Exception as e:
        log.warning(f"options calls telegram: {e}")
        return None


def _strike(k):
    k = float(k)
    return f"${k:,.0f}" if abs(k - round(k)) < 1e-6 else f"${k:,.2f}"


def _text(c):
    schluss = datetime.fromtimestamp(c["T"], timezone.utc)
    rest_h = max(0.0, (c["T"] - time.time()) / 3600)
    rest = f"about {max(1, round(rest_h * 60))} min" if rest_h < 1.5 else f"about {round(rest_h)} h"
    venue = "Kalshi" if c["venue"] == "kalshi" else "Polymarket"
    return (f"Above X call · {venue}\n\n"
            f"Will {NAMEN.get(c['coin'], c['coin'].upper())} be above {_strike(c['K'])} at "
            f"{schluss.strftime('%b %d, %H:%M')} UTC?\n\n"
            f"Call: {c['side'].upper()} at {round(c['price'] * 100)}c\n"
            f"Engine fair price: {round(c['p'] * 100)}c\n"
            f"Closes in {rest}\n\n"
            f"Not financial advice. Past results are no guarantee of future results.")


def _passt(c):
    return (c["venue"] in VENUES and c["coin"] not in SKIP and (c.get("edge") or 0) >= EDGE
            and MIN_P <= float(c["price"]) <= MAX_P)


def runde(pg, neue, ergebnisse=True):
    """Aus optionen.tick: neue Picks pruefen und senden, danach Ergebnisse nachtragen."""
    if not pg:
        return
    _schema(pg)
    live = an()
    heute = pg.execute("SELECT count(*) FROM optionen_calls WHERE created > now() - interval '24 hours'").fetchone()[0]
    # je Termin nur der staerkste neue Pick dieser Runde
    best = {}
    for c in neue:
        if not _passt(c):
            continue
        key = f"{c['venue']}|{c['coin']}|{int(c['T'])}"
        if key not in best or c["edge"] > best[key]["edge"]:
            best[key] = c
    for key, c in sorted(best.items(), key=lambda kv: -kv[1]["edge"]):
        if heute >= PRO_TAG:
            break
        if pg.execute("SELECT 1 FROM optionen_calls WHERE event=%s", (key,)).fetchone():
            continue
        # Seit 2.82.14: erst fuer die Beta-Tester kaufen (eigene Kalshi-Konten), dann den Kanal informieren
        # Seit 2.82.44: mit OPT_LIVE_AUTO=1 kauft Auto mode die Live Calls (optionen_live), nicht diese hier
        if os.getenv("OPT_LIVE_AUTO", "0").strip() not in ("1", "true", "on"):
            try:
                import optionen_auto
                optionen_auto.kaufen(pg, c)
            except Exception as e:
                log.warning(f"options autotrade: {e}")
        msg = None
        if live:
            msg = _tg(_text(c), c.get("url"))
        pg.execute("""INSERT INTO optionen_calls (pick_id, event, coin, venue, side, price, fair, strike, t_resolve, sent, msg_id)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s,%s) ON CONFLICT DO NOTHING""",
                   (c["id"], key, c["coin"], c["venue"], c["side"], c["price"], round(c["p"], 4), c["K"], c["T"],
                    bool(msg), msg))
        heute += 1
        log.info(f"options call {key} {c['side']} at {c['price']} live={live} sent={bool(msg)}")
    if ergebnisse:
        _ergebnisse(pg)


def _ergebnisse(pg):
    rows = pg.execute("""SELECT c.pick_id, c.coin, c.side, c.price, c.strike, c.msg_id, p.won, p.pnl, p.price, p.fee
                         FROM optionen_calls c JOIN optionen_picks p ON p.id = c.pick_id
                         WHERE c.sent AND NOT c.result_sent AND p.settled IS NOT NULL LIMIT 20""").fetchall()
    for pid, coin, side, preis, strike, msg, won, pnl, p_preis, fee in rows:
        name = f"{NAMEN.get(coin, coin.upper())} above {_strike(strike)}"
        kosten = SHARES * (float(p_preis) + float(fee or 0))
        if won:
            roi = float(pnl) / kosten * 100 if kosten else 0
            txt = f"Hit. {name}, {side.upper()} at {round(float(preis) * 100)}c paid $1.00 per contract ({roi:+.0f}%)."
        else:
            txt = f"Miss. {name}, {side.upper()} at {round(float(preis) * 100)}c."
        _tg(txt, reply_to=msg)
        pg.execute("UPDATE optionen_calls SET result_sent=TRUE WHERE pick_id=%s", (pid,))
    try:
        import optionen_auto
        optionen_auto.ergebnisse(pg)
    except Exception as e:
        log.warning(f"options autotrade results: {e}")


def _zeilen(nur_gesendet):
    import optionen
    pg = optionen._pg()
    if not pg:
        return []
    _schema(pg)
    rows = pg.execute(f"""SELECT c.created, c.coin, c.venue, c.side, c.price, c.fair, c.strike, c.t_resolve, c.sent,
                                 p.won, p.pnl, p.price, p.fee, p.settled
                          FROM optionen_calls c JOIN optionen_picks p ON p.id = c.pick_id
                          {"WHERE c.sent" if nur_gesendet else ""} ORDER BY c.created""").fetchall()
    return [{"created": r[0], "coin": r[1], "venue": r[2], "side": r[3], "price": float(r[4]), "fair": float(r[5] or 0),
             "strike": float(r[6]), "resolves": r[7], "sent": bool(r[8]), "won": r[9], "pnl": float(r[10] or 0),
             "cost": SHARES * (float(r[11]) + float(r[12] or 0)), "settled": r[13] is not None} for r in rows]


def _summe(rows):
    fertig = [r for r in rows if r["settled"]]
    kosten = sum(r["cost"] for r in fertig)
    pnl = sum(r["pnl"] for r in fertig)
    tage = {}
    for r in fertig:
        d = r["resolves"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        tage[d] = tage.get(d, 0) + r["pnl"]
    return {"calls": len(rows), "settled": len(fertig), "open": len(rows) - len(fertig),
            "hits": sum(1 for r in fertig if r["won"]), "return_pct": round(pnl / kosten * 100, 1) if kosten else None,
            "days": len(tage), "green_days": sum(1 for v in tage.values() if v > 0),
            "since": rows[0]["created"].isoformat() if rows else None}


def _liste(rows, n=30):
    return [{"time": r["created"].isoformat(), "coin": NAMEN.get(r["coin"], r["coin"]), "venue": r["venue"],
             "question": f"above {_strike(r['strike'])} at {r['resolves'].astimezone(timezone.utc).strftime('%b %d, %H:%M')} UTC",
             "call": f"{r['side'].upper()} at {round(r['price'] * 100)}c",
             "result": None if not r["settled"] else ("hit" if r["won"] else "miss")}
            for r in reversed(rows[-n:])]


def bilanz():
    """Oeffentlich: nur verschickte Calls."""
    rows = _zeilen(True)
    return {"rule": "Every call that went to the channel, scored at the price in the call. 10 contracts each, fees included.",
            "record": _summe(rows), "recent": _liste(rows)}


def bericht():
    """Admin: verschickte und still mitgeschriebene Calls getrennt."""
    alle = _zeilen(False)
    return {"live": an(), "channel_set": bool(_kanal()),
            "filter": {"venues": sorted(VENUES), "min_edge": EDGE, "price": [MIN_P, MAX_P], "per_day": PRO_TAG,
                       "skip": sorted(SKIP)},
            "sent": _summe([r for r in alle if r["sent"]]), "shadow": _summe([r for r in alle if not r["sent"]]),
            "recent": _liste(alle, 40)}


# ══ "While you slept" (seit 2.82.87): jeden Morgen die Nacht als fertiger Post ══
# Um OPT_NIGHT_HOUR (UTC, Standard 7) einmal am Tag: alle Picks, die in den letzten 8 Stunden abgerechnet wurden
# (dieselbe Bilanz wie der Record: Listenpreis, 10 Kontrakte), plus die Serie gruener Tage bis gestern.
# Geht als Entwurf an den Owner (OPT_NIGHT_CHAT oder die Telegram-Chats der OWNER_WALLETS). Mit OPT_NIGHT_PUBLIC=1
# zusaetzlich in TELEGRAM_PUBLIC_CHANNEL_ID. Rote Naechte werden genauso gemeldet.
NACHT_H = int(os.getenv("OPT_NIGHT_HOUR", "7"))
LINK = "https://t.me/OddsEnginePublicBot"


def _geld(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def nacht_text(pg):
    r = pg.execute("""SELECT count(*), sum(CASE WHEN won THEN 1 ELSE 0 END), COALESCE(sum(pnl), 0) FROM optionen_picks
                      WHERE settled IS NOT NULL AND t_resolve > now() - interval '8 hours' AND t_resolve <= now()""").fetchone()
    n, w, pnl = int(r[0] or 0), int(r[1] or 0), float(r[2] or 0)
    if not n:
        return None
    tage = pg.execute("""SELECT to_char(t_resolve AT TIME ZONE 'UTC', 'YYYY-MM-DD') d, sum(pnl) FROM optionen_picks
                         WHERE settled IS NOT NULL AND t_resolve < date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                           AND t_resolve > now() - interval '120 days'
                         GROUP BY 1 ORDER BY 1 DESC""").fetchall()
    serie, summe = 0, 0.0
    for _, v in tage:
        if float(v or 0) > 0:
            serie += 1
            summe += float(v)
        else:
            break
    # Seit 2.82.89: die Trefferquote steht nur drin, wenn sie etwas sagt (viele billige Picks druecken sie, obwohl im Plus)
    z = ["While you slept:", "", f"{n} calls settled" + (f", {w} won." if n and w / n >= 0.4 else "."),
         f"{_geld(pnl)} for the engine." if pnl >= 0 else f"{_geld(pnl)}. Not every night is green."]
    if serie >= 3:
        z += ["", f"{serie} green days in a row before tonight. {_geld(summe)} in that run."]
    z += ["", "It doesn't sleep. You can.", "", f"🟢 {LINK}", "", "$ODDS", "", "Past results are no guarantee of future results."]
    return {"text": "\n".join(z), "n": n, "won": w, "pnl": round(pnl, 2), "streak": serie, "streak_usd": round(summe, 2)}


def _owner_chats():
    chats = [c.strip() for c in os.getenv("OPT_NIGHT_CHAT", "").split(",") if c.strip()]
    if chats:
        return chats
    try:
        import agent
        owner = {w.strip().lower() for w in os.getenv("OWNER_WALLETS", "").split(",") if w.strip()}
        return [str(u["tg"]) for u in agent.alle() if str(u.get("wallet") or "").lower() in owner and u.get("tg")]
    except Exception as e:
        log.debug(f"night recap owner: {e}")
        return []


def _senden(chat, text):
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not (tok and chat):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        log.debug(f"night recap send: {e}")
        return False


def nacht(pg, zwingen=False):
    """Einmal am Tag zur Stunde NACHT_H. zwingen=True schickt sofort (Admin-Test), ohne den Tag zu verbrauchen."""
    import storage
    jetzt = datetime.now(timezone.utc)
    tag = jetzt.strftime("%Y-%m-%d")
    if not zwingen and (jetzt.hour != NACHT_H or storage.kv_get("night_recap_day") == tag):
        return None
    t = nacht_text(pg)
    if not zwingen:
        storage.kv_set("night_recap_day", tag)
    if not t:
        return None
    kopf = ("Night recap, ready to post (numbers from the record):" if t["pnl"] >= 25 else
            "Night recap. Small or red night, better skip posting this one (numbers from the record):")
    gesendet = [c for c in _owner_chats() if _senden(c, kopf + "\n\n" + t["text"])]
    if os.getenv("OPT_NIGHT_PUBLIC", "0").strip().lower() in ("1", "true", "on", "yes") and not zwingen and t["pnl"] >= 25:
        ch = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID", "").strip()
        if ch and _senden(ch, t["text"]):
            gesendet.append("public channel")
    t["sent_to"] = len(gesendet)
    t["ts"] = int(time.time())
    try:
        import json
        storage.kv_set("night_recap_last", json.dumps(t))
    except Exception:
        pass
    return t


# ══ Lab-Stand fuer den Owner (seit 2.82.94): alles Interne in einer Nachricht, per /lab, zu festen Stunden ══
# und als Werkzeug fuer den Chat ("engine_lab"), damit die Engine auf jede Frage dazu die echten Zahlen hat.
LAB_STUNDEN = [int(x) for x in os.getenv("OPT_LAB_HOURS", "7,13,19").split(",") if x.strip().isdigit()]


def lab_daten():
    out = {}
    try:
        import optionen_bilanz
        r = optionen_bilanz.bericht()
        d = r.get("by_day") or []
        serie = 0
        for x in reversed(d[:-1]):
            if x["pnl"] > 0:
                serie += 1
            else:
                break
        out["record"] = {"total_usd": round(r["all"]["pnl"], 2), "picks": r["picks"], "roi_pct": round(r["all"]["roi"] * 100, 2),
                         "today_usd": round(d[-1]["pnl"], 2) if d else None, "today_picks": d[-1]["n"] if d else None,
                         "yesterday_usd": round(d[-2]["pnl"], 2) if len(d) > 1 else None,
                         "green_days_in_row_before_today": serie, "days": r.get("days")}
    except Exception as e:
        out["record_error"] = str(e)[:120]
    try:
        b = bericht()
        out["channel_calls"] = {k: b.get("shadow", {}).get(k) for k in ("settled", "hits", "return_pct", "days", "green_days", "open")}
        out["channel_calls"]["switch_on"] = b.get("live")
        out["channel_calls"]["last"] = [{"time": x["time"][:16], "coin": x["coin"], "call": x["call"],
                                         "market": x["question"], "result": x["result"]} for x in (b.get("recent") or [])[:6]]
    except Exception as e:
        out["calls_error"] = str(e)[:120]
    try:
        import optionen_auto
        a = optionen_auto.bericht()
        out["auto_mode"] = {"accounts": [{"who": t.get("name"), "balance_usd": t.get("balance_usd"), "since_start_usd": t.get("pnl"),
                                          "since_channel_calls": t.get("since_channel")} for t in a.get("testers") or []],
                            "price_vs_call": a.get("fill_gap"), "capacity": a.get("capacity"),
                            "open": (a.get("orders_summary") or {}).get("open")}
    except Exception as e:
        out["auto_error"] = str(e)[:120]
    try:
        import optionen_spread
        s = optionen_spread.sim_bericht()
        out["spread_simulation"] = {k: s.get(k) for k in ("hours", "trades", "profit_usd", "per_day_usd", "per_day_pct",
                                                          "transfers", "transfer_cost_usd", "capital_usd", "coins", "exchanges")}
        import storage
        f = json.loads(storage.kv_get("optionen_finder") or "null") or {}
        out["coin_finder"] = {"hours": f.get("hours"), "top": [{k: c.get(k) for k in ("coin", "share_pct", "median_net_pct",
                                                                                     "main_pair", "suspicious", "min_volume_24h")}
                                                              for c in (f.get("coins") or [])[:8]]}
        gp = optionen_spread.gap_bericht()      # seit 2.82.101: echte Episoden je Coin
        out["gap_episodes"] = {"hours": gp.get("hours"), "total": gp.get("total"),
                               "coins": [{k: c.get(k) for k in ("coin", "confirmed_per_day", "traded_per_day", "median_seconds",
                                                                "median_net_pct", "median_depth_usd", "net_usd", "net_per_day_usd",
                                                                "missed_no_stock", "transfers", "route", "main_pair", "open_now")}
                                         for c in (gp.get("coins") or [])[:10]]}
        sp = optionen_spread.bericht()
        xv = sp.get("xvenue") or {}
        out["kalshi_vs_polymarket"] = {k: xv.get(k) for k in ("found", "avg_net_c", "max_net_c", "settled", "real_avg_c", "both_lost")}
        out["funding_best"] = [{k: x.get(k) for k in ("ex", "coin", "apr_pct", "net_30d_pct")} for x in (sp.get("funding") or [])[:3]]
    except Exception as e:
        out["spread_error"] = str(e)[:120]
    try:
        import optionen_fills
        lk = optionen_fills.lock_bericht()
        h = lk.get("hold") or {}
        best = max(lk.get("levels") or [{}], key=lambda x: x.get("vs_hold", -1e9))
        out["lock_test"] = {"hold_roi": h.get("roi"), "best_level_c": best.get("level_c"), "best_vs_hold_usd": best.get("vs_hold"),
                            "verdict": "holding is better" if (best.get("vs_hold") or 0) <= 0 else "locking would have made more"}
    except Exception as e:
        out["lock_error"] = str(e)[:120]
    return out


def _g2(v):
    return "n/a" if v is None else (f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}")


def lab_text():
    d = lab_daten()
    z = ["Engine lab"]
    r = d.get("record") or {}
    if r:
        z += ["", f"Record: today {_g2(r.get('today_usd'))} ({r.get('today_picks')} picks), yesterday {_g2(r.get('yesterday_usd'))}",
              f"Since day one {_g2(r.get('total_usd'))} on {r.get('picks'):,} picks, ROI {r.get('roi_pct')}%",
              f"Green days in a row before today: {r.get('green_days_in_row_before_today')}"]
    c = d.get("channel_calls") or {}
    if c:
        z += ["", f"Channel calls: {c.get('hits')} of {c.get('settled')} hit, {c.get('return_pct')}%, {c.get('green_days')} of {c.get('days')} days green"
              + ("" if c.get("switch_on") else " (not posted, Auto mode only)")]
        for x in (c.get("last") or [])[:3]:
            z.append(f"  {x['coin']} {x['call']}: {x['result'] or 'open'}")
    a = d.get("auto_mode") or {}
    for t in a.get("accounts") or []:
        sk = t.get("since_channel_calls") or {}
        w = str(t.get("who") or "")
        w = (w[:8] + "…" + w[-4:]) if len(w) > 16 else w
        bal = t.get("balance_usd")
        z += ["", f"Auto mode {w}: since channel calls {sk.get('won')} of {sk.get('n')} won, {_g2(sk.get('pnl'))}, "
                  f"since start {_g2(t.get('since_start_usd'))}, balance " + (f"${bal:,.2f}" if bal is not None else "n/a")]
    fg = a.get("price_vs_call") or {}
    if fg.get("avg_gap_c") is not None:
        z.append(f"Paid vs call: {fg['avg_gap_c']:+.1f}c per contract")
    s = d.get("spread_simulation") or {}
    if s.get("trades") is not None:
        z += ["", f"Spread simulation ({', '.join(c_.upper() for c_ in s.get('coins') or [])}, ${s.get('capital_usd'):,.0f} capital): "
                  f"{s.get('trades')} trades in {s.get('hours')} h, {_g2(s.get('profit_usd'))} after all costs"]
    f = d.get("coin_finder") or {}
    if f.get("top"):
        z += ["", f"Coin finder ({f.get('hours')} h): " + ", ".join(f"{x['coin']} {x['share_pct']}% +{x['median_net_pct']}%" for x in f["top"][:5])]
    g = d.get("gap_episodes") or {}
    if g.get("coins"):
        t = g.get("total") or {}
        z += ["", f"Gap episodes ({g.get('hours')} h, one trade per gap, stock and transfers counted): "
                  f"{t.get('traded')} trades, net {_g2(t.get('net_usd'))}"]
        for c_ in g["coins"][:5]:
            z.append(f"  {c_['coin']}: {c_['confirmed_per_day']} gaps/day, {c_['traded_per_day']} traded/day, "
                     + (f"+{c_['median_net_pct']}% median, " if c_.get('median_net_pct') is not None else "") + f"net {_g2(c_['net_usd'])}, route {c_['route']}")
    x = d.get("kalshi_vs_polymarket") or {}
    if x:
        z.append(f"Kalshi vs Polymarket: {x.get('found') or 0} found, {x.get('settled') or 0} settled")
    lk = d.get("lock_test") or {}
    if lk:
        z.append(f"Lock test: {lk.get('verdict')}")
    return "\n".join(z)


def lab_runde():
    """Zu den Stunden in OPT_LAB_HOURS einmal den Lab-Stand an den Owner."""
    import storage
    jetzt = datetime.now(timezone.utc)
    if jetzt.hour not in LAB_STUNDEN or jetzt.minute > 10:
        return
    key = jetzt.strftime("%Y-%m-%d %H")
    if storage.kv_get("lab_sent") == key:
        return
    storage.kv_set("lab_sent", key)
    t = lab_text()
    for c in _owner_chats():
        _senden(c, t)
