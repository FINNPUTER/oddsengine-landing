"""
tape.py: Trade Tape. Jeder Kauf und Verkauf eines Coins, mit Wallet, Zeit, Kurs und Market Cap. Seit 2.62.

  lesen(chain, token)            alle Trades seit dem Launch (EVM: Swap-Events der Pools, Solana: Transaktionen)
  wallets(t)                     je Wallet: Einstieg (MC), Ausstieg, Kosten, Erloes, Gewinn, Bestand, geschenkt bekommen
  kopierer(t, fuehrer)           Wallets, die wiederholt kurz nach einem Wallet (z. B. dem Dev) dasselbe tun
  bericht(chain, token, ...)     das, was das Werkzeug trade_tape zurueckgibt

EVM (Robinhood, Base, Ethereum, Arbitrum, BNB): Swap-Events von Uniswap V2, V3 und V4 aus den Pools des Coins
(DexScreener), der Kaeufer oder Verkaeufer aus den Transfer-Logs derselben Transaktion. Der Kurs kommt aus dem
Swap selbst (Menge Coin gegen Menge Paarwaehrung), der USD-Wert der Paarwaehrung zur Stunde aus preise.quote_usd.
Solana: Signaturen des Mints und der Pools, jede Transaktion mit den Token- und SOL-Salden der Wallets.
Market Cap = Kurs mal Gesamtangebot (heute).
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from logger import log

V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V4 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
POOL_MANAGER = {"robinhood": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
                "ethereum": "0x000000000004444c5dc75cb358380d2e3de08a90",
                "base": "0x498581ff718922c3f8e6a244956af099b2652b2b",
                "arbitrum": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
                "bsc": "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df"}
NULL = "0x0000000000000000000000000000000000000000"
# Bekannte Pool-Autoritaeten auf Solana (halten die Vaults, sind keine Kaeufer)
SOL_POOLS = {"5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
             "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh", "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC"}
MAX_EVM = int(os.getenv("TAPE_MAX_TRADES", "20000"))
MAX_SOL = int(os.getenv("TAPE_SOL_MAX_TX", "4000"))
BUDGET = float(os.getenv("TAPE_BUDGET_SEC", "90"))
CACHE_SEK = int(os.getenv("TAPE_CACHE_SEC", "600"))

_CACHE = {}
_SPERREN = {}


def _signed(hexwort):
    v = int(hexwort, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def _worte(data):
    d = str(data or "0x")[2:]
    return [d[i:i + 64] for i in range(0, len(d), 64)]


def _paare(chain, token):
    """Eigene Pools des Coins auf der Chain: [{adr, v, quote, qsym}]"""
    import scan
    out = []
    for p in scan._paare(token) or []:
        if scan.DEX_ZU.get(p.get("chainId")) != chain:
            continue
        if str((p.get("baseToken") or {}).get("address", "")).lower() != str(token).lower() and chain != "solana":
            continue
        if chain == "solana" and (p.get("baseToken") or {}).get("address") != token:
            continue
        adr = p.get("pairAddress")
        if not adr:
            continue
        lab = [str(x).lower() for x in p.get("labels") or []]
        v = 4 if (len(adr) == 66 or "v4" in lab) else 3 if "v3" in lab else 2 if "v2" in lab else 0  # 0: unbekannt, V2 und V3 probieren
        q = p.get("quoteToken") or {}
        out.append({"adr": adr if chain in ("solana", "tron", "ton") else adr.lower(), "v": v, "quote": q.get("address"),
                    "qsym": q.get("symbol"), "dex": p.get("dexId"), "liq": scan._liq(p)})
    return out


# ── EVM ──────────────────────────────────────────────────────────────────

def _logs_bereich(chain, adresse, topics, von, bis, fenster):
    import intel
    fl = [(b, min(bis, b + fenster)) for b in range(von, bis + 1, fenster + 1)]

    def hol(ab):
        for versuch in range(6):
            try:
                r = intel._evm(chain, "eth_getLogs", [{"address": adresse, "fromBlock": hex(ab[0]), "toBlock": hex(ab[1]), "topics": topics}])
                if isinstance(r, list):
                    return r
            except Exception as e:
                if not intel._ist_429(e) and (versuch >= 1 or "narrow" in str(e) or "range" in str(e)):
                    # zu grosser Bereich: halbieren
                    if ab[1] - ab[0] > 1000:
                        m = (ab[0] + ab[1]) // 2
                        a, b = hol((ab[0], m)), hol((m + 1, ab[1]))
                        return (a or []) + (b or []) if a is not None and b is not None else None
            time.sleep(0.5 * (versuch + 1))
        return None
    with ThreadPoolExecutor(2 if chain == "robinhood" else 3) as ex:
        teile = list(ex.map(hol, fl))
    voll = all(t is not None for t in teile)
    return [l for t in teile if t for l in t], voll


def _start_block(chain, token, paare):
    import intel
    s, dev = intel._start_evm(chain, token)
    return s, dev


def _evm_lesen(chain, token):
    import intel
    import preise
    token = token.lower()
    t0 = time.time()
    top = int(intel._evm(chain, "eth_blockNumber", []), 16)
    paare = [p for p in _paare(chain, token) if p["v"] in (0, 2, 3, 4)]
    start, dev = _start_block(chain, token, paare)
    if start is None:
        # ohne bekannten Launch: das Fenster der Chain rueckwaerts (Robinhood 30 Tage, sonst kuerzer)
        start = max(0, top - {"robinhood": 26_000_000, "arbitrum": 10_000_000, "base": 1_300_000,
                              "ethereum": 220_000, "bsc": 900_000}.get(chain, 500_000))
    transfers, voll_t = intel._logs(chain, token, start, max_logs=int(os.getenv("TAPE_MAX_TRANSFERS", "60000")))
    fenster = 500_000 if chain == "robinhood" else 50_000
    swaps, voll_s = [], True
    # Je Pool und Thema einzeln: eine Liste von Werten an einer Stelle begrenzt Robinhood auf 100.000 Bloecke je Abfrage
    paare.sort(key=lambda p: -(p.get("liq") or 0))   # seit 2.77.0: die liquidesten Pools zuerst (hoechstens 6 je Art)
    v4 = [p for p in paare if p["v"] == 4]
    if v4 and POOL_MANAGER.get(chain):
        for p in v4[:6]:
            l, ok = _logs_bereich(chain, POOL_MANAGER[chain], [V4, p["adr"]], start, top, fenster)
            swaps += l
            voll_s = voll_s and ok
    alt = [p for p in paare if p["v"] in (0, 2, 3)]
    for p in alt[:6]:
        for thema in ([V2] if p["v"] == 2 else [V3] if p["v"] == 3 else [V2, V3]):
            l, ok = _logs_bereich(chain, p["adr"], [thema], start, top, fenster)
            swaps += l
            voll_s = voll_s and ok
    log.info(f"tape {chain} {token[:10]}: {len(transfers)} transfers, {len(swaps)} swaps from {len(paare)} pools, "
             f"{round(time.time() - t0)} s")
    dez = preise.dezimal(chain, token)
    sup, _ = preise.supply(chain, token)
    pmap = {p["adr"]: p for p in paare}
    pool_adr = {p["adr"] for p in alt} | ({POOL_MANAGER[chain]} if v4 and POOL_MANAGER.get(chain) else set())
    qdez = {p["adr"]: preise.dezimal(chain, p["quote"] or NULL) for p in paare}

    # Token-Salden je Transaktion (fuer den Kaeufer/Verkaeufer) und Bestaende
    je_tx, bestand, geschenkt = {}, {}, {}
    for l in transfers:
        try:
            von, an = "0x" + l["topics"][1][-40:], "0x" + l["topics"][2][-40:]
            m = int(l.get("data") or "0x0", 16)
        except Exception:
            continue
        d = je_tx.setdefault(l.get("transactionHash"), {})
        d[von] = d.get(von, 0) - m
        d[an] = d.get(an, 0) + m
        bestand[von] = bestand.get(von, 0) - m
        bestand[an] = bestand.get(an, 0) + m
    swap_tx = {l.get("transactionHash") for l in swaps}
    # Eingaenge ohne Swap in der Transaktion: geschenkt, weitergeleitet, vom Launch
    for h, d in je_tx.items():
        if h in swap_tx:
            continue
        for a, v in d.items():
            if v > 0 and a not in pool_adr and a != NULL:
                geschenkt[a] = geschenkt.get(a, 0) + v

    roh = []
    for l in swaps:
        t = l.get("topics") or []
        if not t:
            continue
        w = _worte(l.get("data"))
        try:
            if t[0] == V4:
                p = pmap.get(t[1].lower())
                if not p:
                    continue
                a0, a1 = _signed(w[0]), _signed(w[1])
                tok_ist_1 = int(token, 16) > int(str(p["quote"] or NULL).lower(), 16)
                tok, q = (a1, a0) if tok_ist_1 else (a0, a1)
                kauf = tok > 0                     # V4: aus Sicht des Tauschenden
            else:
                p = pmap.get(str(l.get("address")).lower())
                if not p:
                    continue
                tok_ist_1 = int(token, 16) > int(str(p["quote"] or NULL).lower(), 16)
                if t[0] == V3:
                    a0, a1 = _signed(w[0]), _signed(w[1])
                    tok, q = (a1, a0) if tok_ist_1 else (a0, a1)
                    kauf = tok < 0                 # V3: aus Sicht des Pools
                else:
                    i0, i1, o0, o1 = (int(x, 16) for x in w[:4])
                    t_in, t_out = (i1, o1) if tok_ist_1 else (i0, o0)
                    q_in, q_out = (i0, o0) if tok_ist_1 else (i1, o1)
                    kauf = t_out > t_in
                    tok, q = (t_out - t_in), (q_in - q_out)
        except Exception:
            continue
        menge = abs(tok) / 10 ** dez
        qm = abs(q) / 10 ** qdez.get(p["adr"], 18)
        if menge <= 0 or qm <= 0:
            continue
        roh.append({"block": int(l["blockNumber"], 16), "li": int(l.get("logIndex") or "0x0", 16),
                    "tx": l.get("transactionHash"), "side": "buy" if kauf else "sell", "amount": menge,
                    "quote_amt": qm, "quote": p["quote"], "qsym": p["qsym"], "pool": p["adr"]})
    if len(roh) > MAX_EVM:
        roh = roh[:MAX_EVM // 2] + roh[-MAX_EVM // 2:]
        voll_s = False

    # Wer hat gekauft oder verkauft: groesster Saldo in der Transaktion ausser Pools
    ohne, arbitrage = [], set()
    for r in roh:
        if r["tx"] not in je_tx:
            # V4 ohne Token-Bewegung: Arbitrage durch mehrere Pools in einer Transaktion, niemand haelt danach mehr oder weniger
            arbitrage.add(r["tx"])
            continue
        d = je_tx.get(r["tx"]) or {}
        kand = [(a, v) for a, v in d.items() if a not in pool_adr and a != NULL and ((v > 0) if r["side"] == "buy" else (v < 0))]
        if kand:
            r["wallet"] = max(kand, key=lambda x: abs(x[1]))[0]
        else:
            ohne.append(r)
    if ohne:
        hs = list({r["tx"] for r in ohne})[:3000]
        res = intel._evm_batch(chain, [("eth_getTransactionByHash", [h]) for h in hs])
        von = {h: str((x or {}).get("from") or "").lower() for h, x in zip(hs, res)}
        for r in ohne:
            r["wallet"] = von.get(r["tx"]) or None
    roh = [r for r in roh if r.get("wallet") and r["tx"] not in arbitrage]

    # Seit 2.82.45: Kaeufe und Verkaeufe ohne Pool-Swap (Torch-Kurve, Router, Launchpad). Vorher zaehlten die als
    # "ohne Kauf bekommen". Erkennung wie onchain.activity: Tokens kommen von einem Vertrag (Kauf) oder gehen an einen
    # Vertrag (Verkauf), in einer Transaktion ohne Pool-Swap. Bezahlt wird, was die Wallet in derselben Transaktion
    # abgibt (ETH als Wert der Transaktion, sonst der groesste andere Token-Abgang).
    try:
        extra = _ohne_pool(chain, token, je_tx, swap_tx, pool_adr, dez)
        for r in extra:
            g = geschenkt.get(r["wallet"])
            if r["side"] == "buy" and g:
                geschenkt[r["wallet"]] = max(0, g - r["_roh"])
            r.pop("_roh", None)
        roh += extra
        roh.sort(key=lambda r: (r["block"], r["li"]))
        if extra:
            log.info(f"tape {chain} {token[:10]}: {len(extra)} trades without a pool swap (curve or router)")
    except Exception as e:
        log.warning(f"tape {chain} {token[:10]} curve trades: {e}")

    # Zeitstempel je Block
    bl = sorted({r["block"] for r in roh})
    ts = {}
    if bl:
        # Hoechstens TAPE_TS_SAMPLES Bloecke lesen, gleichmaessig verteilt, der Rest wird dazwischen geschaetzt
        n_max = int(os.getenv("TAPE_TS_SAMPLES", "300"))
        probe = bl if len(bl) <= n_max else sorted({bl[int(i * (len(bl) - 1) / (n_max - 1))] for i in range(n_max)})
        res = intel._evm_batch(chain, [("eth_getBlockByNumber", [hex(b), False]) for b in probe])
        for b, x in zip(probe, res):
            try:
                ts[b] = int(x["timestamp"], 16)
            except Exception:
                pass
        # Luecken (429): zwischen bekannten Bloecken linear schaetzen
        bek = sorted(ts.items())
        if bek:
            import bisect
            ks = [k for k, _ in bek]
            for b in bl:
                if b in ts:
                    continue
                i = bisect.bisect_left(ks, b)
                if 0 < i < len(ks):
                    (b0, t0_), (b1, t1_) = bek[i - 1], bek[i]
                    ts[b] = int(t0_ + (t1_ - t0_) * (b - b0) / max(1, b1 - b0))
                else:
                    ts[b] = bek[min(i, len(bek) - 1)][1]
    trades = []
    for r in roh:
        r["ts"] = ts.get(r["block"])
        trades.append(r)
    _bewerten(chain, trades, sup)
    halt = {a: v / 10 ** dez for a, v in bestand.items() if v > 0 and a not in pool_adr and a != NULL}
    bekannt = sorted(ts.items())
    blockzeit = ((bekannt[-1][1] - bekannt[0][1]) / (bekannt[-1][0] - bekannt[0][0])
                 if len(bekannt) > 1 and bekannt[-1][0] > bekannt[0][0] else None)
    return {"chain": chain, "token": token, "dev": dev, "supply": sup, "trades": trades, "block_time": blockzeit, "arbitrage_txs": len(arbitrage),
            "holding": halt, "gifted": {a: v / 10 ** dez for a, v in geschenkt.items()},
            "pools": sorted(pool_adr), "complete": bool(voll_t and voll_s), "launch_block": start,
            "seconds": round(time.time() - t0, 1)}


def _ohne_pool(chain, token, je_tx, swap_tx, pool_adr, dez):
    import intel
    import preise
    from collections import Counter
    kand = {h: d for h, d in je_tx.items() if h not in swap_tx}
    if not kand:
        return []
    # Gegenparteien, die oft vorkommen, sind Kandidaten fuer Kurve oder Router
    zaehl = Counter(a for d in kand.values() for a in d if a != NULL)
    haeufig = [a for a, n in zaehl.most_common(60) if n >= 2] + [a for a in pool_adr]
    vertr = _vertraege(chain, haeufig[:60]) | set(pool_adr)
    try:
        import onchain
        vertr |= onchain._pool_adressen() | {onchain.FABRIK}
    except Exception:
        pass
    faelle = []
    for h, d in kand.items():
        geber = [a for a, v in d.items() if v < 0 and a in vertr]
        nehmer = [a for a, v in d.items() if v > 0 and a in vertr]
        for a, v in d.items():
            if a == NULL or a in vertr:
                continue
            if v > 0 and geber:
                faelle.append((h, a, "buy", v, geber[0]))
            elif v < 0 and nehmer:
                faelle.append((h, a, "sell", -v, nehmer[0]))
    if not faelle:
        return []
    faelle = faelle[:int(os.getenv("TAPE_CURVE_MAX", "1500"))]
    hs = list({f[0] for f in faelle})
    txs = intel._evm_batch(chain, [("eth_getTransactionByHash", [h]) for h in hs])
    rcs = intel._evm_batch(chain, [("eth_getTransactionReceipt", [h]) for h in hs])
    info = {h: (t or {}, r or {}) for h, t, r in zip(hs, txs, rcs)}
    sym_cache = {}

    def sym(t):
        if t not in sym_cache:
            try:
                import onchain
                m = onchain._meta([t]).get(t) or {}
                sym_cache[t] = (m.get("symbol") or "?", int(m.get("decimals") or 18))
            except Exception:
                sym_cache[t] = ("?", preise.dezimal(chain, t))
        return sym_cache[t]
    out = []
    TR = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    for h, w, seite, roh_m, gegen in faelle:
        t, rc = info.get(h, ({}, {}))
        try:
            blk = int(t.get("blockNumber") or rc.get("blockNumber") or "0x0", 16)
        except Exception:
            blk = 0
        if not blk:
            continue
        q, qsym, qm = None, None, None
        wert = int(t.get("value") or "0x0", 16) if seite == "buy" else 0
        if wert > 0 and str(t.get("from") or "").lower() == w:
            q, qsym, qm = "0x" + "0" * 40, "ETH", wert / 1e18
        else:
            beste = None
            for l in rc.get("logs") or []:
                tp = l.get("topics") or []
                if len(tp) < 3 or tp[0] != TR or str(l.get("address")).lower() == token:
                    continue
                von, an = "0x" + tp[1][-40:], "0x" + tp[2][-40:]
                if (seite == "buy" and von == w) or (seite == "sell" and an == w):
                    try:
                        m = int(l.get("data") or "0x0", 16)
                    except Exception:
                        continue
                    if not beste or m > beste[1]:
                        beste = (str(l.get("address")).lower(), m)
            if beste:
                s_, d_ = sym(beste[0])
                q, qsym, qm = beste[0], s_, beste[1] / 10 ** d_
        r = {"block": blk, "li": 0, "tx": h, "side": seite, "amount": roh_m / 10 ** dez, "wallet": w,
             "pool": gegen, "via": "curve or router", "_roh": roh_m}
        if q and qm:
            r.update(quote=q, qsym=qsym, quote_amt=qm)
        else:
            r.update(quote=None, qsym=None, quote_amt=0.0)
        out.append(r)
    return out


# ── Solana ───────────────────────────────────────────────────────────────

def _sigs(adresse, bis_zeit):
    import intel
    out, vor = [], None
    while time.time() < bis_zeit and len(out) < 60000:
        opt = {"limit": 1000}
        if vor:
            opt["before"] = vor
        r = intel._sol("getSignaturesForAddress", [adresse, opt], timeout=10)
        if not r:
            break
        out += [x for x in r if x.get("signature") and not x.get("err")]
        vor = r[-1].get("signature")
        if len(r) < 1000:
            return out, True
    return out, False


def _sol_lesen(mint):
    import intel
    import spaeher
    t0 = time.time()
    bis = t0 + BUDGET * 0.4
    paare = _paare("solana", mint)
    alle, voll = {}, True
    for adr in [mint] + [p["adr"] for p in paare[:4]]:
        s, ok = _sigs(adr, bis)
        voll = voll and ok
        for x in s:
            alle[x["signature"]] = x.get("blockTime") or 0
    sigs = sorted(alle.items(), key=lambda x: x[1])
    if len(sigs) > MAX_SOL:
        sigs = sigs[:MAX_SOL // 2] + sigs[-MAX_SOL // 2:]
        voll = False
    txs = intel._sol_batch([intel._tx(s) for s, _ in sigs], 20) if sigs else []
    deltas = []
    zaehl = {}
    dez = None
    for (sig, bt), tx in zip(sigs, txs):
        if not isinstance(tx, dict) or ((tx.get("meta") or {}).get("err")):
            continue
        d = {o: v for o, v in intel._token_delta(tx, mint).items() if v}
        if not d:
            continue
        if dez is None:
            for b in (tx.get("meta") or {}).get("postTokenBalances") or []:
                if b.get("mint") == mint:
                    dez = int((b.get("uiTokenAmount") or {}).get("decimals") or 0)
                    break
        for o in d:
            zaehl[o] = zaehl.get(o, 0) + 1
        deltas.append((sig, tx.get("blockTime") or bt, tx.get("slot"), tx, d))
    dez = dez if dez is not None else 6
    n = len(deltas)
    pools = set(SOL_POOLS) | {p["adr"] for p in paare}
    if n >= 20:
        pools |= {o for o, c in zaehl.items() if c >= 0.15 * n}
    trades, bestand, geschenkt = [], {}, {}
    for sig, bt, slot, tx, d in deltas:
        for o, v in d.items():
            bestand[o] = bestand.get(o, 0) + v
        for o, v in d.items():
            if o in pools:
                continue
            e = spaeher.sol_einordnen(tx, o) or {}
            menge = abs(v) / 10 ** dez
            sol = e.get("sol") or 0.0
            stab_r = sum(m for mt, m in e.get("raus") or [] if mt in _sol_stable())
            stab_e = sum(m for mt, m in e.get("rein") or [] if mt in _sol_stable())
            if v > 0 and (sol < -0.0005 or stab_r > 0):
                q, qm = ("USD", stab_r) if stab_r > 0 else ("SOL", -sol)
                trades.append({"ts": bt, "slot": slot, "tx": sig, "wallet": o, "side": "buy", "amount": menge,
                               "quote": q, "qsym": q, "quote_amt": qm})
            elif v < 0 and (sol > 0.0005 or stab_e > 0):
                q, qm = ("USD", stab_e) if stab_e > 0 else ("SOL", sol)
                trades.append({"ts": bt, "slot": slot, "tx": sig, "wallet": o, "side": "sell", "amount": menge,
                               "quote": q, "qsym": q, "quote_amt": qm})
            elif v > 0:
                geschenkt[o] = geschenkt.get(o, 0) + v
    import preise
    sup, _ = preise.supply("solana", mint)
    _bewerten("solana", trades, sup)
    halt = {a: v / 10 ** dez for a, v in bestand.items() if v > 0 and a not in pools}
    if not voll:
        halt = {}  # aus Teil-Daten nicht ableitbar, siehe bericht(): aktuelle Bestaende werden direkt gelesen
    dev = None
    try:
        dev = intel.dev_sol(mint)
        dev = dev.get("address") if isinstance(dev, dict) else dev
    except Exception:
        pass
    return {"chain": "solana", "token": mint, "dev": dev, "supply": sup, "trades": trades, "holding": halt,
            "gifted": {a: v / 10 ** dez for a, v in geschenkt.items()}, "pools": sorted(pools),
            "complete": voll, "seconds": round(time.time() - t0, 1)}


def _sol_stable():
    import preise
    return preise.SOL_STABLE


# ── Bewerten ─────────────────────────────────────────────────────────────

def _bewerten(chain, trades, sup):
    """USD je Trade, Kurs je Coin, Market Cap."""
    import preise
    for r in trades:
        if not r.get("ts"):
            continue
        if not r.get("quote") or not r.get("quote_amt"):
            continue
        if chain == "solana":
            qu = (1.0 if r["quote"] == "USD" else preise.quote_usd("solana", preise.WSOL, "SOL", r["ts"])[0])
        else:
            qu = preise.quote_usd(chain, r["quote"], r["qsym"], r["ts"])[0]
        if qu:
            r["usd"] = r["quote_amt"] * qu
            r["price"] = r["usd"] / r["amount"]
            r["mc"] = r["price"] * sup if sup else None


# ── Lesen mit Cache ──────────────────────────────────────────────────────

def lesen(chain, token, frisch=False):
    k = (chain, token if chain in ("solana", "tron", "ton") else token.lower())
    c = _CACHE.get(k)
    if c and not frisch and time.time() - c[0] < CACHE_SEK:
        return c[1]
    sp = _SPERREN.setdefault(k, threading.Lock())
    with sp:
        c = _CACHE.get(k)
        if c and not frisch and time.time() - c[0] < CACHE_SEK:
            return c[1]
        t = _sol_lesen(token) if chain == "solana" else _evm_lesen(chain, token)
        t["trades"].sort(key=lambda r: (r.get("ts") or 0, r.get("block") or r.get("slot") or 0, r.get("li") or 0))
        _CACHE[k] = (time.time(), t)
        if len(_CACHE) > 40:
            for alt in sorted(_CACHE, key=lambda x: _CACHE[x][0])[:10]:
                _CACHE.pop(alt, None)
        return t


_LAEUFT = {}


def lesen_warten(chain, token, warte=None):
    """Lesen im Hintergrund, hoechstens warte Sekunden darauf warten. -> Tape oder None (laeuft noch)."""
    k = (chain, token if chain in ("solana", "tron", "ton") else token.lower())
    c = _CACHE.get(k)
    if c and time.time() - c[0] < CACHE_SEK:
        return c[1]
    th = _LAEUFT.get(k)
    if not th or not th.is_alive():
        def lauf():
            try:
                lesen(chain, token)
            except Exception as e:
                log.warning(f"tape lesen {chain} {str(token)[:10]}: {e}")
        th = threading.Thread(target=lauf, daemon=True)
        th.start()
        _LAEUFT[k] = th
    th.join(warte if warte is not None else float(os.getenv("TAPE_WAIT_SEC", "75")))
    c = _CACHE.get(k)
    return c[1] if c else None


def _vertraege(chain, adressen):
    """Welche Adressen sind Vertraege (Router, Pools, Steuer-Kontrakte)? Nur EVM."""
    if chain == "solana" or not adressen:
        return set()
    import intel
    adressen = list(adressen)[:60]
    try:
        res = intel._evm_batch(chain, [("eth_getCode", [a, "latest"]) for a in adressen])
    except Exception:
        return set()
    import codeart  # seit 2.63: EIP-7702-Wallets (z. B. Smart Accounts) sind Wallets, keine Vertraege
    return {a for a, c in zip(adressen, res) if c and codeart.norm(c) not in ("0x", "0x0")}


# ── Je Wallet ────────────────────────────────────────────────────────────

def wallets(t, preis_jetzt=None):
    """Durchschnittskosten je Wallet. -> {wallet: {...}}"""
    out = {}
    sup = t.get("supply")
    for r in t["trades"]:
        w = out.setdefault(r["wallet"], {"wallet": r["wallet"], "buys": 0, "sells": 0, "bought": 0.0, "sold": 0.0,
                                         "cost_usd": 0.0, "proceeds_usd": 0.0, "realized_usd": 0.0, "_menge": 0.0,
                                         "_kosten": 0.0, "_mc_kauf": 0.0, "_mc_kauf_m": 0.0, "_mc_verk": 0.0,
                                         "_mc_verk_m": 0.0, "first_buy": None, "first_buy_mc": None, "last_trade": None,
                                         "unpriced": 0})
        w["last_trade"] = r.get("ts")
        u = r.get("usd")
        if u is None:
            w["unpriced"] += 1
        if r["side"] == "buy":
            w["buys"] += 1
            w["bought"] += r["amount"]
            w["_menge"] += r["amount"]
            if u is not None:
                w["cost_usd"] += u
                w["_kosten"] += u
            if r.get("mc"):
                w["_mc_kauf"] += r["mc"] * r["amount"]
                w["_mc_kauf_m"] += r["amount"]
            if w["first_buy"] is None:
                w["first_buy"], w["first_buy_mc"] = r.get("ts"), r.get("mc")
        else:
            w["sells"] += 1
            w["sold"] += r["amount"]
            if u is not None:
                w["proceeds_usd"] += u
            if r.get("mc"):
                w["_mc_verk"] += r["mc"] * r["amount"]
                w["_mc_verk_m"] += r["amount"]
            if w["_menge"] > 0:
                nimm = min(r["amount"], w["_menge"])
                schnitt = w["_kosten"] / w["_menge"]
                if u is not None:
                    w["realized_usd"] += u * nimm / r["amount"] - schnitt * nimm
                w["_menge"] -= nimm
                w["_kosten"] -= schnitt * nimm
            elif u is not None:
                w["realized_usd"] += u  # verkauft ohne Kauf hier: geschenkt oder weitergeleitet bekommen
    halt, gesch = t.get("holding") or {}, t.get("gifted") or {}
    for a in set(halt) | set(gesch):
        if a not in out and (halt.get(a, 0) > 0 or gesch.get(a, 0) > 0):
            out[a] = {"wallet": a, "buys": 0, "sells": 0, "bought": 0.0, "sold": 0.0, "cost_usd": 0.0, "proceeds_usd": 0.0,
                      "realized_usd": 0.0, "_menge": 0.0, "_kosten": 0.0, "_mc_kauf": 0, "_mc_kauf_m": 0, "_mc_verk": 0,
                      "_mc_verk_m": 0, "first_buy": None, "first_buy_mc": None, "last_trade": None, "unpriced": 0}
    for a, w in out.items():
        w["avg_entry_mc"] = w["_mc_kauf"] / w["_mc_kauf_m"] if w["_mc_kauf_m"] else None
        w["avg_exit_mc"] = w["_mc_verk"] / w["_mc_verk_m"] if w["_mc_verk_m"] else None
        w["received_without_buying"] = gesch.get(a, 0.0)
        h = halt.get(a, 0.0 if (halt and t.get("complete")) else None)
        w["holds"] = h
        w["holds_pct"] = (h / sup * 100) if (h is not None and sup) else None
        if h is not None and preis_jetzt:
            offen = min(h, w["_menge"])
            w["value_now_usd"] = h * preis_jetzt
            w["unrealized_usd"] = offen * preis_jetzt - (w["_kosten"] if w["_menge"] else 0) * (offen / w["_menge"] if w["_menge"] else 0)
        for k in [k for k in w if k.startswith("_")]:
            del w[k]
    return out


# ── Copy-Trading ─────────────────────────────────────────────────────────

def kopierer(t, fuehrer, sekunden=None, min_treffer=2):
    """Wer handelt wiederholt in dieselbe Richtung kurz nach fuehrer? Im selben Block/Slot heisst: kein Nachahmen
    nach Sicht, sondern gleichzeitig (Bundle, Bot mit Vorwissen oder dieselbe Person)."""
    sek = sekunden or int(os.getenv("TAPE_COPY_SEC", "60"))
    f = str(fuehrer)
    f_l = f.lower()
    fz = [r for r in t["trades"] if str(r["wallet"]).lower() == f_l]
    if not fz:
        return {"leader_trades": 0, "followers": []}
    andere = [r for r in t["trades"] if str(r["wallet"]).lower() != f_l]
    bz = t.get("block_time")
    treffer = {}
    for ft in fz:
        if not ft.get("ts"):
            continue
        gesehen = set()
        for r in andere:
            if r["side"] != ft["side"] or not r.get("ts") or r["wallet"] in gesehen:
                continue
            gleich = (r.get("block") is not None and r.get("block") == ft.get("block")) or \
                     (r.get("slot") is not None and r.get("slot") == ft.get("slot"))
            if gleich and (r.get("li") or 0) < (ft.get("li") or 0):
                continue  # davor im selben Block: kein Nachahmer
            dt = r["ts"] - ft["ts"]
            if bz and r.get("block") is not None and ft.get("block") is not None:
                dt = (r["block"] - ft["block"]) * bz  # genauer als geschaetzte Zeitstempel
            if gleich or 0 <= dt <= sek:
                gesehen.add(r["wallet"])
                x = treffer.setdefault(r["wallet"], {"wallet": r["wallet"], "same_block": 0, "within": 0, "seconds": []})
                if gleich:
                    x["same_block"] += 1
                else:
                    x["within"] += 1
                    x["seconds"].append(round(dt, 1))
    n = len([x for x in fz if x.get("ts")])
    out = []
    for w, x in treffer.items():
        summe = x["same_block"] + x["within"]
        # Bei vielen Trades des Fuehrers reicht ein paar Mal Zufall nicht: mindestens ein Viertel seiner Trades
        noetig = max(min_treffer, -(-n // 4)) if n > 4 else min(min_treffer, max(1, n))
        if summe >= noetig and (n <= 2 or summe >= 2):
            x["matched"] = summe
            x["of_leader_trades"] = n
            x["median_seconds"] = sorted(x["seconds"])[len(x["seconds"]) // 2] if x["seconds"] else 0
            del x["seconds"]
            out.append(x)
    out.sort(key=lambda x: (-x["matched"], -x["same_block"]))
    return {"leader_trades": n, "window_seconds": sek, "followers": out[:15]}


# ── Bericht fuer das Werkzeug ────────────────────────────────────────────

def _kurz(a):
    a = str(a)
    return a[:6] + "…" + a[-4:]


def _r(v, n=0):
    return round(v, n) if isinstance(v, (int, float)) else v


def _zeile(w, sup=None):
    z = {"wallet": w["wallet"], "buys": w["buys"], "sells": w["sells"],
         "avg_entry_mc": _r(w.get("avg_entry_mc")), "first_buy_mc": _r(w.get("first_buy_mc")),
         "first_buy": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(w["first_buy"])) if w.get("first_buy") else None,
         "avg_exit_mc": _r(w.get("avg_exit_mc")), "spent_usd": _r(w["cost_usd"], 2), "got_usd": _r(w["proceeds_usd"], 2),
         "realized_usd": _r(w["realized_usd"], 2), "holds_pct": _r(w.get("holds_pct"), 3),
         "unrealized_usd": _r(w.get("unrealized_usd"), 2)}
    if w.get("received_without_buying") and sup:
        z["received_without_buying_pct"] = _r(w["received_without_buying"] / sup * 100, 3)
    if w.get("tag"):
        z["tag"] = w["tag"]
    return {k: v for k, v in z.items() if v not in (None, 0, 0.0) or k in ("buys", "sells")}


def bericht(chain, token, view="overview", wallet=None, max_mc=None, leader=None):
    """view: overview | early | holders | profit | wallet | copy"""
    import scan
    t = lesen_warten(chain, token)
    if t is None:
        return {"chain": chain, "token": token, "still_reading": True,
                "note": "Still reading the full trade history of this coin, it is long. Ask again in a minute or two, it will be ready then."}
    if not t.get("trades") and not t.get("complete"):
        # Seit 2.77.0: abgebrochenes Lesen (RPC ueberlastet) ist nicht "keine Trades". Nicht merken, neu versuchen lassen.
        _CACHE.pop((chain, token if chain in ("solana", "tron", "ton") else token.lower()), None)
        return {"chain": chain, "token": token, "still_reading": True, "read_failed": True,
                "note": ("Reading the trade history was interrupted (the chain's data service was busy). This does NOT mean "
                         "there were no trades. Say you could not read the trades right now and offer to try again in a "
                         "minute. Do not draw conclusions about sellers or buyers from this.")}
    m = scan.markt(token, chain, scan._paare(token)) or {}
    preis = m.get("price")
    sup = t.get("supply")
    # Solana mit Teil-Daten: aktuelle Bestaende der wichtigsten Wallets direkt lesen
    if chain == "solana" and not t.get("holding"):
        kauf = {}
        for r in t["trades"]:
            if r["side"] == "buy":
                kauf[r["wallet"]] = kauf.get(r["wallet"], 0) + r["amount"]
        top = sorted(kauf, key=kauf.get, reverse=True)[:40]
        try:
            b = scan.bestaende("solana", token, top)
            dz = None
            import preise
            _, dz = preise.supply("solana", token)
            t["holding"] = {a: v / 10 ** (dz or 6) for a, v in b.items() if a != "_supply"}
        except Exception:
            pass
    ws = wallets(t, preis)
    if not t.get("dev"):
        try:  # Dev aus dem Report (alle Chains), wenn der Launch ihn nicht nennt
            t["dev"] = ((scan.analyse(f"{chain}:{token}", deep=False) or {}).get("dev") or {}).get("address")
        except Exception:
            pass
    dev = str(t.get("dev") or "")
    for a, w in ws.items():
        if dev and str(a).lower() == dev.lower():
            w["tag"] = "dev"
    # Seit 2.62.2: Router, Steuer- und Pool-Vertraege kommen in keine Liste von Kaeufern oder Holdern. Sie stehen
    # getrennt unter contracts_skipped. Vorher stand ein Torch-Vertrag als "erster Kaeufer" mit $13.9K Gewinn da.
    gepr, vertr = set(), set()

    def personen(reihe, n):
        out = []
        i = 0
        while len(out) < n and i < len(reihe):
            block = [w for w in reihe[i:i + n * 2] if w["wallet"] not in gepr]
            neu = _vertraege(chain, {w["wallet"] for w in block})
            gepr.update(w["wallet"] for w in block)
            vertr.update(neu)
            for w in reihe[i:i + n * 2]:
                if w["wallet"] not in vertr and len(out) < n:
                    out.append(w)
            i += n * 2
        return out
    kopf = {"chain": chain, "token": token, "symbol": m.get("symbol"), "price_now": preis, "mc_now": m.get("mcap"),
            "supply": sup, "trades_read": len(t["trades"]), "wallets": len(ws), "complete": t.get("complete"),
            "note": ("Market cap = price at the trade times total supply today. Average cost per wallet."
                     + ("" if t.get("complete") else " History was too long to read in full: earliest and latest trades are in, the middle may be missing."))}
    if dev:
        kopf["dev"] = dev
        if dev.lower() in {a.lower() for a in ws}:
            kopf["dev_summary"] = _zeile(next(w for a, w in ws.items() if a.lower() == dev.lower()), sup)
    alle = list(ws.values())

    def ende(k):
        if vertr:
            k["contracts_skipped"] = [{"wallet": a, "note": "contract (router, tax or pool), not a person, left out of the lists"}
                                      for a in sorted(vertr)[:6]]
        return k
    if view == "wallet" and wallet:
        wl = str(wallet).lower() if chain not in ("solana", "tron", "ton") else wallet
        tr = [r for r in t["trades"] if (str(r["wallet"]).lower() if chain not in ("solana", "tron", "ton") else r["wallet"]) == wl]
        w = ws.get(wl) or next((x for a, x in ws.items() if str(a).lower() == str(wallet).lower()), None)
        kopf["wallet"] = _zeile(w, sup) if w else {"wallet": wallet, "trades": 0}
        kopf["trades"] = [{"time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(r["ts"])) if r.get("ts") else None,
                           "side": r["side"], "amount": _r(r["amount"], 2), "paid_or_got": f"{_r(r['quote_amt'], 6)} {r.get('qsym')}",
                           "usd": _r(r.get("usd"), 2), "mc": _r(r.get("mc")), "tx": r["tx"]} for r in tr[-60:]]
        return kopf
    if view == "copy":
        f = leader or dev
        if not f:
            return dict(kopf, error="No leader wallet: give one, the dev is unknown for this coin.")
        kopf["leader"] = f
        kopf["copy"] = kopierer(t, f)
        fl = kopf["copy"].get("followers") or []
        weg = _vertraege(chain, {x["wallet"] for x in fl})
        kopf["copy"]["followers"] = [x for x in fl if x["wallet"] not in weg]
        vertr.update(weg)
        kopf["copy_note"] = ("same_block: traded in the same block or slot as the leader, right after it. That is not copying by "
                             "watching, it is simultaneous (bundle, bot with early knowledge, or the same person). within: same "
                             "direction within the window after the leader.")
        return ende(kopf)
    if view == "early":
        reihe = sorted([w for w in alle if w.get("first_buy")], key=lambda w: w["first_buy"])
        if max_mc:
            reihe = [w for w in reihe if (w.get("first_buy_mc") or 0) <= float(max_mc)]
        kopf["early_buyers"] = [_zeile(w, sup) for w in personen(reihe, 25)]
        return ende(kopf)
    if view == "profit":
        reihe = sorted(alle, key=lambda w: -((w.get("realized_usd") or 0) + (w.get("unrealized_usd") or 0)))
        kopf["top_profit"] = [_zeile(w, sup) for w in personen(reihe, 15)]
        kopf["top_loss"] = [_zeile(w, sup) for w in personen(list(reversed(reihe)), 10)]
        return ende(kopf)
    if view == "holders":
        reihe = sorted([w for w in alle if (w.get("holds") or 0) > 0], key=lambda w: -(w.get("holds") or 0))
        kopf["holders"] = [_zeile(w, sup) for w in personen(reihe, 25)]
        return ende(kopf)
    # overview
    if max_mc:
        unter = [w for w in alle if w.get("avg_entry_mc") and w["avg_entry_mc"] <= float(max_mc)]
        kopf[f"bought_under_{int(float(max_mc))}"] = {"wallets": len(unter),
                                                      "still_holding": sum(1 for w in unter if (w.get("holds") or 0) > 0),
                                                      "list": [_zeile(w, sup) for w in personen(sorted(unter, key=lambda w: w["avg_entry_mc"]), 20)]}
    kopf["early_buyers"] = [_zeile(w, sup) for w in personen(sorted([w for w in alle if w.get("first_buy")], key=lambda w: w["first_buy"]), 10)]
    kopf["largest_holders"] = [_zeile(w, sup) for w in personen(sorted([w for w in alle if (w.get("holds") or 0) > 0],
                                                                        key=lambda w: -(w.get("holds") or 0)), 10)]
    kopf["got_tokens_without_buying"] = [{"wallet": w["wallet"], "pct": _r(w["received_without_buying"] / sup * 100, 3) if sup else None,
                                          "holds_pct": _r(w.get("holds_pct"), 3)}
                                         for w in sorted([w for w in alle if w.get("received_without_buying")],
                                                         key=lambda w: -w["received_without_buying"])[:8]]
    return ende(kopf)


# ── Werkzeug fuer den Motor ──────────────────────────────────────────────

def werkzeug(a):
    """trade_tape: token (Adresse, Link oder Ticker), view, wallet, max_mc, leader."""
    import scan
    hinweis, adr = scan.lesen(a.get("token") or "")
    if not adr:
        return {"error": "Give me the contract address, a chart link or the ticker of the coin."}
    if not scan.ist_adresse(adr):
        f = scan.ticker_finden(adr, hinweis)
        if not f or f.get("ambiguous") or f.get("major"):
            return {"error": "Send the contract address of the coin, the ticker is not unique or not a token."}
        adr, hinweis = f["address"], f["chain"]
    chain = scan.chain_waehlen(adr, scan._paare(adr), hinweis)
    if chain != "solana" and chain not in POOL_MANAGER:
        return {"error": f"Trade tape is not available on {chain} yet."}
    view = str(a.get("view") or "overview").lower()
    try:
        r = bericht(chain, adr, view, a.get("wallet"), a.get("max_mc"), a.get("leader"))
    except Exception as e:
        log.warning(f"tape {chain} {adr[:10]}: {e}")
        return {"error": "Could not read the trades of this coin right now. Try again in a minute."}
    if r.get("still_reading"):
        return r
    if not r.get("trades_read"):
        r["note"] = (r.get("note") or "") + " No swaps found in the pools of this coin (no pool yet, or a launch curve we do not read)."
    r["how_to_answer"] = ("Write market caps like $85K or $1.2M and short wallets as 0x1234…abcd (Solana: first 4…last 4). "
                          "Say what the chain shows, never accuse anyone or guess who is behind a wallet. Average entry = "
                          "average market cap of all buys of that wallet. If complete is false, say the middle of a long "
                          "history may be missing. For copy: same_block is simultaneous (bundle, bot or same person), "
                          "within is seconds after. Keep it short: the 5 to 10 most telling rows, not the whole list.")
    return r
