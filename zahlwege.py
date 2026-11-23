"""Weitere x402-Zahlwege (seit 2.80.0).

1. USDC auf Solana (scheme "exact", network solana). Einloesen ueber einen oeffentlichen x402-Facilitator
   (Standard PayAI, https://facilitator.payai.network): /verify, dann /settle. Der Facilitator bezahlt die
   Solana-Gebuehr (extra.feePayer aus /supported). Wir halten keinen Solana-Schluessel.
   Variablen: X402_SOL_PAYTO (unsere Solana-Adresse fuer die USDC, braucht schon ein USDC-Konto, also einmal
   ein paar Cent USDC hinschicken), X402_SOL_FACILITATOR (optional), Schalter "x402sol".

2. USDG auf Robinhood Chain (scheme "exact", EIP-3009 transferWithAuthorization), eingeloest mit der
   Gas-Wallet wie USDC auf Base. Angeboten wird es nur, wenn der USDG-Vertrag EIP-3009 wirklich kann
   (authorizationState antwortet) und seine EIP-712-Domain lesbar ist. Schalter "x402usdg".

Beide schreiben das Guthaben wie die anderen Wege auf das Konto des Zahlers (ai_accounts).
"""
import json
import os
import time
import hashlib
import urllib.error
import urllib.request
from decimal import Decimal

import ai
import holder
import storage
from logger import log

SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_NET_V2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
RH_ID = 4663


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


def _an(name):
    try:
        import features
        return features.an(name)
    except Exception:
        return False


def _http(url, daten=None, timeout=25):
    kopf = {"User-Agent": "oddsengine-x402", "Accept": "application/json"}
    if daten is not None:
        kopf["Content-Type"] = "application/json"
        daten = json.dumps(daten).encode()
    r = urllib.request.Request(url, data=daten, headers=kopf)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            return json.loads(f.read().decode())
    except urllib.error.HTTPError as e:  # Facilitatoren antworten bei Ablehnung oft mit 400 und JSON
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise


def _atome(usd, dez=6):
    return int((Decimal(str(usd)) * Decimal(10 ** dez)).to_integral_value(rounding="ROUND_UP"))


def _tab():
    ai._pg().execute("""CREATE TABLE IF NOT EXISTS ai_x402_more (
        tx TEXT PRIMARY KEY, way TEXT NOT NULL, payer TEXT NOT NULL, amount NUMERIC(20,6),
        credited NUMERIC(20,8), ts TIMESTAMPTZ DEFAULT now())""")


def _gutschreiben(weg, tx, payer, usd, gas_usd):
    _tab()
    gut = max(0.0, usd - gas_usd)
    pg = ai._pg()
    ai.konto(payer)
    r = pg.execute("""INSERT INTO ai_x402_more (tx, way, payer, amount, credited) VALUES (%s,%s,%s,%s,%s)
                      ON CONFLICT DO NOTHING RETURNING tx""",
                   (tx, weg, holder.norm(payer), Decimal(str(usd)), Decimal(str(round(gut, 8))))).fetchone()
    if r:
        pg.execute("UPDATE ai_accounts SET credit = credit + %s, deposited = deposited + %s WHERE address=%s",
                   (Decimal(str(round(gut, 8))), Decimal(str(round(gut, 8))), holder.norm(payer)))
    return gut


# ── USDC auf Solana ueber Facilitator ────────────────────────────────────

_SOL = {"ts": 0, "fp": None}


def sol_fac():
    return (os.getenv("X402_SOL_FACILITATOR", "").strip() or "https://facilitator.payai.network").rstrip("/")


def sol_payto():
    return os.getenv("X402_SOL_PAYTO", "").strip()


def sol_fee_payer():
    """feePayer des Facilitators fuer Solana mainnet aus /supported, 1 h gemerkt."""
    if _SOL["fp"] and time.time() - _SOL["ts"] < 3600:
        return _SOL["fp"]
    try:
        d = _http(sol_fac() + "/supported", timeout=10)
        for k in d.get("kinds") or []:
            if str(k.get("scheme")) == "exact" and str(k.get("network")) in (SOL_NET_V2, "solana"):
                fp = (k.get("extra") or {}).get("feePayer")
                if fp:
                    _SOL.update(ts=time.time(), fp=fp)
                    return fp
    except Exception as e:
        log.warning(f"x402 sol supported: {e}")
    return None


def sol_bereit():
    return bool(_an("x402sol") and sol_payto() and ai.bereit() and sol_fee_payer())


def sol_v2(usd, m):
    return {"scheme": "exact", "network": SOL_NET_V2, "amount": str(_atome(usd)), "asset": SOL_USDC,
            "payTo": sol_payto(), "maxTimeoutSeconds": 300, "extra": {"feePayer": sol_fee_payer()}}


def sol_v1(usd, m, url):
    return {"scheme": "exact", "network": "solana", "maxAmountRequired": str(_atome(usd)), "resource": url,
            "description": "OddsEngine, {}. ${:.4f} in USDC on Solana.".format(m["name"], usd),
            "mimeType": "application/json", "payTo": sol_payto(), "maxTimeoutSeconds": 300, "asset": SOL_USDC,
            "extra": {"feePayer": sol_fee_payer()}}


def sol_bezahlen(roh, bedarf, url=""):
    """roh: das dekodierte X-PAYMENT / PAYMENT-SIGNATURE (v1 oder v2). -> (payer, info)"""
    if not sol_bereit():
        raise ai.Fehler(400, "USDC on Solana is not live right now", "invalid_payment")
    usd, m = bedarf
    v = int(roh.get("x402Version") or 1)
    anf = sol_v2(usd, m) if v >= 2 else sol_v1(usd, m, url)
    merk = "x402sol_" + hashlib.sha256(json.dumps(roh.get("payload") or {}, sort_keys=True).encode()).hexdigest()[:40]
    if storage.kv_get(merk):
        raise ai.Fehler(402, "this payment was already used, sign a new one", "invalid_payment")
    koerper = {"x402Version": v, "paymentPayload": roh, "paymentRequirements": anf}
    try:
        pr = _http(sol_fac() + "/verify", koerper)
    except Exception as e:
        raise ai.Fehler(502, "the Solana facilitator did not answer: " + str(e)[:100], "payment_failed")
    if not pr.get("isValid"):
        raise ai.Fehler(402, "payment not valid: " + str(pr.get("invalidReason") or "unknown")[:120], "invalid_payment")
    try:
        st = _http(sol_fac() + "/settle", koerper, timeout=60)
    except Exception as e:
        raise ai.Fehler(502, "settlement on Solana failed: " + str(e)[:100], "payment_failed")
    if not st.get("success"):
        raise ai.Fehler(402, "settlement on Solana failed: " + str(st.get("errorReason") or "unknown")[:120], "payment_failed")
    storage.kv_set(merk, "1")
    payer = st.get("payer") or pr.get("payer")
    tx = st.get("transaction")
    bezahlt = int(anf.get("amount") or anf.get("maxAmountRequired")) / 1e6
    gut = _gutschreiben("usdc-solana", tx, payer, bezahlt, _f("X402_SOL_FEE_USD", 0.0))
    log.info(f"x402 sol: {payer} paid ${bezahlt:.4f} tx {tx}")
    return holder.norm(payer), {"success": True, "transaction": tx, "network": "solana", "payer": payer,
                                "credited_usd": round(gut, 6)}


# ── USDG auf Robinhood Chain (EIP-3009) ──────────────────────────────────

_USDG = {"ts": 0, "domain": None}


def _usdg_adr():
    try:
        import onchain
        return onchain.USDG.lower()
    except Exception:
        return "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


def usdg_domain():
    """EIP-712-Domain von USDG, nur wenn der Vertrag EIP-3009 kann. 6 h gemerkt. None = nicht anbieten."""
    if _USDG["domain"] is not None and time.time() - _USDG["ts"] < 6 * 3600:
        return _USDG["domain"] or None
    dom = {}
    try:
        t = _usdg_adr()
        # EIP-3009 vorhanden? authorizationState(address(0), bytes32(0)) muss antworten
        r = holder._evm_rpc("eth_call", [{"to": t, "data": "0xe94a0102" + "0" * 128}, "latest"])
        if not r or r == "0x":
            raise ValueError("no authorizationState")
        from eth_abi import decode
        name = version = None
        try:  # EIP-5267
            r = holder._evm_rpc("eth_call", [{"to": t, "data": "0x84b0196e"}, "latest"])
            x = decode(["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"], bytes.fromhex(r[2:]))
            name, version = x[1], x[2]
            if int(x[3]) != RH_ID:
                raise ValueError("chain id mismatch")
        except Exception:
            r = holder._evm_rpc("eth_call", [{"to": t, "data": "0x06fdde03"}, "latest"])
            name = decode(["string"], bytes.fromhex(r[2:]))[0]
            r = holder._evm_rpc("eth_call", [{"to": t, "data": "0x54fd4d50"}, "latest"])
            version = decode(["string"], bytes.fromhex(r[2:]))[0]
        if name and version:
            dom = {"name": name, "version": version, "chainId": RH_ID, "verifyingContract": t}
    except Exception as e:
        log.info(f"x402 usdg not offered: {str(e)[:100]}")
        dom = {}
    _USDG.update(ts=time.time(), domain=dom)
    return dom or None


def usdg_bereit():
    try:
        import usdc
        return bool(_an("x402usdg") and usdc._konto() and ai.bereit() and usdg_domain())
    except Exception:
        return False


def usdg_v2(usd, m):
    import usdc
    d = usdg_domain()
    return {"scheme": "exact", "network": f"eip155:{RH_ID}", "amount": str(_atome(usd)), "asset": d["verifyingContract"],
            "payTo": holder.norm(usdc.kasse()), "maxTimeoutSeconds": 300,
            "extra": {"name": d["name"], "version": d["version"], "assetTransferMethod": "eip3009"}}


def usdg_v1(usd, m, url):
    import usdc
    d = usdg_domain()
    return {"scheme": "exact", "network": "robinhood", "maxAmountRequired": str(_atome(usd)), "resource": url,
            "description": "OddsEngine, {}. ${:.4f} in USDG on Robinhood Chain.".format(m["name"], usd),
            "mimeType": "application/json", "payTo": holder.norm(usdc.kasse()), "maxTimeoutSeconds": 300,
            "asset": d["verifyingContract"], "extra": {"name": d["name"], "version": d["version"]}}


def _usdg_unterzeichner(a, sig, d):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    msg = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                      {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                     "TransferWithAuthorization": [
                         {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                         {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                         {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
           "primaryType": "TransferWithAuthorization",
           "domain": {"name": d["name"], "version": d["version"], "chainId": RH_ID,
                      "verifyingContract": __import__("eth_utils").to_checksum_address(d["verifyingContract"])},
           "message": {"from": a["from"], "to": a["to"], "value": int(a["value"]), "validAfter": int(a["validAfter"]),
                       "validBefore": int(a["validBefore"]), "nonce": bytes.fromhex(a["nonce"][2:])}}
    return Account.recover_message(encode_typed_data(full_message=msg), signature=sig)


def usdg_bezahlen(p, bedarf):
    import usdc
    if not usdg_bereit():
        raise ai.Fehler(400, "USDG on Robinhood Chain is not live right now", "invalid_payment")
    d = usdg_domain()
    try:
        pl = p.get("payload") or {}
        a = pl["authorization"]
        a = {"from": holder.norm(a["from"]), "to": holder.norm(a["to"]), "value": int(str(a["value"])),
             "validAfter": int(str(a["validAfter"])), "validBefore": int(str(a["validBefore"])), "nonce": str(a["nonce"]).lower()}
        if not a["nonce"].startswith("0x") or len(a["nonce"]) != 66:
            raise ValueError("nonce")
        sig = str(pl["signature"])
        sig = sig if sig.startswith("0x") else "0x" + sig
    except Exception:
        raise ai.Fehler(400, "X-PAYMENT needs payload.signature and payload.authorization", "invalid_payment")
    if a["to"] != holder.norm(usdc.kasse()):
        raise ai.Fehler(402, "payTo does not match", "invalid_payment")
    jetzt = time.time()
    if a["validBefore"] < jetzt + 30 or a["validAfter"] > jetzt + 5:
        raise ai.Fehler(400, "authorization is expired or not valid yet", "invalid_payment")
    try:
        wer = _usdg_unterzeichner(a, sig, d)
    except Exception:
        wer = None
    if not wer or holder.norm(wer) != a["from"]:
        raise ai.Fehler(402, "signature does not match authorization.from", "invalid_payment")
    merk = "x402g_" + hashlib.sha256((a["from"] + a["nonce"]).encode()).hexdigest()[:40]
    if storage.kv_get(merk):
        raise ai.Fehler(402, "this payment was already used, sign a new one", "invalid_payment")
    storage.kv_set(merk, "1")
    usd_noetig, mz = bedarf
    kt = ai.konto(a["from"]) or {}
    if kt.get("credit", 0.0) >= mz["roh_usd"] * 1.05:
        return a["from"], {"success": True, "transaction": None, "network": "robinhood", "payer": a["from"],
                           "paid_from": "credit", "credit_usd": round(kt.get("credit", 0.0), 6)}
    usd = a["value"] / 1e6
    if usd < usd_noetig * 0.97:
        raise ai.Fehler(402, "payment too small, this request needs about ${:.4f} in USDG".format(usd_noetig), "payment_too_small")
    s = sig[2:]
    r_, s_, v = s[:64], s[64:128], int(s[128:130], 16)
    if v < 27:
        v += 27
    data = ("0x" + usdc.TWA_SEL + usdc._adr32(a["from"]) + usdc._adr32(a["to"]) + usdc._hex32(a["value"])
            + usdc._hex32(a["validAfter"]) + usdc._hex32(a["validBefore"]) + a["nonce"][2:] + usdc._hex32(v) + r_ + s_)
    tx, _q = usdc._senden("robinhood", d["verifyingContract"], data)
    gut = _gutschreiben("usdg-robinhood", tx, a["from"], usd, _f("X402_GAS_USD", 0.01))
    log.info(f"x402 usdg: {a['from']} paid ${usd:.4f} tx {tx}")
    return a["from"], {"success": True, "transaction": tx, "network": "robinhood", "payer": a["from"],
                       "credited_usd": round(gut, 6)}


# ── Discovery (Bazaar-Angaben) ───────────────────────────────────────────

SCAN_IN = {"type": "http", "method": "GET", "discoverable": True,
           "queryParams": {"q": {"type": "string", "required": True,
                                 "description": "Token contract address or $TICKER (Solana, Robinhood Chain, Base, Ethereum, Arbitrum)"},
                           "wait": {"type": "integer", "required": False,
                                    "description": "Seconds to wait for the deep scan, 0 to 90, default 75"}}}
SCAN_OUT = {"type": "object", "properties": {
    "findings": {"type": "object", "description": "on-chain facts only, no rating and no safety verdict"},
    "supply": {"type": "object", "description": "top 10, bundled, fresh wallets, clusters, snipers, locked"},
    "launch_block_buys": {"type": "object", "description": "what was bought in the launch block and how fast it was sold"},
    "funder_net": {"type": "object", "description": "who funded the dev, what that wallet did, other wallets it funded, with proof links"},
    "dev": {"type": "object"}, "safety": {"type": "object"}, "market": {"type": "object"}, "links": {"type": "object"}}}
