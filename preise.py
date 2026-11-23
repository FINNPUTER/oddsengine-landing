"""
preise.py: historische USD-Kurse fuer beliebige Coins und das Angebot (Supply). Seit 2.62.

  usd(chain, token, ts)   Kurs in USD zur Stunde von ts (GeckoTerminal, stuendliche Kerzen des liquidesten Pools)
  quote_usd(chain, adr, sym, ts)
                          Wert einer Paarwaehrung: ETH/WETH und SOL ueber Coinbase (steuer.kurs), Stablecoins 1 USD,
                          alles andere (z. B. TORCH) ueber usd()
  supply(chain, token)    Gesamtangebot in ganzen Einheiten, fuer den Market Cap zu einem Kurs

GeckoTerminal erlaubt ohne Schluessel etwa 30 Abfragen pro Minute: alle Abfragen gehen durch eine Drossel,
die Kerzen werden in Bloecken zu 1000 Stunden geladen und gemerkt.
"""
import os
import threading
import time

import requests

from logger import log

GT = "https://api.geckoterminal.com/api/v2"
NETZ = {"solana": "solana", "ethereum": "eth", "base": "base", "bsc": "bsc", "arbitrum": "arbitrum", "robinhood": "robinhood",
        "tron": "tron", "ton": "ton"}
UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
ETH_ADR = {"0x0000000000000000000000000000000000000000", "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}
WSOL = "So11111111111111111111111111111111111111112"
STABLE = {"USDC", "USDT", "DAI", "USDG", "PYUSD", "USDE", "USDC.E", "USDBC", "FDUSD", "USD1", "USDS"}
SOL_STABLE = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
              "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"}
SEITE = 1000 * 3600

_SPERRE = threading.Lock()
_LETZTE = [0.0]
_KERZEN = {}
_POOL = {}
_SUPPLY = {}


def _gt(pfad, params=None):
    """GeckoTerminal mit Drossel (GT_MIN_GAP Sekunden zwischen zwei Abfragen) und Wiederholung bei 429."""
    gap = float(os.getenv("GT_MIN_GAP", "2.1"))
    for versuch in range(4):
        with _SPERRE:
            warte = _LETZTE[0] + gap - time.time()
            if warte > 0:
                time.sleep(warte)
            _LETZTE[0] = time.time()
        try:
            r = requests.get(GT + pfad, params=params or {}, headers=UA, timeout=15)
            if r.status_code == 429:
                time.sleep(4 * (versuch + 1))
                continue
            if r.status_code == 404:
                return None
            return r.json()
        except Exception as e:
            log.debug(f"preise gt {pfad[:60]}: {e}")
            time.sleep(1.0)
    return None


def pool(chain, token):
    """Liquidester Pool des Coins auf der Chain (DexScreener, wie im Scan), sonst GeckoTerminal."""
    k = (chain, str(token).lower())
    if k in _POOL and time.time() - _POOL[k][1] < 6 * 3600:
        return _POOL[k][0]
    p = None
    try:
        import scan
        ps = [x for x in scan._paare(token) if scan.DEX_ZU.get(x.get("chainId")) == chain
              and str((x.get("baseToken") or {}).get("address", "")).lower() == str(token).lower()]
        if ps:
            p = max(ps, key=scan._liq).get("pairAddress")
    except Exception:
        pass
    if not p and NETZ.get(chain):
        d = _gt(f"/networks/{NETZ[chain]}/tokens/{token}/pools", {"page": 1}) or {}
        l = d.get("data") or []
        if l:
            p = (l[0].get("attributes") or {}).get("address")
    _POOL[k] = (p, time.time())
    return p


def usd(chain, token, ts):
    """USD-Kurs von token zur Stunde von ts, oder None."""
    if not ts or not NETZ.get(chain):
        return None
    if chain == "ton" and ":" in str(token):
        import steuer_tt
        token = steuer_tt.freundlich(token)
    p = pool(chain, token)
    if not p:
        return None
    h = int(ts // 3600)
    ende = (int(ts // SEITE) + 1) * SEITE
    k = (chain, p, str(token).lower(), ende)
    if k not in _KERZEN:
        d = _gt(f"/networks/{NETZ[chain]}/pools/{p}/ohlcv/hour",
                {"aggregate": 1, "limit": 1000, "before_timestamp": int(min(ende, time.time() + 3600)),
                 "currency": "usd", "token": token}) or {}
        liste = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        _KERZEN[k] = {int(x[0]) // 3600: float(x[4]) for x in liste if x and x[4]}
    kz = _KERZEN[k]
    for dh in (0, -1, 1, -2, 2, -3, 3, -6, 6, -12, 12, -24, 24):
        if h + dh in kz:
            return kz[h + dh]
    # Vor dem ersten Handel der Seite: der frueheste bekannte Wert, wenn er nah genug ist
    if kz:
        erste = min(kz)
        if 0 < erste - h <= 48:
            return kz[erste]
    return None


def quote_usd(chain, adr, sym, ts):
    """USD je Einheit einer Paarwaehrung zum Zeitpunkt ts. -> (usd, quelle) oder (None, None)."""
    import steuer
    a = str(adr or "").lower()
    s = str(sym or "").upper()
    if chain == "solana":
        if adr == WSOL or s in ("SOL", "WSOL"):
            return steuer.kurs("SOL", ts)[0], "SOL"
        if adr in SOL_STABLE or s in STABLE:
            return 1.0, "USD"
    else:
        if a in ETH_ADR or s == "ETH":
            return steuer.kurs("ETH", ts)[0], "ETH"
        if s == "WETH":
            try:
                kl = steuer._art_evm(chain, a, s)[2]
            except Exception:
                kl = None
            if kl == "ETH":
                return steuer.kurs("ETH", ts)[0], "ETH"
        if s in STABLE:
            try:
                import fluss
                if not fluss.falsch(sym, chain, a):
                    return 1.0, "USD"
            except Exception:
                return 1.0, "USD"
        if chain == "bsc" and s in ("BNB", "WBNB"):
            return usd(chain, a, ts), "GT"
    return usd(chain, adr, ts), "GT"


def supply(chain, token):
    """Gesamtangebot in ganzen Einheiten (float) und Dezimalstellen, oder (None, None)."""
    k = (chain, str(token).lower())
    if k in _SUPPLY and time.time() - _SUPPLY[k][2] < 3600:
        return _SUPPLY[k][0], _SUPPLY[k][1]
    sup = dez = None
    if chain in ("tron", "ton"):
        return None, None  # Market Cap fuer TRON und TON noch nicht im Export
    try:
        if chain == "solana":
            import intel
            v = ((intel._sol("getTokenSupply", [token]) or {}).get("value") or {})
            dez = int(v.get("decimals") or 0)
            sup = int(v.get("amount") or 0) / 10 ** dez
        else:
            import scan
            dez = int(scan._evm(chain, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)
            sup = int(scan._evm(chain, "eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), 16) / 10 ** dez
    except Exception as e:
        log.debug(f"preise supply {token[:10]}: {e}")
    if sup:
        _SUPPLY[k] = (sup, dez, time.time())
    return sup, dez


def dezimal(chain, token):
    """Dezimalstellen eines EVM-Tokens (18 fuer natives ETH)."""
    if str(token).lower() in ETH_ADR:
        return 18
    k = ("dez", chain, str(token).lower())
    if k in _SUPPLY:
        return _SUPPLY[k][0]
    try:
        import scan
        d = int(scan._evm(chain, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)
    except Exception:
        d = 18
    _SUPPLY[k] = (d, None, time.time())
    return d
