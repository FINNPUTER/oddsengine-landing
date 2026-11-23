"""
weiter.py: Token-Weiterleitungen (Forwards) im Tiefen-Scan, seit 2.46.

Frage: Haben fruehe Kaeufer oder der Dev ihre Tokens nicht verkauft, sondern einfach an andere Wallets
geschickt? So wird Angebot versteckt: die Top-Holder-Liste sieht sauber aus, aber dieselben Leute halten
ueber neue Wallets weiter. Erkannt wird nur eine reine Uebertragung von Wallet zu Wallet, kein Verkauf:
  EVM (Robinhood, Base, Ethereum, BNB, Arbitrum): aus allen Transfer-Logs des Coins. Absender und Empfaenger
      sind beide normale Wallets (kein Vertrag), und in derselben Transaktion bewegt kein Pool, Router oder
      Vertrag diesen Coin.
  Solana: aus den Transaktionen der Token-Konten der fruehen Kaeufer und des Devs. Der Absender verliert
      Tokens, ein normales Wallet (System-Konto) bekommt sie, und der Absender bekommt dafuer kein SOL.
Dazu: frische Empfaenger, was die Empfaenger heute noch halten, Sammel-Wallets (bekommen von mehreren
Absendern) und Sammel-Wallets, die das schon bei anderen Coins getan haben (Tabelle iw_fwd).

  evm(chain, token, logs, infra, sup, netto, frueh, dev, codes)   -> dict
  sol(mint, kaeufer, dev, dev_konto, sup)                         -> dict
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

from logger import log

MIN_ANTEIL = float(os.getenv("FWD_MIN_SUPPLY_PCT", "0.02"))   # kleinere Uebertragungen sind Staub
MAX_TX_SOL = int(os.getenv("FWD_MAX_TX_SOL", "320"))
SYSTEM = "11111111111111111111111111111111"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
NULL = "0x0000000000000000000000000000000000000000"


# ── Speicher: Sammel-Wallets ueber alle Coins ─────────────────────────────

def _q(sql, args=(), eins=False):
    import intel
    pg = intel._pg()
    if not pg:
        return None
    if not _q.__dict__.get("schema"):
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_fwd (chain TEXT NOT NULL, token TEXT NOT NULL, src TEXT NOT NULL,
                      dst TEXT NOT NULL, pct DOUBLE PRECISION, ts BIGINT, PRIMARY KEY (chain, token, src, dst))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_iw_fwd_dst ON iw_fwd(chain, dst)")
        _q.schema = True
    return intel._q(sql, args, eins)


def _speichern(chain, token, liste):
    jetzt = int(time.time())
    for f in liste:
        try:
            _q("INSERT INTO iw_fwd (chain, token, src, dst, pct, ts) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (chain, token, src, dst) "
               "DO UPDATE SET pct=EXCLUDED.pct, ts=EXCLUDED.ts", (chain, token, f["from"], f["to"], f["pct"], jetzt))
        except Exception as e:
            log.debug(f"weiter speichern: {e}")


def _anderswo(chain, token, ziele):
    """Wie viele ANDERE Coins hat jedes Ziel-Wallet schon gesammelt? {wallet: anzahl}"""
    if not ziele:
        return {}
    try:
        rows = _q("SELECT dst, COUNT(DISTINCT token) FROM iw_fwd WHERE chain=%s AND dst = ANY(%s) AND token <> %s GROUP BY dst",
                  (chain, list(ziele), token)) or []
        return {r[0]: int(r[1]) for r in rows}
    except Exception:
        return {}


def paare_von(chain, wallet, n=40):
    """Seit 2.61.4: alle Weiterleitungen aus unseren eigenen Scans, an denen dieses Wallet beteiligt war (als Sender oder
    Empfaenger, ueber alle Coins). Unsere Daten, unabhaengig davon, was der Explorer gerade liefert."""
    try:
        rows = _q("SELECT token, src, dst, pct, ts FROM iw_fwd WHERE chain=%s AND (src=%s OR dst=%s) ORDER BY ts DESC LIMIT %s",
                  (chain, wallet.lower(), wallet.lower(), n)) or []
    except Exception:
        return []
    return [{"token": r[0], "from": r[1], "to": r[2], "pct": r[3], "ts": r[4]} for r in rows]


BURN_ADR = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}


def _zusammenfassen(chain, token, liste, halten, frisch, frueh, dev, sup, verbrannt=None):
    """liste: [{from, to, raw, tx}] -> Ergebnis fuer Report, Karte und Bewertung."""
    if not liste:
        return {"checked": True, "count": 0}
    pct = lambda raw: round(raw / sup * 100, 3) if sup else None
    # gleiche Paare zusammenfassen
    paare = {}
    for f in liste:
        k = (f["from"], f["to"])
        p = paare.setdefault(k, {"from": f["from"], "to": f["to"], "raw": 0, "n": 0})
        p["raw"] += f["raw"]
        p["n"] += 1
    paare = list(paare.values())
    for p in paare:
        p["pct"] = pct(p["raw"])
        p["fresh"] = bool(frisch.get(p["to"]))
        p["holds_pct"] = pct(halten[p["to"]]) if halten.get(p["to"]) is not None else None
        p["src"] = "dev" if dev and p["from"] == dev else ("early buyer" if p["from"] in frueh else "holder")
        p["burned_pct"] = pct((verbrannt or {}).get(p["to"], 0)) or 0
    ziele = {}
    for p in paare:
        ziele.setdefault(p["to"], set()).add(p["from"])
    andere = _anderswo(chain, token, set(ziele))
    sammler = []
    for z, quellen in ziele.items():
        if len(quellen) >= 2 or andere.get(z):
            sammler.append({"wallet": z, "from_wallets": len(quellen), "other_tokens": andere.get(z, 0),
                            "holds_pct": pct(halten[z]) if halten.get(z) is not None else None, "fresh": bool(frisch.get(z))})
    sammler.sort(key=lambda s: (-s["other_tokens"], -s["from_wallets"]))
    # Was die Empfaenger heute noch halten (jeder nur einmal), ohne Wallets, die selbst fruehe Kaeufer sind
    gehalten = sum(halten.get(z) or 0 for z in ziele if z not in frueh and z != dev)
    paare.sort(key=lambda p: -(p["raw"]))
    out = {
        "checked": True, "count": sum(p["n"] for p in paare), "pairs": len(paare),
        "senders": len({p["from"] for p in paare}), "receivers": len(ziele),
        "from_early": len({p["from"] for p in paare if p["src"] == "early buyer"}),
        "from_dev": any(p["src"] == "dev" for p in paare),
        "dev_forwarded_pct": round(sum(p["pct"] or 0 for p in paare if p["src"] == "dev"), 3),
        # seit 2.60.6: was die Empfaenger der Dev-Tokens davon verbrannt haben (je Empfaenger hoechstens, was er bekam)
        "dev_burned_pct": round(sum(min(p["pct"] or 0, p["burned_pct"] or 0) for p in paare if p["src"] == "dev"), 3),
        "to_fresh": len({p["to"] for p in paare if p["fresh"]}),
        "forwarded_pct": round(sum(p["pct"] or 0 for p in paare), 3),
        "still_held_pct": pct(gehalten) if sup else None,
        "list": [{k: p[k] for k in ("from", "to", "pct", "fresh", "holds_pct", "src", "n", "burned_pct")} for p in paare[:25]],
        "collectors": sammler[:10],
        "collectors_elsewhere": sum(1 for s in sammler if s["other_tokens"]),
        "collectors_elsewhere_max": max([s["other_tokens"] for s in sammler] or [0]),
    }
    out.update(_ketten(paare, halten, frueh, dev, sup))
    _speichern(chain, token, paare)
    return out


MAX_HOPS = int(os.getenv("FWD_MAX_HOPS", "5"))


def _ketten(paare, halten, frueh, dev, sup):
    """Seit 2.63: Weiterleitungen ueber mehrere Stationen. Von jedem fruehen Kaeufer (auch Bundles) und vom Dev aus
    den reinen Wallet-zu-Wallet-Uebertragungen folgen, bis zu MAX_HOPS Stationen. So faellt ein Bundle nicht mehr
    durch, nur weil es seine Tokens ueber drei oder vier frische Wallets weitergereicht hat.
    -> origins {start: [[wallet, stationen, haelt_raw]]}, chains (laengste Wege), hidden_* (was am Ende noch liegt)"""
    nach = {}
    for p in paare:
        nach.setdefault(p["from"], []).append(p["to"])
    starts = set(frueh) | ({dev} if dev else set())
    origins, wege = {}, []
    for st in starts:
        if st not in nach:
            continue
        gesehen, reihe, erreicht = {st}, [(st, 0, [st])], []
        while reihe:
            w, tiefe, pfad = reihe.pop(0)
            if tiefe >= MAX_HOPS:
                continue
            for z in nach.get(w, []):
                if z in gesehen:
                    continue
                gesehen.add(z)
                erreicht.append([z, tiefe + 1, halten.get(z)])
                reihe.append((z, tiefe + 1, pfad + [z]))
                if tiefe + 1 >= 2:
                    wege.append({"path": pfad + [z], "hops": tiefe + 1, "src": "dev" if st == dev else "early buyer",
                                 "end_holds_pct": round(halten[z] / sup * 100, 3) if sup and halten.get(z) else 0})
        if erreicht:
            origins[st] = erreicht
    # Was die erreichten Wallets heute halten (jede nur einmal, ohne Wallets, die selbst fruehe Kaeufer oder der Dev sind)
    ende = {}
    for st, er in origins.items():
        for z, tiefe, h in er:
            if z in starts or z in ende:
                continue
            ende[z] = (h or 0, tiefe, st)
    pct = lambda raw: round(raw / sup * 100, 3) if sup else None
    wege.sort(key=lambda w: (-w["hops"], -w["end_holds_pct"]))
    return {"origins": {k: v[:40] for k, v in origins.items()},
            "max_hops": max([t for _, t, _ in ende.values()] or [0]),
            "multi_hop_wallets": sum(1 for _, t, _ in ende.values() if t >= 2),
            "hidden_held_pct": pct(sum(h for h, _, _ in ende.values())),
            "hidden_held_multi_hop_pct": pct(sum(h for h, t, _ in ende.values() if t >= 2)),
            "chains": [{"path": w["path"], "hops": w["hops"], "src": w["src"], "end_holds_pct": w["end_holds_pct"]} for w in wege[:8]]}


def versteckt_von(fw, quellen, sup=None):
    """Anteil am Angebot, den Wallets heute halten, die (ueber bis zu MAX_HOPS Stationen) von diesen Startwallets kamen."""
    if not fw or not fw.get("origins"):
        return 0.0
    quellen = set(quellen)
    gezaehlt, summe = set(), 0.0
    halter = {}
    for st, er in fw["origins"].items():
        for z, tiefe, h in er:
            halter[z] = h
    for st, er in fw["origins"].items():
        if st not in quellen:
            continue
        for z, tiefe, h in er:
            if z in quellen or z in gezaehlt:
                continue
            gezaehlt.add(z)
            summe += h or 0
    return summe


# ── EVM ──────────────────────────────────────────────────────────────────

def evm(chain, token, logs, infra, sup, netto, frueh, dev, voll):
    """Alle Weiterleitungen aus den Transfer-Logs. netto: Bestaende aus den Logs (wenn voll)."""
    import intel
    if not logs or not sup:
        return {"checked": False}
    klein = sup * MIN_ANTEIL / 100
    je_tx, kand = {}, []
    verbrannt = {}  # seit 2.60.6: wer hat wie viel an die Burn-Adresse geschickt
    for l in logs:
        try:
            von = "0x" + l["topics"][1][-40:]
            an = "0x" + l["topics"][2][-40:]
            menge = int(l.get("data") or "0x0", 16)
        except Exception:
            continue
        if an in BURN_ADR and von != NULL:
            verbrannt[von] = verbrannt.get(von, 0) + menge
        tx = l.get("transactionHash")
        je_tx.setdefault(tx, []).append((von, an))
        if von in infra or an in infra or von == NULL or an == NULL or von == an or menge < klein:
            continue
        kand.append({"from": von, "to": an, "raw": menge, "tx": tx})
    if not kand:
        return {"checked": True, "count": 0}
    # Wer ist ein Vertrag? Alle Adressen der Kandidaten und ihrer Transaktionen
    adressen = sorted({a for k in kand for a in (k["from"], k["to"])} |
                      {a for k in kand for p in je_tx.get(k["tx"], []) for a in p if a not in infra and a != NULL})[:900]
    codes = {}
    for i in range(0, len(adressen), 120):
        teil = adressen[i:i + 120]
        res = intel._evm_batch(chain, [("eth_getCode", [a, "latest"]) for a in teil])
        import codeart
        for a, c in zip(teil, res):
            if c is None:
                # Seit 2.63: fehlende Antworten (429 im Paket) einzeln nachholen. Vorher fiel so eine Wallet als
                # "kein normales Wallet" raus, und eine echte Weiterleitung des Devs verschwand aus dem Ergebnis.
                try:
                    c = intel._evm(chain, "eth_getCode", [a, "latest"])
                except Exception:
                    c = None
            codes[a] = codeart.norm(c)
    eoa = lambda a: codes.get(a) in ("0x", "0x0")
    liste = []
    for k in kand:
        if not (eoa(k["from"]) and eoa(k["to"])):
            continue
        # In derselben Transaktion bewegt ein Vertrag oder Pool diesen Coin: dann ist es Teil eines Swaps
        if any(a in infra or (a != NULL and not eoa(a)) for p in je_tx.get(k["tx"], []) for a in p):
            continue
        liste.append(k)
    if not liste:
        return {"checked": True, "count": 0}
    ziele = sorted({k["to"] for k in liste})[:200]
    noncen = intel._evm_batch(chain, [("eth_getTransactionCount", [z, "latest"]) for z in ziele])
    frisch = {z: (n is not None and int(n, 16) == 0) for z, n in zip(ziele, noncen)}
    if voll and netto is not None:
        halten = {z: max(0, netto.get(z, 0)) for z in ziele}
    else:
        best = intel._evm_batch(chain, [("eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + z[2:]}, "latest"]) for z in ziele])
        halten = {z: (int(b, 16) if b else None) for z, b in zip(ziele, best)}
    return _zusammenfassen(chain, token, liste, halten, frisch, set(frueh), dev, sup, verbrannt)


# ── Solana ───────────────────────────────────────────────────────────────

def konto_aus_tx(tx, mint, owner):
    """Das Token-Konto (Adresse) eines Besitzers fuer diesen Mint aus einer Transaktion."""
    import intel
    keys = intel._schluessel(tx)
    meta = (tx or {}).get("meta") or {}
    for b in (meta.get("postTokenBalances") or []) + (meta.get("preTokenBalances") or []):
        if b.get("mint") == mint and b.get("owner") == owner:
            i = b.get("accountIndex")
            if isinstance(i, int) and i < len(keys):
                return keys[i]
    return None


# Eine reine Uebertragung ruft nur diese Programme auf. Ist ein DEX, eine Kurve oder ein Aggregator dabei,
# ist es ein Swap und keine Weiterleitung.
EINFACH = {SYSTEM, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
           "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "ComputeBudget111111111111111111111111111111",
           "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"}


def _nur_uebertragung(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    meta = (tx or {}).get("meta") or {}
    ixs = list(msg.get("instructions") or [])
    for g in meta.get("innerInstructions") or []:
        ixs += g.get("instructions") or []
    progs = {ix.get("programId") for ix in ixs if isinstance(ix, dict) and ix.get("programId")}
    return bool(progs) and progs <= EINFACH


def _sol_aenderung(tx, wallet):
    import intel
    meta = (tx or {}).get("meta") or {}
    keys = intel._schluessel(tx)
    if wallet not in keys:
        return 0
    i = keys.index(wallet)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pre) or i >= len(post):
        return 0
    return post[i] - pre[i]


def sol(mint, kaeufer, dev, dev_konto, sup, halter=None):
    """kaeufer: [{wallet, konto, ...}] (fruehe Kaeufer mit ihrem Token-Konto aus dem Kauf).
    halter: die groessten Halter mit Token-Konto. Ihre Konten zeigen, wie sie an die Tokens kamen: kam es per
    reiner Uebertragung, ist der Halter ein Empfaenger einer Weiterleitung (versteckter Halter)."""
    import intel
    if not sup:
        return {"checked": False}
    quellen = [(k["wallet"], k.get("konto")) for k in kaeufer if k.get("konto")]
    if dev and dev_konto:
        quellen.append((dev, dev_konto))
    schon = {kt for _, kt in quellen}
    for h in halter or []:
        if h.get("konto") and not h.get("pool") and not h.get("locked") and h["konto"] not in schon:
            quellen.append((h["address"], h["konto"]))
    if not quellen:
        return {"checked": False}
    roh = _sol_runde(mint, quellen, sup)
    # Seit 2.63: Empfaenger, die selbst wieder weitergereicht haben, Station fuer Station bis MAX_HOPS
    schon_quelle = {w for w, _ in quellen}
    for _ in range(MAX_HOPS - 1):
        neu = []
        for f in roh:
            if f["to"] not in schon_quelle:
                schon_quelle.add(f["to"])
                neu.append(f["to"])
        neu = neu[:25]
        if not neu:
            break
        konten = intel._sol_batch([("getTokenAccountsByOwner", [z, {"mint": mint}, {"encoding": "jsonParsed"}]) for z in neu], 20)
        nq = []
        for z, r in zip(neu, konten):
            for x in (r or {}).get("value") or []:
                if x.get("pubkey"):
                    nq.append((z, x["pubkey"]))
        if not nq:
            break
        roh += _sol_runde(mint, nq, sup)
    return _sol_ende(mint, roh, kaeufer, dev, sup)


def _sol_runde(mint, quellen, sup):
    import intel
    sigs = intel._sol_batch([("getSignaturesForAddress", [kt, {"limit": 40}]) for _, kt in quellen], 20)
    alle = []
    for (w, kt), r in zip(quellen, sigs):
        for s in (r if isinstance(r, list) else []):
            if not s.get("err") and s.get("signature"):
                alle.append((w, s["signature"]))
    gesehen, eindeutig = set(), []
    for w, sg in alle:
        if sg not in gesehen:
            gesehen.add(sg)
            eindeutig.append((w, sg))
    eindeutig = eindeutig[:MAX_TX_SOL]
    txs = intel._sol_batch([intel._tx(sg) for _, sg in eindeutig], 20)
    klein = sup * MIN_ANTEIL / 100
    roh = []
    for (w, sg), tx in zip(eindeutig, txs):
        if not isinstance(tx, dict) or (tx.get("meta") or {}).get("err"):
            continue
        if not _nur_uebertragung(tx):
            continue
        d = intel._token_delta(tx, mint)
        for von, minus in d.items():
            if minus >= 0:
                continue
            empf = [(o, v) for o, v in d.items() if v > 0 and o != von]
            if not empf:
                continue
            # Verkauf: der Absender bekommt SOL (mehr als Gebuehren). Dann ist es keine Weiterleitung.
            if _sol_aenderung(tx, von) > 2_000_000:
                continue
            for o, v in empf:
                if v >= klein:
                    roh.append({"from": von, "to": o, "raw": v, "tx": sg})
    return roh


def _sol_ende(mint, roh, kaeufer, dev, sup):
    import intel
    gesehen_tx = set()
    roh = [f for f in roh if (f["from"], f["to"], f["tx"]) not in gesehen_tx and not gesehen_tx.add((f["from"], f["to"], f["tx"]))]
    if not roh:
        return {"checked": True, "count": 0}
    # Nur echte Wallets als Empfaenger (System-Konten); Pools und Programme fallen raus
    ziele = sorted({f["to"] for f in roh} | {f["from"] for f in roh})
    besitzer = {}
    for i in range(0, len(ziele), 100):
        r = intel._sol("getMultipleAccounts", [ziele[i:i + 100], {"encoding": "base64"}]) or {}
        for a, v in zip(ziele[i:i + 100], r.get("value") or []):
            besitzer[a] = (v or {}).get("owner")
    liste = [f for f in roh if besitzer.get(f["to"]) in (SYSTEM, None) and besitzer.get(f["from"]) in (SYSTEM, None)]
    if not liste:
        return {"checked": True, "count": 0}
    empfaenger = sorted({f["to"] for f in liste})[:120]

    def frisch_von(a):
        r = intel._sol("getSignaturesForAddress", [a, {"limit": 25}])
        if not isinstance(r, list):
            return a, False
        zeiten = [x.get("blockTime") for x in r if x.get("blockTime")]
        return a, len(r) < 25 and bool(zeiten) and (time.time() - min(zeiten)) < 7 * 86400

    with ThreadPoolExecutor(8) as ex:
        frisch = dict(ex.map(frisch_von, empfaenger))
    best = intel._sol_batch([("getTokenAccountsByOwner", [z, {"mint": mint}, {"encoding": "jsonParsed"}]) for z in empfaenger], 20)
    halten = {}
    for z, r in zip(empfaenger, best):
        try:
            halten[z] = sum(int(x["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for x in (r or {}).get("value") or [])
        except Exception:
            halten[z] = None
    return _zusammenfassen("solana", mint, liste, halten, frisch, {k["wallet"] for k in kaeufer}, dev, sup)


# ── Spur: was haben die Empfaenger mit den Tokens gemacht? (seit 2.49.5) ──────

FENSTER = {"robinhood": 200000, "arbitrum": 2000, "base": 500, "ethereum": 100, "bsc": 300}


_ZIEL = {}


_VK = {}
SWAP_TOPICS = {
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",  # Uniswap V2 Swap
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",  # Uniswap V3 Swap
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",  # Uniswap V4 Swap (PoolManager)
}
_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def ist_verkauf(chain, tx, w, token):
    """True, wenn die Transaktion ein Swap war: Swap-Event eines Pools im Beleg, oder die Wallet bekam im selben
    Zug einen anderen Token. So werden Verkaeufe ueber Router und Aggregatoren nicht mehr als 'Vertrag' gemeldet."""
    if not tx:
        return False
    if tx in _VK:
        return _VK[tx]
    try:
        import scan
        rc = scan._evm(chain, "eth_getTransactionReceipt", [tx]) or {}
    except Exception:
        return False
    w = w.lower()[2:]
    for x in rc.get("logs") or []:
        t = x.get("topics") or []
        if not t:
            continue
        if t[0] in SWAP_TOPICS:
            _VK[tx] = True
            return True
        if (t[0] == _TRANSFER and len(t) > 2 and t[2].lower().endswith(w)
                and str(x.get("address") or "").lower() != token.lower()):
            _VK[tx] = True
            return True
    _VK[tx] = False
    return False


def ziel_art(chain, adr):
    """Seit 2.50.10: wohin gingen Tokens? 'lock' (bekannter Locker), 'pool' (Handelspaar: hat token0()),
    'burn', 'contract' (anderer Vertrag, Lock oder Verkauf nicht sicher) oder 'wallet'."""
    import intel
    import scan
    a = str(adr).lower()
    if a in ("0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"):
        return "burn"
    if a in {x.lower() for x in (intel.LOCKER_EVM.get(chain) or {})}:
        return "lock"
    k = (chain, a)
    if k in _ZIEL:
        return _ZIEL[k]
    try:
        import codeart
        code = codeart.norm(scan._evm(chain, "eth_getCode", [a, "latest"]))
    except Exception:
        return "contract"
    if code in ("0x", "0x0", None):
        art = "wallet"
    else:
        try:
            t0 = scan._evm(chain, "eth_call", [{"to": a, "data": "0x0dfe1681"}, "latest"])
            art = "pool" if t0 and len(t0) >= 66 and int(t0, 16) > 0 else "contract"
        except Exception:
            art = "contract"
    _ZIEL[k] = art
    return art


def _logs_wallet(chain, token, wallet, von, bis, richtung):
    """Transfer-Logs des Coins, bei denen wallet Absender (richtung 1) oder Empfaenger (2) ist."""
    import scan
    pad = "0x" + "0" * 24 + wallet[2:]
    topics = [TRANSFER, pad, None] if richtung == 1 else [TRANSFER, None, pad]
    fen = int(os.getenv("TRACE_WINDOW_" + chain.upper(), "0") or 0) or FENSTER.get(chain, 2000)
    # Seit 2.58: Fenster parallel statt nacheinander (bis zu 40 Abfragen machten jeden Robinhood-Scan langsam)
    fenster, b = [], bis
    while b >= von and len(fenster) < int(os.getenv("TRACE_MAX_CALLS", "60")):
        a = max(von, b - fen + 1)
        fenster.append((a, b))
        b = a - 1

    def hol(ab):
        # Seit 2.78.2 mit Wiederholung: ein einziges 429 im Paket machte den ganzen Dev-Check "incomplete",
        # dann stand "0% sold" auf der Karte, obwohl der Dev verkauft hatte.
        for versuch in range(5):
            try:
                return scan._evm(chain, "eth_getLogs", [{"fromBlock": hex(ab[0]), "toBlock": hex(ab[1]), "address": token, "topics": topics}]) or []
            except Exception:
                time.sleep(0.5 * (versuch + 1))
        return None
    with ThreadPoolExecutor(max(1, min(4, len(fenster)))) as ex:
        teile = list(ex.map(hol, fenster))
    out = [l for t in teile if t for l in t]
    return out, b < von and all(t is not None for t in teile)


def spur(chain, token, receivers, start_block, sup):
    """Je Empfaenger: erhalten, verkauft (an Pool, Router oder Vertrag), weitergeschickt (an andere Wallets),
    haelt noch. Nur EVM. -> {"wallets": [...], "complete": bool}"""
    import scan
    if chain == "solana" or not receivers:
        return {"supported": False}
    top = int(scan._evm(chain, "eth_blockNumber", []), 16)
    code = {}

    def ist_vertrag(a):
        if a not in code:
            try:
                import codeart
                code[a] = codeart.norm(scan._evm(chain, "eth_getCode", [a, "latest"])) not in ("0x", "0x0")
            except Exception:
                code[a] = None
        return code[a]
    pct = lambda raw: round(raw / sup * 100, 3) if sup else None
    import intel
    locker = {a.lower(): n for a, n in (intel.LOCKER_EVM.get(chain) or {}).items()}
    out, vollstaendig = [], True
    for w in receivers[:6]:
        w = w.lower()
        rein, ok1 = _logs_wallet(chain, token, w, start_block, top, 2)
        raus, ok2 = _logs_wallet(chain, token, w, start_block, top, 1)
        vollstaendig = vollstaendig and ok1 and ok2
        verkauft, gesperrt, vertrag_x, weiter_an = 0, 0, 0, {}
        verkaeufe = 0
        for l in raus:
            an = "0x" + l["topics"][2][-40:]
            menge = int(l.get("data") or "0x0", 16)
            art = ziel_art(chain, an)
            if art == "lock":
                gesperrt += menge          # seit 2.50.4: in einen Token-Locker geschickt ist kein Verkauf
            elif art in ("pool", "burn"):
                verkauft += menge
                verkaeufe += 1
            elif art == "contract" and ist_verkauf(chain, l.get("transactionHash"), w, token):
                verkauft += menge          # seit 2.60.6: Verkauf ueber Router/Aggregator
                verkaeufe += 1
            elif art == "contract":
                vertrag_x += menge         # seit 2.50.10: unbekannter Vertrag, Lock oder Verkauf nicht sicher
            else:
                weiter_an[an] = weiter_an.get(an, 0) + menge
        try:
            haelt = int(scan._evm(chain, "eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + w[2:]}, "latest"]), 16)
        except Exception:
            haelt = None
        out.append({"wallet": w, "received_pct": pct(sum(int(l.get("data") or "0x0", 16) for l in rein)),
                    "sold_or_swapped_pct": pct(verkauft), "locked_pct": pct(gesperrt),
                    "to_unknown_contract_pct": pct(vertrag_x), "sells": verkaeufe,
                    "sent_on": [{"to": a, "pct": pct(v)} for a, v in sorted(weiter_an.items(), key=lambda x: -x[1])[:5]],
                    "holds_now_pct": pct(haelt) if haelt is not None else None,
                    "explorer": scan.explorer(chain, "address", w)})
    return {"supported": True, "wallets": out, "complete": vollstaendig}


# ── Schnell-Check im normalen Scan (seit 2.49.7): wohin sind die Dev-Tokens gegangen? ──────

_DEV_CACHE = {}


def dev_raus(chain, token, dev):
    """Nur EVM. Ausgehende Transfers des Devs seit dem Launch: verkauft (an Pool, Router, Vertrag) oder an
    normale Wallets weitergeschickt, und was diese Wallets heute noch halten. Wenige RPC-Aufrufe, 10 Min Speicher.
    -> {"sold_pct", "forwarded_pct", "to": [{"to", "pct", "holds_pct"}], "complete"} oder None"""
    import intel
    import scan
    if chain == "solana" or not dev or not str(dev).startswith("0x"):
        return None
    key = (chain, token.lower(), dev.lower())
    c = _DEV_CACHE.get(key)
    if c and time.time() - c[0] < 600:
        return c[1]
    try:
        top = int(scan._evm(chain, "eth_blockNumber", []), 16)
        sup = int(scan._evm(chain, "eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), 16)
    except Exception:
        return None
    if not sup:
        return None
    try:
        start, _ = intel._start_evm(chain, token)
    except Exception:
        start = None
    fen = FENSTER.get(chain, 2000)
    # Seit 2.50.14: so viele Abfragen, dass der ganze Weg seit dem Launch abgedeckt ist (vorher fehlten bei Coins,
    # die aelter als etwa 14 Stunden sind, die fruehen Dev-Transfers)
    obergrenze = int(os.getenv("DEV_OUT_MAX_CALLS", "40"))
    max_calls = min(obergrenze, (top - start) // fen + 2) if start else 8
    von = start if start else max(0, top - fen * max_calls)
    alt = os.environ.get("TRACE_MAX_CALLS")
    os.environ["TRACE_MAX_CALLS"] = str(max_calls)
    try:
        raus, ok = _logs_wallet(chain, token, dev.lower(), von, top, 1)
        rein, ok_rein = _logs_wallet(chain, token, dev.lower(), von, top, 2)
    except Exception:
        return None
    finally:
        if alt is None:
            os.environ.pop("TRACE_MAX_CALLS", None)
        else:
            os.environ["TRACE_MAX_CALLS"] = alt
    verkauft, gesperrt, unklar, an, code = 0, 0, 0, {}, {}
    lk = {x.lower() for x in (intel.LOCKER_EVM.get(chain) or {})}
    ende = time.time() + float(os.getenv("DEV_OUT_BUDGET_SEC", "6"))
    for l in raus:
        ziel = "0x" + l["topics"][2][-40:]
        menge = int(l.get("data") or "0x0", 16)
        if ziel.lower() in lk:
            gesperrt += menge
            continue
        if ziel not in code:
            if time.time() > ende:
                unklar += menge   # seit 2.78.2: Zeit um, Rest als unklar statt den ganzen Check zu verwerfen
                ok = False
                continue
            code[ziel] = ziel_art(chain, ziel)
        art = code[ziel]
        if art in ("pool", "burn"):
            verkauft += menge
        elif art == "contract" and time.time() < ende and ist_verkauf(chain, l.get("transactionHash"), dev, token):
            verkauft += menge      # seit 2.60.6: Verkauf ueber Router/Aggregator
        elif art == "contract":
            unklar += menge
        else:
            an[ziel] = an.get(ziel, 0) + menge
    pct = lambda x: round(x / sup * 100, 3)
    # Seit 2.78.2: woher hatte der Dev seine Tokens? Aus dem Mint (eigene Zuteilung), gekauft (von Pool/Vertrag)
    # oder von Wallets bekommen. Auf Launchpads hat der Dev oft gar keine Zuteilung, nur seine eigenen Kaeufe.
    vom_mint, gekauft_, bekommen = 0, 0, 0
    for l in rein:
        quelle = "0x" + l["topics"][1][-40:]
        menge = int(l.get("data") or "0x0", 16)
        if quelle in ("0x0000000000000000000000000000000000000000",):
            vom_mint += menge
            continue
        if quelle not in code:
            code[quelle] = ziel_art(chain, quelle) if time.time() < ende + 4 else "contract"
        if code[quelle] in ("pool", "contract"):
            gekauft_ += menge
        else:
            bekommen += menge
    liste = []
    for ziel, menge in sorted(an.items(), key=lambda x: -x[1])[:5]:
        if pct(menge) < MIN_ANTEIL:
            continue
        try:
            h = int(scan._evm(chain, "eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + ziel[2:]}, "latest"]), 16)
            h = pct(h)
        except Exception:
            h = None
        liste.append({"to": ziel, "pct": pct(menge), "holds_pct": h})
    out = {"got_from_mint_pct": pct(vom_mint), "bought_pct": pct(gekauft_), "received_from_wallets_pct": pct(bekommen),
           "buys": sum(1 for l in rein if "0x" + l["topics"][1][-40:] != "0x0000000000000000000000000000000000000000"),
           "sold_pct": pct(verkauft), "locked_pct": pct(gesperrt), "unknown_contract_pct": pct(unklar), "forwarded_pct": round(sum(x["pct"] for x in liste), 3), "to": liste,
           "complete": bool(ok and ok_rein and start)}
    _DEV_CACHE[key] = (time.time(), out)
    return out
