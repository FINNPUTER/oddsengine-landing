"""
wochen.py: Sunday Report von Your Engine (seit 2.62.9).

Jeden Sonntag (Standard 18 Uhr Ortszeit des Nutzers) ein Wochenrueckblick fuer seine Wallets. Keine KI, keine Kosten:
  Wert der Wallets (ETH, SOL, Stablecoins, Coins) jetzt und gegenueber dem letzten Report
  Kaeufe, Verkaeufe, realisierter Gewinn oder Verlust der Woche (FIFO wie beim Steuer-Export), Gebuehren
  groesste Gewinner und Verlierer im Bestand
  was der Exit Coach gemeldet hat und wo der Coin jetzt steht
  Wallet Guard: Warnungen der Woche, offene riskante Freigaben
  ein Hinweis fuer die naechste Woche
Wallets: die verknuepften des Nutzers oder beliebige, die er nennt (bis 3 EVM, 2 Solana).

  einstellen(adr, on, wallets, day, hour)   an/aus, Wallets, Tag und Stunde
  status(adr)
  jetzt(adr, wallets)                       sofort einen bauen und schicken (im Hintergrund)
  runde()                                   aus motor.tick
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from logger import log

TAG = int(os.getenv("WEEKLY_DAY", "6"))        # 0 = Montag, 6 = Sonntag
STUNDE = int(os.getenv("WEEKLY_HOUR", "18"))
MAX_EVM, MAX_SOL = 3, 2
TAGE = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}
NAMEN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NATIV = {"ETH", "SOL", "WETH", "WSOL"}
_LOCK = threading.Lock()
_WARTE = []
_ZULETZT = {}


# ── Einstellungen ────────────────────────────────────────────────────────

def _p(adr):
    import motor
    z = motor.zustand(adr)
    w = z["prefs"].setdefault("weekly", {})
    return z, w


def _kurz(a):
    a = str(a)
    return a[:6] + "…" + a[-4:] if a.startswith("0x") else a[:4] + "…" + a[-4:]


def _wallets_lesen(text):
    teile = [w.strip() for w in str(text or "").replace(";", ",").replace(" ", ",").split(",") if w.strip()]
    evm = [w.lower() for w in teile if w.startswith("0x") and len(w) == 42]
    sol = [w for w in teile if not w.startswith("0x") and 32 <= len(w) <= 44 and w.isalnum()]
    return list(dict.fromkeys(evm))[:MAX_EVM], list(dict.fromkeys(sol))[:MAX_SOL]


def _wallets(adr, w=None):
    """(evm, sol): die fuer den Report gemerkten, sonst die verknuepften des Nutzers."""
    if w is None:
        _, w = _p(adr)
    eigene = w.get("wallets") or []
    if eigene:
        return _wallets_lesen(",".join(eigene))
    import steuer
    evm, sol = steuer.wallets_von(adr)
    return evm[:MAX_EVM], sol[:MAX_SOL]


def status(adr):
    z, w = _p(adr)
    evm, sol = _wallets(adr, w)
    tag, stunde = int(w.get("day", TAG)), int(w.get("hour", STUNDE))
    return {"on": bool(w.get("on")), "wallets": [_kurz(x) for x in evm + sol],
            "wallets_from": "set for the report" if w.get("wallets") else "the user's linked wallets",
            "day": NAMEN[tag], "local_hour": stunde, "last_sent": w.get("sent"),
            "what": "Every week: wallet value and change, buys and sells, realized gain or loss, fees, biggest movers, "
                    "what the Exit Coach flagged and where the coin is now, Wallet Guard alerts, risky approvals, one hint "
                    "for next week. Free, no AI cost. Value covers Robinhood Chain and Solana, trades cover Robinhood "
                    "Chain, Ethereum, Base, Arbitrum and Solana."}


def einstellen(adr, on=None, wallets=None, day=None, hour=None):
    import motor
    z, w = _p(adr)
    if wallets:
        if str(wallets).strip().lower() in ("mine", "linked", "own", "reset"):
            w.pop("wallets", None)
        else:
            evm, sol = _wallets_lesen(wallets)
            if not evm and not sol:
                return {"ok": False, "error": "Send the wallet address: 0x... or a Solana address."}
            w["wallets"] = evm + sol
    if day is not None and str(day).strip():
        d = str(day).strip().lower()[:3]
        if d[:2] in TAGE and d not in TAGE:
            d = d[:2]
        if d not in TAGE:
            return {"ok": False, "error": "Day like sunday, monday, ..."}
        w["day"] = TAGE[d]
    if hour is not None and str(hour).strip():
        try:
            h = int(float(str(hour).split(":")[0]))
            if not 0 <= h <= 23:
                raise ValueError
            w["hour"] = h
        except Exception:
            return {"ok": False, "error": "Hour 0 to 23, local time."}
    if on is not None:
        w["on"] = bool(on)
    motor._zustand_speichern(adr, z)
    evm, sol = _wallets(adr, w)
    out = dict(status(adr), ok=True)
    if w.get("on") and not evm and not sol:
        out["needs"] = "No wallet yet. Ask the user for the wallet address (0x... or Solana) and call again with wallets."
    return out


# ── Daten ────────────────────────────────────────────────────────────────

def _preis(asset):
    try:
        import steuer
        return steuer.kurs(asset, int(time.time()) - 3700)[0]
    except Exception:
        return None


def _sol_bestand(w):
    """[(chain, key, sym, usd, menge)] einer Solana-Wallet: SOL, Stablecoins, Coins mit Preis."""
    import holder
    import steuer
    out = []
    try:
        lam = holder._sol_rpc("getBalance", [w])
        sol = (lam.get("value") if isinstance(lam, dict) else lam) / 1e9
        p = _preis("SOL")
        if sol and p:
            out.append(("solana", "SOL", "SOL", sol * p, sol))
    except Exception as e:
        log.debug(f"wochen sol {w[:6]}: {e}")
    mints = {}
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        try:
            r = holder._sol_rpc("getTokenAccountsByOwner", [w, {"programId": prog}, {"encoding": "jsonParsed"}])
        except Exception:
            continue
        for x in (r or {}).get("value") or []:
            try:
                info = x["account"]["data"]["parsed"]["info"]
                m = float(info["tokenAmount"].get("uiAmount") or 0)
            except Exception:
                continue
            if m > 0:
                mints[info["mint"]] = mints.get(info["mint"], 0) + m
    for mint in steuer.SOL_STABLE:
        mints.pop(mint, None)   # Stablecoins zaehlt _sol_stables
    mints.pop("So11111111111111111111111111111111111111112", None)
    liste = list(mints)[:60]
    for i in range(0, len(liste), 30):
        try:
            d = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(liste[i:i + 30]), timeout=15).json()
        except Exception:
            continue
        preis, symb = {}, {}
        for p in d if isinstance(d, list) else []:
            a = (p.get("baseToken") or {}).get("address")
            if a and p.get("priceUsd") and a not in preis:
                preis[a] = float(p["priceUsd"])
                symb[a] = (p.get("baseToken") or {}).get("symbol")
        for m in liste[i:i + 30]:
            usd = mints[m] * preis.get(m, 0)
            if usd >= 1:
                out.append(("solana", m, symb.get(m) or _kurz(m), usd, mints[m]))
    return out


def _sol_stables(w):
    import holder
    import steuer
    summe = 0.0
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",):
        try:
            r = holder._sol_rpc("getTokenAccountsByOwner", [w, {"programId": prog}, {"encoding": "jsonParsed"}])
        except Exception:
            continue
        for x in (r or {}).get("value") or []:
            try:
                info = x["account"]["data"]["parsed"]["info"]
                if info["mint"] in steuer.SOL_STABLE:
                    summe += float(info["tokenAmount"].get("uiAmount") or 0)
            except Exception:
                continue
    return summe


def bestand(evm, sol):
    """-> (gesamt_usd, {key: [sym, preis, usd]}, nur Coins ohne ETH/SOL/Stablecoins im dict)"""
    import onchain
    import fluss
    gesamt, coins = 0.0, {}
    p_eth = _preis("ETH")
    for w in evm:
        try:
            h = onchain.holdings(w)
        except Exception:
            continue
        if h.get("eth") and p_eth:
            gesamt += h["eth"] * p_eth
        for t in h.get("tokens") or []:
            usd = t.get("usd") or 0
            sym = str(t.get("symbol") or "")
            if not usd or fluss.falsch(sym, "robinhood", t.get("token")):
                continue
            gesamt += usd
            if sym.upper() in NATIV or sym.upper() in ("USDG", "USDC", "USDT", "DAI", "PYUSD") or usd < 1:
                continue
            k = f"robinhood:{str(t['token']).lower()}"
            c = coins.setdefault(k, [sym, t.get("price_usd"), 0.0])
            c[2] += usd
    for w in sol:
        for ch, k, sym, usd, m in _sol_bestand(w):
            gesamt += usd
            if k == "SOL":
                continue
            c = coins.setdefault(f"solana:{k}", [sym, usd / m if m else None, 0.0])
            c[2] += usd
        try:
            gesamt += _sol_stables(w)
        except Exception:
            pass
    return gesamt, coins


def handel(evm, sol, t0):
    """Kaeufe, Verkaeufe und realisierter Gewinn seit t0, FIFO ueber die ganze Geschichte."""
    import steuer
    ereignisse, unvollst = steuer.sammeln(evm, sol)
    sales, zeilen, _, _, _ = steuer.rechnen(ereignisse, evm + sol)
    woche = [x for x in zeilen if x["ts"] >= t0]
    kaeufe = sum(1 for x in woche if x["type"] == "buy")
    verkaeufe = sum(1 for x in woche if x["type"] == "sell")
    tausch = sum(1 for x in woche if x["type"] == "swap")
    gebuehren = sum(x.get("fee_usd") or 0 for x in woche)
    je = {}
    for s in sales:
        if s["sold"] < t0 or s.get("gain_usd") is None:
            continue
        sym = str(s.get("asset") or "")
        if sym.upper() in NATIV or sym.upper() in steuer.STABLE_SYM or ":" not in str(s.get("key")):
            continue
        je[sym] = je.get(sym, 0.0) + s["gain_usd"]
    verkauft = {str(s.get("key")) for s in sales if s["sold"] >= t0}
    return {"buys": kaeufe, "sells": verkaeufe, "swaps": tausch, "fees": gebuehren,
            "realized": sum(je.values()) if je else None, "by_coin": je, "sold_keys": verkauft, "cut_off": unvollst}


def _ereignisse(adr, art, t0):
    try:
        import aktiv
        pg = aktiv._pg()
        if not pg:
            return []
        return pg.execute("SELECT ts, detail FROM eng_activity WHERE adr=%s AND art=%s AND ts>=%s ORDER BY ts",
                          (adr, art, int(t0))).fetchall()
    except Exception:
        return []


# ── Bericht ──────────────────────────────────────────────────────────────

def _usd(v):
    import motor
    return motor._usd_kurz(abs(v))


def _plus(v):
    return ("+" if v >= 0 else "-") + _usd(v)


def bauen(adr, evm, sol, geplant=False):
    import motor
    import scan
    jetzt = time.time()
    z, w = _p(adr)
    snap = w.get("snap") or {}
    t0 = int(jetzt - 7 * 86400)
    lok = motor._lokal(adr)
    von = (lok - timedelta(days=7)).strftime("%b %d").replace(" 0", " ")
    bis = lok.strftime("%b %d").replace(" 0", " ")
    zeilen = [f"📊 Your {'Sunday Report' if geplant else 'weekly report'} · {von} to {bis}",
              "Wallets: " + ", ".join(_kurz(x) for x in evm + sol), ""]

    gesamt, coins = bestand(evm, sol)
    alt = snap if snap.get("wallets") == evm + sol else {}
    if alt.get("total"):
        diff = gesamt - alt["total"]
        pct = diff / alt["total"] * 100 if alt["total"] else 0
        seit = datetime.fromtimestamp(alt["ts"], timezone.utc) + timedelta(hours=motor._tz(adr))
        zeilen.append(f"💰 Wallet value: {_usd(gesamt)} ({_plus(diff)}, {pct:+.1f}% since {NAMEN[seit.weekday()]} {seit.strftime('%b %d').replace(' 0', ' ')})")
    else:
        zeilen.append(f"💰 Wallet value: {_usd(gesamt)}. From next week you see how it changed.")

    try:
        h = handel(evm, sol, t0)
    except Exception as e:
        log.warning(f"wochen handel {adr[:8]}: {e}")
        h = None
    if h and (h["buys"] or h["sells"] or h["swaps"]):
        teile = [f"{h['buys']} buys", f"{h['sells']} sells"] + ([f"{h['swaps']} swaps"] if h["swaps"] else [])
        zeilen.append("🔁 This week: " + ", ".join(teile))
        if h["realized"] is not None:
            r = f"Realized: {_plus(h['realized'])}"
            if len(h["by_coin"]) > 1:
                best = max(h["by_coin"].items(), key=lambda x: x[1])
                worst = min(h["by_coin"].items(), key=lambda x: x[1])
                r += f" · best ${best[0]} {_plus(best[1])}"
                if worst[1] < 0:
                    r += f" · worst ${worst[0]} {_plus(worst[1])}"
            zeilen.append(r)
        if h["fees"] >= 0.01:
            zeilen.append(f"Fees: ${h['fees']:,.2f}")
        if h["cut_off"]:
            zeilen.append("History not complete for " + ", ".join(h["cut_off"][:2]) + ", numbers may be off.")
    elif h:
        zeilen.append("🔁 No trades this week.")

    # Bewegungen im Bestand: seit dem letzten Report, sonst seit dem Einstand des Exit Coach
    pos = (motor.zustand(adr)["prefs"].get("coach") or {}).get("pos") or {}
    bew = []
    for k, (sym, px, usd) in coins.items():
        if usd < 5:
            continue
        p_alt = ((alt.get("coins") or {}).get(k) or [None, None])[1]
        if p_alt and px:
            bew.append((sym, (px / p_alt - 1) * 100, usd, "7d"))
        elif (pos.get(k) or {}).get("entry") and px:
            bew.append((sym, (px / pos[k]["entry"] - 1) * 100, usd, "since entry"))
    hoch = sorted([b for b in bew if b[1] >= 5], key=lambda b: -b[1])[:2]
    tief = sorted([b for b in bew if b[1] <= -5], key=lambda b: b[1])[:2]
    if hoch or tief:
        zeilen += ["", "📈 Movers in your wallet"]
        for sym, pct, usd, wo in hoch + tief:
            zeilen.append(f"{'🟢' if pct > 0 else '🔻'} ${sym} {pct:+.0f}% {wo} ({_usd(usd)})")

    # Exit Coach
    coach = []
    for ts, det in _ereignisse(adr, "coach", t0):
        try:
            k, art, wert = str(det).split(" ")[:3]
            wert = float(wert)
        except Exception:
            continue
        p = pos.get(k) or {}
        sym = p.get("sym") or "coin"
        tag = NAMEN[(datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=motor._tz(adr))).weekday()]
        jetzt_x = None
        if p.get("entry"):
            try:
                ch, tk = k.split(":", 1)
                px = scan.zahlen(ch, tk).get("price")
                jetzt_x = px / p["entry"] if px else None
            except Exception:
                pass
        nun = ("no longer in your wallet" if k not in coins and not p else
               f"now {jetzt_x:.2f}x" if jetzt_x and jetzt_x >= 1 else
               f"now {(jetzt_x - 1) * 100:+.0f}%" if jetzt_x else "")
        if art == "profit":
            was = f"${sym} hit +{(wert - 1) * 100:.0f}%" if wert < 2 else f"${sym} hit {wert:g}x"
        elif art == "loss":
            was = f"${sym} dropped {abs(wert):.0f}%"
        else:
            was = f"${sym} gave back half of a {wert:.1f}x"
        coach.append(f"• {was} ({tag})" + (f", {nun}" if nun else ""))
    if coach:
        zeilen += ["", "🎯 Exit Coach this week"] + coach[-5:]

    # Wallet Guard
    prefs = motor.zustand(adr)["prefs"]
    wachen = _ereignisse(adr, "guard", t0) + _ereignisse(adr, "bag", t0)
    riskant = 0
    try:
        import wache
        for x in evm + sol:
            r = wache.pruefen(adr, x)
            riskant += int(r.get("risky") or 0)
    except Exception as e:
        log.debug(f"wochen freigaben: {e}")
    zeilen.append("")
    if prefs.get("guard") is True:
        g = f"🛡 Wallet Guard: {len(wachen)} alert{'s' if len(wachen) != 1 else ''} this week" if wachen else "🛡 Wallet Guard: quiet week, no alerts"
    else:
        g = "🛡 Wallet Guard is off"
    if riskant:
        g += f". {riskant} risky approval{'s' if riskant > 1 else ''} still open"
    zeilen.append(g)

    # Ein Hinweis fuer naechste Woche
    hinweis = None
    if riskant:
        hinweis = "Revoke the risky approval first. One tap below."
    if not hinweis:
        for k, p in pos.items():
            if not p.get("entry") or k not in coins:
                continue
            px = coins[k][1]
            x = px / p["entry"] if px else None
            if x and x >= 2 and (not h or k not in h["sold_keys"]):
                hinweis = f"${p.get('sym') or coins[k][0]} is {x:.1f}x since your entry and you took nothing off. Decide where you take profit."
                break
            if x and x <= 0.5:
                hinweis = f"${p.get('sym') or coins[k][0]} is down {(1 - x) * 100:.0f}% from your entry. Check if the reason you bought still holds."
                break
    if not hinweis and prefs.get("guard") is not True:
        hinweis = "Say \"turn on Wallet Guard\" and I watch your wallets around the clock, with the Exit Coach on top."
    zeilen += ["", "👉 Next week: " + (hinweis or "Nothing needs you right now. Enjoy your Sunday.")]
    zeilen += ["", "Not financial advice."]

    knoepfe = [[("Check approvals", "cb:t:protect:1")]] if riskant else None
    neu = {"ts": int(jetzt), "wallets": evm + sol, "total": round(gesamt, 2),
           "coins": {k: [v[0], v[1], round(v[2], 2)] for k, v in coins.items()}}
    return "\n".join(zeilen), knoepfe, neu


def _senden(adr, evm, sol, geplant):
    import motor
    try:
        text, knoepfe, neu = bauen(adr, evm, sol, geplant)
        motor.senden(adr, text, knoepfe)
        z, w = _p(adr)   # frisch laden: andere Runden speichern in der Zwischenzeit auch
        if geplant or not w.get("snap") or w["snap"].get("wallets") != neu["wallets"]:
            w["snap"] = neu
            motor._zustand_speichern(adr, z)
        try:
            import aktiv
            aktiv.merken(adr, "weekly", f"{len(evm + sol)} wallets {'sunday' if geplant else 'now'}")
        except Exception:
            pass
    except Exception as e:
        log.warning(f"wochen {adr[:8]}: {e}")
        if not geplant:
            try:
                motor.senden(adr, "The weekly report did not finish. Please try again in a few minutes.")
            except Exception:
                pass


def _arbeiter():
    while True:
        with _LOCK:
            if not _WARTE:
                return
            adr, evm, sol, geplant = _WARTE.pop(0)
        _senden(adr, evm, sol, geplant)


def _einreihen(adr, evm, sol, geplant):
    with _LOCK:
        if any(x[0] == adr for x in _WARTE):
            return
        _WARTE.append((adr, evm, sol, geplant))
        laeuft = any(t.name == "wochen" and t.is_alive() for t in threading.enumerate())
    if not laeuft:
        threading.Thread(target=_arbeiter, name="wochen", daemon=True).start()


def jetzt(adr, wallets=None):
    if time.time() - _ZULETZT.get(adr, 0) < 600:
        return {"started": False, "note": "A weekly report for you was just built. The next one can come in a few minutes."}
    if wallets:
        evm, sol = _wallets_lesen(wallets)
    else:
        evm, sol = _wallets(adr)
    if not evm and not sol:
        return {"started": False, "error": "No wallet yet. Ask the user for the wallet address (0x... or Solana)."}
    import motor
    if not motor._tg_von(adr):
        return {"started": False, "error": "The report comes in Telegram. Connect Telegram first."}
    _ZULETZT[adr] = time.time()
    _einreihen(adr, evm, sol, False)
    return {"started": True, "wallets": [_kurz(x) for x in evm + sol],
            "note": "Building it now, it arrives here in a minute or two (bigger wallets take longer)."}


def runde():
    """Jede paar Minuten: wer jetzt seinen Report bekommt (Tag, Stunde, einmal je Woche)."""
    import motor
    nutzer = set(motor._nutzer())
    try:
        import agent
        nutzer |= {u.get("wallet") for u in agent.alle() if u.get("wallet")}
    except Exception:
        pass
    for adr in nutzer:
        try:
            z, w = _p(adr)
            if not w.get("on") or not motor._darf(adr):
                continue
            lok = motor._lokal(adr)
            tag, stunde = int(w.get("day", TAG)), int(w.get("hour", STUNDE))
            woche = lok.strftime("%G-W%V")
            if lok.weekday() != tag or not (stunde <= lok.hour < stunde + 3) or w.get("sent") == woche:
                continue
            evm, sol = _wallets(adr, w)
            if not evm and not sol:
                continue
            w["sent"] = woche
            motor._zustand_speichern(adr, z)
            _einreihen(adr, evm, sol, True)
        except Exception as e:
            log.debug(f"wochen runde {str(adr)[:8]}: {e}")
