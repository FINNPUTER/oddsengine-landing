"""
portfolio.py: Portfolio-Manager von Your Engine, seit 2.73.0.

Der Nutzer sagt, welche Wallets ihm gehoeren (Telegram oder engine.oddsengine.live). Your Engine zeigt dann:
  je Wallet       was darin liegt, mit Wert
  zusammen        eine Position je Coin ueber alle Wallets, Anteil am Ganzen, Einstand und Gewinn/Verlust
  Verlauf         Gesamtwert je Tag (eigener Schnappschuss), Veraenderung zu gestern und zur Vorwoche

Quellen:
  Robinhood Chain   onchain.holdings (ETH, $ODDS, $TORCH, USDG, WETH, jeder Torch-Token) plus Robinhood Stock Tokens
  Ethereum, Base, Arbitrum, Optimism, Polygon   nativer Coin ueber RPC, ERC-20 mit Kurs ueber Blockscout (nur Tokens mit
                    Kurs, Fake-Tokens fliegen raus)
  Solana            SOL, Stablecoins und Coins mit Kurs (wie im Sunday Report)
  Einstand          FIFO aus dem Steuer-Rechner (steuer.rechnen), im Hintergrund, einmal am Tag frisch

Keine Empfehlungen, kein Umschichten, kein automatisches Handeln. Nur Zahlen und die Wallets, die der Nutzer selbst nennt.

  wallets(adr, action, wallet)   add / remove / list der eigenen Wallets
  ansehen(adr)                   das Portfolio (fuer das Werkzeug "portfolio")
  kurz(adr)                      eine Zeile fuers Morgenbriefing
  runde()                        aus motor.tick: Tagesschnappschuss und Einstand im Hintergrund
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import storage
from logger import log

MAX_EXTRA = 10
ANDERE = ("ethereum", "base", "arbitrum", "optimism", "polygon")
NATIV = {"ethereum": "ETH", "base": "ETH", "arbitrum": "ETH", "optimism": "ETH", "polygon": "POL"}
NAME = {"robinhood": "Robinhood Chain", "ethereum": "Ethereum", "base": "Base", "arbitrum": "Arbitrum",
        "optimism": "Optimism", "polygon": "Polygon", "solana": "Solana", "tron": "TRON", "ton": "TON"}
STABLE = {"USDC", "USDT", "DAI", "USDG", "PYUSD", "USDE", "USDC.E", "USDBC", "FDUSD"}
_C = {}
_LAEUFT = set()
_LOCK = threading.Lock()


def _j(k, std):
    try:
        v = storage.kv_get(k)
        return json.loads(v) if v else std
    except Exception:
        return std


def _set(k, v):
    try:
        storage.kv_set(k, json.dumps(v))
    except Exception as e:
        log.debug(f"portfolio kv {k}: {e}")


def _kurz(a):
    a = str(a)
    return a[:6] + "…" + a[-4:]


# ── Eigene Wallets ───────────────────────────────────────────────────────

def eigene(adr):
    """(evm, sol): Haupt-Wallet, verknuepfte, die auf engine.oddsengine.live oder im Chat eingetragenen,
    die der Wallet-Wache und die Solana-Wallet."""
    import motor
    import steuer
    evm, sol = steuer.wallets_von(adr)
    for w in motor._einst(adr).get("wallets") or []:
        if not isinstance(w, dict):
            continue
        a = str(w.get("address") or "")
        if w.get("chain") in ("TRON", "TON") or _tt(a):
            continue   # seit 2.82.32: eigene Liste, siehe eigene_mehr
        if w.get("chain") == "SOL" and a not in sol:
            sol.append(a)
        elif a.startswith("0x") and a.lower() not in evm:
            evm.append(a.lower())
    return evm[:12], sol[:6]


def _tt(a):
    import tronton
    return "TRON" if tronton.ist_tron(a) else ("TON" if tronton.ist_ton(a) else None)


def eigene_mehr(adr):
    """(tron, ton): die eingetragenen TRON- und TON-Wallets (seit 2.82.32)."""
    import motor
    tron, ton = [], []
    for w in motor._einst(adr).get("wallets") or []:
        if not isinstance(w, dict):
            continue
        a = str(w.get("address") or "")
        k = _tt(a)
        if k == "TRON" and a not in tron:
            tron.append(a)
        elif k == "TON" and a not in ton:
            ton.append(a)
    return tron[:4], ton[:4]


def wallets(adr, action="list", wallet=None):
    import motor
    s = motor._einst(adr)
    ws = [w for w in s.get("wallets") or [] if isinstance(w, dict)]
    a = str(wallet or "").strip()
    if action in ("add", "remove"):
        teile = [x for x in re.split(r"[,;\s]+", a) if x]
        if not teile:
            return {"ok": False, "error": "Send the wallet address: 0x..., Solana, TRON or TON."}
        for x in teile:
            if re.fullmatch(r"0x[0-9a-fA-F]{40}", x):
                kette, x = "EVM", x.lower()
            elif _tt(x):
                kette = _tt(x)
            elif re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", x):
                kette = "SOL"
            else:
                return {"ok": False, "error": f"{x[:12]} is not a wallet address."}
            ws = [w for w in ws if w.get("address") not in (x, x.lower())]
            if action == "add":
                if len(ws) >= MAX_EXTRA:
                    return {"ok": False, "error": f"Up to {MAX_EXTRA} extra wallets."}
                ws.append({"chain": kette, "address": x})
        s["wallets"] = ws
        motor._einst_speichern(adr, s)
        z = motor.zustand(adr)
        z["prefs"]["pf_on"] = True
        motor._zustand_speichern(adr, z)
        _C.pop(adr, None)
        _set("pf_cost_" + adr, {})  # Einstand neu rechnen, die Wallets haben sich geaendert
    evm, sol = eigene(adr)
    tron, ton = eigene_mehr(adr)
    return {"ok": True, "wallets": [{"address": w, "chain": "EVM (all chains)"} for w in evm] + [{"address": w, "chain": "Solana"} for w in sol]
            + [{"address": w, "chain": "TRON"} for w in tron] + [{"address": w, "chain": "TON"} for w in ton],
            "note": "Only add wallets that belong to the user. They can also add or remove them on https://engine.oddsengine.live."}


# ── Bestaende ────────────────────────────────────────────────────────────

def _preis(sym):
    v = None
    try:
        import steuer
        v = steuer.kurs(sym, int(time.time()) - 3700)[0]
    except Exception:
        v = None
    if not v:
        try:
            import motor
            v = motor.preis(sym).get("usd")
        except Exception:
            v = None
    return v


def _robinhood(w, p_eth):
    import onchain
    import fluss
    out = []
    try:
        h = onchain.holdings(w)
    except Exception as e:
        log.debug(f"portfolio rh {w[:8]}: {e}")
        return out, f"Robinhood Chain not readable for {_kurz(w)}"
    if h.get("eth") and p_eth:
        out.append({"chain": "robinhood", "key": "ETH", "symbol": "ETH", "amount": h["eth"], "usd": h["eth"] * p_eth, "price": p_eth})
    ohne_preis = []
    for t in h.get("tokens") or []:
        sym = str(t.get("symbol") or "")
        if fluss.falsch(sym, "robinhood", t.get("token")):
            continue
        if not t.get("usd"):
            # seit 2.77.3: Bestand da, Preis gerade nicht lesbar: nicht verschweigen
            if t.get("amount"):
                ohne_preis.append(f"{t.get('amount'):,.0f} {sym}")
            continue
        k = "ETH" if sym.upper() == "WETH" else f"robinhood:{str(t['token']).lower()}"
        out.append({"chain": "robinhood", "key": k, "symbol": sym, "amount": t.get("amount"), "usd": t["usd"],
                    "price": t.get("price_usd"), "token": t["token"]})
    # Robinhood Stock Tokens (TSLA, NVDA ...): Bestand per Multicall, Preis aus stocktoken
    try:
        import stocktoken
        liste = list(stocktoken.liste().values())
        r = onchain._multicall([(x["address"], "0x70a08231" + onchain._pad(w)) for x in liste])
        gehalten = [(x, onchain._u(b)) for x, b in zip(liste, r) if onchain._u(b)]
        if gehalten:
            kurse = {k["contract"]: k for k in stocktoken.kurse([x["symbol"] for x, _ in gehalten])}
            for x, roh in gehalten:
                k = kurse.get(x["address"]) or {}
                menge = roh / 1e18
                if k.get("usd") and menge * k["usd"] >= 1:
                    out.append({"chain": "robinhood", "key": f"robinhood:{x['address']}", "symbol": x["symbol"], "amount": menge,
                                "usd": menge * k["usd"], "price": k["usd"], "token": x["address"], "stock": True,
                                "change_24h_pct": k.get("change_24h_pct")})
    except Exception as e:
        log.debug(f"portfolio aktien {w[:8]}: {e}")
    if ohne_preis:
        return out, f"Robinhood price missing for {_kurz(w)}: {', '.join(ohne_preis[:4])} (held, value not counted)"
    return out, None


def _andere(w, ch, preise_nativ):
    import intel
    import verfolger
    import fluss
    out, fehler = [], None
    try:
        b = int(verfolger._rpc(ch, "eth_getBalance", [w, "latest"]), 16) / 1e18
        p = preise_nativ.get(NATIV[ch])
        if b and p and b * p >= 1:
            out.append({"chain": ch, "key": "ETH" if NATIV[ch] == "ETH" else f"{ch}:native", "symbol": NATIV[ch],
                        "amount": b, "usd": b * p, "price": p})
    except Exception:
        fehler = f"{NAME[ch]} not readable for {_kurz(w)}"
    try:
        # derselbe Weg wie verfolger.bestand (Blockscout v2 token-balances, liefert eine Liste)
        d = intel._bs(ch, f"/api/v2/addresses/{w}/token-balances")
        items = d if isinstance(d, list) else None
        if items is None:
            fehler = fehler or f"{NAME[ch]} tokens not readable for {_kurz(w)}"
        for x in items or []:
            t = x.get("token") or {}
            adr_t = str(t.get("address_hash") or t.get("address") or "").lower()
            sym = str(t.get("symbol") or "")
            kurs = t.get("exchange_rate")
            if not kurs or not adr_t or fluss.falsch(sym, ch, adr_t):
                continue
            if str(t.get("type") or "ERC-20") != "ERC-20":
                continue
            menge = int(x.get("value") or 0) / 10 ** int(t.get("decimals") or 18)
            usd = menge * float(kurs)
            if usd < 1:
                continue
            k = "ETH" if sym.upper() == "WETH" else f"{ch}:{adr_t}"
            out.append({"chain": ch, "key": k, "symbol": sym, "amount": menge, "usd": usd, "price": float(kurs), "token": adr_t})
    except Exception as e:
        log.debug(f"portfolio {ch} {w[:8]}: {e}")
    return out, fehler


def _solana(w, p_sol):
    import wochen
    out = []
    try:
        for ch, k, sym, usd, m in wochen._sol_bestand(w):
            out.append({"chain": "solana", "key": "SOL" if k == "SOL" else f"solana:{k}", "symbol": sym, "amount": m,
                        "usd": usd, "price": usd / m if m else None, "token": None if k == "SOL" else k})
        st = wochen._sol_stables(w)
        if st >= 1:
            out.append({"chain": "solana", "key": "solana:stables", "symbol": "USDC/USDT", "amount": st, "usd": st, "price": 1.0})
    except Exception as e:
        log.debug(f"portfolio sol {w[:6]}: {e}")
        return out, f"Solana not readable for {_kurz(w)}"
    return out, None


def _lesen(evm, sol, tron=(), ton=()):
    """{wallet: [positionen]}, [hinweise]"""
    p_eth = _preis("ETH")
    nativ = {"ETH": p_eth, "POL": _preis("POL")}
    p_sol = _preis("SOL") if sol else None
    jobs = []
    for w in evm:
        jobs.append((w, "robinhood"))
        jobs += [(w, ch) for ch in ANDERE]
    jobs += [(w, "solana") for w in sol]
    jobs += [(w, "tron") for w in tron] + [(w, "ton") for w in ton]
    p_trx = _preis("TRX") if tron else None
    p_ton = _preis("TON") if ton else None

    def lauf(j):
        w, ch = j
        if ch == "robinhood":
            return j, _robinhood(w, p_eth)
        if ch == "solana":
            return j, _solana(w, p_sol)
        if ch in ("tron", "ton"):
            import tronton
            return j, (tronton.bestand_tron(w, p_trx) if ch == "tron" else tronton.bestand_ton(w, p_ton))
        return j, _andere(w, ch, nativ)
    je, hinweise = {}, []
    with ThreadPoolExecutor(3) as ex:   # seit 2.77.3: weniger parallel, sonst 429 beim RPC
        for (w, ch), (pos, fehler) in ex.map(lauf, jobs):
            je.setdefault(w, []).extend(pos)
            if fehler:
                hinweise.append(fehler)
    return je, hinweise


# ── Einstand (FIFO) im Hintergrund ───────────────────────────────────────

def _einstand_rechnen(adr):
    try:
        import steuer
        evm, sol = eigene(adr)
        ereignisse, unvollst = steuer.sammeln(evm, sol)
        _, _, bestaende, _, _ = steuer.rechnen(ereignisse, evm + sol)
        je = {}
        for b in bestaende:
            k = str(b.get("key"))
            x = je.setdefault(k, {"amount": 0.0, "cost_usd": 0.0, "first_buy": None})
            x["amount"] += float(b.get("amount") or 0)
            x["cost_usd"] += float(b.get("cost_usd") or 0)
            fb = b.get("first_buy")
            if fb and (not x["first_buy"] or fb < x["first_buy"]):
                x["first_buy"] = fb
        _set("pf_cost_" + adr, {"ts": int(time.time()), "keys": je, "incomplete": unvollst[:5]})
    except Exception as e:
        log.warning(f"portfolio einstand {adr[:8]}: {e}")
        _set("pf_cost_" + adr, {"ts": int(time.time()), "keys": {}, "error": str(e)[:120]})
    finally:
        _LAEUFT.discard(adr)


def _einstand(adr, starten=True):
    c = _j("pf_cost_" + adr, {})
    if (not c.get("ts") or time.time() - c["ts"] > 86400) and starten and adr not in _LAEUFT:
        with _LOCK:
            if adr not in _LAEUFT:
                _LAEUFT.add(adr)
                threading.Thread(target=_einstand_rechnen, args=(adr,), daemon=True).start()
    return c


# ── Portfolio ────────────────────────────────────────────────────────────

def _bauen(adr):
    evm, sol = eigene(adr)
    tron, ton = eigene_mehr(adr)
    if not evm and not sol and not tron and not ton:
        return {"empty": True}
    je, hinweise = _lesen(evm, sol, tron, ton)
    gesamt = sum(p["usd"] for ps in je.values() for p in ps)
    zus = {}
    for w, ps in je.items():
        for p in ps:
            z = zus.setdefault(p["key"], {"symbol": p["symbol"], "key": p["key"], "chains": set(), "amount": 0.0, "usd": 0.0,
                                          "price": p.get("price"), "wallets": 0, "stock": p.get("stock", False),
                                          "change_24h_pct": p.get("change_24h_pct")})
            z["chains"].add(NAME.get(p["chain"], p["chain"]))
            z["amount"] += float(p.get("amount") or 0)
            z["usd"] += p["usd"]
            z["wallets"] += 1
    return {"evm": evm, "sol": sol, "tron": tron, "ton": ton, "je": je, "zus": zus, "gesamt": gesamt, "hinweise": hinweise, "ts": int(time.time())}


def _daten(adr, frisch=False):
    c = _C.get(adr)
    if c and not frisch and time.time() - c[0] < 180:
        return c[1]
    d = _bauen(adr)
    _C[adr] = (time.time(), d)
    return d


def _verlauf(adr):
    return _j("pf_hist_" + adr, [])


def _vergleich(adr, gesamt):
    h = _verlauf(adr)
    heute = time.strftime("%Y-%m-%d", time.gmtime())
    alt = [x for x in h if x.get("d") != heute]
    out = {}
    if alt:
        g = alt[-1]
        if g.get("total"):
            out["since_last_snapshot_pct"] = round((gesamt - g["total"]) / g["total"] * 100, 2)
            out["since_last_snapshot_usd"] = round(gesamt - g["total"], 2)
            out["last_snapshot_day"] = g["d"]
        w = [x for x in alt if x.get("ts", 0) <= time.time() - 6.5 * 86400]
        if w and w[-1].get("total"):
            out["week_pct"] = round((gesamt - w[-1]["total"]) / w[-1]["total"] * 100, 2)
            out["week_usd"] = round(gesamt - w[-1]["total"], 2)
    return out, alt[-1].get("prices", {}) if alt else {}


def ansehen(adr, wallet=None):
    import motor
    z = motor.zustand(adr)
    if not z["prefs"].get("pf_on"):
        z["prefs"]["pf_on"] = True
        motor._zustand_speichern(adr, z)
    d = _daten(adr)
    if d.get("empty"):
        return {"needs": "No wallet yet. Ask the user which wallets are theirs (0x..., Solana, TRON or TON) and call portfolio with "
                          "action add_wallet. They can also add them on https://engine.oddsengine.live."}
    gesamt = d["gesamt"]
    vgl, alt_preise = _vergleich(adr, gesamt)
    kosten = _einstand(adr)
    ck = kosten.get("keys") or {}
    zus = []
    for k, x in sorted(d["zus"].items(), key=lambda kv: -kv[1]["usd"]):
        e = {"coin": x["symbol"], "chains": sorted(x["chains"]), "amount": round(x["amount"], 6), "value_usd": round(x["usd"], 2),
             "share_pct": round(x["usd"] / gesamt * 100, 1) if gesamt else None, "in_wallets": x["wallets"]}
        if x.get("stock"):
            e["stock_token"] = True
        c = ck.get(k)
        if c and c.get("amount") and c.get("cost_usd"):
            # Einstand je Einheit aus FIFO auf die heutige Menge umgelegt
            einst = c["cost_usd"] / c["amount"] * x["amount"]
            e["cost_usd"] = round(einst, 2)
            e["pnl_usd"] = round(x["usd"] - einst, 2)
            e["pnl_pct"] = round((x["usd"] - einst) / einst * 100, 1) if einst else None
        p_alt = alt_preise.get(k)
        if p_alt and x.get("price"):
            e["price_change_since_last_snapshot_pct"] = round((x["price"] - p_alt) / p_alt * 100, 1)
        elif x.get("change_24h_pct") is not None:
            e["change_24h_pct"] = x["change_24h_pct"]
        zus.append(e)
    klein = [e for e in zus if (e["value_usd"] or 0) < 5]
    zus_gross = [e for e in zus if (e["value_usd"] or 0) >= 5][:25]
    je = []
    for w in d["evm"] + d["sol"] + (d.get("tron") or []) + (d.get("ton") or []):
        ps = sorted(d["je"].get(w) or [], key=lambda p: -p["usd"])
        if wallet and str(wallet).lower() not in (w.lower(), _kurz(w).lower()):
            continue
        je.append({"wallet": w, "short": _kurz(w), "type": "Solana" if w in d["sol"] else ("TRON" if w in (d.get("tron") or []) else "TON" if w in (d.get("ton") or []) else "EVM"),
                   "value_usd": round(sum(p["usd"] for p in ps), 2),
                   "positions": [{"coin": p["symbol"], "chain": NAME.get(p["chain"], p["chain"]), "amount": round(float(p.get("amount") or 0), 6),
                                  "value_usd": round(p["usd"], 2)} for p in ps if p["usd"] >= 1][:15]})
    out = {"total_usd": round(gesamt, 2), **vgl, "combined": zus_gross, "by_wallet": je,
           "small_positions": {"count": len(klein), "value_usd": round(sum(e["value_usd"] for e in klein), 2)} if klein else None,
           "cost_basis": ("ready" if ck else "being calculated from the real buys, the first time takes a few minutes. Say: gain and loss follow in a few minutes") if not kosten.get("error") else "not available right now",
           "covered": "Robinhood Chain (ETH, $ODDS, $TORCH, USDG, every Torch token, Robinhood Stock Tokens), Ethereum, Base, "
                      "Arbitrum, Optimism, Polygon (native coin plus tokens with a market price), Solana (SOL, stablecoins, coins with a price), "
                      "TRON (TRX incl. staked, USDT, USDC, tokens with a real market), TON (TON, jettons with a price).",
           "how_to_show": "First the total and the change, then one combined line per coin (value, share, gain or loss if known), "
                          "then each wallet short. Numbers only, no advice. Mention small positions as one line."}
    if d["hinweise"]:
        # seit 2.77.2: zusammengefasst je Chain statt je Wallet, und klar, was trotzdem drin ist
        fehlpreis = [h for h in d["hinweise"] if "price missing" in h]
        if fehlpreis:
            out["held_without_price"] = fehlpreis[:4]
            out["held_without_price_note"] = ("These tokens are in the wallet but their price could not be read right "
                                              "now, so their value is NOT in the total. Say that plainly.")
        d = dict(d, hinweise=[h for h in d["hinweise"] if "price missing" not in h])
    if d["hinweise"]:
        bezahlt = [k for k in ("Base", "Polygon") if any(h.startswith(k) for h in d["hinweise"])]
        if bezahlt and not os.getenv("BLOCKSCOUT_PAID", "").strip():
            out["not_included"] = (f"Tokens on {' and '.join(bezahlt)} are not included yet (only the native coin). Say "
                                   "that in one short line.")
            d = dict(d, hinweise=[h for h in d["hinweise"] if not any(h.startswith(k) for k in bezahlt)])
    if d["hinweise"]:
        ketten = sorted({h.split(" ")[0] for h in d["hinweise"]})
        out["not_read"] = (f"Token balances on {', '.join(ketten)} could not be read right now (the explorer did not "
                           "answer). Native coins and Robinhood Chain are included. Say this in one short line at the "
                           "end, do not list it per wallet.")
    if not vgl:
        out["history"] = ("First snapshot is taken today. The change since yesterday shows from tomorrow, the weekly "
                          "change after 7 days. Say that in one short line, never say something did not come back.")
    if kosten.get("incomplete"):
        out["cost_basis_note"] = "Some chains could not be read fully, gains may be off: " + "; ".join(kosten["incomplete"][:3])
    return out


def kurz(adr):
    """Zeilen fuers Morgenbriefing oder None."""
    try:
        d = _daten(adr)
    except Exception:
        return None
    if d.get("empty") or not d.get("gesamt"):
        return None
    import motor
    vgl, alt_preise = _vergleich(adr, d["gesamt"])
    z = [f"Your portfolio: {motor._usd_kurz(d['gesamt'])}"
         + (f" ({vgl['since_last_snapshot_pct']:+.1f}% since yesterday)" if vgl.get("since_last_snapshot_pct") is not None else "")]
    bew = []
    for k, x in d["zus"].items():
        p_alt = alt_preise.get(k)
        if p_alt and x.get("price") and x["usd"] >= 20:
            bew.append(((x["price"] - p_alt) / p_alt * 100, x["symbol"]))
    if bew:
        bew.sort()
        oben, unten = bew[-1], bew[0]
        if oben[0] > 0:
            z.append(f"Best: {oben[1]} {oben[0]:+.1f}%")
        if unten[0] < 0:
            z.append(f"Worst: {unten[1]} {unten[0]:+.1f}%")
    top = sorted(d["zus"].values(), key=lambda x: -x["usd"])[:3]
    z.append("Biggest: " + ", ".join(f"{x['symbol']} {motor._usd_kurz(x['usd'])}" for x in top))
    return z


# ── Hintergrund ──────────────────────────────────────────────────────────

def _schnappschuss(adr):
    try:
        d = _daten(adr, frisch=True)
        if d.get("empty"):
            return
        heute = time.strftime("%Y-%m-%d", time.gmtime())
        h = [x for x in _verlauf(adr) if x.get("d") != heute]
        h.append({"d": heute, "ts": int(time.time()), "total": round(d["gesamt"], 2),
                  "prices": {k: x["price"] for k, x in d["zus"].items() if x.get("price")}})
        _set("pf_hist_" + adr, h[-120:])
        _einstand(adr)
    except Exception as e:
        log.warning(f"portfolio schnappschuss {adr[:8]}: {e}")
    finally:
        _LAEUFT.discard("snap:" + adr)


def runde():
    """Einmal am Tag je Nutzer mit Portfolio: Wert und Preise merken (fuer 'seit gestern' und die Woche)."""
    import motor
    heute = time.strftime("%Y-%m-%d", time.gmtime())
    gestartet = 0
    for adr in motor._nutzer():
        if gestartet >= 2:
            break
        try:
            if not motor.zustand(adr)["prefs"].get("pf_on"):
                continue
            h = _verlauf(adr)
            if h and h[-1].get("d") == heute:
                continue
            if "snap:" + adr in _LAEUFT:
                continue
            _LAEUFT.add("snap:" + adr)
            threading.Thread(target=_schnappschuss, args=(adr,), daemon=True).start()
            gestartet += 1
        except Exception:
            continue


# ── Karte (seit 2.77.4) ──────────────────────────────────────────────────
# Die Uebersicht kommt als feste Karte aus dem Code, nicht als Fliesstext des Modells: immer gleich aufgebaut,
# Zahlen sauber ausgerichtet, nichts erfunden. Das Modell schreibt danach nur noch ein bis zwei Saetze.

def _usd(v):
    v = float(v or 0)
    if v >= 1e6:
        return f"${v / 1e6:,.2f}M"
    return f"${v:,.2f}" if v < 10_000 else f"${v:,.0f}"


def _menge(v):
    v = float(v or 0)
    if v >= 1e9:
        return f"{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v:,.0f}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def _pct(v):
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f}%"


def karte(out, html=True):
    """Portfolio als Telegram-Karte. out = ansehen(...)."""
    import html as _h
    e = (lambda x: _h.escape(str(x))) if html else str
    b = (lambda x: f"<b>{e(x)}</b>") if html else (lambda x: str(x))
    c = (lambda x: f"<code>{e(x)}</code>") if html else (lambda x: str(x))
    z = [f"💼 {b('Your portfolio')} · {b(_usd(out.get('total_usd')))}"]
    teile = []
    if out.get("since_last_snapshot_pct") is not None:
        teile.append(f"{'🟢' if out['since_last_snapshot_pct'] >= 0 else '🔴'} {_pct(out['since_last_snapshot_pct'])} since yesterday")
    if out.get("week_pct") is not None:
        teile.append(f"{_pct(out['week_pct'])} this week")
    if teile:
        z.append(" · ".join(teile))
    nw = len(out.get("by_wallet") or [])
    z.append(f"{nw} wallet{'s' if nw != 1 else ''}")
    z += ["", b("By coin")]
    for x in (out.get("combined") or [])[:12]:
        zeile = f"{b(x['coin'])}  {_usd(x['value_usd'])}"
        if x.get("share_pct") is not None:
            zeile += f" · {x['share_pct']:.1f}%"
        z.append(zeile)
        info = [_menge(x.get("amount"))]
        if (x.get("in_wallets") or 0) > 1:
            info.append(f"{x['in_wallets']} wallets")
        if x.get("pnl_pct") is not None:
            info.append(f"{'🟢' if x['pnl_pct'] >= 0 else '🔴'} {_pct(x['pnl_pct'])} vs buy ({'+' if x['pnl_usd'] >= 0 else '−'}{_usd(abs(x['pnl_usd']))})")
        elif x.get("price_change_since_last_snapshot_pct") is not None:
            info.append(f"{_pct(x['price_change_since_last_snapshot_pct'])} since yesterday")
        elif x.get("change_24h_pct") is not None:
            info.append(f"{_pct(x['change_24h_pct'])} 24h")
        if x.get("stock_token"):
            info.append("stock token")
        z.append("└ " + e(" · ".join(info)))
    kl = out.get("small_positions")
    if kl:
        z.append(e(f"+ {kl['count']} small position{'s' if kl['count'] != 1 else ''} ({_usd(kl['value_usd'])})"))
    z += ["", b("By wallet")]
    for w in out.get("by_wallet") or []:
        z.append(f"{c(w.get('short'))}  {b(_usd(w.get('value_usd')))}")
        pos = [f"{p['coin']} {_usd(p['value_usd'])}" for p in (w.get("positions") or [])[:5]]
        if pos:
            z.append("└ " + e(" · ".join(pos)))
        elif not w.get("value_usd"):
            z.append("└ " + e("nothing with a price"))
    hinweise = []
    for h in out.get("held_without_price") or []:
        hinweise.append("Held, price not readable right now: " + h.split(": ", 1)[-1].replace(" (held, value not counted)", ""))
    if out.get("not_included"):
        hinweise.append(out["not_included"].split(". Say")[0] + ".")
    if out.get("not_read"):
        hinweise.append(out["not_read"].split(". Say")[0] + ".")
    if out.get("history"):
        hinweise.append("Change since yesterday shows from tomorrow.")
    if str(out.get("cost_basis") or "").startswith("being"):
        hinweise.append("Gain and loss per coin follow in a few minutes.")
    if hinweise:
        z.append("")
        z += ["ℹ️ " + e(h) for h in hinweise]
    return "\n".join(z)
