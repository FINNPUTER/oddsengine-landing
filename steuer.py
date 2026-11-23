"""
steuer.py: Steuer-Export fuer ganze Wallets ueber alle Chains (seit 2.57).

Was rauskommt (Excel):
  Summary       je Jahr: Erloese, Kosten, Gewinn, davon Haltedauer bis 1 Jahr und ueber 1 Jahr, Gebuehren
  Sales         jeder Verkauf (auch Tausch gegen einen anderen Coin) mit Kaufdatum, Haltedauer, Kosten, Erloes, Gewinn
  Transactions  jede Bewegung eingeordnet (Kauf, Verkauf, Tausch, Eingang, Ausgang, eigene Umbuchung, Bridge)
  Holdings      was noch da ist, mit Einstandswert
  Notes         Methode und Grenzen

Methode: FIFO ueber alle angegebenen Wallets zusammen (ein Bestand je Coin). Werte zum Zeitpunkt der Transaktion:
ETH und SOL aus stuendlichen Coinbase-Kerzen (USD und EUR), Stablecoins 1 USD, EUR fuer Stablecoins ueber das
Verhaeltnis ETH-EUR zu ETH-USD derselben Stunde. Ein Tausch Coin gegen Coin ohne ETH, SOL oder Stablecoin auf
einer Seite hat keinen bekannten Wert, das steht dann in der Zeile.
Gilt fuer Solana, Ethereum, Base, Arbitrum, Optimism, Robinhood Chain und seit 2.82.49 TRON und TON (steuer_tt.py).
"""
import io
import json
import os
import threading
import time

import requests

from logger import log

# Seit 2.63.1 auch Optimism (nativ ETH). Chains mit anderem nativen Coin (Polygon, BNB, Avalanche) werden als
# "Aktivitaet gefunden, nicht abgedeckt" gemeldet statt still zu fehlen, weil hier alles Native als ETH gebucht wird.
KETTEN = ("robinhood", "base", "ethereum", "arbitrum", "optimism")
MAX_TX = int(os.getenv("TAX_MAX_TX", "5000"))
UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
SOL_STABLE = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
              "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH": "USDG", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": "PYUSD"}
STABLE_SYM = {"USDC", "USDT", "DAI", "USDG", "PYUSD", "USDE", "USDC.E", "USDBC", "FDUSD"}
_LAEUFT = {}


# ── Historische Preise ───────────────────────────────────────────────────

_KERZEN = {}
BLOCK = 288 * 3600  # 288 Stundenkerzen je Abfrage (Coinbase erlaubt 300)


def _kerzen(produkt, ts):
    """Schlusskurs der Stunde, in der ts liegt (Coinbase Exchange, stuendlich)."""
    b = int(ts // BLOCK)
    k = (produkt, b)
    if k not in _KERZEN:
        start, ende = b * BLOCK, (b + 1) * BLOCK
        d = {}
        for versuch in range(3):
            try:
                r = requests.get(f"https://api.exchange.coinbase.com/products/{produkt}/candles",
                                 params={"granularity": 3600, "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                                         "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ende))}, headers=UA, timeout=15)
                if r.status_code == 429:
                    time.sleep(1.5 * (versuch + 1))
                    continue
                for x in r.json() or []:
                    d[int(x[0]) // 3600] = float(x[4])
                break
            except Exception:
                time.sleep(0.8)
        _KERZEN[k] = d
    d = _KERZEN[k]
    h = int(ts // 3600)
    for dh in (0, -1, 1, -2, 2, -3, 3, -6, 6, -12, 12):
        if h + dh in d:
            return d[h + dh]
    return None


def kurs(asset, ts):
    """-> (usd, eur) je Einheit fuer ETH, SOL oder einen Stablecoin, sonst (None, None)."""
    if asset in ("ETH", "SOL"):
        u, e = _kerzen(f"{asset}-USD", ts), _kerzen(f"{asset}-EUR", ts)
        if u and not e:
            eu, ee = _kerzen("ETH-USD", ts), _kerzen("ETH-EUR", ts)
            e = u * ee / eu if eu and ee else None
        return u, e
    if asset in ("TRX", "TON"):
        import steuer_tt
        u = steuer_tt.kurs_usd(asset, ts)
        eu, ee = _kerzen("ETH-USD", ts), _kerzen("ETH-EUR", ts)
        return u, (u * ee / eu if u and eu and ee else None)
    if asset == "USD":
        eu, ee = _kerzen("ETH-USD", ts), _kerzen("ETH-EUR", ts)
        return 1.0, (ee / eu if eu and ee else None)
    return None, None


# ── Bewegungen einsammeln ────────────────────────────────────────────────

def _art_evm(chain, token, sym):
    """Coin-Schluessel und Preisklasse fuer einen EVM-Token."""
    import fluss
    t = str(token or "").lower()
    echt = fluss._ECHT.get(chain) or {}
    if t in (echt.get("WETH") or set()) or (chain == "robinhood" and str(sym).upper() == "WETH"):
        return "ETH", "ETH", "ETH"
    s = str(sym or "").upper()
    for st in ("USDC", "USDT", "DAI", "USDE"):
        if t in (echt.get(st) or set()):
            return f"{chain}:{t}", st, "USD"
    if chain == "robinhood" and s in STABLE_SYM and not fluss.falsch(sym, chain, token):
        return f"{chain}:{t}", s, "USD"
    return f"{chain}:{t}", (sym or t[:8]), None


_NICHT_LESBAR = set()


def _seiten(chain, action, w):
    import intel
    out = []
    for seite in range(1, MAX_TX // 1000 + 2):
        d = intel._bs(chain, "/api", {"module": "account", "action": action, "address": w, "sort": "asc",
                                      "page": seite, "offset": 1000}) or {}
        if seite == 1 and not isinstance(d.get("result"), list) and "No " not in str(d.get("message") or ""):
            # Seit 2.62.3: Explorer nicht erreichbar (Cloudflare, Grenze, Schluessel fehlt), nicht still als "keine Trades" werten
            _NICHT_LESBAR.add(chain)
            log.warning(f"steuer: {chain} explorer not readable for {action}: {str(d)[:120]}. "
                        "Set BLOCKSCOUT_API_KEY (free at dev.blockscout.com).")
        if seite > 1 and not isinstance(d.get("result"), list):
            # Seit 2.63.1: eine spaetere Seite fehlt, die Liste waere sonst still unvollstaendig
            _NICHT_LESBAR.add(chain)
            log.warning(f"steuer: {chain} page {seite} of {action} not readable for {w}")
        r = d.get("result") if isinstance(d.get("result"), list) else []
        out += r
        if len(r) < 1000 or len(out) >= MAX_TX:
            break
    return out, len(out) >= MAX_TX


def evm_ereignisse(chain, w):
    """Alle Bewegungen eines EVM-Wallets auf einer Chain -> Liste von Ereignissen."""
    import fluss
    w = w.lower()
    ev, voll = {}, True

    def e(h, ts):
        return ev.setdefault(h, {"chain": chain, "tx": h, "ts": int(ts or 0), "wallet": w, "rein": [], "raus": [], "fee": 0.0})
    txs, abgeschnitten = _seiten(chain, "txlist", w)
    voll = voll and not abgeschnitten
    for x in txs:
        h = x.get("hash")
        if not h:
            continue
        x_e = e(h, x.get("timeStamp"))
        von, an, v = str(x.get("from") or "").lower(), str(x.get("to") or "").lower(), int(x.get("value") or 0)
        if von == w:
            try:
                x_e["fee"] += int(x.get("gasUsed") or 0) * int(x.get("gasPrice") or 0) / 1e18
            except Exception:
                pass
        if str(x.get("isError") or "0") != "0":
            continue
        if v and von == w:
            x_e["raus"].append(("ETH", "ETH", "ETH", v / 1e18, an))
        elif v and an == w:
            x_e["rein"].append(("ETH", "ETH", "ETH", v / 1e18, von))
    intern, abgeschnitten = _seiten(chain, "txlistinternal", w)
    voll = voll and not abgeschnitten
    for x in intern:
        h = x.get("transactionHash") or x.get("hash")
        v = int(x.get("value") or 0)
        if not h or not v or str(x.get("isError") or "0") != "0":
            continue
        von, an = str(x.get("from") or "").lower(), str(x.get("to") or "").lower()
        if an == w:
            e(h, x.get("timeStamp"))["rein"].append(("ETH", "ETH", "ETH", v / 1e18, von))
        elif von == w:
            e(h, x.get("timeStamp"))["raus"].append(("ETH", "ETH", "ETH", v / 1e18, an))
    toks, abgeschnitten = _seiten(chain, "tokentx", w)
    voll = voll and not abgeschnitten
    for x in toks:
        try:
            menge = int(x.get("value") or 0) / 10 ** int(x.get("tokenDecimal") or 18)
        except Exception:
            continue
        if not menge or fluss.falsch(x.get("tokenSymbol"), chain, x.get("contractAddress")):
            continue
        key, sym, klasse = _art_evm(chain, x.get("contractAddress"), x.get("tokenSymbol"))
        von, an = str(x.get("from") or "").lower(), str(x.get("to") or "").lower()
        if von == w:
            e(x.get("hash"), x.get("timeStamp"))["raus"].append((key, sym, klasse, menge, an))
        elif an == w:
            e(x.get("hash"), x.get("timeStamp"))["rein"].append((key, sym, klasse, menge, von))
    for x in ev.values():
        x["fee_asset"] = "ETH"
    return list(ev.values()), voll


def sol_ereignisse(w):
    import fluss
    import intel
    import spaeher
    sigs, vor = [], None
    while len(sigs) < MAX_TX:
        opt = {"limit": 1000}
        if vor:
            opt["before"] = vor
        r = intel._sol("getSignaturesForAddress", [w, opt]) or []
        if not r:
            break
        sigs += [x for x in r if not x.get("err")]
        vor = r[-1].get("signature")
        if len(r) < 1000:
            break
    voll = len(sigs) < MAX_TX
    txs = intel._sol_batch([intel._tx(x["signature"]) for x in sigs], 20) if sigs else []
    out = []
    for s, t in zip(sigs, txs):
        if not isinstance(t, dict):
            continue
        e_ = spaeher.sol_einordnen(t, w)
        if not e_:
            continue
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (((t.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])]
        fee = ((t.get("meta") or {}).get("fee") or 0) / 1e9 if keys and keys[0] == w else 0.0
        x = {"chain": "solana", "tx": s["signature"], "ts": s.get("blockTime") or t.get("blockTime") or 0, "wallet": w,
             "rein": [], "raus": [], "fee": fee, "fee_asset": "SOL"}
        for mint, m in e_.get("rein") or []:
            sym = SOL_STABLE.get(mint) or fluss._sym_sol(mint)
            x["rein"].append((f"solana:{mint}", sym, "USD" if mint in SOL_STABLE else None, m, e_.get("gegen")))
        for mint, m in e_.get("raus") or []:
            sym = SOL_STABLE.get(mint) or fluss._sym_sol(mint)
            x["raus"].append((f"solana:{mint}", sym, "USD" if mint in SOL_STABLE else None, m, e_.get("gegen")))
        sol = e_.get("sol") or 0
        if sol > 1e-6:
            x["rein"].append(("SOL", "SOL", "SOL", sol, e_.get("gegen")))
        elif sol < -1e-6:
            x["raus"].append(("SOL", "SOL", "SOL", -sol, e_.get("gegen")))
        if x["rein"] or x["raus"] or fee:
            out.append(x)
    return out, voll


# ── Rechnen ──────────────────────────────────────────────────────────────

def _netto(x):
    """Gleicher Coin rein und raus in derselben Transaktion (z. B. ETH zu WETH): nur der Saldo zaehlt."""
    s = {}
    meta = {}
    for k, sym, kl, m, geg in x["rein"]:
        s[k] = s.get(k, 0) + m
        meta[k] = (sym, kl, geg)
    for k, sym, kl, m, geg in x["raus"]:
        s[k] = s.get(k, 0) - m
        meta.setdefault(k, (sym, kl, geg))
        if s[k] < 0:
            meta[k] = (sym, kl, geg)
    rein = [(k, meta[k][0], meta[k][1], v, meta[k][2]) for k, v in s.items() if v > 1e-12]
    raus = [(k, meta[k][0], meta[k][1], -v, meta[k][2]) for k, v in s.items() if v < -1e-12]
    return rein, raus


def _wert(posten, ts, auch_coins=True):
    """(usd, eur) fuer bewertbare Posten, None wenn keiner bewertbar ist."""
    u = e = 0.0
    gefunden = False
    for k, sym, kl, m, _ in posten:
        if kl:
            pu, pe = kurs(kl, ts)
            if pu:
                u += m * pu
                e += m * (pe or 0)
                gefunden = True
    if gefunden or not auch_coins:
        return (u, e) if gefunden else None
    # Seit 2.62: Coin gegen Coin (z. B. gegen TORCH) ueber den historischen Kurs des Coins (GeckoTerminal, stuendlich)
    for k, sym, kl, m, _ in posten:
        pu = _coin_usd(k, ts)
        if pu:
            _, eur = kurs("USD", ts)
            u += m * pu
            e += m * pu * (eur or 0)
            gefunden = True
    return (u, e) if gefunden else None


def _coin_usd(k, ts):
    """USD je Einheit eines Coins ohne feste Preisklasse ('chain:adresse'), oder None."""
    if os.getenv("TAX_COIN_PRICES", "1") == "0" or ":" not in str(k):
        return None
    ch, a = str(k).split(":", 1)
    try:
        import preise
        return preise.usd(ch, a, ts)
    except Exception:
        return None


_SUP = {}


def _mc(k, preis):
    """Market Cap zu einem Kurs: Kurs mal Gesamtangebot heute. None fuer ETH, SOL und Stablecoins."""
    if not preis or ":" not in str(k):
        return None
    if k not in _SUP:
        ch, a = str(k).split(":", 1)
        try:
            import preise
            _SUP[k] = preise.supply(ch, a)[0]
        except Exception:
            _SUP[k] = None
    return preis * _SUP[k] if _SUP[k] else None


def _bruecken(ereignisse, eigene):
    """Ein Ausgang ETH/SOL/Stable auf einer Chain und ein Eingang derselben Art auf einer anderen Chain
    innerhalb 2 Stunden mit hoechstens 3 % Unterschied: eigene Umbuchung ueber eine Bridge.
    Seit 2.63 merkt sich der Ausgang, wohin es ging (Wallet und Coin), damit die Kaufposten mitwandern."""
    offen = []
    for x in ereignisse:
        rein, raus = _netto(x)
        if raus and not rein and len(raus) == 1 and raus[0][2]:
            offen.append(x)
    for x in ereignisse:
        rein, raus = _netto(x)
        if not (rein and not raus and len(rein) == 1 and rein[0][2]):
            continue
        for o in offen:
            if o.get("bruecke") or o["chain"] == x["chain"]:
                continue
            ro = _netto(o)[1][0]
            if ro[2] == rein[0][2] and 0 <= x["ts"] - o["ts"] <= 7200 and abs(ro[3] - rein[0][3]) <= 0.03 * ro[3]:
                o["bruecke"] = x["bruecke"] = True
                o["bruecke_zu"] = (x["wallet"], rein[0][0], rein[0][1])
                x["bruecke_von"] = o["wallet"]
                break


def _w(a):
    a = str(a or "")
    return a.lower() if a.startswith("0x") else a


PRO_WALLET = os.getenv("TAX_FIFO", "wallet") != "pool"
GAS_ALS_VERKAUF = os.getenv("TAX_FEE_AS_SALE", "1") != "0"


def rechnen(ereignisse, eigene, pro_wallet=None):
    """FIFO. -> (sales, zeilen, bestaende, gebuehren_je_jahr, ohne_wert)

    Seit 2.63:
      pro Wallet   jede Wallet hat ihren eigenen Bestand je Coin (BMF: Verbrauchsfolge je Wallet). Schickt man Coins an
                   eine eigene, mit angegebene Wallet (auch ueber eine Bridge), wandern die Kaufposten mit ihrem echten
                   Kaufdatum und Einstand mit. TAX_FIFO=pool rechnet wie frueher ueber alle Wallets zusammen.
      Gas          mit ETH oder SOL bezahlte Netzwerkgebuehren sind eine Veraeusserung dieser ETH oder SOL: eigene Zeile
                   im Sales-Blatt (Gewinn oder Verlust auf die verbrauchten Einheiten). TAX_FEE_AS_SALE=0 schaltet das ab.
    """
    if pro_wallet is None:
        pro_wallet = PRO_WALLET
    eigene = {_w(a) for a in eigene}
    ereignisse = sorted(ereignisse, key=lambda x: (x["ts"], x["tx"]))
    _bruecken(ereignisse, eigene)
    lose = {}      # (wallet, coin) -> [[menge, kosten_usd, kosten_eur, ts, tx, sym, stueckpreis_usd]]
    sales, zeilen, gebuehren = [], [], {}
    ohne_wert = 0

    def topf(w, k):
        return lose.setdefault((_w(w) if pro_wallet else "*", k), [])

    def zugang(w, k, sym, m, cu, ce, ts, tx, stueck=None):
        topf(w, k).append([m, cu, ce, ts, tx, sym, stueck])

    def umbuchen(von, nach, k, k_neu, sym_neu, m):
        """Kaufposten von einer eigenen Wallet in die andere, mit Kaufdatum und Einstand."""
        if not pro_wallet and k == k_neu:
            return
        quelle, ziel = topf(von, k), topf(nach, k_neu)
        rest = m
        while rest > 1e-12 and quelle:
            l = quelle[0]
            nimm = min(rest, l[0])
            anteil = nimm / l[0] if l[0] else 0
            cu, ce = l[1] * anteil, l[2] * anteil
            ziel.append([nimm, cu, ce, l[3], l[4], sym_neu or l[5], l[6] if len(l) > 6 else None])
            l[0] -= nimm
            l[1] -= cu
            l[2] -= ce
            if l[0] <= 1e-12:
                quelle.pop(0)
            rest -= nimm
        if rest > 1e-12:
            # Mehr umgebucht als bekannt (Herkunft nicht in den angegebenen Wallets): Rest ohne Einstand, Datum heute
            ziel.append([rest, 0.0, 0.0, None, None, sym_neu, None])
        ziel.sort(key=lambda l: (l[3] is None, l[3] or 0))

    def abgang(w, k, sym, m, eu, ee, ts, tx, chain, art, hinweis=""):
        rest = m
        liste = topf(w, k)
        while rest > 1e-12 and liste:
            l = liste[0]
            nimm = min(rest, l[0])
            anteil = nimm / l[0] if l[0] else 0
            cu, ce = l[1] * anteil, l[2] * anteil
            pu, pe = (eu * nimm / m, ee * nimm / m) if eu is not None else (None, None)
            if art == "sale":
                gekauft = l[3] if l[3] else ts
                tage = int((ts - gekauft) / 86400)
                sales.append({"sold": ts, "asset": sym, "key": k, "amount": nimm, "bought": l[3], "days": tage, "over_1y": tage > 365,
                              "buy_price_usd": l[6] if len(l) > 6 else None,
                              "sale_price_usd": (pu / nimm) if (pu is not None and nimm) else None,
                              "cost_usd": cu, "cost_eur": ce, "proceeds_usd": pu, "proceeds_eur": pe,
                              "gain_usd": (pu - cu) if pu is not None else None, "gain_eur": (pe - ce) if pe is not None else None,
                              "buy_tx": l[4], "sell_tx": tx, "chain": chain, "wallet": w,
                              "note": hinweis or ("" if l[3] else "came from a wallet that is not in this export, buy date and cost unknown")})
            l[0] -= nimm
            l[1] -= cu
            l[2] -= ce
            if l[0] <= 1e-12:
                liste.pop(0)
            rest -= nimm
        if rest > 1e-12 and art == "sale":
            pu, pe = (eu * rest / m, ee * rest / m) if eu is not None else (None, None)
            sales.append({"sold": ts, "asset": sym, "key": k, "amount": rest, "bought": None, "days": None, "over_1y": False,
                          "buy_price_usd": None, "sale_price_usd": (pu / rest) if (pu is not None and rest) else None,
                          "cost_usd": 0.0, "cost_eur": 0.0, "proceeds_usd": pu, "proceeds_eur": pe,
                          "gain_usd": pu, "gain_eur": pe, "buy_tx": None, "sell_tx": tx, "chain": chain, "wallet": w,
                          "note": (hinweis + " " if hinweis else "") + "no earlier buy found in this history, cost set to 0"})

    for x in ereignisse:
        ts, tx, ch, w = x["ts"], x["tx"], x["chain"], _w(x["wallet"])
        rein, raus = _netto(x)
        jahr = time.gmtime(ts).tm_year if ts else 0
        fee_u = fee_e = 0.0
        if x.get("fee"):
            pu, pe = kurs(x["fee_asset"], ts)
            fee_u, fee_e = x["fee"] * (pu or 0), x["fee"] * (pe or 0)
            g = gebuehren.setdefault(jahr, [0.0, 0.0])
            g[0] += fee_u
            g[1] += fee_e
            if GAS_ALS_VERKAUF and pu:
                abgang(w, x["fee_asset"], x["fee_asset"], x["fee"], fee_u, fee_e, ts, tx, ch, "sale",
                       f"network fee paid in {x['fee_asset']}")
            else:
                abgang(w, x["fee_asset"], x["fee_asset"], x["fee"], None, None, ts, tx, ch, "fee")
        intern_r = [p for p in raus if p[4] and _w(p[4]) in eigene]
        intern_e = [p for p in rein if p[4] and _w(p[4]) in eigene]
        if x.get("bruecke"):
            if x.get("bruecke_zu") and raus:
                nach, k_neu, sym_neu = x["bruecke_zu"]
                umbuchen(w, nach, raus[0][0], k_neu, sym_neu, raus[0][3])
            zeilen.append(_zeile(x, "bridge (own)", rein, raus, None, fee_u, fee_e,
                                 "buy dates and cost move with the coins" if pro_wallet else ""))
            continue
        if raus and not rein and len(intern_r) == len(raus):
            for p in raus:
                if _w(p[4]) != w:
                    umbuchen(w, p[4], p[0], p[0], p[1], p[3])
            zeilen.append(_zeile(x, "transfer between own wallets", rein, raus, None, fee_u, fee_e,
                                 "buy dates and cost move with the coins" if pro_wallet else ""))
            continue
        if rein and not raus and len(intern_e) == len(rein):
            # Die Posten hat der Ausgang der anderen eigenen Wallet schon mitgebracht
            zeilen.append(_zeile(x, "transfer between own wallets", rein, raus, None, fee_u, fee_e))
            continue
        if not rein and not raus:
            if x.get("fee"):
                zeilen.append(_zeile(x, "fee only", rein, raus, None, fee_u, fee_e))
            continue
        # Seit 2.62: zuerst die genaue Seite (ETH, SOL, Stablecoin), erst dann der Stundenkurs eines Coins
        w_raus, w_rein = _wert(raus, ts, False), _wert(rein, ts, False)
        wert = w_raus or w_rein or _wert(raus, ts) or _wert(rein, ts)
        w_raus = w_raus or (_wert(raus, ts) if raus and not rein else None)
        w_rein = w_rein or (_wert(rein, ts) if rein and not raus else None)
        if raus and rein:
            art = "buy" if all(p[2] for p in raus) else "sell" if all(p[2] for p in rein) else "swap"
            hinweis = "" if wert else "token for token swap, no price found for either coin at that time: no known value"
            if not wert:
                ohne_wert += 1
            for p in raus:
                anteil = _anteil(p, raus, ts)
                abgang(w, p[0], p[1], p[3], wert[0] * anteil if wert else None, wert[1] * anteil if wert else None,
                       ts, tx, ch, "sale", hinweis)
            for p in rein:
                anteil = _anteil(p, rein, ts)
                zugang(w, p[0], p[1], p[3], (wert[0] * anteil if wert else 0.0) + fee_u / len(rein),
                       (wert[1] * anteil if wert else 0.0) + fee_e / len(rein), ts, tx,
                       (wert[0] * anteil / p[3]) if (wert and p[3]) else None)
            zeilen.append(_zeile(x, art, rein, raus, wert, fee_u, fee_e, hinweis))
        elif rein:
            # Eingang ohne Gegenleistung (Airdrop, Zahlung, Umbuchung von fremd): Wert zum Eingang, wenn bekannt
            for p in rein:
                pw = _wert([p], ts)
                zugang(w, p[0], p[1], p[3], pw[0] if pw else 0.0, pw[1] if pw else 0.0, ts, tx,
                       (pw[0] / p[3]) if (pw and p[3]) else None)
            zeilen.append(_zeile(x, "received", rein, raus, w_rein, fee_u, fee_e,
                                 "" if w_rein else "value unknown, cost basis set to 0"))
            zeilen[-1]["from"] = next((p[4] for p in rein if p[4]), None)
        else:
            # Ausgang an fremde Adresse ohne Gegenleistung (Zahlung, Geschenk, Drain): Bestand geht raus, kein Verkauf
            for p in raus:
                abgang(w, p[0], p[1], p[3], None, None, ts, tx, ch, "out")
            zeilen.append(_zeile(x, "sent out", rein, raus, w_raus, fee_u, fee_e,
                                 "not counted as a sale. If this was a payment for something, it may be taxable: check it"))
    bestaende = []
    for (w, k), liste in lose.items():
        m = sum(l[0] for l in liste)
        if m > 1e-9:
            bestaende.append({"asset": liste[0][5], "key": k, "wallet": w, "amount": m, "cost_usd": sum(l[1] for l in liste),
                              "cost_eur": sum(l[2] for l in liste), "first_buy": min((l[3] for l in liste if l[3]), default=None)})
    return sales, zeilen, bestaende, gebuehren, ohne_wert


def herkunft(zeilen, eigene, n=6):
    """Wer hat ETH, SOL oder Stablecoins geschickt, ohne dass es eine der angegebenen Wallets ist? Das koennen eigene
    Wallets sein, die im Export fehlen: dann stimmen Kaufdatum und Einstand nicht. -> [{wallet, chain, usd, times}]"""
    eigene = {_w(a) for a in eigene}
    je = {}
    for z in zeilen:
        von = z.get("from")
        if z.get("type") != "received" or not von or _w(von) in eigene or not z.get("value_usd"):
            continue
        if not any(s in str(z.get("in") or "") for s in ("ETH", "SOL", "TRX", "TON", "USD", "DAI")):
            continue
        k = (_w(von), z["chain"])
        e = je.setdefault(k, {"wallet": von, "chain": z["chain"], "usd": 0.0, "times": 0})
        e["usd"] += z["value_usd"]
        e["times"] += 1
    liste = [e for e in je.values() if e["usd"] >= 50]
    try:
        import kartei
        liste = [e for e in liste if (kartei.art(e["chain"], e["wallet"]) or {}).get("kind") in (None, "", "wallet")]
    except Exception:
        pass
    return sorted(liste, key=lambda e: -e["usd"])[:n]


def _anteil(p, posten, ts):
    w = _wert([p], ts)
    gesamt = _wert(posten, ts)
    if w and gesamt and gesamt[0]:
        return w[0] / gesamt[0]
    bewertbar = [q for q in posten if q[2]]
    if bewertbar:
        return 0.0 if not p[2] else 1.0 / len(bewertbar)
    return 1.0 / len(posten)


def _zeile(x, art, rein, raus, wert, fee_u, fee_e, hinweis=""):
    fmt = lambda ps: ", ".join(f"{_z(p[3])} {p[1]}" for p in ps)
    # Seit 2.62: Kurs und Market Cap des gehandelten Coins (der gekaufte, sonst der verkaufte)
    coin = next((p for p in rein if not p[2] and ":" in str(p[0])), None) or next((p for p in raus if not p[2] and ":" in str(p[0])), None)
    stueck = (wert[0] / coin[3]) if (coin and wert and coin[3] and art in ("buy", "sell", "swap")) else None
    return {"ts": x["ts"], "chain": x["chain"], "wallet": x["wallet"], "type": art, "out": fmt(raus), "in": fmt(rein),
            "value_usd": wert[0] if wert else None, "value_eur": wert[1] if wert else None, "fee_usd": fee_u, "fee_eur": fee_e,
            "tx": x["tx"], "note": hinweis, "coin": coin[1] if coin else None, "coin_key": coin[0] if coin else None,
            "coin_price_usd": stueck}


def _z(m):
    return f"{m:,.0f}" if m >= 1000 else f"{m:,.6g}"


# ── Excel ────────────────────────────────────────────────────────────────

def _link(chain, tx):
    import scan
    try:
        return scan.explorer(chain, "tx", tx)
    except Exception:
        return tx


def excel(wallets, sales, zeilen, bestaende, gebuehren, ohne_wert, jahr=None, unvollstaendig=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    fett = Font(bold=True)
    kopf_f = PatternFill("solid", fgColor="0E3B24")
    kopf_s = Font(bold=True, color="FFFFFF")
    datum = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else ""
    r2 = lambda v: round(v, 2) if isinstance(v, (int, float)) else v

    def blatt(ws, kopf, breiten=None):
        ws.append(kopf)
        for c in ws[1]:
            c.font, c.fill = kopf_s, kopf_f
            c.alignment = Alignment(vertical="center")
        ws.freeze_panes = "A2"
        for i, b in enumerate(breiten or []):
            ws.column_dimensions[chr(65 + i)].width = b

    jahre = sorted({time.gmtime(s["sold"]).tm_year for s in sales} | set(gebuehren))
    if jahr:
        jahre = [j for j in jahre if j == jahr]
    ws = wb.active
    ws.title = "Summary"
    blatt(ws, ["Year", "Sales", "Proceeds EUR", "Cost EUR", "Gain EUR", "Gain EUR held up to 1 year",
               "Gain EUR held over 1 year", "Sales without known value", "Fees EUR", "Proceeds USD", "Cost USD", "Gain USD"],
          [8, 8, 15, 15, 14, 24, 24, 24, 12, 15, 15, 14])
    for j in jahre:
        s_j = [s for s in sales if time.gmtime(s["sold"]).tm_year == j]
        bek = [s for s in s_j if s["proceeds_eur"] is not None]
        kurz = sum(s["gain_eur"] for s in bek if not s["over_1y"])
        lang = sum(s["gain_eur"] for s in bek if s["over_1y"])
        ws.append([j, len(s_j), r2(sum(s["proceeds_eur"] for s in bek)), r2(sum(s["cost_eur"] for s in bek)), r2(kurz + lang),
                   r2(kurz), r2(lang), len(s_j) - len(bek), r2((gebuehren.get(j) or [0, 0])[1]),
                   r2(sum(s["proceeds_usd"] for s in bek)), r2(sum(s["cost_usd"] for s in bek)),
                   r2(sum(s["gain_usd"] for s in bek))])
    ws.append([])
    ws.append(["Wallets", ", ".join(wallets)])
    ws.append(["Method", ("FIFO per wallet (German BMF view): each wallet has its own order of buys. Coins sent between your own "
                          "listed wallets keep their buy date and cost." if PRO_WALLET else "FIFO across all listed wallets together.")
               + " Network fees paid in ETH, SOL, TRX or TON count as a disposal of that coin." * GAS_ALS_VERKAUF
               + " Values at the time of each transaction."])
    ws.append(["Germany", "Gains on coins held over 1 year are tax free (§ 23 EStG). Short term gains are tax free if the "
                          "total of all private sales gains in the year stays under the exemption limit (1,000 EUR since 2024)."])
    ws.append(["Not tax advice", "This sheet is generated from public chain data. Check it with your tax advisor."])
    for c in ws["A"]:
        c.font = fett

    ws = wb.create_sheet("Sales")
    blatt(ws, ["Sold (UTC)", "Asset", "Amount", "Bought (UTC)", "Holding days", "Over 1 year", "Cost EUR", "Proceeds EUR",
               "Gain EUR", "Cost USD", "Proceeds USD", "Gain USD", "Chain", "Sell tx", "Buy tx", "Note",
               "Price at buy USD", "Market cap at buy USD", "Price at sale USD", "Market cap at sale USD", "Wallet"],
          [17, 12, 16, 17, 12, 11, 13, 13, 12, 13, 13, 12, 11, 30, 30, 50, 16, 20, 16, 20, 14])
    for s in sales:
        if jahr and time.gmtime(s["sold"]).tm_year != jahr:
            continue
        ws.append([datum(s["sold"]), s["asset"], s["amount"], datum(s["bought"]), s["days"], "yes" if s["over_1y"] else "no",
                   r2(s["cost_eur"]), r2(s["proceeds_eur"]), r2(s["gain_eur"]), r2(s["cost_usd"]), r2(s["proceeds_usd"]),
                   r2(s["gain_usd"]), s["chain"], _link(s["chain"], s["sell_tx"]), _link(s["chain"], s["buy_tx"]) if s["buy_tx"] else "",
                   s["note"], s.get("buy_price_usd"), r2(_mc(s.get("key"), s.get("buy_price_usd"))),
                   s.get("sale_price_usd"), r2(_mc(s.get("key"), s.get("sale_price_usd"))),
                   (s.get("wallet") or "")[:6] + "…" + (s.get("wallet") or "")[-4:] if s.get("wallet") else ""])

    ws = wb.create_sheet("Transactions")
    blatt(ws, ["Date (UTC)", "Chain", "Wallet", "Type", "Out", "In", "Value EUR", "Value USD", "Fee EUR", "Fee USD", "Tx", "Note",
               "Coin", "Coin price USD", "Market cap USD"],
          [17, 11, 14, 22, 30, 30, 12, 12, 10, 10, 30, 50, 12, 16, 18])
    for z in zeilen:
        if jahr and z["ts"] and time.gmtime(z["ts"]).tm_year != jahr:
            continue
        ws.append([datum(z["ts"]), z["chain"], z["wallet"][:6] + "…" + z["wallet"][-4:], z["type"], z["out"], z["in"],
                   r2(z["value_eur"]), r2(z["value_usd"]), r2(z["fee_eur"]), r2(z["fee_usd"]), _link(z["chain"], z["tx"]), z["note"],
                   z.get("coin") or "", z.get("coin_price_usd"), r2(_mc(z.get("coin_key"), z.get("coin_price_usd")))])

    ws = wb.create_sheet("Holdings")
    blatt(ws, ["Asset", "Amount", "Cost basis EUR", "Cost basis USD", "First buy (UTC)", "Token", "Wallet"], [14, 18, 16, 16, 17, 50, 14])
    for b in sorted(bestaende, key=lambda b: -b["cost_eur"]):
        wl = b.get("wallet") or ""
        ws.append([b["asset"], b["amount"], r2(b["cost_eur"]), r2(b["cost_usd"]), datum(b["first_buy"]), b["key"],
                   (wl[:6] + "…" + wl[-4:]) if len(wl) > 12 else ("all" if wl == "*" else wl)])

    ws = wb.create_sheet("Notes")
    ws.column_dimensions["A"].width = 120
    for t in ["How this export works",
              "",
              "Every buy, sell and swap from the wallets above, on Solana, Ethereum, Base, Arbitrum, Optimism, Robinhood Chain, TRON and TON.",
              "A swap of one coin into another counts as selling the first coin (German tax view).",
              ("Cost basis: FIFO, first in first out, for each wallet on its own (German BMF view). When you send coins from "
               "one of your listed wallets to another, the coins keep their original buy date and cost, also over a bridge."
               if PRO_WALLET else "Cost basis: FIFO, first in first out, one pool per coin across all listed wallets."),
              "ETH, SOL, TRX, TON and stablecoins are assets too: paying with them (for example buying a coin with ETH or USDC) is a "
              "disposal of that ETH or USDC, with its own gain or loss in EUR against what it cost you.",
              ("Network fees paid in ETH, SOL, TRX or TON are a disposal of that coin and have their own lines in Sales "
               "(note: network fee). They are also added to the cost of what you bought." if GAS_ALS_VERKAUF else ""),
              "If ETH, SOL, TRX, TON or stablecoins came from a wallet of yours that is NOT in this export, their buy date and cost are "
              "unknown here: they count from the day they arrived. Add that wallet and export again.",
              "Values: ETH and SOL from hourly Coinbase prices at the time of the transaction, TRX and TON from hourly exchange "
              "prices (daily for older trades when no hourly price is available), stablecoins at 1 USD.",
              "EUR for stablecoins, TRX and TON: ETH-EUR divided by ETH-USD in the same hour.",
              "" if GAS_ALS_VERKAUF else "Gas and network fees are added to the cost of what you bought. Fees on sales lower nothing here, they are listed per year.",
              "Coins sent between your own listed wallets, and bridges between your own chains, are not sales.",
              "Coins sent to someone else are not counted as a sale. If it was a payment, check it.",
              "Coins received without paying (airdrops, payments) get their value at arrival if it is known, otherwise cost 0.",
              "Token for token swaps (for example a coin paired with TORCH) get their value from the hourly USD price of the coin "
              "on its most liquid pool (GeckoTerminal).",
              f"Swaps where no price was found at all have no known value here: {ohne_wert}.",
              "Market cap columns: the coin price at that time multiplied by the total supply today (approximate for coins "
              "whose supply changed through burns or mints).",
              "Fake tokens (address poisoning) are left out.",
              ("Not complete: " + ", ".join(unvollstaendig) + f". Either more than {MAX_TX} transactions or the explorer was not reachable.") if unvollstaendig else "",
              "",
              "This file is generated from public chain data and can contain mistakes. It is not tax advice."]:
        ws.append([t])
    ws["A1"].font = Font(bold=True, size=13)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Ablauf ───────────────────────────────────────────────────────────────

def wallets_von(adr):
    import motor
    p = motor.zustand(adr)["prefs"]
    evm = [w.lower() for w in motor._wallets(adr) if str(w).startswith("0x")] + \
          [str(w).lower() for w in (p.get("guard_wallets") or []) if str(w).startswith("0x")]
    sol = [w for w in [p.get("sol_wallet")] + [w for w in (p.get("guard_wallets") or []) if not str(w).startswith("0x")] if w]
    return list(dict.fromkeys(evm)), list(dict.fromkeys(sol))


def sammeln(evm, sol, tron=(), ton=()):
    """Alle Bewegungen der Wallets -> (ereignisse, unvollstaendig). Seit 2.62.9 auch fuer den Sunday Report."""
    import intel
    ereignisse, unvollst = [], []
    for w in evm:
        # Seit 2.63.1: aktive Chains direkt von der Chain (Nonce, Guthaben, Token-Eingang). Vorher entschieden die
        # Blockscout-Zaehler, und die stehen bei neuen Wallets oft noch auf 0: die Chain fiel dann still aus dem Export.
        try:
            import verfolger
            aktiv = {k["chain"] for k in verfolger.aktive_ketten(w)}
        except Exception as ex:
            log.warning(f"steuer: chain check failed for {w}: {ex}")
            aktiv = set(KETTEN)
        for ch in sorted(aktiv - set(KETTEN)):
            unvollst.append(f"{w[:6]}…{w[-4:]} on {ch} (activity found, this chain is not in the tax export yet)")
        for ch in KETTEN:
            if ch not in aktiv:
                continue
            e, voll = evm_ereignisse(ch, w)
            ereignisse += e
            if not voll:
                unvollst.append(f"{w[:6]}…{w[-4:]} on {ch}")
    for w in sol:
        e, voll = sol_ereignisse(w)
        ereignisse += e
        if not voll:
            unvollst.append(f"{w[:4]}…{w[-4:]} on solana")
    if tron or ton:
        import steuer_tt
        for w in tron:
            try:
                e, voll = steuer_tt.tron_ereignisse(w)
                ereignisse += e
                if not voll:
                    unvollst.append(f"{w[:4]}…{w[-4:]} on tron")
            except Exception as ex:
                log.warning(f"steuer: tron {w[:8]}: {ex}")
                unvollst.append(f"{w[:4]}…{w[-4:]} on tron (history not readable right now)")
        for w in ton:
            try:
                e, voll = steuer_tt.ton_ereignisse(w)
                ereignisse += e
                if not voll:
                    unvollst.append(f"{w[:4]}…{w[-4:]} on ton")
            except Exception as ex:
                log.warning(f"steuer: ton {w[:8]}: {ex}")
                unvollst.append(f"{w[:4]}…{w[-4:]} on ton (history not readable right now)")
    for ch in sorted(_NICHT_LESBAR):
        unvollst.append(f"{ch} (explorer not reachable, history missing)")
    _NICHT_LESBAR.clear()
    return ereignisse, unvollst


def bauen(evm, sol, jahr=None, tron=(), ton=()):
    import tronton
    zeige = list(evm) + list(sol) + list(tron) + list(ton)
    ton = [tronton.roh(w) for w in ton]
    alle = list(evm) + list(sol) + list(tron) + list(ton)
    ereignisse, unvollst = sammeln(evm, sol, tron, ton)
    sales, zeilen, bestaende, geb, ohne = rechnen(ereignisse, alle)
    datei = excel(zeige, sales, zeilen, bestaende, geb, ohne, jahr, unvollst)
    s_j = [s for s in sales if not jahr or time.gmtime(s["sold"]).tm_year == jahr]
    bek = [s for s in s_j if s["gain_eur"] is not None]
    andere = herkunft(zeilen, alle)
    for x in andere:
        if x["chain"] == "ton" and ":" in str(x["wallet"]):
            import steuer_tt
            x["wallet"] = steuer_tt.freundlich(x["wallet"])
    zusammen = {"from_others": andere,
                "sales": len(s_j), "gain_eur": round(sum(s["gain_eur"] for s in bek), 2),
                "short_term_gain_eur": round(sum(s["gain_eur"] for s in bek if not s["over_1y"]), 2),
                "without_value": len(s_j) - len(bek), "transactions": len(zeilen), "cut_off": unvollst}
    return datei, zusammen


def _teilen(text):
    """-> (evm, sol, tron, ton). Seit 2.82.49 auch TRON (T...) und TON (EQ/UQ...)."""
    import tronton
    teile = [w.strip() for w in str(text or "").replace(";", ",").replace(" ", ",").split(",") if w.strip()]
    tron = [w for w in teile if tronton.ist_tron(w)]
    ton = [w for w in teile if tronton.ist_ton(w)]
    return ([w.lower() for w in teile if w.startswith("0x") and len(w) == 42],
            [w for w in teile if not w.startswith("0x") and 32 <= len(w) <= 44 and w.isalnum() and w not in tron],
            tron, ton)


def starten(adr, wallets=None, jahr=None, add_wallets=None):
    """Aus motor: laeuft im Hintergrund, schickt die Excel-Datei in Telegram.
    Seit 2.63: add_wallets kommt zu den eigenen Wallets dazu und wird fuer die naechsten Exporte gemerkt."""
    if _LAEUFT.get(adr) and time.time() - _LAEUFT[adr] < 1800:
        return {"started": False, "note": "A tax export for you is already running. It arrives here when it is done."}
    import motor
    evm, sol = wallets_von(adr)
    try:
        import portfolio
        tron, ton = (list(x) for x in portfolio.eigene_mehr(adr))
    except Exception:
        tron, ton = [], []
    z = motor.zustand(adr)
    gemerkt = list(z["prefs"].get("tax_wallets") or [])
    if add_wallets:
        e2, s2, t2, n2 = _teilen(add_wallets)
        gemerkt = list(dict.fromkeys(gemerkt + e2 + s2 + t2 + n2))[:12]
        z["prefs"]["tax_wallets"] = gemerkt
        motor._zustand_speichern(adr, z)
    e3, s3, t3, n3 = _teilen(",".join(gemerkt))
    evm, sol = list(dict.fromkeys(evm + e3)), list(dict.fromkeys(sol + s3))
    tron, ton = list(dict.fromkeys(tron + t3)), list(dict.fromkeys(ton + n3))
    if wallets:
        e1, s1, t1, n1 = _teilen(wallets)
        if e1 or s1 or t1 or n1:
            evm, sol, tron, ton = e1, s1, t1, n1
    if not evm and not sol and not tron and not ton:
        return {"started": False, "error": "No wallet to export. Send me the wallet addresses."}
    try:
        jahr = int(jahr) if jahr else None
    except Exception:
        jahr = None
    u = motor._tg_von(adr)
    if not u or not u.get("tg"):
        return {"started": False, "error": "The export comes as an Excel file in Telegram. Connect Telegram first."}
    _LAEUFT[adr] = time.time()

    def lauf():
        t0 = time.time()
        try:
            datei, z = bauen(evm, sol, jahr, tron, ton)
            name = f"tax-export-{jahr or 'all-years'}.xlsx"
            text = (f"Your tax export{' for ' + str(jahr) if jahr else ''} is ready.\n\n"
                    f"Sales: {z['sales']}\nGain or loss: {z['gain_eur']:,.2f} EUR\n"
                    f"Of that, held up to 1 year: {z['short_term_gain_eur']:,.2f} EUR\n"
                    + (f"Swaps without a known value: {z['without_value']} (marked in the file)\n" if z["without_value"] else "")
                    + (f"Not complete: {', '.join(z['cut_off'])}. Try again later.\n" if z["cut_off"] else "")
                    + ("\nIs one of these wallets yours? They sent you ETH, SOL, TRX, TON or stablecoins:\n"
                       + "\n".join(f"{x['wallet']} ({x['chain']}, about ${x['usd']:,.0f})" for x in z["from_others"][:4])
                       + "\nIf yes, say \"add <address> to my tax export\". Then buy dates and cost carry over.\n"
                       if z.get("from_others") else "")
                    + ("\nFIFO per wallet" if PRO_WALLET else "\nFIFO") + ", values at the time of each trade. Not tax advice, check it with your tax advisor.")
            datei_senden(u["tg"], name, datei, text)
            log.info(f"steuer {adr[:8]}: {z['transactions']} tx, {z['sales']} sales, {time.time() - t0:.0f}s")
        except Exception as e:
            log.warning(f"steuer {adr[:8]}: {e}")
            try:
                motor.senden(adr, "The tax export did not finish. Please try again in a few minutes.")
            except Exception:
                pass
        finally:
            _LAEUFT.pop(adr, None)
    threading.Thread(target=lauf, daemon=True).start()
    return {"started": True, "wallets": evm + sol + tron + ton, "year": jahr,
            "note": "Building the export now. Big wallets take a few minutes. The Excel file arrives here in Telegram."}


def datei_senden(chat_id, name, daten, text):
    from config import cfg
    r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendDocument", data={"chat_id": chat_id, "caption": text[:1000]},
                      files={"document": (name, daten, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                      timeout=120)
    return bool(r.ok)
