"""
beutel.py: Bag-Wache von Your Engine, seit 2.48. Teil von Wallet Guard (Schalter 'guard', Nutzer-Einstellung 'guard').

Jeder Scanner schaut einmal, vor dem Kauf. Gerugged wird danach. Die Bag-Wache bewacht jeden Coin, den der Nutzer
haelt (Robinhood ueber seine verknuepften Wallets, Solana ueber seine Solana-Wallet) plus Coins, die er ausdruecklich
dazunimmt, und meldet sofort in Telegram:
  Dev verkauft (wie viel vom eigenen Bestand, wie viel vom Angebot, was er noch haelt)
  Top-Holder oder eine Wallet-Gruppe mit gleichem Geldgeber lädt ab
  Liquiditaet wird abgezogen
  Preissturz in kurzer Zeit
  Lock laeuft bald aus oder ist ausgelaufen
Kosten: je Coin (nicht je Nutzer) alle BAG_POLL_SEC ein Saldo-Abruf fuer Dev, Top-Holder und Gruppe, keine KI.

  runde()               aus motor (Hintergrund, alle 60 s)
  dazu(adr, token)      einen Coin zusaetzlich bewachen
  weg(adr, token)       nicht mehr bewachen
  liste(adr)            was gerade bewacht wird
"""
import json
import os
import time

import storage
from logger import log

TAKT = int(os.getenv("BAG_POLL_SEC", "120"))
DEV_MIN = float(os.getenv("BAG_DEV_MIN_PCT", "0.01"))     # jede Dev-Bewegung ab 0.01 % Supply (seit 2.50.12)
FWD_MIN = float(os.getenv("BAG_FWD_MIN_PCT", "0.05"))     # Empfaenger: Bewegungen ab 0.05 % Supply
FWD_TIEFE = int(os.getenv("BAG_FWD_DEPTH", "3"))          # Weiterleitungsketten bis 3 Stufen verfolgen
FWD_MAX = int(os.getenv("BAG_FWD_MAX", "15"))             # hoechstens so viele Empfaenger je Coin
MAX_JE_NUTZER = int(os.getenv("BAG_MAX_COINS", "12"))
MIN_USD = float(os.getenv("BAG_MIN_USD", "5"))
_HALT = {}


def _an():
    try:
        import features
        return features.an("guard")
    except Exception:
        return False


def _kurz(a):
    a = str(a)
    return a[:4] + "…" + a[-4:]


def _usd(v):
    try:
        import motor
        return motor._usd_kurz(v)
    except Exception:
        return f"${v:,.0f}" if v else "n/a"


# ── Was haelt der Nutzer ─────────────────────────────────────────────────

def _haelt_robinhood(wallets):
    import onchain
    out = []
    for w in wallets[:4]:
        try:
            h = onchain.holdings(w)
        except Exception:
            continue
        for t in h.get("tokens") or []:
            if (t.get("usd") or 0) >= MIN_USD and t.get("token"):
                out.append(("robinhood", t["token"].lower(), t.get("symbol") or _kurz(t["token"]), float(t.get("usd") or 0),
                            float(t.get("amount") or 0)))
    return out


def _haelt_solana(wallet):
    import holder
    import requests
    mints = {}
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        try:
            r = holder._sol_rpc("getTokenAccountsByOwner", [wallet, {"programId": prog}, {"encoding": "jsonParsed"}])
        except Exception:
            continue
        for x in (r or {}).get("value") or []:
            try:
                info = x["account"]["data"]["parsed"]["info"]
                menge = float(info["tokenAmount"].get("uiAmount") or 0)
            except Exception:
                continue
            if menge > 0:
                mints[info["mint"]] = mints.get(info["mint"], 0) + menge
    stabil = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
              "So11111111111111111111111111111111111111112"}
    liste = [m for m in mints if m not in stabil][:60]
    out = []
    for i in range(0, len(liste), 30):
        try:
            d = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(liste[i:i + 30]), timeout=15).json()
        except Exception:
            continue
        preis, sym = {}, {}
        for p in d if isinstance(d, list) else []:
            a = (p.get("baseToken") or {}).get("address")
            if a and p.get("priceUsd") and a not in preis:
                preis[a] = float(p["priceUsd"])
                sym[a] = (p.get("baseToken") or {}).get("symbol")
        for m in liste[i:i + 30]:
            usd = mints[m] * preis.get(m, 0)
            if usd >= MIN_USD:
                out.append(("solana", m, sym.get(m) or _kurz(m), usd, mints[m]))
    return out


def _coins(adr):
    """[(chain, token, symbol)] fuer einen Nutzer: gehaltene Coins (30 min gemerkt) plus ausdruecklich dazugenommene."""
    import motor
    k = "bag_user_" + adr
    try:
        c = json.loads(storage.kv_get(k) or "null")
    except Exception:
        c = None
    if not c or time.time() - c.get("ts", 0) > 1800 or "held" not in c:
        p = motor.zustand(adr)["prefs"]
        evm = [w for w in motor._wallets(adr) if str(w).startswith("0x")] + \
              [w for w in (p.get("guard_wallets") or []) if str(w).startswith("0x")]
        sol = [p["sol_wallet"]] if p.get("sol_wallet") else []
        sol += [w for w in (p.get("guard_wallets") or []) if not str(w).startswith("0x")]
        gehalten = _haelt_robinhood(list(dict.fromkeys(evm)))
        for s in list(dict.fromkeys(sol))[:2]:
            gehalten += _haelt_solana(s)
        gehalten.sort(key=lambda x: -x[3])
        # Mengen werden seit 2.50 mitgemerkt (Exit Coach); dieselbe Wallet doppelt verknuepft zaehlt einmal
        menge = {}
        for a, b, s, u, m in gehalten:
            x = menge.setdefault((a, b), [a, b, s, 0.0, 0.0])
            x[3] += u
            x[4] += m
        c = {"ts": time.time(), "coins": [[a, b, s] for a, b, s, _, _ in gehalten],
             "held": sorted(menge.values(), key=lambda x: -x[3])}
        try:
            storage.kv_set(k, json.dumps(c))
        except Exception:
            pass
    extra = motor.zustand(adr)["prefs"].get("bag_extra") or []
    alle, gesehen = [], set()
    for ch, t, s in [tuple(x) for x in extra] + [tuple(x) for x in c.get("coins") or []]:
        if (ch, t) not in gesehen and (ch, t) not in {tuple(x) for x in (motor.zustand(adr)["prefs"].get("bag_off") or [])}:
            gesehen.add((ch, t))
            alle.append((ch, t, s))
    return alle[:MAX_JE_NUTZER]


GRUPPE_STUNDEN = float(os.getenv("GROUP_WATCH_HOURS", "24"))


def gruppe_dazu(cid, thema, chain, token, symbol, nachricht=None):
    """Seit 2.51: ein in der Gruppe gescannter Coin wird GRUPPE_STUNDEN lang fuer die Gruppe bewacht."""
    try:
        liste = json.loads(storage.kv_get("bag_grp") or "[]")
    except Exception:
        liste = []
    jetzt = time.time()
    liste = [x for x in liste if x.get("bis", 0) > jetzt and not (x["cid"] == cid and x["token"] == token)]
    if sum(1 for x in liste if x["cid"] == cid) >= int(os.getenv("GROUP_WATCH_MAX", "20")):
        liste = [x for x in liste if x["cid"] != cid] + sorted([x for x in liste if x["cid"] == cid], key=lambda x: x["bis"])[1:]
    liste.append({"cid": cid, "thema": thema, "chain": chain, "token": token, "sym": symbol, "msg": nachricht,
                  "bis": jetzt + GRUPPE_STUNDEN * 3600})
    storage.kv_set("bag_grp", json.dumps(liste[-500:]))


def _gruppen():
    try:
        jetzt = time.time()
        return [x for x in json.loads(storage.kv_get("bag_grp") or "[]") if x.get("bis", 0) > jetzt]
    except Exception:
        return []


def gehalten(adr):
    """[(chain, token, symbol, usd, amount)] der gehaltenen Coins (nicht der nur dazugenommenen), aus dem Speicher."""
    try:
        c = json.loads(storage.kv_get("bag_user_" + adr) or "null") or {}
    except Exception:
        c = {}
    return [tuple(x) for x in c.get("held") or []][:MAX_JE_NUTZER]


def dazu(adr, token):
    import motor
    import scan
    r = scan.analyse(token, deep=False)
    if not r.get("ok"):
        return r
    z = motor.zustand(adr)
    p = z["prefs"]
    eintrag = [r["chain"], r["address"] if r["chain"] in ("solana", "tron", "ton") else r["address"].lower(), r.get("symbol") or _kurz(r["address"])]
    p["bag_extra"] = [x for x in (p.get("bag_extra") or []) if x[1] != eintrag[1]][-9:] + [eintrag]
    p["bag_off"] = [x for x in (p.get("bag_off") or []) if x[1] != eintrag[1]]
    motor._zustand_speichern(adr, z)
    return {"ok": True, "watching": f"${eintrag[2]}", "chain": r.get("chain_name"),
            "alerts": ("price crash and liquidity pulls. Dev and holder alerts are not available on this chain yet, say that"
                       if r["chain"] in ("tron", "ton") else
                       "dev sells, top holder or wallet group dumps, liquidity pulls, price crash, lock ending"),
            "guard_on": p.get("guard") is True}


def weg(adr, token):
    import motor
    t = str(token or "").strip().lstrip("$").lower()
    z = motor.zustand(adr)
    p = z["prefs"]
    treffer = [x for x in _coins(adr) if x[1].lower() == t or str(x[2]).lower() == t]
    if not treffer:
        return {"ok": False, "error": "That coin is not on your bag watch."}
    ch, tok, sym = treffer[0]
    p["bag_extra"] = [x for x in (p.get("bag_extra") or []) if x[1] != tok]
    p["bag_off"] = (p.get("bag_off") or []) + [[ch, tok]]
    motor._zustand_speichern(adr, z)
    return {"ok": True, "stopped": f"${sym}"}


def liste(adr):
    return [{"chain": ch, "token": t, "symbol": f"${s}"} for ch, t, s in _coins(adr)]


# ── Zustand je Coin (fuer alle Nutzer geteilt) ──────────────────────────

def _info(chain, token):
    """Dev, Top-Holder, eine Wallet-Gruppe, Lock-Datum. 6 h gemerkt, aus dem vorhandenen Report."""
    k = f"bag_tok_{chain}_{token}"
    try:
        s = json.loads(storage.kv_get(k) or "null")
    except Exception:
        s = None
    if s and time.time() - s.get("info_ts", 0) < 6 * 3600:
        return s
    import scan
    r = scan.analyse(f"{chain}:{token}", deep=False)
    if not r.get("ok"):
        return s
    dev = (r.get("dev") or {}).get("address")
    tops = [x["address"] for x in (r.get("supply") or {}).get("top") or []
            if x.get("address") and not x.get("pool") and not x.get("locked") and not x.get("account")
            and str(x["address"]).lower() != str(dev or "").lower()][:3]
    gruppe = []
    for c in ((r.get("map") or {}).get("clusters") or []):
        if c.get("kind") == "same funder" and (c.get("supply_pct") or 0) >= 1:
            gruppe = [m for m in (c.get("members") or []) if m not in tops and m != dev][:6]
            break
    # Seit 2.50.8: Empfaenger von Weiterleitungen (Dev und fruehe Kaeufer) mitbeobachten: verkaufen, locken, weiterschicken
    fwd = {}
    for p in ((r.get("forwards") or {}).get("list") or []):
        if (p.get("pct") or 0) >= 0.5 and p.get("to") and p["to"] not in tops and p["to"] != dev:
            fwd.setdefault(p["to"], {"to": p["to"], "src": p.get("src") or "holder", "pct": 0})["pct"] += p.get("pct") or 0
    for x in (((r.get("dev") or {}).get("out") or {}).get("to") or []):
        if (x.get("pct") or 0) >= 0.5 and x.get("to"):
            fwd.setdefault(x["to"], {"to": x["to"], "src": "dev", "pct": x.get("pct") or 0})
    neu = s or {}
    alt_dyn = [x for x in (neu.get("fwd") or []) if x.get("depth") and x["to"] not in fwd]
    neu["fwd"] = (sorted(fwd.values(), key=lambda x: -(x["pct"] or 0))[:6] + alt_dyn)[:FWD_MAX]
    if chain == "solana":
        # Startpunkt merken, damit spaeter nur NEUE Bewegungen der Empfaenger eingeordnet werden
        try:
            import intel
            for x in neu["fwd"] + ([{"to": dev}] if dev else []):
                k = "fwd_sig_" + x["to"]
                if not neu.get(k):
                    sg = intel._sol("getSignaturesForAddress", [x["to"], {"limit": 1}]) or []
                    neu[k] = sg[0]["signature"] if sg else None
        except Exception:
            pass
    neu.update(info_ts=time.time(), sym=r.get("symbol") or _kurz(token), dev=dev, tops=tops, gruppe=gruppe,
               unlock=(r.get("supply") or {}).get("unlock_ts"), locked_pct=(r.get("supply") or {}).get("locked_pct"),
               chart=(r.get("links") or {}).get("page") or (r.get("links") or {}).get("chart"),
               torch=(r.get("links") or {}).get("trade_torch"))
    neu.setdefault("bal", {})
    neu.setdefault("gesendet", {})
    return neu


def _pruefe_coin(chain, token):
    """-> Liste von Alarmzeilen fuer diesen Coin (fuer alle Halter gleich)."""
    import scan
    s = _info(chain, token)
    if not s:
        return [], None
    alarme = []
    jetzt = time.time()
    zn = scan.zahlen(chain, token)
    px, liq = zn.get("price"), zn.get("liq")
    # Liquiditaet
    # Seit 2.62.8: Ein LP-Abzug nimmt BEIDE Seiten aus dem Pool (Coin und ETH). Verkaeufe dagegen bringen Coins in den
    # Pool und nehmen nur ETH heraus, der Dollarwert der Liquiditaet faellt dann auch, das ist aber kein Rug.
    # Vorher reichte ein Fall des Dollarwerts um 40 %, und normale Verkaeufe wurden als "Possible LP pull" gemeldet.
    ref = s.get("liq_ref")
    qb, qq = zn.get("liq_base"), zn.get("liq_quote")
    rb, rq = s.get("liq_ref_b"), s.get("liq_ref_q")
    if liq is not None and ref and ref > 1000 and liq < ref * 0.6 and qb is not None and qq is not None and rb and rq:
        if qq < rq * 0.6 and qb < rb * 0.8:
            alarme.append(f"💧 Liquidity was pulled: the pool lost {100 - qq / rq * 100:.0f}% of its ETH side and "
                          f"{100 - qb / rb * 100:.0f}% of its coin side at once ({_usd(ref)} to {_usd(liq)}). "
                          "That is liquidity removed, not just selling.")
        s["liq_ref"], s["liq_ref_b"], s["liq_ref_q"] = liq, qb, qq
    elif liq and (not ref or liq > ref or not rb):
        s["liq_ref"], s["liq_ref_b"], s["liq_ref_q"] = liq, qb, qq
    # Preissturz: gegen den Hoechstwert der letzten 15 Minuten
    hist = [h for h in s.get("px", []) if jetzt - h[0] < 900]
    if px:
        hoch = max([h[1] for h in hist] or [px])
        if hoch and px < hoch * 0.7 and not _kurz_her(s, "crash", 1800):
            alarme.append(f"📉 Price down {100 - px / hoch * 100:.0f}% in the last 15 minutes.")
        hist.append([jetzt, px])
    s["px"] = hist[-20:]
    # Bestaende von Dev, Top-Holdern und der Gruppe
    fwd_w = [x["to"] for x in s.get("fwd") or []]
    wer = [x for x in [s.get("dev")] + (s.get("tops") or []) + (s.get("gruppe") or []) + fwd_w if x]
    if wer:
        try:
            b = scan.bestaende(chain, token, wer)
        except Exception:
            b = {}
        sup = b.get("_supply") or (s.get("bal") or {}).get("_supply") or 0
        alt = s.get("bal") or {}
        gruppe_neu = sum(b.get(g) or 0 for g in s.get("gruppe") or [] if b.get(g) is not None)
        gruppe_alt = s.get("gruppe_ref") or 0
        for w in wer:
            a, n = alt.get(w), b.get(w)
            if n is None:
                continue
            if a and n < a:
                weg = a - n
                anteil = weg / sup * 100 if sup else None
                if w == s.get("dev") and sup and (anteil or 0) >= DEV_MIN:
                    # seit 2.50.11: verbrannt, gelockt, verkauft oder weitergeschickt, nicht mehr pauschal "sold"
                    alarme.append(abfluss(chain, token, s, w, weg, n, sup, "The dev"))
                    alt[w] = n
                elif w in (s.get("tops") or []) and n < a * 0.5:
                    alarme.append(f"🐋 Top holder {_kurz(w)} dumped {100 - n / a * 100:.0f}% of their bag"
                                  + (f" ({anteil:.2f}% of supply)." if anteil else "."))
                    alt[w] = n
            if sup and a and n < a and w in fwd_w and (a - n) / sup * 100 >= FWD_MIN:
                alarme.append(_fwd_alarm(chain, token, s, w, a - n, n, sup))
                alt[w] = n
            # Seit 2.50.12: jede Bewegung des Devs, auch rein (Kauf oder Zufluss)
            if sup and w == s.get("dev") and a is not None and n > a and (n - a) / sup * 100 >= DEV_MIN:
                alarme.append(zufluss(chain, token, s, w, n - a, n, sup))
            if a is None or n > (a or 0) or w in (s.get("gruppe") or []):
                alt[w] = n
        if s.get("gruppe") and gruppe_alt and gruppe_neu < gruppe_alt * 0.6:
            alarme.append(f"👥 A wallet group with the same funder ({len(s['gruppe'])} wallets) sold "
                          f"{100 - gruppe_neu / gruppe_alt * 100:.0f}% of what it held"
                          + (f" ({(gruppe_alt - gruppe_neu) / sup * 100:.2f}% of supply)." if sup else "."))
            s["gruppe_ref"] = gruppe_neu
        elif s.get("gruppe"):
            s["gruppe_ref"] = max(gruppe_alt, gruppe_neu)
        alt["_supply"] = sup or alt.get("_supply")
        s["bal"] = alt
        for neu_w in s.pop("_neu", []) or []:
            try:
                nb = scan.bestaende(chain, token, [neu_w])
                if nb.get(neu_w) is not None:
                    alt[neu_w] = nb[neu_w]
                if chain == "solana":
                    import intel
                    sg = intel._sol("getSignaturesForAddress", [neu_w, {"limit": 1}]) or []
                    s["fwd_sig_" + neu_w] = sg[0]["signature"] if sg else None
                else:
                    s["blk_" + neu_w.lower()] = int(scan._evm(chain, "eth_blockNumber", []), 16)
            except Exception as e:
                log.debug(f"beutel neuer empfaenger: {e}")
    # Lock
    u = s.get("unlock")
    if u and s.get("locked_pct"):
        if 0 < u - jetzt < 48 * 3600 and not s["gesendet"].get(f"lock48_{u}"):
            s["gesendet"][f"lock48_{u}"] = int(jetzt)
            alarme.append(f"🔓 {s['locked_pct']:.1f}% of supply unlocks in {int((u - jetzt) // 3600)} hours.")
        elif jetzt >= u and not s["gesendet"].get(f"lock0_{u}"):
            s["gesendet"][f"lock0_{u}"] = int(jetzt)
            alarme.append(f"🔓 The lock on {s['locked_pct']:.1f}% of supply has ended. Those tokens can be sold now.")
    storage.kv_set(f"bag_tok_{chain}_{token}", json.dumps(s))
    return alarme, (s, zn)


def _fwd_alarm(chain, token, s, w, weg, rest, sup):
    """Was hat ein Weiterleitungs-Empfaenger mit den Tokens gemacht? Locker, Verkauf oder weitergeschickt."""
    info = next((x for x in s.get("fwd") or [] if x["to"] == w), {})
    wer = {"dev": "the dev", "early buyer": "an early buyer"}.get(info.get("src"), "a holder")
    if info.get("src") == "forward" and info.get("via"):
        wer = f"{_kurz(info['via'])} (a forward chain)"
    return abfluss(chain, token, s, w, weg, rest, sup, f"The wallet {_kurz(w)} that got {info.get('pct', 0):.1f}% from {wer}")


def zufluss(chain, token, s, w, plus, jetzt_hat, sup):
    """Seit 2.61.7: Dev bekam Tokens. Ueber einen Swap = gekauft (mit ETH-Betrag), sonst von welchem Wallet.
    -> eine Meldungszeile"""
    p = lambda v: f"{v / sup * 100:.2f}%"
    roh = (f"📥 The dev just got {p(plus)} of supply more (a buy or a transfer in). Now holds {p(jetzt_hat)}.")
    if chain == "solana":
        return roh
    try:
        import scan
        import spur
        import weiter
        top = int(scan._evm(chain, "eth_blockNumber", []), 16)
        kb = "blkin_" + w.lower()
        von = s.get(kb) or top - weiter.FENSTER.get(chain, 2000) * 2
        rein, _ = weiter._logs_wallet(chain, token, w.lower(), von, top, 2)
        s[kb] = top
        gekauft, nativ, gesehen, von_adr = 0, 0, set(), {}
        for l in rein:
            m = int(l.get("data") or "0x0", 16)
            tx = l.get("transactionHash")
            if weiter.ist_verkauf(chain, tx, w, token):  # Swap-Event im Beleg: hier ein Kauf
                gekauft += m
                if tx and tx not in gesehen:
                    gesehen.add(tx)
                    try:
                        t = scan._evm(chain, "eth_getTransactionByHash", [tx]) or {}
                        if str(t.get("from") or "").lower() == w.lower():
                            nativ += int(t.get("value") or "0x0", 16)
                    except Exception:
                        pass
            else:
                her = "0x" + l["topics"][1][-40:]
                von_adr[her] = von_adr.get(her, 0) + m
        teile = []
        if gekauft:
            teile.append(f"bought {p(gekauft)} of supply" + (f" for {nativ / 1e18:.4g} {spur._nativ(chain)}" if nativ else ""))
        if von_adr:
            her, m = max(von_adr.items(), key=lambda x: x[1])
            teile.append(f"received {p(sum(von_adr.values()))} from " + (_kurz(her) if len(von_adr) == 1 else f"{len(von_adr)} wallets, most from {_kurz(her)}"))
        if not teile:
            return roh
        return f"📥 The dev {' and '.join(teile)}. Now holds {p(jetzt_hat)}."
    except Exception as e:
        log.debug(f"beutel zufluss {w[:8]}: {e}")
        return roh


def abfluss(chain, token, s, w, weg, rest, sup, kopf):
    """Seit 2.50.11 fuer Dev und Weiterleitungs-Empfaenger: wohin sind die Tokens gegangen?
    Verbrannt, gelockt, verkauft, in einen unbekannten Vertrag oder an ein anderes Wallet. -> eine Meldungszeile"""
    p = lambda v: f"{v / sup * 100:.2f}%"
    gesperrt = verkauft = in_vertrag = verbrannt = 0
    weiter_an = {}
    n_tx = set()   # seit 2.63: wie viele Transaktionen seit der letzten Pruefung (vorher hiess alles "just")
    if chain != "solana":
        try:
            import intel
            import scan
            import weiter
            top = int(scan._evm(chain, "eth_blockNumber", []), 16)
            kb = "blk_" + w.lower()
            von = s.get(kb) or top - weiter.FENSTER.get(chain, 2000) * 2
            raus, _ = weiter._logs_wallet(chain, token, w.lower(), von, top, 1)
            s[kb] = top
            for l in raus:
                n_tx.add(l.get("transactionHash"))
                an = "0x" + l["topics"][2][-40:]
                m = int(l.get("data") or "0x0", 16)
                art = weiter.ziel_art(chain, an)
                if art == "lock":
                    gesperrt += m
                elif art == "burn":
                    verbrannt += m
                elif art == "pool":
                    verkauft += m
                elif art == "contract":
                    # Seit 2.60.6: ueber einen Router verkauft? Swap-Event im Beleg oder ein anderer Token kam zurueck
                    if weiter.ist_verkauf(chain, l.get("transactionHash"), w, token):
                        verkauft += m
                    else:
                        in_vertrag += m
                else:
                    weiter_an[an] = weiter_an.get(an, 0) + m
        except Exception as e:
            log.debug(f"beutel fwd {w[:8]}: {e}")
    else:
        # Solana (seit 2.50.9): neue Transaktionen des Empfaengers lesen. Locker-Programm beteiligt = gelockt,
        # SOL oder anderer Token zurueck = verkauft, sonst weitergeschickt an den Besitzer, der die Tokens bekam.
        try:
            import intel
            import spaeher
            k = "fwd_sig_" + w
            opt = {"limit": 15}
            if s.get(k):
                opt["until"] = s[k]
            sigs = [x for x in (intel._sol("getSignaturesForAddress", [w, opt]) or []) if not x.get("err")]
            if sigs:
                s[k] = sigs[0]["signature"]
            txs = intel._sol_batch([intel._tx(x["signature"]) for x in sigs[:10]], 10) if sigs else []
            ui = {"lock": 0.0, "sell": 0.0, "burn": 0.0}
            an_ui = {}
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                e = spaeher.sol_einordnen(tx, w)
                if not e:
                    continue
                menge = sum(m for mint, m in e["raus"] if mint == token)
                if menge <= 0:
                    continue
                n_tx.add(id(tx))
                keys, _ = spaeher._schluessel(tx)
                progs = set(keys)
                for ins in (((tx.get("transaction") or {}).get("message") or {}).get("instructions") or []):
                    if isinstance(ins, dict) and ins.get("programId"):
                        progs.add(ins["programId"])
                typen = {str(((ins or {}).get("parsed") or {}).get("type") or "") for ins in
                         (((tx.get("transaction") or {}).get("message") or {}).get("instructions") or []) if isinstance(ins, dict)}
                for inn in ((tx.get("meta") or {}).get("innerInstructions") or []):
                    typen |= {str(((i or {}).get("parsed") or {}).get("type") or "") for i in inn.get("instructions") or [] if isinstance(i, dict)}
                if typen & {"burn", "burnChecked"} and e["art"] == "out" and not e.get("gegen"):
                    ui["burn"] += menge
                elif progs & set(intel.LOCKER_SOL):
                    ui["lock"] += menge
                elif e["art"] in ("sell", "swap"):
                    ui["sell"] += menge
                else:
                    ziel = e.get("gegen") or "?"
                    an_ui[ziel] = an_ui.get(ziel, 0) + menge
            gesamt = ui["lock"] + ui["sell"] + ui["burn"] + sum(an_ui.values())
            if gesamt > 0:
                f = weg / gesamt
                gesperrt, verkauft, verbrannt = int(ui["lock"] * f), int(ui["sell"] * f), int(ui["burn"] * f)
                weiter_an = {a: int(m * f) for a, m in an_ui.items() if a != "?"}
        except Exception as e:
            log.debug(f"beutel fwd sol {w[:8]}: {e}")
    teile = []
    if verbrannt:
        teile.append(f"burned {p(verbrannt)} of supply")
    if gesperrt:
        bis = ""
        if s.get("unlock") and s.get("unlock") > time.time():
            bis = " until " + time.strftime("%d %b %Y", time.gmtime(s["unlock"]))
        teile.append(f"locked {p(gesperrt)} of supply{bis}")
    if verkauft:
        teile.append(f"sold {p(verkauft)} of supply")
    if in_vertrag:
        teile.append(f"moved {p(in_vertrag)} of supply into a contract (a lock or a sale, not a known pool)")
    for an, m in sorted(weiter_an.items(), key=lambda x: -x[1])[:2]:
        teile.append(f"sent {p(m)} {'to' if kopf == 'The dev' else 'on to'} {_kurz(an)}")
    if weiter_an and chain != "solana":
        # Seit 2.63: neue Weiterleitungen sofort in die gemeinsame Liste (iw_fwd), damit Trace und Wallet history sie
        # kennen. Vorher stand dort nur, was der letzte Tiefen-Scan gesehen hatte.
        try:
            import weiter
            weiter._speichern(chain, str(token).lower(), [{"from": w.lower(), "to": an.lower(), "pct": round(m / sup * 100, 3)}
                                                          for an, m in weiter_an.items() if sup])
        except Exception as e:
            log.debug(f"beutel fwd speichern: {e}")
    # Seit 2.50.12: wer jetzt Tokens bekommen hat, wird ab sofort selbst beobachtet (Kette bis Tiefe FWD_TIEFE)
    tiefe = 0 if kopf == "The dev" else next((x.get("depth", 1) for x in s.get("fwd") or [] if x["to"] == w), 1)
    if tiefe < FWD_TIEFE:
        bekannt = {x["to"] for x in s.get("fwd") or []} | {s.get("dev")}
        for an, m in weiter_an.items():
            if an in bekannt or len(s.get("fwd") or []) >= FWD_MAX or m / sup * 100 < FWD_MIN:
                continue
            s.setdefault("fwd", []).append({"to": an, "src": "dev" if kopf == "The dev" else "forward",
                                            "pct": round(m / sup * 100, 3), "depth": tiefe + 1, "via": w})
            s.setdefault("_neu", []).append(an)
    if not teile:
        teile.append(f"moved {p(weg)} of supply")
    zeichen = "🔥" if verbrannt else "🔒" if gesperrt else "💸" if verkauft else "⚠️" if in_vertrag else "📤"
    neu_n = len([x for x in s.get("_neu") or [] if x in weiter_an])
    wann = "just " if len(n_tx) <= 1 else ""
    viele = f" in {len(n_tx)} transactions since the last check" if len(n_tx) > 1 else ""
    return (f"{zeichen} {kopf} {wann}" + ", ".join(teile) + f"{viele}. Still holds {p(rest)}."
            + (f" Now watching {'that wallet' if neu_n == 1 else str(neu_n) + ' new wallets'} too." if neu_n else ""))


def _kurz_her(s, art, sek):
    g = s.setdefault("gesendet", {})
    if time.time() - (g.get(art) or 0) < sek:
        return True
    g[art] = int(time.time())
    return False


def runde():
    """Alle Coins aller Nutzer mit Wallet Guard: jeder Coin einmal je Runde, Alarm an alle Halter."""
    if not _an():
        return
    import motor
    je_coin = {}
    for adr in motor._nutzer():
        try:
            if motor.zustand(adr)["prefs"].get("guard") is not True or not motor._darf(adr):
                continue
            for ch, t, s in _coins(adr):
                je_coin.setdefault((ch, t), []).append(adr)
        except Exception as e:
            log.debug(f"beutel nutzer {adr[:8]}: {e}")
    gruppen = {}
    for g in _gruppen():
        gruppen.setdefault((g["chain"], g["token"]), []).append(g)
        je_coin.setdefault((g["chain"], g["token"]), [])
    jetzt = time.time()
    for (ch, t), nutzer in je_coin.items():
        if jetzt - _HALT.get((ch, t), 0) < TAKT:
            continue
        _HALT[(ch, t)] = jetzt
        try:
            alarme, extra = _pruefe_coin(ch, t)
        except Exception as e:
            log.debug(f"beutel {ch} {t[:8]}: {e}")
            continue
        if not alarme:
            continue
        s, zn = extra
        text = (f"⚠️ ${s.get('sym')}: something just happened in a coin you hold\n\n" + "\n".join(alarme)
                + (f"\n\nPrice {motor._px(zn.get('price'))} · MC {_usd(zn.get('mc'))} · Liq {_usd(zn.get('liq'))}" if zn.get("price") else ""))
        knoepfe = [[("Full report", s["chart"])]] if s.get("chart") else []
        if s.get("torch"):
            knoepfe.append([("🔥 Sell on Torch", s["torch"])])
        try:  # seit 2.61.7: direkt reagieren koennen statt raten, was man jetzt tun soll
            import scan
            info = {"chain": ch, "address": t, "symbol": s.get("sym")}
            zeile = [("❓ What can I do?", f"cb:h:{scan._kurzid(info)}")]
            if s.get("dev") and ch != "solana" and any("dev" in x.lower() for x in alarme):
                zeile.insert(0, ("🧭 Trace the dev", motor.frage_knopf(
                    f"Trace the wallet {s['dev']} on {ch} for ${s.get('sym')}: where did its money come from and where did its tokens go?")))
            knoepfe.append(zeile)
        except Exception as e:
            log.debug(f"beutel knoepfe: {e}")
        knoepfe = knoepfe or None
        verkauf_reihe = None
        try:  # seit 2.69.10: wie beim Exit Coach direkt verkaufen, die eigene Wallet unterschreibt
            import scan
            i_ = scan._kurzid({"chain": ch, "address": t, "symbol": s.get("sym")})
            verkauf_reihe = [("Sell 25%", f"cb:s:{i_}:25"), ("Sell 50%", f"cb:s:{i_}:50"), ("Sell all", f"cb:s:{i_}:100")]
        except Exception:
            pass
        for adr in nutzer:
            try:
                k_ = knoepfe
                try:
                    if verkauf_reihe and motor._handel_an(adr):
                        k_ = list(knoepfe or [])
                        k_.insert(1 if s.get("chart") else 0, verkauf_reihe)
                except Exception:
                    k_ = knoepfe
                motor.senden(adr, text, k_)
                try:  # seit 2.52: bei LP-Zug, Dev-Verkauf, Dump anrufen, wenn eingerichtet
                    import anruf
                    anruf.wichtig(adr, f"${s.get('sym')}:\n" + "\n".join(alarme), "bag")
                except Exception as e:
                    log.debug(f"beutel anruf: {e}")
                import aktiv
                aktiv.merken(adr, "bag", f"{ch}:{t} {alarme[0][:100]}")
            except Exception as e:
                log.debug(f"beutel senden: {e}")
        if gruppen.get((ch, t)):
            import agent
            gtext = (f"⚠️ ${s.get('sym')}: update on a coin posted here\n\n" + "\n".join(alarme)
                     + (f"\n\nPrice {motor._px(zn.get('price'))} · MC {_usd(zn.get('mc'))} · Liq {_usd(zn.get('liq'))}" if zn.get("price") else ""))
            for g in gruppen[(ch, t)]:
                p = {"chat_id": g["cid"], "text": gtext, "disable_web_page_preview": True}
                if g.get("thema"):
                    p["message_thread_id"] = g["thema"]
                if g.get("msg"):
                    p["reply_parameters"] = {"message_id": g["msg"], "allow_sending_without_reply": True}
                if s.get("chart"):
                    p["reply_markup"] = {"inline_keyboard": [[{"text": "Full report", "url": s["chart"]}]]}
                try:
                    agent._tg("sendMessage", **p)
                except Exception as e:
                    log.debug(f"beutel gruppe senden: {e}")
