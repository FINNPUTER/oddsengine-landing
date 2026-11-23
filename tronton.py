"""Seit 2.82.32: TRON und TON.

Was hier liegt: Adressen erkennen, Jetton-Daten (TON), Wallet-Bestaende fuer das Portfolio (TRON, TON).
Marktdaten kommen wie bei allen Chains von DexScreener (scan.py), Vertrags-Flags fuer TRON von GoPlus.
Kein Tiefen-Scan (Buendel, Gruppen) und kein Handel auf diesen Chains.

Quellen: TronGrid (api.trongrid.io, optional TRONGRID_KEY), tonapi.io (optional TONAPI_KEY).
"""
import os
import re
import time

import requests

from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
B58 = "1-9A-HJ-NP-Za-km-z"
TRON_RE = re.compile(rf"(?<![{B58}])T[{B58}]{{33}}(?![{B58}])")
TON_RE = re.compile(r"(?<![A-Za-z0-9_\-])(?:EQ|UQ)[A-Za-z0-9_\-]{46}(?![A-Za-z0-9_\-])")
KETTEN = ("tron", "ton")
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
STABLE_TRON = {USDT_TRON: ("USDT", 6), "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8": ("USDC", 6),
               "TPYmHEhy5n8TCEfYGqW2rPxsghSfzghPDn": ("USDD", 18)}
_C = {}


def ist_tron(a):
    return bool(TRON_RE.fullmatch(str(a or "")))


def ist_ton(a):
    return bool(TON_RE.fullmatch(str(a or "")))


def _cache(k, sek, fn):
    c = _C.get(k)
    if c and time.time() - c[0] < sek:
        return c[1]
    v = fn()
    if v is not None:
        _C[k] = (time.time(), v)
        if len(_C) > 2000:
            for x in sorted(_C, key=lambda y: _C[y][0])[:500]:
                _C.pop(x, None)
    return v


# ── TON ──────────────────────────────────────────────────────────────────

def _ton(pfad, params=None):
    h = dict(UA)
    key = os.getenv("TONAPI_KEY", "").strip()
    if key:
        h["Authorization"] = "Bearer " + key
    r = requests.get(os.getenv("TONAPI_URL", "https://tonapi.io").rstrip("/") + pfad, params=params, headers=h, timeout=12)
    if r.status_code != 200:
        raise RuntimeError(f"tonapi {r.status_code}")
    return r.json()


def null_adresse(a):
    """TON Null-Adresse (0:000...0) oder addr_none."""
    a = str(a or "").strip()
    if ":" in a:
        return set(a.split(":", 1)[1]) <= {"0"}
    return a in ("", "none", "addr_none")


def jetton(adr):
    """Was tonapi ueber einen Jetton weiss: {mintable, holders, admin, symbol, name, verified}. None, wenn nicht lesbar."""
    def lesen():
        try:
            d = _ton(f"/v2/jettons/{adr}")
        except Exception as e:
            log.debug(f"tronton jetton {str(adr)[:8]}: {e}")
            return None
        md = d.get("metadata") or {}
        out = {"symbol": md.get("symbol"), "name": md.get("name"), "image": md.get("image")}
        if isinstance(d.get("mintable"), bool):
            out["mintable"] = d["mintable"]
        try:
            if d.get("holders_count") is not None:
                out["holders"] = int(d["holders_count"])
        except Exception:
            pass
        ad = d.get("admin")
        adr_ad = ad.get("address") if isinstance(ad, dict) else None
        if adr_ad and not null_adresse(adr_ad):
            out["admin"] = adr_ad
        else:
            # Seit 2.82.35: kein Admin oder die Null-Adresse heisst abgegeben. Dann kann niemand mehr minten,
            # auch wenn der Vertrag selbst "mintable" meldet (das tut der Standard-Minter immer).
            out["admin_revoked"] = True
            out["mintable"] = False
        if d.get("verification"):
            out["verified"] = str(d["verification"])
        return out
    return _cache(("jetton", adr), 120, lesen)


def bestand_ton(w, p_ton):
    """-> ([positionen], fehler). TON plus Jettons, fuer die tonapi einen Preis kennt."""
    out, fehler = [], None
    try:
        d = _ton(f"/v2/accounts/{w}")
        b = int(d.get("balance") or 0) / 1e9
        if b and p_ton and b * p_ton >= 1:
            out.append({"chain": "ton", "key": "TON", "symbol": "TON", "amount": b, "usd": b * p_ton, "price": p_ton})
    except Exception as e:
        log.debug(f"tronton ton {str(w)[:8]}: {e}")
        return out, "TON not readable for " + str(w)[:6]
    try:
        d = _ton(f"/v2/accounts/{w}/jettons", {"currencies": "usd"})
        for x in d.get("balances") or []:
            j = x.get("jetton") or {}
            if str(j.get("verification") or "") == "blacklist":
                continue
            try:
                kurs = float((((x.get("price") or {}).get("prices") or {}).get("USD")) or 0)
                menge = int(x.get("balance") or 0) / 10 ** int(j.get("decimals") or 9)
            except Exception:
                continue
            usd = menge * kurs
            if not kurs or usd < 1:
                continue
            a = str(j.get("address") or "")
            sym = str(j.get("symbol") or "?")[:16]
            out.append({"chain": "ton", "key": f"ton:{a}", "symbol": sym, "amount": menge, "usd": usd, "price": kurs, "token": a})
    except Exception as e:
        log.debug(f"tronton ton jettons {str(w)[:8]}: {e}")
        fehler = "TON tokens not readable for " + str(w)[:6]
    return out, fehler


# ── TRON ─────────────────────────────────────────────────────────────────

def _tron(pfad, params=None, body=None):
    h = dict(UA)
    key = os.getenv("TRONGRID_KEY", "").strip()
    if key:
        h["TRON-PRO-API-KEY"] = key
    url = os.getenv("TRONGRID_URL", "https://api.trongrid.io").rstrip("/") + pfad
    r = requests.post(url, json=body, headers=h, timeout=12) if body is not None else requests.get(url, params=params, headers=h, timeout=12)
    if r.status_code != 200:
        raise RuntimeError(f"trongrid {r.status_code}")
    return r.json()


def _dezimal_tron(token):
    def lesen():
        try:
            d = _tron("/wallet/triggerconstantcontract", body={"owner_address": token, "contract_address": token,
                                                                "function_selector": "decimals()", "visible": True})
            return int((d.get("constant_result") or [None])[0], 16)
        except Exception as e:
            log.debug(f"tronton decimals {token[:8]}: {e}")
            return None
    return _cache(("dez", token), 86400, lesen)


def _preise_tron(tokens):
    """{adresse: (symbol, usd, liq)} von DexScreener, nur Paare auf TRON."""
    out = {}
    for i in range(0, len(tokens), 30):
        teil = tokens[i:i + 30]
        try:
            d = requests.get("https://api.dexscreener.com/tokens/v1/tron/" + ",".join(teil), headers=UA, timeout=10).json()
        except Exception as e:
            log.debug(f"tronton dex: {e}")
            continue
        for p in d if isinstance(d, list) else (d.get("pairs") or []):
            bt = p.get("baseToken") or {}
            a = str(bt.get("address") or "")
            try:
                usd, liq = float(p.get("priceUsd") or 0), float((p.get("liquidity") or {}).get("usd") or 0)
            except Exception:
                continue
            if a in teil and usd and liq > (out.get(a) or (None, 0, 0))[2]:
                out[a] = (str(bt.get("symbol") or "?")[:16], usd, liq)
    return out


def bestand_tron(w, p_trx):
    """-> ([positionen], fehler). TRX, Stablecoins und TRC20 mit echtem Markt (Liquiditaet ab 5.000 $, sonst Spam)."""
    out = []
    try:
        d = _tron(f"/v1/accounts/{w}")
    except Exception as e:
        log.debug(f"tronton tron {str(w)[:8]}: {e}")
        return out, "TRON not readable for " + str(w)[:6]
    k = ((d.get("data") or [None])[0]) or {}
    trx = int(k.get("balance") or 0) / 1e6
    for f in k.get("frozenV2") or []:   # gestakte TRX gehoeren weiter der Wallet
        try:
            trx += int(f.get("amount") or 0) / 1e6
        except Exception:
            pass
    if trx and p_trx and trx * p_trx >= 1:
        out.append({"chain": "tron", "key": "TRX", "symbol": "TRX", "amount": trx, "usd": trx * p_trx, "price": p_trx})
    roh = {}
    for x in k.get("trc20") or []:
        for a, v in (x or {}).items():
            try:
                if int(v) > 0:
                    roh[a] = int(v)
            except Exception:
                pass
    stable = 0.0
    for a, (sym, dez) in STABLE_TRON.items():
        if a in roh:
            m = roh.pop(a) / 10 ** dez
            if m >= 1:
                if sym == "USDD":
                    out.append({"chain": "tron", "key": f"tron:{a}", "symbol": sym, "amount": m, "usd": m, "price": 1.0, "token": a})
                else:
                    stable += m
    if stable >= 1:
        out.append({"chain": "tron", "key": "tron:stables", "symbol": "USDT/USDC", "amount": stable, "usd": stable, "price": 1.0})
    rest = sorted(roh)[:90]
    if rest:
        pr = _preise_tron(rest)
        for a, (sym, usd, liq) in pr.items():
            if liq < float(os.getenv("TRON_MIN_LIQ", "5000")):
                continue
            dez = _dezimal_tron(a)
            if dez is None:
                continue
            m = roh[a] / 10 ** dez
            if m * usd >= 1:
                out.append({"chain": "tron", "key": f"tron:{a}", "symbol": sym, "amount": m, "usd": m * usd, "price": usd, "token": a})
    return out, None


# ── Selbsttest ───────────────────────────────────────────────────────────

def test():
    """Fuer /v1/admin/selftest: sind die drei Quellen erreichbar und liefern sie das erwartete Format?"""
    teile = []
    d = requests.get("https://api.dexscreener.com/tokens/v1/tron/" + USDT_TRON, headers=UA, timeout=10).json()
    n = len(d if isinstance(d, list) else (d.get("pairs") or []))
    teile.append(f"DexScreener TRON pairs: {n}")
    k = ((_tron("/v1/accounts/" + USDT_TRON).get("data") or [None])[0]) or {}
    teile.append("TronGrid account: " + ("ok" if "balance" in k or "address" in k else "empty"))
    dez = _dezimal_tron(USDT_TRON)
    teile.append(f"USDT decimals: {dez}")
    j = _ton("/v2/jettons/EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs")
    teile.append("tonapi jetton: " + str((j.get("metadata") or {}).get("symbol")))
    if not n or dez != 6 or not j.get("metadata"):
        raise RuntimeError("; ".join(teile))
    return "; ".join(teile)


# ── Tiefen-Scan (seit 2.82.35) ───────────────────────────────────────────
# Wie bei Solana und EVM: erste Kaeufer, Buendel im selben Block, Sniper, frische Wallets, gleiche Geldgeber,
# vorbereitete Wallets, Top Holder. Die Auswertung selbst ist dieselbe wie auf allen Chains (intel._auswerten).
# Nicht drin: Weiterleitungen (forwards) und Haltedauer.

import hashlib  # noqa: E402
import threading  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
NULL_TRON = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
TRON_BUYERS = int(os.getenv("TRON_EARLY_BUYERS", "40"))
TON_BUYERS = int(os.getenv("TON_EARLY_BUYERS", "25"))
BOERSE_WORTE = ("binance", "okx", "bybit", "kucoin", "gate", "htx", "huobi", "mexc", "bitget", "crypto.com", "coinbase",
                "kraken", "bitfinex", "poloniex", "exchange", "cex", "wallet in telegram", "fragment")
_TAKT = {}
_TAKT_LOCK = threading.Lock()


def _warte(host, abstand):
    """Einfacher Takt je Dienst, damit die freien Limits halten."""
    with _TAKT_LOCK:
        jetzt = time.time()
        frei = max(jetzt, _TAKT.get(host, 0))
        _TAKT[host] = frei + abstand
    if frei > jetzt:
        time.sleep(frei - jetzt)


def b58(a):
    """TRON-Adresse in Base58 (T...), egal ob als T..., 41+hex oder 0x+hex geliefert."""
    a = str(a or "").strip()
    if a.startswith("T") and len(a) == 34:
        return a
    h = a[2:] if a.lower().startswith("0x") else a
    if len(h) == 40:
        h = "41" + h
    if len(h) != 42:
        return a
    roh = bytes.fromhex(h)
    roh += hashlib.sha256(hashlib.sha256(roh).digest()).digest()[:4]
    n, s = int.from_bytes(roh, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(roh) - len(roh.lstrip(b"\0"))) + s


def _hex20(a):
    """Base58 (T...) -> 20 Byte hex ohne 41."""
    n = 0
    for c in str(a):
        n = n * 58 + _B58.index(c)
    return n.to_bytes(25, "big")[1:21].hex()


def _tg(pfad, params=None, body=None):
    _warte("trongrid", 0.12 if os.getenv("TRONGRID_KEY", "").strip() else 0.35)
    return _tron(pfad, params, body)


def _konstant(token, sel, param=""):
    d = _tg("/wallet/triggerconstantcontract", body={"owner_address": token, "contract_address": token,
                                                      "function_selector": sel, "parameter": param, "visible": True})
    return int((d.get("constant_result") or ["0"])[0] or "0", 16)


def _tron_vertrag(a):
    try:
        d = _tg("/wallet/getcontract", body={"value": a, "visible": True})
        return bool(d.get("bytecode") or d.get("contract_address"))
    except Exception:
        return False


def _tron_boerse(f):
    """Boerse oder Dienst: 200 ausgehende Transaktionen in weniger als 6 Stunden."""
    try:
        d = _tg(f"/v1/accounts/{f}/transactions", {"only_from": "true", "limit": 200})
        ts = [x.get("block_timestamp") or 0 for x in d.get("data") or []]
        return len(ts) >= 200 and (max(ts) - min(ts)) < 6 * 3600 * 1000
    except Exception:
        return False


def _tron_wallet(k):
    """Alter, Geldgeber und Vorgeschichte eines Kaeufers."""
    w, kauf_ms = k["wallet"], int(k["ts"] * 1000)
    try:
        d = ((_tg(f"/v1/accounts/{w}").get("data") or [None])[0]) or {}
        erstellt = int(d.get("create_time") or 0) / 1000
        if erstellt and k["ts"]:
            k["alter_tage"] = round((k["ts"] - erstellt) / 86400, 2)
            k["fresh"] = (k["ts"] - erstellt) < 2 * 86400
    except Exception:
        pass
    try:
        d = _tg(f"/v1/accounts/{w}/transactions", {"only_to": "true", "order_by": "block_timestamp,asc", "limit": 1})
        t = (d.get("data") or [None])[0] or {}
        c = ((t.get("raw_data") or {}).get("contract") or [{}])[0]
        if c.get("type") == "TransferContract":
            f = b58(((c.get("parameter") or {}).get("value") or {}).get("owner_address"))
            if f and f != w:
                k["funder"] = f
    except Exception:
        pass
    try:
        d = _tg(f"/v1/accounts/{w}/transactions", {"max_timestamp": kauf_ms - 1, "limit": 50})
        k["vorher"] = len(d.get("data") or [])
    except Exception:
        pass
    return k


def _tron_transfers(token, seiten=3):
    out, fp = [], None
    for _ in range(seiten):
        p = {"event_name": "Transfer", "order_by": "block_timestamp,asc", "limit": 200, "only_confirmed": "true"}
        if fp:
            p["fingerprint"] = fp
        d = _tg(f"/v1/contracts/{token}/events", p)
        for e in d.get("data") or []:
            r = e.get("result") or {}
            von, an, wert = r.get("from") or r.get("0"), r.get("to") or r.get("1"), r.get("value") or r.get("2")
            try:
                out.append({"from": b58(von), "to": b58(an), "raw": int(wert), "block": int(e.get("block_number") or 0),
                            "ts": int(e.get("block_timestamp") or 0) // 1000, "tx": e.get("transaction_id")})
            except Exception:
                continue
        fp = (d.get("meta") or {}).get("fingerprint")
        if not fp:
            break
    return out


def tief_tron(token):
    import intel
    import scan
    tr = _tron_transfers(token)
    if not tr:
        return {"measured": False, "why": "no transfers read"}
    try:
        sup = _konstant(token, "totalSupply()")
    except Exception:
        sup = 0
    dev = None
    try:
        d = _tg("/wallet/getcontract", body={"value": token, "visible": True})
        dev = b58(d.get("origin_address")) if d.get("origin_address") else None
    except Exception:
        pass
    g = scan.goplus("tron", token) or {}
    halter = []
    for h in g.get("holders") or []:
        try:
            halter.append({"address": b58(h.get("address")), "pct": round(float(h.get("percent") or 0) * 100, 3),
                           "pool": bool(int(h.get("is_contract") or 0)) and not int(h.get("is_locked") or 0),
                           "locked": bool(int(h.get("is_locked") or 0))})
        except Exception:
            continue
    if not dev and g.get("creator_address"):
        dev = b58(g["creator_address"])
    start = tr[0]["block"]
    gesehen, kand, verkaeufer = set(), [], set()
    for t in tr:
        if t["from"] not in (NULL_TRON, token):
            verkaeufer.add(t["from"])
        w = t["to"]
        if w in gesehen or w in (NULL_TRON, token, dev) or not w:
            continue
        gesehen.add(w)
        kand.append({"wallet": w, "slot": t["block"], "ts": t["ts"], "raw": t["raw"]})
    kand = kand[:TRON_BUYERS * 2]
    with ThreadPoolExecutor(4) as ex:
        vertrag = dict(zip([k["wallet"] for k in kand], ex.map(_tron_vertrag, [k["wallet"] for k in kand])))
    kaeufer = [k for k in kand if not vertrag.get(k["wallet"])][:TRON_BUYERS]
    if not kaeufer:
        return {"measured": False, "why": "no early buyers found"}
    with ThreadPoolExecutor(4) as ex:
        kaeufer[:] = list(ex.map(_tron_wallet, kaeufer))

    def bal(w):
        try:
            return w, _konstant(token, "balanceOf(address)", _hex20(w).rjust(64, "0"))
        except Exception:
            return w, None
    wer = [k["wallet"] for k in kaeufer] + ([dev] if dev else [])
    with ThreadPoolExecutor(4) as ex:
        bestand = dict(ex.map(bal, wer))
    erg = intel._auswerten("tron", token, dev, start, kaeufer, bestand, sup, halter, verkaeufer,
                           boerse=_tron_boerse, slot_key="slot", sniper_spanne=2)
    erg["forwards"] = {"checked": False, "why": "not traced on TRON yet"}
    return erg


# TON: Transfers von toncenter (v3), Konten und Holder von tonapi.

def _tc(pfad, params=None):
    key = os.getenv("TONCENTER_KEY", "").strip()
    _warte("toncenter", 0.12 if key else 1.1)
    h = dict(UA)
    if key:
        h["X-API-Key"] = key
    r = requests.get(os.getenv("TONCENTER_URL", "https://toncenter.com").rstrip("/") + pfad, params=params, headers=h, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"toncenter {r.status_code}")
    return r.json()


def _ta(pfad, params=None):
    _warte("tonapi", 0.12 if os.getenv("TONAPI_KEY", "").strip() else 1.1)
    return _ton(pfad, params)


def roh(a):
    """TON-Adresse einheitlich als 0:hex klein. Freundliche Form (EQ/UQ) wird umgerechnet."""
    a = str(a or "").strip()
    if ":" in a:
        wc, h = a.split(":", 1)
        return f"{wc}:{h.lower()}"
    try:
        import base64
        b = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4))
        if len(b) == 36:
            wc = b[1] if b[1] < 128 else b[1] - 256
            return f"{wc}:{b[2:34].hex()}"
    except Exception:
        pass
    return a


def _ton_konten(adressen):
    """{roh: {is_wallet, name}} ueber tonapi, in Stuecken zu 100."""
    out = {}
    for i in range(0, len(adressen), 100):
        teil = adressen[i:i + 100]
        try:
            _warte("tonapi", 0.12 if os.getenv("TONAPI_KEY", "").strip() else 1.1)
            h = dict(UA)
            key = os.getenv("TONAPI_KEY", "").strip()
            if key:
                h["Authorization"] = "Bearer " + key
            r = requests.post(os.getenv("TONAPI_URL", "https://tonapi.io").rstrip("/") + "/v2/accounts/_bulk",
                              json={"account_ids": teil}, headers=h, timeout=15)
            for x in (r.json() or {}).get("accounts") or []:
                out[roh(x.get("address"))] = {"is_wallet": x.get("is_wallet"), "name": x.get("name")}
        except Exception as e:
            log.debug(f"tronton ton bulk: {e}")
    return out


def _ton_boerse_name(name):
    n = str(name or "").lower()
    return any(w in n for w in BOERSE_WORTE)


def _ton_wallet(k):
    w = k["wallet"]
    try:
        d = _tc("/api/v3/transactions", {"account": w, "sort": "asc", "limit": 1})
        t = (d.get("transactions") or [None])[0] or {}
        zeit = int(t.get("now") or 0)
        quelle = roh(((t.get("in_msg") or {}).get("source")) or "")
        if zeit and k["ts"]:
            k["alter_tage"] = round((k["ts"] - zeit) / 86400, 2)
            k["fresh"] = (k["ts"] - zeit) < 2 * 86400
        if quelle and quelle != w and ":" in quelle:
            k["funder"] = quelle
    except Exception:
        pass
    try:
        d = _tc("/api/v3/transactions", {"account": w, "end_utime": max(0, int(k["ts"]) - 1), "sort": "desc", "limit": 50})
        k["vorher"] = len(d.get("transactions") or [])
    except Exception:
        pass
    return k


def _ton_start(token):
    """Startzeit des Coins aus DexScreener (aeltestes Paar). Ohne Fenster sortiert toncenter ueber die ganze Historie
    und bricht bei grossen Jettons mit 500 ab (seit 2.82.38)."""
    try:
        import scan
        ts = [p.get("pairCreatedAt") for p in scan._paare(token) if p.get("chainId") == "ton" and p.get("pairCreatedAt")]
        return int(min(ts) / 1000) if ts else None
    except Exception:
        return None


def _ton_transfers(token, start=None, limit=256):
    p = {"jetton_master": token, "sort": "asc", "limit": limit, "offset": 0}
    if start:
        p["start_utime"] = max(0, start - 6 * 3600)
        p["end_utime"] = start + 3 * 86400
    try:
        return _tc("/api/v3/jetton/transfers", p)
    except Exception:
        if not start:
            raise
        # Erster Versuch im Fenster gescheitert: kleineres Fenster, weniger Zeilen
        p.update(end_utime=start + 86400, limit=128)
        return _tc("/api/v3/jetton/transfers", p)


def tief_ton(token):
    import intel
    try:
        d = _ton_transfers(token, _ton_start(token))
    except Exception as e:
        return {"measured": False, "why": f"transfers not readable ({str(e)[:40]})"}
    tr = []
    for x in d.get("jetton_transfers") or []:
        if x.get("transaction_aborted"):
            continue
        try:
            tr.append({"from": roh(x.get("source")), "to": roh(x.get("destination")), "raw": int(x.get("amount") or 0),
                       "ts": int(x.get("transaction_now") or 0)})
        except Exception:
            continue
    if not tr:
        return {"measured": False, "why": "no transfers read"}
    j = _ta(f"/v2/jettons/{token}")
    sup = int(j.get("total_supply") or 0)
    master = roh(token)
    dev = None
    try:
        t = (_tc("/api/v3/transactions", {"account": token, "sort": "asc", "limit": 1}).get("transactions") or [None])[0] or {}
        dev = roh(((t.get("in_msg") or {}).get("source")) or "") or None
    except Exception:
        pass
    halter, bestand = [], {}
    try:
        hd = _ta(f"/v2/jettons/{token}/holders", {"limit": 20})
        for h in hd.get("addresses") or []:
            o = h.get("owner") or {}
            a = roh(o.get("address"))
            menge = int(h.get("balance") or 0)
            bestand[a] = menge
            x = {"address": a, "pct": round(menge / sup * 100, 3) if sup else 0, "pool": o.get("is_wallet") is False}
            if o.get("name"):
                x["pool_name"] = o["name"]
            halter.append(x)
    except Exception as e:
        log.debug(f"tronton ton holders: {e}")
    start = tr[0]["ts"]
    gesehen, kand, verkaeufer = set(), [], set()
    for t in tr:
        if t["from"]:
            verkaeufer.add(t["from"])
        w = t["to"]
        if not w or w in gesehen or w in (master, dev):
            continue
        gesehen.add(w)
        kand.append({"wallet": w, "slot": t["ts"], "ts": t["ts"], "raw": t["raw"]})
    kand = kand[:TON_BUYERS * 2]
    konten = _ton_konten([k["wallet"] for k in kand])
    kaeufer = [k for k in kand if (konten.get(k["wallet"]) or {}).get("is_wallet") is not False][:TON_BUYERS]
    if not kaeufer:
        return {"measured": False, "why": "no early buyers found"}
    for k in kaeufer:   # nacheinander: ohne Key erlaubt toncenter etwa eine Anfrage pro Sekunde
        _ton_wallet(k)
    fehlt = [k["wallet"] for k in kaeufer if k["wallet"] not in bestand] + ([dev] if dev and dev not in bestand else [])
    for w in fehlt:
        try:
            bestand[w] = int(_ta(f"/v2/accounts/{w}/jettons/{token}").get("balance") or 0)
        except Exception:
            bestand[w] = 0   # tonapi antwortet ohne Bestand mit einem Fehler: dann haelt das Wallet nichts
    namen = _ton_konten(list({k["funder"] for k in kaeufer if k.get("funder")})[:100])

    def boerse(f):
        return _ton_boerse_name((namen.get(f) or {}).get("name"))
    erg = intel._auswerten("ton", master, dev, start, kaeufer, bestand, sup, halter, verkaeufer,
                           boerse=boerse, slot_key="slot", sniper_spanne=6)
    erg["forwards"] = {"checked": False, "why": "not traced on TON yet"}
    return erg


def test_tief():
    """Selbsttest: liefern die Quellen fuer den Tiefen-Scan das Format, das tief_tron und tief_ton erwarten?"""
    teile = []
    tr = _tron_transfers(USDT_TRON, seiten=1)
    ok_tron = bool(tr) and tr[0]["to"].startswith("T") and tr[0]["block"] > 0
    teile.append(f"TRON transfers: {len(tr)}" + (f", first block {tr[0]['block']}" if tr else ""))
    jetzt = int(time.time())
    d = _tc("/api/v3/jetton/transfers", {"jetton_master": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
                                         "start_utime": jetzt - 3600, "end_utime": jetzt,
                                         "sort": "asc", "limit": 5, "offset": 0})
    x = (d.get("jetton_transfers") or [None])[0] or {}
    ok_ton = bool(x.get("destination")) and bool(x.get("transaction_now"))
    teile.append(f"TON transfers: {len(d.get('jetton_transfers') or [])}" + (", fields ok" if ok_ton else ", fields missing"))
    if not (ok_tron and ok_ton):
        raise RuntimeError("; ".join(teile))
    return "; ".join(teile)
