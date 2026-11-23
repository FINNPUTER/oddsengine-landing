"""
stocktoken.py: Robinhood Stock Tokens auf Robinhood Chain, seit 2.72.0.

Aktien und ETFs (TSLA, NVDA, AAPL, SPY ...) gibt es als Token auf Robinhood Chain. Sie handeln rund um die Uhr,
auch nachts und am Wochenende, wenn die Boerse zu ist. Your Engine liest sie nur (Preise, Bewegung, Liquiditaet).

  liste()            offizielle Liste aus Robinhoods Asset-Registry (api.robinhood.com/rhj/assets), 6 h Cache.
                     Nur diese Adressen gelten. Ein Token mit gleichem Ticker, aber anderer Adresse ist keine Aktie.
  adresse(sym)       offizielle Contract-Adresse zu einem Ticker oder None
  kurse(syms)        24/7-Preis je Ticker: usd, Veraenderung 1 h / 24 h, Liquiditaet, Volumen, Pool-Link
  bewegung(n)        groesste Gewinner und Verlierer unter allen Stock Tokens (24 h, nur mit genug Liquiditaet)
  mit_boerse(syms)   Token-Preis neben dem letzten Boersenkurs (Yahoo), Abstand in Prozent

Quellen: Robinhood (Liste), DexScreener (Preise und Bewegung, Pool mit der meisten Liquiditaet).
"""
import json
import time

import requests

from logger import log

REGISTRY = "https://api.robinhood.com/rhj/assets"
UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
MIN_LIQ = 10_000
_L = {"ts": 0.0, "d": None}
_K = {}
_B = {"ts": 0.0, "d": None}
HINWEIS = ("Robinhood Stock Tokens on Robinhood Chain trade around the clock, also when the stock market is closed, "
           "so the token price can move away from the last close. Whether you may hold them depends on where you live.")


def liste():
    """{TICKER: {"symbol","name","address","logo","multiplier"}}"""
    if _L["d"] is not None and time.time() - _L["ts"] < 6 * 3600:
        return _L["d"]
    d = None
    try:
        roh = requests.get(REGISTRY, headers=UA, timeout=15).json().get("assets") or []
        d = {}
        for a in roh:
            if a.get("status") != "ASSET_STATUS_ACTIVE":
                continue
            dep = next((x for x in a.get("deployments") or [] if int(x.get("chainId") or 0) == 4663), None)
            sym = str(a.get("tokenSymbol") or "").upper()
            if not dep or not sym:
                continue
            d[sym] = {"symbol": sym, "name": str(a.get("tokenName") or sym).split(" • ")[0].strip(),
                      "address": str(dep.get("contractAddress") or "").lower(), "logo": a.get("logoUrl"),
                      "multiplier": float(a.get("currentMultiplier") or 1)}
        try:
            import storage
            storage.kv_set("stocktoken_liste", json.dumps(d))
        except Exception:
            pass
    except Exception as e:
        log.warning(f"stocktoken liste: {e}")
        try:
            import storage
            d = json.loads(storage.kv_get("stocktoken_liste") or "null")
        except Exception:
            d = None
    if d:
        _L.update(ts=time.time(), d=d)
    return d or _L["d"] or {}


def _norm(sym):
    s = str(sym or "").strip().upper().lstrip("$")
    for w in (" STOCK TOKEN", " TOKEN", " STOCK", " SHARES", " SHARE"):
        if s.endswith(w):
            s = s[: -len(w)].strip()
    return s


def adresse(sym):
    """Offizielle Adresse zu einem Ticker (TSLA, $NVDA, "TSLA stock") oder None."""
    if str(sym or "").lower().startswith("0x"):
        a = str(sym).lower()
        return a if any(x["address"] == a for x in liste().values()) else None
    x = liste().get(_norm(sym))
    return x["address"] if x else None


def ist_aktie(sym):
    return adresse(sym) is not None


def _paare(adressen):
    """DexScreener: bestes Paar (meiste Liquiditaet) je Token-Adresse. 30 Adressen pro Abfrage, 2 Minuten Cache."""
    out, fehlt = {}, []
    for a in adressen:
        c = _K.get(a)
        if c and time.time() - c[0] < 120:
            out[a] = c[1]
        else:
            fehlt.append(a)
    for i in range(0, len(fehlt), 30):
        teil = fehlt[i:i + 30]
        try:
            d = requests.get("https://api.dexscreener.com/tokens/v1/robinhood/" + ",".join(teil), headers=UA, timeout=15).json()
        except Exception as e:
            log.debug(f"stocktoken dex: {e}")
            continue
        beste = {}
        for p in d if isinstance(d, list) else []:
            a = str((p.get("baseToken") or {}).get("address") or "").lower()
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if a in teil and p.get("priceUsd") and liq > float(((beste.get(a) or {}).get("liquidity") or {}).get("usd") or -1):
                beste[a] = p
        for a in teil:
            p = beste.get(a)
            _K[a] = (time.time(), p)
            out[a] = p
    return out


def _zeile(x, p):
    if not p:
        return {"symbol": x["symbol"], "name": x["name"], "contract": x["address"], "usd": None,
                "note": "no pool with a price found right now"}
    ch = p.get("priceChange") or {}
    f = lambda v: float(v) if v not in (None, "") else None
    return {"symbol": x["symbol"], "name": x["name"], "contract": x["address"], "usd": f(p.get("priceUsd")),
            "change_1h_pct": f(ch.get("h1")), "change_24h_pct": f(ch.get("h24")),
            "liquidity_usd": round(f((p.get("liquidity") or {}).get("usd")) or 0), "volume_24h_usd": round(f((p.get("volume") or {}).get("h24")) or 0),
            "pool": p.get("url"), "logo": x.get("logo")}


def kurse(syms):
    l = liste()
    gew = []
    for s in syms or []:
        a = adresse(s)
        if a:
            x = next((v for v in l.values() if v["address"] == a), None)
            if x and x not in gew:
                gew.append(x)
    gew = gew[:30]
    ps = _paare([x["address"] for x in gew])
    return [_zeile(x, ps.get(x["address"])) for x in gew]


def bewegung(n=5):
    """Groesste Gewinner und Verlierer der letzten 24 h unter allen Stock Tokens mit genug Liquiditaet. 5 Minuten Cache."""
    if _B["d"] is not None and time.time() - _B["ts"] < 300:
        alle = _B["d"]
    else:
        l = list(liste().values())
        ps = _paare([x["address"] for x in l])
        alle = [z for z in (_zeile(x, ps.get(x["address"])) for x in l)
                if z.get("usd") and z.get("change_24h_pct") is not None and (z.get("liquidity_usd") or 0) >= MIN_LIQ]
        _B.update(ts=time.time(), d=alle)
    auf = sorted(alle, key=lambda z: -z["change_24h_pct"])
    return {"up": auf[:n], "down": list(reversed(auf[-n:])) if len(auf) > n else [], "tokens_checked": len(alle),
            "min_liquidity_usd": MIN_LIQ}


def mit_boerse(syms):
    """Token-Preis (24/7) neben dem Boersenkurs (Yahoo, leicht verzoegert). Abstand in Prozent, mit Multiplikator."""
    import aktien
    tk = kurse(syms)
    bk = {k["symbol"]: k for k in aktien.kurse([t["symbol"] for t in tk])}
    l = liste()
    for t in tk:
        b = bk.get(t["symbol"])
        if not b or not t.get("usd"):
            continue
        fair = b["price"] * (l.get(t["symbol"], {}).get("multiplier") or 1)
        t["stock_price"] = b["price"]
        t["stock_change_pct"] = b.get("change_pct")
        t["token_vs_stock_pct"] = round((t["usd"] - fair) / fair * 100, 2) if fair else None
    return tk


def zeile(t):
    """'TSLA $408.11 (-0.6% 24h)' fuer Briefing und Kurzantworten."""
    if not t.get("usd"):
        return None
    v = t["usd"]
    s = f"{t['symbol']} ${v:,.2f}" if v >= 1 else f"{t['symbol']} ${v:.4f}"
    if t.get("change_24h_pct") is not None:
        s += f" ({t['change_24h_pct']:+.1f}% 24h)"
    return s
