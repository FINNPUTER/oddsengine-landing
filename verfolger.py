"""
verfolger.py: Geldspur ueber viele Hops und Chains, seit 2.63.1. Grundlage fuer Wallet-Tracing, Source of Funds und Steuer.

Warum: Am 29.10.2026 (Binance-Abfluesse) fand die alte Spur fuer eine Wallet mit Milliarden-Bewegungen nichts. Sie las nur
native Ueberweisungen der letzten 150 Transaktionen, ohne interne Transfers, ohne Tokens, ohne Paging, nur eine Chain und
nur einen Hop. Dieses Modul macht, was dort von Hand noetig war:

  bewegungen(chain, w)        alle Transfers einer Wallet: nativ, intern (aus Vertraegen), Tokens. Mit Paging,
                              ohne gefaelschte Tokens, mit Dollarwert, so weit bekannt.
  aktive_ketten(w)            auf welchen EVM-Chains die Adresse aktiv ist (Nonce und Guthaben direkt von der Chain,
                              Explorer-Zaehler hinken bei neuen Wallets hinterher)
  art(chain, a)               was eine Gegenstelle ist: Boerse, Bridge, Mixer, DEX/Swap, Lending, Vertrag, Wallet
  vorwaerts(adr, ...)         wohin das Geld ging: Hop fuer Hop bis zu Boerse, Bridge, Mixer oder ruhender Wallet.
                              Swaps, Lending, Freeze-Status, Endbestaende, und eine ehrliche Luckenliste.
  rueckwaerts(adr, ...)       woher das Geld kam (Source of Funds): bis zur Boerse oder Bridge, eigene Wallets als Umbuchung

Alles nur lesend, nur oeffentliche Daten. Keine Aussage, wer hinter einer Wallet steckt.
"""
import os
import re
import time

from logger import log

EVM_KETTEN = ("ethereum", "arbitrum", "base", "optimism", "polygon", "bsc", "avalanche", "robinhood")
NATIV = {"bsc": "BNB", "avalanche": "AVAX", "polygon": "POL"}
EXPLORER = {"ethereum": "https://etherscan.io", "arbitrum": "https://arbiscan.io", "base": "https://basescan.org",
            "optimism": "https://optimistic.etherscan.io", "polygon": "https://polygonscan.com", "bsc": "https://bscscan.com",
            "avalanche": "https://snowtrace.io", "robinhood": "https://robinhoodchain.blockscout.com"}
NAME_KETTE = {"ethereum": "Ethereum", "arbitrum": "Arbitrum", "base": "Base", "optimism": "Optimism", "polygon": "Polygon",
              "bsc": "BNB Chain", "avalanche": "Avalanche", "robinhood": "Robinhood Chain"}
# Stablecoins, die der Herausgeber sperren kann: (Vertrag, Funktion zum Abfragen der Sperre)
SPERRBAR = {
    "ethereum": [("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", "0xe47d6060"),
                 ("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0xfe575a87"),
                 ("RLUSD", "0x8292bb45bf1ee4d140127049757c2e0ff06317ed", "0xbc8c4b4f")],
    "arbitrum": [("USDT", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "0xe47d6060"),
                 ("USDC", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "0xfe575a87")],
    "base": [("USDC", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "0xfe575a87")],
    "avalanche": [("USDt", "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7", "0xe47d6060"),
                  ("USDC", "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e", "0xfe575a87")],
}
_DEX = re.compile(r"uniswap|pool|router|swap|settlement|reactor|aggregator|1inch|paraswap|velora|cow|0x |pair|curve|"
                  r"balancer|sushi|pancake|kyber|odos|bebop|hashflow|executor|snwap|adapter|zeroex|lifi|jupiter", re.I)
_LENDING = re.compile(r"aave|compound|morpho|spark|euler|lending|wrappedtokengateway|pool v3", re.I)
_MIXER = re.compile(r"tornado|mixer|railgun|sinbad|blender", re.I)
_BRIDGE = re.compile(r"bridge|portal|across|stargate|wormhole|layerzero|hop protocol|synapse|orbiter|relay\.link|"
                     r"inbox|l1.?gateway|canonical", re.I)
_BOERSE = re.compile(r"binance|coinbase|okx|kraken|bybit|bitget|kucoin|gate\.io|\bgate\b|mexc|htx|huobi|crypto\.com|"
                     r"bitfinex|bitstamp|gemini|upbit|bithumb|exchange|hot wallet|deposit", re.I)
_KONTO = re.compile(r"safe|smart account|delegator|multisig|wallet", re.I)
_C = {}


def _cache(k, sek, f):
    v = _C.get(k)
    if v and time.time() - v[0] < sek:
        return v[1]
    r = f()
    _C[k] = (time.time(), r)
    return r


def _kurz(a):
    a = str(a or "")
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def link(chain, art, a):
    b = EXPLORER.get(chain)
    return f"{b}/{'tx' if art == 'tx' else 'address'}/{a}" if b and a else None


def _rpc(chain, m, p):
    import scan
    return scan._evm(chain, m, p)


# ── Daten ────────────────────────────────────────────────────────────────

def _seiten(chain, action, w, max_n, seit=None):
    """Neueste zuerst, Seiten zu 1000, bis max_n oder bis vor 'seit' (Unix-Zeit).
    -> (zeilen, status) mit status True (alles gelesen), "cut" (sehr lange Geschichte, aelteste fehlen) oder
    "down" (Explorer hat nicht geantwortet, auch nach Wiederholung). Die Unterscheidung steht so im Bericht."""
    import intel
    out = []
    for seite in range(1, max_n // 1000 + 2):
        r = None
        for versuch in range(2):
            d = intel._bs(chain, "/api", {"module": "account", "action": action, "address": w, "sort": "desc",
                                          "page": seite, "offset": 1000}) or {}
            r = d.get("result") if isinstance(d.get("result"), list) else None
            if r is not None:
                break
            time.sleep(3)
        if r is None:
            return out, "down"
        out += r
        if seit and r and int(r[-1].get("timeStamp") or 0) < seit:
            return [x for x in out if int(x.get("timeStamp") or 0) >= seit], True
        if len(r) < 1000:
            return out, True
        if len(out) >= max_n:
            return out, "cut"
    return out, "cut"


def _schlechter(a, b):
    rang = {True: 0, "cut": 1, "down": 2}
    return a if rang.get(a, 2) >= rang.get(b, 2) else b


def _preis_nativ(chain):
    try:
        import motor
        return _cache(("pn", chain), 300, lambda: (motor.preis(NATIV.get(chain, "ETH")) or {}).get("usd")) or 0
    except Exception:
        return 0


def _preis_token(chain, token):
    try:
        import scan
        return _cache(("pt", chain, token), 600, lambda: (scan.zahlen(chain, token) or {}).get("price")) or 0
    except Exception:
        return 0


_STABIL = {"USDT", "USDC", "USDT0", "USDC.E", "DAI", "USDE", "USD1", "RLUSD", "FDUSD", "PYUSD", "USDS", "TUSD", "USDG", "USDT.E"}
_DS_ID = {"ethereum": "ethereum", "arbitrum": "arbitrum", "base": "base", "optimism": "optimism", "polygon": "polygon",
          "bsc": "bsc", "avalanche": "avalanche", "robinhood": "robinhood"}


def _stabil_echt(chain, sym, token):
    """Nur echte Stablecoin-Vertraege bekommen ohne Kurs 1 Dollar, nie eine Faelschung mit gleichem Namen."""
    import fluss
    s = str(sym or "").upper()
    if s not in _STABIL:
        return False
    echt = set((fluss._ECHT.get(chain) or {}).get(s) or set()) | {v for n, v, _ in SPERRBAR.get(chain, []) if n.upper() == s}
    try:
        import spaeher
        o = (spaeher.OFFIZIELL.get(chain) or {}).get(s)
        if o:
            echt.add(o.lower())
    except Exception:
        pass
    return str(token).lower() in echt


def _preise_batch(chain, tokens):
    """Dollarpreise vieler Tokens auf einmal (DexScreener, 30 je Abfrage, Paar mit der meisten Liquiditaet). -> {token: preis}"""
    import requests
    out, offen = {}, []
    for t in tokens:
        v = _C.get(("pt", chain, t))
        if v and time.time() - v[0] < 600:
            out[t] = v[1]
        else:
            offen.append(t)
    dsid = _DS_ID.get(chain)
    for i in range(0, len(offen), 30):
        teil = offen[i:i + 30]
        try:
            d = requests.get(f"https://api.dexscreener.com/tokens/v1/{dsid}/" + ",".join(teil),
                             headers={"User-Agent": "Mozilla/5.0 oddsengine"}, timeout=15).json()
        except Exception:
            d = []
        best = {}
        for p in d if isinstance(d, list) else []:
            t = str((p.get("baseToken") or {}).get("address") or "").lower()
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if t in teil and p.get("priceUsd") and liq >= 20000 and liq > best.get(t, (0, 0))[0]:
                best[t] = (liq, float(p["priceUsd"]))
        for t in teil:
            pr = best[t][1] if t in best else None
            _C[("pt", chain, t)] = (time.time(), pr)
            out[t] = pr
    return out


def bewegungen(chain, w, max_n=5000, seit=None, preise=True):
    """-> (liste, vollstaendig). Eintrag: hash, ts, von, an, asset, token (None = nativ), menge, usd, intern"""
    import fluss
    w = w.lower()
    out, voll = [], True
    nat = NATIV.get(chain, "ETH")
    for action in ("txlist", "txlistinternal", "tokentx"):
        try:
            rows, ok = _seiten(chain, action, w, max_n, seit)
        except Exception as e:
            log.debug(f"verfolger {chain} {action}: {e}")
            rows, ok = [], "down"
        voll = _schlechter(voll, ok)
        for x in rows:
            if str(x.get("isError") or "0") != "0":
                continue
            von, an = str(x.get("from") or "").lower(), str(x.get("to") or "").lower()
            if w not in (von, an):
                continue
            if action == "tokentx":
                sym, tok = x.get("tokenSymbol") or "token", str(x.get("contractAddress") or "").lower()
                if fluss.falsch(sym, chain, tok):
                    continue
                try:
                    menge = int(x.get("value") or 0) / 10 ** int(x.get("tokenDecimal") or 18)
                except Exception:
                    continue
            else:
                sym, tok = nat, None
                menge = int(x.get("value") or 0) / 1e18
            if menge <= 0:
                continue
            out.append({"hash": x.get("hash") or x.get("transactionHash"), "ts": int(x.get("timeStamp") or 0), "von": von,
                        "an": an, "asset": sym, "token": tok, "menge": menge, "intern": action == "txlistinternal", "usd": 0.0})
    if preise:
        px = _preise_batch(chain, {it["token"] for it in out if it["token"]})
        pn = _preis_nativ(chain)
        for it in out:
            p = pn if not it["token"] else px.get(it["token"])
            # Seit 2.82.10: ein echter Stablecoin ist immer 1 Dollar, auch wenn ein duenner Pool etwas anderes sagt
            if it["token"] and _stabil_echt(chain, it["asset"], it["token"]):
                p = 1.0
            it["usd"] = (p or 0) * it["menge"]
    out.sort(key=lambda x: -x["ts"])
    return out, voll


def aktive_ketten(w):
    """EVM-Chains, auf denen die Adresse etwas getan hat oder etwas haelt (direkt von der Chain)."""
    w = w.lower()
    out = []
    for ch in EVM_KETTEN:
        try:
            n = int(_rpc(ch, "eth_getTransactionCount", [w, "latest"]), 16)
            b = int(_rpc(ch, "eth_getBalance", [w, "latest"]), 16)
        except Exception:
            # Chain nicht erreichbar: lieber mitlesen als still auslassen
            out.append({"chain": ch, "nonce": None, "native": None, "unknown": True})
            continue
        if n or b > 10 ** 15:
            out.append({"chain": ch, "nonce": n, "native": b / 1e18})
            continue
        # Nur empfangen (Nonce 0, kein Guthaben): ueber den Explorer pruefen, ob Tokens kamen
        try:
            import intel
            d = intel._bs(ch, "/api", {"module": "account", "action": "tokentx", "address": w, "page": 1, "offset": 1}) or {}
            if isinstance(d.get("result"), list) and d["result"]:
                out.append({"chain": ch, "nonce": 0, "native": 0.0})
        except Exception:
            pass
    return out


def art(chain, a):
    """-> {"kind": exchange|bridge|mixer|dex|lending|burn|contract|wallet|service, "name", "source"}"""
    a = str(a or "").lower()
    if a in ("0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"):
        return {"kind": "burn", "name": "burn address", "source": "standard"}

    def hol():
        nm = {}
        try:
            import spur
            nm = dict(spur.name(chain, a)) if chain in ("robinhood", "base", "ethereum", "arbitrum", "optimism", "polygon") else {}
        except Exception:
            nm = {}
        k, n = nm.get("kind") or "wallet", nm.get("name") or ""
        if _MIXER.search(n):
            k = "mixer"
        elif k in ("exchange", "bridge", "burn", "locker"):
            pass
        elif _BRIDGE.search(n):
            k = "bridge"
        elif _LENDING.search(n):
            k = "lending"
        elif k == "pool" or _DEX.search(n):
            k = "dex"
        elif _BOERSE.search(n) and k != "contract":
            k = "exchange"
        elif k == "contract" and _KONTO.search(n):
            k = "wallet"      # Safe-Multisig oder Smart Account: haelt Geld wie eine Wallet, also weiter verfolgen
        if k == "wallet" and not n:
            # Ohne Explorer-Daten (z. B. Avalanche): Vertrag oder Wallet ueber die Chain
            try:
                import codeart
                c = codeart.norm(_rpc(chain, "eth_getCode", [a, "latest"]))
                if c not in ("0x", "0x0", None) and not str(c).startswith("0xef0100"):
                    k = "contract"
            except Exception:
                pass
        return {"kind": k, "name": n or None, "source": nm.get("source")}
    return _cache(("art", chain, a), 1800, hol)


def gesperrt(chain, w):
    """Welche sperrbaren Stablecoins der Herausgeber fuer diese Adresse gesperrt hat -> ["USDT", ...]"""
    out = []
    for sym, vertrag, sel in SPERRBAR.get(chain, []):
        try:
            r = _rpc(chain, "eth_call", [{"to": vertrag, "data": sel + w.lower()[2:].rjust(64, "0")}, "latest"])
            if r and int(r, 16):
                out.append(sym)
        except Exception:
            continue
    return out


def bestand(chain, w):
    """Aktueller Bestand: nativ (Chain) und die groessten Tokens (Explorer, wo es geht). -> {"native", "tokens":[(sym,usd,menge)], "usd"}"""
    import intel
    nat = 0.0
    try:
        nat = int(_rpc(chain, "eth_getBalance", [w, "latest"]), 16) / 1e18
    except Exception:
        pass
    usd = nat * _preis_nativ(chain)
    toks = []
    if chain in intel.BLOCKSCOUT:
        d = intel._bs(chain, f"/api/v2/addresses/{w}/token-balances")
        for b in d if isinstance(d, list) else []:
            try:
                t = b.get("token") or {}
                m = int(b.get("value") or 0) / 10 ** int(t.get("decimals") or 18)
                u = m * float(t.get("exchange_rate") or 0)
                import fluss
                if m > 0 and not fluss.falsch(t.get("symbol"), chain, t.get("address_hash") or t.get("address")):
                    toks.append((t.get("symbol") or "token", u, m))
            except Exception:
                continue
    toks.sort(key=lambda x: -x[1])
    usd += sum(u for _, u, _ in toks)
    return {"native": nat, "native_sym": NATIV.get(chain, "ETH"), "tokens": toks[:8], "usd": usd}


# ── Vorwaerts: wohin ging das Geld ───────────────────────────────────────

ENDE = ("exchange", "bridge", "mixer", "burn", "service", "locker")


def _fmt(menge, sym):
    if menge >= 1e6:
        return f"{menge / 1e6:,.2f}M {sym}"
    if menge >= 1000:
        return f"{menge:,.0f} {sym}"
    return f"{menge:,.4g} {sym}"


def _hop_grenze(usd, max_hops):
    """Wie tief ein Betrag verfolgt wird. Grosse Summen werden oft ueber viele Zwischenwallets geschoben,
    kleine Reste nicht endlos. Seit 2.63.1 (vorher feste 6 Hops, die Binance-Kette ueber 0x897C brauchte 8+)."""
    if usd >= 10_000_000:
        return max_hops
    if usd >= 1_000_000:
        return min(max_hops, 9)
    if usd >= 100_000:
        return min(max_hops, 7)
    return min(max_hops, 5)


def _luecken_text(luecken, n=25):
    """Wichtigste Luecken zuerst (nach Betrag), gleiche Texte nur einmal."""
    gesehen, out = set(), []
    for _, t in sorted(luecken, key=lambda x: -x[0]):
        if t not in gesehen:
            gesehen.add(t)
            out.append(t)
    rest = len(gesehen) - n if len(gesehen) > n else 0
    return out[:n] + ([f"{rest} smaller gap(s) not listed."] if rest else [])


def vorwaerts(adr, chain=None, max_hops=12, max_wallets=80, budget_s=300, seit=None, min_anteil=0.002, min_usd=1000):
    """Folgt dem Geld vorwaerts. -> dict mit wallets, kanten, enden, swaps, luecken. chain=None: alle aktiven EVM-Chains.
    Groesste Betraege zuerst (Prioritaetsschlange), Hop-Tiefe nach Betrag (_hop_grenze)."""
    import heapq
    t0 = time.time()
    start = str(adr or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", start):
        return {"error": "Give me an EVM wallet address (0x...). Solana and Bitcoin are not covered by the deep trace yet."}
    ketten = [chain] if chain else [k["chain"] for k in aktive_ketten(start)] or ["ethereum"]
    wallets, kanten, luecken = {}, [], []
    zaehler = 0
    queue = []
    for ch in ketten:
        heapq.heappush(queue, (-float("inf"), zaehler, ch, start, 0))
        zaehler += 1
    gesehen = set()
    while queue:
        if time.time() - t0 > budget_s:
            offen = sorted(queue)[:6]
            luecken.append((-offen[0][0] if offen else 0, f"Time limit reached, {len(queue)} wallet(s) not opened yet, biggest: "
                            + ", ".join(f"{_kurz(w)} on {NAME_KETTE.get(c, c)}" + ("" if u == -float("inf") else f" (${-u / 1e6:,.1f}M)") for u, _, c, w, _ in offen)))
            break
        neg, _, ch, w, hop = heapq.heappop(queue)
        wert = -neg
        if (ch, w) in gesehen:
            continue
        if len(gesehen) >= max_wallets:
            luecken.append((wert, f"Stopped after {max_wallets} wallets, {len(queue) + 1} more not opened "
                                  f"(biggest left: {_kurz(w)} on {NAME_KETTE.get(ch, ch)}{'' if wert == float('inf') else f', ${wert / 1e6:,.1f}M'})."))
            break
        gesehen.add((ch, w))
        bew, voll = bewegungen(ch, w, seit=seit)
        if voll == "down":
            luecken.append((wert if wert != float("inf") else 1e15,
                            f"{_kurz(w)} on {NAME_KETTE.get(ch, ch)}: the explorer did not answer, this wallet could not be read. Run again later."))
        elif voll == "cut":
            luecken.append((wert if wert != float("inf") else 1e14,
                            f"{_kurz(w)} on {NAME_KETTE.get(ch, ch)}: very long history, only the newest transfers were read."))
        raus = [b for b in bew if b["von"] == w]
        rein = [b for b in bew if b["an"] == w]
        summe_raus = sum(b["usd"] for b in raus)
        schwelle = max(min_usd, summe_raus * min_anteil)
        # nach Gegenstelle und Asset buendeln
        gruppen = {}
        for b in raus:
            g = gruppen.setdefault((b["an"], b["asset"], b["token"]), {"menge": 0.0, "usd": 0.0, "tx": b["hash"], "n": 0, "ts": b["ts"]})
            g["menge"] += b["menge"]
            g["usd"] += b["usd"]
            g["n"] += 1
        swaps_rein = {}
        for b in rein:
            if b["von"] != w and b["usd"] >= schwelle / 10 and art(ch, b["von"])["kind"] in ("dex", "lending"):
                k = swaps_rein.setdefault(b["asset"], [0.0, 0.0])
                k[0] += b["menge"]
                k[1] += b["usd"]
        wallets[(ch, w)] = {"chain": ch, "wallet": w, "hop": hop, "in_usd": sum(b["usd"] for b in rein),
                            "out_usd": summe_raus, "got_from_swaps_or_loans": {a: v for a, v in swaps_rein.items() if v[1] >= schwelle}}
        # Seit 2.63.1: Durchlaufstellen mit viel fremdem Geld (Market Maker, Solver, MEV-Bots, Boersen ohne Etikett)
        # nicht weiterverfolgen. Dort mischt sich das Geld mit dem von tausenden anderen, und ihre Empfaenger haben mit
        # der Spur nichts zu tun (gefunden an der Binance-Spur vom 30.10.2026: ein MEV-Bot fuehrte zu einem Market
        # Maker mit 3,7 Mrd. USDC, dessen Kunden sonst als "Empfaenger" in der Karte gelandet waeren).
        gegenstellen = len({b["an"] for b in raus})
        if hop > 0 and wert != float("inf") and (voll == "cut" or (summe_raus > max(10 * wert, 1_000_000) and gegenstellen > 30)):
            wallets[(ch, w)]["durchlauf"] = True
            luecken.append((wert, f"{_kurz(w)} on {NAME_KETTE.get(ch, ch)} received {_usd(wert)} from the trail but is a busy wallet "
                                  f"(sent {_usd(summe_raus)} to {gegenstellen} addresses, likely a market maker, bot or unlabeled service). "
                                  "Money mixes with others there, so it is not followed further."))
            continue
        unbewertet = 0
        for (an, asset, tok), g in sorted(gruppen.items(), key=lambda x: -x[1]["usd"]):
            if g["usd"] < schwelle:
                if g["usd"] == 0 and tok:
                    unbewertet += 1
                continue
            a = art(ch, an)
            kante = {"chain": ch, "from": w, "to": an, "asset": asset, "amount": g["menge"], "usd": round(g["usd"]),
                     "transfers": g["n"], "proof": link(ch, "tx", g["tx"]), "to_kind": a["kind"], "to_name": a.get("name"), "hop": hop}
            kanten.append(kante)
            if an == w or a["kind"] != "wallet" and a["kind"] != "contract":
                continue
            if a["kind"] == "contract":
                luecken.append((g["usd"], f"{_fmt(g['menge'], asset)} (${g['usd'] / 1e6:,.1f}M) went into an unnamed contract "
                                          f"{_kurz(an)} on {NAME_KETTE.get(ch, ch)}, not followed (could be a swap service, a vault or a smart account)."))
            elif hop + 1 < _hop_grenze(g["usd"], max_hops):
                heapq.heappush(queue, (-g["usd"], zaehler, ch, an, hop + 1))
                zaehler += 1
            else:
                luecken.append((g["usd"], f"{_kurz(an)} on {NAME_KETTE.get(ch, ch)} received {_fmt(g['menge'], asset)} "
                                          f"(${g['usd'] / 1e6:,.2f}M) at hop {hop + 1}: depth limit for this amount, not opened."))
        if unbewertet:
            luecken.append((0, f"{_kurz(w)} on {NAME_KETTE.get(ch, ch)}: {unbewertet} token transfer group(s) without a known price were not followed."))
    # Endpunkte: Wallets, die selbst nichts Nennenswertes weitergegeben haben, plus Boersen, Bridges, Mixer
    enden = []
    # Seit 2.63.1: Endbestaende mit eigenem Zeitbudget (vorher brach diese Schleife ab, wenn die Spur ihr Budget
    # aufgebraucht hatte, und "Still holding" blieb leer). Groesste Eingaenge zuerst, dazu jede Wallet, die nur
    # empfangen hat und nicht geoeffnet wurde, weil sie am Ende der Spur liegt.
    for k in kanten:
        if k["to_kind"] == "wallet" and (k["chain"], k["to"]) not in wallets:
            wallets[(k["chain"], k["to"])] = {"chain": k["chain"], "wallet": k["to"], "hop": k["hop"] + 1,
                                               "in_usd": k["usd"], "out_usd": 0, "got_from_swaps_or_loans": {}, "offen": True}
    ende_bis = time.time() + int(os.getenv("SPUR_BESTAND_S", "150"))
    for (ch, w), info in sorted(wallets.items(), key=lambda x: -(x[1].get("in_usd") or 0)):
        if info.get("durchlauf"):
            continue
        if time.time() > ende_bis:
            luecken.append((1e12, "Time limit for current balances reached, the smallest wallets were not checked."))
            break
        try:
            b = bestand(ch, w)
            sp = gesperrt(ch, w)
        except Exception as ex:
            log.debug(f"verfolger bestand {ch} {w}: {ex}")
            continue
        if b["usd"] >= min_usd or sp:
            enden.append({"chain": ch, "wallet": w, "holds_usd": round(b["usd"]),
                          "holds": [_fmt(b["native"], b["native_sym"])] + [f"{_fmt(m, s)} (${u / 1e6:,.1f}M)" for s, u, m in b["tokens"][:4] if u >= 100000],
                          "frozen_by_issuer": sp, "link": link(ch, "address", w), "hop": info["hop"]})
    for k in kanten:
        if k["to_kind"] in ENDE:
            enden.append({"chain": k["chain"], "wallet": k["to"], "deposit_to": k["to_kind"], "name": k.get("to_name"),
                          "received": _fmt(k["amount"], k["asset"]), "usd": k["usd"], "proof": k["proof"]})
    enden.sort(key=lambda e: -(e.get("holds_usd") or e.get("usd") or 0))
    ins_dienst = [e for e in enden if e.get("deposit_to")]
    return {"start": start, "chains": [NAME_KETTE.get(c, c) for c in ketten], "wallets_opened": len([1 for x in wallets.values() if not x.get("offen")]),
            "edges": sorted(kanten, key=lambda k: (k["hop"], -k["usd"]))[:150], "endpoints": enden[:30],
            "reached_exchange_bridge_or_mixer": ins_dienst[:15], "gaps": _luecken_text(luecken),
            "took_s": round(time.time() - t0), "as_of": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}


# ── Rueckwaerts: woher kam das Geld (Source of Funds) ────────────────────

def rueckwaerts(adr, chain=None, eigene=None, max_hops=5, max_wallets=30, budget_s=150, min_anteil=0.02, min_usd=500):
    """Woher kam das Geld dieser Wallet? Folgt groesseren Eingaengen zurueck bis Boerse, Bridge oder Hop-Grenze.
    eigene: weitere Wallets des Nutzers, Eingaenge von dort sind Umbuchungen (werden weiter zurueckverfolgt)."""
    t0 = time.time()
    start = str(adr or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", start):
        return {"error": "Give me an EVM wallet address (0x...)."}
    eigen = {start} | {str(x).lower() for x in (eigene or []) if str(x).startswith("0x")}
    ketten = [chain] if chain else [k["chain"] for k in aktive_ketten(start)] or ["ethereum"]
    queue = [(ch, start, 0) for ch in ketten]
    gesehen, kanten, quellen, luecken = set(), [], [], []
    while queue:
        if time.time() - t0 > budget_s:
            luecken.append((1e13, f"Time limit reached, {len(queue)} source wallet(s) were not traced further back."))
            break
        ch, w, hop = queue.pop(0)
        if (ch, w) in gesehen or len(gesehen) >= max_wallets:
            continue
        gesehen.add((ch, w))
        bew, voll = bewegungen(ch, w, max_n=20000)
        if voll == "down":
            luecken.append((1e15, f"{_kurz(w)} on {NAME_KETTE.get(ch, ch)}: the explorer did not answer, this wallet could not be read. Run again later."))
        elif voll == "cut":
            luecken.append((1e14, f"{_kurz(w)} on {NAME_KETTE.get(ch, ch)}: very long history (over 20,000 transfers), the oldest were not read."))
        rein = [b for b in bew if b["an"] == w and b["von"] != w]
        summe = sum(b["usd"] for b in rein)
        schwelle = max(min_usd, summe * min_anteil)
        gruppen = {}
        for b in rein:
            g = gruppen.setdefault((b["von"], b["asset"]), {"menge": 0.0, "usd": 0.0, "tx": b["hash"], "erst": b["ts"], "n": 0})
            g["menge"] += b["menge"]
            g["usd"] += b["usd"]
            g["n"] += 1
            g["erst"] = min(g["erst"], b["ts"])
        # Seit 2.78.0: die allererste Zahlung wird immer verfolgt, auch wenn sie klein ist (Gas-Geld fuer einen Dev).
        # Vorher fiel sie unter min_usd heraus, und die Spur eines frisch gefuellten Deployers endete ohne Geldgeber.
        erste_k = min(gruppen, key=lambda k_: gruppen[k_]["erst"] or 1e18) if gruppen else None
        for (von, asset), g in sorted(gruppen.items(), key=lambda x: -x[1]["usd"]):
            if g["usd"] < schwelle and (von, asset) != erste_k:
                continue
            a = art(ch, von)
            herkunft = ("own wallet" if von in eigen else a["kind"])
            kanten.append({"chain": ch, "to": w, "from": von, "asset": asset, "amount": g["menge"], "usd": round(g["usd"]),
                           "first_funding": (von, asset) == erste_k,
                           "first": time.strftime("%Y-%m-%d", time.gmtime(g["erst"])) if g["erst"] else None,
                           "from_kind": herkunft, "from_name": a.get("name"), "proof": link(ch, "tx", g["tx"]), "hop": hop})
            if a["kind"] in ("exchange", "bridge", "mixer", "service"):
                quellen.append({"chain": ch, "source": a.get("name") or a["kind"], "kind": a["kind"], "usd": round(g["usd"]),
                                "into": w, "proof": link(ch, "tx", g["tx"])})
            elif a["kind"] in ("dex", "lending"):
                quellen.append({"chain": ch, "source": a.get("name") or a["kind"], "kind": "swap or loan", "usd": round(g["usd"]),
                                "into": w, "proof": link(ch, "tx", g["tx"]),
                                "note": "bought with or borrowed against assets this wallet already had: those came in through the other inflows"})
            elif hop + 1 < max_hops:
                queue.append((ch, von, hop + 1))
            else:
                luecken.append((g["usd"], f"{_kurz(von)} sent {_fmt(g['menge'], asset)} (${g['usd']:,.0f}): hop limit, its own source not traced."))
    return {"wallet": start, "chains": [NAME_KETTE.get(c, c) for c in ketten], "flows": kanten[:80], "origins": quellen[:30],
            "gaps": _luecken_text(luecken), "took_s": round(time.time() - t0), "as_of": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}


# ── Bericht als Telegram-Text (HTML) ─────────────────────────────────────

def _usd(v):
    v = float(v or 0)
    if v >= 1e9:
        return f"${v / 1e9:,.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:,.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:,.0f}K"
    return f"${v:,.0f}"


def bericht(erg, rueck=False, max_zeichen=3800):
    """Ergebnis von vorwaerts/rueckwaerts als kurzer Telegram-Text (HTML). Jede Zeile mit Beleg-Link, Luecken offen."""
    from html import escape as e
    if not erg or erg.get("error"):
        return e(str((erg or {}).get("error") or "The trace did not finish."))
    start = erg.get("start") or erg.get("wallet")
    z = [f"<b>{'Where the money came from' if rueck else 'Where the money went'}</b>",
         f"<code>{e(start)}</code>",
         f"{e(', '.join(erg.get('chains') or []))} · as of {e(erg.get('as_of', ''))}", ""]
    if rueck:
        # seit 2.78.0: der Weg des ersten Geldes, Schritt fuer Schritt (Gas-Geld eines Devs zeigt oft den echten Geldgeber)
        pfad = sorted([k for k in erg.get("flows") or [] if k.get("first_funding")], key=lambda k: k.get("hop") or 0)
        if pfad:
            z.append("<b>First funding, step by step</b>")
            for k in pfad[:6]:
                ziel = f' <a href="{e(k["proof"])}">proof</a>' if k.get("proof") else ""
                wer = e(k.get("from_name") or k["from"][:6] + "…" + k["from"][-4:])
                z.append(f"• {_kurz(k['to'])} got {e(_fmt(k['amount'], k['asset']))} from {wer} ({e(str(k.get('from_kind') or 'wallet'))})"
                         + (f" on {e(k['first'])}" if k.get("first") else "") + ziel)
            z.append("")
        q = erg.get("origins") or []
        if q:
            z.append("<b>Origins</b>")
            for o in q[:10]:
                ziel = f'<a href="{e(o["proof"])}">proof</a>' if o.get("proof") else ""
                z.append(f"• {_usd(o['usd'])} from {e(str(o['source']))} ({e(o['kind'])}) into {_kurz(o['into'])} {ziel}")
        else:
            z.append("No exchange, bridge or swap was reached within the traced hops.")
        eigen = [k for k in erg.get("flows") or [] if k.get("from_kind") == "own wallet"]
        if eigen:
            z.append(f"\n{len(eigen)} transfer(s) between your own wallets, treated as moves, not income.")
    else:
        enden = [x for x in erg.get("endpoints") or [] if x.get("holds_usd") is not None]
        if enden:
            z.append("<b>Still holding</b>")
            for x in enden[:10]:
                frz = f" · {'+'.join(x['frozen_by_issuer'])} frozen by issuer" if x.get("frozen_by_issuer") else ""
                was = ", ".join(x.get("holds") or [])[:80]
                z.append(f'• <a href="{e(x["link"] or "")}">{_kurz(x["wallet"])}</a> {e(NAME_KETTE.get(x["chain"], x["chain"]))}: '
                         f"{_usd(x['holds_usd'])} ({e(was)}){e(frz)} · hop {x.get('hop')}")
        dienst = erg.get("reached_exchange_bridge_or_mixer") or []
        if dienst:
            z.append("\n<b>Reached exchanges, bridges, mixers</b>")
            for x in dienst[:8]:
                z.append(f'• {e(x.get("received") or "")} ({_usd(x.get("usd"))}) to {e(x.get("name") or x["deposit_to"])} '
                         f'{_kurz(x["wallet"])} <a href="{e(x.get("proof") or "")}">proof</a>')
        z.append(f"\n{erg.get('wallets_opened', 0)} wallets opened, {len(erg.get('edges') or [])} transfers followed, "
                 f"{erg.get('took_s', 0)}s.")
    g = erg.get("gaps") or []
    if g:
        z.append("\n<b>Open gaps (not guessed, listed)</b>")
        for t in g[:8]:
            z.append(f"• {e(t)}")
        if len(g) > 8:
            z.append(f"• {len(g) - 8} more in the full report")
    else:
        z.append("\nNo open gaps within the traced range.")
    z.append("\nOn-chain data only. No claim about who is behind any wallet.")
    out = "\n".join(z)
    while len(out) > max_zeichen and len(z) > 6:
        z.pop(-3)
        out = "\n".join(z)
    return out
