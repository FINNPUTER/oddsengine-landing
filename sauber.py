"""
sauber.py: "Clean my wallet" und "Before you sign" (seit 2.59). Solana und EVM. Non-custodial.

Clean my wallet
    Solana: leere Token-Konten schliessen (die Miete, etwa 0.002 SOL je Konto, kommt zurueck), Wrapped SOL auspacken,
            offene Delegates entfernen und auf Wunsch Staub verbrennen (nur Token MIT Preis und unter CLEAN_DUST_USD).
            Token ohne Preis werden nie verbrannt: das koennen frische Coins auf einer Launch-Kurve sein.
    EVM:    riskante Freigaben widerrufen (wache.pruefen + handel.widerruf_alle). Spam-Token lassen sich auf EVM nicht
            loeschen, nur in der Wallet ausblenden. Wichtig: nie mit ihnen handeln, ihre Links sind die Falle.
    Ablauf wie beim Handel: Auftrag kv trade_<id> (kind "clean"), Knopf auf engine.oddsengine.live/#trade=<id>,
    die Wallet des Nutzers unterschreibt selbst. Your Engine haelt keinen Schluessel.

Before you sign
    pruefen() liest, was eine Unterschrift wirklich tut: EVM-Calldata (approve, setApprovalForAll, permit, Permit2,
    transfer, ...), signTypedData-JSON (Permit, Permit2, Seaport-Listings), eth_sign-Hashes, Login-Texte und
    Solana-Transaktionen (base64 oder base58, legacy und v0 mit Lookup-Tabellen). Dazu Spender-Einstufung
    (wache.einstufen, kartei.drainer) und Domain-Pruefung (schild). Ergebnis: STOP, CAREFUL oder LOOKS NORMAL.
    Braucht keine Unterschrift und keine Verbindung zur Wallet.
"""
import base64
import json
import os
import re
import secrets
import time

import requests

from logger import log

SEITE = os.getenv("ENGINE_PUBLIC_URL", "https://engine.oddsengine.live").rstrip("/")
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
TX_MAX = 1232
GUELTIG = 7 * 86400


def _f(k, d):
    try:
        return float(os.getenv(k, "") or d)
    except Exception:
        return d


# ── Base58 und kleine Helfer ─────────────────────────────────────────────

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58d(s):
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    roh = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + roh


def b58e(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + s


def _cu16(n):
    out = b""
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _cu16_lesen(d, i):
    n, s = 0, 0
    while True:
        b = d[i]
        i += 1
        n |= (b & 0x7f) << s
        s += 7
        if not b & 0x80:
            return n, i


def _kurz(a):
    a = str(a or "")
    return a[:6] + "…" + a[-4:] if len(a) > 12 else a


# ── Solana: Transaktion bauen (legacy, eine Unterschrift) ────────────────

def sol_nachricht(owner, ixs, blockhash):
    """ixs: [(programm, [(konto, schreibbar)], daten)]. owner zahlt und unterschreibt. -> Nachricht als Bytes."""
    schreib, lesen = [], []
    for prog, konten, _ in ixs:
        for k, w in konten:
            if k == owner:
                continue
            if w:
                if k in lesen:
                    lesen.remove(k)
                if k not in schreib:
                    schreib.append(k)
            elif k not in schreib and k not in lesen:
                lesen.append(k)
    progs = []
    for prog, _, _ in ixs:
        if prog not in progs and prog not in schreib and prog not in lesen:
            progs.append(prog)
    lesen = [k for k in lesen if k not in progs] + progs
    keys = [owner] + schreib + lesen
    idx = {k: i for i, k in enumerate(keys)}
    roh = [b58d(k) for k in keys]
    if any(len(k) != 32 for k in roh):
        raise ValueError("bad address")
    m = bytes([1, 0, len(lesen)]) + _cu16(len(keys)) + b"".join(roh) + b58d(blockhash) + _cu16(len(ixs))
    for prog, konten, daten in ixs:
        m += bytes([idx[prog]]) + _cu16(len(konten)) + bytes(idx[k] for k, _ in konten) + _cu16(len(daten)) + daten
    return m


def sol_tx(nachricht):
    return bytes([1]) + b"\x00" * 64 + nachricht


def _ix_schliessen(prog, konto, owner):
    return (prog, [(konto, True), (owner, True), (owner, False)], bytes([9]))


def _ix_verbrennen(prog, konto, mint, owner, menge):
    return (prog, [(konto, True), (mint, True), (owner, False)], bytes([8]) + int(menge).to_bytes(8, "little"))


def _ix_widerruf(prog, konto, owner):
    return (prog, [(konto, True), (owner, False)], bytes([5]))


def posten_ixs(p, owner):
    """Ein Posten -> seine Anweisungen. art: close | unwrap | burn | revoke"""
    if p["art"] == "revoke":
        return [_ix_widerruf(p["prog"], p["konto"], owner)]
    out = []
    if p["art"] == "burn" and int(p.get("raw") or 0) > 0:
        out.append(_ix_verbrennen(p["prog"], p["konto"], p["mint"], owner, p["raw"]))
    out.append(_ix_schliessen(p["prog"], p["konto"], owner))
    return out


def sol_buendeln(owner, posten, blockhash):
    """Posten in moeglichst wenige Transaktionen unter 1232 Bytes packen. -> [base64]"""
    txs, akt = [], []
    for p in posten:
        probe = akt + posten_ixs(p, owner)
        if akt and len(sol_tx(sol_nachricht(owner, probe, blockhash))) > TX_MAX:
            txs.append(akt)
            akt = posten_ixs(p, owner)
        else:
            akt = probe
    if akt:
        txs.append(akt)
    return [base64.b64encode(sol_tx(sol_nachricht(owner, t, blockhash))).decode() for t in txs]


# ── Solana: Wallet durchsehen ────────────────────────────────────────────

def _sol_rpc(m, p):
    import holder
    return holder._sol_rpc(m, p)


def sol_konten(w):
    out = []
    for prog in (TOKEN, TOKEN22):
        r = _sol_rpc("getTokenAccountsByOwner", [w, {"programId": prog}, {"encoding": "jsonParsed"}]) or {}
        for x in r.get("value") or []:
            try:
                info = x["account"]["data"]["parsed"]["info"]
            except Exception:
                continue
            ta = info.get("tokenAmount") or {}
            gesperrt = 0
            for e in info.get("extensions") or []:
                if e.get("extension") == "transferFeeAmount":
                    gesperrt = int(((e.get("state") or {}).get("withheldAmount")) or 0)
            out.append({"konto": x["pubkey"], "prog": prog, "mint": info.get("mint"), "raw": int(ta.get("amount") or 0),
                        "dez": int(ta.get("decimals") or 0), "ui": float(ta.get("uiAmount") or 0), "state": info.get("state"),
                        "delegate": info.get("delegate"), "close_auth": info.get("closeAuthority"),
                        "lamports": int(x["account"].get("lamports") or 0), "withheld": gesperrt,
                        "native": bool(info.get("isNative"))})
    return out


def sol_preise(mints):
    """USD-Preise ueber Jupiter (Paket zu 50). Fehlende = kein Preis."""
    import handel
    out = {}
    mints = list(dict.fromkeys(m for m in mints if m))
    for i in range(0, len(mints), 50):
        try:
            d = requests.get(handel._jup() + "/price/v3", params={"ids": ",".join(mints[i:i + 50])}, headers=handel._jh(), timeout=12).json()
            for m, v in (d or {}).items():
                if isinstance(v, dict) and v.get("usdPrice") is not None:
                    out[m] = float(v["usdPrice"])
        except Exception as e:
            log.debug(f"sauber preise: {e}")
    return out


def sol_plan(w, staub=False, grenze=None):
    """-> dict mit posten (was passiert) und behalten (was bleibt). Liest nur."""
    grenze = _f("CLEAN_DUST_USD", 1.0) if grenze is None else float(grenze)
    konten = sol_konten(w)
    preise = sol_preise([k["mint"] for k in konten if k["raw"] > 0 and k["mint"] != WSOL]) if konten else {}
    posten, behalten = [], []
    for k in konten:
        if k["state"] == "frozen":
            behalten.append(dict(k, warum="frozen by the token issuer, cannot be closed"))
            continue
        darf_schliessen = (not k["close_auth"] or k["close_auth"] == w) and k["withheld"] == 0
        if k["mint"] == WSOL or k["native"]:
            if darf_schliessen:
                posten.append(dict(k, art="unwrap"))
            continue
        if k["raw"] == 0:
            if darf_schliessen:
                posten.append(dict(k, art="close"))
            elif k["delegate"]:
                posten.append(dict(k, art="revoke"))
            continue
        nft = k["dez"] == 0 and k["raw"] == 1
        preis = preise.get(k["mint"])
        wert = preis * k["ui"] if preis is not None else None
        if staub and darf_schliessen and not nft and wert is not None and wert < grenze:
            posten.append(dict(k, art="burn", usd=round(wert, 4)))
            continue
        if k["delegate"]:
            posten.append(dict(k, art="revoke", usd=round(wert, 2) if wert is not None else None))
        behalten.append(dict(k, usd=round(wert, 2) if wert is not None else None,
                             warum=("NFT" if nft else "no price, may be a new coin" if wert is None else
                                    "worth more than the dust limit" if wert >= grenze else "dust, burn not chosen")))
    miete = sum(p["lamports"] for p in posten if p["art"] in ("close", "burn", "unwrap"))
    wsol = sum(p["raw"] for p in posten if p["art"] == "unwrap")
    return {"posten": posten, "behalten": behalten, "rent_lamports": miete, "wsol_lamports": wsol, "grenze": grenze}


def _anzahl(posten, art):
    return sum(1 for p in posten if p["art"] == art)


def sol_zusammenfassung(plan):
    p = plan["posten"]
    teile = []
    if _anzahl(p, "close"):
        teile.append(f"close {_anzahl(p, 'close')} empty token accounts")
    if _anzahl(p, "burn"):
        teile.append(f"burn {_anzahl(p, 'burn')} dust tokens under ${plan['grenze']:g}")
    if _anzahl(p, "unwrap"):
        teile.append(f"unwrap {plan['wsol_lamports'] / 1e9:.4f} wrapped SOL")
    if _anzahl(p, "revoke"):
        teile.append(f"remove {_anzahl(p, 'revoke')} delegates")
    zurueck = (plan["rent_lamports"] + plan["wsol_lamports"]) / 1e9
    return ", ".join(teile), zurueck


# ── Clean my wallet ──────────────────────────────────────────────────────

def _eigene(adr):
    import motor
    p = motor.zustand(adr)["prefs"]
    evm = [w.lower() for w in motor._wallets(adr) if str(w).startswith("0x")] + \
          [w.lower() for w in (p.get("guard_wallets") or []) if str(w).startswith("0x")]
    sol = ([p["sol_wallet"]] if p.get("sol_wallet") else []) + [w for w in (p.get("guard_wallets") or []) if not str(w).startswith("0x")]
    return list(dict.fromkeys(evm)), list(dict.fromkeys(sol))


def aufraeumen(adr, wallet=None, staub=False):
    """Clean my wallet. -> Ergebnis fuer das Modell, mit links (Knoepfe) zum Unterschreiben."""
    import storage
    wallet = str(wallet or "").strip()
    if wallet:
        evm, sol = ([wallet.lower()], []) if wallet.startswith("0x") else ([], [wallet])
    else:
        evm, sol = _eigene(adr)
    if not evm and not sol:
        return {"ok": False, "error": "No wallet to clean. Send me the address (0x or Solana)."}
    out = {"ok": True, "solana": [], "evm": [], "links": []}
    for w in sol[:3]:
        try:
            plan = sol_plan(w, staub)
        except Exception as e:
            out["solana"].append({"wallet": _kurz(w), "error": f"could not read the wallet: {str(e)[:80]}"})
            continue
        was, zurueck = sol_zusammenfassung(plan)
        eintrag = {"wallet": _kurz(w), "will_do": was or "nothing to clean", "sol_back": round(zurueck, 4),
                   "kept": [{"token": (k["mint"] or "")[:4] + "…", "amount": k["ui"], "usd": k.get("usd"), "why": k["warum"]}
                            for k in plan["behalten"][:8]],
                   "kept_count": len(plan["behalten"])}
        if not staub:
            dust = [k for k in plan["behalten"] if k.get("warum") == "dust, burn not chosen"]
            if dust:
                eintrag["dust_available"] = f"{len(dust)} tokens worth under ${plan['grenze']:g} could be burned too (ask first)"
        if plan["posten"]:
            tid = secrets.token_hex(8)
            a = {"id": tid, "kind": "clean", "adr": adr, "chain": "solana", "wallet": w, "staub": bool(staub),
                 "posten": [{k_: p[k_] for k_ in ("konto", "prog", "mint", "raw", "art", "lamports")} for p in plan["posten"]],
                 "side": "clean", "symbol": "", "text_in": was, "sol_back": zurueck, "ts": int(time.time()), "status": "pending"}
            storage.kv_set("trade_" + tid, json.dumps(a))
            eintrag["sign_link"] = f"{SEITE}/#trade={tid}"
            out["links"].append((f"🧹 Clean {_kurz(w)} (+{zurueck:.3f} SOL)", eintrag["sign_link"]))
        out["solana"].append(eintrag)
    if evm:
        try:
            import wache
            for w in evm[:3]:
                r = wache.pruefen(adr, w)
                riskant = [x for x in (r.get("approvals") or []) if x.get("risk") != "ok"]
                e = {"wallet": _kurz(w), "risky_approvals": len(riskant),
                     "approvals": [{k: x.get(k) for k in ("chain", "token", "spender", "spender_name", "risk", "why")} for x in riskant[:8]]}
                for g in r.get("revoke_all") or []:
                    out["links"].append((f"🧹 Revoke all {g['count']} risky ({g['chain']})", g["link"]))
                if not r.get("revoke_all"):
                    for x in riskant[:3]:
                        if x.get("revoke_link"):
                            out["links"].append((f"Revoke {x['token']}"[:30], x["revoke_link"]))
                out["evm"].append(e)
        except Exception as e:
            log.debug(f"sauber evm: {e}")
        out["evm_spam"] = ("Spam tokens on EVM cannot be deleted from the chain. Hide them in the wallet app. Never swap, send "
                           "or approve them and never open the website in their name: that is how the trap works.")
    out["how_to_answer"] = ("Say per wallet what the clean does and how much SOL comes back, then that the button opens their own "
                            "wallet (Phantom, Solflare, MetaMask, Rabby) to sign, Your Engine never holds keys. Mention kept tokens "
                            "only briefly. Tokens without a price are never burned. Burning is final, only offer it when asked. "
                            "If a wallet is a Torch wallet: it can only sign inside Torch, so this button cannot sign for it.")
    return out


def seite(a):
    """Bestaetigungsseite fuer kind clean (Solana)."""
    p = a.get("posten") or []
    return {"ok": True, "id": a["id"], "kind": "clean", "chain": "solana", "chain_name": "Solana", "side": "clean",
            "symbol": "", "token": "", "you_pay": a.get("text_in") or "Clean wallet",
            "you_get": f"+{float(a.get('sol_back') or 0):.4f} SOL back", "count": len(p),
            "needs_wallet": True, "known_wallet": a.get("wallet"), "explorer": "https://solscan.io"}


def bauen(a, pubkey):
    """Frisch bauen: nur Posten, die es noch gibt. Verbrannt wird hoechstens, was beim Plan da war."""
    pk = str(pubkey or "").strip()
    if pk != a.get("wallet"):
        w = a.get("wallet") or ""
        return {"ok": False, "error": f"Connect the wallet {w[:4]}…{w[-4:]}, that is the one to clean."}
    jetzt = {k["konto"]: k for k in sol_konten(pk)}
    posten = []
    for p in a.get("posten") or []:
        k = jetzt.get(p["konto"])
        if not k or k["state"] == "frozen":
            continue
        if p["art"] == "close" and k["raw"] != 0:
            continue
        if p["art"] == "burn":
            if k["raw"] > int(p["raw"]):
                continue  # inzwischen mehr bekommen: nichts verbrennen, was beim Plan nicht da war
            p = dict(p, raw=k["raw"])
        if p["art"] == "revoke" and not k["delegate"]:
            continue
        posten.append(p)
    if not posten:
        return {"ok": False, "error": "Nothing left to clean, the wallet is already clean."}
    bh = ((_sol_rpc("getLatestBlockhash", [{"commitment": "finalized"}]) or {}).get("value") or {}).get("blockhash")
    if not bh:
        return {"ok": False, "error": "Solana did not answer. Try again in a moment."}
    txs = sol_buendeln(pk, posten, bh)
    was, zurueck = sol_zusammenfassung({"posten": posten, "rent_lamports": sum(p["lamports"] for p in posten if p["art"] != "revoke"),
                                        "wsol_lamports": sum(p["raw"] for p in posten if p["art"] == "unwrap"),
                                        "grenze": _f("CLEAN_DUST_USD", 1.0)})
    return {"ok": True, "tx": txs[0], "txs": txs, "you_pay": was, "you_get": f"+{zurueck:.4f} SOL back", "warnings": [],
            "rpc": os.getenv("SOLANA_PUBLIC_RPC", "").strip() or "https://api.mainnet-beta.solana.com"}


# ── Before you sign ──────────────────────────────────────────────────────

SEL = {
    "095ea7b3": ("approve", "Lets {a} spend your {t}"),
    "39509351": ("increaseAllowance", "Lets {a} spend more of your {t}"),
    "a22cb465": ("setApprovalForAll", "Lets {a} move ALL your NFTs or items of this collection"),
    "a9059cbb": ("transfer", "Sends your {t} to {a}"),
    "23b872dd": ("transferFrom", "Moves tokens from one wallet to {a}"),
    "42842e0e": ("safeTransferFrom", "Sends an NFT to {a}"),
    "d505accf": ("permit", "Signs a spending permission for {a}"),
    "87517c45": ("permit2.approve", "Lets {a} spend your {t} through Permit2"),
    "2b67b570": ("permit2.permit", "Signs a Permit2 spending permission"),
    "2a2d80d1": ("permit2.permitBatch", "Signs a Permit2 permission for several tokens at once"),
    "3593564c": ("execute", "A swap through the Uniswap Universal Router"),
    "24856bc3": ("execute", "A swap through the Uniswap Universal Router"),
    "ac9650d8": ("multicall", "Several actions in one transaction"),
    "5ae401dc": ("multicall", "Several actions in one transaction"),
    "1fff991f": ("swap", "A swap"),
    "12aa3caf": ("swap", "A 1inch swap"),
    "e9e05c42": ("depositTransaction", "A bridge deposit"),
    "d0e30db0": ("deposit", "Wraps ETH"),
    "2e1a7d4d": ("withdraw", "Unwraps WETH"),
}
UNBEGRENZT = 2 ** 200
DOMAINS = ["uniswap.org", "jup.ag", "raydium.io", "pump.fun", "torchpad.fun", "opensea.io", "metamask.io", "phantom.app",
           "oddsengine.live", "1inch.io", "pancakeswap.finance", "aave.com", "lido.fi", "blur.io", "magiceden.io",
           "relay.link", "orca.so", "meteora.ag", "arbitrum.io", "base.org", "robinhood.com", "coinbase.com", "binance.com",
           "solflare.com", "rabby.io", "revoke.cash", "dexscreener.com", "etherscan.io", "solscan.io"]


def _wort(hexs, i):
    return hexs[i * 64:(i + 1) * 64]


def _adr(w):
    return "0x" + w[-40:]


def _int(w):
    try:
        return int(w, 16)
    except Exception:
        return 0


def _menge(n):
    if n >= UNBEGRENZT:
        return "UNLIMITED"
    return f"{n:,}" if n < 10 ** 6 else f"{n:.3e} (raw units)"


def evm_calldata(data, ziel=None):
    """-> (methode, beschreibung, spender, unbegrenzt, menge)"""
    d = data[2:] if data.startswith("0x") else data
    sel, rest = d[:8].lower(), d[8:]
    if sel not in SEL:
        return ("unknown", f"An unknown contract call ({sel}) to {_kurz(ziel) if ziel else 'a contract'}", ziel, False, None)
    name, txt = SEL[sel]
    sp, n = None, None
    if sel in ("095ea7b3", "39509351", "a9059cbb"):
        sp, n = _adr(_wort(rest, 0)), _int(_wort(rest, 1))
    elif sel == "a22cb465":
        sp, n = _adr(_wort(rest, 0)), UNBEGRENZT if _int(_wort(rest, 1)) else 0
        if not n:
            return ("setApprovalForAll(false)", f"Removes the approval of {_kurz(sp)}", sp, False, 0)
    elif sel in ("23b872dd", "42842e0e"):
        sp, n = _adr(_wort(rest, 1)), _int(_wort(rest, 2))
    elif sel == "d505accf":
        sp, n = _adr(_wort(rest, 1)), _int(_wort(rest, 2))
    elif sel == "87517c45":
        sp, n = _adr(_wort(rest, 1)), _int(_wort(rest, 2))
    if name == "approve" and n == 0:
        return ("approve(0)", f"Removes the approval of {_kurz(sp)}", sp, False, 0)
    return (name, txt.format(a=_kurz(sp) if sp else "a contract", t="tokens"), sp, bool(n and n >= UNBEGRENZT), n)


def _zahl(x):
    try:
        x = str(x if x is not None else "0").strip()
        return int(x, 16) if x.lower().startswith("0x") else int(x)
    except Exception:
        return 0


def typed_data(j):
    """signTypedData v4. -> (methode, beschreibung, spender, unbegrenzt, rote_flagge)"""
    typ = str(j.get("primaryType") or "")
    m = j.get("message") or {}
    dom = j.get("domain") or {}
    if typ in ("OrderComponents", "Order", "BulkOrder") or "seaport" in str(dom.get("name", "")).lower():
        anbieter = str(m.get("offerer") or "").lower()
        empf = {str(c.get("recipient") or "").lower() for c in (m.get("consideration") or []) if isinstance(c, dict)}
        fremd = [e for e in empf if e and e != anbieter]
        wert_an_dich = sum(_zahl(c.get("startAmount")) for c in (m.get("consideration") or [])
                           if isinstance(c, dict) and str(c.get("recipient") or "").lower() == anbieter) if anbieter else 0
        rot = bool(fremd) and wert_an_dich == 0
        return ("Seaport listing", "A marketplace listing that sells your NFTs or tokens" +
                (", and you get NOTHING back, everything goes to another wallet" if rot else ""), (fremd or [None])[0], rot, rot)
    if typ in ("PermitSingle", "PermitBatch", "PermitTransferFrom", "PermitBatchTransferFrom"):
        sp = m.get("spender")
        det = m.get("details") or m.get("permitted") or {}
        det = det[0] if isinstance(det, list) and det else det
        n = _zahl((det or {}).get("amount"))
        batch = "Batch" in typ
        return ("Permit2 " + typ, f"Signs a Permit2 permission for {'several tokens' if batch else 'a token'} to {_kurz(sp)}. "
                "No gas, no transaction, but it lets them take the tokens", sp, n >= UNBEGRENZT or batch, False)
    if typ == "Permit":
        sp = m.get("spender")
        n = _zahl(m.get("value"))
        return ("Permit", f"Signs a spending permission for {dom.get('name') or 'a token'} to {_kurz(sp)}. No gas, no "
                "transaction, but it lets them take the tokens", sp, n >= UNBEGRENZT, False)
    return (typ or "typed data", f"A signed message of type {typ or 'unknown'} for {dom.get('name') or 'a website'}", None, False, False)


SOL_PROGS = {
    SYSTEM: "System", TOKEN: "Token", TOKEN22: "Token-2022", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token",
    "ComputeBudget111111111111111111111111111111": "Compute budget", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUJoi5QNyVTaV4": "Jupiter",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo": "Memo",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium", "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM", "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca", "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora", "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC", "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95": "Lighthouse",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s": "Metaplex", "BGUMAp9Gq7iTEuizy4pqaxsTyUCBK68MDfK752saRPUY": "Bubblegum",
}


def _nachricht(d, i):
    version = None
    if d[i] & 0x80:
        version = d[i] & 0x7f
        i += 1
    n_sig = d[i]
    i += 3
    nk, i = _cu16_lesen(d, i)
    keys = [b58e(d[i + 32 * x:i + 32 * (x + 1)]) for x in range(nk)]
    i += 32 * nk + 32
    ni, i = _cu16_lesen(d, i)
    ixs = []
    for _ in range(ni):
        p = d[i]
        i += 1
        na, i = _cu16_lesen(d, i)
        acc = list(d[i:i + na])
        i += na
        nd, i = _cu16_lesen(d, i)
        ixs.append((p, acc, d[i:i + nd]))
        i += nd
    alts = []
    if version is not None:
        na, i = _cu16_lesen(d, i)
        for _ in range(na):
            k = b58e(d[i:i + 32])
            i += 32
            nw, i = _cu16_lesen(d, i)
            w = list(d[i:i + nw])
            i += nw
            nr, i = _cu16_lesen(d, i)
            r = list(d[i:i + nr])
            i += nr
            alts.append((k, w, r))
    if i != len(d) or not keys or any(p >= len(keys) + 256 for p, _, _ in ixs):
        raise ValueError("not a message")
    return {"keys": keys, "ixs": ixs, "signer": keys[:n_sig], "alts": alts, "version": version, "n_sig": n_sig}


def _sol_lesen(d):
    """Ganze Transaktion (mit Unterschriften) oder nur die Nachricht."""
    try:
        n, j = _cu16_lesen(d, 0)
        if 1 <= n <= 8:
            t = _nachricht(d, j + 64 * n)
            if t["n_sig"] == n:
                return t
    except Exception:
        pass
    return _nachricht(d, 0)


def _alts_aufloesen(alts):
    schreib, lesen = [], []
    for k, w, r in alts:
        try:
            v = ((_sol_rpc("getAccountInfo", [k, {"encoding": "jsonParsed"}]) or {}).get("value") or {})
            adrs = (((v.get("data") or {}).get("parsed") or {}).get("info") or {}).get("addresses") or []
        except Exception:
            adrs = []
        schreib += [adrs[x] if x < len(adrs) else None for x in w]
        lesen += [adrs[x] if x < len(adrs) else None for x in r]
    return schreib + lesen


def sol_lesbar(roh_text):
    s = str(roh_text or "").strip()
    for dek in (lambda x: base64.b64decode(x, validate=True), b58d):
        try:
            b = dek(s)
            if len(b) > 64:
                t = _sol_lesen(b)
                if t["keys"]:
                    return t
        except Exception:
            continue
    return None


def sol_pruefen(roh_text, wallet=None):
    """-> (schritte, flaggen[(stufe, grund)], unbekannte_programme)"""
    t = sol_lesbar(roh_text)
    if not t:
        return None
    keys = t["keys"] + (_alts_aufloesen(t["alts"]) if t["alts"] else [])
    ich = wallet or (t["signer"][0] if t["signer"] else None)
    k = lambda x: keys[x] if x < len(keys) and keys[x] else "?"
    schritte, flaggen, fremd, abfluss = [], [], [], 0
    for p_i, acc, data in t["ixs"]:
        prog = k(p_i)
        name = SOL_PROGS.get(prog)
        op = data[0] if data else None
        if prog == SYSTEM and len(data) >= 4:
            op = int.from_bytes(data[:4], "little")
            if op == 2 and len(data) >= 12:
                lam = int.from_bytes(data[4:12], "little")
                schritte.append(f"Sends {lam / 1e9:.4f} SOL to {_kurz(k(acc[1]))}")
                if k(acc[0]) == ich:
                    abfluss += 1
                if lam >= 0.5e9:
                    flaggen.append(("careful", f"sends {lam / 1e9:.2f} SOL out of your wallet"))
            elif op == 1:
                schritte.append(f"Hands your wallet account to the program {_kurz(data[4:36] and b58e(data[4:36]))}")
                flaggen.append(("stop", "it changes the OWNER of your wallet account. After that the wallet is not yours anymore"))
            elif op == 0:
                schritte.append("Creates a new account")
            else:
                schritte.append(f"System instruction {op}")
            continue
        if prog in (TOKEN, TOKEN22):
            if op in (3, 12):
                n = int.from_bytes(data[1:9], "little") if len(data) >= 9 else 0
                ziel = k(acc[2] if op == 12 else acc[1]) if len(acc) > 2 else "?"
                schritte.append(f"Moves tokens from your token account to {_kurz(ziel)} ({n:,} raw units)")
                abfluss += 1
            elif op in (4, 13):
                n = int.from_bytes(data[1:9], "little") if len(data) >= 9 else 0
                dlg = k(acc[2] if op == 13 else acc[1]) if len(acc) > 2 else "?"
                schritte.append(f"Gives {_kurz(dlg)} the right to move your tokens ({'UNLIMITED' if n >= 2 ** 63 else f'{n:,} raw units'})")
                flaggen.append(("stop" if n >= 2 ** 63 else "careful", f"it lets {_kurz(dlg)} move your tokens later, without asking you again"))
            elif op == 6:
                art = data[1] if len(data) > 1 else None
                schritte.append("Changes who controls your token account")
                if art in (2, 3):
                    flaggen.append(("stop", "it hands control of your token account to someone else"))
                else:
                    flaggen.append(("careful", "it changes an authority (normal only when you are the token creator)"))
            elif op == 9:
                dest = k(acc[1]) if len(acc) > 1 else "?"
                schritte.append(f"Closes a token account, the rent goes to {_kurz(dest)}")
                if ich and dest != ich:
                    flaggen.append(("careful", "closes your token account and sends the rent to another wallet"))
            elif op == 8 or op == 15:
                schritte.append("Burns tokens")
            elif op == 5:
                schritte.append("Removes a delegate (safe)")
            elif op in (1, 18):
                schritte.append("Sets up a token account")
            else:
                schritte.append(f"Token instruction {op}")
            continue
        if name:
            if name not in ("Compute budget", "Associated Token", "Memo"):
                schritte.append(f"Uses {name}")
            continue
        fremd.append(prog)
        schritte.append(f"Calls a program we do not know: {_kurz(prog)}")
    if abfluss >= 4:
        flaggen.append(("stop", f"it moves {abfluss} different assets out in one go, the pattern of a wallet drainer"))
    if fremd:
        flaggen.append(("careful", f"{len(fremd)} unknown program{'s' if len(fremd) > 1 else ''}: check the site before you sign"))
    if t["alts"] and any(x is None for x in keys):
        flaggen.append(("careful", "some accounts could not be read"))
    return {"steps": schritte[:14], "flags": flaggen, "unknown_programs": [_kurz(x) for x in fremd[:5]],
            "signer": _kurz(ich) if ich else None}


def _domain(site):
    s = str(site or "").strip().lower()
    m = re.search(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", s)
    return m.group(1) if m else None


def domain_pruefen(site):
    d = _domain(site)
    if not d:
        return None, None
    haupt = ".".join(d.split(".")[-2:])
    if haupt in DOMAINS:
        return "ok", f"{haupt} is the real site"
    try:
        import schild
        for o in DOMAINS:
            if schild._domain_aehnlich(haupt, o):
                return "stop", f"{d} looks like {o} but is NOT it, a look-alike domain"
        st, grund = schild.scam_text(site)
        if st == "hart":
            return "stop", grund
    except Exception:
        pass
    if re.search(r"(?i)(claim|airdrop|reward|drop|connect|verify|rectify|migrat|unlock|bonus)", d):
        return "careful", f"{d} uses bait words (claim, airdrop, reward) typical for drainer sites"
    return "unknown", f"{d} is not on our list of known sites"


def _spender(chain, sp):
    if not sp:
        return None
    try:
        import wache
        e = wache.einstufen(chain, sp)
    except Exception:
        e = {"level": "high", "name": None, "why": "could not check"}
    try:
        import spur
        n = spur.name(chain, sp)
        if n and not e.get("name"):
            e = dict(e, name=n)
    except Exception:
        pass
    return e


# ── Empfaenger einer Zahlung (seit 2.82.104): wer ist die Adresse, an die gleich Geld geht? ──────────────
# Alles aus eigenen Daten und der Kette, mit Zeitlimit (SIGN_RECIPIENT_SEC, Standard 8 s), damit die Antwort schnell bleibt.
_EMPF_SEK = float(os.getenv("SIGN_RECIPIENT_SEC", "8"))
_FLAG_TEXT = {"drainer": ("stop", "this address already drained another wallet"),
              "drain_dest": ("stop", "stolen funds were sent to this address before"),
              "poison": ("stop", "this address was used for address poisoning (fake look-alike transfers)"),
              "serial_dev": ("careful", "this wallet launched several coins, it is a serial token deployer")}


def _aehnlich(a, erlaubt):
    """Address poisoning: gleiche ersten 4 und letzten 4 Zeichen wie ein erlaubter Empfaenger, aber nicht dieselbe Adresse."""
    a = str(a or "").lower()
    if not a.startswith("0x") or len(a) != 42:
        return None
    for e in erlaubt or ():
        e = str(e).lower()
        if e != a and len(e) == 42 and e[2:6] == a[2:6] and e[-4:] == a[-4:]:
            return e
    return None


def _empfaenger(chain, to):
    """-> {"chain", "kind", "name", "flags": [(stufe, text, beweis)], "first_seen", "outgoing_tx", "funded_by", ...}"""
    import spur
    w = str(to).strip()
    evm = w.startswith("0x") and len(w) == 42
    if not evm and not spur._B58.match(w):
        return {"error": "not a wallet address"}
    if evm:
        w = w.lower()
    out = {"address": w, "flags": []}
    ch = spur._kette_wallet(w, chain)
    out["chain"] = ch
    try:
        nm = spur.name(ch, w)
        out["kind"], out["name"] = nm.get("kind"), nm.get("name")
        for f in nm.get("flags") or []:
            st, txt = _FLAG_TEXT.get(f["flag"], ("careful", f"flagged in OddsEngine records as {f['flag']}"))
            out["flags"].append((st, txt, (f.get("proof") or [None])[0]))
    except Exception:
        pass
    geber = None
    try:
        import kartei
        geber = kartei.geldgeber(ch, w)
    except Exception:
        kartei = None
    if evm and ch in ("robinhood", "base", "ethereum", "arbitrum"):
        try:
            import scan
            out["outgoing_tx"] = int(scan._evm(ch, "eth_getTransactionCount", [w, "latest"]), 16)
            code = scan._evm(ch, "eth_getCode", [w, "latest"])
            out["is_contract"] = bool(code and code not in ("0x", "0x0"))
        except Exception:
            pass
        try:
            erst, erste_ts = spur._erstes_rein_evm(ch, w)
            ts = (erst or {}).get("ts") or erste_ts
            if ts:
                out["first_seen_ts"] = int(ts)
                out["first_seen"] = time.strftime("%Y-%m-%d", time.gmtime(ts))
            if erst and not geber:
                geber = {"from": erst["from"], "chain": ch, "tx": erst.get("tx"), "ts": erst.get("ts")}
        except Exception:
            pass
    if geber and geber.get("from"):
        out["funded_by"] = geber["from"]
        try:
            import kartei as _ka
            for f in _ka.flags(geber.get("chain") or ch, geber["from"]):
                if f["flag"] in ("drainer", "drain_dest", "poison"):
                    out["flags"].append(("stop", f"it was funded by {_kurz(geber['from'])}, a wallet in our records as "
                                                 f"{f['flag'].replace('_', ' ')}", (f.get("proofs") or [None])[0]))
        except Exception:
            pass
    return out


def _empfaenger_mit_limit(chain, to):
    import threading
    box = {}

    def lauf():
        try:
            box["r"] = _empfaenger(chain, to)
        except Exception as e:
            box["r"] = {"error": str(e)[:120]}
    t = threading.Thread(target=lauf, daemon=True, name="sign-recipient")
    t.start()
    t.join(_EMPF_SEK)
    return box.get("r")


_PAYEE_SCHEMA = [False]


def _payee_merken(agent, to):
    """Merkt sich je Agent, an wen er schon gezahlt hat. -> wie oft vorher gesehen (0 = zum ersten Mal)."""
    try:
        import kartei
        if not _PAYEE_SCHEMA[0]:
            kartei._q("""CREATE TABLE IF NOT EXISTS mcp_payees (agent TEXT, payee TEXT, first_ts BIGINT, last_ts BIGINT, n INT,
                         PRIMARY KEY (agent, payee))""")
            _PAYEE_SCHEMA[0] = True
        a, p = str(agent).lower(), (str(to).lower() if str(to).startswith("0x") else str(to))
        r = kartei._q("SELECT n FROM mcp_payees WHERE agent=%s AND payee=%s", (a, p), eins=True)
        jetzt = int(time.time())
        kartei._q("""INSERT INTO mcp_payees (agent, payee, first_ts, last_ts, n) VALUES (%s,%s,%s,%s,1)
                     ON CONFLICT (agent, payee) DO UPDATE SET last_ts=EXCLUDED.last_ts, n=mcp_payees.n+1""", (a, p, jetzt, jetzt))
        return int(r[0]) if r else 0
    except Exception:
        return None


def pruefen(adr=None, chain=None, raw=None, method=None, spender=None, token=None, amount=None, site=None, wallet=None,
            to=None, allow=None, cap_usd=None, memo=None):
    """Before you sign. Nimmt was da ist: rohe Daten aus der Wallet, oder Felder aus einem Screenshot."""
    chain = str(chain or "").lower().strip() or None
    raw = str(raw or "").strip()
    stufe, gruende, schritte = [], [], []
    out = {"ok": True}
    methode, unbegrenzt = (str(method or "").strip() or None), False
    # 1. Rohe Daten deuten
    if raw:
        j = None
        if raw.startswith("{"):
            try:
                j = json.loads(raw)
            except Exception:
                j = None
        if j:
            methode, txt, sp, unbegrenzt, rot = typed_data(j)
            schritte.append(txt)
            spender = spender or sp
            vc = (j.get("domain") or {}).get("verifyingContract")
            cid = (j.get("domain") or {}).get("chainId")
            if cid and not chain:
                chain = {1: "ethereum", 8453: "base", 42161: "arbitrum", 56: "bsc", 4663: "robinhood"}.get(int(str(cid), 0) if isinstance(cid, str) else int(cid))
            if rot:
                stufe.append("stop")
                gruende.append("this listing gives your assets away for nothing")
            if methode.startswith("Permit"):
                stufe.append("careful")
                gruende.append("a gasless permit: it looks like a harmless login message but it moves money rights")
            out["token_contract"] = vc
        elif re.fullmatch(r"0x[0-9a-fA-F]{64}", raw):
            methode = "eth_sign"
            schritte.append("Signs a raw hash. The wallet cannot show what it is")
            stufe.append("stop")
            gruende.append("blind signing a raw hash can authorize ANY transaction, real sites never ask for this")
        elif re.fullmatch(r"0x[0-9a-fA-F]{8,}", raw):
            methode, txt, sp, unbegrenzt, n = evm_calldata(raw, token)
            schritte.append(txt)
            spender = spender or sp
            if n is not None and n < UNBEGRENZT and methode in ("approve", "permit", "permit2.approve"):
                out["amount_raw"] = _menge(n)
        else:
            s = sol_pruefen(raw, wallet)
            if s:
                chain = "solana"
                methode = methode or "Solana transaction"
                schritte += s["steps"]
                for st, g in s["flags"]:
                    stufe.append(st)
                    gruende.append(g)
                out["unknown_programs"] = s["unknown_programs"]
            elif len(raw) < 2000:
                methode = methode or "message"
                schritte.append("Signs a text message")
                if re.search(r"(?i)(sign in|login|log in|nonce|welcome|verify (you|ownership))", raw):
                    gruende.append("a login message: it proves you own the wallet and cannot move money on its own")
                    stufe.append("ok")
                else:
                    stufe.append("careful")
                    gruende.append("a text message we cannot place, only sign if you know why the site needs it")
    # 2. Methode aus dem Screenshot oder Text
    mlow = str(methode or "").lower()
    if not raw and mlow:
        if "approvalforall" in mlow or "approval for all" in mlow:
            schritte.append("Lets the spender move ALL your NFTs or items of this collection")
            stufe.append("careful")
            unbegrenzt = True
        elif "permit" in mlow:
            schritte.append("A gasless permit: lets the spender take your tokens without a transaction")
            stufe.append("careful")
        elif "approve" in mlow or "spending cap" in mlow or "allowance" in mlow:
            schritte.append("Lets the spender move your tokens")
        elif "eth_sign" in mlow or "blind" in mlow:
            stufe.append("stop")
            gruende.append("blind signing can authorize anything")
        elif "assign" in mlow or "owner" in mlow:
            stufe.append("stop")
            gruende.append("it changes the owner of your account")
    if str(amount or "").lower() in ("unlimited", "max", "infinite") or "unlimited" in str(amount or "").lower():
        unbegrenzt = True
    if unbegrenzt:
        gruende.append("the permission is UNLIMITED, it covers everything you hold of it now and later")
    # 3. Spender
    if spender and chain != "solana" and str(spender).startswith("0x"):
        e = _spender(chain or "ethereum", spender)
        if e:
            out["spender"] = {"address": spender, "name": e.get("name"), "risk": e["level"], "why": e["why"]}
            if e["level"] == "critical":
                stufe.append("stop")
                gruende.append(f"the spender is {e['why']}")
            elif e["level"] == "high":
                stufe.append("careful")
                gruende.append(f"the spender is {e['why']}" + (" and the permission is unlimited" if unbegrenzt else ""))
                if unbegrenzt:
                    stufe.append("stop")
            else:
                stufe.append("ok")
    elif spender and chain == "solana":
        try:
            import kartei
            if kartei.drainer("solana", spender):
                stufe.append("stop")
                gruende.append("this address already drained another wallet")
        except Exception:
            pass
    # 4. Website
    if site:
        st, g = domain_pruefen(site)
        if st:
            out["site"] = {"domain": _domain(site), "check": st, "why": g}
            if st in ("stop", "careful"):
                stufe.append(st)
                gruende.append(g)
    # 5. Zahlungs-Richtlinie (seit 2.82.103): fuer Agenten, die aus einer festen Empfaengerliste zahlen.
    #    Der Agent (oder sein Harness) gibt den Empfaenger, seine erlaubte Liste und sein Pro-Zahlung-Limit mit.
    #    Zahlungen an einen nicht gelisteten Empfaenger oder ueber dem Limit werden geflaggt, egal WARUM der Agent
    #    das will. Das faengt den Fall ab, dass ein Agent ueber eine Rechnung oder Nachricht umgelenkt wird.
    def _liste(x):
        if x is None:
            return None
        if isinstance(x, str):
            teile = re.split(r"[\s,;]+", x)
        elif isinstance(x, (list, tuple, set)):
            teile = [str(t) for t in x]
        else:
            teile = [str(x)]
        return {t.strip().lower() for t in teile if t and t.strip()}
    erlaubt = _liste(allow)
    empf = str(to or "").strip()
    if empf or erlaubt is not None or cap_usd is not None:
        out["payment"] = {"to": empf or None, "allowlist_size": (len(erlaubt) if erlaubt is not None else None)}
        if empf and erlaubt is not None:
            if empf.lower() in erlaubt:
                stufe.append("ok")
                schritte.append(f"Pays {_kurz(empf)}, which is on the approved list")
            else:
                stufe.append("stop")
                gruende.append(f"the recipient {_kurz(empf)} is NOT on your approved payee list, a payment here was never "
                               "set up, do not sign")
                out["payment"]["off_allowlist"] = True
        if empf:
            # Look-alike eines erlaubten Empfaengers (Address poisoning)
            zwilling = _aehnlich(empf, erlaubt)
            if zwilling:
                stufe.append("stop")
                gruende.insert(0, f"the recipient {empf} is a LOOK-ALIKE of your approved payee {zwilling}: same "
                                  "first and last characters, different address. This is how address poisoning steals payments")
                out["payment"]["lookalike_of"] = zwilling
            # Wer ist die Adresse? Eigene Aufzeichnungen und die Kette
            info = _empfaenger_mit_limit(chain, empf)
            if info is None:
                out["recipient"] = {"address": empf, "check": "not finished in time, only the list and limit were checked"}
            elif info.get("error"):
                out["recipient"] = {"address": empf, "check": info["error"]}
                stufe.append("careful")
                gruende.append("the recipient is not a valid wallet address")
            else:
                chain = chain or info.get("chain")
                rec = {k: info.get(k) for k in ("address", "chain", "kind", "name", "first_seen", "outgoing_tx", "is_contract",
                                                 "funded_by") if info.get(k) is not None}
                rec["flags"] = [{"level": st, "why": txt, "proof": bw} for st, txt, bw in info["flags"]]
                for st, txt, bw in info["flags"]:
                    stufe.append(st)
                    gruende.append(txt + (f" (proof: {bw})" if bw else ""))
                alter_t = (time.time() - info["first_seen_ts"]) / 86400 if info.get("first_seen_ts") else None
                aus = info.get("outgoing_tx")
                bekannt = erlaubt is not None and empf.lower() in erlaubt
                if not bekannt and info.get("kind") in (None, "wallet") and not info.get("is_contract"):
                    if alter_t is None and aus == 0:
                        stufe.append("careful")
                        gruende.append(f"the recipient has no history on {info.get('chain')}: never funded, never sent anything")
                        rec["fresh"] = True
                    elif alter_t is not None and alter_t < 7 and (aus or 0) == 0:
                        stufe.append("careful")
                        gruende.append(f"the recipient is brand new: first funded {max(0, int(alter_t))} day(s) ago and never "
                                       "sent a transaction")
                        rec["fresh"] = True
                if info.get("name"):
                    schritte.append(f"Recipient: {info['name']} ({info.get('kind')})")
                out["recipient"] = rec
            if adr:
                vorher = _payee_merken(adr, empf)
                if vorher is not None:
                    out["payment"]["seen_before"] = vorher
                    if vorher == 0:
                        schritte.append("First time this agent checks a payment to this address")
        try:
            betr = _zahl(amount) if amount is not None else None
        except Exception:
            betr = None
        try:
            limit = float(cap_usd) if cap_usd is not None else None
        except Exception:
            limit = None
        if betr is not None and limit is not None and betr > limit:
            stufe.append("stop")
            gruende.append(f"the amount ({betr:g}) is over your per-payment limit of {limit:g}, do not sign")
            out["payment"]["over_cap"] = True
        if memo and empf and str(memo):
            # Hinweis, wenn im Memo eine andere Adresse steht als der echte Empfaenger (Umlenk-Versuch)
            import re as _re
            adrs = {a.lower() for a in _re.findall(r"0x[0-9a-fA-F]{40}", str(memo))}
            if adrs and empf.lower() not in adrs:
                stufe.append("careful")
                gruende.append("the note names a different address than the one being paid, check which one is real")

    if not raw and not methode and not spender and not site and not (to or (allow is not None) or (cap_usd is not None)):
        return {"ok": False, "error": "Send me what the wallet shows: a screenshot of the signing window, the website, the "
                                      "spender address, or the raw data (Show data / Hex in MetaMask, the transaction in Phantom)."}
    urteil = "stop" if "stop" in stufe else "careful" if ("careful" in stufe or unbegrenzt) else "looks_normal"
    out.update(verdict={"stop": "🔴 DO NOT SIGN", "careful": "🟠 CAREFUL", "looks_normal": "🟢 LOOKS NORMAL"}[urteil],
               chain=chain, method=methode, what_it_does=schritte[:12], reasons=list(dict.fromkeys(gruende))[:8])
    out["how_to_answer"] = (
        "Start with the verdict line. Then in plain words what signing does (what_it_does), then the reasons. If DO NOT SIGN: "
        "tell them to reject in the wallet and close the site; if they already signed, offer the wallet check to revoke at once. "
        "If CAREFUL: say exactly what to check (real domain, limited amount instead of unlimited). Never ask for a seed phrase. "
        "LOOKS NORMAL is not a guarantee, say that in one short line. If 'payment' or 'recipient' is there, this is a payment "
        "by an agent: say who the recipient is from 'recipient' (name, kind, age, flags with proof), whether it is on the "
        "approved list and inside the limit. An agent must not sign on DO NOT SIGN and should ask its owner on CAREFUL.")
    return out
