"""
spaeher.py: Wallet Tracker von Your Engine, seit 2.50.5.

"Beobachte dieses Wallet und sag mir jede Bewegung": fuer FREMDE Wallets (Dev, Wal, Freund), nicht zur Sicherheit
des eigenen Wallets (das ist Wallet Guard). Gemeldet wird jede Bewegung, auch eingehend:
  gekauft / verkauft / getauscht (gleiche Transaktion, Tokens rein und raus, oder vom Wallet selbst ausgeloest)
  erhalten (von wem) / geschickt (an wen)
  ETH rein oder raus ohne Token-Bewegung (ab TRACK_MIN_ETH)
Gelesen wird je Kette einmal fuer alle beobachteten Wallets (eth_getLogs Transfer, Absender ODER Empfaenger),
dazu der ETH-Stand. Ketten wie Wallet Guard (Robinhood, Arbitrum, plus Base/ETH/BNB mit eigenem RPC). Keine KI,
keine Kosten fuer den Nutzer.

Schalter:
  TRACK_POLL_SEC     wie oft gelesen wird (Standard 30)
  TRACK_MAX          Wallets je Nutzer (Standard 10)
  TRACK_MIN_ETH      kleinste gemeldete ETH-Bewegung (Standard 0.01)
  TRACK_MAX_HOUR     Meldungen je Nutzer und Stunde (Standard 40)
"""
import json
import os
import re
import time

import storage
from logger import log

T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TAKT = int(os.getenv("TRACK_POLL_SEC", "30"))
MAX = int(os.getenv("TRACK_MAX", "10"))
MIN_ETH = float(os.getenv("TRACK_MIN_ETH", "0.01"))
MAX_STUNDE = int(os.getenv("TRACK_MAX_HOUR", "40"))
MAX_CREW = int(os.getenv("TRACK_MAX_CREW", "40"))      # seit 2.63: Wallets aus "Track this coin's wallets", eigenes Kontingent
_NAMEN = {}                                             # Wallet -> Label ueber alle Nutzer, je Runde neu
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
WSOL = "So11111111111111111111111111111111111111112"
MIN_SOL = float(os.getenv("TRACK_MIN_SOL", "0.05"))
_LETZT = {"t": 0}
_ZAEHLER = {}
_BEREICH = {}                                           # Kette -> (von, bis) der letzten Runde, fuer die ETH-Herkunft
BRUECKE_TYPEN = {"0x64", "0x65", "0x66", "0x68", "0x69"}  # Arbitrum-Orbit: Einzahlungen und Retryables von Ethereum (L1)
ETH_SCAN_MAX = int(os.getenv("TRACK_ETH_SCAN_BLOCKS", "3000"))


def _ketten():
    import wache
    return list(wache.KETTEN_EVM)


def _kurz(a):
    a = str(a)
    return a[:6] + "…" + a[-4:]


def _pad(a):
    return "0x" + "0" * 24 + a[2:].lower()


# ── Liste je Nutzer ──────────────────────────────────────────────────────

def _liste(adr):
    import motor
    z = motor.zustand(adr)
    return z, z["prefs"].setdefault("tracked", [])


def dazu(adr, wallet, label=None, crew=None):
    import motor
    w = str(wallet or "").strip()
    if _EVM.match(w):
        w = w.lower()
    elif not _SOL.match(w):
        return {"ok": False, "error": "That is not a wallet address. Send a 0x wallet or a Solana wallet."}
    z, liste = _liste(adr)
    alt = next((x for x in liste if x["wallet"] == w), None)
    if alt:
        if label:
            alt["label"] = str(label)[:30]
            motor._zustand_speichern(adr, z)
        return {"ok": True, "already": True, "wallet": w, "label": alt.get("label"), "chains": _ketten(),
                "tracks": "every buy, sell, swap, incoming and outgoing transfer, and ETH or SOL moves"}
    if crew:
        if sum(1 for x in liste if x.get("crew")) >= MAX_CREW:
            return {"ok": False, "error": f"You already track {MAX_CREW} coin wallets. Stop one coin first."}
    elif sum(1 for x in liste if not x.get("crew")) >= MAX:
        return {"ok": False, "error": f"You already track {MAX} wallets. Remove one first."}
    e_ = {"wallet": w, "label": (str(label)[:30] if label else None), "since": int(time.time())}
    if crew:
        e_["crew"] = crew
    liste.append(e_)
    motor._zustand_speichern(adr, z)
    try:
        if w.startswith("0x"):
            storage.kv_set("track_eth_" + w, json.dumps(_eth_staende(w)))
        else:
            import intel
            s = intel._sol("getSignaturesForAddress", [w, {"limit": 1}]) or []
            storage.kv_set("track_sig_" + w, s[0]["signature"] if s else "-")
    except Exception:
        pass
    return {"ok": True, "wallet": w, "label": label, "chains": ["solana"] if not w.startswith("0x") else _ketten(),
            "tracks": "every buy, sell, swap, incoming and outgoing transfer, and ETH or SOL moves, as a Telegram message within about a minute",
            "note": "Only new moves from now on. Use wallet_activity for what happened before."}


def weg(adr, wallet):
    import motor
    t = str(wallet or "").strip()
    t = t.lower() if t.startswith("0x") else t
    z, liste = _liste(adr)
    vorher = len(liste)
    z["prefs"]["tracked"] = [x for x in liste if x["wallet"] != t and str(x.get("label") or "").lower() != t.lower()
                             and not x["wallet"].endswith(t.replace("…", "").replace("...", "")[-4:] if len(t) >= 4 else "#")]
    motor._zustand_speichern(adr, z)
    return {"ok": True, "removed": vorher - len(z["prefs"]["tracked"])}


def crew_dazu(adr, chain, token):
    """Seit 2.63: alle wichtigen Wallets eines Coins auf einmal beobachten, mit Namen: Dev, Bundle 1..n,
    Dev-Nebenwallets (bekamen Tokens direkt vom Dev) und Trail-Enden (am Ende einer Weiterleitungskette).
    Jede Bewegung kommt dann mit diesem Namen, und Gegenstellen, die ebenfalls beobachtet werden, heissen auch so."""
    import intel
    import muster
    erg = intel.aus_cache(chain, token) or {}
    if not erg.get("measured"):
        return {"ok": False, "error": "The deep scan of this coin is not in yet. Try again in a minute."}
    tk = muster._k(chain, token)
    sym = (muster._coins(chain, [tk]).get(tk) or {}).get("symbol") or muster._kurz(tk)
    wer = []
    if erg.get("dev"):
        wer.append((erg["dev"], f"${sym} dev"))
    bundles = sorted([b for b in erg.get("buyers") or [] if b.get("bundled")], key=lambda b: -(b.get("pct") or 0))
    for i, b in enumerate(bundles[:6], 1):
        wer.append((b["wallet"], f"${sym} bundle {i}"))
    fw = erg.get("forwards") or {}
    dev = str(erg.get("dev") or "").lower()
    seiten = [f for f in fw.get("list") or [] if f.get("src") == "dev" or str(f.get("from") or "").lower() == dev]
    for i, f in enumerate(seiten[:4], 1):
        wer.append((f["to"], f"${sym} dev side {i}"))
    enden = [c["path"][-1] for c in fw.get("chains") or [] if (c.get("end_holds_pct") or 0) > 0]
    for i, w in enumerate(list(dict.fromkeys(enden))[:4], 1):
        wer.append((w, f"${sym} trail {i}"))
    gesehen, neu, schon = set(), [], 0
    for w, lab in wer:
        k = muster._k(chain, w)
        if k in gesehen:
            continue
        gesehen.add(k)
        r = dazu(adr, w, lab, crew=f"{chain}:{tk}")
        if r.get("ok") and not r.get("already"):
            neu.append(lab)
        elif r.get("already"):
            schon += 1
        elif r.get("error"):
            return {"ok": bool(neu), "added": neu, "error": r["error"]}
    if not neu and not schon:
        return {"ok": False, "error": "No dev, bundle or side wallets known for this coin."}
    return {"ok": True, "coin": f"${sym}", "added": neu, "already": schon,
            "note": "Every move of these wallets now comes as a message with its name (dev, bundle 1, dev side 1, trail 1). "
                    "When one sends to another wallet we watch, that one is named too. ETH or SOL sent out shows where it went."}


def crew_weg(adr, chain, token):
    import motor
    import muster
    k = f"{chain}:{muster._k(chain, token)}"
    z, liste = _liste(adr)
    vorher = len(liste)
    z["prefs"]["tracked"] = [x for x in liste if x.get("crew") != k]
    motor._zustand_speichern(adr, z)
    return {"ok": True, "removed": vorher - len(z["prefs"]["tracked"])}


def _name(a):
    """Gegenstelle mit Label, wenn wir sie beobachten (z. B. '$LOBIN dev side 1 (0x3b…9d51)')."""
    if not a:
        return "?"
    k = a.lower() if str(a).startswith("0x") else a
    return f"{_NAMEN[k]} ({_kurz(a)})" if _NAMEN.get(k) else _kurz(a)


def _ziel_eth(chain, w, delta):
    """Wohin ging das ETH (oder woher kam es)? Letzte passende Transaktion ueber Blockscout. -> Text oder None"""
    try:
        import intel
        d = intel._bs(chain, f"/api/v2/addresses/{w}/transactions", {"filter": "from" if delta < 0 else "to"}) or {}
        for it in (d.get("items") or [])[:5]:
            if int(it.get("value") or 0) <= 0:
                continue
            g = (it.get("to") if delta < 0 else it.get("from")) or {}
            a = g.get("hash")
            if not a:
                continue
            name = g.get("name")
            try:
                import kartei
                kz = kartei.art(chain, a) or {}
                name = name or kz.get("name")
            except Exception:
                pass
            return (_name(a) + (f", {name}" if name and not _NAMEN.get(a.lower()) else "")), it.get("hash")
    except Exception as e:
        log.debug(f"spaeher ziel {w[:8]}: {e}")
    return None


def _batch(chain, calls):
    import intel
    try:
        return intel._evm_batch(chain, calls) or [None] * len(calls)
    except Exception:
        return [None] * len(calls)


def _eth_txs(chain, ws):
    """Seit 2.63.1: alle Transaktionen der letzten Runde, die eine beobachtete Wallet beruehren (von oder an).
    Liest die Bloecke direkt, weil Blockscout automatische Abfragen oft blockt. -> {wallet: [tx]}"""
    a, b = _BEREICH.get(chain) or (None, None)
    if not a or b < a:
        return {}
    a = max(a, b - ETH_SCAN_MAX + 1)
    ws = {w.lower() for w in ws}
    out = {}
    nums = list(range(a, b + 1))
    bloecke = _batch(chain, [("eth_getBlockByNumber", [hex(n), True]) for n in nums])
    for blk in bloecke:
        for tx in (blk or {}).get("transactions") or []:
            if not isinstance(tx, dict):
                continue
            f, t = str(tx.get("from") or "").lower(), str(tx.get("to") or "").lower()
            for w in (f, t):
                if w in ws:
                    out.setdefault(w, []).append(tx)
    return out


def _wer(chain, rpc, a, mit_fakten=True):
    """Gegenstelle so genau wie moeglich: Crew-Label, bekannte Boerse/Bruecke, sonst kurze Fakten zur Wallet."""
    k = a.lower()
    if _NAMEN.get(k):
        return f"{_NAMEN[k]} ({_kurz(a)})"
    try:
        import kartei
        kz = kartei.art(chain, a) or {}
        if kz.get("name") or kz.get("kind") not in (None, "wallet"):
            return f"{kz.get('name') or kz.get('kind')} ({_kurz(a)})"
    except Exception:
        pass
    if not mit_fakten:
        return _kurz(a)
    try:
        code = rpc("eth_getCode", [a, "latest"])
        import codeart
        if codeart.norm(code) not in ("0x", "0x0", None):
            return f"a contract ({_kurz(a)})"
    except Exception:
        pass
    teile = []
    try:
        n = int(rpc("eth_getTransactionCount", [a, "latest"]), 16)
        teile.append("fresh wallet, no transactions before" if n <= 1 else f"{n:,} transactions")
    except Exception:
        pass
    try:
        bal = int(rpc("eth_getBalance", [a, "latest"]), 16) / 1e18
        teile.append(("holds " + f"{bal:.3f}".rstrip("0").rstrip(".") + " ETH") if bal >= 0.001 else "now empty")
    except Exception:
        pass
    return f"{_kurz(a)}" + (f" ({', '.join(teile)})" if teile else "")


# ── seit 2.66.5: Liquiditaets-Positionen (Uniswap v3/v4) erkennen ──
T_V4_MODIFY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
T_V4_INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
T_V3_DEC = "0x26f6a048ee9138f2c0ce266f322cb99228e8d619ae2bff30c67f8dcf9d2377b4"
T_V3_INC = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
T_V3_COLLECT = "0x40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01"
_POOL = {}


def _s256(x):
    v = int(x, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def _v4_paar(chain, rpc, manager, pid):
    """Die zwei Werte eines Uniswap-v4-Pools aus dem Initialize-Ereignis (einmal je Pool, dann gemerkt)."""
    import wache
    k = f"{chain}:{pid}"
    if k in _POOL:
        return _POOL[k]
    try:
        alt = storage.kv_get("pool_" + k)
        if alt:
            _POOL[k] = alt
            return alt
    except Exception:
        pass
    paar = None
    try:
        hoch = int(rpc("eth_blockNumber", []), 16)
        bis = hoch
        while bis > 0 and paar is None:
            von = max(0, bis - 9_999_999)
            r = rpc("eth_getLogs", [{"address": manager, "fromBlock": hex(von), "toBlock": hex(bis), "topics": [T_V4_INIT, pid]}]) or []
            if r:
                syms = []
                for t in r[0]["topics"][2:4]:
                    a = "0x" + t[-40:]
                    syms.append("ETH" if int(a, 16) == 0 else "$" + wache._symbol(chain, a)[0])
                paar = "/".join(syms)
            bis = von - 1
    except Exception as e:
        log.debug(f"spaeher v4 paar: {e}")
    _POOL[k] = paar
    if paar:
        try:
            storage.kv_set("pool_" + k, paar)
        except Exception:
            pass
    return paar


def _lp_text(chain, rpc, rc, eth_txt, rein):
    """Liquiditaet raus, rein oder umgeschichtet? -> Zeile oder None."""
    plus = minus = 0
    paar = None
    v3 = {"dec": False, "inc": False, "col": False}
    for l in rc.get("logs") or []:
        tp = l.get("topics") or []
        if not tp:
            continue
        if tp[0] == T_V4_MODIFY and len(tp) >= 2:
            d = (l.get("data") or "0x")[2:]
            if len(d) >= 192:
                dl = _s256(d[128:192])
                plus += dl > 0
                minus += dl < 0
                if paar is None:
                    paar = _v4_paar(chain, rpc, l["address"], tp[1])
        elif tp[0] == T_V3_DEC:
            v3["dec"] = True
        elif tp[0] == T_V3_INC:
            v3["inc"] = True
        elif tp[0] == T_V3_COLLECT:
            v3["col"] = True
    wo = f" in the {paar} pool" if paar else ""
    if plus or minus:
        dex = "Uniswap v4"
    elif any(v3.values()):
        dex = "Uniswap v3"
        plus, minus = int(v3["inc"]), int(v3["dec"])
        if not plus and not minus and v3["col"]:
            return f"🌾 Collected LP fees{wo} ({dex}): {eth_txt} came in", None
    else:
        return None
    zurueck = f", {eth_txt} came back to the wallet" if rein else f", {eth_txt} went in"
    if plus and minus:
        return f"🔁 Moved an LP position{wo} ({dex}): closed the old range and opened a new one{zurueck}. Fees and leftovers of the old position pay out like this.", None
    if minus:
        return f"💧 Took liquidity out{wo} ({dex}){zurueck}", None
    return f"💧 Added liquidity{wo} ({dex}){zurueck}", None


def _eigene_nach(chain, rpc, w, bloecke=None):
    """Transaktionen, die die Wallet selbst geschickt hat, aus den letzten Bloecken ueber eth_getLogs
    (jede Spur mit der Adresse in einem Topic). Fuer den Fall, dass die Runde die Transaktion nicht gesehen hat."""
    bloecke = bloecke or int(os.getenv("TRACK_LOOKBACK_BLOCKS", "12000"))
    try:
        hoch = int(rpc("eth_blockNumber", []), 16)
    except Exception:
        return []
    pad = "0x" + "0" * 24 + w[2:]
    hashes = []
    for pos in range(1, 4):
        try:
            r = rpc("eth_getLogs", [{"fromBlock": hex(max(0, hoch - bloecke)), "toBlock": hex(hoch), "topics": [None] * pos + [pad]}]) or []
        except Exception:
            continue
        for l in r:
            h = l.get("transactionHash")
            if h and h not in hashes:
                hashes.append(h)
    out = []
    for h in hashes[-8:]:
        try:
            tx = rpc("eth_getTransactionByHash", [h]) or {}
        except Exception:
            continue
        if str(tx.get("from") or "").lower() == w:
            out.append(tx)
    return out


def _eth_detail(chain, rpc, w, delta, txs, alle):
    """-> (zeile, tx) mit Herkunft (bei Eingang) oder Ziel (bei Ausgang) des ETH, oder None."""
    import wache
    w = w.lower()
    eth = lambda v: f"{v / 1e18:.4f}".rstrip("0").rstrip(".") + " ETH"
    rein = delta > 0
    direkt = [t for t in txs if int(t.get("value") or "0x0", 16) > 0
              and str(t.get("to" if rein else "from") or "").lower() == w]
    if direkt:
        direkt.sort(key=lambda t: -int(t.get("value") or "0x0", 16))
        t = direkt[0]
        g = str(t.get("from" if rein else "to") or "")
        if rein and t.get("type") in BRUECKE_TYPEN:
            wer = "the bridge from Ethereum"
        else:
            wer = _wer(chain, rpc, g)
        zeile = (f"📥 Received {eth(abs(delta))} from {wer}" if rein else f"📤 Sent {eth(abs(delta))} to {wer}")
        if len(direkt) > 1:
            n_g = len({str(x.get('from' if rein else 'to')).lower() for x in direkt})
            zeile += f"\nIn {len(direkt)} transfers" + (f" from {n_g} wallets" if rein and n_g > 1 else f" to {n_g} wallets" if n_g > 1 else "")
        # gleiche Gegenstelle bei anderen beobachteten Wallets in dieser Runde (z. B. ein Geldgeber fuer mehrere Bundles)
        auch = []
        for w2, l2 in alle.items():
            if w2 == w:
                continue
            for x in l2:
                if str(x.get("from" if rein else "to") or "").lower() == g.lower() and str(x.get("to" if rein else "from") or "").lower() == w2 \
                        and int(x.get("value") or "0x0", 16) > 0:
                    auch.append(f"{_NAMEN.get(w2) or _kurz(w2)} ({eth(int(x['value'], 16))})")
                    break
        if auch:
            zeile += (f"\n🔗 The same wallet also funded {', '.join(auch[:4])} just now" if rein
                      else f"\n🔗 {', '.join(auch[:4])} sent to the same wallet just now")
        return zeile, t.get("hash")
    # Kein direkter ETH-Transfer: eigener Vertragsaufruf (Swap, Auszahlung) oder interne Zahlung eines Vertrags
    eigene = [t for t in txs if str(t.get("from") or "").lower() == w]
    if not eigene:
        eigene = _eigene_nach(chain, rpc, w)  # seit 2.66.5: auch kurz vor der Runde
    for t in eigene[::-1]:
        try:
            rc = rpc("eth_getTransactionReceipt", [t["hash"]]) or {}
        except Exception:
            continue
        raus, reinkam = [], []
        for l in rc.get("logs") or []:
            tp = l.get("topics") or []
            if len(tp) < 3 or tp[0] != T_TRANSFER:
                continue
            von, an = ("0x" + tp[1][-40:]).lower(), ("0x" + tp[2][-40:]).lower()
            roh = int(l.get("data") or "0x0", 16)
            if von == w:
                raus.append((l["address"].lower(), roh))
            elif an == w:
                reinkam.append((l["address"].lower(), roh))
        lp = _lp_text(chain, rpc, rc, eth(abs(delta)), rein)
        if lp:
            return lp[0], t.get("hash")
        ziel = _wer(chain, rpc, str(t.get("to") or ""), mit_fakten=False) if t.get("to") else "a contract"

        def txt(lst):
            out = []
            for tok, roh in lst[:3]:
                sym, dez = wache._symbol(chain, tok)
                out.append(f"{wache._menge(roh, dez)} ${sym}")
            return ", ".join(out)
        if rein and raus:
            return f"💸 Sold {txt(raus)} for {eth(delta)} (swap through {ziel})", t.get("hash")
        if not rein and reinkam:
            return f"🛒 Bought {txt(reinkam)} for {eth(-delta)} (swap through {ziel})", t.get("hash")
        if rein:
            return f"📥 Received {eth(delta)} from {ziel} (a payout after a call by this wallet)", t.get("hash")
        return f"📤 Sent {eth(-delta)} into {ziel}", t.get("hash")
    if rein:
        return f"📥 Received {eth(delta)} through a contract (internal transfer, no direct transaction to this wallet)", None
    return None


def zeigen(adr):
    _, liste = _liste(adr)
    return {"wallets": [{"wallet": x["wallet"], "label": x.get("label")} for x in liste], "max": MAX, "chains": _ketten()}


def _alle():
    """{wallet: [nutzer]} ueber alle Nutzer."""
    import motor
    out = {}
    for adr in motor._nutzer():
        try:
            for x in motor.zustand(adr)["prefs"].get("tracked") or []:
                out.setdefault(x["wallet"], []).append((adr, x.get("label")))
                if x.get("label"):
                    _NAMEN.setdefault(x["wallet"], x["label"])
        except Exception:
            continue
    return out


# ── Lesen und einordnen ──────────────────────────────────────────────────

def _eth_staende(w):
    import wache
    out = {}
    for ch in _ketten():
        try:
            out[ch] = int(wache._evm_rpc(ch)("eth_getBalance", [w, "latest"]), 16)
        except Exception:
            pass
    return out


def einordnen(w, logs, tx_von):
    """Logs einer Transaktion fuer ein Wallet -> (art, rein, raus). rein/raus: [(token, roh, gegenueber)]"""
    rein = [("0x" + l["topics"][1][-40:], l) for l in logs if ("0x" + l["topics"][2][-40:]).lower() == w]
    raus = [("0x" + l["topics"][2][-40:], l) for l in logs if ("0x" + l["topics"][1][-40:]).lower() == w]
    r = [(l["address"].lower(), int(l.get("data") or "0x0", 16), g) for g, l in rein]
    o = [(l["address"].lower(), int(l.get("data") or "0x0", 16), g) for g, l in raus]
    selbst = str(tx_von or "").lower() == w
    if r and o:
        art = "swap"
    elif r:
        art = "buy" if selbst else "in"
    elif o:
        art = "sell" if selbst and _an_vertrag(o) else "out"
    else:
        art = None
    return art, r, o


_VERTRAG = {}


def _an_vertrag(o):
    return any(_VERTRAG.get(g.lower()) for _, _, g in o)


# Seit 2.61.6: echte Vertraege der grossen Stablecoins und Wrapped-ETH. Ein Token mit gleichem Symbol, aber anderem
# Vertrag ist eine Faelschung (Spam-Airdrop, oft als Koeder fuer Phishing-Seiten).
OFFIZIELL = {
    "robinhood": {"USDG": "0x5fc5360d0400a0fd4f2af552add042d716f1d168"},
    "base": {"USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "WETH": "0x4200000000000000000000000000000000000006"},
    "ethereum": {"USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                 "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"},
    "arbitrum": {"USDC": "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "USDT": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
                 "WETH": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"},
}


_STABIL_SYM = {"USDG", "USDC", "USDT", "USDT0", "USDC.E", "DAI", "PYUSD", "USD1", "USDE", "USDS"}


def _preis(chain, token):
    """Seit 2.82.10: echte Stablecoins (offizielle Adresse) sind immer 1 Dollar. Vorher kam fuer USDG auf Robinhood
    aus einem duennen Pool ein Kurs von rund 64 Dollar, und ein Tausch von 100 USDG stand als 6.390 Dollar da."""
    off = OFFIZIELL.get(chain) or {}
    t = str(token).lower()
    for sym, adr in off.items():
        if str(adr).lower() == t and str(sym).upper() in _STABIL_SYM:
            return 1.0
    try:
        import scan
        p = scan.zahlen(chain, token).get("price")
    except Exception:
        return None
    try:
        import wache
        sym = str(wache._symbol(chain, token)[0] or "").upper().lstrip("$")
        if sym in _STABIL_SYM and p and not (0.9 <= float(p) <= 1.1):
            return None   # ein Stablecoin-Name mit unsinnigem Kurs: lieber kein Dollarwert als ein falscher
    except Exception:
        pass
    return p


def faelschung(chain, token, sym):
    """True, wenn ein Token sich als bekannter Stablecoin oder WETH ausgibt, aber nicht der echte Vertrag ist.
    Seit 2.62.7 auch: Namen aus fremden Buchstaben oder mit unsichtbaren Zeichen (z. B. ein falsches "ETH")."""
    try:
        import fluss
        if fluss.falsch(sym, chain, token):
            return True
    except Exception:
        pass
    echt = (OFFIZIELL.get(chain) or {}).get(str(sym or "").upper().lstrip("$"))
    if chain == "robinhood" and not echt and str(sym or "").upper() in ("USDC", "USDT", "WETH"):
        try:
            import onchain
            if str(sym).upper() == "WETH":
                echt = onchain.WETH.lower()
        except Exception:
            pass
    return bool(echt) and token.lower() != echt.lower()


def _text(chain, w, label, art, r, o, tx, eth_delta, preise):
    import scan
    import wache
    name = f"{label} ({_kurz(w)})" if label else _kurz(w)

    def teil(t, roh):
        sym, dez = wache._symbol(chain, t)
        menge = roh / 10 ** dez
        px = preise.get(t)
        usd = f" (≈ ${menge * px:,.0f})" if px and menge * px >= 1 else ""
        return f"{wache._menge(roh, dez)} ${sym}{usd}"
    eth = f"{abs(eth_delta) / 1e18:.4f}".rstrip("0").rstrip(".") + " ETH" if eth_delta else None
    if art == "swap":
        zeile = f"🔄 Swapped {', '.join(teil(t, x) for t, x, _ in o)} for {', '.join(teil(t, x) for t, x, _ in r)}"
    elif art == "buy":
        zeile = f"🛒 Bought {', '.join(teil(t, x) for t, x, _ in r)}" + (f" for {eth}" if eth and eth_delta < 0 else "")
    elif art == "sell":
        zeile = f"💸 Sold {', '.join(teil(t, x) for t, x, _ in o)}" + (f" for {eth}" if eth and eth_delta > 0 else "")
    elif art == "in":
        import wache as _w
        fake = [t for t, _, _ in r if faelschung(chain, t, _w._symbol(chain, t)[0])]
        if fake and len(fake) == len(r):
            sym = _w._symbol(chain, fake[0])[0]
            zeile = (f"⚠️ Spam airdrop: fake ${sym} from {_kurz(r[0][2])}. This is NOT the real ${sym}, it has no value. "
                     "Don't try to sell or claim it, those sites drain wallets. Just ignore it.")
        else:
            zeile = f"📥 Received {', '.join(teil(t, x) for t, x, _ in r)} from {_name(r[0][2])}"
    elif art == "out":
        # Seit 2.62.7: gefaelschte Tokens schreiben erfundene Transfers "aus" fremden Wallets ins Log (Address Poisoning).
        # Die Wallet hat nichts gesendet, also keine Meldung.
        import wache as _w
        if all(faelschung(chain, t, _w._symbol(chain, t)[0]) for t, _, _ in o):
            return None
        zeile = f"📤 Sent {', '.join(teil(t, x) for t, x, _ in o)} to {_name(o[0][2])}"
    elif art == "eth" and isinstance(r, tuple):
        # seit 2.63.1: Herkunft oder Ziel aus den Bloecken der Runde (Absender, Bruecke, Swap, Crew)
        zeile, tx = r[0], tx or r[1]
    elif art == "eth":
        # seit 2.63: wohin das ETH ging (oder woher es kam)
        ziel = _ziel_eth(chain, w, eth_delta)
        if ziel:
            tx = tx or ziel[1]
        zeile = (f"📥 Received {eth}" + (f" from {ziel[0]}" if ziel else "")) if eth_delta > 0 else \
                (f"📤 Sent {eth}" + (f" to {ziel[0]}" if ziel else ""))
    else:
        return None
    kette = {"robinhood": "Robinhood Chain", "arbitrum": "Arbitrum", "base": "Base", "ethereum": "Ethereum", "bsc": "BNB"}.get(chain, chain)
    link = scan.explorer(chain, "tx", tx) if tx else scan.explorer(chain, "address", w)
    return f"👀 Tracked wallet {name} · {kette}\n\n{zeile}" + (f"\n\n{link}" if link else "")


def _runde_kette(chain, wallets):
    import wache
    rpc = wache._evm_rpc(chain)
    top = int(rpc("eth_blockNumber", []), 16)
    k = "track_blk_" + chain
    try:
        von = int(storage.kv_get(k) or 0)
    except Exception:
        von = 0
    fen = wache._fenster(chain)
    if not von or top - von > fen * 6:
        von = top - min(fen, 200)
    if von >= top:
        return []
    logs = []
    ws = sorted(wallets)
    bis = min(top, von + fen * 4)
    a = von + 1
    while a <= bis:
        b = min(bis, a + fen - 1)
        for i in range(0, len(ws), 50):
            pads = [_pad(w) for w in ws[i:i + 50]]
            logs += rpc("eth_getLogs", [{"fromBlock": hex(a), "toBlock": hex(b), "topics": [T_TRANSFER, pads]}]) or []
            logs += rpc("eth_getLogs", [{"fromBlock": hex(a), "toBlock": hex(b), "topics": [T_TRANSFER, None, pads]}]) or []
        a = b + 1
    storage.kv_set(k, str(bis))
    _BEREICH[chain] = (von + 1, bis)
    return logs


def runde():
    import scan
    import wache
    import motor
    if time.time() - _LETZT["t"] < TAKT:
        return 0
    _LETZT["t"] = time.time()
    alle = _alle()
    if not alle:
        return 0
    gesendet = 0
    sol = {w: n for w, n in alle.items() if not w.startswith("0x")}
    if sol:
        try:
            gesendet += _runde_sol(sol)
        except Exception as e:
            log.debug(f"spaeher sol: {e}")
    beobachtet = {w: n for w, n in alle.items() if w.startswith("0x")}
    if not beobachtet:
        return gesendet
    stunde = int(time.time() // 3600)
    for chain in _ketten():
        try:
            logs = _runde_kette(chain, list(beobachtet))
        except Exception as e:
            log.debug(f"spaeher {chain}: {e}")
            continue
        rpc = wache._evm_rpc(chain)
        je_tx = {}
        for l in logs:
            if len(l.get("topics") or []) < 3:
                continue          # ERC-721 und Sonderfaelle ueberspringen
            je_tx.setdefault(l["transactionHash"], []).append(l)
        # ETH-Staende
        eth_neu = {}
        for w in beobachtet:
            try:
                eth_neu[w] = int(rpc("eth_getBalance", [w, "latest"]), 16)
            except Exception:
                pass
        preise = {}
        bewegt = set()
        meldungen = []
        for tx, ls in list(je_tx.items())[:60]:
            try:
                von = (rpc("eth_getTransactionByHash", [tx]) or {}).get("from")
            except Exception:
                von = None
            for w in beobachtet:
                if not any(w in (("0x" + l["topics"][1][-40:]).lower(), ("0x" + l["topics"][2][-40:]).lower()) for l in ls):
                    continue
                for l in ls:
                    g = ("0x" + l["topics"][2][-40:]).lower()
                    if ("0x" + l["topics"][1][-40:]).lower() == w and g not in _VERTRAG:
                        try:
                            import codeart
                            _VERTRAG[g] = codeart.norm(rpc("eth_getCode", [g, "latest"])) not in ("0x", "0x0", None)
                        except Exception:
                            _VERTRAG[g] = False
                art, r, o = einordnen(w, ls, von)
                if not art:
                    continue
                for t, _, _ in r + o:
                    if t not in preise:
                        preise[t] = _preis(chain, t)
                bewegt.add(w)
                alt = json.loads(storage.kv_get("track_eth_" + w) or "{}").get(chain)
                delta = (eth_neu.get(w) - alt) if (alt is not None and eth_neu.get(w) is not None) else 0
                meldungen.append((w, art, r, o, tx, delta if abs(delta) >= MIN_ETH * 1e18 else 0))
        # ETH ohne Token-Bewegung
        for w in beobachtet:
            if w in bewegt or w not in eth_neu:
                continue
            try:
                alt = json.loads(storage.kv_get("track_eth_" + w) or "{}").get(chain)
            except Exception:
                alt = None
            if alt is not None and abs(eth_neu[w] - alt) >= MIN_ETH * 1e18:
                meldungen.append((w, "eth", [], [], None, eth_neu[w] - alt))
        for w in beobachtet:
            if w in eth_neu:
                try:
                    st = json.loads(storage.kv_get("track_eth_" + w) or "{}")
                    st[chain] = eth_neu[w]
                    storage.kv_set("track_eth_" + w, json.dumps(st))
                except Exception:
                    pass
        nur_eth = [m for m in meldungen if m[1] == "eth"]
        if nur_eth:
            try:
                alle_tx = _eth_txs(chain, [m[0] for m in nur_eth])
            except Exception as e:
                log.debug(f"spaeher eth scan {chain}: {e}")
                alle_tx = {}
            neu = []
            for m in meldungen:
                if m[1] == "eth":
                    try:
                        d = _eth_detail(chain, rpc, m[0], m[5], alle_tx.get(m[0].lower()) or [], alle_tx)
                    except Exception as e:
                        log.debug(f"spaeher eth detail: {e}")
                        d = None
                    if d:
                        m = (m[0], "eth", d, [], d[1], m[5])
                neu.append(m)
            meldungen = neu
        for w, art, r, o, tx, delta in meldungen:
            # Seit 2.69.11: Kauf oder Bruecke ueber Relay auf eine andere Chain (z. B. Torch: von Robinhood direkt
            # auf Solana kaufen). Ohne das sah man nur "Sent 0.3 ETH to a contract".
            br = bruecke(tx) if tx and (art in ("eth", "out", "sell") or delta < 0) else None
            for adr, label in beobachtet[w]:
                zk = (adr, stunde)
                if _ZAEHLER.get(zk, 0) >= MAX_STUNDE or not motor._darf(adr):
                    continue
                t = _bruecke_text(adr, chain, w, label, br, tx) if br else _text(chain, w, label, art, r, o, tx, delta, preise)
                if not t:
                    continue
                try:
                    motor.senden(adr, t)
                    _ZAEHLER[zk] = _ZAEHLER.get(zk, 0) + 1
                    gesendet += 1
                except Exception as e:
                    log.debug(f"spaeher senden: {e}")
    return gesendet


# ── Solana ───────────────────────────────────────────────────────────────

def _schluessel(tx):
    ks = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k.get("pubkey") if isinstance(k, dict) else k for k in ks], [bool(k.get("signer")) if isinstance(k, dict) else i == 0 for i, k in enumerate(ks)]


def sol_einordnen(tx, w):
    """-> {"art", "rein": [(mint, menge)], "raus": [...], "sol": delta SOL, "gegen": adresse} oder None"""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return None
    keys, signer = _schluessel(tx)
    def stand(liste):
        d = {}
        for b in liste or []:
            amt = b.get("uiTokenAmount") or {}
            try:
                v = int(amt.get("amount") or 0) / 10 ** int(amt.get("decimals") or 0)
            except Exception:
                v = 0
            k = (b.get("owner"), b.get("mint"))
            d[k] = d.get(k, 0) + v
        return d
    vor, nach = stand(meta.get("preTokenBalances")), stand(meta.get("postTokenBalances"))
    delta = {}
    for k in set(vor) | set(nach):
        dv = nach.get(k, 0) - vor.get(k, 0)
        if abs(dv) > 0:
            delta[k] = dv
    sol = 0.0
    if w in keys:
        i = keys.index(w)
        try:
            sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
            if i == 0:
                sol += (meta.get("fee") or 0) / 1e9
        except Exception:
            sol = 0.0
    rein, raus = [], []
    for (owner, mint), dv in delta.items():
        if owner != w:
            continue
        if mint == WSOL:
            sol += dv
            continue
        (rein if dv > 0 else raus).append((mint, abs(dv)))
    selbst = w in keys and signer[keys.index(w)]
    klein = 0.003
    gegen = None
    if rein and raus:
        art = "swap"
    elif rein and sol < -klein:
        art = "buy"
    elif raus and sol > klein:
        art = "sell"
    elif rein:
        art = "buy" if selbst else "in"
        gegen = next((o for (o, m), dv in delta.items() if m == rein[0][0] and o != w and dv < 0), None)
    elif raus:
        art = "out"
        gegen = next((o for (o, m), dv in delta.items() if m == raus[0][0] and o != w and dv > 0), None)
    elif abs(sol) >= MIN_SOL:
        art = "sol"
        try:
            ds = [(keys[j], (meta["postBalances"][j] - meta["preBalances"][j])) for j in range(len(keys)) if keys[j] != w]
            ds = [x for x in ds if (x[1] < 0) == (sol > 0)]
            gegen = max(ds, key=lambda x: abs(x[1]))[0] if ds else None
        except Exception:
            gegen = None
    else:
        return None
    return {"art": art, "rein": rein, "raus": raus, "sol": round(sol, 6), "gegen": gegen}


def _sol_text(w, label, e, sig, preise):
    import wache
    name = f"{label} ({_kurz(w)})" if label else _kurz(w)

    def teil(liste):
        out = []
        for mint, menge in liste:
            sym = wache._mint_name(mint)
            sym = sym if sym.startswith("$") else "$" + sym
            px = preise.get(mint)
            usd = f" (≈ ${menge * px:,.0f})" if px and menge * px >= 1 else ""
            out.append((f"{menge:,.0f}" if menge >= 100 else f"{menge:,.4f}".rstrip("0").rstrip(".")) + f" {sym}{usd}")
        return ", ".join(out)
    s = abs(e["sol"])
    sol = (f"{s:,.3f}".rstrip("0").rstrip(".") + " SOL") if s >= 0.001 else None
    a = e["art"]
    if a == "swap":
        z = f"🔄 Swapped {teil(e['raus'])} for {teil(e['rein'])}"
    elif a == "buy":
        z = f"🛒 Bought {teil(e['rein'])}" + (f" for {sol}" if sol and e["sol"] < 0 else "")
    elif a == "sell":
        z = f"💸 Sold {teil(e['raus'])}" + (f" for {sol}" if sol and e["sol"] > 0 else "")
    elif a == "in":
        z = f"📥 Received {teil(e['rein'])}" + (f" from {_name(e['gegen'])}" if e.get("gegen") else "")
    elif a == "out":
        z = f"📤 Sent {teil(e['raus'])}" + (f" to {_name(e['gegen'])}" if e.get("gegen") else "")
    else:
        z = (f"📥 Received {sol}" + (f" from {_name(e['gegen'])}" if e.get("gegen") else "")) if e["sol"] > 0 else \
            (f"📤 Sent {sol}" + (f" to {_name(e['gegen'])}" if e.get("gegen") else ""))
    return f"👀 Tracked wallet {name} · Solana\n\n{z}\n\nhttps://solscan.io/tx/{sig}"


def _runde_sol(beobachtet):
    import intel
    import motor
    import scan
    gesendet, stunde, preise = 0, int(time.time() // 3600), {}
    for w, nutzer in list(beobachtet.items())[:60]:
        k = "track_sig_" + w
        letzte = storage.kv_get(k)
        opt = {"limit": 20}
        if letzte and letzte != "-":
            opt["until"] = letzte
        sigs = intel._sol("getSignaturesForAddress", [w, opt]) or []
        if not isinstance(sigs, list) or not sigs:
            if not letzte:
                storage.kv_set(k, "-")
            continue
        storage.kv_set(k, sigs[0]["signature"])
        if not letzte:
            continue          # erster Lauf: nur die Stelle merken
        neu = [s for s in sigs if not s.get("err")][:10][::-1]
        txs = intel._sol_batch([intel._tx(s["signature"]) for s in neu], 10)
        for s, tx in zip(neu, txs):
            if not isinstance(tx, dict):
                continue
            e = sol_einordnen(tx, w)
            if not e:
                continue
            for mint, _ in e["rein"] + e["raus"]:
                if mint not in preise:
                    try:
                        preise[mint] = scan.zahlen("solana", mint).get("price")
                    except Exception:
                        preise[mint] = None
            t = _sol_text(w, None, e, s["signature"], preise)
            for adr, label in nutzer:
                zk = (adr, stunde)
                if _ZAEHLER.get(zk, 0) >= MAX_STUNDE or not motor._darf(adr):
                    continue
                try:
                    motor.senden(adr, _sol_text(w, label, e, s["signature"], preise) if label else t)
                    _ZAEHLER[zk] = _ZAEHLER.get(zk, 0) + 1
                    gesendet += 1
                except Exception as ex:
                    log.debug(f"spaeher sol senden: {ex}")
    return gesendet


# ── Seit 2.69.11: Kaeufe und Bruecken ueber Relay auf andere Chains ──────────

import requests as _rq

RELAY_KETTEN = {792703809: "solana", 1: "ethereum", 8453: "base", 42161: "arbitrum", 56: "bsc", 4663: "robinhood",
                10: "optimism", 137: "polygon", 43114: "avalanche"}
RELAY_NAMEN = {"solana": "Solana", "ethereum": "Ethereum", "base": "Base", "arbitrum": "Arbitrum", "bsc": "BNB Chain",
               "robinhood": "Robinhood Chain", "optimism": "Optimism", "polygon": "Polygon", "avalanche": "Avalanche"}
_RELAY = {}
_NATIV_ADR = ("0x0000000000000000000000000000000000000000", "11111111111111111111111111111111",
              "So11111111111111111111111111111111111111112")


def bruecke(tx):
    """Relay-Auftrag zu einer Transaktion: was ging rein, was kam auf welcher Chain bei wem an. None, wenn keiner."""
    if not tx:
        return None
    if tx in _RELAY:
        return _RELAY[tx]
    erg = None
    try:
        d = _rq.get("https://api.relay.link/requests/v2", params={"hash": tx}, timeout=12).json()
        q = (d.get("requests") or [None])[0]
        if q and q.get("status") not in ("failure", "refund", "refunded"):
            md = (q.get("data") or {}).get("metadata") or {}
            ci, co = md.get("currencyIn") or {}, md.get("currencyOut") or {}
            cin, cout = ci.get("currency") or {}, co.get("currency") or {}
            ziel = RELAY_KETTEN.get(cout.get("chainId"))
            if ziel and cin.get("chainId") != cout.get("chainId"):
                aus = [x.get("hash") for x in (q.get("data") or {}).get("outTxs") or [] if x.get("hash")]
                erg = {"status": q.get("status"), "von_kette": RELAY_KETTEN.get(cin.get("chainId")), "kette": ziel,
                       "rein": ci.get("amountFormatted"), "rein_sym": cin.get("symbol"), "usd": ci.get("amountUsd"),
                       "raus": co.get("amountFormatted"), "raus_sym": cout.get("symbol"), "token": cout.get("address"),
                       "nativ": str(cout.get("address") or "") in _NATIV_ADR,
                       "empfaenger": q.get("recipient") or md.get("recipient"), "aus_tx": aus[0] if aus else None}
    except Exception as e:
        log.debug(f"relay {tx[:10]}: {e}")
        return None   # nicht merken, naechste Runde nochmal
    if len(_RELAY) > 2000:
        _RELAY.clear()
    _RELAY[tx] = erg
    return erg


def _zahl_kurz(x):
    try:
        v = float(x)
    except Exception:
        return str(x)
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _bruecke_text(adr, chain, w, label, b, tx):
    import scan
    name = f"{label} ({_kurz(w)})" if label else _kurz(w)
    von = RELAY_NAMEN.get(b.get("von_kette") or chain, chain)
    nach = RELAY_NAMEN.get(b["kette"], b["kette"])
    usd = ""
    try:
        usd = f" (≈ ${float(b.get('usd')):,.0f})" if b.get("usd") else ""
    except Exception:
        pass
    rein = f"{_zahl_kurz(b.get('rein'))} {b.get('rein_sym')}{usd}"
    ziel = b.get("empfaenger") or ""
    if b.get("nativ"):
        zeile = (f"🌉 Bridged {rein} from {von} to {nach}: {_zahl_kurz(b.get('raus'))} {b.get('raus_sym')} "
                 f"{'landed' if b.get('status') == 'success' else 'on its way'} in {_kurz(ziel)}.")
    else:
        zeile = (f"🛒 Bought {_zahl_kurz(b.get('raus'))} ${b.get('raus_sym')} on {nach} with {rein} from {von}, "
                 f"cross-chain. The tokens went to {_kurz(ziel)}.")
    # Ziel-Wallet gleich mitbeobachten, damit man sieht, was dort gekauft wird
    if ziel and ziel.lower() != w.lower() and (_SOL.match(ziel) or _EVM.match(ziel)):
        try:
            _, liste = _liste(adr)
            schon = any(x["wallet"] in (ziel, ziel.lower()) for x in liste)
            if not schon:
                r = dazu(adr, ziel, label=((label or _kurz(w)) + f" {nach}")[:30])
                if r.get("ok"):
                    if _SOL.match(ziel) and b.get("aus_tx"):
                        # ab der Bruecke lesen, sonst fehlt der Kauf direkt danach
                        storage.kv_set("track_sig_" + ziel, b["aus_tx"])
                    zeile += f"\n\nNow also following {_kurz(ziel)} on {nach}, so you see what it buys there."
                else:
                    zeile += f"\n\nSay 'track {ziel}' to see what it buys on {nach}."
        except Exception as e:
            log.debug(f"spaeher ziel: {e}")
    if b["kette"] == "solana" and b.get("aus_tx"):
        link = f"https://solscan.io/tx/{b['aus_tx']}"
    else:
        link = scan.explorer(chain, "tx", tx)
    return f"👀 Tracked wallet {name} · {von} → {nach}\n\n{zeile}" + (f"\n\n{link}" if link else "")
