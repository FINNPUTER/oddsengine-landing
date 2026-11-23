"""
agents.py: OddsEngine AI fuer Agenten. Seit 2.31.

Drei Teile, jeder hinter einem Schalter auf admin.html:

  agents  Agent-Keys mit Tagesgrenze und erlaubten Modellen, Key per Wallet-Unterschrift
          ohne Browser (POST /v1/agents/key) und ein MCP-Server unter /mcp.
  x402    Bezahlen pro Anfrage ohne Konto: POST /v1/chat/completions ohne Key bekommt
          402 mit dem Preis in $ODDS. Der Agent unterschreibt ein EIP-2612 permit,
          der Server reicht es mit der Gas-Wallet ein und zieht die $ODDS direkt in die
          Einnahmen-Wallet. Der Betrag wird dem Agenten als Guthaben gutgeschrieben,
          die Anfrage daraus bezahlt, der Rest bleibt fuer die naechste.
          Seit 2.32 auch Standard-x402 in USDC auf Base (scheme exact), siehe usdc.py.

Die Gas-Wallet (X402_GAS_KEY) haelt nur ETH fuer Gebuehren. Sie bekommt nie $ODDS und
nie Kundengeld: transferFrom geht vom Agenten direkt an AI_DEPOSIT_ADDRESS.

Variablen (Service web):
    X402_GAS_KEY        privater Schluessel der Gas-Wallet (nur hier, nirgendwo sonst)
    X402_GAS_USD        Aufschlag pro Zahlung fuer das Gas, Standard 0.01
    X402_MIN_USD        kleinste Zahlung, Standard 0.05
    X402_MAX_TX_DAY     hoechstens so viele Zahlungen pro Tag, Standard 500
    X402_GAS_MIN_ETH    Warnung an den Betreiber darunter, Standard 0.0005
    ODDS_TOKEN_VERSION  EIP-712 version des Tokens, Standard "1"
"""
import base64
import json
import os
import time
from decimal import Decimal

import ai
import holder
import storage
from logger import log

PERMIT_SEL = "d505accf"
TRANSFER_FROM_SEL = "23b872dd"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


# ── Agent-Keys mit Grenzen ───────────────────────────────────────────────

def tabellen():
    pg = ai._pg()
    pg.execute("ALTER TABLE ai_keys ADD COLUMN IF NOT EXISTS day_cap NUMERIC(20,8)")
    pg.execute("ALTER TABLE ai_keys ADD COLUMN IF NOT EXISTS models TEXT")
    pg.execute("""CREATE TABLE IF NOT EXISTS ai_x402 (
        tx TEXT PRIMARY KEY, payer TEXT NOT NULL, odds NUMERIC(38,8), usd NUMERIC(20,8),
        gas_usd NUMERIC(20,8), permit_tx TEXT, ts TIMESTAMPTZ DEFAULT now())""")


_TAB = {"ok": False}


def _tab():
    if not _TAB["ok"]:
        tabellen()
        _TAB["ok"] = True


def grenzen_setzen(adresse, kid, day_cap=None, models=None):
    _tab()
    cap = None
    if day_cap not in (None, "", 0, "0"):
        cap = max(0.0, float(day_cap))
    ms = None
    if models:
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",")]
        ms = ",".join(sorted({str(m).strip() for m in models if str(m).strip()}))[:2000] or None
    ai._pg().execute("UPDATE ai_keys SET day_cap=%s, models=%s WHERE id=%s AND address=%s",
                     (Decimal(str(cap)) if cap is not None else None, ms, str(kid), holder.norm(adresse)))
    return {"id": kid, "day_cap": cap, "models": ms.split(",") if ms else None}


def grenzen(kid):
    _tab()
    r = ai._pg().execute("SELECT day_cap, models FROM ai_keys WHERE id=%s", (kid,)).fetchone()
    if not r:
        return None, None
    return (float(r[0]) if r[0] is not None else None), (r[1].split(",") if r[1] else None)


def pruefen(kid, model):
    """Wirft ai.Fehler, wenn der Key das Modell nicht darf oder sein Tageslimit erreicht hat."""
    cap, ms = grenzen(kid)
    model = model or ai.AUTO_ID
    if ms and model not in ms:
        raise ai.Fehler(403, "this key may only use: " + ", ".join(ms), "model_not_allowed")
    if cap is not None:
        heute = float(ai._pg().execute(
            """SELECT coalesce(sum(charged),0) FROM ai_usage
               WHERE key_id=%s AND ts > date_trunc('day', now())""", (kid,)).fetchone()[0])
        if heute >= cap:
            raise ai.Fehler(429, "this key reached its daily limit of ${}".format(
                ("{:.2f}" if cap >= 0.01 else "{:.4f}").format(cap)), "key_limit")


def key_mit_grenzen(adresse, name, day_cap, models):
    k = ai.key_neu(adresse, name)
    g = grenzen_setzen(adresse, k["id"], day_cap, models)
    k.update(day_cap=g["day_cap"], models=g["models"])
    return k


# ── Gas-Wallet und Chain ─────────────────────────────────────────────────

def _gas_konto():
    roh = os.getenv("X402_GAS_KEY", "").strip()
    if not roh:
        return None
    from eth_account import Account
    try:
        return Account.from_key(roh if roh.startswith("0x") else "0x" + roh)
    except Exception:
        log.warning("x402: X402_GAS_KEY is not a valid private key")
        return None


def gas_adresse():
    k = _gas_konto()
    return k.address if k else None


def bereit():
    return bool(_gas_konto() and holder.mint() and holder.evm() and ai.bereit())


def _kette():
    return int(holder.ketten_info().get("chain_id") or 4663)


def _hex32(n):
    return hex(int(n))[2:].rjust(64, "0")


def _adr32(a):
    return holder.norm(a)[2:].rjust(64, "0")


def _call(data):
    return holder._evm_rpc("eth_call", [{"to": holder.mint(), "data": data}, "latest"])


def gas_eth():
    a = gas_adresse()
    if not a:
        return None
    try:
        return int(holder._evm_rpc("eth_getBalance", [a, "latest"]), 16) / 1e18
    except Exception:
        return None


_DOM = {}


def _domain():
    """EIP-712 Domain des Tokens, gegen DOMAIN_SEPARATOR auf der Chain geprueft."""
    if _DOM.get("d"):
        return _DOM["d"]
    from eth_account.messages import encode_typed_data  # noqa: F401
    from eth_utils import keccak
    roh = _call("0x06fdde03")
    try:
        b = bytes.fromhex(roh[2:])
        n = int.from_bytes(b[32:64], "big")
        name = b[64:64 + n].decode()
    except Exception:
        name = "OddsEngine"
    chain = _kette()
    soll = (_call("0x3644e515") or "").lower()
    for version in [os.getenv("ODDS_TOKEN_VERSION", "1"), "1", "2"]:
        typ = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
        enc = (typ + keccak(text=name) + keccak(text=version) + int(chain).to_bytes(32, "big")
               + bytes.fromhex(_adr32(holder.mint())))
        if "0x" + keccak(enc).hex().replace("0x", "") == soll:
            _DOM["d"] = {"name": name, "version": version, "chainId": chain,
                         "verifyingContract": holder.mint()}
            return _DOM["d"]
    _DOM["d"] = {"name": name, "version": os.getenv("ODDS_TOKEN_VERSION", "1"), "chainId": chain,
                 "verifyingContract": holder.mint(), "unverified": True}
    return _DOM["d"]


def _permit_unterzeichner(owner, spender, value, nonce, deadline, sig):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    d = _domain()
    msg = {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"},
                             {"name": "verifyingContract", "type": "address"}],
            "Permit": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"},
                       {"name": "value", "type": "uint256"}, {"name": "nonce", "type": "uint256"},
                       {"name": "deadline", "type": "uint256"}]},
        "primaryType": "Permit",
        "domain": {k: d[k] for k in ("name", "version", "chainId", "verifyingContract")},
        "message": {"owner": owner, "spender": spender, "value": int(value), "nonce": int(nonce),
                    "deadline": int(deadline)}}
    return Account.recover_message(encode_typed_data(full_message=msg), signature=sig)


def _senden(data):
    """Transaktion an den Token von der Gas-Wallet, wartet auf die Quittung."""
    from eth_account import Account
    k = _gas_konto()
    chain = _kette()
    nonce = int(holder._evm_rpc("eth_getTransactionCount", [k.address, "pending"]), 16)
    try:
        gas = int(holder._evm_rpc("eth_estimateGas", [{"from": k.address, "to": holder.mint(),
                                                       "data": data}]), 16)
    except RuntimeError as e:
        raise ai.Fehler(402, "payment would fail on chain: {}".format(str(e)[:120]), "payment_failed")
    preis = int(holder._evm_rpc("eth_gasPrice", []), 16)
    from eth_utils import to_checksum_address
    tx = {"to": to_checksum_address(holder.mint()), "data": data, "value": 0, "nonce": nonce, "chainId": chain,
          "gas": int(gas * 1.3) + 10000, "maxFeePerGas": preis * 2 + 1, "maxPriorityFeePerGas": max(1, preis // 10),
          "type": 2}
    roh = Account.sign_transaction(tx, k.key)
    raw = getattr(roh, "raw_transaction", None) or getattr(roh, "rawTransaction")
    h = holder._evm_rpc("eth_sendRawTransaction", ["0x" + bytes(raw).hex().replace("0x", "")])
    for _ in range(60):
        q = holder._evm_rpc("eth_getTransactionReceipt", [h])
        if q:
            if int(q.get("status") or "0x0", 16) != 1:
                raise ai.Fehler(402, "payment transaction failed on chain", "payment_failed")
            return h, q
        time.sleep(1)
    raise ai.Fehler(504, "payment is still pending on chain, try again shortly", "payment_pending")


# ── x402 ─────────────────────────────────────────────────────────────────

def _bedarf_usd(body):
    """Hoechstbetrag fuer diese Anfrage in Dollar, wie ai.vorbereiten ihn reservieren wuerde,
    plus Gas. Ohne Konto rechnen wir mit dem Grundpreis."""
    mid = str(body.get("model") or ai.AUTO_ID).strip()
    m = ai.modell(mid)
    if not m:
        raise ai.Fehler(404, f"model '{mid}' is not available. See GET /v1/models", "model_not_found")
    k = 1 + ai.markup()
    pin = (m["in_base"] if m["in_base"] is not None else ai.AUTO_PREIS[0]) / 1e6
    pout = (m["out_base"] if m["out_base"] is not None else ai.AUTO_PREIS[1]) / 1e6
    ein = ai._schaetzung_tokens(body)
    aus = int(body.get("max_tokens") or body.get("max_completion_tokens") or 2048)
    aus = min(aus, m.get("max_output") or 32768)
    usd = (ein * pin + aus * pout) * k
    if ai._mit_web(body):
        usd += (ai._f("AI_WEB_RESERVE_USD", 0.03) + 20000 * pin) * k
    if m.get("kind") == "image" or "image" in (body.get("modalities") or []):
        usd += ((m.get("image_out_base") or 60.0) / 1e6) * 8000 * k
    m = dict(m, roh_usd=usd)
    return max(_f("X402_MIN_USD", 0.05), usd * 1.15) + _f("X402_GAS_USD", 0.01), m


def bedarf_fest(preis_usd, name):
    """Seit 2.79.0: fester Preis (z. B. ein Scan) statt Modellkosten. Gleiches Format wie _bedarf_usd."""
    return max(preis_usd, 0.001) + _f("X402_GAS_USD", 0.01), {"name": name, "roh_usd": preis_usd, "fest": True}


def angebot(body, url, bedarf=None, schema=None):
    """Die 402-Antwort: was diese Anfrage hoechstens kostet. USDC auf Base, seit 2.80 auch USDC auf Solana und
    USDG auf Robinhood Chain (wenn an), und $ODDS. schema: Bazaar-Angaben (input/output) fuer die Discovery."""
    usd, m = bedarf or _bedarf_usd(body)
    import usdc
    import zahlwege
    vorne = [usdc.anforderung(usd, m, url)] if usdc.bereit() else []
    acc2 = [usdc.anforderung_v2(usd, m)] if vorne else []
    try:
        if zahlwege.usdg_bereit():
            vorne.append(zahlwege.usdg_v1(usd, m, url))
            acc2.append(zahlwege.usdg_v2(usd, m))
        if zahlwege.sol_bereit():
            vorne.append(zahlwege.sol_v1(usd, m, url))
            acc2.append(zahlwege.sol_v2(usd, m))
    except Exception as e:
        log.warning(f"x402 extra ways: {e}")
    if schema:
        for x in vorne:
            x["outputSchema"] = schema
    kurs, _ = holder.kurs()
    v2 = {"x402Version": 2, "error": "payment required",
          "resource": {"url": url, "description": ("OddsEngine, {}" if bedarf else "OddsEngine AI, {}").format(m["name"]),
                       "mimeType": "application/json"},
          "accepts": acc2}
    if schema:
        v2["extensions"] = {"bazaar": {"info": schema}}
    if not kurs or not bereit():
        if vorne:
            return {"x402Version": 1, "error": "payment required", "accepts": vorne, "_v2": v2}
        raise ai.Fehler(503, "the $ODDS price is not available right now", "service_unavailable")
    dez = holder._dezimalen()
    menge = Decimal(str(usd / kurs)).quantize(Decimal("0.01"))
    roh = int(menge * (Decimal(10) ** dez))
    d = _domain()
    v2["accepts"].append({"scheme": "permit", "network": "eip155:{}".format(d["chainId"]), "amount": str(roh),
                          "asset": holder.mint(), "payTo": ai.eingang(), "maxTimeoutSeconds": 300,
                          "extra": {"token": "ODDS", "decimals": dez, "spender": gas_adresse(),
                                    "eip712": {k: d[k] for k in ("name", "version", "chainId",
                                                                  "verifyingContract")}}})
    return {"x402Version": 1, "error": "payment required", "_v2": v2,
            "accepts": vorne + [{
                "scheme": "permit", "network": "robinhood", "chainId": d["chainId"],
                "maxAmountRequired": str(roh), "asset": holder.mint(), "payTo": ai.eingang(),
                "resource": url, "mimeType": "application/json", "maxTimeoutSeconds": 300,
                "description": "OddsEngine{}, {}. Up to ${:.4f} in $ODDS. Whatever is not used "
                               "stays as credit on your wallet and pays your next requests: send a "
                               "fresh permit and we use the credit first, no new transfer."
                               .format("" if m.get("fest") else " AI", m["name"], usd),
                "extra": {"token": "ODDS", "decimals": dez, "spender": gas_adresse(),
                          "eip712": {k: d[k] for k in ("name", "version", "chainId", "verifyingContract")},
                          "usd": round(usd, 6), "price_usd": kurs,
                          "how": "Sign an EIP-2612 Permit(owner, spender, value, nonce, deadline) for at "
                                 "least maxAmountRequired, then repeat the request with header X-PAYMENT: "
                                 "base64 of {\"x402Version\":1,\"scheme\":\"permit\",\"network\":\"robinhood\","
                                 "\"payload\":{\"owner\",\"value\",\"deadline\",\"signature\"}}"}}]}


def _heute_n():
    try:
        return int(ai._pg().execute(
            "SELECT count(*) FROM ai_x402 WHERE ts > date_trunc('day', now())").fetchone()[0])
    except Exception:
        return 0


def bezahlen(kopf, body, bedarf=None):
    """X-PAYMENT pruefen, auf der Chain einloesen, gutschreiben. Gibt (payer, info) zurueck.
    bedarf: fester Preis aus bedarf_fest (seit 2.79.0), sonst nach Modell."""
    _tab()
    if _heute_n() >= int(_f("X402_MAX_TX_DAY", 500)):
        raise ai.Fehler(429, "x402 is busy today, use an API key instead", "x402_limit")
    try:
        p = json.loads(base64.b64decode(kopf + "=" * (-len(kopf) % 4)).decode())
    except Exception:
        raise ai.Fehler(400, "X-PAYMENT is not valid base64 JSON", "invalid_payment")
    roh_p = p
    if isinstance(p, dict) and int(p.get("x402Version") or 1) >= 2 and isinstance(p.get("accepted"), dict):
        acc = p["accepted"]
        netz = str(acc.get("network") or "")
        p = {"x402Version": 2, "scheme": acc.get("scheme"),
             "network": "base" if netz in ("eip155:8453", "base") else
             ("robinhood" if netz.startswith("eip155:") else netz),
             "payload": p.get("payload") or {}}
    if isinstance(p, dict) and p.get("scheme") == "exact" and str(p.get("network") or "").startswith("solana"):
        import zahlwege   # seit 2.80: USDC auf Solana ueber Facilitator, braucht das Original (v1 oder v2)
        return zahlwege.sol_bezahlen(roh_p, bedarf or _bedarf_usd(body))
    if isinstance(p, dict) and p.get("scheme") == "exact" and p.get("network") in ("robinhood", "eip155:4663"):
        import zahlwege   # seit 2.80: USDG auf Robinhood Chain
        return zahlwege.usdg_bezahlen(p, bedarf or _bedarf_usd(body))
    if isinstance(p, dict) and p.get("scheme") == "exact":
        import usdc
        return usdc.bezahlen(p, body, bedarf or _bedarf_usd(body))
    if not bereit():
        raise ai.Fehler(400, "the permit scheme is not available right now, pay with scheme exact (USDC on base)",
                        "invalid_payment")
    try:
        pl = p.get("payload") or {}
        owner = holder.norm(pl["owner"])
        value = int(str(pl["value"]))
        deadline = int(pl["deadline"])
        sig = str(pl["signature"])
        sig = sig if sig.startswith("0x") else "0x" + sig
    except Exception:
        raise ai.Fehler(400, "X-PAYMENT is not valid base64 JSON with payload owner, value, deadline, "
                             "signature", "invalid_payment")
    if p.get("scheme") not in (None, "permit"):
        raise ai.Fehler(400, "supported schemes: exact (USDC on base) and permit ($ODDS)", "invalid_payment")
    if deadline < time.time() + 60:
        raise ai.Fehler(400, "permit deadline is too close or already passed", "invalid_payment")
    spender = gas_adresse()
    nonce = int(_call("0x7ecebe00" + _adr32(owner)), 16)
    try:
        wer = _permit_unterzeichner(owner, spender, value, nonce, deadline, sig)
    except Exception:
        wer = None
    if not wer or holder.norm(wer) != owner:
        raise ai.Fehler(402, "permit signature does not match the owner", "invalid_payment")
    # Jede Unterschrift genau einmal, auch wenn sie nur Guthaben freigibt
    import hashlib
    merk = "x402s_" + hashlib.sha256(sig.lower().encode()).hexdigest()[:40]
    if storage.kv_get(merk):
        raise ai.Fehler(402, "this payment was already used, sign a new permit", "invalid_payment")
    storage.kv_set(merk, "1")
    usd_noetig, mz = bedarf or _bedarf_usd(body)
    gas_usd = _f("X402_GAS_USD", 0.01)
    # Reicht das Guthaben vom letzten Mal, zahlt der Agent nichts neu und spart das Gas
    kt = ai.konto(owner) or {}
    if kt.get("credit", 0.0) >= mz["roh_usd"] * 1.05:
        return owner, {"success": True, "transaction": None, "network": "robinhood", "payer": owner,
                       "paid_from": "credit", "credit_usd": round(kt.get("credit", 0.0), 6)}
    kurs, _ = holder.kurs()
    dez = holder._dezimalen()
    menge = value / (10 ** dez)
    if not kurs or menge * kurs < usd_noetig * 0.97:
        raise ai.Fehler(402, "payment too small, this request needs about ${:.4f} in $ODDS".format(usd_noetig),
                        "payment_too_small")
    stand = int(_call("0x70a08231" + _adr32(owner)), 16)
    if stand < value:
        raise ai.Fehler(402, "not enough $ODDS in the paying wallet", "insufficient_funds")
    s = sig[2:]
    r_, s_, v = s[:64], s[64:128], int(s[128:130], 16)
    if v < 27:
        v += 27
    permit = ("0x" + PERMIT_SEL + _adr32(owner) + _adr32(spender) + _hex32(value) + _hex32(deadline)
              + _hex32(v) + r_ + s_)
    ptx, _ = _senden(permit)
    zug = "0x" + TRANSFER_FROM_SEL + _adr32(owner) + _adr32(ai.eingang()) + _hex32(value)
    ttx, quit = _senden(zug)
    li = 0
    an = "0x" + _adr32(ai.eingang())
    for lg in quit.get("logs") or []:
        t = lg.get("topics") or []
        if len(t) == 3 and t[0].lower() == TRANSFER_TOPIC and t[2].lower().endswith(an[-40:].lower()):
            li = int(lg.get("logIndex") or "0x0", 16)
            break
    usd = max(0.0, menge * kurs - gas_usd)
    pg = ai._pg()
    pg.execute("""INSERT INTO ai_deposits (tx, li, block, sender, odds, usd, price)
                  VALUES (%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (tx, li) DO UPDATE SET usd=EXCLUDED.usd, price=EXCLUDED.price
                  WHERE ai_deposits.credited_to IS NULL""",
               (ttx, li, int(quit.get("blockNumber") or "0x0", 16), owner, Decimal(str(menge)),
                Decimal(str(round(usd, 8))), Decimal(str(kurs))))
    ai.konto(owner)
    ai._gutschreiben(ttx, li, owner)
    pg.execute("""INSERT INTO ai_x402 (tx, payer, odds, usd, gas_usd, permit_tx)
                  VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
               (ttx, owner, Decimal(str(menge)), Decimal(str(round(usd, 8))), Decimal(str(gas_usd)), ptx))
    log.info(f"x402: {owner} paid {menge:.2f} ODDS (${usd:.4f}) tx {ttx}")
    return owner, {"success": True, "transaction": ttx, "network": "robinhood", "payer": owner,
                   "credited_usd": round(usd, 6)}


def antwort_kopf(info, v2=False):
    if v2:
        info = dict(info)
        info["network"] = {"base": "eip155:8453", "robinhood": "eip155:4663"}.get(info.get("network"),
                                                                             info.get("network"))
    return base64.b64encode(json.dumps(info).encode()).decode()


def angebot_kopf(v2):
    return base64.b64encode(json.dumps(v2).encode()).decode()


# ── MCP (Streamable HTTP, JSON-RPC 2.0) ─────────────────────────────────

WERKZEUGE = [
    {"name": "list_models",
     "description": "Search the 460+ models on OddsEngine AI. Returns id, name, maker, price per "
                    "typical message in USD and what it is good at.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "words to search in name, id or maker"},
         "category": {"type": "string", "enum": ["picked", "new", "free", "code", "writing", "reasoning",
                                                 "vision", "image", "long context", "cheap", "web search"]},
         "limit": {"type": "integer", "default": 20}}}},
    {"name": "estimate_cost",
     "description": "What one request costs on a model before you run it, in USD, fee included.",
     "inputSchema": {"type": "object", "required": ["model"], "properties": {
         "model": {"type": "string"}, "input_tokens": {"type": "integer", "default": 1000},
         "output_tokens": {"type": "integer", "default": 500}}}},
    {"name": "chat",
     "description": "Ask a model. Paid from the balance of this key's wallet.",
     "inputSchema": {"type": "object", "required": ["model"], "properties": {
         "model": {"type": "string", "description": "model id, or oddsengine/auto"},
         "prompt": {"type": "string"},
         "messages": {"type": "array", "items": {"type": "object"}},
         "max_tokens": {"type": "integer"}}}},
    {"name": "compare",
     "description": "Send one prompt to 2 or 3 models at once and get every answer with its cost.",
     "inputSchema": {"type": "object", "required": ["models", "prompt"], "properties": {
         "models": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
         "prompt": {"type": "string"}, "max_tokens": {"type": "integer", "default": 800}}}},
    {"name": "balance",
     "description": "Balance in USD, your fee and your $ODDS holdings.",
     "inputSchema": {"type": "object", "properties": {}}},
    # Seit 2.60.4: die Krypto-Werkzeuge von Your Engine auch fuer Agenten
    {"name": "scan_token",
     "description": "Full safety and market scan of any token on Solana, Base, Ethereum, BNB Chain, Arbitrum or Robinhood "
                    "Chain, facts only: price, market cap, liquidity, honeypot and taxes, mint and freeze rights, LP lock, top "
                    "holders, bundles, snipers, same-funder clusters, dev wallet and earlier launches. The deep part (bundles, "
                    "clusters) may say running: call again after a minute.",
     "inputSchema": {"type": "object", "required": ["token"], "properties": {
         "token": {"type": "string", "description": "contract address, mint, chart link or $TICKER"}}}},
    {"name": "trace_wallet",
     "description": "Who is behind a wallet: its funder, biggest inflows, labels (exchange, bridge, contract), age and links "
                    "to other wallets, with proof links.",
     "inputSchema": {"type": "object", "required": ["address"], "properties": {
         "address": {"type": "string"}, "chain": {"type": "string"}}}},
    {"name": "money_out",
     "description": "Follow the money out of a wallet: where the funds went, split into which wallets, swapped, bridged or "
                    "sent to an exchange.",
     "inputSchema": {"type": "object", "required": ["address"], "properties": {
         "address": {"type": "string"}, "chain": {"type": "string"}}}},
    {"name": "drain_report",
     "description": "Drain report for a drained wallet or a drain transaction: who took the funds, where the money went, "
                    "whether it reached an exchange, and what to do now. Every step with proof.",
     "inputSchema": {"type": "object", "properties": {
         "hash": {"type": "string"}, "wallet": {"type": "string"}, "chain": {"type": "string"}}}},
    {"name": "check_before_sign",
     "description": "What a wallet signature really does before signing: EVM calldata, signTypedData JSON (Permit, Permit2, "
                    "Seaport), eth_sign hashes, login texts and Solana transactions. Returns DO NOT SIGN, CAREFUL or LOOKS "
                    "NORMAL with reasons (unlimited approvals, known drainers, look-alike domains, owner changes). For a "
                    "paying agent, pass to, allow (approved payees) and cap_usd: a payment to an address not on the list, "
                    "or over the limit, is flagged before it is signed. With to alone it also checks the recipient itself from "
                    "OddsEngine records and the chain: known drainers, wallets funded by drainers, look-alikes of an approved "
                    "payee (address poisoning), brand new or empty addresses.",
     "inputSchema": {"type": "object", "properties": {
         "raw": {"type": "string"}, "chain": {"type": "string"}, "method": {"type": "string"},
         "spender": {"type": "string"}, "token": {"type": "string"}, "amount": {"type": "string"},
         "site": {"type": "string"}}}},
    {"name": "wallet_approvals",
     "description": "Open token approvals and Solana delegates of a wallet with risk per spender and revoke links the wallet "
                    "owner can sign.",
     "inputSchema": {"type": "object", "required": ["wallet"], "properties": {"wallet": {"type": "string"}}}},
]


def _text(obj, fehler=False):
    t = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return {"content": [{"type": "text", "text": t}], "isError": bool(fehler)}


def _passt(m, cat):
    tags = m.get("tags") or []
    return {"picked": m.get("picked"), "new": m.get("new"), "free": m.get("free"),
            "image": m.get("kind") == "image", "cheap": m.get("tier") == "cheap",
            "vision": "vision" in tags}.get(cat, cat in tags)


def mcp_min_hold():
    """So viele $ODDS muss die Wallet hinter einem Key halten, um MCP zu nutzen.
    Verbundene Wallets (z. B. Torch) zaehlen mit. 0 schaltet die Sperre ab."""
    return _f("MCP_MIN_HOLD", 250000)


# ── Partner (seit 2.82.101): Nutzer, die ueber einen Partner-Link kommen (engine.oddsengine.live/?ref=nomad) ──
# PARTNERS = "nomad:100:1" (Name:wie viele Wallets Startguthaben bekommen:Dollar je Wallet), mehrere mit Komma.
# Partner-Nutzer brauchen fuer MCP keine Haltemenge, zahlen aber jeden Aufruf aus ihrem Guthaben.
def partner_liste():
    out = {}
    for teil in (os.getenv("PARTNERS", "nomad:100:1") or "").split(","):
        x = [y.strip() for y in teil.split(":")]
        if x and x[0]:
            try:
                out[x[0].lower()] = (int(x[1]) if len(x) > 1 else 0, float(x[2]) if len(x) > 2 else 0.0)
            except Exception:
                out[x[0].lower()] = (0, 0.0)
    return out


def partner_von(adr):
    try:
        return storage.kv_get("partner_" + holder.norm(adr)) or None
    except Exception:
        return None


def partner_markieren(adr, ref):
    """Wallet einem Partner zuordnen (nur beim ersten Mal). Gibt {"partner", "granted_usd"} zurueck oder None."""
    ref = str(ref or "").strip().lower()[:20]
    p = partner_liste()
    if ref not in p:
        return None
    a = holder.norm(adr)
    alt = storage.kv_get("partner_" + a)
    if alt:
        return {"partner": alt, "granted_usd": 0}
    storage.kv_set("partner_" + a, ref)
    try:
        liste = json.loads(storage.kv_get("partner_list_" + ref) or "[]")
    except Exception:
        liste = []
    cap, usd = p[ref]
    gegeben = 0.0
    schon = storage.kv_get("beta_grant_" + a) or storage.kv_get("partner_grant_" + a)
    if usd > 0 and not schon and sum(1 for x in liste if x.get("usd")) < cap:
        ai.konto(a, anlegen=True)
        ai._pg().execute("UPDATE ai_accounts SET credit = credit + %s WHERE address=%s", (Decimal(str(round(usd, 4))), a))
        storage.kv_set("partner_grant_" + a, str(usd))
        gegeben = usd
    liste.append({"wallet": a, "ts": int(time.time()), "usd": gegeben})
    storage.kv_set("partner_list_" + ref, json.dumps(liste[-2000:]))
    log.info(f"partner {ref}: {a} joined, start credit ${gegeben}")
    return {"partner": ref, "granted_usd": gegeben}


def partner_bericht():
    out = []
    for ref, (cap, usd) in partner_liste().items():
        try:
            liste = json.loads(storage.kv_get("partner_list_" + ref) or "[]")
        except Exception:
            liste = []
        zeilen = []
        for x in liste[-200:]:
            kt = ai.konto(x["wallet"], anlegen=False) or {}
            zeilen.append({**x, "credit": round(kt.get("credit", 0.0), 4), "spent": round(kt.get("spent", 0.0), 4)})
        out.append({"partner": ref, "wallets": len(liste), "with_credit": sum(1 for x in liste if x.get("usd")), "cap": cap,
                    "usd_each": usd, "given_usd": round(sum(x.get("usd") or 0 for x in liste), 2),
                    "spent_usd": round(sum(z["spent"] for z in zeilen), 4), "link": f"https://engine.oddsengine.live/?ref={ref}",
                    "recent": list(reversed(zeilen))[:30]})
    return {"partners": out}


def _mcp_frei(adr):
    """Seit 2.82.101: Tester aus admin.html (Your Engine beta) und MCP_ALLOW (Komma-Liste) duerfen MCP ohne die
    Haltemenge nutzen, z. B. Partner fuer eine Demo. Bezahlt wird trotzdem jeder Aufruf aus dem Guthaben."""
    try:
        a = holder.norm(adr)
        if a in {holder.norm(x) for x in os.getenv("MCP_ALLOW", "").split(",") if x.strip()}:
            return True
        if (storage.kv_get("partner_" + a) or "") in partner_liste():
            return True
        import motor
        return a in motor.beta_liste()
    except Exception:
        return False


def mcp_sperre(wer):
    """None, wenn der Key MCP nutzen darf, sonst der Text fuer den Agenten."""
    if not wer:
        return ("OddsEngine MCP is for $ODDS holders. Create an API key with a wallet that holds "
                "at least {:,.0f} $ODDS (linked wallets count) and send it as "
                "Authorization: Bearer oe_live_...".format(mcp_min_hold()))
    mindest = mcp_min_hold()
    if mindest <= 0:
        return None
    if _mcp_frei(wer[0]):
        return None
    try:
        held = float(ai.holder_stufe(wer[0]).get("held") or 0)
    except Exception:
        held = 0.0
    if held < mindest:
        return ("OddsEngine MCP is for $ODDS holders. This wallet holds {:,.0f} $ODDS, it needs "
                "{:,.0f}. Linked wallets count too.".format(held, mindest))
    return None


def _zahl(x, standard, lo, hi):
    """Zahl aus einem Werkzeug-Argument: falscher Typ faellt auf den Standard, der Wert bleibt in den Grenzen."""
    try:
        v = int(float(x)) if x is not None and x != "" else standard
    except Exception:
        v = standard
    return max(lo, min(hi, v))


# ── Protokoll aller MCP-Aufrufe (seit 2.82.101): wer, welches Werkzeug, was durchkam und was nicht ──
_MCP_SCHEMA = [False]


def _mcp_log(wer, name, args, status, text, ms):
    try:
        pg = ai._pg()
        if not _MCP_SCHEMA[0]:
            pg.execute("""CREATE TABLE IF NOT EXISTS mcp_calls (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
                wallet TEXT, partner TEXT, tool TEXT, target TEXT, status TEXT, note TEXT, ms INT)""")
            pg.execute("CREATE INDEX IF NOT EXISTS ix_mcp_calls_ts ON mcp_calls(ts)")
            _MCP_SCHEMA[0] = True
        a = wer[0] if wer else None
        ziel = ""
        if isinstance(args, dict):
            for k in ("q", "token", "address", "query", "wallet", "chain", "model", "to"):
                if args.get(k):
                    ziel += ("" if not ziel else " ") + str(args.get(k))[:80]
        pg.execute("INSERT INTO mcp_calls (wallet, partner, tool, target, status, note, ms) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                   (a, partner_von(a) if a else None, str(name)[:40], ziel[:160] or None, status, (text or "")[:200], int(ms)))
    except Exception as e:
        log.debug(f"mcp log: {e}")


def _mcp_status(r):
    if not (isinstance(r, dict) and r.get("isError")):
        return "ok", ""
    t = ""
    try:
        t = r["content"][0]["text"]
    except Exception:
        pass
    tl = t.lower()
    if "for $odds holders" in tl:
        return "refused_holding", t
    if "credit is empty" in tl:
        return "no_credit", t
    if "daily limit" in tl:
        return "day_limit", t
    if "needs an api key" in tl:
        return "no_key", t
    if "failed on our side" in tl:
        return "error", t
    return "tool_error", t


def werkzeug(name, args, wer):
    t0 = time.time()
    try:
        r = _werkzeug(name, args, wer)
    except Exception as e:
        _mcp_log(wer, name, args, "error", str(e), (time.time() - t0) * 1000)
        raise
    st, txt = _mcp_status(r)
    _mcp_log(wer, name, args, st, txt, (time.time() - t0) * 1000)
    return r


def mcp_calls(tage=7, wallet=None, partner=None, n=200):
    pg = ai._pg()
    try:
        bed, par = ["ts > now() - make_interval(days => %s)"], [int(tage)]
        if wallet:
            bed.append("wallet = %s")
            par.append(holder.norm(wallet))
        if partner:
            bed.append("partner = %s")
            par.append(partner)
        w = " AND ".join(bed)
        rows = pg.execute(f"""SELECT ts, wallet, partner, tool, target, status, note, ms FROM mcp_calls WHERE {w}
                              ORDER BY ts DESC LIMIT %s""", par + [int(n)]).fetchall()
        sums = pg.execute(f"""SELECT wallet, max(partner), count(*), count(*) FILTER (WHERE status='ok'), max(ts)
                              FROM mcp_calls WHERE {w} GROUP BY wallet ORDER BY 5 DESC LIMIT 50""", par).fetchall()
        st = pg.execute(f"SELECT status, count(*) FROM mcp_calls WHERE {w} GROUP BY 1", par).fetchall()
    except Exception as e:
        return {"calls": [], "wallets": [], "error": str(e)[:160]}
    return {"days": tage, "by_status": {a: int(b) for a, b in st},
            "wallets": [{"wallet": r[0], "partner": r[1], "calls": int(r[2]), "ok": int(r[3]), "last": int(r[4].timestamp())}
                        for r in sums],
            "calls": [{"ts": int(r[0].timestamp()), "wallet": r[1], "partner": r[2], "tool": r[3], "target": r[4],
                       "status": r[5], "note": r[6], "ms": r[7]} for r in rows]}


def _werkzeug(name, args, wer):
    args = args or {}
    sperre = mcp_sperre(wer)
    if sperre:
        return _text(sperre, True)
    if name == "list_models":
        q = str(args.get("query") or "").lower()
        cat = args.get("category")
        out = []
        for m in ai.modelle():
            if q and q not in (m["name"] + " " + m["id"] + " " + (m.get("provider") or "")).lower():
                continue
            if cat and not _passt(m, cat):
                continue
            out.append({"id": m["id"], "name": m["name"], "maker": m.get("provider"),
                        "per_message_usd": m.get("per_msg"), "per_image_usd": m.get("per_image"),
                        "free": bool(m.get("free")), "new": bool(m.get("new")),
                        "tags": m.get("tags"), "context": m.get("context")})
            if len(out) >= _zahl(args.get("limit"), 20, 1, 50):
                break
        return _text(out)
    if name == "estimate_cost":
        m = ai.modell(str(args.get("model") or ""))
        if not m:
            return _text("unknown model, use list_models", True)
        k = 1 + (ai.markup(wer[0]) if wer else ai.markup())
        if m.get("in_base") is None:
            return _text({"model": m["id"], "note": "price depends on the model it routes to"})
        usd = (_zahl(args.get("input_tokens"), 1000, 0, 10_000_000) * m["in_base"]
               + _zahl(args.get("output_tokens"), 500, 0, 10_000_000) * m["out_base"]) / 1e6 * k
        return _text({"model": m["id"], "usd": round(usd, 6), "fee": round(k - 1, 4)})
    if name in KRYPTO:
        if not kontingent(wer):
            return _text("Daily limit for the crypto tools reached ({:,.0f} calls per wallet). It resets at 00:00 UTC."
                         .format(_f("MCP_TOOL_DAY", 150)), True)
        # Seit 2.60.6: wie im Bot. Halten ist die Voraussetzung, jede Abfrage kostet MCP_TOOL_PRICE vom Guthaben.
        preis = _f("MCP_TOOL_PRICE", 0.02)
        if preis > 0 and wer and not ai._reservieren(wer[0], preis):
            return _text("Your Engine credit is empty. Each crypto tool call costs ${:.2f}. Top up in @OddsEnginePublicBot "
                         "(/balance), then ask again.".format(preis), True)
        try:
            r = krypto(name, args, wer)
        except Exception:
            if preis > 0 and wer:
                ai._ausgleichen(wer[0], preis, 0)
            raise
        fehl = isinstance(r, dict) and r.get("error") and len(r) <= 3
        if preis > 0 and wer:
            ai._ausgleichen(wer[0], preis, 0 if fehl else preis)
        return _text(_sauber(r), bool(fehl))
    if not wer:
        return _text("this tool needs an API key: Authorization: Bearer oe_live_...", True)
    adr, kid = wer
    if name == "balance":
        kt = ai.konto(adr) or {}
        st = ai.holder_stufe(adr)
        return _text({"usd": round(kt.get("credit", 0.0), 4), "fee": st.get("markup"),
                      "holding_odds": st.get("held"), "next_tier": st.get("next")})
    if name == "chat":
        msgs = args.get("messages") or [{"role": "user", "content": str(args.get("prompt") or "")}]
        body = {"model": args.get("model") or ai.AUTO_ID, "messages": msgs}
        if args.get("max_tokens"):
            body["max_tokens"] = _zahl(args["max_tokens"], 800, 1, 200_000)
        try:
            pruefen(kid, body["model"])
            d = ai.anfrage(adr, kid, body)
        except ai.Fehler as e:
            return _text(e.text if hasattr(e, "text") else str(e), True)
        msg = ((d.get("choices") or [{}])[0].get("message") or {})
        return _text({"model": d.get("model"), "content": msg.get("content"),
                      "cost_usd": (d.get("usage") or {}).get("oddsengine_charged_usd")})
    if name == "compare":
        ms = [str(x) for x in (args.get("models") or [])][:3]
        if len(ms) < 2:
            return _text("give 2 or 3 models", True)
        out = []
        for mid in ms:
            try:
                pruefen(kid, mid)
                d = ai.anfrage(adr, kid, {"model": mid, "max_tokens": _zahl(args.get("max_tokens"), 800, 1, 200_000),
                                          "messages": [{"role": "user", "content": str(args.get("prompt") or "")}]})
                msg = ((d.get("choices") or [{}])[0].get("message") or {})
                out.append({"model": mid, "content": msg.get("content"),
                            "cost_usd": (d.get("usage") or {}).get("oddsengine_charged_usd")})
            except ai.Fehler as e:
                out.append({"model": mid, "error": getattr(e, "text", str(e))})
        return _text(out)
    return _text("unknown tool " + str(name), True)


VERSION = "2.82.111"
PROTOKOLLE = ("2025-06-18", "2025-03-26", "2024-11-05")   # MCP-Versionen, die dieser Server spricht
NUR_LESEN = {"list_models", "estimate_cost", "balance", "scan_token", "trace_wallet", "money_out", "drain_report",
             "check_before_sign", "wallet_approvals"}
NUTZUNG = "Results are for your own use. No resale, mirroring or bulk export of tool results."
INTERN = {"shown_to_user", "debug", "cache"}


def _sauber(r):
    """Interne Felder raus, bevor ein Ergebnis das Haus verlaesst (seit 2.60.6)."""
    if isinstance(r, dict):
        return {k: _sauber(v) for k, v in r.items() if k not in INTERN and not str(k).startswith("_")}
    if isinstance(r, list):
        return [_sauber(x) for x in r]
    return r


def kontingent(wer):
    """Tageskontingent der Krypto-Tools pro Wallet (MCP_TOOL_DAY, Standard 150, 0 = aus). Seit 2.60.6.
    Zaehlt pro Wallet, nicht pro Key, damit mehrere Keys es nicht umgehen. Beim ersten Ueberschreiten
    bekommt der Betreiber eine Nachricht."""
    if not wer:
        return True
    grenze = int(_f("MCP_TOOL_DAY", 150))
    if grenze <= 0:
        return True
    k = "mcpq_{}_{}".format(wer[0], time.strftime("%Y%m%d", time.gmtime()))
    try:
        n = int(storage.kv_get(k) or 0) + 1
        storage.kv_set(k, str(n))
    except Exception:
        return True
    if n == grenze + 1:
        try:
            ai._an_betreiber("OddsEngine MCP: Wallet {} hat heute das Limit von {} Krypto-Abfragen erreicht "
                             "(Key {}). Wenn das oefter passiert, auf admin.html pruefen oder den Key sperren."
                             .format(wer[0], grenze, wer[1]))
        except Exception:
            pass
    return n <= grenze


CONNECTOR = "connector"
MCP_URL = "https://api.oddsengine.live/mcp"


def connector_neu(adr):
    """Persoenlicher Connector-Link fuer ChatGPT, Claude und andere Apps, die nur eine URL nehmen.
    Ein neuer Link sperrt den alten. Seit 2.60.6."""
    adr = holder.norm(adr)
    connector_weg(adr)
    k = key_mit_grenzen(adr, CONNECTOR, _f("CONNECTOR_DAY_CAP", 1), None)
    return {"url": MCP_URL + "/k/" + k["key"], "id": k["id"], "day_cap": k.get("day_cap")}


def connector_weg(adr):
    n = 0
    for k in ai.keys_liste(adr):
        if k.get("name") == CONNECTOR:
            ai.key_weg(adr, k["id"])
            n += 1
    return n


def connector_info(adr):
    """Fuer den Bot: Link erzeugen und erklaeren, inklusive Haltepruefung."""
    mindest = 0 if _mcp_frei(adr) else mcp_min_hold()
    try:
        held = float(ai.holder_stufe(adr).get("held") or 0)
    except Exception:
        held = 0.0
    if mindest > 0 and held < mindest:
        return {"ok": False, "needs_odds": mindest, "holds_odds": round(held),
                "note": "The ChatGPT and Claude connector is for $ODDS holders. Say how many $ODDS they hold and how many "
                        "they need (linked wallets count). Offer the Trade on Torch link for $ODDS. Do not create a link."}
    c = connector_neu(adr)
    try:
        guthaben = round((ai.konto(adr) or {}).get("credit", 0.0), 2)
    except Exception:
        guthaben = None
    return {"ok": True, "link": c["url"], "ai_credit_cap_per_day_usd": c["day_cap"], "credit_usd": guthaben,
            "price_per_crypto_call_usd": _f("MCP_TOOL_PRICE", 0.02),
            "how_to_answer": "Send the link on its own line and say it works like a password: only paste it into their own "
                             "ChatGPT or Claude, never share it. A new link turns the old one off, 'disconnect' turns it off. "
                             "Then the steps, short. Claude: Settings, Connectors, Add custom connector, paste the link. "
                             "ChatGPT: Settings, Apps and Connectors, Advanced, turn on Developer mode, then Create, paste the link, "
                             "Authentication: No authentication. Then they just ask there, for example 'scan $SIL' or 'is this "
                             "approval safe?'. Holding $ODDS unlocks it, each call is paid from their Your Engine credit "
                             "(crypto tools ${:.2f} per call, AI models at the normal price). If credit is low, tell them to "
                             "top up with /balance.".format(_f("MCP_TOOL_PRICE", 0.02)),
            "terms": NUTZUNG}


KRYPTO = {"scan_token", "trace_wallet", "money_out", "drain_report", "check_before_sign", "wallet_approvals"}


def krypto(name, args, wer):
    """Die Krypto-Werkzeuge von Your Engine fuer Agenten. Lesen nur, bewegen nichts."""
    if name == "scan_token":
        import scan
        r = scan.analyse(str(args.get("token") or ""))
        if not r.get("ok"):
            return {"error": r.get("error") or "not found", "candidates": r.get("candidates")}
        return scan.fuer_modell(r)
    if name == "trace_wallet":
        import spur
        return spur.wallet_profil(args.get("address"), args.get("chain"))
    if name == "money_out":
        import fluss
        return fluss.folgen(args.get("address"), args.get("chain"))
    if name == "drain_report":
        import fluss
        return fluss.report_fuer_tx(args.get("hash") or args.get("wallet"), args.get("wallet"), args.get("chain"))
    if name == "check_before_sign":
        import sauber
        return sauber.pruefen(wer[0] if wer else None, args.get("chain"), args.get("raw"), args.get("method"), args.get("spender"),
                              args.get("token"), args.get("amount"), args.get("site"))
    if name == "wallet_approvals":
        import wache
        return wache.pruefen(wer[0] if wer else None, args.get("wallet"))
    return {"error": "unknown tool"}


def _werkzeugliste():
    """Mit Hinweisen fuer Clients (seit 2.82.30): welche Werkzeuge nur lesen."""
    out = []
    for w in WERKZEUGE:
        w = dict(w)
        w["annotations"] = {"readOnlyHint": w["name"] in NUR_LESEN, "destructiveHint": False, "openWorldHint": True}
        out.append(w)
    return out


def mcp(nachricht, wer):
    """Eine JSON-RPC-Nachricht beantworten. None bei Notifications."""
    mid = nachricht.get("id")
    meth = nachricht.get("method")
    if mid is None:
        return None
    def ok(r):
        return {"jsonrpc": "2.0", "id": mid, "result": r}
    def fehler(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if not isinstance(meth, str) or not meth:
        return fehler(-32600, "invalid request")
    if meth == "initialize":
        p = nachricht.get("params")
        v = p.get("protocolVersion") if isinstance(p, dict) else None
        if v not in PROTOKOLLE:
            v = PROTOKOLLE[0]
        return ok({"protocolVersion": v, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "oddsengine-ai", "version": VERSION},
                   "instructions": "Every major AI model behind one key, plus the crypto tools of Your Engine. "
                                   "Use list_models to find a model, estimate_cost before big requests, chat or compare to run. "
                                   "For crypto: scan_token before buying, check_before_sign before any signature, "
                                   "wallet_approvals to find risky approvals, trace_wallet and money_out to follow funds, "
                                   "drain_report after a drain. The crypto tools only read the chain, they never move funds."})
    if meth == "ping":
        return ok({})
    if meth == "tools/list":
        return ok({"tools": _werkzeugliste()})
    if meth == "tools/call":
        p = nachricht.get("params")
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p.get("name"):
            return fehler(-32602, "invalid params: name is required")
        if p.get("arguments") is not None and not isinstance(p.get("arguments"), dict):
            return fehler(-32602, "invalid params: arguments must be an object")
        try:
            return ok(werkzeug(p.get("name"), p.get("arguments"), wer))
        except Exception as e:
            # Seit 2.82.30: der Text des Fehlers bleibt im Log. Nach draussen geht nur eine Kennung.
            kennung = "%08x" % (int(time.time() * 1000) & 0xffffffff)
            log.warning(f"mcp tool {p.get('name')} [{kennung}]: {e}")
            return ok(_text(f"The tool failed on our side. Try again in a moment. Reference {kennung}.", True))
    return fehler(-32601, "method not found")
