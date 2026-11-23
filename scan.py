"""
scan.py: der CA-Report fuer jede Chain. Seit 2.39.

Ein Coin, egal wo: Solana, Base, Ethereum, BNB Chain, Arbitrum, Robinhood Chain.

  analyse(q)        Chain erkennen, alles einsammeln, Urteil bilden, Karte fuer Telegram bauen
  trades(...)       die letzten Kaeufe und Verkaeufe eines Paars (fuer die Live-Seite)
  nachfassen()      aus motor.tick: fertige Tiefen-Scans nachliefern

Schichten:
  Markt       DexScreener (jede Chain, ohne Schluessel): Preis, MC, Liquiditaet, Volumen,
              Kaeufe/Verkaeufe je Fenster, Alter, Boerse, Chart
  Sicherheit  EVM: GoPlus (Honeypot, Steuern, Mint, Owner, LP-Lock, Holder, Creator).
              Solana: Mint- und Freeze-Rechte direkt vom Mint-Konto (RPC, HELIUS_API_KEY wenn da).
              Robinhood: onchain.token (Torch, Dev, Burn, Top 10).
  Tiefe       intel.py, eigene Erkennung: erste Kaeufer, Bundles, Sniper, frische Wallets, Cluster,
              Dev-Historie, bewertete Kaeufer aus der eigenen Wallet-Datenbank. Kein fremder Dienst.
"""
import json
import os
import re
import threading
import time

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
SEITE = os.getenv("ENGINE_PUBLIC_URL", "https://engine.oddsengine.live").rstrip("/")

# key: (Name, DexScreener-Id, GeckoTerminal-Netz, GoPlus-Chain-Id, Explorer, Token-Pfad, Adress-Pfad,
#       Tx-Pfad, Nativ, EVM-Chain-Id, Tiefen-Name)
KETTEN = {
    "solana":    ("Solana", "solana", "solana", None, "https://solscan.io", "/token/", "/account/", "/tx/", "SOL", None, "solana"),
    "ethereum":  ("Ethereum", "ethereum", "eth", "1", "https://etherscan.io", "/token/", "/address/", "/tx/", "ETH", 1, "ethereum"),
    "base":      ("Base", "base", "base", "8453", "https://basescan.org", "/token/", "/address/", "/tx/", "ETH", 8453, "base"),
    "bsc":       ("BNB Chain", "bsc", "bsc", "56", "https://bscscan.com", "/token/", "/address/", "/tx/", "BNB", 56, None),
    "arbitrum":  ("Arbitrum", "arbitrum", "arbitrum", "42161", "https://arbiscan.io", "/token/", "/address/", "/tx/", "ETH", 42161, None),
    "robinhood": ("Robinhood Chain", "robinhood", "robinhood", None, "https://robinhoodchain.blockscout.com", "/token/", "/address/", "/tx/", "ETH", 4663, "robinhood"),
    # Seit 2.82.15: Elysium (Kinetiq, L2 auf HyperEVM). Dev, Holder, Cluster und Verbindungen kommen aus elysium.py.
    # Seit 2.82.32: TRON und TON. Markt von DexScreener, Vertrags-Flags fuer TRON von GoPlus, Jetton-Daten von tonapi
    # (tronton.py). Kein Tiefen-Scan, kein Handel.
    "tron":      ("TRON", "tron", "tron", "tron", "https://tronscan.org/#", "/token20/", "/address/", "/transaction/", "TRX", None, None),
    "ton":       ("TON", "ton", "ton", None, "https://tonviewer.com", "/", "/", "/transaction/", "TON", None, None),
    "elysium":   ("Elysium", "elysium", "elysium", None, os.getenv("ELYSIUM_EXPLORER", "https://elysium.kinetiq.xyz/explorer").rstrip("/"), "/token/", "/address/", "/tx/", "HYPE", 99901, None),
}
DEX_ZU = {v[1]: k for k, v in KETTEN.items()}
# Chains ohne Tiefen-Scan (Buendel, Gruppen, erste Kaeufer)
OHNE_TIEFE = ("elysium",)
GROSS = ("solana", "tron", "ton")   # Adressen mit Gross- und Kleinschreibung

# Eigene Coins (ODDS, TORCH): nie als Nachahmer markieren. OWN_TOKENS (Komma-Liste) erweitert das.
EIGENE = {"0x9c87b4cfa21266d1638f437c96dbe1012cb81111", "0x000050e18ffa3f8f7ad170eb9cef160eb3ed1111"} | {
    x.strip().lower() for x in os.getenv("OWN_TOKENS", "").split(",") if x.strip()}
NAMEN = {"sol": "solana", "solana": "solana", "eth": "ethereum", "ethereum": "ethereum", "mainnet": "ethereum",
         "base": "base", "bsc": "bsc", "bnb": "bsc", "binance": "bsc", "arb": "arbitrum", "arbitrum": "arbitrum",
         "robinhood": "robinhood", "rh": "robinhood", "torch": "robinhood", "elysium": "elysium", "kinetiq": "elysium",
         "tron": "tron", "trx": "tron", "trc20": "tron", "ton": "ton", "toncoin": "ton"}
DEX_NAMEN = {"pumpfun": "pump.fun curve", "pumpswap": "PumpSwap", "raydium": "Raydium", "meteora": "Meteora",
             "orca": "Orca", "uniswap": "Uniswap", "aerodrome": "Aerodrome", "pancakeswap": "PancakeSwap",
             "sushiswap": "SushiSwap", "camelot": "Camelot", "launchlab": "LaunchLab", "moonshot": "Moonshot",
             "boop": "boop", "bonk": "letsbonk", "believe": "Believe", "meteoradbc": "Meteora DBC",
             "sunswap": "SunSwap", "sunpump": "SunPump", "stonfi": "STON.fi", "dedust": "DeDust"}

SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
from tronton import TRON_RE, TON_RE  # noqa: E402


def ist_adresse(a):
    """Vertrags- oder Wallet-Adresse auf einer der Chains, die der Scanner kennt."""
    a = str(a or "")
    return bool(EVM_RE.fullmatch(a) or SOL_RE.fullmatch(a) or TON_RE.fullmatch(a))


def ist_evm(chain):
    return (KETTEN.get(chain) or (None,) * 10)[9] is not None
GRUEN, GELB, ROT = "🟢", "🟠", "🔴"
AMPEL = {"green": GRUEN, "amber": GELB, "red": ROT}


def _f(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def _ff(k, d):
    try:
        return float(os.getenv(k, "") or d)
    except Exception:
        return float(d)


# ── Zahlen lesbar ────────────────────────────────────────────────────────

def usd(v):
    v = _f(v)
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.2f}B"
    if a >= 1e6:
        return f"${v/1e6:.2f}M"
    if a >= 1e3:
        return f"${v/1e3:.1f}K"
    if a == 0:
        return "$0"
    return f"${v:,.0f}" if a >= 10 else f"${v:,.2f}"


def preis(v):
    v = _f(v)
    if v is None:
        return "n/a"
    if v >= 1:
        return f"${v:,.4f}".rstrip("0").rstrip(".")
    if v == 0:
        return "$0"
    s = f"{v:.12f}".rstrip("0")
    # 0.000001234 -> $0.0₅1234 waere huebsch, liest aber nicht jeder. Vier gueltige Stellen reichen.
    nach = s.split(".")[1]
    nullen = len(nach) - len(nach.lstrip("0"))
    return "$" + f"{v:.{nullen + 4}f}".rstrip("0")


def pct(v, plus=True):
    v = _f(v)
    if v is None:
        return "n/a"
    return (f"{v:+.1f}%" if plus else f"{v:.1f}%").replace(".0%", "%")


def zahl(v):
    """12.345 -> '12.3', 2.0 -> '2', 0.04 -> '0.04'."""
    v = _f(v)
    if v is None:
        return "?"
    if abs(v) < 1 and v != 0:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.1f}".rstrip("0").rstrip(".")


def balken(p, n=10):
    p = max(0.0, min(100.0, _f(p) or 0.0))
    voll = int(round(p / 100 * n))
    return "▰" * voll + "▱" * (n - voll)


def kurz(a):
    a = str(a or "")
    return a[:4] + "…" + a[-4:] if len(a) > 12 else a


def alter_text(sek):
    if sek is None:
        return None
    sek = max(0, sek)
    if sek < 3600:
        return f"{int(sek // 60)} min"
    if sek < 172800:
        return f"{sek / 3600:.1f} h".replace(".0 h", " h")
    if sek < 86400 * 60:
        return f"{int(sek // 86400)} days"
    return f"{int(sek // (86400 * 30))} months"


def explorer(chain, art, a):
    k = KETTEN.get(chain)
    if not k or not a:
        return None
    pfad = {"token": k[5], "address": k[6], "tx": k[7]}[art]
    return k[4] + pfad + a


# ── Eingabe verstehen ────────────────────────────────────────────────────

def lesen(q):
    """Aus Text, Link oder Ticker: (chain_hinweis, adresse_oder_ticker)."""
    q = str(q or "").strip()
    hinweis = None
    m = re.search(r"dexscreener\.com/([a-z0-9]+)/", q, re.I) or re.search(r"geckoterminal\.com/([a-z0-9_]+)/", q, re.I)
    if m:
        hinweis = NAMEN.get(m.group(1).lower()) or DEX_ZU.get(m.group(1).lower())
    if re.search(r"pump\.fun|solscan|birdeye|photon-sol|bullx|axiom\.trade|gmgn\.ai/sol", q, re.I):
        hinweis = "solana"
    if re.search(r"basescan|/base/", q, re.I):
        hinweis = hinweis or "base"
    if re.search(r"etherscan\.io", q, re.I):
        hinweis = hinweis or "ethereum"
    if re.search(r"bscscan", q, re.I):
        hinweis = hinweis or "bsc"
    if re.search(r"torchpad|robinhoodchain", q, re.I):
        hinweis = hinweis or "robinhood"
    if re.search(r"tronscan|sunswap|sunpump|sun\.io", q, re.I):
        hinweis = hinweis or "tron"
    if re.search(r"tonviewer|tonscan|ston\.fi|dedust\.io", q, re.I):
        hinweis = hinweis or "ton"
    if ":" in q and not q.lower().startswith("http"):
        a, b = q.split(":", 1)
        if a.lower() in NAMEN:
            hinweis, q = NAMEN[a.lower()], b.strip()
    m = EVM_RE.search(q)
    if m:
        return hinweis, m.group(0).lower()
    m = TON_RE.search(q)
    if m:
        return "ton", m.group(0)
    m = TRON_RE.search(q)
    if m and hinweis != "solana":
        # T + 33 Zeichen ist eine TRON-Adresse. Ohne Hinweis entscheidet chain_waehlen nach den Paaren.
        return hinweis, m.group(0)
    m = SOL_RE.search(q)
    if m and not re.fullmatch(r"[a-z]+", m.group(0)):
        return "solana", m.group(0)
    return hinweis, q.lstrip("$").strip()


# ── Markt: DexScreener ───────────────────────────────────────────────────

_DEX = {}


def _paare(adr):
    c = _DEX.get(adr)
    if c and time.time() - c[0] < 20:
        return c[1]
    ps, ok = [], False
    # Seit 2.51.1: zweiter Versuch und zweiter Endpunkt. Ein einzelner Fehler (429, Timeout) gab sonst "MC n/a" auf der Karte.
    urls = ["https://api.dexscreener.com/latest/dex/tokens/" + adr]
    if TON_RE.fullmatch(adr):
        urls.append("https://api.dexscreener.com/tokens/v1/ton/" + adr)
    elif TRON_RE.fullmatch(adr):
        urls.append("https://api.dexscreener.com/tokens/v1/tron/" + adr)
    elif not adr.startswith("0x"):
        urls.append("https://api.dexscreener.com/tokens/v1/solana/" + adr)
    for i, url in enumerate(urls + urls[:1]):
        try:
            r = requests.get(url, headers=UA, timeout=8)
            if r.status_code == 429:
                log.warning(f"scan dex 429 {adr[:8]}")
                time.sleep(1.2)
                continue
            d = r.json()
            ps = (d if isinstance(d, list) else d.get("pairs")) or []
            ok = True
            break
        except Exception as e:
            log.warning(f"scan dex {adr[:8]} try {i + 1}: {str(e)[:80]}")
    if not ok and c:
        return c[1]
    if ok:
        _DEX[adr] = (time.time(), ps)
    return ps


def _liq(p):
    return float((p.get("liquidity") or {}).get("usd") or 0)


def chain_waehlen(adr, paare, hinweis=None):
    """Auf welcher Chain lebt der Coin? Hinweis gewinnt, sonst die Chain mit der meisten Liquiditaet,
    bei 0x ohne Paar: Robinhood (Torch-Kurve), bei base58 immer Solana."""
    eigene = [p for p in paare if str(p.get("baseToken", {}).get("address", "")).lower() == adr.lower()] or paare
    if hinweis and hinweis in KETTEN:
        return hinweis
    if TON_RE.fullmatch(adr):
        return "ton"
    if TRON_RE.fullmatch(adr):
        # sehr selten ist so eine Adresse auch ein Solana-Coin: dann gewinnen die Paare
        if any(p.get("chainId") == "solana" for p in eigene) and not any(p.get("chainId") == "tron" for p in eigene):
            return "solana"
        return "tron"
    if not adr.startswith("0x"):
        return "solana"
    summe = {}
    for p in eigene:
        k = DEX_ZU.get(p.get("chainId"))
        if k:
            summe[k] = summe.get(k, 0) + _liq(p)
    if summe:
        return max(summe, key=summe.get)
    return "robinhood"


def markt(adr, chain, paare):
    ps = [p for p in paare if DEX_ZU.get(p.get("chainId")) == chain]
    eigene = [p for p in ps if str(p.get("baseToken", {}).get("address", "")).lower() == adr.lower()]
    ps = sorted(eigene or ps, key=lambda p: -_liq(p))
    if not ps:
        return {}
    p = ps[0]
    alt = min([x.get("pairCreatedAt") for x in ps if x.get("pairCreatedAt")] or [None]) if ps else None
    info = p.get("info") or {}
    links = {}
    # Links aus ALLEN Pools des Coins (seit 2.49.6): DexScreener haengt neue Socials nicht immer sofort an jedes Paar
    for x in [p] + [y for y in (eigene or ps) if y is not p]:
        xi = x.get("info") or {}
        for w in xi.get("websites") or []:
            if w.get("url"):
                links.setdefault("website", w["url"])
        for s in xi.get("socials") or []:
            if s.get("url") and s.get("type"):
                links.setdefault(s["type"], s["url"])
        if not info.get("imageUrl") and xi.get("imageUrl"):
            info = dict(info, imageUrl=xi["imageUrl"])
    # Volumen und Kaeufe/Verkaeufe ueber ALLE Pools des Coins auf dieser Chain (seit 2.44), nicht nur das groesste Paar
    zaehl = eigene or [p]
    vol, tx = {}, {}
    for k in ("m5", "h1", "h6", "h24"):
        vs = [_f((x.get("volume") or {}).get(k)) for x in zaehl]
        vol[k] = round(sum(v for v in vs if v), 2) if any(v is not None for v in vs) else None
        ts = [(x.get("txns") or {}).get(k) for x in zaehl if (x.get("txns") or {}).get(k)]
        if ts:
            tx[k] = {"buys": sum(int(t.get("buys") or 0) for t in ts), "sells": sum(int(t.get("sells") or 0) for t in ts)}
    return {"price": _f(p.get("priceUsd")), "mcap": _f(p.get("marketCap")) or _f(p.get("fdv")), "fdv": _f(p.get("fdv")),
            "liq": _liq(p) or None, "vol": vol, "txns": tx,
            # Seit 2.62.8: beide Seiten des Pools einzeln, damit die Wache Verkaeufe von einem LP-Abzug unterscheidet
            "liq_base": _f((p.get("liquidity") or {}).get("base")), "liq_quote": _f((p.get("liquidity") or {}).get("quote")),
            "change": {k: _f(v) for k, v in (p.get("priceChange") or {}).items() if k in ("m5", "h1", "h6", "h24")},
            "pair": p.get("pairAddress"), "url": p.get("url"), "dex": p.get("dexId"),
            "quote": (p.get("quoteToken") or {}).get("symbol"),
            "symbol": (p.get("baseToken") or {}).get("symbol"), "name": (p.get("baseToken") or {}).get("name"),
            "image": info.get("imageUrl"), "created_ms": alt, "links": links, "pairs": len(ps),
            "liq_all": round(sum(_liq(x) for x in ps), 2)}


def _lp_evm(chain, token, mk):
    """-> {"pool", "kind": "v2"|"v3"|"v4", "supply_pct", "burned_pct"} oder None."""
    pair = str(mk.get("pair") or "").lower()
    if not pair:
        return None
    import preise
    sup, dez = preise.supply(chain, token)
    if not sup:
        return None

    def bal(tok, wer):
        h = _evm(chain, "eth_call", [{"to": tok, "data": "0x70a08231" + wer[2:].rjust(64, "0")}, "latest"])
        return int(h, 16) if h and h != "0x" else 0
    out = {"pool": pair}
    if len(pair) == 66:  # Uniswap V4: Pool-Id, das Geld liegt im PoolManager
        import intel
        pm = (intel.POOLS_EVM.get(chain) or (None,))[0]
        if not pm:
            return None
        out["kind"] = "v4"
        out["supply_pct"] = round(bal(token, pm) / 10 ** dez / sup * 100, 1)
        return out
    out["supply_pct"] = round(bal(token, pair) / 10 ** dez / sup * 100, 1)
    try:  # V2: der Pool ist selbst ein Token (LP), getReserves gibt es nur dort
        rs = _evm(chain, "eth_call", [{"to": pair, "data": "0x0902f1ac"}, "latest"])
        v2 = bool(rs and len(rs) >= 194)
    except Exception:
        v2 = False
    if v2:
        out["kind"] = "v2"
        ts = int(_evm(chain, "eth_call", [{"to": pair, "data": "0x18160ddd"}, "latest"]) or "0x0", 16)
        if ts:
            weg = bal(pair, "0x000000000000000000000000000000000000dead") + bal(pair, "0x" + "0" * 40)
            out["burned_pct"] = round(min(1.0, weg / ts) * 100, 1)
    else:
        out["kind"] = "v3"
    return out


# ── Sicherheit EVM: GoPlus ───────────────────────────────────────────────

_GP = {}


def goplus(chain, adr):
    gid = KETTEN.get(chain, (None,) * 4)[3]
    if not gid:
        return None
    c = _GP.get((chain, adr))
    if c and time.time() - c[0] < 120:
        return c[1]
    r = None
    try:
        d = requests.get(f"https://api.gopluslabs.io/api/v1/token_security/{gid}",
                         params={"contract_addresses": adr}, headers=UA, timeout=15).json()
        res = d.get("result") or {}
        r = res.get(adr.lower()) or (list(res.values())[0] if res else None)
    except Exception as e:
        log.debug(f"scan goplus {adr}: {e}")
    _GP[(chain, adr)] = (time.time(), r)
    return r


def _ja(v):
    return str(v) == "1"


def sicherheit_evm(g):
    if not g:
        return {}
    out = {"honeypot": _ja(g.get("is_honeypot")) if g.get("is_honeypot") not in (None, "") else None,
           "buy_tax": _f(g.get("buy_tax")), "sell_tax": _f(g.get("sell_tax")),
           "mintable": _ja(g.get("is_mintable")), "open_source": _ja(g.get("is_open_source")) if g.get("is_open_source") not in (None, "") else None,
           "proxy": _ja(g.get("is_proxy")), "take_back": _ja(g.get("can_take_back_ownership")),
           "hidden_owner": _ja(g.get("hidden_owner")), "pausable": _ja(g.get("transfer_pausable")),
           "blacklist": _ja(g.get("is_blacklisted")), "cannot_sell_all": _ja(g.get("cannot_sell_all")),
           "slippage_mod": _ja(g.get("slippage_modifiable")),
           "owner": g.get("owner_address") or None, "creator": g.get("creator_address") or None,
           "creator_pct": (_f(g.get("creator_percent")) or 0) * 100 if g.get("creator_percent") not in (None, "") else None,
           "holders": int(_f(g.get("holder_count")) or 0) or None}
    for k in ("buy_tax", "sell_tax"):
        if out[k] is not None:
            out[k] = round(out[k] * 100, 2)
    tot = 0.0
    for lp in g.get("lp_holders") or []:
        p = _f(lp.get("percent")) or 0
        a = str(lp.get("address") or "").lower()
        if str(lp.get("is_locked")) == "1" or a.endswith("dead") or a == "0x" + "0" * 40:
            tot += p
    if g.get("lp_holders"):
        out["lp_locked_pct"] = round(min(1.0, tot) * 100, 1)
    out["owner_renounced"] = (not out["owner"]) or out["owner"] in ("0x" + "0" * 40, "0x000000000000000000000000000000000000dead")
    top = []
    for h in g.get("holders") or []:
        top.append({"address": str(h.get("address") or "").lower(), "pct": round((_f(h.get("percent")) or 0) * 100, 2),
                    "pool": bool(h.get("is_contract")) and not str(h.get("is_locked")) == "1",
                    "locked": str(h.get("is_locked")) == "1", "label": (h.get("tag") or "")[:30] or None})
    out["top"] = top[:15]
    return out


# ── Sicherheit Solana: Mint-Konto ────────────────────────────────────────

_SM = {}


def sicherheit_sol(adr):
    c = _SM.get(adr)
    if c and time.time() - c[0] < 300:
        return c[1]
    out = {}
    try:
        import holder
        r = holder._sol_rpc("getAccountInfo", [adr, {"encoding": "jsonParsed"}])
        v = (r or {}).get("value") or {}
        d = (v.get("data") or {})
        info = ((d.get("parsed") or {}).get("info") or {}) if isinstance(d, dict) else {}
        if info:
            out = {"mint_revoked": info.get("mintAuthority") is None, "freeze_revoked": info.get("freezeAuthority") is None,
                   "decimals": info.get("decimals"), "supply_raw": info.get("supply"),
                   "token2022": (d.get("program") == "spl-token-2022")}
            for e in info.get("extensions") or []:
                art = e.get("extension")
                st = e.get("state") or {}
                if art == "transferFeeConfig":
                    bps = ((st.get("newerTransferFee") or {}).get("transferFeeBasisPoints")
                           or (st.get("olderTransferFee") or {}).get("transferFeeBasisPoints") or 0)
                    out["transfer_fee_pct"] = round(float(bps) / 100, 2)
                elif art == "permanentDelegate" and st.get("delegate"):
                    out["permanent_delegate"] = True
                elif art == "nonTransferable":
                    out["non_transferable"] = True
                elif art == "transferHook" and st.get("programId"):
                    out["transfer_hook"] = True
                elif art == "tokenMetadata":
                    out["symbol"] = st.get("symbol")
                    out["name"] = st.get("name")
                    out["uri"] = st.get("uri")
    except Exception as e:
        log.debug(f"scan sol mint {adr}: {e}")
    _SM[adr] = (time.time(), out)
    return out


_RC = {}


def rugcheck_sol(mint):
    """RugCheck-Bericht (frei, ohne Schluessel), 5 Minuten im Speicher. Liefert Ersteller, Halterzahl,
    die groessten Halter mit Eigentuemer und benannte Konten (Pools, Locker). Seit 2.49.4."""
    c = _RC.get(mint)
    if c and time.time() - c[0] < 300:
        return c[1]
    out = None
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", headers=UA, timeout=12)
        if r.status_code == 200:
            d = r.json() or {}
            bekannt = {a: (v or {}) for a, v in (d.get("knownAccounts") or {}).items() if isinstance(v, dict)}
            pool_konten = set()
            for m in d.get("markets") or []:
                if isinstance(m, dict):
                    for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
                        v = m.get(k)
                        if isinstance(v, str):
                            pool_konten.add(v)
            top = []
            for h in d.get("topHolders") or []:
                o, k = h.get("owner") or h.get("address"), h.get("address")
                art = str((bekannt.get(o) or bekannt.get(k) or {}).get("type") or "").upper()
                name = (bekannt.get(o) or bekannt.get(k) or {}).get("name")
                top.append({"address": o, "pct": round(float(h.get("pct") or 0), 3),
                            "pool": art == "AMM" or k in pool_konten or o in pool_konten,
                            "locked": art == "LOCKER", "locker": name if art == "LOCKER" else None,
                            "insider": bool(h.get("insider"))})
            out = {"creator": d.get("creator") or None, "holders": d.get("totalHolders") or None,
                   "top": top, "known": bekannt}
    except Exception as e:
        log.debug(f"rugcheck {mint}: {e}")
    _RC[mint] = (time.time(), out)
    if len(_RC) > 300:
        for k in sorted(_RC, key=lambda x: _RC[x][0])[:150]:
            _RC.pop(k, None)
    return out


def _sol_beschriften(r, rc):
    """Pools und Locker aus RugCheck auf die Halterliste legen, dann Top 10 und groessten Halter neu rechnen."""
    if not rc:
        return
    sp = r["supply"]
    namen = rc.get("known") or {}
    rc_top = {x["address"]: x for x in rc.get("top") or []}
    liste = sp.get("top") or []
    if not liste and rc.get("top"):
        liste = [dict(x) for x in rc["top"][:15]]
    for x in liste:
        a = x.get("address")
        art = str((namen.get(a) or {}).get("type") or "").upper()
        q = rc_top.get(a) or {}
        try:
            import intel
            bek_pool = a in intel.POOL_AUTH
        except Exception:
            bek_pool = False
        if art == "AMM" or q.get("pool") or bek_pool:
            x["pool"], x["locked"] = True, False
        elif art == "LOCKER" or q.get("locked"):
            x.update(locked=True, pool=False, locker=x.get("locker") or (namen.get(a) or {}).get("name") or q.get("locker"))
    sp["top"] = liste
    marken = {x.get("address"): x for x in liste}
    for x in (r.get("map") or {}).get("holders") or []:
        m = marken.get(x.get("address"))
        if m:
            x.update(pool=m.get("pool"), locked=m.get("locked"), locker=m.get("locker"))
    echt = [x for x in liste if not x.get("pool") and not x.get("locked")]
    if echt:
        sp["top10_pct"] = round(sum(float(x["pct"]) for x in echt[:10]), 2)
        sp["largest_pct"] = round(float(echt[0]["pct"]), 2)
    if rc.get("holders") and not sp.get("holders"):
        sp["holders"] = rc["holders"]
    dv = r["dev"]
    if rc.get("creator") and not dv.get("address"):
        dv.update(address=rc["creator"], explorer=explorer("solana", "address", rc["creator"]))
    if dv.get("address") and dv.get("holds_pct") is None:
        im_top = [x for x in rc.get("top") or [] if x["address"] == dv["address"]]
        if im_top:
            dv["holds_pct"] = round(sum(x["pct"] for x in im_top), 3)


def top_sol(adr, supply_raw):
    """Ohne Tiefen-Schicht: die groessten Halter mit Eigentuemer, Pools und Locker erkannt (seit 2.49.4).
    Vorher waren es nackte Token-Konten, ein Pool erschien dann als 'Holder 26%'."""
    try:
        import intel
        sup = int(float(supply_raw or 0))
        if not sup:
            return []
        out = []
        for h in intel._halter_sol(adr, sup)[:15]:
            x = {"address": h["address"], "pct": round(h["pct"], 2), "pool": bool(h.get("pool"))}
            if h.get("locked"):
                x.update(locked=True, locker=h.get("locker"), pool=False)
            out.append(x)
        return out
    except Exception as e:
        log.debug(f"scan top_sol {adr}: {e}")
        return []


# ── Urteil ───────────────────────────────────────────────────────────────

def urteil(r):
    gut, schlecht, kritisch = [], [], []
    s, sp, m = r.get("safety") or {}, r.get("supply") or {}, r.get("market") or {}
    if s.get("honeypot"):
        kritisch.append("Honeypot: buys work, sells fail")
    if s.get("cannot_sell_all"):
        kritisch.append("You cannot sell your whole bag")
    st, bt = s.get("sell_tax"), s.get("buy_tax")
    if st is not None and st >= 25:
        kritisch.append(f"Sell tax {zahl(st)}%")
    elif (st or 0) >= 6 or (bt or 0) >= 6:
        schlecht.append(f"Tax {zahl(bt or 0)}% buy / {zahl(st or 0)}% sell")
    if s.get("freeze_revoked") is False:
        kritisch.append("Freeze authority is still active (your tokens can be frozen)")
    if s.get("permanent_delegate"):
        kritisch.append("Permanent delegate: someone can move your tokens")
    if s.get("take_back") or s.get("hidden_owner"):
        kritisch.append("Owner can take control back" if s.get("take_back") else "Hidden owner in the contract")
    if s.get("mint_revoked") is False or s.get("mintable"):
        schlecht.append("Supply can still be minted")
    if s.get("pausable") or s.get("blacklist"):
        schlecht.append("Owner can pause or blacklist transfers")
    if (s.get("transfer_fee_pct") or 0) >= 3:
        schlecht.append(f"Transfer fee {zahl(s['transfer_fee_pct'])}%")
    if s.get("open_source") is False:
        schlecht.append("Contract not verified")
    lp = s.get("lp_locked_pct")
    if s.get("lp_launchpad"):
        gut.append(f"Liquidity held by the launchpad ({s['lp_launchpad']}), not by a team wallet")
    jung = (r.get("age_sec") or 0) < 30 * 86400 or (m.get("mcap") or 0) < 5e6
    vk = s.get("lp_in_contracts_pct") or 0
    if lp is not None and m.get("liq") and lp < 50 and r.get("stage_curve") is not True and jung:
        # seit 2.81.1: Liquiditaet in Hook- oder anderen Vertraegen ist kein "nicht gelockt", wir koennen den Lock
        # dort nur nicht lesen. Dann sagen wir genau das.
        if vk >= 10:
            schlecht.append(f"{zahl(lp)}% of LP in a locker we can read, {zahl(vk)}% sits in hook or other contracts")
        else:
            schlecht.append(f"Only {zahl(lp)}% of LP locked or burned")
    b = sp.get("bundled_total_pct") if sp.get("bundled_total_pct") is not None else sp.get("bundled_pct")
    if b is not None:
        if b >= 25:
            kritisch.append(f"{zahl(b)}% of supply sits in bundled wallets" + (" or wallets they passed tokens to" if (sp.get("bundled_fwd_pct") or 0) >= 0.1 else ""))
        elif b >= 10:
            schlecht.append(f"{zahl(b)}% bundled")
        elif b < 3 and (sp.get("launch_bought_pct") or 0) < 3:
            # seit 2.82.18: die Zahl sagt, was gebuendelte Wallets HEUTE halten, nicht ob es beim Start ein Buendel gab
            gut.append("Bundled wallets hold under 3% of supply now")
    # Seit 2.78.0: Launch-Block nach Kaufmenge und das Funder-Netz um den Dev
    try:
        import funder
        for stufe, txt in funder.zeilen(r.get("funder_net"), sp.get("launch_buys")):
            (kritisch if stufe == "kritisch" else schlecht).append(txt)
    except Exception as e_:
        log.debug(f"urteil funder: {e_}")
    fr = sp.get("fresh_pct")
    if fr is not None and fr >= 30:
        schlecht.append(f"{zahl(fr)}% of early buyers are fresh wallets")
    pw = sp.get("prepared") or {}
    if (pw.get("count") or 0) >= int(os.getenv("PREP_MIN_WALLETS", "5")) and (pw.get("pct_of_buyers") or 0) >= 25:
        txt = (f"{pw['count']} of the first {pw['of']} buyers look like prepared wallets"
               + (f" (little history, {pw['exchange_funded']} funded from exchanges)" if pw.get("exchange_funded") else " (little history, aged just enough)")
               + (f", holding {zahl(pw['supply_pct'])}%" if (pw.get("supply_pct") or 0) >= 1 else ""))
        (kritisch if (pw.get("pct_of_buyers") or 0) >= 40 and (pw.get("supply_pct") or 0) >= 5 else schlecht).append(txt)
    sf = sp.get("same_funder") or 0
    cl = sp.get("clusters") or 0
    if sf >= 2:
        schlecht.append(f"{sf} wallet groups funded by the same source")
    elif cl >= 2:
        schlecht.append(f"{cl} wallet groups that bought in sync (not in the same block)")
    if (sp.get("snipers") or 0) >= 5:
        schlecht.append(f"{sp['snipers']} snipers in the first block")
    t10 = sp.get("top10_pct")
    if t10 is not None:
        if t10 >= 50:
            schlecht.append(f"Top 10 wallets hold {zahl(t10)}%")
        elif t10 <= 20:
            gut.append(f"Top 10 hold only {zahl(t10)}%")
    lg = sp.get("largest_pct")
    if lg is not None and lg >= 10 and not sp.get("largest_locked"):
        schlecht.append(f"One wallet holds {zahl(lg)}%")
    dv = r.get("dev") or {}
    fw0 = r.get("forwards") or {}
    dout = dv.get("out") or {}
    dev_weg = (fw0.get("dev_forwarded_pct") or 0) >= 1 or (dout.get("forwarded_pct") or 0) >= 1
    if (dv.get("holds_pct") or 0) >= 5:
        schlecht.append(f"Dev still holds {zahl(dv['holds_pct'])}%")
    elif dv.get("holds_pct") is not None and dv["holds_pct"] < 0.5 and dv.get("address") and not dev_weg:
        gut.append("Dev holds nothing")
    if (dv.get("launches_seen") or 0) >= 3 and not dv.get("launches_ran"):
        schlecht.append(f"Dev launched {dv['launches_seen']} coins before, none ran")
    elif dv.get("launches_ran"):
        gut.append(f"Dev had {dv['launches_ran']} earlier coin{'s' if dv['launches_ran'] != 1 else ''} that ran")
    if s.get("mint_revoked") and s.get("freeze_revoked"):
        gut.append("Mint and freeze revoked")
    if s.get("honeypot") is False and (st or 0) < 1 and (bt or 0) < 1 and not s.get("mintable"):
        gut.append("No honeypot, no tax")
    if lp is not None and lp >= 90:
        gut.append(f"LP {zahl(lp)}% locked or burned")
    if (sp.get("locked_pct") or 0) >= 1:
        bis = sp.get("unlock_ts")
        if bis and bis - time.time() < 7 * 86400:
            schlecht.append(f"{zahl(sp['locked_pct'])}% of supply unlocks {('in ' + str(max(0, int((bis - time.time()) // 86400))) + ' days') if bis > time.time() else 'now'}")
        else:
            gut.append(f"{zahl(sp['locked_pct'])}% of supply locked" + (f" until {time.strftime('%d %b %Y', time.gmtime(bis))}" if bis else ""))
    # Schnell-Check aus dem normalen Scan, solange der Tiefen-Scan die Weiterleitungen noch nicht hat
    if (dout.get("forwarded_pct") or 0) >= 1 and not (fw0.get("from_dev") and (fw0.get("dev_forwarded_pct") or 0) >= 1):
        n_an = len(dout.get("to") or [])
        noch_q = round(sum(x.get("holds_pct") or 0 for x in dout.get("to") or []), 3)
        wo = "another wallet" if n_an == 1 else f"{n_an} other wallets"
        zeile = (f"Dev moved {zahl(dout['forwarded_pct'])}% to {wo} that still hold{'s' if n_an == 1 else ''} {zahl(noch_q)}%"
                 if noch_q >= 1 else f"Dev sent {zahl(dout['forwarded_pct'])}% of supply to other wallets instead of selling")
        (kritisch if dout["forwarded_pct"] >= 5 else schlecht).append(zeile)
        # Ist der groesste Holder genau dieses Empfaenger-Wallet, reicht die Dev-Zeile
        if lg is not None and any(abs((x.get("holds_pct") or 0) - lg) < 0.1 for x in dout.get("to") or []):
            schlecht[:] = [x for x in schlecht if not x.startswith("One wallet holds")]
    # Weiterleitungen (seit 2.46): Tokens ohne Verkauf an andere Wallets geschickt
    fw = r.get("forwards") or {}
    if fw.get("count"):
        dfp = fw.get("dev_forwarded_pct") or 0
        noch = 0
        if fw.get("from_dev") and dfp >= 1:
            bei = {}
            for p in fw.get("list") or []:
                if p.get("src") == "dev" and p.get("holds_pct") is not None:
                    bei[p["to"]] = p["holds_pct"]
            noch = round(sum(bei.values()), 3)
            if noch >= 1:
                (kritisch if dfp >= 5 else schlecht).append(
                    f"Dev moved {zahl(dfp)}% to {'another wallet' if len(bei) == 1 else str(len(bei)) + ' other wallets'} "
                    f"that still hold{'s' if len(bei) == 1 else ''} {zahl(noch)}%")
            elif (fw.get("dev_burned_pct") or 0) >= dfp * 0.9:
                # seit 2.60.6: weitergeleitet und dort verbrannt ist kein Risiko, sondern weniger Supply
                gut.append(f"The {zahl(dfp)}% the dev moved out was burned")
            else:
                (kritisch if dfp >= 5 else schlecht).append(f"Dev sent {zahl(dfp)}% of supply to other wallets instead of selling")
        if (fw.get("still_held_pct") or 0) - noch >= 3:
            # seit 2.63: ohne den Teil der Dev-Empfaenger, der steht schon in der Zeile darueber
            rest_ = round((fw.get("still_held_pct") or 0) - noch, 2)
            schlecht.append(f"{'Other wallets' if noch else 'Wallets'} that got tokens without buying still hold {zahl(rest_)}%"
                            + (f" ({zahl(fw['still_held_pct'])}% with the dev's)" if noch else ""))
        if fw.get("collectors_elsewhere"):
            n_, mx = fw["collectors_elsewhere"], fw.get("collectors_elsewhere_max") or 0
            schlecht.append(f"{n_} wallet{'s' if n_ != 1 else ''} collecting forwarded supply did the same on other coins"
                            + (f" (one on {mx})" if mx > 1 else ""))
        if (fw.get("to_fresh") or 0) >= 3:
            schlecht.append(f"Tokens forwarded to {fw['to_fresh']} fresh wallets")
    elif fw.get("checked") and fw.get("count") == 0:
        gut.append("No tokens forwarded to hidden wallets")
    sm = r.get("smart") or {}
    if (sm.get("found") or 0) > 0:
        gut.append(f"{sm['found']} wallet{'s' if sm['found'] != 1 else ''} with a strong track record among the "
                   + ("top holders" if sm.get("top_holders") else "first buyers"))
    liq = m.get("liq")
    if liq is not None and liq < 5000 and r.get("stage_curve") is not True:
        schlecht.append(f"Thin liquidity ({usd(liq)})")
    h1 = (m.get("txns") or {}).get("h1") or {}
    kb, vk = h1.get("buys") or 0, h1.get("sells") or 0
    if kb + vk >= 20:
        if vk > kb * 1.6:
            schlecht.append(f"Sells dominate the last hour ({vk} vs {kb} buys)")
        elif kb > vk * 1.6:
            gut.append(f"Buyers lead the last hour ({kb} vs {vk})")
    tw = r.get("twins") or {}
    if tw.get("likely_copy") and str(r.get("address") or "").lower() not in EIGENE:
        schlecht.append(f"Copycat risk: an older, bigger ${r.get('symbol')} exists")
    if r.get("renamed"):
        schlecht.append("Name on chain differs from the listing")
    sc = r.get("risk_score")
    lv = str(r.get("risk_level") or "").upper()
    for n in r.get("notes") or []:
        nl = n.lower()
        if "bundle pattern" in nl or "forwarded" in nl or "sniper pattern" in nl:
            schlecht.append(n[0].upper() + n[1:])
            break
    sb = r.get("seen_before") or {}
    if (sb.get("group") and sb.get("coins_dead")) or any(x.get("dead_after", 0) >= 2 for x in sb.get("standouts") or []):
        schlecht.append("Wallets from earlier coins that fell hard are in this one too")
    if kritisch:
        stufe = "red"
    elif len(schlecht) >= 3 or (sc is not None and sc < 50):
        stufe = "red" if len(schlecht) >= 5 else "amber"
    elif schlecht or (sc is not None and sc < 70) or lv == "MEDIUM":
        stufe = "amber"
    else:
        stufe = "green"
    titel = {"green": "Looks clean so far", "amber": "Mixed, check the flags", "red": "High risk"}[stufe]
    vorne = kritisch + schlecht
    if stufe == "green":
        grund = ", ".join(gut[:3]) or "Nothing stands out in the data"
    elif gut and vorne:
        grund = f"{', '.join(gut[:2])}. But {vorne[0][0].lower() + vorne[0][1:]}" + (f", and {vorne[1][0].lower() + vorne[1][1:]}" if len(vorne) > 1 else "")
    else:
        grund = ", ".join(vorne[:3])
    return {"level": stufe, "title": titel, "reason": grund + ".", "good": gut, "bad": vorne}


# ── Alles zusammen ───────────────────────────────────────────────────────

_CACHE = {}


def analyse(q, deep=None, frisch=False):
    """Der Report. deep=None: Tiefen-Scan automatisch fuer junge oder kleine Coins starten."""
    hinweis, a = lesen(q)
    if not a:
        return {"ok": False, "error": "Give me a contract address, a chart link or a ticker."}
    gleichnamige = None
    if not ist_adresse(a):
        f = ticker_finden(a, hinweis)
        if f and f.get("ambiguous"):
            return {"ok": False, "ambiguous": True, "symbol": f["symbol"], "candidates": f["candidates"],
                    "error": f"There are several coins called ${f['symbol']}. Send the contract address of the one you mean."}
        if f and f.get("major"):
            return {"ok": False, "major": True, "error": f"{f['symbol']} is a major coin with its own chain, not a token contract. Use the price and chart tools for it."}
        if not f:
            return {"ok": False, "error": f"I could not find '{a}'. Paste the contract address (works for Solana, Base, Ethereum, BNB Chain, Arbitrum, Robinhood Chain, Elysium, TRON and TON)."}
        a, hinweis, gleichnamige = f["address"], f["chain"], f.get("others")
    ck = (hinweis, a)
    c = _CACHE.get(ck)
    if c and not frisch and time.time() - c[0] < 25 and deep is not True:
        return c[1]
    paare = _paare(a)
    chain = chain_waehlen(a, paare, hinweis)
    mk = markt(a, chain, paare)
    k = KETTEN[chain]
    r = {"ok": True, "chain": chain, "chain_name": k[0], "address": a, "symbol": mk.get("symbol"), "name": mk.get("name"),
         "image": mk.get("image"), "market": mk, "safety": {}, "supply": {}, "dev": {}, "smart": {},
         "links": {"chart": mk.get("url"), "explorer": explorer(chain, "token", a),
                   "page": f"{SEITE}/#token={chain}:{a}"}}
    if gleichnamige:
        r["same_ticker_elsewhere"] = gleichnamige
    weitere = sorted({DEX_ZU.get(p.get("chainId")) for p in paare if DEX_ZU.get(p.get("chainId")) and DEX_ZU.get(p.get("chainId")) != chain})
    if weitere:
        r["also_on"] = [KETTEN[x][0] for x in weitere]
    if mk.get("created_ms"):
        r["age_sec"] = int(time.time() - mk["created_ms"] / 1000)
    dex = mk.get("dex")
    if dex:
        r["venue"] = DEX_NAMEN.get(dex, dex.replace("-", " ").title() if dex.islower() else dex)
        r["stage_curve"] = dex in ("pumpfun", "launchlab", "moonshot", "meteoradbc", "boop", "bonk", "believe")
    # Sicherheit je Chain
    if chain == "solana":
        s = sicherheit_sol(a)
        r["safety"].update({x: s.get(x) for x in ("mint_revoked", "freeze_revoked", "transfer_fee_pct",
                                                   "permanent_delegate", "non_transferable", "transfer_hook") if s.get(x) is not None})
        r["symbol"] = r["symbol"] or s.get("symbol")
        r["name"] = r["name"] or s.get("name")
        sol_supply = s.get("supply_raw")
        # Dev: bei pump.fun-Coins steht der Ersteller in der Bonding Curve (seit 2.49.4)
        try:
            import intel
            ersteller = intel.dev_sol(a)
            if ersteller:
                r["dev"].update(address=ersteller, explorer=explorer(chain, "address", ersteller))
                roh = intel.bestand_sol(ersteller, a)
                if roh is not None and sol_supply:
                    r["dev"]["holds_pct"] = round(roh / float(sol_supply) * 100, 3)
        except Exception as e:
            log.debug(f"scan dev_sol {a}: {e}")
    elif chain == "robinhood":
        try:
            import onchain
            t = onchain.token(a)
        except Exception as e:
            t = {"error": str(e)}
        if not t.get("error"):
            r["symbol"] = r["symbol"] or t.get("symbol")
            r["name"] = r["name"] or t.get("name")
            if t.get("creator"):
                r["dev"].update(address=t["creator"], holds_pct=t.get("creator_holds_pct"),
                                explorer=t.get("creator_explorer"))
            if t.get("holders") is not None:
                r["supply"]["holders"] = t["holders"]
            if t.get("top10_wallets_pct") is not None:
                r["supply"]["top10_pct"] = t["top10_wallets_pct"]
                r["supply"]["largest_pct"] = t.get("largest_holder_pct")
            if t.get("top_list"):
                r["supply"]["top"] = t["top_list"][:15]
            for lk in t.get("locked") or []:
                tops = r["supply"].setdefault("top", [])
                if not any(x.get("address") == lk["address"] for x in tops):
                    tops.append({"address": lk["address"], "pct": lk["pct"], "pool": False, "locked": True, "locker": lk["locker"],
                                 "unlock_ts": lk.get("unlock_ts")})
                else:
                    for x in tops:
                        if x.get("address") == lk["address"] and lk.get("unlock_ts"):
                            x.update(locked=True, locker=lk["locker"], unlock_ts=lk["unlock_ts"], pool=False)
            if t.get("burned_pct_of_supply"):
                r["burned_pct"] = t["burned_pct_of_supply"]
            if t.get("torch"):
                r["torch"] = t["torch"]
                if not t["torch"].get("graduated"):
                    r["stage_curve"] = True
                    r["venue"] = "Torch curve"
                elif not r.get("venue"):
                    r["venue"] = "Torch, graduated"
            if t.get("launched_on_torch") and t.get("launched_utc") and not r.get("age_sec"):
                try:
                    import calendar
                    r["age_sec"] = int(time.time() - calendar.timegm(time.strptime(t["launched_utc"], "%Y-%m-%d %H:%M UTC")))
                except Exception:
                    pass
            if not mk.get("price") and t.get("price_usd"):
                mk.update(price=t.get("price_usd"), mcap=t.get("market_cap_usd"), liq=t.get("liquidity_usd"))
            r["links"]["trade_torch"] = t.get("trade")
            if t.get("creator"):
                try:
                    import onchain
                    frueher = [l for l in onchain.launches() if l.get("creator") == t["creator"] and l["token"] != a]
                    r["dev"]["launches_seen"] = len(frueher)
                except Exception:
                    pass
    elif chain == "elysium":
        try:
            import elysium
            e = elysium.scan(a)
            if not e.get("error"):
                if e.get("dev"):
                    r["dev"].update(address=e["dev"], explorer=explorer(chain, "address", e["dev"]),
                                    holds_pct=e.get("dev_holds_pct"))
                    if e.get("dev_funded_by"):
                        df = e["dev_funded_by"]
                        r["dev"]["funded_by"] = None if df.get("self") else df.get("funder")
                        r["dev"]["funding"] = {k: df.get(k) for k in ("hype", "bridge", "self", "funder_origin") if df.get(k) is not None}
                if e.get("top_holders"):
                    r["supply"]["top"] = [{"address": h["wallet"], "pct": h["pct"]} for h in e["top_holders"][:10]]
                    r["supply"]["top10_pct"] = e.get("top10_wallets_pct")
                if e.get("first_block_buyers") is not None:
                    r["supply"]["first_block_buyers"] = e["first_block_buyers"]
                r["elysium"] = {k: e.get(k) for k in ("funding_clusters", "early_buyers_funded_by_dev_side", "created_via",
                                                       "same_block_groups", "connections_summary", "first_block_buyers",
                                                       "holders_note", "warning") if e.get(k) is not None}
                if not r.get("symbol"):
                    r["symbol"], r["name"] = e.get("symbol"), e.get("name")
                em = e.get("market") or {}
                if not mk and em.get("price_usd"):
                    mk = {"price": em.get("price_usd"), "mcap": _f(em.get("mcap_usd")), "fdv": _f(em.get("fdv_usd")),
                          "liq": _f(em.get("liquidity_usd")), "url": em.get("chart"), "dex": em.get("dex"),
                          "source": em.get("source")}
                    r["market"] = mk
                    r["links"]["chart"] = em.get("chart")
            r["safety"]["note"] = "No contract safety check covers Elysium yet (GoPlus does not support it)."
        except Exception as ex:
            log.debug(f"scan elysium {a}: {ex}")
    elif chain == "ton":
        try:
            import tronton
            j = tronton.jetton(a) or {}
            r["symbol"], r["name"] = r["symbol"] or j.get("symbol"), r["name"] or j.get("name")
            r["image"] = r.get("image") or j.get("image")
            if j.get("mintable") is not None:
                r["safety"]["mintable"] = j["mintable"]
            if j.get("holders"):
                r["supply"]["holders"] = j["holders"]
            if j.get("admin_revoked"):
                r["safety"]["owner_renounced"] = True
            if j.get("admin"):
                r["jetton_admin"] = {"address": j["admin"], "explorer": explorer(chain, "address", j["admin"])}
        except Exception as ex:
            log.debug(f"scan ton {a}: {ex}")
    else:
        g = sicherheit_evm(goplus(chain, a))
        if g:
            r["safety"].update({x: g.get(x) for x in ("honeypot", "buy_tax", "sell_tax", "mintable", "open_source", "proxy",
                                                       "take_back", "hidden_owner", "pausable", "blacklist", "cannot_sell_all",
                                                       "lp_locked_pct", "owner_renounced") if g.get(x) is not None})
            if g.get("holders"):
                r["supply"]["holders"] = g["holders"]
            if g.get("creator"):
                r["dev"].update(address=g["creator"], holds_pct=round(g["creator_pct"], 3) if g.get("creator_pct") is not None else None,
                                explorer=explorer(chain, "address", g["creator"]))
            if g.get("top"):
                r["supply"]["top"] = g["top"]
    # Tiefe: eigene Erkennung (intel.py), Ergebnis aus dem Cache oder im Hintergrund
    import intel
    # Seit 2.58.1: automatisch fuer jeden Coin unter DEEP_MAX_MCAP (Standard 100 Mio. $), egal wie alt.
    # Vorher nur bis 14 Tage Alter: bei aelteren Coins (TORCH, TIT) kam der Tiefen-Scan nie von selbst.
    starten = chain not in OHNE_TIEFE and (deep is True or (deep is None and (mk.get("mcap") or 0) < float(os.getenv("DEEP_MAX_MCAP", "100000000"))))
    it = intel.tiefe(chain, a, mk.get("mcap"), r.get("symbol"), starten=starten)
    r["deep"] = {"state": it.get("state"), "age_min": it.get("age_min"), "why": it.get("why"), "basis": it.get("basis")}
    if it.get("state") == "ready" and it.get("measured"):
        sp = r["supply"]
        for x, y in (("bundled_supply_pct", "bundled_pct"), ("fresh_wallet_pct", "fresh_pct"), ("clusters", "clusters"),
                     ("same_funder_groups", "same_funder"), ("snipers", "snipers"), ("cluster_supply_pct", "cluster_pct"),
                     ("still_holding_pct", "early_holding_pct"), ("bundled_supply_incl_forwarded_pct", "bundled_total_pct"),
                     ("bundled_forwarded_pct", "bundled_fwd_pct"), ("forward_max_hops", "fwd_hops")):
            if it.get(x) is not None and not (str(it.get("basis") or "").startswith("top") and y in ("bundled_pct", "snipers", "early_holding_pct")):
                sp[y] = it[x]
        if (it.get("prepared") or {}).get("checked"):
            sp["prepared"] = it["prepared"]
        lb_ = it.get("launch_buys") or {}
        if lb_.get("checked") and not str(it.get("basis") or "").startswith("top"):
            sp["launch_buys"] = lb_
            sp["launch_bought_pct"] = lb_.get("bought_pct")
        fn0 = it.get("funder_net") or {}
        if fn0.get("checked") or fn0.get("gaps"):
            r["funder_net"] = fn0
        elif fn0.get("state") == "running":
            r["funder_net"] = {"state": "running", "note": "The funder check is still running, about a minute. Refresh the scan to see it."}
        if it.get("dev"):
            if not r["dev"].get("address"):
                r["dev"].update(address=it["dev"], explorer=explorer(chain, "address", it["dev"]))
            if it.get("dev_holds_pct") is not None and r["dev"].get("holds_pct") is None:
                r["dev"]["holds_pct"] = it["dev_holds_pct"]
        if it.get("launcher"):
            r["dev"]["launcher"] = it["launcher"]
            r["dev"]["launcher_explorer"] = explorer(chain, "address", it["launcher"])
            r["dev"]["launcher_meaning"] = ("The coin was created through this factory or launcher contract. The dev is the "
                                            "wallet that called it. A launcher that created many coins is a tool used by "
                                            "many people, not the dev, so never count its other coins as the dev's launches.")
        dh = it.get("dev_history") or {}
        if dh.get("seen") is not None:
            r["dev"]["launches_seen"] = max(r["dev"].get("launches_seen") or 0, dh["seen"])
            r["dev"]["launches_ran"] = dh.get("ran")
        rb = it.get("rated") or {}
        if rb.get("checked"):
            # seit 2.77.1: als gutes Zeichen zaehlen nur Wallets mit starker Bilanz (SMART_MIN_SCORE, 50), nicht jede
            # Wallet mit irgendeinem Punktwert (Bots und Aggregatoren tauchen in vielen Coins auf)
            r["smart"] = {"found": rb.get("strong") if rb.get("strong") is not None else (rb.get("found") or 0),
                          "any": rb.get("found") or 0, "checked": rb["checked"], "best": rb.get("best"),
                          "top_holders": str(it.get("basis") or "").startswith("top")}
        hm = it.get("holders_map") or []
        if it.get("holders_count") and not sp.get("holders"):
            sp["holders"] = it["holders_count"]
        if hm:
            # Seit 2.62.5: bekannte Pool-Adressen (V4 PoolManager, Launchpad-Kurven) auch in alten Tiefen-Scans markieren
            try:
                import intel
                bek = {str(x).lower() for x in intel.POOLS_EVM.get(chain, ())} | set(intel.POOL_AUTH)
                for x in hm:
                    if str(x.get("address") or "").lower() in bek or x.get("address") in bek:
                        x["pool"], x["locked"] = True, False
            except Exception:
                pass
            echt = [x for x in hm if not x.get("pool") and not x.get("locked")]
            r["supply"]["top"] = hm[:15]
            r["supply"]["top10_pct"] = round(sum(x["pct"] for x in echt[:10]), 2)
            r["supply"]["largest_pct"] = echt[0]["pct"] if echt else None
            r["supply"].pop("top_note", None)
        if chain == "solana" and not sp.get("holders"):
            try:
                hz = intel.holder_zahl_sol(a)
                if hz:
                    sp["holders"] = hz
            except Exception:
                pass
        fw = it.get("forwards") or {}
        if fw.get("checked"):
            r["forwards"] = fw
        cl = it.get("cluster_map") or []
        von = {}
        for c in cl:
            for mm in c.get("members") or []:
                von.setdefault(str(mm).lower(), c.get("id"))
        basis = [dict(x) for x in (r["supply"].get("top") or [])]
        for x in basis:
            if von.get(str(x.get("address")).lower()):
                x["cluster"] = von[str(x["address"]).lower()]
        r["supply"]["top"] = basis or r["supply"].get("top")
        r["map"] = {"holders": basis[:40] + [{"address": b["wallet"], "pct": b["pct"], "pool": False, "cluster": b["cluster"]}
                                             for b in it.get("buyers") or [] if b.get("cluster") and b["pct"] > 0
                                             and str(b["wallet"]).lower() not in {str(x.get("address")).lower() for x in basis}][:25],
                    "clusters": [{"id": c.get("id"), "kind": "same funder" if c.get("type") == "funding" else "bought together",
                                  "supply_pct": c.get("supply_pct"), "wallets": c.get("wallets"), "funder": c.get("funder"),
                                  "exchange_like": False, "members": (c.get("members") or [])[:30]} for c in cl]}
        n = []
        if str(it.get("basis") or "").startswith("top"):
            n.append("Launch is too far back to read bundles; clusters and fresh wallets are checked on the top holders")
        if (it.get("snipers") or 0) >= 3:
            n.append(f"{it['snipers']} wallets bought in the launch block or right after")
        if (it.get("cluster_supply_pct") or 0) >= 5:
            n.append(f"Wallets in clusters still hold {zahl(it['cluster_supply_pct'])}% of supply")
        if it.get("still_holding_pct") is not None and it["still_holding_pct"] <= 25:
            n.append(f"None of the first {it.get('early_buyers')} buyers still hold" if not it["still_holding_pct"]
                     else f"Only {zahl(it['still_holding_pct'])}% of the first {it.get('early_buyers')} buyers still hold")
        if dh.get("ran"):
            n.append(f"Dev's earlier coins that ran: {', '.join(dh.get('examples') or [])}".rstrip(": "))
        r["notes"] = n
        try:  # seit 2.63: wo waren diese Wallets schon, was haben sie dort getan
            import muster
            m = muster.finden(chain, a, it)
            if m.get("known"):
                r["seen_before"] = m
        except Exception as e:
            log.debug(f"scan muster: {e}")
    # Solana ohne Tiefe: groesste Konten
    if chain == "solana" and not r["supply"].get("top") and locals().get("sol_supply"):
        top = top_sol(a, sol_supply)
        if top:
            r["supply"]["top"] = top
        if not r["supply"].get("holders"):
            try:
                import intel
                hz = intel.holder_zahl_sol(a)
                if hz:
                    r["supply"]["holders"] = hz
            except Exception:
                pass
    # Seit 2.62.5: Schutz vor falschen "One wallet holds X%"-Warnungen. Haelt eine angebliche Wallet ueber 20%,
    # pruefen wir frisch, ob dort Code liegt (Pool, Router, Vault). Ein Vertrag ist keine Person, ausser es ist ein Locker.
    if ist_evm(chain) and r["supply"].get("top"):
        try:
            import codeart
            geaendert = False
            for x in [x for x in r["supply"]["top"] if not x.get("pool") and not x.get("locked") and float(x.get("pct") or 0) >= 20][:3]:
                c = codeart.norm(_evm(chain, "eth_getCode", [x["address"], "latest"]))
                if c and c not in ("0x", "0x0"):
                    x["pool"], geaendert = True, True
            if geaendert:
                r["supply"]["top10_pct"] = None
        except Exception as e:
            log.debug(f"scan top check {a}: {e}")
    # Seit 2.62.7: LP auf EVM selbst lesen (GoPlus kennt Robinhood Chain nicht): wie viel vom Angebot im Pool liegt,
    # und bei V2-Pools, wie viel der LP-Tokens verbrannt ist. V3/V4 haben keine LP-Tokens, nur Positionen.
    if ist_evm(chain) and (r.get("market") or {}).get("pair"):
        try:
            r["lp"] = _lp_evm(chain, a, r["market"])
            lp_ = r["lp"] or {}
            if lp_.get("burned_pct") is not None and r["safety"].get("lp_locked_pct") is None:
                r["safety"]["lp_locked_pct"] = lp_["burned_pct"]
        except Exception as e:
            log.debug(f"scan lp {a}: {e}")
    # Seit 2.71.0: Uniswap-V3/V4-Positionen auf Robinhood Chain: wem gehoeren die LP-NFTs (Titan, HoodLock, Launchpad,
    # verbrannt, Wallet). Erster Lauf je Token im Hintergrund, hoechstens 10 s Wartezeit im Scan.
    if chain == "robinhood":
        try:
            import lplock
            st = lplock.lp_status_warten(a, 10)
            if st:
                r["lp"] = dict(r.get("lp") or {}, positions=st, positions_line=lplock.lp_zeile(st))
                gesichert = round((st.get("locked_pct") or 0) + (st.get("burned_pct") or 0), 1)
                r["safety"]["lp_in_contracts_pct"] = round((st.get("hook_pct") or 0) + (st.get("other_pct") or 0), 1)
                if (st.get("launchpad_pct") or 0) < 50 and r["safety"].get("lp_locked_pct") is None:
                    r["safety"]["lp_locked_pct"] = gesichert
                elif (st.get("launchpad_pct") or 0) >= 50:
                    # seit 2.77.0: Liquiditaet beim Launchpad (z.B. Torch nach der Graduation) ist kein "0% locked"
                    pad = [n for n in st.get("lockers") or [] if n in lplock.LAUNCHPAD.values()]
                    r["safety"]["lp_launchpad"] = ", ".join(pad) or "the launchpad"
                    r["safety"]["lp_launchpad_pct"] = st.get("launchpad_pct")
                    r["safety"].pop("lp_locked_pct", None)
                if st.get("unlock_ts"):
                    r["safety"]["lp_unlock_ts"] = st["unlock_ts"]
        except Exception as e:
            log.debug(f"scan lplock {a}: {e}")
    if r["supply"].get("top") and r["supply"].get("top10_pct") is None:
        echt = [x for x in r["supply"]["top"] if not x.get("pool") and not x.get("locked")]
        r["supply"]["top10_pct"] = round(sum(x["pct"] for x in echt[:10]), 2)
        r["supply"]["largest_pct"] = echt[0]["pct"] if echt else None
    if chain == "solana":
        try:
            _sol_beschriften(r, rugcheck_sol(a))
        except Exception as e:
            log.debug(f"scan rugcheck {a}: {e}")
    gesp = [x for x in r["supply"].get("top") or [] if x.get("locked")]
    if gesp:
        namen = sorted({str(x.get("locker") or x.get("label") or "").strip() for x in gesp} - {""})
        r["supply"]["locked_pct"] = round(sum(float(x.get("pct") or 0) for x in gesp), 2)
        r["supply"]["locked_by"] = ", ".join(namen)[:60] or None
        ub = [x.get("unlock_ts") for x in gesp if x.get("unlock_ts")]
        if ub:
            r["supply"]["unlock_ts"] = min(ub)
    if r["dev"].get("address") and not r["dev"].get("explorer"):
        r["dev"]["explorer"] = explorer(chain, "address", r["dev"]["address"])
    if not mk and not r.get("symbol"):
        return {"ok": False, "error": f"No market found for {a} on any chain I read. It may be brand new or not tradable yet."}
    r["age"] = alter_text(r.get("age_sec"))
    try:
        zw = zwillinge(r)
        if zw:
            r["twins"] = zw
    except Exception as e:
        log.debug(f"scan zwillinge: {e}")
    try:
        nv = narrativ(r)
        if nv:
            r["narrative"] = nv
    except Exception as e:
        log.debug(f"scan narrativ: {e}")
    # Seit 2.80.7: der Builder-Eintrag von HEY Research Lab (nur Robinhood Chain), als "What HEY read"
    try:
        import hey
        hy = hey.lesen(chain, a)
        if hy:
            r["hey"] = hy
    except Exception as e:
        log.debug(f"scan hey: {e}")
    if chain == "solana":
        alt_name = (sicherheit_sol(a) or {}).get("name")
        if alt_name and mk.get("name") and alt_name.strip().lower() != str(mk.get("name")).strip().lower() \
                and alt_name.strip().lower() not in str(mk.get("name")).lower():
            r["renamed"] = {"onchain": alt_name, "listed": mk.get("name")}
    # Dev haelt 0 %? Dann sofort nachsehen, wohin seine Tokens gegangen sind (seit 2.49.7), nicht erst im Tiefen-Scan
    dv0 = r.get("dev") or {}
    if ist_evm(chain) and dv0.get("address") and dv0.get("holds_pct") is not None and dv0["holds_pct"] < 0.5:
        try:
            import weiter
            dr = weiter.dev_raus(chain, a, dv0["address"])
            if dr:
                dv0["out"] = dr
        except Exception as e:
            log.debug(f"scan dev_raus: {e}")
    r["verdict"] = urteil(r)
    # Seit 2.50.3: ohne Tiefen-Scan (Buendel, Weiterleitungen, Gruppen) nie "sauber" sagen, sondern "noch nichts gefunden"
    v_ = r["verdict"]
    tief = r.get("deep") or {}
    if chain in ("tron", "ton"):
        r["scope_note"] = f"On {KETTEN[chain][0]} forwarded tokens are not traced yet. No trading on this chain."
    if r.get("chain") not in OHNE_TIEFE and v_.get("level") == "green" and tief.get("state") != "ready" and not (r.get("forwards") or {}).get("checked"):
        v_.update(level="amber", pending=True,
                  title="Nothing bad yet, deep check running" if tief.get("state") == "running"
                  else (f"Nothing bad on first look. Deep check did not finish ({tief['why']}), tap Refresh"
                        if tief.get("state") == "unavailable" and tief.get("why")
                        else "Nothing bad on first look. Bundles and forwards not checked yet"))
    r["card"] = karte(r)
    # Seit 2.81: oeffentlich nur Fakten. Die Stufe bleibt intern (Radar-Statistik), Titel und Grund sind neutral.
    v_ = r.get("verdict") or {}
    v_["title"] = "What the chain shows"
    v_["reason"] = "Facts read on chain. No rating, no advice."
    r["card_html"] = karte(r, html=True)
    _CACHE[ck] = (time.time(), r)
    if (r.get("deep") or {}).get("state") == "running":
        r["deep_note"] = "The deep scan (bundles, clusters, early buyers) is running and arrives in about a minute."
    return r


# ── Karte fuer Telegram (Klartext, kurze Zeilen) ─────────────────────────

# ── Gleicher Name, andere Adresse; Narrative ─────────────────────────────

_SUCHE = {}
STOP = {"the", "a", "of", "and", "coin", "token", "inu", "on", "to", "in", "for", "by", "is", "it", "my", "official",
        "sol", "solana", "eth", "base", "bnb", "ai", "x", "new", "fun", "pump", "meme", "robinhood", "torch"}


def _suche(q):
    q = str(q or "").strip()
    c = _SUCHE.get(q.lower())
    if c and time.time() - c[0] < 600:
        return c[1]
    try:
        ps = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": q}, headers=UA, timeout=10).json().get("pairs") or []
    except Exception:
        ps = []
    _SUCHE[q.lower()] = (time.time(), ps)
    return ps


CG_KETTEN = {"solana": "solana", "ethereum": "ethereum", "base": "base", "binance-smart-chain": "bsc",
             "arbitrum-one": "arbitrum", "robinhood": "robinhood", "tron": "tron", "the-open-network": "ton"}


def _coingecko(sym):
    """Etablierte Coins: CoinGecko kennt den echten Coin zu einem Ticker (nach Marktkapitalisierung).
    -> {"id", "rank", "chain", "address"} oder {"id", "rank", "major": True} (eigene Chain, kein Contract) oder None.
    24 h im kv-Speicher, damit das kostenlose Limit reicht. COINGECKO_API_KEY (Demo-Key) optional."""
    key = "cgt_" + sym.lower()
    try:
        c = json.loads(storage.kv_get(key) or "null")
        if c and time.time() - c.get("ts", 0) < 86400:
            return c.get("d")
    except Exception:
        pass
    h = dict(UA)
    if os.getenv("COINGECKO_API_KEY", "").strip():
        h["x-cg-demo-api-key"] = os.getenv("COINGECKO_API_KEY").strip()
    try:
        d = requests.get("https://api.coingecko.com/api/v3/search", params={"query": sym}, headers=h, timeout=8).json()
        if "coins" not in d:
            raise ValueError("coingecko limit")
        cs = [x for x in d.get("coins") or [] if str(x.get("symbol") or "").lower() == sym.lower() and x.get("market_cap_rank")]
        if not cs:
            out = None
        else:
            cs.sort(key=lambda x: x["market_cap_rank"])
            cid, rank = cs[0]["id"], cs[0]["market_cap_rank"]
            i = requests.get(f"https://api.coingecko.com/api/v3/coins/{cid}", headers=h, timeout=8,
                             params={"localization": "false", "tickers": "false", "market_data": "false",
                                     "community_data": "false", "developer_data": "false"}).json()
            if i.get("id") != cid:
                raise ValueError("coingecko limit")  # Limit-Antwort nie als 'grosser Coin ohne Contract' werten
            pl = {CG_KETTEN[k]: v for k, v in (i.get("platforms") or {}).items() if k in CG_KETTEN and v}
            heim = CG_KETTEN.get(i.get("asset_platform_id") or "")
            if heim and pl.get(heim):
                out = {"id": cid, "rank": rank, "chain": heim, "address": pl[heim]}
            elif pl:
                k = next(k for k in ("solana", "ethereum", "base", "bsc", "arbitrum", "robinhood") if k in pl)
                out = {"id": cid, "rank": rank, "chain": k, "address": pl[k]}
            elif not i.get("asset_platform_id"):
                out = {"id": cid, "rank": rank, "major": True}
            else:
                out = None
    except Exception as e:
        log.debug(f"scan coingecko {sym}: {e}")
        return None  # Fehler nicht cachen
    try:
        storage.kv_set(key, json.dumps({"ts": time.time(), "d": out}))
    except Exception:
        pass
    return out


def ticker_finden(sym, chain=None):
    """Ticker -> der echte Coin, ueber den ganzen Markt, nicht nur Torch.
    1. ODDS und TORCH fest. 2. CoinGecko (etablierte Coins, nach Marktkapitalisierung). 3. DexScreener: exakter
    Ticker, echtes Volumen und echte Trades gewinnen (Liquiditaet allein ist faelschbar). 4. Torch-Kurve (onchain).
    Gleichnamige Coins woanders kommen als 'others' mit."""
    q = str(sym or "").strip().lstrip("$")
    if not q:
        return None
    import onchain
    if q.lower() in ("odds", "torch"):
        t = onchain._suche(q) or {"odds": "0x9c87b4cfa21266d1638f437c96dbe1012cb81111",
                                  "torch": "0x000050e18ffa3f8f7ad170eb9cef160eb3ed1111"}[q.lower()]
        return {"chain": "robinhood", "address": t}
    toks = {}
    for p in _suche(q):
        b = p.get("baseToken") or {}
        k = DEX_ZU.get(p.get("chainId"))
        if not k or str(b.get("symbol") or "").strip().lstrip("$").lower() != q.lower():
            continue
        if chain and k != chain:
            continue
        adr = str(b.get("address") or "")
        if not adr:
            continue
        t = toks.setdefault((k, adr.lower()), {"chain": k, "address": adr if k in GROSS else adr.lower(),
                                               "liq": 0.0, "vol24": 0.0, "tx24": 0, "mcap": 0.0})
        t["liq"] += _liq(p)
        t["vol24"] += float((p.get("volume") or {}).get("h24") or 0)
        tx = (p.get("txns") or {}).get("h24") or {}
        t["tx24"] += int(tx.get("buys") or 0) + int(tx.get("sells") or 0)
        t["mcap"] = max(t["mcap"], float(p.get("marketCap") or p.get("fdv") or 0))

    def andere(ohne):
        echt = [t for t in toks.values() if t["address"].lower() != ohne.lower() and t["liq"] >= 1000
                and (not t["mcap"] or t["liq"] <= t["mcap"] * 1.5)]
        echt.sort(key=lambda t: (-t["vol24"], -t["tx24"]))
        return [{"chain": KETTEN[t["chain"]][0], "address": t["address"], "liquidity_usd": round(t["liq"]),
                 "mcap_usd": round(t["mcap"]), "volume_24h_usd": round(t["vol24"])} for t in echt[:5]]

    # Echtes Geschehen: Liquiditaet ueber der Marktkapitalisierung ist ein Fake-Pool, ohne Trades zaehlt nichts
    plausibel = [t for t in toks.values() if t["liq"] >= 2000 and t["tx24"] >= 20 and (not t["mcap"] or t["liq"] <= t["mcap"] * 1.5)]
    best = max(plausibel, key=lambda t: (t["vol24"], t["tx24"])) if plausibel else None
    def mehrdeutig(liste):
        """Mehrere echte Coins mit diesem Ticker und keiner klar vorne: nicht raten, nach dem Contract fragen."""
        kand = sorted(liste, key=lambda t: -t["vol24"])[:5]
        return {"ambiguous": True, "symbol": q.upper(), "candidates": [
            {"chain": KETTEN[t["chain"]][0], "address": t["address"], "mcap_usd": round(t["mcap"]),
             "liquidity_usd": round(t["liq"]), "volume_24h_usd": round(t["vol24"])} for t in kand]}

    cg = _coingecko(q)
    if cg and cg.get("address") and (not chain or cg["chain"] == chain):
        a = cg["address"] if cg["chain"] in GROSS else cg["address"].lower()
        cg_vol = sum(t["vol24"] for t in toks.values() if t["address"].lower() == a.lower())
        # Ein kleiner CoinGecko-Coin verliert gegen einen Coin mit deutlich mehr echtem Handel (z. B. LOUD auf Torch)
        if not (best and cg["rank"] > 500 and best["vol24"] > max(cg_vol, 1) * 3 and best["address"].lower() != a.lower()):
            # Etablierter Coin (Top 500) ist eindeutig. Ein kleiner mit ernsthafter Konkurrenz gleichen Namens nicht.
            rivalen = [t for t in plausibel if t["address"].lower() != a.lower() and t["vol24"] >= max(cg_vol, 1) * 0.25]
            if cg["rank"] > 500 and rivalen:
                eigen = toks.get((cg["chain"], a.lower())) or {"chain": cg["chain"], "address": a, "liq": 0.0, "vol24": cg_vol, "mcap": 0.0}
                return mehrdeutig([eigen] + rivalen)
            return {"chain": cg["chain"], "address": a, "coingecko_rank": cg["rank"], "others": andere(a)}
    elif cg and cg.get("major") and not chain:
        return {"major": True, "symbol": q.upper(), "coingecko_id": cg["id"]}
    if best:
        # Zweiter Coin mit mindestens einem Viertel des Handels des ersten: zu nah dran, also nachfragen
        rivalen = [t for t in plausibel if t is not best and t["vol24"] >= best["vol24"] * 0.25]
        if rivalen:
            return mehrdeutig([best] + rivalen)
        out = {"chain": best["chain"], "address": best["address"], "others": andere(best["address"])}
        return out
    t = onchain._suche(q)
    if t:
        return {"chain": "robinhood", "address": t}
    return None


def zwillinge(r):
    """Andere Coins mit demselben Ticker oder Namen. Wer war zuerst da, wer ist groesser?"""
    sym = str(r.get("symbol") or "").strip().lstrip("$")
    if len(sym) < 2:
        return None
    eigen = str(r["address"]).lower()
    toks = {}
    for p in _suche(sym):
        b = p.get("baseToken") or {}
        if str(b.get("symbol") or "").strip().lstrip("$").lower() != sym.lower():
            continue
        a = str(b.get("address") or "")
        if not a or a.lower() == eigen:
            continue
        k = DEX_ZU.get(p.get("chainId")) or p.get("chainId")
        t = toks.setdefault(a.lower(), {"address": a, "chain": k, "liq": 0.0, "mcap": 0.0, "created_ms": None})
        t["liq"] += _liq(p)
        t["mcap"] = max(t["mcap"], float(p.get("marketCap") or p.get("fdv") or 0))
        if p.get("pairCreatedAt"):
            t["created_ms"] = min(t["created_ms"] or p["pairCreatedAt"], p["pairCreatedAt"])
    if not toks:
        return None
    andere = sorted(toks.values(), key=lambda t: -t["mcap"])
    mein_alter = (r.get("market") or {}).get("created_ms")
    aelter = [t for t in andere if t["created_ms"] and mein_alter and t["created_ms"] < mein_alter]
    groesser = [t for t in andere if t["mcap"] > ((r.get("market") or {}).get("mcap") or 0)]
    gleiche_chain = [t for t in andere if t["chain"] == r["chain"]]
    out = {"count": len(andere), "same_chain": len(gleiche_chain), "older": len(aelter), "bigger": len(groesser),
           "top": [{"address": t["address"], "chain": KETTEN.get(t["chain"], (t["chain"],))[0], "mcap": t["mcap"],
                    "age": alter_text(time.time() - t["created_ms"] / 1000) if t["created_ms"] else None} for t in andere[:3]]}
    # Seit 2.77.0: Nachahmer nur auf derselben Chain. Gleicher Ticker auf einer anderen Chain ist meist ein ganz
    # anderes Projekt (LOUD auf Robinhood hat nichts mit LOUD auf BNB von 2021 zu tun).
    out["likely_copy"] = bool(aelter and groesser and any(t in groesser and t["chain"] == r["chain"] for t in aelter))
    if not out["likely_copy"]:
        out["note"] = ("Same ticker on other chains or smaller/newer coins: unrelated projects, do not call this coin a copy "
                       "or copycat.")
    if eigen in EIGENE:  # seit 2.60.5: eigene Coins (ODDS, TORCH) nie als Nachahmer, auch nicht im Text und fuer das Modell
        out["likely_copy"] = False
        out["note"] = "Official coin. Other coins with the same ticker are unrelated."
    return out


def narrativ(r):
    """Laeuft das Thema dieses Coins gerade? Neue Coins mit demselben Wort in 3 Tagen, wie viele davon laufen."""
    woerter = re.findall(r"[a-z]{3,}", f"{r.get('name') or ''} {r.get('symbol') or ''}".lower())
    woerter = [w for w in dict.fromkeys(woerter) if w not in STOP][:2]
    if not woerter:
        return None
    best = None
    for w in woerter:
        jetzt = time.time() * 1000
        neu, gelaufen, gesehen = 0, 0, set()
        for p in _suche(w):
            b = p.get("baseToken") or {}
            a = str(b.get("address") or "").lower()
            if not a or a in gesehen or a == str(r["address"]).lower():
                continue
            if w not in f"{b.get('name') or ''} {b.get('symbol') or ''}".lower():
                continue
            # Seit 2.62.7: Coins mit demselben Symbol sind Kopien (stehen schon unter "Same name"), kein Thema
            if str(b.get("symbol") or "").lower() == str(r.get("symbol") or "").lower():
                continue
            if not p.get("pairCreatedAt") or jetzt - p["pairCreatedAt"] > 3 * 86400 * 1000:
                continue
            gesehen.add(a)
            neu += 1
            mc = float(p.get("marketCap") or p.get("fdv") or 0)
            if mc >= 1_000_000 or float((p.get("priceChange") or {}).get("h24") or 0) >= 200:
                gelaufen += 1
        if neu >= 3 and (best is None or (gelaufen, neu) > (best["ran"], best["new"])):
            best = {"word": w.upper(), "new": neu, "ran": gelaufen, "hot": gelaufen >= 2}
    return best


def karte(r, html=False):
    """Die Karte fuer Telegram und den Web-Chat. Seit 2.41.1 mit klarer Gliederung; html=True fuer Telegram
    (fett, CA zum Antippen kopierbar)."""
    import html as H
    e = (lambda x: H.escape(str(x), quote=False)) if html else (lambda x: str(x))
    fett = (lambda x: f"<b>{e(x)}</b>") if html else (lambda x: str(x))
    m, s, sp, dv, v = r.get("market") or {}, r.get("safety") or {}, r.get("supply") or {}, r.get("dev") or {}, r.get("verdict") or {}
    sym = r.get("symbol") or kurz(r["address"])
    kopf = fett(f"${sym}") + (f"  {e(r['name'])}" if r.get("name") and str(r["name"]).lower() != str(sym).lower() else "")
    ort = " · ".join(x for x in (r.get("chain_name"), r.get("venue"), (r.get("age") + " old") if r.get("age") else None) if x)
    z = [kopf, e(ort), ""]
    # Seit 2.81: nur Fakten, kein Urteil (keine Ampel, kein "safe"/"risky"). Was on chain steht, ohne Wertung.
    schlecht, gut = list(v.get("bad") or []), list(v.get("good") or [])
    z.append(fett("🔎 What the chain shows") + (" " + e("(deep check still running)") if v.get("pending") else ""))
    for x in (schlecht[:4] + gut[:max(2, 6 - min(4, len(schlecht)))]):
        z.append("• " + e(x))
    if not schlecht and not gut:
        z.append("• " + e("Nothing notable found on chain yet"))
    # Markt
    z.append("")
    z.append(f"💰 MC {usd(m.get('mcap'))} · Liq {usd(m.get('liq'))} · {preis(m.get('price'))}")
    ch = m.get("change") or {}
    if ch:
        z.append("📈 " + " · ".join(f"{n} {pct(ch[k])}" for k, n in (("m5", "5m"), ("h1", "1h"), ("h24", "24h")) if ch.get(k) is not None))
    h1 = (m.get("txns") or {}).get("h1") or {}
    if h1.get("buys") is not None:
        h24 = (m.get("txns") or {}).get("h24") or {}
        z.append(f"🔁 1h {h1['buys']} buys / {h1['sells']} sells · Vol {usd((m.get('vol') or {}).get('h1'))}"
                 + (f" · 24h {h24['buys']}/{h24['sells']} · Vol {usd((m.get('vol') or {}).get('h24'))}" if h24.get("buys") is not None else ""))
    lp_ = r.get("lp") or {}
    if lp_.get("supply_pct") is not None:
        art_ = {"v2": "Uniswap V2", "v3": "V3 pool", "v4": "Uniswap V4"}.get(lp_.get("kind"), "pool")
        zeile = f"💧 LP: pool holds {zahl(lp_['supply_pct'])}% of supply ({art_})"
        if lp_.get("burned_pct") is not None:
            zeile += f" · {zahl(lp_['burned_pct'])}% of LP burned"
        elif lp_.get("kind") in ("v3", "v4") and not lp_.get("positions_line"):
            zeile += " · no LP token, positions are NFTs"
        z.append(e(zeile))
    if lp_.get("positions_line"):
        z.append(e("🔐 " + lp_["positions_line"]))
    if r.get("torch"):
        tt = r["torch"]
        z.append("🎓 Torch: graduated" if tt.get("graduated") else f"🎓 Torch curve: {tt.get('progress_to_graduation_pct')}% to graduation")
    # Sicherheit
    teile = []
    if s.get("honeypot") is not None:
        teile.append("Honeypot!" if s["honeypot"] else "No honeypot")
    if s.get("buy_tax") is not None or s.get("sell_tax") is not None:
        teile.append(f"Tax {zahl(s.get('buy_tax') or 0)}/{zahl(s.get('sell_tax') or 0)}")
    if s.get("mint_revoked") is not None:
        teile.append("Mint revoked" if s["mint_revoked"] else "Mint ACTIVE")
    elif s.get("mintable") is not None:
        teile.append("Mintable" if s["mintable"] else "Not mintable")
    if s.get("freeze_revoked") is not None:
        teile.append("Freeze revoked" if s["freeze_revoked"] else "Freeze ACTIVE")
    if s.get("lp_launchpad"):
        teile.append(f"LP {zahl(s.get('lp_launchpad_pct') or 100)}% held by {s['lp_launchpad']}")
    elif s.get("lp_locked_pct") is not None:
        teile.append(f"LP {zahl(s['lp_locked_pct'])}% locked" + (f", {zahl(s['lp_in_contracts_pct'])}% in contracts"
                                                                 if (s.get("lp_in_contracts_pct") or 0) >= 10 else ""))
    elif s.get("lp_locked") is not None:
        teile.append("LP locked" if s["lp_locked"] else "LP not locked")
    if s.get("owner_renounced") is not None:
        teile.append("Owner renounced" if s["owner_renounced"] else "Owner active")
    lock = None
    if (sp.get("locked_pct") or 0) > 0:
        # seit 2.77.0: jeder Lock wird gezeigt, auch ein kleiner (unter 0.01 % als "<0.01%")
        lock = (f"🔒 {'<0.01' if sp['locked_pct'] < 0.01 else zahl(sp['locked_pct'])}% of supply locked"
                + (f" until {time.strftime('%d %b %Y', time.gmtime(sp['unlock_ts']))}" if sp.get("unlock_ts") else "")
                + (f" ({sp['locked_by']})" if sp.get("locked_by") else ""))
    if teile or lock:
        z += ["", "🛡 " + fett("Contract")]
        if teile:
            z.append(e(" · ".join(teile)))
        if lock:
            z.append(e(lock))
    # Holder
    sz = []
    kurzinfo = []
    if sp.get("holders"):
        kurzinfo.append(f"{int(sp['holders']):,} holders")
    if sp.get("top10_pct") is not None:
        kurzinfo.append(f"top 10 {zahl(sp['top10_pct'])}%")
    if sp.get("largest_pct") is not None:
        kurzinfo.append(f"largest {zahl(sp['largest_pct'])}%")
    if kurzinfo:
        sz.append(" · ".join(kurzinfo))
    if sp.get("bundled_pct") is not None:
        if (sp.get("bundled_fwd_pct") or 0) >= 0.1:
            # seit 2.63: auch was Bundles ueber mehrere Wallets weitergereicht haben
            sz.append(f"{balken(sp['bundled_total_pct'])} {zahl(sp['bundled_total_pct'])}% bundled "
                      f"({zahl(sp['bundled_pct'])}% in the bundle wallets, {zahl(sp['bundled_fwd_pct'])}% passed on "
                      f"through up to {sp.get('fwd_hops') or 1} wallet{'s' if (sp.get('fwd_hops') or 1) != 1 else ''})")
        else:
            sz.append(f"{balken(sp['bundled_pct'])} {zahl(sp['bundled_pct'])}% bundled")
    if sp.get("fresh_pct") is not None:
        sz.append(f"{balken(sp['fresh_pct'])} {zahl(sp['fresh_pct'])}% fresh wallets"
                  + (" (top holders)" if str((r.get("deep") or {}).get("basis") or "").startswith("top") else ""))
    pw_ = sp.get("prepared") or {}
    if (pw_.get("count") or 0) >= 3:
        sz.append(f"🧳 Prepared wallets: {pw_['count']} of the first {pw_['of']} buyers"
                  + (f", {pw_['exchange_funded']} exchange funded" if pw_.get("exchange_funded") else "")
                  + (f" · hold {zahl(pw_['supply_pct'])}%" if (pw_.get("supply_pct") or 0) >= 0.5 else ""))
    if str((r.get("deep") or {}).get("basis") or "").startswith("top"):
        # seit 2.77.1: Start zu weit zurueck (viele Trades): nie "0 Bundles" zeigen, als waere es geprueft
        sz.append("⚠️ Launch too far back to read: bundles and snipers from the first minutes are not checked")
        if any(sp.get(x) is not None for x in ("clusters", "same_funder")):
            sz.append(f"Among the top holders: clusters {sp.get('clusters') or 0} · same funder {sp.get('same_funder') or 0}")
    elif any(sp.get(x) is not None for x in ("clusters", "same_funder", "snipers")):
        sz.append(f"Clusters {sp.get('clusters') or 0} · same funder {sp.get('same_funder') or 0} · snipers {sp.get('snipers') or 0}")
    fw = r.get("forwards") or {}
    if fw.get("count"):
        # seit 2.61.2 verstaendlicher: "51 from 44 wallets" war missverstaendlich
        t = (f"📤 Forwards: {fw['senders']} wallet{'s' if fw['senders'] != 1 else ''} sent tokens to "
             f"{fw.get('receivers') or '?'} other{'s' if (fw.get('receivers') or 0) != 1 else ''} ({fw['count']} transfers)")
        if fw.get("to_fresh"):
            t += f" ({fw['to_fresh']} to fresh wallets)"
        if fw.get("still_held_pct"):
            t += f" · receivers still hold {zahl(fw['still_held_pct'])}%"
        sz.append(t)
        if fw.get("collectors_elsewhere"):
            sz.append(f"📥 Collection wallets: {fw['collectors_elsewhere']} also collected on other coins"
                      + (f" (one on {fw['collectors_elsewhere_max']})" if (fw.get("collectors_elsewhere_max") or 0) > 1 else ""))
    elif fw.get("checked"):
        sz.append("📤 Forwards: none")
    if r.get("burned_pct"):
        sz.append(f"🔥 Burned {zahl(r['burned_pct'])}% of supply")
    sm = r.get("smart") or {}
    if sm.get("checked"):
        sz.append(f"🧠 Strong wallets: {sm['found']} of the " + ("top " if sm.get("top_holders") else "first ")
                  + f"{sm['checked']} " + ("holders" if sm.get("top_holders") else "buyers")
                  + (f" (best score {sm['best']})" if sm.get("best") else ""))
    if (r.get("jetton_admin") or {}).get("address"):
        sz.append("Jetton admin wallet: " + kurz(r["jetton_admin"]["address"]))
    if r.get("scope_note"):
        sz.append("ℹ️ " + r["scope_note"])
    if sz:
        z += ["", "👥 " + fett("Holders")] + [e(x) for x in sz]
    # Dev
    if dv.get("address"):
        d = f"{kurz(dv['address'])}" + (f" · holds {zahl(dv['holds_pct'])}%" if dv.get("holds_pct") is not None else "")
        do_ = dv.get("out") or {}
        if (do_.get("bought_pct") or 0) >= 0.5 and (do_.get("got_from_mint_pct") or 0) < 0.5:
            d += f" · bought {zahl(do_['bought_pct'])}%"
        if (do_.get("forwarded_pct") or 0) >= 1:
            d += f" · moved {zahl(do_['forwarded_pct'])}% to {len(do_.get('to') or [])} wallet{'s' if len(do_.get('to') or []) != 1 else ''}"
        if (do_.get("locked_pct") or 0) >= 1:
            d += f" · locked {zahl(do_['locked_pct'])}%"
        if (do_.get("sold_pct") or 0) >= 1:
            d += f" · sold {zahl(do_['sold_pct'])}%"
        fnet = r.get("funder_net") or {}
        if fnet.get("dev_funder"):
            d += f" · funded by {kurz(fnet['dev_funder'])}" + (f" ({fnet['funder_name']})" if fnet.get("funder_name") else "")
        if dv.get("launches_seen"):
            d += (f" · {dv['launches_seen']} earlier launch{'es' if dv['launches_seen'] != 1 else ''}"
                  + (f", {dv['launches_ran']} ran" if dv.get("launches_ran") else ", none ran" if dv.get("launches_ran") == 0 else ""))
        if dv.get("launcher"):
            d += f" · deployed through launcher contract {kurz(dv['launcher'])}"
        z += ["", "🧑‍💻 " + fett("Dev") + " " + e(d)]
    doppelt = ("top 10", "largest wallet", "fresh wallet", "multiple minor", "low mcap", "price dropped", "price rose")
    fl = [n for n in (r.get("notes") or []) if not any(d in n.lower() for d in doppelt)][:3]
    if fl:
        z += [""] + ["• " + e(n) for n in fl]
    sb = (r.get("seen_before") or {}).get("lines") or []
    if sb:
        z += ["", "🧬 " + fett("Wallet history")] + ["• " + e(n) for n in sb[:4]]
    # Seit 2.82.16: Elysium-Befunde (Geldgeber-Gruppen, gleiche Bloecke, Verbindungen zu bekannten Wallets)
    el = r.get("elysium") or {}
    if r.get("chain") == "elysium":
        zl = []
        if el.get("first_block_buyers"):
            zl.append(f"{el['first_block_buyers']} of the first 30 buyers bought in the first block")
        for g in (el.get("same_block_groups") or [])[:2]:
            if g.get("wallets", 0) >= 3 and g["wallets"] != el.get("first_block_buyers"):
                zl.append(f"{g['wallets']} early buyers in the same block")
        for c in (el.get("funding_clusters") or [])[:3]:
            wer = "the dev" if c.get("funder_is_dev") else "the dev's funder" if c.get("funder_is_dev_funder") else kurz(c["funder"])
            zl.append(f"{len(c['wallets'])} early buyers were funded by {wer}")
        fu = dv.get("funding") or {}
        if fu.get("self"):
            zl.append(f"Dev bridged in {zahl(fu.get('hype'))} HYPE from HyperEVM itself")
        elif dv.get("funded_by"):
            uo = fu.get("funder_origin") or {}
            zl.append(f"Dev was funded with {zahl(fu.get('hype'))} HYPE by {kurz(dv['funded_by'])}"
                      + (" (bridged from HyperEVM)" if fu.get("bridge") else "")
                      + (f". That wallet bridged in {zahl(uo.get('hype'))} HYPE from HyperEVM itself"
                         + (f" {zahl(uo['minutes_before_funding_dev'])} min earlier" if uo.get("minutes_before_funding_dev") is not None else "")
                         if uo.get("self") else ""))
        if el.get("holders_note"):
            zl.append(el["holders_note"])
        if el.get("warning"):
            zl.append(el["warning"])
        if zl:
            z += ["", "🧩 " + fett("Elysium, straight from the chain")] + ["• " + e(n) for n in zl[:7]]
        cs = el.get("connections_summary") or []
        z += ["", "🔗 " + fett("Connections to wallets we know")] + (["• " + e(n) for n in cs[:6]] if cs else
                                                                    ["• " + e("None found yet. Every scan adds to the map.")])
    # Name und Narrativ
    tw, nv = r.get("twins") or {}, r.get("narrative")
    extra = []
    if tw.get("count"):
        if tw.get("likely_copy"):
            extra.append(f"⚠️ Copycat: {tw['count']} other ${r.get('symbol')}, {tw['older']} older and bigger. "
                         f"Biggest on {tw['top'][0]['chain']} at MC {usd(tw['top'][0]['mcap'])}")
        else:
            extra.append(f"🪞 Same name: {tw['count']} other ${r.get('symbol')}" + (f", {tw['same_chain']} on this chain" if tw.get("same_chain") else "")
                         + (". This one is the oldest" if not tw.get("older") else ""))
    if r.get("renamed"):
        extra.append(f"⚠️ Name on chain is \"{r['renamed']['onchain']}\", listed as \"{r['renamed']['listed']}\"")
    if nv:
        extra.append(f"📣 Narrative {nv['word']}: {nv['new']} new coins in 3 days, {nv['ran']} running" + (" (hot)" if nv.get("hot") else ""))
    if r.get("also_on"):
        extra.append(f"Also trades on {', '.join(r['also_on'])}")
    if extra:
        z += [""] + [e(x) for x in extra]
    if r.get("hey"):
        import hey
        z += ["", fett("📚 What HEY read")] + hey.zeilen(r["hey"], html)
    tief = r.get("deep") or {}
    if tief.get("state") == "running":
        z += ["", e("⏳ Deep scan running, bundles and first buyers follow shortly.")]
    elif tief.get("state") == "ready":
        # Seit 2.62.7: war der Tiefen-Scan schon fertig (vorab oder kurz vorher), steht er direkt in dieser Karte.
        # Das sagen wir jetzt, sonst wartet man auf eine "Deep scan is in"-Nachricht, die nicht mehr kommt.
        am = tief.get("age_min") or 0
        z += ["", e("🔎 Deep scan included: bundles, clusters, first buyers" + (f" (checked {am} min ago)" if am >= 2 else "") + ".")]
    z += ["", f"<code>{e(r['address'])}</code>" if html else r["address"]]
    return "\n".join(z)


def knoepfe(r):
    """Telegram-Knoepfe unter der Karte. ('text', url) oder ('text', 'cb:...')."""
    out = [[("Full report", r["links"]["page"])]]
    try:  # seit 2.69.6: Live-Preis als oberste Reihe, der Bot aktualisiert ihn jede Minute (live.py)
        import live
        lk = live.knopf(r, _kurzid(r))
        if lk:
            out.insert(0, [lk])
    except Exception:
        pass
    if r["links"].get("trade_torch"):  # seit 2.59.1: Torch-Coins auf Torch handeln, die Gebuehren bleiben im Torch-Oekosystem
        out.append([("🔥 Trade on Torch", r["links"]["trade_torch"])])
    zeile = []
    if r["links"].get("chart"):
        zeile.append(("Chart", r["links"]["chart"]))
    zeile.append(("Watch it", f"cb:w:{_kurzid(r)}"))
    zeile.append(("Refresh", f"cb:r:{_kurzid(r)}"))
    out.append(zeile)
    # Seit 2.63 feste Reihenfolge: erst beobachten und alarmieren, dann Recherche, dann Video, zuletzt die Hilfe
    if (r.get("deep") or {}).get("state") == "ready" and r.get("chain") not in ("tron", "ton") and ((r.get("dev") or {}).get("address") or (r.get("supply") or {}).get("bundled_pct")):
        out.append([("👁 Track dev, bundles and side wallets", f"cb:tc:{_kurzid(r)}")])
    fw = r.get("forwards") or {}
    if (fw.get("count") or 0) > 0 or ((r.get("dev") or {}).get("out") or {}).get("forwarded_pct", 0) >= 1:
        out.append([("🔔 Alert me when forwarded wallets sell or lock", f"cb:f:{_kurzid(r)}")])
    out.append([("🔎 Project research", f"cb:p:{_kurzid(r)}")])
    try:  # seit 2.81.2: Holder-Karte von KYLL (BalloonMap, kostenlos) als Link, nur Robinhood Chain, Schalter "balloonmap"
        import features
        adr = str(r.get("address") or "")
        if r.get("chain") == "robinhood" and adr.startswith("0x") and len(adr) == 42 and features.an("balloonmap"):
            out.append([("🎈 Holder map by KYLL", f"https://www.kyll.fun/embed/balloonmap/{adr.lower()}")])
    except Exception:
        pass
    try:  # seit 2.61: Promo-Video ueber loudscene, nur wenn eingerichtet
        import video
        if video.an():
            out.append([("🎬 Promo video by loudscene", f"cb:v:{_kurzid(r)}")])
    except Exception:
        pass
    out.append([("❓ What can I do here?", f"cb:h:{_kurzid(r)}")])  # seit 2.61.7
    return out


def _kurzid(r):
    """Callback-Daten sind auf 64 Bytes begrenzt: Chain und Adresse unter kurzer Id merken."""
    import hashlib
    i = hashlib.sha1(f"{r['chain']}:{r['address']}".encode()).hexdigest()[:12]
    try:
        storage.kv_set("scan_id_" + i, json.dumps({"chain": r["chain"], "address": r["address"], "symbol": r.get("symbol")}))
    except Exception:
        pass
    return i


def aus_id(i):
    try:
        return json.loads(storage.kv_get("scan_id_" + re.sub(r"[^0-9a-f]", "", str(i))[:12]) or "null")
    except Exception:
        return None


# ── Fuer das Modell: kompakt, ohne Karte doppelt ─────────────────────────

def fuer_modell(r):
    if not r.get("ok"):
        return r
    aus = {k: r.get(k) for k in ("chain_name", "symbol", "name", "address", "venue", "age", "also_on", "twins", "narrative", "renamed",
                                 "safety", "dev", "smart", "notes", "deep_note", "burned_pct", "torch", "seen_before")}
    if r.get("hey"):
        aus["hey"] = dict(r["hey"], meaning=(
            "What HEY Research Lab recorded about the team behind this token: verified builder or not, recent ships, "
            "status. Quote it as 'what HEY read' and name HEY as the source. It is NOT a rating, NOT a safety verdict "
            "and says nothing about price. Never mix it with our own risk findings."))
    fw = r.get("forwards") or {}
    if fw.get("checked"):
        aus["forwards"] = {k: fw.get(k) for k in ("count", "senders", "receivers", "from_early", "from_dev", "dev_forwarded_pct", "dev_burned_pct",
                                                  "to_fresh", "forwarded_pct", "still_held_pct", "collectors_elsewhere",
                                                  "collectors_elsewhere_max")}
        aus["forwards"]["meaning"] = ("Forwards are plain wallet to wallet transfers of this coin, no sale. Early buyers or the dev "
                                      "moving tokens to new wallets can hide how much they really hold.")
    if aus.get("burned_pct") is not None:
        # seit 2.78.0: die Engine hat diese Zahl einmal dem Dev zugeschrieben ("der Dev hat verbrannt/verkauft")
        aus["burned_supply"] = {"pct": aus.pop("burned_pct"),
                                "meaning": "Share of the total supply sitting at burn addresses, from anyone. It is NOT linked to the dev "
                                           "or any wallet. Never say the dev (or anyone) burned or sold this amount unless forwards.dev_burned_pct says so."}
    fn_ = r.get("funder_net") or {}
    if fn_:
        aus["funder_net"] = dict(fn_, meaning=(
            "The money view around the dev, from chain data with proof links: dev_funder = the wallet that first sent the dev native coin. "
            "funder_traded = what that funder did with THIS coin (bought = from pools/contracts, received = from wallets). "
            "funded_wallets_traded = other wallets the same funder filled that traded this coin. funded_wallets_launched = those that "
            "launched coins themselves (same launch contract = same launchpad). eth_back_to_funder = native sent back to the funder by "
            "the wallets it funded. admins = token owner / Safe owners funded by the same wallet. Same funder means the wallets CAN belong "
            "to the same person, say 'can', never 'do'. List gaps plainly, never guess what is in them."))
    lb_ = (r.get("supply") or {}).get("launch_buys")
    if lb_:
        aus["launch_block_buys"] = dict(lb_, meaning="What wallets bought in the launch block and the 2 blocks after, by amount bought (not by what they "
                                                     "hold now). sold_pct = part of it that is already out again; fastest_exit_s = seconds from launch.")
    aus["market"] = {k: (r.get("market") or {}).get(k) for k in ("price", "mcap", "liq", "change", "txns", "vol")}
    aus["supply"] = {k: v for k, v in (r.get("supply") or {}).items() if k not in ("top", "launch_buys")}
    v_ = r.get("verdict") or {}
    aus["findings"] = {"on_chain_facts": list(v_.get("bad") or []) + list(v_.get("good") or []),
                       "deep_check_running": bool(v_.get("pending")),
                       "rule": ("Facts only. OddsEngine gives NO safety verdict on any token: never call a coin safe, risky, "
                                "clean, a rug, a scam or say buy/avoid. Say what the chain shows and let the user decide.")}
    aus["links"] = r.get("links")
    aus["shown_to_user"] = ("The full report card with these numbers, the on-chain findings and buttons (Full report, Chart, "
                            "Watch it, Refresh) is ALREADY shown to the user above your answer. Do not repeat the numbers. "
                            "Add at most two short lines: the one thing that matters most here, and what they can do next "
                            "(watch it, or buy with the trade tool). If they ask questions, answer from this data.")
    return {k: v for k, v in aus.items() if v not in (None, {}, [])}


# ── Live-Handel fuer die Seite: GeckoTerminal ────────────────────────────

_TR = {}


def trades(chain, pair, n=40):
    netz = KETTEN.get(chain, (None,) * 3)[2]
    if not netz or not pair:
        return []
    c = _TR.get((chain, pair))
    if c and time.time() - c[0] < 8:
        return c[1]
    out = []
    try:
        d = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{netz}/pools/{pair}/trades",
                         headers=UA, timeout=12).json()
        for x in (d.get("data") or [])[:n]:
            a = x.get("attributes") or {}
            kauf = a.get("kind") == "buy"
            out.append({"ts": a.get("block_timestamp"), "side": "buy" if kauf else "sell",
                        "usd": _f(a.get("volume_in_usd")), "wallet": a.get("tx_from_address"),
                        "tx": a.get("tx_hash"),
                        "amount": _f(a.get("to_token_amount") if kauf else a.get("from_token_amount")),
                        "price": _f(a.get("price_to_in_usd") if kauf else a.get("price_from_in_usd"))})
    except Exception as e:
        log.debug(f"scan trades {pair}: {e}")
        if c:
            return c[1]
    _TR[(chain, pair)] = (time.time(), out)
    return out


_KZ = {}
KERZEN_TF = {"1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15), "1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1)}


def kerzen(chain, pair, tf="5m", token=None):
    """Seit 2.63: Kerzen fuer den eigenen Chart im Full Report (vorher nur ein fremdes iframe, das oft leer blieb).
    GeckoTerminal OHLCV mit drei Versuchen, 30 s gemerkt; faellt GeckoTerminal aus, gilt der letzte Stand, sonst
    Kerzen aus den letzten Trades. -> {candles: [[t, o, h, l, c, v], ...] aufsteigend, source}"""
    netz = KETTEN.get(chain, (None,) * 3)[2]
    if not netz or not pair:
        return {"candles": [], "source": None}
    tf = tf if tf in KERZEN_TF else "5m"
    k = (chain, pair, tf)
    c = _KZ.get(k)
    if c and time.time() - c[0] < 30:
        return c[1]
    art, agg = KERZEN_TF[tf]
    params = {"aggregate": agg, "limit": 300, "currency": "usd"}
    if token:
        params["token"] = token
    for versuch in range(3):
        try:
            r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{netz}/pools/{pair}/ohlcv/{art}",
                             params=params, headers=UA, timeout=10)
            if r.status_code == 200:
                lst = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                if lst:
                    out = {"candles": sorted([[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] or 0)]
                                              for x in lst], key=lambda x: x[0]), "source": "geckoterminal", "tf": tf}
                    _KZ[k] = (time.time(), out)
                    return out
            if r.status_code == 404 and "token" in params:
                params.pop("token", None)
                continue
        except Exception as e:
            log.debug(f"scan kerzen {pair}: {e}")
        time.sleep(0.6 * (versuch + 1))
    if c:
        return c[1]
    # Notfall: Kerzen aus den letzten Trades (1 Minute je Kerze)
    je = {}
    for t in trades(chain, pair, 100):
        try:
            from datetime import datetime
            ts = int(datetime.fromisoformat(str(t["ts"]).replace("Z", "+00:00")).timestamp()) // 60 * 60
        except Exception:
            continue
        p_ = t.get("price")
        if not p_:
            continue
        x = je.setdefault(ts, [ts, p_, p_, p_, p_, 0.0])
        x[2], x[3], x[4] = max(x[2], p_), min(x[3], p_), p_
        x[5] += t.get("usd") or 0
    out = {"candles": sorted(je.values(), key=lambda x: x[0]), "source": "trades" if je else None, "tf": "1m"}
    return out


def seite(q):
    """Alles fuer die Live-Seite auf engine.oddsengine.live. Startet keinen neuen Tiefen-Scan."""
    r = analyse(q)  # seit 2.58.1: der Full Report startet den Tiefen-Scan auch selbst
    if not r.get("ok"):
        return r
    m = r.get("market") or {}
    tr = trades(r["chain"], m.get("pair"))
    dev = str((r.get("dev") or {}).get("address") or "").lower()
    top = {str(x.get("address") or "").lower(): x for x in (r.get("supply") or {}).get("top") or []}
    for t in tr:
        w = str(t.get("wallet") or "").lower()
        if dev and w == dev:
            t["tag"] = "dev"
        elif w in top and not top[w].get("pool"):
            t["tag"] = "top holder"
    out = dict(r)
    out.pop("risk_score", None)
    if out.get("verdict"):
        out["verdict"] = {k: v for k, v in out["verdict"].items() if k != "level"}
    out["trades"] = tr
    out["explorer_base"] = KETTEN[r["chain"]][4]
    out["tx_path"] = KETTEN[r["chain"]][7]
    out["addr_path"] = KETTEN[r["chain"]][6]
    netz = KETTEN.get(r["chain"], (None,) * 3)[2]
    if netz and m.get("pair"):
        out["embed"] = (f"https://www.geckoterminal.com/{netz}/pools/{m['pair']}?embed=1&info=0&swaps=0&grayscale=0"
                        "&light_chart=0&chart_type=market_cap&resolution=5m&bg_color=0b0f0d")
    elif m.get("url"):
        out["embed"] = m["url"] + "?embed=1&loadChartSettings=0&trades=0&tabs=0&info=0&chartLeftToolbar=0&chartTheme=dark&theme=dark&chartStyle=1&chartType=usd&interval=5"
    # Seit 2.62.7: DexScreener als zweite Quelle, falls GeckoTerminal leer bleibt (umschaltbar auf der Seite)
    if m.get("url") and out.get("embed") and "geckoterminal" in out["embed"]:
        out["embed_alt"] = m["url"] + "?embed=1&loadChartSettings=0&trades=0&tabs=0&info=0&chartLeftToolbar=0&chartTheme=dark&theme=dark&chartStyle=1&chartType=usd&interval=5"
    out["tradable"] = True
    return out


# ── Tiefen-Scan nachliefern (aus motor.tick) ─────────────────────────────

def _echter_coin(r):
    """Seit 2.65: ohne Namen/Ticker ist es meist eine Wallet oder ein Vertrag ohne Token, dafuer kein Nachfassen."""
    sym = str(r.get("symbol") or "").strip().strip("$")
    return bool(sym) and sym != "?"


def nachfassen_merken(adr, r):
    if (r.get("deep") or {}).get("state") != "running" or not _echter_coin(r):
        return
    try:
        l = json.loads(storage.kv_get("scan_follow") or "[]")
    except Exception:
        l = []
    l = [x for x in l if not (x["adr"] == adr and x["address"] == r["address"])]
    l.append({"adr": adr, "chain": r["chain"], "address": r["address"], "symbol": r.get("symbol"),
              "at": int(time.time() + 25), "n": 0})
    storage.kv_set("scan_follow", json.dumps(l[-200:]))


_NF_LOCK = threading.Lock()


def nachfassen(senden, verlauf=None, nur=None):
    with _NF_LOCK:
        _nachfassen(senden, verlauf, nur)


def _nachfassen(senden, verlauf=None, nur=None):
    try:
        l = json.loads(storage.kv_get("scan_follow") or "[]")
    except Exception:
        l = []
    if not l:
        return
    jetzt, rest = time.time(), []
    for x in l:
        dran = nur and x["chain"] == nur[0] and str(x["address"]).lower() == str(nur[1]).lower()
        if (nur and not dran) or (not nur and x["at"] > jetzt):
            rest.append(x)
            continue
        r = analyse(f"{x['chain']}:{x['address']}", deep=False, frisch=True)
        st = (r.get("deep") or {})
        x["n"] = x.get("n", 0) + 1
        fertig = r.get("ok") and st.get("state") == "ready" and (st.get("age_min") or 0) <= 15
        # Seit 2.51.1: nie mehr stumm aufgeben. Klappt der Tiefen-Scan nicht (oder dauert er ueber 8 Minuten), sagt die Karte es.
        # Seit 2.59.3: bis zu 9 Minuten warten (vorher 9 Versuche zu 20 s, also nur 3 Minuten, zu kurz fuer grosse Coins)
        if not fertig and (st.get("state") == "unavailable" or x["n"] >= 27):
            if not _echter_coin(x):
                continue
            try:
                senden(x["adr"], f"⏳ The deep check for ${x.get('symbol') or 'this coin'} could not finish. "
                                 "Bundles and forwards are not checked yet. Tap Refresh to run it again.", knoepfe(r) if r.get("ok") else None)
            except Exception as e:
                log.debug(f"scan nachfassen aufgeben: {e}")
            log.warning(f"scan deep gave up {x['chain']} {x['address'][:8]} state={st.get('state')} n={x['n']}")
            continue
        if fertig:
            karte = re.sub(r"\n\n🔎 Deep scan included[^\n]*", "", r.get("card_html", ""))  # steht schon in der Ueberschrift
            text = f"🔎 <b>Deep scan is in</b>\n\n" + karte
            try:
                senden(x["adr"], text, knoepfe(r), html=True)
            except Exception as e:
                log.debug(f"scan nachfassen senden: {e}")
            if verlauf:
                try:
                    verlauf(x["adr"], r)
                except Exception as e:
                    log.debug(f"scan nachfassen verlauf: {e}")
            continue
        # Seit 2.60.2: zwischendurch sagen, was gerade passiert, damit niemand denkt, es haengt (nach ~40 s und ~2,5 min)
        if x["n"] in (2, 8) and not x.get(f"p{x['n']}"):
            x[f"p{x['n']}"] = 1
            try:
                import intel
                fp = intel.fortschritt(x["chain"], x["address"])
                sym = "$" + str(x.get("symbol") or "this coin").lstrip("$")
                if fp:
                    schritt, gesamt, was = fp
                    senden(x["adr"], f"⏳ Deep scan for {sym}: step {schritt} of {gesamt}, {was}. "
                                     + ("Almost there." if schritt >= gesamt - 2 else "Big coins take a bit longer, it comes in automatically."))
                else:
                    senden(x["adr"], f"⏳ Deep scan for {sym} is still running. It comes in automatically, no need to tap anything.")
            except Exception as e:
                log.debug(f"scan nachfassen fortschritt: {e}")
        x["at"] = int(jetzt + 20)
        rest.append(x)
    storage.kv_set("scan_follow", json.dumps(rest))


# ── Fuer die Live-Wache (motor._token_wache) ─────────────────────────────

RPC = {"ethereum": "https://ethereum-rpc.publicnode.com", "base": "https://mainnet.base.org",
       "bsc": "https://bsc-dataseed.bnbchain.org", "arbitrum": "https://arb1.arbitrum.io/rpc",
       "avalanche": "https://api.avax.network/ext/bc/C/rpc", "optimism": "https://mainnet.optimism.io",
       "polygon": "https://polygon-bor-rpc.publicnode.com"}


def zahlen(chain, adr):
    """Kurze Marktzahlen fuer die Wache: Preis, MC, Liq, Vol 1h, Kaeufe/Verkaeufe 5m, Chart."""
    m = markt(adr, chain, _paare(adr))
    tx = (m.get("txns") or {}).get("m5") or {}
    return {"price": m.get("price"), "mc": m.get("mcap"), "liq": m.get("liq"), "liq_base": m.get("liq_base"),
            "liq_quote": m.get("liq_quote"), "vol_h1": (m.get("vol") or {}).get("h1"),
            "buys5": tx.get("buys"), "sells5": tx.get("sells"), "chg_h1": (m.get("change") or {}).get("h1"),
            "url": m.get("url"), "sym": m.get("symbol")}


def _evm(chain, methode, params):
    if chain == "robinhood":
        import holder
        return holder._evm_rpc(methode, params)
    url = os.getenv("RPC_" + chain.upper(), "").strip() or RPC.get(chain)
    d = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": methode, "params": params}, headers=UA, timeout=12).json()
    if "result" not in d:
        raise RuntimeError(str(d.get("error"))[:100])
    return d["result"]


def bestaende(chain, token, wallets):
    """Rohe Bestaende {wallet: int} plus '_supply'. Fehlende Werte fehlen einfach."""
    out = {}
    if chain == "solana":
        import holder
        try:
            s = holder._sol_rpc("getTokenSupply", [token])
            out["_supply"] = int(((s or {}).get("value") or {}).get("amount") or 0)
        except Exception:
            pass
        for w in wallets:
            try:
                r = holder._sol_rpc("getTokenAccountsByOwner", [w, {"mint": token}, {"encoding": "jsonParsed"}])
                out[w] = sum(int((((x.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("tokenAmount", {}).get("amount") or 0)
                             for x in ((r or {}).get("value") or []))
            except Exception:
                pass
        return out
    if not ist_evm(chain):
        return out
    try:
        out["_supply"] = int(_evm(chain, "eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), 16)
    except Exception:
        pass
    for w in wallets:
        try:
            out[w] = int(_evm(chain, "eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + w.lower().replace("0x", "")}, "latest"]), 16)
        except Exception:
            pass
    return out
