"""
muster.py: Wallet-Gedaechtnis ueber alle Coins (seit 2.63).

Frage: Wo war diese Wallet schon ueberall drin, in welcher Rolle, und was hat sie dort getan?
Jeder Tiefen-Scan schreibt mit (Tabelle wl_seen), je Coin und Wallet:
  first_buyer   unter den ersten Kaeufern          bundle      gebuendelter Kauf im Launch
  top_holder    unter den groessten Haltern        dev         der Dev
  dev_side      hat Tokens direkt vom Dev bekommen side        hat Tokens von einem fruehen Kaeufer bekommen
  cluster       in einem Cluster (gleicher Geldgeber oder zusammen gekauft)
mit dem Anteil, den sie beim Scan noch hielt. Dazu kommen die aelteren Daten (iw_early seit 2.45, iw_fwd seit 2.46).

finden(chain, token, erg) liefert fuer einen neuen Scan das Muster:
  wie viele seiner Wallets wir schon aus anderen Coins kennen, aus welchen, was die dort getan haben,
  wie es diesen Coins danach ging (unter 20 % vom Hoch oder gelaufen) und ob dieselbe Gruppe mehrmals zusammen auftaucht.
Nur Chain-Fakten mit Beleg, keine Personen.

  merken(chain, token, erg, symbol)    aus intel.scannen
  finden(chain, token, erg)            -> dict fuer Karte und Modell
  wallet(chain, w)                     -> alle Auftritte einer Wallet
  runde()                              aus motor.tick: einmaliges Nachlesen der Torch-Coins der letzten Wochen
"""
import json
import os
import threading
import time

from logger import log

_SCHEMA = [False]
TOT = float(os.getenv("PATTERN_DEAD_SHARE", "0.2"))      # unter 20 % vom Hoch = abgestuerzt
GELAUFEN = float(os.getenv("PATTERN_RAN_X", "3"))
NACHLESEN_TAGE = int(os.getenv("PATTERN_BACKFILL_DAYS", "21"))
NACHLESEN_MAX = int(os.getenv("PATTERN_BACKFILL_MAX", "160"))
NACHLESEN_SYMBOLE = [s.strip() for s in os.getenv("PATTERN_BACKFILL_SYMBOLS", "SIL,ANTI,LOBIN").split(",") if s.strip()]
NAME = {"first_buyer": "first buyer", "bundle": "bundled buyer", "top_holder": "top holder", "dev": "dev",
        "dev_side": "got tokens from the dev", "dev_funder": "funded the dev", "side": "got tokens from an early buyer", "cluster": "in a cluster"}


def _q(sql, args=(), eins=False):
    import intel
    if not _SCHEMA[0]:
        pg = intel._pg()
        if not pg:
            return None if eins else []
        try:
            with intel._LOCK:
                pg.execute("""CREATE TABLE IF NOT EXISTS wl_seen (
                    chain TEXT NOT NULL, token TEXT NOT NULL, wallet TEXT NOT NULL, role TEXT NOT NULL,
                    pct DOUBLE PRECISION, held_pct DOUBLE PRECISION, ts BIGINT,
                    PRIMARY KEY (chain, token, wallet, role))""")
                pg.execute("CREATE INDEX IF NOT EXISTS ix_wl_seen_wallet ON wl_seen(chain, wallet)")
                pg.execute("ALTER TABLE iw_tokens ADD COLUMN IF NOT EXISTS last_mcap DOUBLE PRECISION")
                pg.execute("ALTER TABLE wl_seen ADD COLUMN IF NOT EXISTS buy_ts BIGINT")
                pg.execute("ALTER TABLE wl_seen ADD COLUMN IF NOT EXISTS exit_ts BIGINT")
            _SCHEMA[0] = True
        except Exception as e:
            log.debug(f"muster schema: {e}")
            return None if eins else []
    return intel._q(sql, args, eins)


def _k(chain, a):
    return a if chain in ("solana", "tron", "ton") else str(a or "").lower()


def _kurz(a):
    a = str(a)
    return a[:6] + "…" + a[-4:] if a.startswith("0x") else a[:4] + "…" + a[-4:]


# ── Mitschreiben ─────────────────────────────────────────────────────────

def _rollen(chain, erg):
    """[(wallet, role, pct_beim_kauf_oder_erhalt, held_pct)] aus einem Tiefen-Scan."""
    out = []
    dev = _k(chain, erg.get("dev") or "")
    halter = {_k(chain, x.get("address")): x.get("pct") for x in erg.get("holders_map") or []}
    if dev:
        out.append((dev, "dev", None, halter.get(dev, erg.get("dev_holds_pct"))))
    for b in erg.get("buyers") or []:
        w = _k(chain, b.get("wallet"))
        if not w or w == dev:
            continue
        out.append((w, "bundle" if b.get("bundled") else "first_buyer", None, b.get("pct")))
    for x in (erg.get("holders_map") or [])[:20]:
        w = _k(chain, x.get("address"))
        if not w or x.get("pool") or x.get("locked") or w == dev:
            continue
        out.append((w, "top_holder", x.get("pct"), x.get("pct")))
    for f in ((erg.get("forwards") or {}).get("list") or []):
        w = _k(chain, f.get("to"))
        if not w:
            continue
        out.append((w, "dev_side" if f.get("src") == "dev" else "side", f.get("pct"), f.get("holds_pct")))
    for c in erg.get("cluster_map") or []:
        for m in (c.get("members") or [])[:30]:
            out.append((_k(chain, m), "cluster", None, halter.get(_k(chain, m))))
    return out


def merken(chain, token, erg, symbol=None):
    if not erg.get("measured"):
        return
    tk, jetzt = _k(chain, token), int(time.time())
    zeiten = {_k(chain, b.get("wallet")): (b.get("buy_ts") or b.get("ts"), b.get("exit_ts")) for b in erg.get("buyers") or []}
    for w, rolle, pct, held in _rollen(chain, erg):
        bt, et = zeiten.get(w, (None, None)) if rolle in ("first_buyer", "bundle") else (None, None)
        try:
            _q("INSERT INTO wl_seen (chain, token, wallet, role, pct, held_pct, ts, buy_ts, exit_ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
               "ON CONFLICT (chain, token, wallet, role) DO UPDATE SET held_pct=EXCLUDED.held_pct, ts=EXCLUDED.ts, "
               "pct=COALESCE(EXCLUDED.pct, wl_seen.pct), buy_ts=COALESCE(EXCLUDED.buy_ts, wl_seen.buy_ts), "
               "exit_ts=COALESCE(EXCLUDED.exit_ts, wl_seen.exit_ts)", (chain, tk, w, rolle, pct, held, jetzt, bt, et))
        except Exception as e:
            log.debug(f"muster merken: {e}")
            return


# ── Muster finden ────────────────────────────────────────────────────────

def _auftritte(chain, token, wallets):
    """{wallet: [(token, role, held_pct)]} aus anderen Coins: wl_seen plus die aelteren Tabellen."""
    tk = _k(chain, token)
    ws = list({_k(chain, w) for w in wallets if w})
    if not ws:
        return {}
    out = {}
    for r in _q("SELECT wallet, token, role, held_pct FROM wl_seen WHERE chain=%s AND wallet = ANY(%s) AND token<>%s",
                (chain, ws, tk)) or []:
        out.setdefault(r[0], {})[(r[1], r[2])] = r[3]
    for r in _q("SELECT wallet, token FROM iw_early WHERE chain=%s AND wallet = ANY(%s) AND token<>%s", (chain, ws, tk)) or []:
        d = out.setdefault(r[0], {})
        if not any(t == r[1] for t, _ in d):
            d[(r[1], "first_buyer")] = None
    try:
        for r in _q("SELECT dst, token FROM iw_fwd WHERE chain=%s AND dst = ANY(%s) AND token<>%s", (chain, ws, tk)) or []:
            d = out.setdefault(r[0], {})
            if not any(t == r[1] for t, _ in d):
                d[(r[1], "side")] = None
    except Exception:
        pass
    return {w: [(t, ro, h) for (t, ro), h in d.items()] for w, d in out.items()}


def _coins(chain, tokens):
    if not tokens:
        return {}
    rows = _q("SELECT token, symbol, first_mcap, peak_mcap, multiple, last_mcap FROM iw_tokens WHERE chain=%s AND token = ANY(%s)",
              (chain, list(tokens))) or []
    out = {}
    for r in rows:
        peak, last = r[3] or 0, r[5]
        out[r[0]] = {"symbol": r[1], "multiple": r[4], "peak": peak, "last": last,
                     "dead": bool(peak and last is not None and last < peak * TOT),
                     "ran": bool((r[4] or 0) >= GELAUFEN)}
    # Seit 2.63: Coins ohne Namen (aus aelteren Tabellen oder Scans ohne Symbol) einmal nachschlagen und merken,
    # damit die Karte $SIL zeigt statt "$Tx67…pump"
    # Seit 2.63: fehlende Namen UND fehlende aktuelle Market Caps nachschlagen. Ohne aktuellen Stand war "unter 20 %
    # vom Hoch" fast nie erfuellt (nur Coins, die zufaellig nachverfolgt wurden), und die Zeile widersprach dem Rest.
    ohne = [t for t in tokens if not (out.get(t) or {}).get("symbol") or (out.get(t) or {}).get("last") is None]
    if ohne:
        for t, (sym, mc) in _namen(chain, ohne, mit_mc=True).items():
            e = out.setdefault(t, {"symbol": None, "multiple": None, "peak": 0, "last": None, "dead": False, "ran": False})
            e["symbol"] = e.get("symbol") or sym
            if mc:
                e["last"] = mc
                peak = max(e.get("peak") or 0, mc)
                e["peak"] = peak
                e["dead"] = bool(peak and mc < peak * TOT)
            try:
                _q("UPDATE iw_tokens SET symbol=COALESCE(NULLIF(symbol,''), %s), last_mcap=COALESCE(%s, last_mcap), "
                   "peak_mcap=GREATEST(COALESCE(peak_mcap,0), COALESCE(%s,0)) WHERE chain=%s AND token=%s", (sym, mc, mc, chain, t))
            except Exception:
                pass
    out["_bekannt"] = sum(1 for t in tokens if (out.get(t) or {}).get("last") is not None)
    return out


_NAMEN = {}
DS_KETTE = {"solana": "solana", "robinhood": "robinhood", "base": "base", "ethereum": "ethereum", "arbitrum": "arbitrum", "bsc": "bsc"}


def _namen(chain, tokens, mit_mc=False):
    """{token: symbol} (mit_mc: {token: (symbol, market_cap)}) ueber DexScreener, 30 je Abfrage, 15 min gemerkt."""
    import requests
    out, offen = {}, []
    for t in tokens:
        c = _NAMEN.get((chain, t))
        if c is not None and time.time() - c[2] < 900:
            if c[0] or c[1]:
                out[t] = (c[0], c[1]) if mit_mc else c[0]
        else:
            offen.append(t)
    kette = DS_KETTE.get(chain)
    if not kette:
        return out
    for i in range(0, min(len(offen), 90), 30):
        teil = offen[i:i + 30]
        try:
            d = requests.get(f"https://api.dexscreener.com/tokens/v1/{kette}/" + ",".join(teil), timeout=12).json()
        except Exception:
            continue
        gefunden = {}
        for p in d if isinstance(d, list) else []:
            bt = p.get("baseToken") or {}
            a = bt.get("address") or ""
            a = a if chain in ("solana", "tron", "ton") else a.lower()
            mc = float(p.get("marketCap") or p.get("fdv") or 0) or None
            if a and (a not in gefunden or (mc or 0) > (gefunden[a][1] or 0)):
                gefunden[a] = (str(bt.get("symbol") or "")[:20] or None, mc)
        for t in teil:
            sym, mc = gefunden.get(t, (None, None))
            _NAMEN[(chain, t)] = (sym, mc, time.time())
            if sym or mc:
                out[t] = (sym, mc) if mit_mc else sym
    return out


def finden(chain, token, erg):
    """Das Muster fuer einen frischen Tiefen-Scan. -> {} wenn nichts bekannt ist."""
    try:
        rollen = _rollen(chain, erg)
    except Exception:
        return {}
    hier = {}
    for w, rolle, _, _ in rollen:
        hier.setdefault(w, set()).add(rolle)
    try:
        auf = _auftritte(chain, token, list(hier))
    except Exception as e:
        log.debug(f"muster finden: {e}")
        return {}
    auf = {w: a for w, a in auf.items() if a}
    if not auf:
        return {"checked": len(hier), "known": 0}
    coins = _coins(chain, {t for a in auf.values() for t, _, _ in a})
    je_coin = {}
    for w, a in auf.items():
        for t, ro, held in a:
            je_coin.setdefault(t, set()).add(w)
    reihe = sorted(je_coin.items(), key=lambda x: -len(x[1]))
    # Was diese Wallets dort getan haben: als fruehe Kaeufer komplett raus?
    raus = sum(1 for w, a in auf.items() if any(ro in ("first_buyer", "bundle") and held is not None and held <= 0.001 for _, ro, held in a))
    tote = [t for t in je_coin if coins.get(t, {}).get("dead")]
    gelaufen = [t for t in je_coin if coins.get(t, {}).get("ran")]
    # Gruppe: dieselben Wallets zusammen in mindestens 2 anderen Coins
    gruppe, g_coins = set(), []
    liste = [(t, ws) for t, ws in reihe if len(ws) >= 3][:12]
    for i in range(len(liste)):
        for j in range(i + 1, len(liste)):
            schnitt = liste[i][1] & liste[j][1]
            if len(schnitt) >= 3 and len(schnitt) > len(gruppe):
                gruppe, g_coins = schnitt, [liste[i][0], liste[j][0]]
    if gruppe:
        g_coins += [t for t, ws in liste if t not in g_coins and len(gruppe & ws) >= 3]
    sym = lambda t: "$" + (coins.get(t, {}).get("symbol") or _kurz(t))
    # Einzelne auffaellige Wallets: Dev, Top-Holder und Dev-Empfaenger mit vielen Auftritten
    einzeln = []
    for w, a in sorted(auf.items(), key=lambda x: -len(x[1])):
        r_hier = hier.get(w) or set()
        if not r_hier & {"dev", "top_holder", "dev_side"} or len(a) < 2:
            continue
        frueh = [t for t, ro, _ in a if ro in ("first_buyer", "bundle")]
        einzeln.append({"wallet": w, "here": sorted(r_hier), "coins": len({t for t, _, _ in a}), "early_in": len(set(frueh)),
                        "dead_after": sum(1 for t in set(t for t, _, _ in a) if coins.get(t, {}).get("dead")),
                        "examples": [sym(t) for t in list(dict.fromkeys(t for t, _, _ in a))[:4]]})
        if len(einzeln) >= 3:
            break
    haltezeit = []
    try:
        for r_ in _q("SELECT exit_ts - buy_ts FROM wl_seen WHERE chain=%s AND wallet = ANY(%s) AND token<>%s AND buy_ts IS NOT NULL "
                     "AND exit_ts IS NOT NULL AND exit_ts >= buy_ts", (chain, list(auf), _k(chain, token))) or []:
            haltezeit.append(r_[0] / 3600)
    except Exception:
        pass
    haltezeit.sort()
    median_h = haltezeit[len(haltezeit) // 2] if len(haltezeit) >= 3 else None
    out = {"checked": len(hier), "known": len(auf), "coins": len(je_coin),
           "hold_median_h": round(median_h, 2) if median_h is not None else None, "hold_samples": len(haltezeit),
           "top": [{"coin": sym(t), "token": t, "wallets": len(ws), "dead": coins.get(t, {}).get("dead"),
                    "ran": coins.get(t, {}).get("ran")} for t, ws in reihe[:5]],
           "sold_out_there": raus, "coins_dead": len(tote), "coins_ran": len(gelaufen), "coins_with_price": coins.get("_bekannt"),
           "group": {"wallets": len(gruppe), "coins": [sym(t) for t in g_coins[:4]],
                     "members": sorted(gruppe)[:12]} if gruppe else None,
           "standouts": einzeln}
    out["lines"] = zeilen(out)
    return out


def zeilen(m):
    """Kurze Zeilen fuer die Karte. Nur Zahlen und Coins, keine Urteile."""
    if not m or not m.get("known"):
        return []
    z = []
    top = ", ".join(f"{x['coin']} ({x['wallets']})" for x in m["top"][:3])
    s = f"Seen before: {m['known']} of this coin's wallets were in {m['coins']} other coin{'s' if m['coins'] != 1 else ''} we checked: {top}"
    z.append(s)
    teile = []
    if m.get("sold_out_there"):
        teile.append(f"{m['sold_out_there']} of them sold everything there")
    if m.get("coins_dead"):
        basis = m.get("coins_with_price")
        teile.append(f"{m['coins_dead']} of {'the ' + str(basis) + ' coins with a live price' if basis else 'those coins'} "
                     f"{'is' if m['coins_dead'] == 1 else 'are'} now under {int(TOT * 100)}% of their peak")
    if m.get("coins_ran"):
        teile.append(f"{m['coins_ran']} of those coins ran {GELAUFEN:g}x or more")
    if teile:
        z.append(", ".join(teile)[0].upper() + ", ".join(teile)[1:])
    if m.get("hold_median_h") is not None:
        h = m["hold_median_h"]
        dauer = f"{h * 60:.0f} minutes" if h < 1 else f"{h:.1f} hours" if h < 48 else f"{h / 24:.1f} days"
        z.append(f"When they sold out in those coins, it was after a median of {dauer} ({m['hold_samples']} exits)")
    g = m.get("group")
    if g:
        z.append(f"Same group: {g['wallets']} wallets were in {', '.join(g['coins'])} together too")
    for e in m.get("standouts") or []:
        rolle = "Dev" if "dev" in e["here"] else ("Top holder" if "top_holder" in e["here"] else "Wallet that got tokens from the dev")
        z.append(f"{rolle} {_kurz(e['wallet'])} was in {e['coins']} other coins ({', '.join(e['examples'])})"
                 + (f", {e['dead_after']} now under {int(TOT * 100)}% of peak" if e["dead_after"] else ""))
    return z[:5]


def wallet(chain, w):
    """Alle Auftritte einer Wallet, fuer 'where was this wallet before?'."""
    wk = _k(chain, w)
    auf = _auftritte(chain, "-", [wk]).get(wk) or []
    halte = {}
    try:
        for r_ in _q("SELECT token, buy_ts, exit_ts FROM wl_seen WHERE chain=%s AND wallet=%s AND buy_ts IS NOT NULL", (chain, wk)) or []:
            halte[r_[0]] = (r_[1], r_[2])
    except Exception:
        pass
    coins = _coins(chain, {t for t, _, _ in auf})
    je = {}
    for t, ro, held in auf:
        e = je.setdefault(t, {"coin": "$" + (coins.get(t, {}).get("symbol") or _kurz(t)), "token": t, "roles": [], "held_pct_at_scan": held})
        e["roles"].append(NAME.get(ro, ro))
        c = coins.get(t) or {}
        bt, et = halte.get(t, (None, None))
        if bt:
            e["bought"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(bt))
            e["held_hours_until_sold_out"] = round((et - bt) / 3600, 2) if et else None
            e["still_in_at_scan"] = et is None
        e.update(peak_multiple=round(c["multiple"], 1) if c.get("multiple") else None,
                 now_vs_peak=round(c["last"] / c["peak"] * 100) if c.get("peak") and c.get("last") is not None else None)
    liste = sorted(je.values(), key=lambda e: -(e.get("peak_multiple") or 0))
    return {"wallet": w, "chain": chain, "coins": len(liste), "seen_in": liste[:25],
            "meaning": "Coins from our own scans where this wallet showed up, with its role, what it still held when we scanned, "
                       "how far the coin ran from our first check (peak_multiple) and where it is now against its peak in %. "
                       "Chain facts only."}


# ── Einmal nachlesen: Torch-Coins der letzten Wochen ─────────────────────

_LAUF = [None]


def _mcaps(adressen):
    import requests
    out = {}
    for i in range(0, len(adressen), 30):
        try:
            d = requests.get("https://api.dexscreener.com/tokens/v1/robinhood/" + ",".join(adressen[i:i + 30]), timeout=20).json()
        except Exception:
            continue
        for p in d if isinstance(d, list) else []:
            a = str((p.get("baseToken") or {}).get("address") or "").lower()
            mc = p.get("marketCap") or p.get("fdv") or 0
            if a and mc and mc > (out.get(a) or (0, ""))[0]:
                out[a] = (float(mc), (p.get("baseToken") or {}).get("symbol"))
    return out


def _nach_symbol(sym):
    import requests
    try:
        d = requests.get("https://api.dexscreener.com/latest/dex/search?q=" + sym, timeout=20).json()
    except Exception:
        return None
    paare = [p for p in d.get("pairs") or [] if p.get("chainId") == "robinhood"
             and str((p.get("baseToken") or {}).get("symbol") or "").upper() == sym.upper()]
    paare.sort(key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0))
    if not paare:
        return None
    p = paare[0]
    return (str(p["baseToken"]["address"]).lower(), float(p.get("marketCap") or p.get("fdv") or 0), sym.upper())


def _nachlesen():
    import storage
    import intel
    import onchain
    try:
        st = json.loads(storage.kv_get("muster_nachlesen") or "null") or {}
    except Exception:
        st = {}
    if st.get("done"):
        return
    liste = st.get("queue")
    if liste is None:
        liste = []
        for s in NACHLESEN_SYMBOLE:
            x = _nach_symbol(s)
            if x:
                liste.append(list(x))
        try:
            top = int(onchain._rpc("eth_blockNumber", []), 16)
            ab = top - NACHLESEN_TAGE * 86400 * 9   # Robinhood Chain: etwa 9 bis 10 Bloecke je Sekunde
            neu = [l["token"].lower() for l in onchain.launches() if l.get("block", 0) >= ab]
        except Exception as e:
            log.warning(f"muster nachlesen launches: {e}")
            neu = []
        mc = _mcaps(neu)
        kandidaten = sorted(((a, mc[a][0], mc[a][1]) for a in mc if mc[a][0] >= 10000), key=lambda x: -x[1])
        bekannt = {x[0] for x in liste}
        liste += [list(x) for x in kandidaten if x[0] not in bekannt][:NACHLESEN_MAX]
        st = {"queue": liste, "pos": 0, "started": int(time.time())}
        storage.kv_set("muster_nachlesen", json.dumps(st))
        log.info(f"muster: reading back {len(liste)} Torch coins")
    while st["pos"] < len(liste):
        a, mc, sym = liste[st["pos"]]
        try:
            if not (intel.aus_cache("robinhood", a) or {}).get("measured"):
                intel.scannen("robinhood", a, mc, sym)
            else:
                merken("robinhood", a, intel.aus_cache("robinhood", a), sym)
        except Exception as e:
            log.debug(f"muster nachlesen {a[:10]}: {e}")
        st["pos"] += 1
        storage.kv_set("muster_nachlesen", json.dumps(st))
        time.sleep(float(os.getenv("PATTERN_BACKFILL_PAUSE", "8")))
    st["done"] = int(time.time())
    storage.kv_set("muster_nachlesen", json.dumps(st))
    log.info(f"muster: read back {len(liste)} coins")


def runde():
    if os.getenv("PATTERN_BACKFILL", "1") == "0":
        return
    if _LAUF[0] and _LAUF[0].is_alive():
        return
    _LAUF[0] = threading.Thread(target=_nachlesen, name="muster", daemon=True)
    _LAUF[0].start()


def stand():
    import storage
    try:
        st = json.loads(storage.kv_get("muster_nachlesen") or "null") or {}
    except Exception:
        st = {}
    n = _q("SELECT COUNT(*), COUNT(DISTINCT wallet), COUNT(DISTINCT token) FROM wl_seen", (), eins=True) or (0, 0, 0)
    return {"rows": n[0], "wallets": n[1], "coins": n[2], "backfill": {"pos": st.get("pos"), "of": len(st.get("queue") or []),
                                                                       "done": st.get("done")}}


def letzter(chain, token, mcap):
    """Aus intel beim Nachverfolgen: der aktuelle Market Cap, damit 'unter 20 % vom Hoch' stimmt."""
    try:
        _q("UPDATE iw_tokens SET last_mcap=%s WHERE chain=%s AND token=%s", (mcap, chain, token))
    except Exception:
        pass


# ── Coins vergleichen: gibt es Verbindungen zwischen den Wallets? (seit 2.63) ──

def _aus_scans(chain, tokens):
    """{token: {wallet: set(rollen)}} und {token: dev} aus wl_seen, iw_early und den Tiefen-Scans im Cache."""
    import intel
    rollen, devs, geldgeber = {}, {}, {}
    for t in tokens:
        rollen[t] = {}
        erg = intel.aus_cache(chain, t) or {}
        if erg.get("measured"):
            for w, ro, _, _ in _rollen(chain, erg):
                rollen[t].setdefault(w, set()).add(ro)
            if erg.get("dev"):
                devs[t] = _k(chain, erg["dev"])
            for c in erg.get("cluster_map") or []:
                if c.get("type") == "funding" and c.get("funder"):
                    for m in c.get("members") or []:
                        geldgeber[_k(chain, m)] = _k(chain, c["funder"])
    for r in _q("SELECT token, wallet, role FROM wl_seen WHERE chain=%s AND token = ANY(%s)", (chain, list(tokens))) or []:
        rollen.setdefault(r[0], {}).setdefault(r[1], set()).add(r[2])
        if r[2] == "dev":
            devs.setdefault(r[0], r[1])
    for r in _q("SELECT token, wallet FROM iw_early WHERE chain=%s AND token = ANY(%s)", (chain, list(tokens))) or []:
        rollen.setdefault(r[0], {}).setdefault(r[1], set()).add("first_buyer")
    return rollen, devs, geldgeber


def vergleichen(eingaben):
    """eingaben: Ticker oder Adressen. -> Verbindungen zwischen den Wallets dieser Coins, nur mit Beleg aus unseren Scans."""
    import scan
    import intel
    coins, fehler, laeuft = [], [], []
    for e in [x.strip() for x in str(eingaben or "").replace(";", ",").replace(" ", ",").split(",") if x.strip()][:6]:
        try:
            r = scan.analyse(e, deep=True)
        except Exception as ex:
            r = {"ok": False, "error": str(ex)[:80]}
        if not r.get("ok"):
            fehler.append({"input": e, "error": r.get("error")})
            continue
        ch, a = r["chain"], _k(r["chain"], r["address"])
        st = (r.get("deep") or {}).get("state")
        if st != "ready" and ch != "elysium":   # auf Elysium gibt es keinen Tiefen-Scan, dort laeuft also nichts
            laeuft.append("$" + str(r.get("symbol") or e))
        coins.append({"chain": ch, "token": a, "symbol": r.get("symbol") or _kurz(a)})
    if len(coins) < 2:
        return {"ok": False, "error": "Name at least two coins (tickers or contract addresses).", "not_found": fehler}
    # Seit 2.82.19: alle EVM-Chains zusammen vergleichen. Eine EVM-Adresse ist auf jeder Chain dieselbe Wallet,
    # also zaehlt dieselbe Adresse in einem Robinhood-Coin und einem Elysium-Coin als dieselbe Wallet.
    ketten = {}
    for c in coins:
        ketten.setdefault("solana" if c["chain"] == "solana" else "evm", []).append(c)
    out = {"ok": True, "coins": ["$" + c["symbol"] for c in coins], "not_found": fehler, "results": []}
    if laeuft:
        out["deep_scan_running"] = laeuft
        out["note_running"] = "The deep scan of these coins is still running. Ask again in a minute or two for the full picture."
    einzeln = [c for cs in ketten.values() if len(cs) < 2 for c in cs]
    if einzeln and len(ketten) > 1:
        out["not_comparable"] = {"coins": ["$" + c["symbol"] for c in einzeln],
                                 "why": "Solana wallets and EVM wallets have different addresses, so they cannot be matched."}
    for gruppe, cs in ketten.items():
        if len(cs) < 2:
            continue
        chains = sorted({c["chain"] for c in cs})
        chain = chains[0]
        kname = lambda ch: (scan.KETTEN.get(ch) or (ch,))[0]
        sym = {c["token"]: "$" + c["symbol"] + (f" ({kname(c['chain'])})" if len(chains) > 1 else "") for c in cs}
        rollen, devs, geldgeber = {}, {}, {}
        for c in cs:
            ro, dv_, gg_ = _aus_scans(c["chain"], [c["token"]])
            rollen.update(ro)
            devs.update(dv_)
            geldgeber.update(gg_)
            t = c["token"]
            rollen.setdefault(t, {})
            if t not in devs:   # ohne Tiefen-Scan (Elysium): Dev aus iw_devs
                d = _q("SELECT dev FROM iw_devs WHERE chain=%s AND token=%s ORDER BY ts DESC LIMIT 1", (c["chain"], t), eins=True)
                if d and d[0]:
                    devs[t] = _k(c["chain"], d[0])
            if t in devs:
                rollen[t].setdefault(devs[t], set()).add("dev")
                # wer die Dev-Wallet gefuellt hat, zaehlt als eigene Rolle
                f = _q("SELECT funder FROM wl_wallets WHERE chain=%s AND wallet=%s AND funder IS NOT NULL", (c["chain"], devs[t]), eins=True)
                if f and f[0] and _k(c["chain"], f[0]) != devs[t]:
                    rollen[t].setdefault(_k(c["chain"], f[0]), set()).add("dev_funder")
        # 1. Dieselbe Wallet in mehreren dieser Coins
        je_wallet = {}
        for t, ws in rollen.items():
            for w, ro in ws.items():
                je_wallet.setdefault(w, {})[t] = ro
        gemeinsam = []
        for w, d in je_wallet.items():
            if len(d) < 2:
                continue
            frueh = sum(1 for ro in d.values() if ro & {"first_buyer", "bundle"})
            gemeinsam.append({"wallet": w, "in": len(d), "early_in": frueh,
                              "roles": {sym[t]: sorted(NAME.get(x, x) for x in ro) for t, ro in d.items()}})
        gemeinsam.sort(key=lambda x: (-x["in"], -x["early_in"]))
        # 2. Paare: wie viele frühe Kaeufer teilen sich je zwei Coins
        paare = []
        liste = [c["token"] for c in cs]
        for i in range(len(liste)):
            for j in range(i + 1, len(liste)):
                a_, b_ = liste[i], liste[j]
                fa = {w for w, ro in rollen.get(a_, {}).items() if ro & {"first_buyer", "bundle"}}
                fb = {w for w, ro in rollen.get(b_, {}).items() if ro & {"first_buyer", "bundle"}}
                alle = set(rollen.get(a_, {})) & set(rollen.get(b_, {}))
                if alle:
                    paare.append({"pair": f"{sym[a_]} and {sym[b_]}", "shared_wallets": len(alle), "shared_early_buyers": len(fa & fb),
                                  "early_buyers_checked": [len(fa), len(fb)]})
        # 3. Dev eines Coins taucht in einem anderen auf
        dev_quer = []
        for t, dv in devs.items():
            for t2 in liste:
                if t2 != t and dv in rollen.get(t2, {}):
                    dev_quer.append({"dev_of": sym[t], "wallet": dv, "in": sym[t2],
                                     "as": sorted(NAME.get(x, x) for x in rollen[t2][dv])})
        # 4. Gleicher Geldgeber fuer Wallets aus verschiedenen Coins (aus Clustern und unserer Wallet-Kartei)
        alle_w = list(je_wallet)
        try:
            for r in _q("SELECT wallet, funder FROM wl_wallets WHERE chain = ANY(%s) AND wallet = ANY(%s) AND funder IS NOT NULL",
                        (chains, alle_w[:2000])) or []:
                geldgeber.setdefault(r[0], r[1])
        except Exception:
            pass
        je_geber = {}
        for w, g in geldgeber.items():
            for t in je_wallet.get(w, {}):
                je_geber.setdefault(g, {}).setdefault(t, set()).add(w)
        geber = []
        for g, d in je_geber.items():
            if len(d) < 2:
                continue
            try:
                import kartei
                art = (kartei.art(chain, g) or {})
            except Exception:
                art = {}
            geber.append({"funder": g, "funder_is": art.get("name") or art.get("kind") or "wallet",
                          "exchange": art.get("kind") in ("exchange", "cex"),
                          "funded_wallets": {sym[t]: len(ws) for t, ws in d.items()}})
        geber.sort(key=lambda x: (x["exchange"], -sum(x["funded_wallets"].values())))
        # 5. Tokens von einer Wallet an eine andere, wo beide in diesen Coins vorkommen
        wege = []
        try:
            for r in _q("SELECT token, src, dst, pct FROM iw_fwd WHERE chain = ANY(%s) AND token = ANY(%s) AND (src = ANY(%s) OR dst = ANY(%s))",
                        (chains, liste, alle_w[:2000], alle_w[:2000])) or []:
                von, nach = je_wallet.get(r[1], {}), je_wallet.get(r[2], {})
                if set(von) | set(nach) and len(set(von) | set(nach) | {r[0]}) >= 2:
                    wege.append({"coin": sym.get(r[0]), "from": r[1], "to": r[2], "pct": r[3],
                                 "from_in": [sym[t] for t in von], "to_in": [sym[t] for t in nach]})
        except Exception:
            pass
        # 6. Wer die Dev-Wallet eines Coins gefuellt hat und in einem anderen dieser Coins vorkommt (seit 2.82.19)
        team_quer = []
        for w, d in je_wallet.items():
            geb = [t for t, ro in d.items() if "dev_funder" in ro]
            andere = {t: ro for t, ro in d.items() if t not in geb}
            if geb and andere:
                team_quer.append({"wallet": w, "funded_dev_of": [sym[t] for t in geb],
                                  "also_in": {sym[t]: sorted(NAME.get(x, x) for x in ro) for t, ro in andere.items()}})
        stark = len([g for g in gemeinsam if g["early_in"] >= 2]) + len(dev_quer) + len(wege) + len(team_quer)
        out["results"].append({
            "chain": chain if len(chains) == 1 else "cross-chain (" + " + ".join(kname(c) for c in chains) + ")",
            "coins": [sym[t] for t in liste],
            "wallets_compared": {sym[t]: len(rollen.get(t, {})) for t in liste},
            "summary": {"same_wallet_in_several": len(gemeinsam), "same_early_buyer_in_several": sum(1 for g in gemeinsam if g["early_in"] >= 2),
                        "dev_seen_in_other_coin": len(dev_quer), "dev_funder_seen_in_other_coin": len(team_quer),
                        "shared_funders_not_exchange": sum(1 for g in geber if not g["exchange"]),
                        "forwards_between_them": len(wege),
                        "link_strength": "strong" if stark >= 3 else "some" if stark or any(not g["exchange"] for g in geber) else "none found"},
            "same_wallets": gemeinsam[:15], "pairs": paare, "dev_links": dev_quer[:10], "dev_funder_links": team_quer[:10], "shared_funders": geber[:10], "forwards": wege[:10]})
    out["how_to_report"] = ("Lead with link_strength and the plain numbers. Then the strongest proof first: a dev of one coin in another, "
                            "a wallet that funded the dev of one coin and shows up in another (dev_funder_links), "
                            "the same wallets buying early in several of these coins (list them short, with their roles), tokens sent "
                            "between wallets of these coins, then shared funders that are not exchanges (an exchange as funder proves "
                            "nothing). Say what it can mean ('can be the same group'), never that it is. Name no people. If nothing "
                            "is found, say that clearly: no link in our scans is not proof that there is none. Offer the wallet history "
                            "of any wallet and tracking.")
    return out
