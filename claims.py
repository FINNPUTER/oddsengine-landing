"""
claims.py: Creator-Fee-Claims auf Torch melden (seit 2.60.3).

Wenn der Ersteller eines Torch-Coins seine Creator Fees abholt ("Claim creator fees", Router 0x3402...0fa6,
Selektor 0x1e83409a mit der Coin-Adresse), bekommen alle eine Nachricht, die den Coin beobachten ("Watch it"),
ihn halten (Bag Watch) oder ihn ausdruecklich dazugenommen haben. Coin, Empfaenger und Betrag kommen aus dem Claim-Event
des Routers, die Waehrung (TORCH oder ETH) aus der Transaktion.

runde() aus motor.tick, alle CLAIMS_POLL_SEC (Standard 30) Sekunden. Liest nur neue Bloecke seit dem letzten Lauf.
"""
import json
import os
import time

import storage
from logger import log

ROUTER = "0x340278a0ef7c7e83418771504871619453bd0fa6"
SELEKTOR = "0x1e83409a"
CLAIM_TOPIC = "0xf7a40077ff7a04c7e61f6f26fb13774259ddf1b6bce9ecf26a8276cdd3992683"  # live an echten Claims geprueft
TORCH = "0x000050e18ffa3f8f7ad170eb9cef160eb3ed1111"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FENSTER = 400_000
SPAETER = 900


def _rpc(m, p):
    import holder
    return holder._evm_rpc(m, p)


def _kurz(a):
    a = str(a or "")
    return a[:6] + "…" + a[-4:]


def neue_claims(von, bis):
    """-> [{token, receiver, amount, currency, tx, block}] aus dem Claim-Event des Routers (Topic CLAIM_TOPIC:
    topic1 Coin, topic2 Empfaenger, data Betrag in der Paarwaehrung). TORCH, wenn die Transaktion TORCH bewegt, sonst ETH."""
    try:
        logs = _rpc("eth_getLogs", [{"address": ROUTER, "fromBlock": hex(von), "toBlock": hex(bis), "topics": [CLAIM_TOPIC]}]) or []
    except Exception as e:
        log.debug(f"claims logs: {e}")
        return None
    out = []
    for l in logs:
        t = l.get("topics") or []
        if len(t) < 3:
            continue
        roh = int(l.get("data") or "0x0", 16)
        waehrung = "ETH"
        try:
            rc = _rpc("eth_getTransactionReceipt", [l["transactionHash"]]) or {}
            if any(str(x.get("address") or "").lower() == TORCH and (x.get("topics") or [None])[0] == TRANSFER
                   for x in rc.get("logs") or []):
                waehrung = "TORCH"
        except Exception:
            pass
        out.append({"token": "0x" + t[1][-40:].lower(), "receiver": "0x" + t[2][-40:].lower(), "amount": roh / 1e18,
                    "currency": waehrung, "tx": l.get("transactionHash"), "block": int(l.get("blockNumber") or "0x0", 16)})
    return out


def _beobachter():
    """{token: set(adr)} aus Watch it, Bag Watch und ausdruecklich dazugenommenen Coins (nur Robinhood)."""
    import motor
    out = {}
    for adr in motor._nutzer():
        try:
            if not motor._darf(adr):
                continue
            z = motor.zustand(adr)
            for w in z.get("tokwatch") or []:
                if (w.get("chain") or "robinhood") == "robinhood" and w.get("token"):
                    out.setdefault(str(w["token"]).lower(), set()).add(adr)
            if z["prefs"].get("guard") is True:
                import beutel
                for ch, t, s in beutel._coins(adr):
                    if ch == "robinhood":
                        out.setdefault(str(t).lower(), set()).add(adr)
        except Exception as e:
            log.debug(f"claims beobachter {adr[:8]}: {e}")
    return out


def text(c, sym, preis):
    usd = f" (about ${c['amount'] * preis:,.0f})" if preis and c["amount"] else ""
    if c["currency"] == "TORCH":
        betrag = f"{c['amount']:,.0f} TORCH{usd}"
    else:
        betrag = f"{c['amount']:.4f} ETH{usd}" if c["amount"] else "their creator fees"
    an = f" to {_kurz(c['receiver'])}" if c.get("receiver") else ""
    return (f"💰 ${sym}: the creator just claimed {betrag} in creator fees{an}.\n\n"
            "Claiming fees is normal for a dev, it is not a sell of the coin.\n"
            f"https://robinhoodchain.blockscout.com/tx/{c['tx']}")


def runde(senden=None):
    if os.getenv("CLAIMS_ALERTS", "1") == "0":
        return 0
    import motor
    senden = senden or motor.senden
    try:
        top = int(_rpc("eth_blockNumber", []), 16)
    except Exception:
        return 0
    try:
        von = int(storage.kv_get("claims_block") or 0)
    except Exception:
        von = 0
    if not von or top - von > 5 * FENSTER:
        von = top - 20_000  # Neustart: nur die letzten Minuten, keine alten Claims nachmelden
    if von >= top:
        return 0
    claims = neue_claims(von + 1, min(top, von + FENSTER))
    if claims is None:
        return 0
    storage.kv_set("claims_block", str(min(top, von + FENSTER)))
    if not claims:
        return 0
    wer = _beobachter()
    gesendet = 0
    preise = {}
    for c in claims:
        nutzer = wer.get(c["token"]) or set()
        if not nutzer:
            continue
        if c["currency"] not in preise:
            try:
                if c["currency"] == "TORCH":
                    import scan
                    preise["TORCH"] = scan.zahlen("robinhood", TORCH).get("price") or 0
                else:
                    import handel
                    preise["ETH"] = handel._eth_usd() or 0
            except Exception:
                preise[c["currency"]] = 0
        preis = preise[c["currency"]]
        try:
            import onchain
            sym = (onchain._meta([c["token"]]).get(c["token"]) or {}).get("symbol") or _kurz(c["token"])
        except Exception:
            sym = _kurz(c["token"])
        for adr in nutzer:
            k = f"claim_sent_{adr}_{c['tx']}"
            if storage.kv_get(k):
                continue
            storage.kv_set(k, "1")
            try:
                senden(adr, text(c, sym, preis))
                gesendet += 1
            except Exception as e:
                log.debug(f"claims senden: {e}")
    log.info(f"claims: {len(claims)} claims, {gesendet} alerts")
    return gesendet
