"""
fluss.py: Geld vorwaerts verfolgen, durch Swaps und ueber Bridges (seit 2.54). Dazu der Drain-Report fuer Wallet Guard.

Jede Transaktion eines Wallets wird eingeordnet:
  swap      gab A, bekam B (das Geld bleibt beim Wallet, nur in anderer Form)
  send      an ein privates Wallet
  bridge    ueber Relay oder Across auf eine andere Chain, mit Empfaenger und Ziel-Transaktion
  cash_out  an eine Boerse oder einen sehr aktiven Dienst
  contract  an einen anderen Vertrag (Einzahlung, Staking, unbekannt)
  receive   etwas kam herein
folgen() geht von einem Wallet aus mehrere Schritte weit: Weiterleitungen und Bridges werden verfolgt,
Swaps bleiben beim selben Wallet, Boersen beenden die Spur ("ausgezahlt bei ...").

Bridges, gegen echte Daten geprueft:
  Relay   api.relay.link/requests (v3 mit RELAY_API_KEY, sonst v2 bis zur Abschaltung), findet Herkunft und Ziel
          zu jeder Transaktion, auf beiden Seiten
  Across  app.across.to/api/deposit/status, findet zur Einzahlung Ziel-Chain und Ziel-Transaktion
Andere Bridges: ehrlich als "bridge or contract" gemeldet, ohne geratenes Ziel.

Ketten: Robinhood, Base, Ethereum, Arbitrum (Blockscout) und Solana. Jede Aussage mit Beleg-Link.
"""
import json
import os
import time

import requests

import storage
from logger import log

KETTEN_ID = {1: "ethereum", 8453: "base", 42161: "arbitrum", 4663: "robinhood", 56: "bsc", 792703809: "solana",
             10: "optimism", 137: "polygon", 130: "unichain", 59144: "linea", 324: "zksync", 534352: "scroll", 81457: "blast"}
ID_VON = {v: k for k, v in KETTEN_ID.items()}
FOLGBAR = ("robinhood", "base", "ethereum", "arbitrum", "solana")
BUDGET = float(os.getenv("FLOW_BUDGET_SEC", "40"))
ZEIGEN = int(os.getenv("FLOW_SHOW", "8"))
UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
_C = {}
_GIFT = {}  # Wallet -> Doppelgaenger-Adressen aus gefaelschten Token-Transfers (Address Poisoning)


def _cache(k, sek, f):
    v = _C.get(k)
    if v and time.time() - v[0] < sek:
        return v[1]
    r = f()
    _C[k] = (time.time(), r)
    return r


def _link(chain, art, a):
    import scan
    if chain not in scan.KETTEN:
        return None
    return scan.explorer(chain, art, a)


def _ketten_name(cid):
    return KETTEN_ID.get(int(cid)) if cid is not None else None


# ── Bridges ──────────────────────────────────────────────────────────────

def relay(h):
    """Relay-Auftrag zu einer Transaktion (Einzahlung oder Auszahlung). -> {"from_chain","from","from_tx","to_chain","to","to_tx",
    "amount_in","amount_out","usd"} oder None"""
    def hol():
        key = os.getenv("RELAY_API_KEY", "").strip()
        try:
            if key:
                r = requests.get("https://api.relay.link/requests/v3", params={"hash": h}, headers=dict(UA, **{"x-api-key": key}), timeout=12)
            else:
                r = requests.get("https://api.relay.link/requests/v2", params={"hash": h}, headers=UA, timeout=12)
            reqs = (r.json() or {}).get("requests") or []
        except Exception as e:
            log.debug(f"fluss relay {h[:10]}: {e}")
            return None
        if not reqs:
            return None
        q = reqs[0]
        d = q.get("data") or {}
        ein = (d.get("inTxs") or [{}])[0]
        aus = (d.get("outTxs") or [{}])[0]
        meta = d.get("metadata") or {}
        ci, co = meta.get("currencyIn") or {}, meta.get("currencyOut") or {}
        def menge(c):
            cur = c.get("currency") or {}
            return f"{c.get('amountFormatted')} {cur.get('symbol') or ''}".strip() if c.get("amountFormatted") else None
        return {"bridge": "Relay", "status": q.get("status"),
                "from_chain": _ketten_name(ein.get("chainId")), "from": str((ein.get("data") or {}).get("from") or q.get("user") or "").lower() or None,
                "from_tx": ein.get("hash"),
                "to_chain": _ketten_name(aus.get("chainId")) or _ketten_name((co.get("currency") or {}).get("chainId")),
                "to": str(q.get("recipient") or (aus.get("data") or {}).get("to") or "").lower() or None, "to_tx": aus.get("hash"),
                "amount_in": menge(ci), "amount_out": menge(co), "usd": ci.get("amountUsd")}
    return _cache(("relay", h), 3600, hol)


def across(chain, h):
    cid = ID_VON.get(chain)
    if not cid:
        return None
    def hol():
        try:
            d = requests.get("https://app.across.to/api/deposit/status", params={"originChainId": cid, "depositTxHash": h},
                             headers=UA, timeout=12).json()
        except Exception:
            return None
        if not isinstance(d, dict) or d.get("error") or not d.get("destinationChainId"):
            return None
        return {"bridge": "Across", "status": d.get("status"), "from_chain": chain, "from_tx": h,
                "to_chain": _ketten_name(d.get("destinationChainId")) or f"chain {d.get('destinationChainId')}",
                "to_tx": d.get("fillTx") or d.get("fillTxnRef"), "to": d.get("recipient")}
    return _cache(("across", chain, h), 3600, hol)


def bridge(chain, h):
    return relay(h) or (across(chain, h) if chain != "solana" else None)


# ── Bewegungen eines Wallets ─────────────────────────────────────────────

def _bs_liste(chain, action, w, n, sort="desc"):
    import intel
    d = intel._bs(chain, "/api", {"module": "account", "action": action, "address": w, "sort": sort, "page": 1, "offset": n}) or {}
    return d.get("result") if isinstance(d.get("result"), list) else []


def _liste(chain, action, w, n, alt):
    """Neueste n Eintraege, mit alt > 0 dazu die aeltesten alt Eintraege (grosse Wallets haben das Wichtigste oft ganz am Anfang)."""
    rows = _bs_liste(chain, action, w, n)
    if not alt:
        return rows
    gesehen, out = set(), []
    for x in rows + _bs_liste(chain, action, w, alt, "asc"):
        k = (x.get("hash") or x.get("transactionHash"), x.get("logIndex"), x.get("traceId"), x.get("from"), x.get("to"), x.get("value"))
        if k not in gesehen:
            gesehen.add(k)
            out.append(x)
    return out


def _fluesse_evm(chain, w, n=100, alt=0):
    import intel
    nat = "ETH" if chain != "bsc" else "BNB"
    tx = {}
    def eintrag(h, ts):
        return tx.setdefault(h, {"hash": h, "ts": int(ts or 0), "raus": [], "rein": [], "an": None})
    for x in _liste(chain, "txlist", w, n, alt):
        if str(x.get("isError") or "0") != "0":
            continue
        e = eintrag(x["hash"], x.get("timeStamp"))
        von, an, v = str(x.get("from") or "").lower(), str(x.get("to") or "").lower(), int(x.get("value") or 0)
        if von == w:
            e["an"] = an
            if v:
                e["raus"].append({"asset": nat, "token": None, "amount": v / 1e18, "to": an})
        elif an == w and v:
            e["rein"].append({"asset": nat, "token": None, "amount": v / 1e18, "from": von})
    for x in _liste(chain, "txlistinternal", w, n, alt):
        if str(x.get("to") or "").lower() == w and int(x.get("value") or 0) and str(x.get("isError") or "0") == "0":
            eintrag(x.get("hash") or x.get("transactionHash"), x.get("timeStamp"))["rein"].append({"asset": nat, "token": None, "amount": int(x["value"]) / 1e18,
                                                                    "from": str(x.get("from") or "").lower()})
    for x in _liste(chain, "tokentx", w, n * 2, alt * 2 if alt else 0):
        try:
            menge = int(x.get("value") or 0) / 10 ** int(x.get("tokenDecimal") or 18)
        except Exception:
            continue
        if not menge:
            continue
        if falsch(x.get("tokenSymbol"), chain, x.get("contractAddress")):
            if str(x.get("from") or "").lower() == w:
                ziel = str(x.get("to") or "").lower()
                _GIFT.setdefault(w, set()).add(ziel)
                try:
                    import kartei
                    kartei.markieren(chain, ziel, "poison", f"look-alike address planted in the history of {w[:6]}…{w[-4:]} with a fake "
                                     f"'{x.get('tokenSymbol')}' token", _link(chain, "tx", x.get("hash")) or "")
                except Exception:
                    pass
            continue
        e = eintrag(x["hash"], x.get("timeStamp"))
        it = {"asset": x.get("tokenSymbol") or "token", "token": str(x.get("contractAddress") or "").lower(), "amount": menge}
        if str(x.get("from") or "").lower() == w:
            e["raus"].append(dict(it, to=str(x.get("to") or "").lower()))
        elif str(x.get("to") or "").lower() == w:
            e["rein"].append(dict(it, **{"from": str(x.get("from") or "").lower()}))
    return sorted(tx.values(), key=lambda e: -e["ts"])


def _fluesse_sol(w, n=60):
    import intel
    import spaeher
    sigs = [x for x in intel._sol("getSignaturesForAddress", [w, {"limit": n}]) or [] if not x.get("err")]
    txs = intel._sol_batch([intel._tx(x["signature"]) for x in sigs], 20) if sigs else []
    out = []
    for s, t in zip(sigs, txs):
        e = spaeher.sol_einordnen(t, w) if isinstance(t, dict) else None
        if not e:
            continue
        f = {"hash": s["signature"], "ts": s.get("blockTime") or 0, "raus": [], "rein": [], "an": e.get("gegen")}
        for mint, m in e.get("raus") or []:
            f["raus"].append({"asset": None, "token": mint, "amount": m, "to": e.get("gegen")})
        for mint, m in e.get("rein") or []:
            f["rein"].append({"asset": None, "token": mint, "amount": m, "from": e.get("gegen")})
        sol = e.get("sol") or 0
        if sol < -0.003:
            f["raus"].append({"asset": "SOL", "token": None, "amount": -sol, "to": e.get("gegen")})
        elif sol > 0.003:
            f["rein"].append({"asset": "SOL", "token": None, "amount": sol, "from": e.get("gegen")})
        out.append(f)
    return out


# Gefaelschte Tokens (Address Poisoning): Namen wie "ETH" aus kyrillischen oder griechischen Buchstaben, mit unsichtbaren Zeichen.
# Gegen echte Robinhood-Daten gefunden: "Е\u206fΤ\u206fН" an 0x82193b1b...d5f3, die Doppelgaenger-Adresse von 0x82196d30...d5f3.
_GLEICH = str.maketrans({"А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
                         "Х": "X", "У": "Y", "Ѕ": "S", "І": "I", "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
                         "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
                         "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X", "ο": "o"})
_GROSSE = {"ETH", "WETH", "USDC", "USDT", "SOL", "WSOL", "BNB", "DAI", "USDG", "WBTC", "BTC", "STETH", "WSTETH", "CMETH", "METH",
           "RETH", "CBETH", "FRAX", "USDE", "AVAX", "WAVAX"}
# Die echten Vertraege der bekannten Tokens. Ein Token mit so einem Namen, aber anderem Vertrag ist eine Faelschung
# (gegen echte Daten gefunden: ein "stETH" von 0x7de2...8d64 statt Lido, gesendet an Doppelgaenger der Bybit-Exploit-Wallet).
_ECHT = {
    "ethereum": {"WETH": {"0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"}, "USDC": {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"},
                 "USDT": {"0xdac17f958d2ee523a2206206994597c13d831ec7"}, "DAI": {"0x6b175474e89094c44da98b954eedeac495271d0f"},
                 "WBTC": {"0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"}, "STETH": {"0xae7ab96520de3a18e5e111b5eaab095312d7fe84"},
                 "WSTETH": {"0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"}, "CMETH": {"0xe6829d9a7ee3040e1276fa75293bde931859e8fa"},
                 "METH": {"0xd5f7838f5c461feff7fe49ea5ebaf7728bb0adfa"}, "RETH": {"0xae78736cd615f374d3085123a210448e74fc6393"},
                 "CBETH": {"0xbe9895146f7af43049ca1c1ae358b0541ea49704"}, "FRAX": {"0x853d955acef822db058eb8505911ed77f175b99e"},
                 "USDE": {"0x4c9edd5852cd905f086c759e8383e09bff1e68b3"}, "ETH": set()},
    "base": {"WETH": {"0x4200000000000000000000000000000000000006"}, "USDC": {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"},
             "DAI": {"0x50c5725949a6f0c72e6c4a641f24049a917db0cb"}, "CBETH": {"0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22"},
             "WSTETH": {"0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452"}, "USDT": {"0xfde4c96c8593536e31f229ea8f37b2ada2699bb2"},
             "ETH": set()},
    "arbitrum": {"WETH": {"0x82af49447d8a07e3bd95bd0d56f35241523fbab1"},
                 "USDC": {"0xaf88d065e77c8cc2239327c5edb3a432268e5831", "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"},
                 "USDT": {"0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"}, "DAI": {"0xda10009cbd5d07dd0cecc66161fc93d7c9000da1"},
                 "WBTC": {"0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"}, "WSTETH": {"0x5979d7b546e38e414f7e9822514be443a4800529"},
                 "ETH": set()},
    # Seit 2.63.1 (Binance-Abfluesse 29.10.2026: gefaelschte "AVAX" und "USDt" von Doppelgaenger-Adressen auf Avalanche)
    "avalanche": {"USDT": {"0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7"}, "USDC": {"0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e"},
                  "AVAX": set(), "WAVAX": {"0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7"}, "ETH": set()},
}


def falsch(sym, chain=None, token=None):
    """True fuer gefaelschte Tokens: unsichtbare Zeichen, ein bekannter Name aus fremden Buchstaben, oder ein bekannter
    Name mit dem falschen Vertrag."""
    import unicodedata
    s = str(sym or "")
    if any(unicodedata.category(c) in ("Cf", "Mn") for c in s):
        return True
    t = s.translate(_GLEICH)
    sauber = "".join(c for c in t if c.isalnum()).upper()
    if sauber in _GROSSE and t != s:
        return True
    echt = (_ECHT.get(chain) or {}).get(sauber)
    return echt is not None and bool(token) and str(token).lower() not in echt


def _sym_sol(mint):
    def hol():
        try:
            import scan
            return (scan.markt(mint, "solana", scan._paare(mint)) or {}).get("symbol")
        except Exception:
            return None
    return _cache(("sym", mint), 3600, hol) or (mint[:4] + "…")


def _usd(chain, it):
    """Dollarwert eines Postens, wenn wir ihn kennen (fuer die Reihenfolge der Spur)."""
    try:
        if not it.get("token"):
            import motor
            p = _cache(("px", it["asset"]), 300, lambda: (motor.preis(it["asset"]) or {}).get("usd"))
            return (p or 0) * it["amount"]
        import scan
        p = _cache(("tp", chain, it["token"]), 300, lambda: (scan.zahlen(chain, it["token"]) or {}).get("price"))
        return (p or 0) * it["amount"]
    except Exception:
        return 0


def _text(chain, it):
    a = it.get("asset") or (_sym_sol(it["token"]) if chain == "solana" else "token")
    m = it["amount"]
    z = f"{m:,.0f}" if m >= 1000 else f"{m:,.4g}"
    return f"{z} {'$' + a if it.get('token') and not a.startswith('$') else a}"


def einordnen(chain, w, f, mit_bridge=True):
    """Eine Bewegung -> Liste von Schritten {"kind", ...}."""
    import spur
    schritte = []
    raus, rein = f["raus"], f["rein"]
    beleg = _link(chain, "tx", f["hash"])
    wann = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(f["ts"])) if f.get("ts") else None
    if raus and rein and {(x.get("token") or x.get("asset")) for x in raus} != {(x.get("token") or x.get("asset")) for x in rein}:
        return [{"kind": "swap", "gave": [_text(chain, x) for x in raus], "got": [_text(chain, x) for x in rein],
                 "usd": round(sum(_usd(chain, x) for x in raus), 2), "when": wann, "proof": beleg, "tx": f["hash"]}]
    if raus:
        ziele = {}
        for x in raus:
            ziele.setdefault(x.get("to") or f.get("an"), []).append(x)
        for ziel, posten in ziele.items():
            usd = round(sum(_usd(chain, x) for x in posten), 2)
            basis = {"amount": [_text(chain, x) for x in posten], "usd": usd, "to": ziel, "when": wann, "proof": beleg, "tx": f["hash"],
                     "ts": f.get("ts")}
            nm = spur.name(chain, ziel) if ziel else {"kind": "contract"}
            br = bridge(chain, f["hash"]) if mit_bridge and nm["kind"] in ("contract", "service", "program", "exchange") else None
            if br and br.get("to_chain"):
                schritte.append(dict(basis, kind="bridge", bridge=br["bridge"], to_chain=br["to_chain"], to=br.get("to") or ziel,
                                     received=br.get("amount_out"), arrival_proof=_link(br["to_chain"], "tx", br["to_tx"]) if br.get("to_tx") else None))
            elif nm["kind"] in ("exchange", "service"):
                schritte.append(dict(basis, kind="cash_out", to_name=nm.get("name")))
            elif nm["kind"] == "wallet":
                schritte.append(dict(basis, kind="send"))
            elif nm["kind"] == "burn":
                schritte.append(dict(basis, kind="burn"))
            else:
                schritte.append(dict(basis, kind="contract", to_name=nm.get("name")))
        return schritte
    if rein:
        return [{"kind": "receive", "got": [_text(chain, x) for x in rein], "from": rein[0].get("from"),
                 "usd": round(sum(_usd(chain, x) for x in rein), 2), "when": wann, "proof": beleg, "tx": f["hash"]}]
    return []


def bewegungen(adr, chain=None, n=40, seit=None, nach_wert=False):
    """Was hat das Wallet zuletzt gemacht? Neueste zuerst. nach_wert: die groessten Abfluesse ueber die ganze Geschichte
    (neueste und aelteste Eintraege), fuer die Geldspur."""
    import spur
    a = str(adr or "").strip()
    chain = spur._kette_wallet(a, chain)
    if chain not in FOLGBAR:
        return {"error": "Money flow works on Solana, Robinhood Chain, Base, Ethereum and Arbitrum."}
    w = a if chain in ("solana", "tron", "ton") else a.lower()
    roh = _fluesse_sol(w, n) if chain == "solana" else _fluesse_evm(chain, w, max(n, 60), alt=150 if nach_wert else 0)
    if seit:
        roh = [f for f in roh if f["ts"] >= seit]
    if nach_wert:
        wert = lambda f: sum(_usd(chain, x) for x in f["raus"])
        roh = sorted([f for f in roh if f["raus"]], key=lambda f: -wert(f))
    out = []
    bis = time.time() + BUDGET
    for f in roh[:n]:
        if not f["raus"] and not f["rein"]:
            continue
        out.extend(einordnen(chain, w, f, mit_bridge=time.time() < bis))
    r = {"chain": chain, "wallet": w, "moves": out}
    if _GIFT.get(w):
        r["poisoning"] = {"lookalike_addresses": sorted(_GIFT[w])[:5],
                          "meaning": "Someone else created fake token transfers that only look like they came from this wallet, to "
                                     "look-alike addresses of wallets it really paid. The owner did not send them. That is address "
                                     "poisoning: a scam that hopes the owner copies the wrong address from the history. Left out of "
                                     "the trail. Never say this wallet sent fake tokens."}
    return r


# ── Vorwaerts folgen ─────────────────────────────────────────────────────

def folgen(adr, chain=None, tiefe=3, breite=3, seit=None, bis=None):
    """Dem Geld eines Wallets folgen: Weiterleitungen und Bridges, bis Boerse, Burn oder Ende."""
    import spur
    bis = bis or time.time() + BUDGET * 2
    a = str(adr or "").strip()
    chain = spur._kette_wallet(a, chain)
    start = a if chain in ("solana", "tron", "ton") else a.lower()
    gesehen = set()
    endpunkte = []

    def schritt(ch, w, ebene, ab):
        if (ch, w) in gesehen or time.time() > bis:
            return []
        gesehen.add((ch, w))
        if ch not in FOLGBAR:
            endpunkte.append({"kind": "unsupported_chain", "chain": ch, "wallet": w})
            return []
        b = bewegungen(w, ch, n=30 if ebene == 1 else 12, seit=ab, nach_wert=True)
        moves = [m for m in b.get("moves") or [] if m["kind"] in ("send", "bridge", "cash_out", "swap", "contract", "burn")]
        knoten = []
        weiter = sorted([m for m in moves if m["kind"] in ("send", "bridge")], key=lambda m: -(m.get("usd") or 0))[:breite]
        for m in moves:
            if m["kind"] == "cash_out":
                endpunkte.append({"kind": "exchange", "chain": ch, "wallet": m["to"], "name": m.get("to_name"), "proof": m["proof"]})
            knoten.append(m)
        # Seit 2.56: viele kleine Schritte zusammenfassen, sonst wird die Antwort abgeschnitten
        if len(knoten) > ZEIGEN:
            knoten.sort(key=lambda m: -(m.get("usd") or 0))
            rest = knoten[ZEIGEN:]
            arten = {}
            for m in rest:
                arten[m["kind"]] = arten.get(m["kind"], 0) + 1
            knoten = knoten[:ZEIGEN] + [{"kind": "more", "count": len(rest), "usd": round(sum(m.get("usd") or 0 for m in rest)),
                                         "kinds": arten, "to_wallets": len({m.get("to") for m in rest if m.get("to")})}]
        if ebene >= tiefe:
            return knoten
        for m in weiter:
            if m["kind"] == "bridge":
                ziel_ch = m["to_chain"]
                m["next"] = schritt(ziel_ch, m["to"] if ziel_ch in ("solana", "tron", "ton") else str(m["to"] or "").lower(), ebene + 1, m.get("ts") or ab)
            else:
                m["next"] = schritt(ch, m["to"], ebene + 1, ab)
        return knoten

    baum = schritt(chain, start, 1, seit)

    def schlank(k):
        for m in k:
            m.pop("tx", None)
            m.pop("ts", None)
            if m.get("next"):
                schlank(m["next"])
    schlank(baum)
    extra = {"poisoning_seen": sorted(_GIFT.get(start, set()))[:5]} if _GIFT.get(start) else {}
    return dict(extra, **{"chain": chain, "wallet": start, "trail": baum, "ends_at_exchange": endpunkte,
            "how_to_answer": "Tell the path in order: what was swapped into what, where it was sent, which bridge to which chain, and "
                             "where it ended (an exchange deposit, a fresh wallet, still sitting there). One line per step with the "
                             "proof link. Money that reaches an exchange can only be identified by that exchange: say the user can "
                             "report the transaction to them. Never name the person behind a wallet. Normal things stay normal: creator fees "
                             "claimed from the Torch fee router, buying, selling and moving to an own wallet are not suspicious by "
                             "themselves, describe them neutrally. Dollar values are at today's price, say so once. A 'more' "
                             "entry sums up the smaller steps on that level (count, total, how many wallets): mention it as one line. "
                             "Lead with the biggest amounts: the trail is sorted by value "
                             "over the whole history, not by date. If many fresh wallets got similar round amounts, say the money "
                             "was split up. A trail that stops at unlabeled wallets means no exchange is proven yet, not that "
                             "none was used. If poisoning_seen is there, warn that someone planted look-alike addresses in "
                             "this wallet's history with fake transfers; the wallet did not send them."})


def herkunft_bridge(chain, h):
    """Fuer die Geldspur rueckwaerts: kam diese Zahlung ueber eine Bridge? -> {"from_chain","from","from_tx",...} oder None"""
    r = relay(h)
    if r and r.get("from_chain") and r.get("from"):
        return r
    return None


# ── Drain-Report (Wallet Guard) ──────────────────────────────────────────

def drain_merken(adr, chain, opfer, dieb, tx, taeter=None, gestohlen=None):
    """Aus wache: Tokens wurden mit einer alten Freigabe abgezogen. Wir verfolgen den Empfaenger 30 Minuten lang.
    Seit 2.55 in der Kartei: der Ausfuehrende als 'drainer', der Empfaenger als 'drain_dest', jeweils mit Beleg."""
    try:
        import kartei
        beleg = _link(chain, "tx", tx) or tx
        if taeter:
            kartei.markieren(chain, taeter, "drainer", "moved a Your Engine user's tokens out with an old approval", beleg)
        if dieb and dieb != taeter:
            kartei.markieren(chain, dieb, "drain_dest", "received tokens taken in a drain", beleg)
    except Exception as e:
        log.debug(f"drain kartei: {e}")
    try:
        l = json.loads(storage.kv_get("drain_follow") or "[]")
    except Exception:
        l = []
    if any(x["tx"] == tx and x["adr"] == adr for x in l):
        return
    l.append({"adr": adr, "chain": chain, "victim": opfer, "thief": dieb, "tx": tx, "ts": int(time.time()), "n": 0,
              "stolen": gestohlen})
    storage.kv_set("drain_follow", json.dumps(l[-100:]))


def _kname(ch):
    import scan
    return (scan.KETTEN.get(ch) or (str(ch).title(),))[0]


def _lage(f):
    """Seit 2.56: der Stand in einem Satz, damit jeder sofort weiss, was los ist. -> (Status, Was jetzt)"""
    boersen = f.get("ends_at_exchange") or []
    schritte, wallets, bruecken = [], set(), []

    def sammeln(k):
        for m in k:
            schritte.append(m)
            if m["kind"] in ("send", "more") and m.get("to"):
                wallets.add(m["to"])
            if m["kind"] == "more":
                wallets.update(f"more{i}" for i in range(m.get("to_wallets") or m.get("count") or 0))
            if m["kind"] == "bridge":
                bruecken.append(_kname(m.get("to_chain")))
            sammeln(m.get("next") or [])
    sammeln(f.get("trail") or [])
    if boersen:
        namen = sorted({b.get("name") or "an exchange" for b in boersen})
        return (f"🚨 The money reached {', '.join(namen[:2])}.",
                "Open a support ticket with them RIGHT NOW and paste this report. They can freeze the deposit, but only fast.")
    if not schritte:
        return ("⏳ The money is still sitting in the thief's wallet.",
                "Revoke the approval now. We keep watching that wallet and tell you the moment it moves.")
    if bruecken:
        return (f"🌉 The money was moved to {', '.join(dict.fromkeys(bruecken))}. No exchange yet.",
                "Revoke the approval and secure what's left. We keep following it and tell you if it reaches an exchange.")
    if len(wallets) >= 5:
        return (f"🔀 The money was split across {len(wallets)} wallets. No exchange yet.",
                "Revoke the approval and secure what's left. Splitting usually comes right before a cash out, keep this report ready.")
    return ("👀 The money moved, but has not reached an exchange yet.",
            "Revoke the approval and secure what's left. Keep this report ready for the exchange.")


def _zahl(m):
    return f"{m:,.0f}" if m >= 1000 else f"{m:,.4g}"


def _report_text(x, f):
    wem = "Wallet" if x.get("fremd") else "Your wallet"
    status, jetzt = _lage(f)
    zeilen = ["🧾 DRAIN REPORT", f"{wem} {x['victim'][:6]}…{x['victim'][-4:]}", "",
              status, f"👉 {jetzt}", "",
              "WHAT HAPPENED"]
    if x.get("stolen"):
        zeilen.append(f"Taken: {x['stolen']}")
    zeilen.append(f"Taken by: {x['thief'][:6]}…{x['thief'][-4:]}")
    if x.get("tx"):
        zeilen.append(f"Proof: {_link(x.get('chain') or 'ethereum', 'tx', x['tx']) or x['tx']}")
    zeilen += ["", "WHERE THE MONEY WENT"]
    vorher = len(zeilen)

    def gehen(knoten, tief=0):
        for m in knoten:
            pre = "  " * tief + "• "
            if m["kind"] == "swap":
                zeilen.append(f"{pre}swapped {', '.join(m['gave'])} into {', '.join(m['got'])}\n{'  ' * tief}  {m['proof']}")
            elif m["kind"] == "send":
                zeilen.append(f"{pre}sent {', '.join(m['amount'])} to {m['to'][:6]}…{m['to'][-4:]}\n{'  ' * tief}  {m['proof']}")
            elif m["kind"] == "bridge":
                zeilen.append(f"{pre}bridged {', '.join(m['amount'])} with {m['bridge']} to {_kname(m['to_chain'])}\n{'  ' * tief}  {m['proof']}")
            elif m["kind"] == "more":
                zeilen.append(f"{pre}and {m['count']} more transfers to {m.get('to_wallets') or m['count']} wallets")
            elif m["kind"] == "cash_out":
                zeilen.append(f"{pre}🚨 deposited {', '.join(m['amount'])} at {m.get('to_name') or 'an exchange'}\n{'  ' * tief}  {m['proof']}")
            gehen(m.get("next") or [], tief + 1)
    gehen(f.get("trail") or [])
    if len(zeilen) == vorher:
        zeilen.append("• nothing moved yet, the tokens are still in that wallet")
    zeilen += ["", "WHAT TO DO",
               "1. Revoke the approval and move everything left to a fresh wallet.",
               "2. If the money reached an exchange, open a ticket with them now and paste this report. Exchanges can freeze a deposit, but only fast.",
               "3. File a police report where you live and keep this report.",
               "4. Ignore anyone who offers to recover your funds for a fee. That is always the next scam.",
               "", "Your Engine cannot reverse a drain. It shows you where the money went."]
    return "\n".join(zeilen)


def widerruf_knoepfe(adr, wallet):
    """Seit 2.56: Knoepfe 'Revoke' fuer offene riskante Freigaben, wenn das ausgeraubte Wallet dem Nutzer gehoert."""
    try:
        import wache
        r = wache.pruefen(adr, wallet)
    except Exception as e:
        log.debug(f"report widerruf: {e}")
        return None
    k = [(f"Revoke all {g['count']} risky", g["link"]) for g in (r.get("revoke_all") or [])[:1]]
    k += [(f"Revoke {x['token']}"[:30], x["revoke_link"]) for x in (r.get("approvals") or [])
          if x.get("revoke_link") and x.get("risk") != "ok"][:3]
    return k or None


def drain_suchen(w, chain=None, n=6):
    """Seit 2.56: die juengste Abbuchung eines Wallets, die NICHT das Wallet selbst ausgeloest hat (Drain-Muster).
    -> (tx_hash, chain) oder None. Prueft je Kette die letzten ausgehenden Token-Transfers."""
    import spur
    ketten = [chain] if chain in ("ethereum", "base", "arbitrum", "robinhood") else ["robinhood", "base", "ethereum", "arbitrum"]
    treffer = []
    for ch in ketten:
        gesehen = set()
        for x in _bs_liste(ch, "tokentx", w, 60):
            if str(x.get("from") or "").lower() != w or falsch(x.get("tokenSymbol"), ch, x.get("contractAddress")):
                continue
            h = x.get("hash")
            if not h or h in gesehen or str(x.get("to") or "").lower() in ("0x" + "0" * 40,):
                continue
            gesehen.add(h)
            if len(gesehen) > n:
                break
            try:
                _, tx, _rc = spur._evm_tx_finden(h, ch)
            except Exception:
                tx = None
            if tx and str(tx.get("from") or "").lower() != w:
                treffer.append((int(x.get("timeStamp") or 0), h, ch))
                break
    if not treffer:
        return None
    _, h, ch = max(treffer)
    return h, ch


def report_fuer_tx(h, opfer=None, chain=None):
    """Seit 2.56: Drain-Report auf Zuruf, fuer jede Drain-Transaktion (auch ohne Wallet Guard). Wer hat die Tokens
    genommen, und wohin ging das Geld seitdem. Markiert nichts in der Kartei, weil die Angabe nicht geprueft ist."""
    import re as _re
    import spur
    import wache
    m = _re.search(r"(0x[0-9a-fA-F]{64})", str(h or ""))
    if not m:
        w = _re.search(r"(0x[0-9a-fA-F]{40})(?![0-9a-fA-F])", str(h or ""))
        if w:
            gefunden = drain_suchen(w.group(1).lower(), chain)
            if not gefunden:
                return {"error": "I found no drain on this wallet: no tokens left it in a transaction that someone else sent. "
                                 "If you have the hash of the drain, send it."}
            h, chain = gefunden
            opfer = opfer or w.group(1).lower()
            m = _re.search(r"(0x[0-9a-fA-F]{64})", h)
        else:
            return {"error": "Send the transaction hash of the drain (0x… with 64 characters) or the drained wallet. Solana drains are not supported here yet."}
    ch, tx, rc = spur._evm_tx_finden(m.group(1).lower(), chain)
    if not tx:
        return {"error": "I could not find that transaction on Robinhood Chain, Base, Ethereum, Arbitrum or BNB Chain."}
    sender = str(tx.get("from") or "").lower()
    opfer = str(opfer or "").lower() or None
    T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    posten = []
    for l in (rc or {}).get("logs") or []:
        t = l.get("topics") or []
        if len(t) == 3 and t[0] == T:
            von, an = "0x" + t[1][-40:], "0x" + t[2][-40:]
            try:
                menge = int(l.get("data") or "0x0", 16)
            except Exception:
                menge = 0
            if menge:
                posten.append({"from": von, "to": an, "token": l["address"].lower(), "raw": menge})
    wert = int(tx.get("value") or "0x0", 16)
    if not posten and not wert:
        # Coins, die ein Vertrag (z. B. eine Safe) intern verschickt hat, stehen nicht in den Logs
        try:
            import intel
            d = intel._bs(ch, "/api", {"module": "account", "action": "txlistinternal", "txhash": m.group(1)}) or {}
            for x in d.get("result") or [] if isinstance(d.get("result"), list) else []:
                v = int(x.get("value") or 0)
                if v and str(x.get("isError") or "0") == "0":
                    posten.append({"from": str(x.get("from") or "").lower(), "to": str(x.get("to") or "").lower(), "token": None, "raw": v})
        except Exception as e:
            log.debug(f"report intern: {e}")
    if not opfer:
        # Typischer Drain: Tokens verlassen ein Wallet, das die Transaktion NICHT geschickt hat (alte Freigabe)
        fremd = [p for p in posten if p["from"] != sender]
        opfer = (fremd[0]["from"] if fremd else (sender if posten or wert else None))
    if not opfer:
        return {"error": "No token or coin left a wallet in this transaction, so it does not look like a drain."}
    raus = [p for p in posten if p["from"] == opfer]
    if raus:
        ziele = {}
        for p in raus:
            ziele[p["to"]] = ziele.get(p["to"], 0) + 1
        dieb = max(ziele, key=ziele.get)
    elif sender == opfer and wert:
        dieb = str(tx.get("to") or "").lower()
    else:
        return {"error": f"Nothing left {opfer[:6]}…{opfer[-4:]} in this transaction."}
    try:
        b = wache._evm_rpc(ch)("eth_getBlockByNumber", [tx["blockNumber"], False])
        ts = int(b["timestamp"], 16)
    except Exception:
        ts = None
    f = folgen(dieb, ch, tiefe=3, breite=3, seit=(ts - 60) if ts else None)
    gestohlen = []
    for p in raus:
        try:
            if p["token"]:
                sym, dez = wache._symbol(ch, p["token"])
            else:
                sym, dez = ("BNB" if ch == "bsc" else "ETH"), 18
            gestohlen.append(f"{_zahl(p['raw'] / 10 ** dez)} {sym}")
        except Exception:
            pass
    if not raus and wert:
        gestohlen.append(f"{_zahl(wert / 1e18)} {'BNB' if ch == 'bsc' else 'ETH'}")
    x = {"victim": opfer, "thief": dieb, "tx": m.group(1), "chain": ch, "fremd": True, "stolen": ", ".join(gestohlen[:4]) or None}
    return {"chain": ch, "victim": opfer, "taken_by": dieb, "executed_by": sender if sender != opfer else None,
            "drain_proof": _link(ch, "tx", m.group(1)), "report": _report_text(x, f), "reached_exchange": f.get("ends_at_exchange"),
            "how_to_answer": "Send the report text as it is, then one short line: if it reached an exchange, open a ticket with "
                             "that exchange right now and paste the report. Never promise the money comes back."}


def drain_runde():
    """Aus motor.tick: offene Drains nach 3, 10 und 30 Minuten verfolgen und den Report schicken."""
    try:
        l = json.loads(storage.kv_get("drain_follow") or "[]")
    except Exception:
        l = []
    if not l:
        return 0
    import motor
    jetzt, rest, gesendet = time.time(), [], 0
    stufen = (180, 600, 1800)
    for x in l:
        # Seit 2.62.7: ein "Drain" mit einem gefaelschten Token (z. B. "ETH" aus fremden Buchstaben) war Spam.
        # Nicht weiter verfolgen und die Markierungen aus der Kartei wieder entfernen, sonst stehen Unbeteiligte als Drainer da.
        try:
            teile = str(x.get("stolen") or "").split(" ", 1)
            if len(teile) == 2 and falsch(teile[1].replace(" NFT", ""), x.get("chain")):
                try:
                    import kartei
                    kartei._q("DELETE FROM wl_flags WHERE proof LIKE %s", ("%" + str(x.get("tx")) + "%",))
                except Exception:
                    pass
                log.info(f"drain spam dropped {x.get('tx')}")
                continue
        except Exception:
            pass
        alter = jetzt - x["ts"]
        if x["n"] >= len(stufen):
            continue
        if alter < stufen[x["n"]]:
            rest.append(x)
            continue
        try:
            f = folgen(x["thief"], x["chain"], tiefe=3, breite=3, seit=x["ts"] - 60)
        except Exception as e:
            log.warning(f"drain folgen: {e}")
            f = {}
        x["n"] += 1
        bei_boerse = bool(f.get("ends_at_exchange"))
        schon = x.get("sent_hash")
        neu = json.dumps(f.get("trail") or [], sort_keys=True, default=str)[:4000]
        if bei_boerse or x["n"] >= len(stufen) or (f.get("trail") and neu != schon):
            if neu != schon:
                motor.senden(x["adr"], _report_text(x, f), widerruf_knoepfe(x["adr"], x["victim"]))
                x["sent_hash"] = neu
                gesendet += 1
        if x["n"] < len(stufen) and not bei_boerse:
            rest.append(x)
    storage.kv_set("drain_follow", json.dumps(rest))
    return gesendet
