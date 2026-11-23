"""
spur.py: Trace in Your Engine (seit 2.53). "Wer ist das? Woher kommt das Geld? Was ist in dieser Transaktion passiert?"

Drei Werkzeuge fuer das Modell, jede Aussage mit Explorer-Link als Beleg:
  wallet_profil(adr, chain)      Name/Art (Boerse, Locker, Pool, Burn, Vertrag, Wallet), Alter, Aktivitaet,
                                 Geldspur rueckwaerts bis zu einer Boerse/Bridge (bis TRACE_HOPS Schritte),
                                 Geschwister (andere Wallets vom selben Geldgeber), Score aus unseren Smart-Wallet-Daten
  tx_erklaeren(hash, chain)      wer, was, wie viel: Transfers, Swap, Approval, mit Token-Namen
  halter_wer(token, rang)        der N-groesste echte Halter eines Coins (ohne Pools, Locker, Burn) und sein Profil

Namen kommen nur aus Quellen, die es belegen: unsere Listen (Boersen, Locker, Burn), Blockscout-Namen und
oeffentliche Tags. Eine Person wird nie geraten. Ketten: Solana und EVM mit Blockscout (Robinhood, Base, Ethereum,
Arbitrum). BNB Chain nur mit Transaktion und Haltern, ohne Geldspur (kein freier Index).
"""
import os
import re
import time

import requests

from logger import log

HOPS = int(os.getenv("TRACE_HOPS", "4"))
BUDGET = float(os.getenv("TRACE_BUDGET_SEC", "25"))
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
EVM_KETTEN = ("robinhood", "base", "ethereum", "arbitrum", "bsc")
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")
_CACHE = {}


def _c(k, sek, f):
    v = _CACHE.get(k)
    if v and time.time() - v[0] < sek:
        return v[1]
    r = f()
    _CACHE[k] = (time.time(), r)
    return r


def _link(chain, art, a):
    import scan
    return scan.explorer(chain, art, a)


def _sym(chain, token):
    try:
        import onchain
        return "$" + ((onchain._meta([token]).get(token) or {}).get("symbol") or "") if chain == "robinhood" else token[:6] + "…"
    except Exception:
        return token[:6] + "…"


def _kurz(a):
    a = str(a or "")
    return a[:6] + "…" + a[-4:] if len(a) > 12 else a


def _nativ(chain):
    import scan
    return (scan.KETTEN.get(chain) or ("", "", "", "", "", "", "", "", "ETH"))[8]


# ── Namen ────────────────────────────────────────────────────────────────

def name(chain, adr):
    """Wie _name_roh, aber zuerst aus der Wallet-Kartei (seit 2.55), neue Erkenntnisse werden gespeichert, Markierungen haengen dran."""
    k = ("nm", chain, adr if chain in ("solana", "tron", "ton") else str(adr).lower())
    def hol():
        try:
            import kartei
            out = kartei.art(chain, adr)
        except Exception:
            kartei, out = None, None
        if not out:
            out = _name_roh(chain, adr)
            if kartei and (out.get("kind") != "wallet" or out.get("name")):
                try:
                    kartei.art_merken(chain, adr, out.get("kind"), out.get("name"), out.get("source"))
                except Exception:
                    pass
        out = dict(out)
        try:
            import kartei as _ka
            fl = _ka.flags(chain, adr)
            if fl:
                out["flags"] = [{"flag": f["flag"], "detail": f["detail"], "count": f["count"], "proof": f["proofs"][:2]} for f in fl]
        except Exception:
            pass
        return out
    return _c(k, 600, hol)


def _name_roh(chain, adr):
    """-> {"kind", "name", "source"}: was wir ueber die Adresse BELEGEN koennen."""
    import intel
    a = adr if chain in ("solana", "tron", "ton") else str(adr).lower()
    if chain != "solana":
        if a in intel.TOTE:
            return {"kind": "burn", "name": "burn address", "source": "standard"}
        lk = (intel.LOCKER_EVM.get(chain) or {}).get(a)
        if lk:
            return {"kind": "locker", "name": lk, "source": "OddsEngine list"}
        if a in intel.BOERSEN_EVM:
            return {"kind": "exchange", "name": "exchange hot wallet", "source": "OddsEngine list"}
        out = {"kind": "wallet", "name": None, "source": None}
        d = _c(("bsa", chain, a), 3600, lambda: intel._bs(chain, f"/api/v2/addresses/{a}")) if chain in intel.BLOCKSCOUT else None
        if isinstance(d, dict) and d.get("hash"):
            tags = [t.get("display_name") or t.get("label") for t in (d.get("public_tags") or []) if isinstance(t, dict)]
            tags += [t.get("name") for t in ((d.get("metadata") or {}).get("tags") or []) if isinstance(t, dict)]
            tags = [t for t in tags if t]
            if d.get("is_contract") and d.get("proxy_type") != "eip7702":
                # Seit 2.54.1: bei Proxys den Namen der Logik nehmen (z. B. TorchFeeRouter statt ERC1967Proxy)
                impl = next((i.get("name") for i in d.get("implementations") or [] if isinstance(i, dict) and i.get("name")), None)
                nm_ = impl or d.get("name") or (d.get("token") or {}).get("name")
                # Seit 2.56: Proxy mit eigenem Namen zeigt beides, z. B. "GnosisSafeProxy (implementation: Sweeper)"
                if impl and d.get("name") and d.get("name") != impl:
                    nm_ = f"{d['name']} (implementation: {impl})"
                if nm_ and "TorchFeeRouter" in nm_:
                    nm_ = "Torch fee router (creator and holder fee payouts, normal)"
                out = {"kind": "contract", "name": nm_, "source": "Blockscout"}
            if tags:
                out["name"] = ", ".join(dict.fromkeys(tags))[:80]
                out["source"] = "Blockscout public tag"
                if any(re.search(r"binance|coinbase|okx|kraken|bybit|bitget|kucoin|gate|mexc|htx|huobi|crypto\.com|exchange", t, re.I) for t in tags):
                    out["kind"] = "exchange"
                elif any(re.search(r"bridge|portal|across|stargate|wormhole|layerzero", t, re.I) for t in tags):
                    out["kind"] = "bridge"
            if out["kind"] == "wallet" and not tags:
                z = _c(("cnt", chain, a), 600, lambda: intel._bs(chain, f"/api/v2/addresses/{a}/counters")) or {}
                try:
                    n = int(z.get("transactions_count") or 0)
                except Exception:
                    n = 0
                if n > 5000:
                    out = {"kind": "service", "name": f"very busy wallet ({n:,} transactions): typical for an exchange, bridge or relayer",
                           "source": "activity"}
            if out["kind"] == "contract":
                try:
                    import weiter
                    art = weiter.ziel_art(chain, a)
                    if art == "pool":
                        out["kind"] = "pool"
                except Exception:
                    pass
        return out
    if adr in intel.BOERSEN_SOL:
        return {"kind": "exchange", "name": "exchange hot wallet", "source": "OddsEngine list"}
    if adr in intel.LOCKER_SOL:
        return {"kind": "locker", "name": intel.LOCKER_SOL[adr], "source": "OddsEngine list"}
    if adr == "1nc1nerator11111111111111111111111111111111":
        return {"kind": "burn", "name": "incinerator", "source": "standard"}
    try:
        if intel._boerse_sol(adr):
            return {"kind": "service", "name": "very busy wallet: typical for an exchange, bridge or relayer", "source": "activity"}
    except Exception:
        pass
    try:
        info = intel._sol("getAccountInfo", [adr, {"encoding": "base64"}]) or {}
        v = info.get("value") or {}
        if v and v.get("executable"):
            return {"kind": "program", "name": None, "source": "chain"}
        if v and v.get("owner") not in (intel.SYSTEM, None):
            return {"kind": "contract", "name": None, "source": "chain (program owned account, e.g. a pool or vault)"}
    except Exception:
        pass
    return {"kind": "wallet", "name": None, "source": None}


# ── Geldspur ─────────────────────────────────────────────────────────────

def _erstes_rein_evm(chain, w):
    """Erste eingehende Zahlung in der Nativwaehrung: (von, tx, zeit, betrag) oder None. Dazu Anzahl Transaktionen."""
    import intel
    d = intel._bs(chain, "/api", {"module": "account", "action": "txlist", "address": w, "sort": "asc", "page": 1, "offset": 25}) or {}
    liste = d.get("result") if isinstance(d.get("result"), list) else []
    erste = int(liste[0]["timeStamp"]) if liste and liste[0].get("timeStamp") else None
    for x in liste:
        if str(x.get("to") or "").lower() == w and int(x.get("value") or 0) > 0:
            return {"from": str(x.get("from") or "").lower(), "tx": x.get("hash"), "ts": int(x.get("timeStamp") or 0),
                    "amount": int(x["value"]) / 1e18}, erste
    # Ohne Nativ-Eingang: erste interne Zahlung (z. B. aus einem Vertrag oder einer Bridge)
    d = intel._bs(chain, "/api", {"module": "account", "action": "txlistinternal", "address": w, "sort": "asc", "page": 1, "offset": 10}) or {}
    for x in (d.get("result") if isinstance(d.get("result"), list) else []):
        if str(x.get("to") or "").lower() == w and int(x.get("value") or 0) > 0:
            return {"from": str(x.get("from") or "").lower(), "tx": x.get("hash") or x.get("transactionHash"),
                    "ts": int(x.get("timeStamp") or 0), "amount": int(x["value"]) / 1e18}, erste
    return None, erste


def _erstes_rein_sol(w, bis):
    """Aelteste Transaktion lesen und schauen, wer SOL geschickt hat. Blaettert hoechstens 5 Seiten zurueck."""
    import intel
    vor, letzte, n = None, [], 0
    for _ in range(5):
        if time.time() > bis:
            return None, None, n, False
        opt = {"limit": 1000}
        if vor:
            opt["before"] = vor
        r = intel._sol("getSignaturesForAddress", [w, opt], timeout=10) or []
        if not r:
            break
        letzte = r
        n += len(r)
        vor = r[-1].get("signature")
        if len(r) < 1000:
            break
    else:
        return None, None, n, False  # zu alt oder zu aktiv
    if not letzte:
        return None, None, 0, True
    aelteste = letzte[-1]
    tx = intel._sol("getTransaction", [aelteste["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    von = intel._geldgeber_aus(tx, w)
    betrag = None
    try:
        keys = intel._schluessel(tx)
        i = keys.index(w)
        betrag = (tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]) / 1e9
    except Exception:
        pass
    hop = {"from": von, "tx": aelteste["signature"], "ts": aelteste.get("blockTime"), "amount": betrag} if von else None
    return hop, aelteste.get("blockTime"), n, True


def geldspur(chain, adr, hops=None, bis=None):
    """Rueckwaerts: wer hat dieses Wallet zuerst gefuellt, und wer das davor ... bis Boerse, Bridge oder Ende.
    Seit 2.54: kam das Geld ueber Relay, geht die Spur auf der Herkunfts-Chain weiter."""
    bis = bis or time.time() + BUDGET
    hops = hops or HOPS
    kette, w, gesehen = [], adr if chain in ("solana", "tron", "ton") else str(adr).lower(), set()
    for _ in range(hops):
        if time.time() > bis or (chain, w) in gesehen:
            break
        gesehen.add((chain, w))
        try:
            import kartei
            gemerkt = kartei.geldgeber(chain, w)
        except Exception:
            kartei, gemerkt = None, None
        if gemerkt and gemerkt.get("chain") == chain:
            hop = {"from": gemerkt["from"], "tx": gemerkt["tx"], "ts": gemerkt["ts"], "amount": None}
        elif chain == "solana":
            hop, _, _, komplett = _erstes_rein_sol(w, bis)
            if not komplett:
                kette.append({"wallet": w, "chain": chain, "stop": "history too long to read back (very active wallet)"})
                break
        elif chain in ("robinhood", "base", "ethereum", "arbitrum"):
            # seit 2.78.0 mit Wiederholung, und "Explorer antwortet nicht" ist keine "keine Zahlung gefunden"
            try:
                import funder
                hop, st_ = funder.geldgeber(chain, w)
            except Exception:
                hop, st_ = _erstes_rein_evm(chain, w)[0], "ok"
            if not hop and st_ == "down":
                kette.append({"wallet": w, "chain": chain, "stop": "the explorer did not answer, the funder could not be read (run again later)"})
                break
        else:
            kette.append({"wallet": w, "chain": chain, "stop": f"funding trail not available on {chain}"})
            break
        if not hop or not hop.get("from"):
            kette.append({"wallet": w, "chain": chain, "stop": "no funding payment found"})
            break
        if kartei and not gemerkt:
            try:
                kartei.geldgeber_merken(chain, w, hop["from"], hop.get("tx"), hop.get("ts"))
            except Exception:
                pass
        nm = name(chain, hop["from"])
        eintrag = {"wallet": w, "chain": chain, "funded_by": hop["from"], "funder_kind": nm["kind"], "funder_name": nm.get("name"),
                   "amount": f"{hop['amount']:.4g} {_nativ(chain)}" if hop.get("amount") else None,
                   "when": time.strftime("%Y-%m-%d", time.gmtime(hop["ts"])) if hop.get("ts") else None,
                   "proof": _link(chain, "tx", hop["tx"])}
        if nm["kind"] in ("exchange", "bridge", "service", "contract", "program"):
            try:
                import fluss
                br = fluss.herkunft_bridge(chain, hop["tx"])
            except Exception:
                br = None
            if br:
                eintrag.update(funder_kind="bridge", funder_name=f"{br['bridge']} bridge",
                               bridged_from={"chain": br["from_chain"], "sender": br["from"], "sent": br.get("amount_in"),
                                             "same_wallet": br["from"] == str(w).lower(),
                                             "proof": _link(br["from_chain"], "tx", br["from_tx"]) if br.get("from_tx") else None})
                kette.append(eintrag)
                if kartei:
                    try:  # die echte Herkunft merken, nicht den Dienst der Bridge
                        kartei.geldgeber_merken(chain, w, br["from"], br.get("from_tx"), hop.get("ts"), funder_chain=br["from_chain"])
                    except Exception:
                        pass
                if br["from_chain"] in ("robinhood", "base", "ethereum", "arbitrum", "solana"):
                    chain, w = br["from_chain"], br["from"]
                    continue
                break
        kette.append(eintrag)
        if nm["kind"] in ("exchange", "bridge", "service", "contract", "program", "pool", "locker"):
            break
        w = hop["from"]
    return kette


def geschwister(chain, funder, ohne, n=8):
    """Andere Wallets, die derselbe Geldgeber gefuellt hat: aus der Kartei und (EVM mit Blockscout) aus der Chain."""
    import intel
    aus_kartei = []
    try:
        import kartei
        if name(chain, funder)["kind"] == "wallet":
            aus_kartei = [{"wallet": x["wallet"], "proof": _link(chain, "tx", x["funded_tx"]) if x.get("funded_tx") else None,
                           "source": "OddsEngine records"} for x in kartei.geschwister(chain, funder, ohne, n)]
    except Exception:
        pass
    neu = _geschwister_chain(chain, funder, ohne, n)
    bekannt = {x["wallet"] for x in neu}
    return (neu + [x for x in aus_kartei if x["wallet"] not in bekannt])[:n]


def _geschwister_chain(chain, funder, ohne, n=8):
    import intel
    if chain == "solana" or chain not in intel.BLOCKSCOUT or name(chain, funder)["kind"] != "wallet":
        return []
    d = intel._bs(chain, "/api", {"module": "account", "action": "txlist", "address": funder, "sort": "desc", "page": 1, "offset": 100}) or {}
    out, seen = [], {str(ohne).lower()}
    for x in (d.get("result") if isinstance(d.get("result"), list) else []):
        an = str(x.get("to") or "").lower()
        if str(x.get("from") or "").lower() == funder and int(x.get("value") or 0) > 0 and an not in seen and x.get("input") in ("0x", "", None):
            seen.add(an)
            out.append({"wallet": an, "got": f"{int(x['value']) / 1e18:.4g} {_nativ(chain)}", "proof": _link(chain, "tx", x.get("hash"))})
        if len(out) >= n:
            break
    return out


# ── Kette erkennen ───────────────────────────────────────────────────────

def _kette_wallet(adr, hinweis=None):
    import scan
    if hinweis and hinweis in scan.KETTEN:
        return hinweis
    if not str(adr).startswith("0x"):
        return "solana"
    import intel
    best, n = "robinhood", -1
    for ch in ("robinhood", "base", "ethereum", "arbitrum"):
        d = _c(("cnt", ch, adr.lower()), 600, lambda ch=ch: intel._bs(ch, f"/api/v2/addresses/{adr.lower()}/counters")) or {}
        try:
            k = int(d.get("transactions_count") or 0) + int(d.get("token_transfers_count") or 0)
        except Exception:
            k = 0
        if k > n:
            best, n = ch, k
    if n > 0:
        return best
    # Seit 2.63.1: Die Zaehler des Explorers hinken bei neuen Wallets oft hinterher (zeigten 0 fuer eine Wallet mit
    # Milliarden-Bewegungen). Dann direkt die Kette fragen: Nonce und Guthaben je Kette, die aktivste gewinnt.
    import scan
    best, wert = "robinhood", (-1, -1)
    for ch in ("ethereum", "base", "arbitrum", "robinhood"):
        try:
            nonce = int(scan._evm(ch, "eth_getTransactionCount", [adr.lower(), "latest"]), 16)
            bal = int(scan._evm(ch, "eth_getBalance", [adr.lower(), "latest"]), 16)
        except Exception:
            continue
        if (nonce, bal) > wert:
            best, wert = ch, (nonce, bal)
    return best


def groesste_zufluesse(chain, w, n=3):
    """Seit 2.56: die groessten Zahlungen, die dieses Wallet je bekommen hat (ueber die ganze Geschichte, auch interne
    Transfers aus Vertraegen wie einer Safe). Die erste Gas-Zahlung ist oft nicht die wichtigste Herkunft."""
    import fluss
    posten = []
    for f in fluss._fluesse_evm(chain, w, 60, alt=150):
        for x in f["rein"]:
            usd = fluss._usd(chain, x)
            if usd > 0:
                posten.append((usd, x, f))
    out = []
    for usd, x, f in sorted(posten, key=lambda p: -p[0])[:n]:
        von = x.get("from")
        nm = name(chain, von) if von else {}
        out.append({"amount": fluss._text(chain, x), "usd_at_today_price": round(usd), "from": von, "from_kind": nm.get("kind"),
                    "from_name": nm.get("name"), "from_name_source": nm.get("source"),
                    "when": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(f["ts"])) if f.get("ts") else None,
                    "proof": _link(chain, "tx", f["hash"])})
    return out


def wallet_profil(adr, chain=None):
    a = str(adr or "").strip()
    if not (a.startswith("0x") and len(a) == 42) and not _B58.match(a):
        return {"error": "That is not a wallet address."}
    chain = _kette_wallet(a, chain)
    bis = time.time() + BUDGET
    w = a if chain in ("solana", "tron", "ton") else a.lower()
    nm = name(chain, w)
    out = {"wallet": w, "chain": chain, "explorer": _link(chain, "address", w), "kind": nm["kind"],
           "name": nm.get("name"), "name_source": nm.get("source")}
    if nm.get("flags"):
        out["flags"] = nm["flags"]
    if chain == "solana":
        _, erste, n, komplett = _erstes_rein_sol(w, bis)
        out["transactions_seen"] = f"{n}+" if not komplett else n
        if erste:
            out["first_seen"] = time.strftime("%Y-%m-%d", time.gmtime(erste))
    elif chain in ("robinhood", "base", "ethereum", "arbitrum"):
        import intel
        d = intel._bs(chain, f"/api/v2/addresses/{w}/counters") or {}
        # Seit 2.63.1: "0" vom Explorer heisst bei neuen Wallets oft nur "noch nicht gezaehlt", dann weglassen
        if str(d.get("transactions_count") or "0") != "0":
            out["transactions"] = d.get("transactions_count")
            out["token_transfers"] = d.get("token_transfers_count")
        try:
            import scan
            out["eth_balance_now"] = round(int(scan._evm(chain, "eth_getBalance", [w, "latest"]), 16) / 1e18, 4)
            out["outgoing_tx_count"] = int(scan._evm(chain, "eth_getTransactionCount", [w, "latest"]), 16)
        except Exception:
            pass
        _, erste = _erstes_rein_evm(chain, w)
        if erste:
            out["first_seen"] = time.strftime("%Y-%m-%d", time.gmtime(erste))
    if chain in ("robinhood", "base", "ethereum", "arbitrum") and time.time() < bis:
        try:
            out["biggest_inflows"] = groesste_zufluesse(chain, w)
        except Exception as e:
            log.debug(f"spur zufluesse: {e}")
    if nm["kind"] in ("wallet",):
        if chain == "bsc":
            out["funding_trail"] = "not available on BNB Chain yet"
        else:
            out["funding_trail"] = geldspur(chain, w, bis=bis)
            erster = next((h for h in out["funding_trail"] if h.get("funded_by")), None)
            if erster and time.time() < bis:
                out["same_funder_wallets"] = geschwister(chain, erster["funded_by"], w)
        try:
            out.update(eigene_daten(chain, w))
            if (out.get("dev_of") or {}).get("count", 0) >= 3:
                import kartei
                kartei.markieren(chain, w, "serial_dev", f"launched {out['dev_of']['count']} coins", "")
        except Exception as e:
            log.debug(f"spur eigene daten: {e}")
        try:
            import weiter
            fw = weiter.paare_von(chain, w) if chain != "solana" else []
            if fw:
                out["forwards_in_our_scans"] = [
                    {"coin": _sym(chain, f["token"]), "token": f["token"],
                     "direction": "sent" if f["from"] == w else "received",
                     "counterparty": f["to"] if f["from"] == w else f["from"], "pct_of_supply": f["pct"],
                     "proof": _link(chain, "token", f["token"]) + "?a=" + w} for f in fw[:15]]
        except Exception as e:
            log.debug(f"spur fwd: {e}")
        try:
            import intel
            iw = intel.wallet(w)
            if iw.get("known"):
                out["smart_wallet"] = {"scores": iw.get("scores"), "early_in": (iw.get("early_in") or [])[:5]}
        except Exception:
            pass
    out["how_to_answer"] = ("Say who or what this wallet is ONLY from 'name' and 'kind' (with their source). If name is empty, call it an "
                            "unknown wallet and describe it by behaviour: age, activity, where its money came from, wallets with the same "
                            "funder. If flags are there (drainer, drain_dest, poison, serial_dev), say them first with their proof: they are "
                            "facts from OddsEngine's own records. If dev_of is there, lead with it: this wallet launched those coins (our own records). Never guess a person or a project. Put the proof link behind every claim. If the trail ends at an "
                            "exchange or a very busy service wallet (exchange, bridge, relayer), say the money came through it, which hides "
                            "who is behind it. Wallets with the same funder can belong to the same person, but say 'can', not 'do'. If forwards_in_our_scans is there, list those coins and counterparties too: they are wallet to wallet forwards OddsEngine saw in its own scans, with proof. If biggest_inflows is there, lead with the largest one: how much came in, from what (from_name with its source, e.g. a Safe multisig and its implementation contract), when, proof. That is usually the real story, the first funding is often only gas money. No tag in our sources proves nothing: never call a wallet clean or 'not labeled as a scam'.")
    return out


# ── Transaktion ──────────────────────────────────────────────────────────

def _evm_tx_finden(h, hinweis=None):
    import wache
    reihen = [hinweis] if hinweis in EVM_KETTEN else []
    reihen += [k for k in EVM_KETTEN if k not in reihen]
    for ch in reihen:
        try:
            rpc = wache._evm_rpc(ch)
            tx = rpc("eth_getTransactionByHash", [h])
            if tx:
                return ch, tx, rpc("eth_getTransactionReceipt", [h])
        except Exception:
            continue
    return None, None, None


def tx_erklaeren(h, chain=None):
    h = str(h or "").strip()
    m = re.search(r"(0x[0-9a-fA-F]{64})", h)
    if m:
        import wache
        ch, tx, rc = _evm_tx_finden(m.group(1).lower(), chain)
        if not tx:
            return {"error": "I could not find that transaction on Robinhood Chain, Base, Ethereum, Arbitrum or BNB Chain."}
        von, an = str(tx.get("from") or "").lower(), str(tx.get("to") or "").lower()
        wert = int(tx.get("value") or "0x0", 16) / 1e18
        transfers, approvals = [], []
        for l in (rc or {}).get("logs") or []:
            t = l.get("topics") or []
            if len(t) == 3 and t[0] == T_TRANSFER:
                sym, dez = wache._symbol(ch, l["address"].lower())
                menge = int(l.get("data") or "0x0", 16) / 10 ** dez if l.get("data") not in (None, "0x") else 0
                transfers.append({"token": sym, "token_address": l["address"].lower(), "from": "0x" + t[1][-40:], "to": "0x" + t[2][-40:],
                                  "amount": round(menge, 6)})
            elif len(t) == 3 and t[0] == T_APPROVAL:
                sym, _ = wache._symbol(ch, l["address"].lower())
                approvals.append({"token": sym, "owner": "0x" + t[1][-40:], "spender": "0x" + t[2][-40:]})
        rein = [x for x in transfers if x["to"] == von]
        raus = [x for x in transfers if x["from"] == von]
        art = "swap" if rein and (raus or wert) else "token transfer" if raus else "approval" if approvals else \
              "native transfer" if wert and not (tx.get("input") or "0x") != "0x" else "contract call"
        status = int((rc or {}).get("status") or "0x1", 16)
        ts = None
        try:
            b = wache._evm_rpc(ch)("eth_getBlockByNumber", [tx["blockNumber"], False])
            ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(b["timestamp"], 16)))
        except Exception:
            pass
        return {"chain": ch, "hash": m.group(1), "proof": _link(ch, "tx", m.group(1)), "status": "success" if status == 1 else "failed",
                "time": ts, "from": von, "from_name": name(ch, von), "to": an, "to_name": name(ch, an) if an else None,
                f"{_nativ(ch)}_sent": round(wert, 6), "kind": art, "transfers": transfers[:20], "approvals": approvals[:5],
                "sender_received": rein[:5], "sender_sent": raus[:5],
                "how_to_answer": "Explain in plain words who did what: sent, bought, sold, approved. Name tokens and amounts. "
                                 "Use from_name/to_name only when they have a name. End with the proof link."}
    m = re.search(r"([1-9A-HJ-NP-Za-km-z]{80,90})", h)
    if m:
        import intel
        import spaeher
        tx = intel._sol("getTransaction", [m.group(1), {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        if not tx:
            return {"error": "I could not find that Solana transaction."}
        keys, signer = spaeher._schluessel(tx)
        wer = keys[0] if keys else None
        e = spaeher.sol_einordnen(tx, wer) or {}
        def namen(liste):
            out = []
            for mint, menge in liste:
                try:
                    import scan
                    s = (scan.markt(mint, "solana", scan._paare(mint)) or {}).get("symbol")
                except Exception:
                    s = None
                out.append({"token": s or _kurz(mint), "mint": mint, "amount": round(menge, 6)})
            return out
        return {"chain": "solana", "hash": m.group(1), "proof": _link("solana", "tx", m.group(1)),
                "status": "failed" if (tx.get("meta") or {}).get("err") else "success",
                "time": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(tx["blockTime"])) if tx.get("blockTime") else None,
                "signer": wer, "signer_name": name("solana", wer) if wer else None, "kind": e.get("art"),
                "received": namen(e.get("rein") or []), "sent": namen(e.get("raus") or []),
                "sol_change": round(e.get("sol") or 0, 6), "counterparty": e.get("gegen"),
                "how_to_answer": "Explain in plain words who did what (buy, sell, swap, sent, received), with tokens and amounts. "
                                 "End with the proof link."}
    return {"error": "Send the transaction hash (0x… on EVM chains, or a Solana signature)."}


# ── Halter ───────────────────────────────────────────────────────────────

def halter_wer(token, rang=1):
    import scan
    try:
        r = scan.analyse(token, deep=False)
    except Exception as e:
        return {"error": f"Could not read that coin ({str(e)[:60]})."}
    if not r.get("ok"):
        return {"error": r.get("error") or "Could not read that coin."}
    top = [x for x in ((r.get("supply") or {}).get("top") or []) if not x.get("pool") and not x.get("locked")]
    import intel
    top = [x for x in top if str(x.get("address") or "").lower() not in intel.TOTE]
    try:
        rang = max(1, int(rang or 1))
    except Exception:
        rang = 1
    if len(top) < rang:
        return {"error": f"I only see {len(top)} real holders in the top list."}
    h = top[rang - 1]
    prof = wallet_profil(h["address"], r["chain"])
    return {"coin": f"${r.get('symbol')}", "chain": r["chain"], "rank": rang, "holds_pct": h.get("pct"),
            "note": "Pools, lockers and the burn address are left out of the ranking.", "wallet": prof}


# ── Seit 2.53.1: unsere eigenen Daten, Verbindung zweier Wallets, Geld vorwaerts ──

def eigene_daten(chain, w):
    """Was OddsEngine selbst ueber das Wallet weiss: Dev welcher Coins, frueh in welchen Coins (aus unseren Scans)."""
    import intel
    k = intel._k(chain, w)
    out = {}
    devs = intel._q("SELECT d.token, t.symbol, t.multiple, d.ts FROM iw_devs d LEFT JOIN iw_tokens t ON t.chain=d.chain AND t.token=d.token "
                    "WHERE d.chain=%s AND d.dev=%s ORDER BY d.ts DESC LIMIT 12", (chain, k)) or []
    rh = []
    if chain == "robinhood":
        try:
            import onchain
            rh = [l["token"] for l in onchain.launches() if l.get("creator") == k]
        except Exception:
            rh = []
    if devs or rh:
        liste = [{"coin": f"${r[1]}" if r[1] else _kurz(r[0]), "token": r[0],
                  "peak_x_since_first_check": round(r[2], 1) if r[2] else None,
                  "scanned": time.strftime("%Y-%m-%d", time.gmtime(r[3])) if r[3] else None,
                  "proof": _link(chain, "token", r[0])} for r in devs]
        bekannt = {r[0] for r in devs}
        liste += [{"coin": _kurz(t), "token": t, "proof": _link(chain, "token", t)} for t in rh if t not in bekannt][:10]
        out["dev_of"] = {"count": len(bekannt | set(rh)), "coins": liste[:12],
                         "source": "OddsEngine scans" + (" and Torch launch records" if rh else "")}
    frueh = intel._q("SELECT e.token, t.symbol, t.multiple, e.rank, e.flag FROM iw_early e LEFT JOIN iw_tokens t "
                     "ON t.chain=e.chain AND t.token=e.token WHERE e.chain=%s AND e.wallet=%s ORDER BY COALESCE(t.multiple,0) DESC LIMIT 12",
                     (chain, k)) or []
    if frueh:
        out["early_buyer_in"] = [{"coin": f"${r[1]}" if r[1] else _kurz(r[0]), "buyer_rank": r[3],
                                  "peak_x_since_first_check": round(r[2], 1) if r[2] else None,
                                  "flag": {"bundle": "bought in a bundle", "dev_group": "funded by that coin's dev"}.get(r[4])}
                                 for r in frueh]
    return out


def _mit_wem_evm(chain, w, n=200):
    """Gegenparteien eines EVM-Wallets: {adresse: [tx, ...]} aus Nativ- und Token-Transfers."""
    import intel
    out = {}
    for action in ("txlist", "tokentx"):
        d = intel._bs(chain, "/api", {"module": "account", "action": action, "address": w, "sort": "desc", "page": 1, "offset": n}) or {}
        for x in (d.get("result") if isinstance(d.get("result"), list) else []):
            von, an = str(x.get("from") or "").lower(), str(x.get("to") or "").lower()
            g = an if von == w else von if an == w else None
            if g:
                out.setdefault(g, []).append(x.get("hash"))
    return out


def verbindung(a, b, chain=None):
    """Haengen zwei Wallets zusammen? Direkte Transfers, gemeinsame Transaktionen, gleicher Geldgeber, gleiche fruehe Coins."""
    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b or a.lower() == b.lower():
        return {"error": "Send two different wallet addresses."}
    chain = _kette_wallet(a, chain)
    if chain != "solana":
        a, b = a.lower(), b.lower()
    bis = time.time() + BUDGET
    belege = []
    if chain == "solana":
        import intel
        sa = {x["signature"] for x in intel._sol("getSignaturesForAddress", [a, {"limit": 500}]) or [] if not x.get("err")}
        sb = {x["signature"] for x in intel._sol("getSignaturesForAddress", [b, {"limit": 500}]) or [] if not x.get("err")}
        gemeinsam = list(sa & sb)[:6]
        if gemeinsam:
            belege.append({"signal": "shared transactions", "detail": f"{len(sa & sb)} transactions touch both wallets "
                           "(a direct transfer, or both in the same bundle)", "proof": [_link(chain, "tx", s) for s in gemeinsam]})
    elif chain in ("robinhood", "base", "ethereum", "arbitrum"):
        ga = _mit_wem_evm(chain, a)
        if b in ga:
            belege.append({"signal": "direct transfers", "detail": f"{len(ga[b])} transfers between the two wallets",
                           "proof": [_link(chain, "tx", h) for h in ga[b][:6]]})
        gb = _mit_wem_evm(chain, b) if time.time() < bis else {}
        gemein = [g for g in set(ga) & set(gb) if g not in (a, b)]
        wallets = []
        for g in gemein[:15]:
            if time.time() > bis:
                break
            if name(chain, g)["kind"] == "wallet":
                wallets.append(g)
        if wallets:
            belege.append({"signal": "same counterparties", "detail": f"both sent to or received from the same {len(wallets)} private wallet(s)",
                           "wallets": [{"wallet": g, "proof": _link(chain, "address", g)} for g in wallets[:5]]})
    if time.time() < bis:
        ta = geldspur(chain, a, hops=3, bis=bis)
        tb = geldspur(chain, b, hops=3, bis=bis) if time.time() < bis else []
        fa = {h["funded_by"]: h for h in ta if h.get("funded_by")}
        fb = {h["funded_by"]: h for h in tb if h.get("funded_by")}
        for g in set(fa) & set(fb):
            if fa[g].get("funder_kind") == "wallet":
                belege.append({"signal": "same funder", "detail": "both were funded by the same private wallet", "funder": g,
                               "proof": [fa[g]["proof"], fb[g]["proof"]]})
            else:
                belege.append({"signal": "same service", "detail": f"both were funded through the same {fa[g].get('funder_kind')} "
                               "(weak signal, many people use it)", "funder": g})
        if a in fb or b in fa:
            h = fb.get(a) or fa.get(b)
            belege.append({"signal": "one funded the other", "detail": "one wallet sent the first money to the other", "proof": [h["proof"]]})
    if chain != "solana":
        try:
            import weiter
            pa = weiter.paare_von(chain, a, 200)
            direkt = [f for f in pa if b in (f["from"], f["to"])]
            if direkt and not any(x["signal"] == "direct transfers" for x in belege):
                belege.append({"signal": "direct transfers", "detail": f"{len(direkt)} wallet to wallet forward(s) between the two, "
                               "seen in our own scans", "coins": [{"coin": _sym(chain, f["token"]), "from": f["from"], "to": f["to"],
                               "pct_of_supply": f["pct"], "proof": _link(chain, "token", f["token"]) + "?a=" + a} for f in direkt[:6]]})
            pb = weiter.paare_von(chain, b, 200)
            gemein_t = {f["token"] for f in pa} & {f["token"] for f in pb}
            if gemein_t:
                belege.append({"signal": "forwarding in the same coins", "detail": f"both forwarded tokens in {len(gemein_t)} same coin(s)",
                               "coins": [_link(chain, "token", t) for t in list(gemein_t)[:5]]})
        except Exception as e:
            log.debug(f"spur verbindung fwd: {e}")
    import intel
    ea = {r[0] for r in intel._q("SELECT token FROM iw_early WHERE chain=%s AND wallet=%s", (chain, intel._k(chain, a))) or []}
    eb = {r[0] for r in intel._q("SELECT token FROM iw_early WHERE chain=%s AND wallet=%s", (chain, intel._k(chain, b))) or []}
    if ea & eb:
        belege.append({"signal": "early in the same coins", "detail": f"both were among the first buyers of {len(ea & eb)} same coin(s)",
                       "coins": [_link(chain, "token", t) for t in list(ea & eb)[:5]]})
    stark = [x for x in belege if x["signal"] in ("direct transfers", "same funder", "one funded the other", "shared transactions")]
    return {"chain": chain, "a": a, "b": b, "verdict": "linked" if stark else "possible link" if belege else "no link found",
            "evidence": belege,
            "how_to_answer": "Say 'linked', 'possibly linked' or 'no link found' and list each signal with its proof. Linked wallets CAN "
                             "belong to the same person or team, but say 'can'. 'Same service' alone means nothing. No accusations."}


def geld_raus(adr, chain=None, n=150):
    """Wohin hat dieses Wallet sein Geld (ETH/SOL) geschickt? Groesste Empfaenger der letzten Transaktionen."""
    a = str(adr or "").strip()
    chain = _kette_wallet(a, chain)
    w = a if chain in ("solana", "tron", "ton") else a.lower()
    summe, beleg = {}, {}
    if chain == "solana":
        import intel
        import spaeher
        sigs = [x["signature"] for x in intel._sol("getSignaturesForAddress", [w, {"limit": min(n, 100)}]) or [] if not x.get("err")]
        txs = intel._sol_batch([intel._tx(s) for s in sigs], 20) if sigs else []
        for s, tx in zip(sigs, txs):
            e = spaeher.sol_einordnen(tx, w) if isinstance(tx, dict) else None
            if e and e.get("art") == "sol" and (e.get("sol") or 0) < 0 and e.get("gegen"):
                g = e["gegen"]
                summe[g] = summe.get(g, 0) + abs(e["sol"])
                beleg.setdefault(g, s)
    elif chain in ("robinhood", "base", "ethereum", "arbitrum"):
        import intel
        d = intel._bs(chain, "/api", {"module": "account", "action": "txlist", "address": w, "sort": "desc", "page": 1, "offset": n}) or {}
        for x in (d.get("result") if isinstance(d.get("result"), list) else []):
            if str(x.get("from") or "").lower() == w and int(x.get("value") or 0) > 0 and str(x.get("isError") or "0") == "0":
                g = str(x.get("to") or "").lower()
                summe[g] = summe.get(g, 0) + int(x["value"]) / 1e18
                beleg.setdefault(g, x.get("hash"))
    else:
        return {"error": "Outgoing money trail works on Solana, Robinhood Chain, Base, Ethereum and Arbitrum."}
    top = sorted(summe.items(), key=lambda x: -x[1])[:6]
    ziele = []
    for g, v in top:
        nm = name(chain, g)
        ziele.append({"to": g, "total": f"{v:.4g} {_nativ(chain)}", "kind": nm["kind"], "name": nm.get("name"),
                      "latest_proof": _link(chain, "tx", beleg[g]), "address": _link(chain, "address", g)})
    return {"chain": chain, "wallet": w, "looked_at": "the most recent transactions", "sent_to": ziele,
            "how_to_answer": "List where the money went, biggest first, with kind/name and proof. Money sent to an exchange or "
                             "service usually means cashing out or bridging; money sent to a private wallet can be the same person. "
                             "Offer trace_wallet on the biggest private wallet to follow it further."}
