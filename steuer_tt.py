"""
steuer_tt.py: TRON und TON fuer den Steuer-Export (seit 2.82.49).

  kurs_usd(asset, ts)     USD je TRX oder TON zur Stunde von ts. OKX (stuendlich, USDT), sonst Kraken
                          (stuendlich fuer die letzten 30 Tage, sonst Tageskerze).
  tron_ereignisse(w)      Bewegungen einer TRON-Wallet im Format von steuer.py (TronGrid: Transaktionen,
                          interne Transaktionen, TRC20-Transfers, je Transaktion gebuendelt).
  ton_ereignisse(w)       Bewegungen einer TON-Wallet (tonapi Events: TON-Transfers, Jetton-Transfers, Swaps,
                          ein Event je Ablauf, damit ein Swap ueber mehrere Nachrichten eine Zeile bleibt).
  test()                  fuer den Selbsttest.

Ein Ereignis: {"chain", "tx", "ts", "wallet", "rein": [(key, sym, klasse, menge, gegen)], "raus": [...],
               "fee", "fee_asset"}. klasse: "TRX", "TON", "USD" oder None (Kurs dann ueber GeckoTerminal).
"""
import os
import time

import requests

from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
MAX_TX = int(os.getenv("TAX_MAX_TX", "5000"))
USDT_TON = "0:b113a994b5024a16719f69139328eb759596c38a25f59028b146fecdc3621dfe"
STABLE_TON = {USDT_TON: "USDT"}
_K = {}
BLOCK = 100 * 3600


# ── Kurse TRX und TON ────────────────────────────────────────────────────

def _okx(asset, ts):
    b = int(ts // BLOCK)
    k = ("okx", asset, b)
    if k not in _K:
        d = {}
        try:
            r = requests.get("https://www.okx.com/api/v5/market/history-candles",
                             params={"instId": f"{asset}-USDT", "bar": "1H", "limit": 100,
                                     "after": str((b + 1) * BLOCK * 1000)}, headers=UA, timeout=15)
            for x in (r.json() or {}).get("data") or []:
                d[int(x[0]) // 3600000] = float(x[4])
        except Exception as e:
            log.debug(f"steuer_tt okx {asset}: {e}")
        if d or b < int(time.time() // BLOCK):
            _K[k] = d
        else:
            return {}
    return _K[k]


def _kraken(asset, ts):
    nah = time.time() - ts < 29 * 86400
    k = ("kraken", asset, nah, int(time.time() // 3600) if nah else int(time.time() // 86400))
    if k not in _K:
        d = {}
        try:
            r = requests.get("https://api.kraken.com/0/public/OHLC",
                             params={"pair": f"{asset}USD", "interval": 60 if nah else 1440}, headers=UA, timeout=15)
            res = (r.json() or {}).get("result") or {}
            for name, liste in res.items():
                if name == "last" or not isinstance(liste, list):
                    continue
                for x in liste:
                    t = int(x[0])
                    if nah:
                        d[t // 3600] = float(x[4])
                    else:
                        for h in range(24):
                            d[t // 3600 + h] = float(x[4])
        except Exception as e:
            log.debug(f"steuer_tt kraken {asset}: {e}")
        _K[k] = d
    return _K[k]


def kurs_usd(asset, ts):
    """USD je TRX oder TON zur Stunde von ts, oder None."""
    if asset not in ("TRX", "TON") or not ts:
        return None
    h = int(ts // 3600)
    for quelle in (_okx, _kraken):
        d = quelle(asset, ts)
        for dh in (0, -1, 1, -2, 2, -3, 3, -6, 6, -12, 12, -24, 24):
            if h + dh in d:
                return d[h + dh]
    return None


def freundlich(a):
    """TON 0:hex -> freundliche Form (EQ..., bounceable), wie DexScreener und GeckoTerminal sie nutzen."""
    import base64
    a = str(a or "")
    if ":" not in a:
        return a
    wc, h = a.split(":", 1)
    roh = bytes([0x11, int(wc) & 0xFF]) + bytes.fromhex(h)
    crc = 0
    for byte in roh:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return base64.urlsafe_b64encode(roh + crc.to_bytes(2, "big")).decode()


# ── TRON ─────────────────────────────────────────────────────────────────

def _tg_seiten(pfad, params):
    import tronton
    out, fp = [], None
    while len(out) < MAX_TX:
        p = dict(params, limit=200, order_by="block_timestamp,asc")
        if fp:
            p["fingerprint"] = fp
        d = tronton._tg(pfad, p)
        liste = d.get("data") or []
        out += liste
        fp = (d.get("meta") or {}).get("fingerprint")
        if not fp or not liste:
            break
    return out, len(out) < MAX_TX


def tron_ereignisse(w):
    """-> (ereignisse, vollstaendig)"""
    import tronton
    ev = {}

    def e(h, ms):
        return ev.setdefault(h, {"chain": "tron", "tx": h, "ts": int((ms or 0) // 1000), "wallet": w, "rein": [], "raus": [],
                                 "fee": 0.0, "fee_asset": "TRX"})
    txs, voll1 = _tg_seiten(f"/v1/accounts/{w}/transactions", {"only_confirmed": "true"})
    for x in txs:
        if x.get("internal_tx_id"):
            # interne Transaktion: TRX aus einem Vertrag (z. B. Erloes eines Verkaufs auf SunSwap)
            h = x.get("tx_id")
            data = x.get("data") or {}
            if not h or data.get("rejected"):
                continue
            v = int(((data.get("call_value") or {}).get("_")) or 0)
            von, an = tronton.b58(x.get("from_address")), tronton.b58(x.get("to_address"))
            if v and an == w:
                e(h, x.get("block_timestamp"))["rein"].append(("TRX", "TRX", "TRX", v / 1e6, von))
            elif v and von == w:
                e(h, x.get("block_timestamp"))["raus"].append(("TRX", "TRX", "TRX", v / 1e6, an))
            continue
        h = x.get("txID")
        if not h:
            continue
        ret = (x.get("ret") or [{}])[0] or {}
        c = (((x.get("raw_data") or {}).get("contract") or [{}])[0]) or {}
        v = ((c.get("parameter") or {}).get("value")) or {}
        von = tronton.b58(v.get("owner_address"))
        x_e = e(h, x.get("block_timestamp"))
        if von == w:
            gebuehr = ret.get("fee")
            if gebuehr is None:
                gebuehr = int(x.get("energy_fee") or 0) + int(x.get("net_fee") or 0)
            x_e["fee"] += int(gebuehr or 0) / 1e6
        if (ret.get("contractRet") or "SUCCESS") != "SUCCESS":
            continue
        typ = c.get("type")
        if typ == "TransferContract":
            an, menge = tronton.b58(v.get("to_address")), int(v.get("amount") or 0) / 1e6
            if menge and von == w:
                x_e["raus"].append(("TRX", "TRX", "TRX", menge, an))
            elif menge and an == w:
                x_e["rein"].append(("TRX", "TRX", "TRX", menge, von))
        elif typ == "TriggerSmartContract" and von == w and int(v.get("call_value") or 0):
            x_e["raus"].append(("TRX", "TRX", "TRX", int(v["call_value"]) / 1e6, tronton.b58(v.get("contract_address"))))
    toks, voll2 = _tg_seiten(f"/v1/accounts/{w}/transactions/trc20", {"only_confirmed": "true"})
    for x in toks:
        ti = x.get("token_info") or {}
        t = ti.get("address")
        try:
            menge = int(x.get("value") or 0) / 10 ** int(ti.get("decimals") or 0)
        except Exception:
            continue
        if not t or not menge or not x.get("transaction_id"):
            continue
        sym = ti.get("symbol") or t[:6]
        stab = tronton.STABLE_TRON.get(t)
        posten = (f"tron:{t}", stab[0] if stab else sym, "USD" if stab else None, menge)
        von, an = x.get("from"), x.get("to")
        if von == w:
            e(x["transaction_id"], x.get("block_timestamp"))["raus"].append(posten + (an,))
        elif an == w:
            if not stab and str(sym).upper() in ("USDT", "USDC", "USDD", "TRX"):
                continue  # gefaelschter Stablecoin (Adress-Vergiftung)
            e(x["transaction_id"], x.get("block_timestamp"))["rein"].append(posten + (von,))
    out = [x for x in ev.values() if x["rein"] or x["raus"] or x["fee"]]
    return out, voll1 and voll2


# ── TON ──────────────────────────────────────────────────────────────────

def _jetton(j):
    """(key, sym, klasse, dezimalen) aus einem tonapi Jetton-Objekt, None bei gesperrten (Spam) Jettons."""
    import tronton
    j = j or {}
    if j.get("verification") == "blacklist":
        return None
    a = tronton.roh(j.get("address"))
    sym = j.get("symbol") or a[2:10]
    if a in STABLE_TON:
        return f"ton:{a}", STABLE_TON[a], "USD", int(j.get("decimals") or 6)
    if str(sym).upper() in ("USDT", "USD₮", "USDC", "TON"):
        return None  # gefaelschter Stablecoin oder TON-Kopie
    return f"ton:{a}", sym, None, int(j.get("decimals") if j.get("decimals") is not None else 9)


def ton_ereignisse(w):
    """-> (ereignisse, vollstaendig). w in beliebiger Form, intern als 0:hex."""
    import tronton
    ich = tronton.roh(w)
    out, vor, n = [], None, 0
    voll = True
    while True:
        p = {"limit": 100}
        if vor:
            p["before_lt"] = vor
        d = tronton._ta(f"/v2/accounts/{ich}/events", p)
        evs = d.get("events") or []
        for ev in evs:
            n += 1
            if ev.get("in_progress"):
                continue
            x = {"chain": "ton", "tx": ev.get("event_id"), "ts": int(ev.get("timestamp") or 0), "wallet": ich,
                 "rein": [], "raus": [], "fee": 0.0, "fee_asset": "TON"}
            extra = int(ev.get("extra") or 0)
            if extra < 0:
                x["fee"] = -extra / 1e9
            akt = [a for a in ev.get("actions") or [] if a.get("status") == "ok"]
            swap = any(a.get("type") == "JettonSwap" and tronton.roh(((a.get("JettonSwap") or {}).get("user_wallet") or {})
                                                                       .get("address")) == ich for a in akt)
            for a in akt:
                typ = a.get("type")
                if swap and typ in ("TonTransfer", "JettonTransfer"):
                    continue  # der Swap traegt schon beide Seiten
                inh = a.get(typ) or {}
                if typ == "TonTransfer":
                    von = tronton.roh((inh.get("sender") or {}).get("address"))
                    an = tronton.roh((inh.get("recipient") or {}).get("address"))
                    m = int(inh.get("amount") or 0) / 1e9
                    if m and von == ich:
                        x["raus"].append(("TON", "TON", "TON", m, an))
                    elif m and an == ich:
                        x["rein"].append(("TON", "TON", "TON", m, von))
                elif typ == "JettonTransfer":
                    j = _jetton(inh.get("jetton"))
                    if not j:
                        continue
                    von = tronton.roh((inh.get("sender") or {}).get("address"))
                    an = tronton.roh((inh.get("recipient") or {}).get("address"))
                    m = int(inh.get("amount") or 0) / 10 ** j[3]
                    if m and von == ich:
                        x["raus"].append(j[:3] + (m, an))
                    elif m and an == ich:
                        x["rein"].append(j[:3] + (m, von))
                elif typ == "JettonSwap":
                    if tronton.roh((inh.get("user_wallet") or {}).get("address")) != ich:
                        continue
                    dex = inh.get("dex") or "dex"
                    if int(inh.get("ton_in") or 0):
                        x["raus"].append(("TON", "TON", "TON", int(inh["ton_in"]) / 1e9, dex))
                    elif inh.get("jetton_master_in"):
                        j = _jetton(inh["jetton_master_in"])
                        if j:
                            x["raus"].append(j[:3] + (int(inh.get("amount_in") or 0) / 10 ** j[3], dex))
                    if int(inh.get("ton_out") or 0):
                        x["rein"].append(("TON", "TON", "TON", int(inh["ton_out"]) / 1e9, dex))
                    elif inh.get("jetton_master_out"):
                        j = _jetton(inh["jetton_master_out"])
                        if j:
                            x["rein"].append(j[:3] + (int(inh.get("amount_out") or 0) / 10 ** j[3], dex))
            if x["rein"] or x["raus"] or x["fee"]:
                out.append(x)
        vor = d.get("next_from")
        if not evs or not vor:
            break
        if n >= MAX_TX:
            voll = False
            break
    return out, voll


def test():
    """Selbsttest: Kurse vor 40 und vor 400 Tagen, je eine Seite Verlauf auf TRON und TON."""
    import tronton
    jetzt = time.time()
    teile = []
    for a in ("TRX", "TON"):
        k1, k2 = kurs_usd(a, jetzt - 40 * 86400), kurs_usd(a, jetzt - 400 * 86400)
        if not k1 or not k2:
            raise RuntimeError(f"no {a} price history")
        teile.append(f"{a} ${k1:.3f} / ${k2:.3f}")
    d = tronton._tg(f"/v1/accounts/{tronton.USDT_TRON}/transactions/trc20", {"limit": 3, "only_confirmed": "true"})
    if not isinstance(d.get("data"), list):
        raise RuntimeError("TronGrid history not readable")
    t = tronton._ta(f"/v2/accounts/{USDT_TON}/events", {"limit": 3})
    if not isinstance(t.get("events"), list):
        raise RuntimeError("tonapi events not readable")
    return ", ".join(teile) + ", TRON and TON history readable"
