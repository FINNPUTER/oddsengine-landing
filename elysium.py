"""
elysium.py: Scanner fuer Elysium (Kinetiq, Arbitrum-Orbit-L2 auf HyperEVM, Chain 99901, Gas HYPE). Seit 2.82.15.

Preis, Market Cap, Liquiditaet und Volumen kommen von DexScreener (chainId "elysium"), sonst GeckoTerminal (Netz "elysium").
Vertrag und Supply liest der Scanner per RPC, Ersteller, Holder, erste Kaeufer und Geldgeber ueber den
Blockscout-Explorer von Kinetiq. Danach gleicht er jede gefundene Adresse mit allem ab, was die Engine schon kennt:
  - fruehe Kaeufe in anderen Coins (iw_early, alle Chains)          - Smart-Money-Bewertung (iw_wallets)
  - Devs anderer Coins (iw_devs, Torch-Launches auf Robinhood)       - Wallet-Kartei (Art, Name, Geldgeber, Markierungen)
  - gemeinsame Geldgeber unter den gescannten Wallets (Cluster)      - Aktivitaet auf Robinhood Chain (gleiche Adresse)
EVM-Adressen sind auf allen Chains dieselben, deshalb funktioniert der Abgleich ueber Chains hinweg.

Jeder Scan wird gespeichert (iw_tokens, iw_early, iw_devs, Kartei mit chain='elysium'). So wachsen die Verbindungen
zwischen Elysium-Projekten mit jedem Scan.

Einstellungen: ELYSIUM_RPC (Standard https://rpc.elysium.kinetiq.xyz), ELYSIUM_API (Blockscout, Standard
https://elysium.kinetiq.xyz/api, danach .../explorer/api), ELYSIUM_EXPLORER (Links, Standard
https://elysium.kinetiq.xyz/explorer). Schalter "elysium" (aus: nur Admin und Beta).
"""
import os
import time
from collections import Counter, defaultdict

import requests

from logger import log

CHAIN = "elysium"
CHAIN_ID = 99901
RPC = os.getenv("ELYSIUM_RPC", "https://rpc.elysium.kinetiq.xyz").rstrip("/")
EXPLORER = os.getenv("ELYSIUM_EXPLORER", "https://elysium.kinetiq.xyz/explorer").rstrip("/")
APIS = [a.rstrip("/") for a in (os.getenv("ELYSIUM_API", ""), "https://elysium.kinetiq.xyz/api",
                                "https://elysium.kinetiq.xyz/explorer/api") if a]
UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
NULL = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RH_API = "https://robinhoodchain.blockscout.com/api"
_API_OK = [None]
_CODE = {}


def an():
    try:
        import features
        return features.an("elysium")
    except Exception:
        return False


# ── Zugriff ─────────────────────────────────────────────────────────────

def _rpc(methode, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": methode, "params": params}, headers=UA, timeout=15)
    d = r.json()
    if d.get("error"):
        raise RuntimeError(str(d["error"])[:160])
    return d.get("result")


def _call(to, data):
    try:
        return _rpc("eth_call", [{"to": to, "data": data}, "latest"]) or "0x"
    except Exception:
        return "0x"


def _str(hexd):
    try:
        b = bytes.fromhex(hexd[2:])
        if len(b) >= 64:
            n = int.from_bytes(b[32:64], "big")
            return b[64:64 + n].decode("utf-8", "ignore").strip("\x00").strip()
        return b.decode("utf-8", "ignore").strip("\x00").strip()
    except Exception:
        return ""


def _int(hexd):
    try:
        return int(hexd, 16) if hexd and hexd != "0x" else 0
    except Exception:
        return 0


def _pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def _bs(params, api=None):
    """Blockscout im Etherscan-Stil. Nimmt die erste Basis, die antwortet, und merkt sie sich."""
    liste = [api] if api else ([_API_OK[0]] if _API_OK[0] else []) + [a for a in APIS if a != _API_OK[0]]
    for basis in liste:
        try:
            r = requests.get(basis, params=params, headers=UA, timeout=15)
            if r.status_code != 200:
                continue
            d = r.json()
            if not api:
                _API_OK[0] = basis
            res = d.get("result")
            if isinstance(res, list) or isinstance(res, dict):
                return res
            if str(d.get("status")) == "0":
                return []
        except Exception as e:
            log.debug(f"elysium bs {basis}: {e}")
    return None


def _ist_vertrag(a):
    a = a.lower()
    if a not in _CODE:
        try:
            _CODE[a] = (_rpc("eth_getCode", [a, "latest"]) or "0x") not in ("0x", "0x0")
        except Exception:
            _CODE[a] = None
    return _CODE[a]


def _link(art, a):
    return f"{EXPLORER}/{art}/{a}"


# ── Bausteine ───────────────────────────────────────────────────────────

def meta(ca):
    name = _str(_call(ca, "0x06fdde03"))
    sym = _str(_call(ca, "0x95d89b41"))
    dez = _int(_call(ca, "0x313ce567")) or 18
    sup = _int(_call(ca, "0x18160ddd")) / 10 ** dez
    weg = (_int(_call(ca, "0x70a08231" + _pad(DEAD))) + _int(_call(ca, "0x70a08231" + _pad(NULL)))) / 10 ** dez
    return {"name": name, "symbol": sym, "decimals": dez, "supply": sup, "burned": weg}


def _v2(pfad, params=None):
    """Blockscout v2 (JSON). Basis aus der funktionierenden API-Adresse: .../api -> .../api/v2"""
    for basis in ([_API_OK[0]] if _API_OK[0] else []) + [a for a in APIS if a != _API_OK[0]]:
        try:
            r = requests.get(basis.rstrip("/") + "/v2" + pfad, params=params or {}, headers=UA, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log.debug(f"elysium v2 {pfad}: {e}")
    return None


def ersteller(ca):
    """-> (dev, tx, fabrik). Wurde der Coin ueber eine Fabrik (Launchpad) erstellt, ist der Dev der Absender der
    Erstell-Transaktion, nicht die Fabrik."""
    dev, tx = None, None
    r = _bs({"module": "contract", "action": "getcontractcreation", "contractaddresses": ca})
    if isinstance(r, list) and r:
        dev, tx = (r[0].get("contractCreator") or "").lower() or None, r[0].get("txHash")
    if not dev:
        d = _v2(f"/addresses/{ca}") or {}
        dev = (d.get("creator_address_hash") or "").lower() or None
        tx = d.get("creation_transaction_hash") or d.get("creation_tx_hash")
    fabrik = None
    if tx:
        try:
            t = _rpc("eth_getTransactionByHash", [tx]) or {}
            absender = (t.get("from") or "").lower()
            if absender and absender != dev:
                fabrik, dev = dev, absender
        except Exception as e:
            log.debug(f"elysium tx {tx}: {e}")
    return dev, tx, fabrik


# Elysium ist eine Arbitrum-Orbit-Chain. Eine Einzahlung von HyperEVM erscheint hier als Zahlung von der
# "Alias-Adresse" des Absenders: Absender + 0x1111...1111 (erste und letzte zwei Bytes je +0x1111, die Mitte gleich).
# Das ist KEINE eigene Wallet. Seit 2.82.17 wird sie zurueckgerechnet.
ALIAS = 0x1111000000000000000000000000000000001111


def ohne_alias(a):
    try:
        return "0x" + format((int(a, 16) - ALIAS) % (1 << 160), "040x")
    except (TypeError, ValueError):
        return None


def _nonce(a):
    try:
        return _int(_rpc("eth_getTransactionCount", [a, "latest"]))
    except Exception:
        return None


def geldgeber(w):
    """Erste eingehende Zahlung mit Wert: wer hat diese Wallet zuerst mit HYPE gefuellt.
    bridge=True: das Geld kam ueber die Bruecke von HyperEVM. self=True: die Wallet hat es selbst herueber gebracht,
    dann ist "funder" die Wallet selbst und die Spur geht auf HyperEVM weiter."""
    w = w.lower()
    r = _bs({"module": "account", "action": "txlist", "address": w, "sort": "asc", "page": 1, "offset": 10})
    for t in r or []:
        if (t.get("to") or "").lower() == w and int(t.get("value") or 0) > 0:
            f = (t.get("from") or "").lower()
            g = {"funder": f, "tx": t.get("hash"), "ts": int(t.get("timeStamp") or 0),
                 "hype": round(int(t.get("value") or 0) / 1e18, 4)}
            roh = ohne_alias(f)
            if roh == w:
                g.update(bridge=True, self=True, funder=w, alias=f,
                         note="Bridged in from HyperEVM by the wallet itself. The sender shown on Elysium is only its bridge alias.")
            elif roh and _nonce(f) == 0 and _ist_vertrag(f) is False:
                # Absender hat auf Elysium nie selbst etwas gesendet: sehr wahrscheinlich ein Bruecken-Alias
                g.update(bridge=True, self=False, funder=roh, alias=f,
                         note="Probably bridged in from HyperEVM by this sender (address shown on Elysium is its bridge alias).")
            return g
    return None


def holder(ca, dez, sup, n=30):
    r = _bs({"module": "token", "action": "getTokenHolders", "contractaddress": ca, "page": 1, "offset": n})
    out = []
    for h in r or []:
        a = (h.get("address") or "").lower()
        v = int(h.get("value") or 0) / 10 ** dez
        out.append({"wallet": a, "amount": v, "pct": round(v / sup * 100, 3) if sup else None})
    if not out:  # zweiter Weg: Blockscout v2
        d = _v2(f"/tokens/{ca}/holders") or {}
        for h in d.get("items") or []:
            a = ((h.get("address") or {}).get("hash") or "").lower()
            try:
                v = int(h.get("value") or 0) / 10 ** dez
            except (TypeError, ValueError):
                continue
            if a:
                out.append({"wallet": a, "amount": v, "pct": round(v / sup * 100, 3) if sup else None})
    return sorted(out, key=lambda x: -x["amount"])[:n]


def bestand(ca, w, dez):
    """Bestand einer Wallet direkt von der Chain, None wenn der RPC nicht antwortet."""
    try:
        return _int(_rpc("eth_call", [{"to": ca, "data": "0x70a08231" + _pad(w)}, "latest"])) / 10 ** dez
    except Exception:
        return None


def transfers(ca, n=400):
    r = _bs({"module": "account", "action": "tokentx", "contractaddress": ca, "sort": "asc", "page": 1, "offset": n})
    return r or []


def erste_kaeufer(ca, dez, tx, dev, n=40):
    """Erste Empfaenger von Tokens, ohne Null, Dead, den Vertrag selbst und Vertraege (Pools, Router, Launchpad)."""
    gesehen, out = set(), []
    for t in tx:
        an_ = (t.get("to") or "").lower()
        if an_ in (NULL, DEAD, ca.lower()) or an_ in gesehen:
            continue
        gesehen.add(an_)
        if _ist_vertrag(an_):
            continue
        out.append({"wallet": an_, "rank": len(out) + 1, "block": int(t.get("blockNumber") or 0),
                    "ts": int(t.get("timeStamp") or 0), "from": (t.get("from") or "").lower(),
                    "amount": int(t.get("value") or 0) / 10 ** dez, "tx": t.get("hash"), "is_dev": an_ == dev})
        if len(out) >= n:
            break
    return out


def markt(ca):
    """Preis und Market Cap: DexScreener (Paar mit der meisten Liquiditaet auf Elysium), sonst GeckoTerminal."""
    try:
        d = requests.get(f"https://api.dexscreener.com/tokens/v1/elysium/{ca}", headers=UA, timeout=10).json()
        ps = d if isinstance(d, list) else (d.get("pairs") or [])
        if not ps:
            d = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", headers=UA, timeout=10).json()
            ps = [p for p in (d.get("pairs") or []) if p.get("chainId") == "elysium"]
        eig = [p for p in ps if str((p.get("baseToken") or {}).get("address", "")).lower() == ca.lower()] or ps
        if eig:
            p = max(eig, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
            return {"source": "DexScreener", "price_usd": float(p.get("priceUsd") or 0) or None,
                    "mcap_usd": p.get("marketCap") or p.get("fdv"), "fdv_usd": p.get("fdv"),
                    "liquidity_usd": (p.get("liquidity") or {}).get("usd"), "volume_usd": p.get("volume"),
                    "change_pct": p.get("priceChange"), "txns": p.get("txns"), "dex": p.get("dexId"),
                    "pair": p.get("pairAddress"), "quote": (p.get("quoteToken") or {}).get("symbol"),
                    "created_ms": p.get("pairCreatedAt"), "chart": p.get("url"), "pairs": len(eig)}
    except Exception as e:
        log.debug(f"elysium dexscreener {ca[:10]}: {e}")
    try:
        d = requests.get(f"https://api.geckoterminal.com/api/v2/networks/elysium/tokens/{ca}", headers=UA, timeout=10).json()
        a = (d.get("data") or {}).get("attributes") or {}
        if a:
            return {"source": "GeckoTerminal", "price_usd": float(a.get("price_usd") or 0) or None,
                    "mcap_usd": a.get("market_cap_usd") or a.get("fdv_usd"), "fdv_usd": a.get("fdv_usd"),
                    "liquidity_usd": a.get("total_reserve_in_usd"), "volume_usd": a.get("volume_usd"),
                    "chart": f"https://www.geckoterminal.com/elysium/tokens/{ca}"}
    except Exception as e:
        log.debug(f"elysium gecko {ca[:10]}: {e}")
    return {}


def robinhood_aktiv(w):
    r = _bs({"module": "account", "action": "txlist", "address": w, "page": 1, "offset": 1, "sort": "asc"}, api=RH_API)
    if isinstance(r, list) and r:
        return {"first_ts": int(r[0].get("timeStamp") or 0)}
    return None


# ── Abgleich mit allem, was die Engine kennt ────────────────────────────

def verbindungen(wallets, rollen=None, robinhood=True, token=None):
    """wallets: Liste Adressen. -> {adresse: [Treffer]}, nur Adressen mit mindestens einem Treffer."""
    import intel
    rollen = rollen or {}
    ws = sorted({w.lower() for w in wallets if w and w.lower() not in (NULL, DEAD)})
    if not ws:
        return {}
    treffer = defaultdict(list)
    # 1) fruehe Kaeufe in anderen Coins, jede Chain
    for w, chain, tok, rank, sym, mult in intel._q(
            """SELECT e.wallet, e.chain, e.token, e.rank, t.symbol, t.multiple FROM iw_early e
               LEFT JOIN iw_tokens t ON t.chain = e.chain AND t.token = e.token
               WHERE lower(e.wallet) = ANY(%s) AND NOT (e.chain = 'elysium' AND e.token = %s)
               ORDER BY t.multiple DESC NULLS LAST""", (ws, (token or "").lower())) or []:
        treffer[w.lower()].append({"kind": "early_buyer", "chain": chain, "token": tok, "symbol": sym, "rank": rank,
                                   "multiple": round(float(mult), 1) if mult else None})
    # 2) Smart-Money-Bewertung
    for w, chain, score, label, winners, best in intel._q(
            "SELECT wallet, chain, score, label, winners, best_x FROM iw_wallets WHERE lower(wallet) = ANY(%s) AND score > 0",
            (ws,)) or []:
        treffer[w.lower()].append({"kind": "smart_wallet", "chain": chain, "score": score, "label": label,
                                   "winners": winners, "best_x": round(float(best or 0), 1)})
    # 3) Dev anderer Coins
    for chain, dev, tok in intel._q("SELECT chain, dev, token FROM iw_devs WHERE lower(dev) = ANY(%s) "
                                    "AND NOT (chain = 'elysium' AND token = %s)", (ws, (token or "").lower())) or []:
        treffer[dev.lower()].append({"kind": "dev_of", "chain": chain, "token": tok})
    try:
        import onchain
        menge = set(ws)
        for l in onchain.launches() or []:
            c = str(l.get("creator") or "").lower()
            if c in menge:
                treffer[c].append({"kind": "dev_of", "chain": "robinhood", "token": l.get("token"), "via": "Torch launch"})
    except Exception as e:
        log.debug(f"elysium torch launches: {e}")
    # 4) Kartei: Art, Name, Geldgeber, Markierungen (auch: diese Adresse hat andere Wallets gefuellt)
    try:
        import kartei
        for chain, w, kind, name, funder in kartei._q(
                "SELECT chain, wallet, kind, name, funder FROM wl_wallets WHERE lower(wallet) = ANY(%s)", (ws,)) or []:
            if kind or name:
                treffer[w.lower()].append({"kind": "known_wallet", "chain": chain, "type": kind, "name": name})
        for chain, w, flag, detail in kartei._q(
                "SELECT chain, wallet, flag, detail FROM wl_flags WHERE lower(wallet) = ANY(%s)", (ws,)) or []:
            treffer[w.lower()].append({"kind": "flag", "chain": chain, "flag": flag, "detail": detail})
        for funder, n, chains in kartei._q(
                """SELECT lower(funder), count(*), string_agg(DISTINCT chain, ',') FROM wl_wallets
                   WHERE lower(funder) = ANY(%s) AND NOT (lower(wallet) = ANY(%s)) GROUP BY lower(funder)""", (ws, ws)) or []:
            treffer[funder].append({"kind": "funded_wallets", "count": n, "chains": chains})
    except Exception as e:
        log.debug(f"elysium kartei: {e}")
    # 5) gleiche Adresse auf Robinhood Chain aktiv (nur fuer die wichtigsten Rollen, kostet Abfragen)
    if robinhood:
        for w in [w for w in ws if rollen.get(w) in ("dev", "dev_funder", "funder", "wallet")][:12]:
            akt = robinhood_aktiv(w)
            if akt:
                treffer[w].append({"kind": "active_on", "chain": "robinhood", "since": akt["first_ts"]})
    return {w: v for w, v in treffer.items() if v}


# ── Scan ────────────────────────────────────────────────────────────────

def scan(ca, speichern=True):
    ca = (ca or "").strip()
    if not (ca.startswith("0x") and len(ca) == 42):
        return {"error": "Send the token contract address on Elysium (0x...)."}
    ca_l = ca.lower()
    try:
        m = meta(ca)
    except Exception as e:
        return {"error": f"Elysium RPC not reachable ({str(e)[:80]}). Set ELYSIUM_RPC."}
    if not m["symbol"] and not m["supply"]:
        return {"error": "No ERC-20 token at this address on Elysium (chain 99901)."}
    dev, dev_tx, fabrik = ersteller(ca)
    out = {"chain": "Elysium", "chain_id": CHAIN_ID, "token": ca_l, "name": m["name"], "symbol": m["symbol"],
           "supply": m["supply"], "burned_pct": round(m["burned"] / m["supply"] * 100, 3) if m["supply"] else None,
           "explorer": _link("token", ca), "dev": dev, "dev_link": _link("address", dev) if dev else None}
    if fabrik:
        out["created_via"] = {"contract": fabrik, "link": _link("address", fabrik)}
    mk = markt(ca)
    out["market"] = mk or {"note": "No trading pair found on DexScreener or GeckoTerminal yet."}
    if mk.get("price_usd") and not mk.get("mcap_usd") and m["supply"]:
        mk["mcap_usd"] = round(mk["price_usd"] * (m["supply"] - m["burned"]), 2)
    if _API_OK[0] is None and dev is None:
        out["warning"] = "Elysium explorer API did not answer. Holders, early buyers and funders are missing. Set ELYSIUM_API."
    rollen = {}
    # Dev und sein Geldgeber
    dev_funder = None
    if dev:
        rollen[dev] = "dev"
        dev_funder = geldgeber(dev)
        if dev_funder:
            out["dev_funded_by"] = dict(dev_funder, link=_link("address", dev_funder["funder"]))
            if not dev_funder.get("self"):
                rollen[dev_funder["funder"]] = "dev_funder"
                # eine Stufe weiter: woher hatte der Geldgeber sein HYPE
                try:
                    u = geldgeber(dev_funder["funder"])
                except Exception:
                    u = None
                if u:
                    out["dev_funded_by"]["funder_origin"] = dict(
                        u, minutes_before_funding_dev=round((dev_funder["ts"] - u["ts"]) / 60, 1) if u.get("ts") and dev_funder.get("ts") else None)
                    if not u.get("self"):
                        rollen.setdefault(u["funder"], "funder")
    # Holder
    hs = holder(ca, m["decimals"], m["supply"])
    out["top_holders"] = [dict(h, contract=_ist_vertrag(h["wallet"]), is_dev=h["wallet"] == dev) for h in hs[:20]]
    wallet_holder = [h for h in hs if not _ist_vertrag(h["wallet"])]
    # ohne Holder-Liste keine Zahl (vorher stand dann faelschlich 0 %)
    out["top10_wallets_pct"] = round(sum(h["pct"] or 0 for h in wallet_holder[:10]), 2) if hs else None
    if not hs:
        out["holders_note"] = "Holder list not available from the Elysium explorer right now."
    if dev:
        b = bestand(ca, dev, m["decimals"])
        out["dev_holds_pct"] = round(b / m["supply"] * 100, 3) if b is not None and m["supply"] else None
    # erste Kaeufer und gleiche Bloecke
    tx = transfers(ca)
    fk = erste_kaeufer(ca, m["decimals"], tx, dev)
    out["early_buyers"] = fk[:30]
    if fk:
        bl = Counter(x["block"] for x in fk[:30])
        start = min(bl)
        out["first_block_buyers"] = bl[start]
        out["same_block_groups"] = [{"block": b, "wallets": n} for b, n in bl.most_common() if n >= 3][:5]
    for x in fk[:30]:
        rollen.setdefault(x["wallet"], "early")
    for h in wallet_holder[:15]:
        rollen.setdefault(h["wallet"], "holder")
    # Geldgeber der ersten 15 Kaeufer: gemeinsame Geldgeber = Cluster
    gg, selbst = {}, 0
    for x in fk[:15]:
        g = geldgeber(x["wallet"])
        if g and g.get("self"):
            selbst += 1
        elif g:
            gg[x["wallet"]] = g["funder"]
            rollen.setdefault(g["funder"], "funder")
    cl = defaultdict(list)
    for w, f in gg.items():
        cl[f].append(w)
    out["funding_clusters"] = [{"funder": f, "funder_link": _link("address", f), "wallets": ws,
                                "funder_is_dev": f == dev, "funder_is_dev_funder": bool(dev_funder and f == dev_funder["funder"])}
                               for f, ws in sorted(cl.items(), key=lambda kv: -len(kv[1])) if len(ws) >= 2 or f == dev]
    out["early_buyers_bridged_in_themselves"] = selbst
    if dev:
        aus_dev = [w for w, f in gg.items() if f == dev or (dev_funder and f == dev_funder["funder"])]
        out["early_buyers_funded_by_dev_side"] = len(aus_dev)
    # Abgleich mit der Datenbank
    v = verbindungen(list(rollen.keys()), rollen, token=ca_l)
    out["connections"] = [{"wallet": w, "role": rollen.get(w, "?"), "link": _link("address", w), "found": t}
                          for w, t in sorted(v.items(), key=lambda kv: (["dev", "dev_funder", "funder", "early", "holder"]
                                                                         .index(rollen.get(kv[0], "holder")), -len(kv[1])))]
    out["connections_summary"] = _zusammenfassung(out["connections"])
    if speichern:
        try:
            _merken(ca_l, m, dev, fk, gg, dev_funder)
        except Exception as e:
            log.warning(f"elysium merken: {e}")
    return out


def _zusammenfassung(conns):
    z = []
    for c in conns:
        for t in c["found"]:
            k = t["kind"]
            if k == "dev_of":
                z.append(f"{c['role']} {c['wallet'][:8]} is dev of {t.get('token', '')[:10]} on {t['chain']}")
            elif k == "early_buyer" and (t.get("multiple") or 0) >= 2:
                z.append(f"{c['role']} {c['wallet'][:8]} was early (#{t.get('rank')}) in {t.get('symbol') or t['token'][:8]} "
                         f"on {t['chain']}, which ran {t['multiple']}x")
            elif k == "smart_wallet":
                z.append(f"{c['role']} {c['wallet'][:8]} is a rated smart wallet on {t['chain']} (score {t['score']})")
            elif k == "flag":
                z.append(f"{c['role']} {c['wallet'][:8]} is flagged: {t['flag']}")
            elif k == "known_wallet":
                z.append(f"{c['role']} {c['wallet'][:8]} is known: {t.get('name') or t.get('type')}")
            elif k == "funded_wallets":
                z.append(f"{c['role']} {c['wallet'][:8]} funded {t['count']} wallets we know ({t['chains']})")
            elif k == "active_on":
                z.append(f"{c['role']} {c['wallet'][:8]} is also active on Robinhood Chain")
    return z[:25]


def _merken(ca, m, dev, fk, gg, dev_funder):
    import intel
    jetzt = int(time.time())
    intel._q("""INSERT INTO iw_tokens (chain, token, symbol, first_seen, checked, credited) VALUES (%s,%s,%s,%s,%s,TRUE)
                ON CONFLICT (chain, token) DO UPDATE SET checked=EXCLUDED.checked""", (CHAIN, ca, m["symbol"][:20], jetzt, jetzt))
    for x in fk:
        intel._q("""INSERT INTO iw_early (chain, token, wallet, rank, buy_ts, src) VALUES (%s,%s,%s,%s,%s,'elysium')
                    ON CONFLICT (chain, token, wallet) DO NOTHING""", (CHAIN, ca, x["wallet"], x["rank"], x["ts"]))
    if dev:
        intel._q("INSERT INTO iw_devs (chain, dev, token, ts) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (CHAIN, dev, ca, jetzt))
    try:
        import kartei
        for w, f in gg.items():
            kartei.geldgeber_merken(CHAIN, w, f, None, None, CHAIN)
        if dev and dev_funder:
            kartei.geldgeber_merken(CHAIN, dev, dev_funder["funder"], dev_funder.get("tx"), dev_funder.get("ts"), CHAIN)
    except Exception as e:
        log.debug(f"elysium kartei merken: {e}")


def wallet(adr):
    """Eine Wallet: welche Elysium-Coins sie bewegt hat, wer sie gefuellt hat, und alle Verbindungen."""
    adr = (adr or "").strip().lower()
    if not (adr.startswith("0x") and len(adr) == 42):
        return {"error": "Send a 0x wallet address."}
    out = {"chain": "Elysium", "wallet": adr, "link": _link("address", adr), "contract": _ist_vertrag(adr)}
    g = geldgeber(adr)
    if g:
        out["funded_by"] = dict(g, link=_link("address", g["funder"]))
    r = _bs({"module": "account", "action": "tokentx", "address": adr, "sort": "desc", "page": 1, "offset": 200}) or []
    coins = {}
    for t in r:
        c = (t.get("contractAddress") or "").lower()
        e = coins.setdefault(c, {"token": c, "symbol": t.get("tokenSymbol"), "in": 0, "out": 0, "first_ts": 0})
        if (t.get("to") or "").lower() == adr:
            e["in"] += 1
        else:
            e["out"] += 1
        e["first_ts"] = int(t.get("timeStamp") or 0)
    out["elysium_tokens"] = list(coins.values())[:40]
    rollen = {adr: "wallet"}
    if g and not g.get("self"):
        rollen[g["funder"]] = "funder"
    v = verbindungen(list(rollen.keys()), rollen)
    out["connections"] = [{"wallet": w, "role": rollen.get(w), "found": t} for w, t in v.items()]
    out["connections_summary"] = _zusammenfassung(out["connections"])
    return out
