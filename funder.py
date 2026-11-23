"""Funder-Netz (seit 2.78.0).

Der Tiefen-Scan schaute bisher auf den Coin (wer haelt jetzt was). Hier kommt die Geld-Sicht dazu:
wer hat den Dev finanziert, was hat dieser Geldgeber selbst mit dem Coin gemacht, welche anderen Wallets hat er
gefuellt, was haben die mit dem Coin gemacht, haben sie ueber dasselbe Launchpad eigene Coins gestartet, und floss
Geld zu ihm zurueck. Dazu: Admin-Wallets (Owner, Safe-Owner) mit demselben Geldgeber.

Alles aus Chain-Daten mit Beweislinks. Keine Aussage darueber, wer hinter einem Wallet steht.
Die Token-Bewegungen kommen aus den Transfer-Logs, die der Tiefen-Scan ohnehin liest (kein Explorer noetig).
Explorer (Blockscout) nur fuer Geldgeber und Kinder-Wallets, mit Wiederholung; was nicht lesbar war, steht in "gaps".
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("funder")

MAX_KIDS = int(os.getenv("FUNDER_MAX_CHILDREN", "40"))
_START = {}
BUDGET = float(os.getenv("FUNDER_BUDGET_SEC", "45"))
TOTE = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}


def _kurz(a):
    a = str(a or "")
    return a[:6] + "…" + a[-4:] if len(a) > 12 else a


def _link(chain, art, a):
    try:
        import spur
        return spur._link(chain, art, a)
    except Exception:
        return None


def _txlist(chain, w, sort="asc", offset=25, versuche=3):
    """-> (liste, ok). ok False = Explorer hat nicht geantwortet (auch nach Wiederholung)."""
    import intel
    for v in range(versuche):
        d = intel._bs(chain, "/api", {"module": "account", "action": "txlist", "address": w, "sort": sort,
                                      "page": 1, "offset": offset})
        if isinstance(d, dict) and isinstance(d.get("result"), list):
            return d["result"], True
        if isinstance(d, dict) and str(d.get("message") or "").lower().startswith("no transactions"):
            return [], True
        time.sleep(2.0 * (v + 1))
    return [], False


def geldgeber(chain, w):
    """Erste eingehende Nativ-Zahlung: {"from", "tx", "ts", "amount"} | None, dazu status "ok" | "none" | "down"."""
    import intel
    w = str(w).lower()
    try:
        import kartei
        g = kartei.geldgeber(chain, w)
        if g and g.get("chain") == chain and g.get("from"):
            return {"from": g["from"], "tx": g.get("tx"), "ts": g.get("ts"), "amount": None}, "ok"
    except Exception:
        pass
    liste, ok = _txlist(chain, w, "asc", 25)
    for x in liste:
        if str(x.get("to") or "").lower() == w and int(x.get("value") or 0) > 0:
            return {"from": str(x.get("from") or "").lower(), "tx": x.get("hash"), "ts": int(x.get("timeStamp") or 0),
                    "amount": int(x["value"]) / 1e18}, "ok"
    # ohne normalen Eingang: erste interne Zahlung (Bridge, Vertrag)
    for v in range(2):
        d = intel._bs(chain, "/api", {"module": "account", "action": "txlistinternal", "address": w, "sort": "asc",
                                      "page": 1, "offset": 10})
        if isinstance(d, dict) and isinstance(d.get("result"), list):
            for x in d["result"]:
                if str(x.get("to") or "").lower() == w and int(x.get("value") or 0) > 0:
                    return {"from": str(x.get("from") or "").lower(), "tx": x.get("hash") or x.get("transactionHash"),
                            "ts": int(x.get("timeStamp") or 0), "amount": int(x["value"]) / 1e18}, "ok"
            return None, "none" if ok else "down"
        time.sleep(2.0)
    return None, "none" if ok else "down"


def bewegungen(logs, sup):
    """Je Adresse aus den Transfer-Logs: rein, raus (roh), erster Eingangsblock, Block des Ausstiegs (Bestand ~0)."""
    out = {}
    rest = sup * 0.00002 if sup else 0
    for l in logs or []:
        try:
            von = "0x" + l["topics"][1][-40:]
            an = "0x" + l["topics"][2][-40:]
            m = int(l.get("data") or "0x0", 16)
            bl = int(l["blockNumber"], 16)
        except Exception:
            continue
        a = out.setdefault(an, {"rein": 0, "raus": 0, "stand": 0, "erst": None, "exit": None, "tx": None, "von": {}})
        a["rein"] += m
        a["von"][von] = a["von"].get(von, 0) + m
        a["stand"] += m
        if a["erst"] is None:
            a["erst"], a["tx"] = bl, l.get("transactionHash")
        a["exit"] = None
        b = out.setdefault(von, {"rein": 0, "raus": 0, "stand": 0, "erst": None, "exit": None, "tx": None, "von": {}})
        b["raus"] += m
        b["stand"] -= m
        if b["erst"] is not None and b["stand"] <= rest and b["exit"] is None:
            b["exit"] = bl
    return out


_CODE = {}


def gekauft(chain, b):
    """Aufteilung der Eingaenge einer Adresse: aus Vertraegen (Pool, Router, Launchpad = Kauf) oder von Wallets (Transfer).
    -> (kauf_roh, transfer_roh)"""
    import intel
    import codeart
    offen = [a for a in (b.get("von") or {}) if a not in _CODE and a not in TOTE]
    if offen:
        codes = intel._evm_batch(chain, [("eth_getCode", [a, "latest"]) for a in offen[:40]]) or []
        for a, c in zip(offen[:40], codes):
            if c is not None:
                _CODE[a] = codeart.norm(c) not in ("0x", "0x0")
    kauf = sum(m for a, m in (b.get("von") or {}).items() if _CODE.get(a) or a in TOTE)
    return kauf, (b.get("rein") or 0) - kauf


def _pct(x, sup):
    return round(x / sup * 100, 3) if sup else 0.0


def startkaeufe(chain, block, logs, sup, kaeufer, zeit, fenster=2):
    """Was im Launch-Block (und den naechsten 'fenster' Bloecken) gekauft wurde, nach Kaufmenge, nicht nach heutigem Bestand.
    zeit(block) -> Unix-Zeit oder None. Nur Wallets ohne Code (kaeufer sind schon gefiltert)."""
    bw = bewegungen(logs, sup)
    fruh = [k for k in kaeufer if k.get("slot") is not None and k["slot"] - block <= fenster]
    if not fruh or not sup:
        return {"checked": True, "wallets": 0, "bought_pct": 0.0, "sold_pct": 0.0}
    # Menge, die im Fenster ankam
    im = {}
    w_set = {k["wallet"] for k in fruh}
    for l in logs:
        try:
            bl = int(l["blockNumber"], 16)
            if bl - block > fenster:
                continue
            an = "0x" + l["topics"][2][-40:]
            if an in w_set:
                im[an] = im.get(an, 0) + int(l.get("data") or "0x0", 16)
        except Exception:
            continue
    t0 = zeit(block)
    liste, summe_kauf, verkauft, schnellste = [], 0, 0, None
    for k in fruh:
        w = k["wallet"]
        menge = im.get(w) or k.get("raw") or 0
        b = bw.get(w) or {}
        raus_s = None
        if b.get("exit") is not None and t0:
            te = zeit(b["exit"])
            if te:
                raus_s = max(0, te - t0)
        summe_kauf += menge
        if b.get("exit") is not None:
            verkauft += menge
            if raus_s is not None:
                schnellste = raus_s if schnellste is None else min(schnellste, raus_s)
        liste.append({"wallet": w, "bought_pct": _pct(menge, sup), "out_after_s": raus_s, "funder": k.get("funder"),
                      "proof": _link(chain, "tx", k.get("tx"))})
    liste.sort(key=lambda x: -x["bought_pct"])
    return {"checked": True, "wallets": len(fruh), "bought_pct": _pct(summe_kauf, sup), "sold_pct": _pct(verkauft, sup),
            "fastest_exit_s": schnellste, "list": liste[:8]}


def _erzeugung(chain, logs, token):
    """(tx, sender, an, selektor) der Transaktion, die den Coin geminted hat. Der Selektor (erste 4 Bytes der Eingabe)
    unterscheidet "Coin erstellen" von "Coin kaufen", wenn beides ueber denselben Launchpad-Vertrag laeuft."""
    import intel
    for l in logs[:50]:
        try:
            if "0x" + l["topics"][1][-40:] in TOTE:
                tx = l.get("transactionHash")
                t = intel._evm(chain, "eth_getTransactionByHash", [tx]) or {}
                sel = str(t.get("input") or "")[:10].lower()
                return (tx, str(t.get("from") or "").lower() or None, str(t.get("to") or "").lower() or None,
                        sel if len(sel) == 10 else None)
        except Exception:
            continue
    return None, None, None, None


def _owner(chain, token):
    """Owner des Tokens und, ist der Owner ein Safe, dessen Owner-Liste."""
    import intel
    out = []
    try:
        r = intel._evm(chain, "eth_call", [{"to": token, "data": "0x8da5cb5b"}, "latest"]) or "0x"
        o = "0x" + r[-40:] if len(r) >= 66 else None
    except Exception:
        o = None
    if not o or o in TOTE:
        return None, []
    try:
        r = intel._evm(chain, "eth_call", [{"to": o, "data": "0xa0e67e2b"}, "latest"]) or "0x"
        roh = r[2:]
        if len(roh) >= 128:
            n = int(roh[64:128], 16)
            for i in range(min(n, 10)):
                out.append("0x" + roh[128 + i * 64: 128 + (i + 1) * 64][-40:])
    except Exception:
        pass
    return o, out


def netz(chain, token, dev, block, logs, sup, kaeufer, zeit, infra=()):
    """Das Funder-Netz um den Dev. Gibt immer ein dict zurueck; checked=False, wenn nichts lesbar war."""
    t0 = time.time()
    token, dev = str(token).lower(), str(dev or "").lower()
    out = {"checked": False, "gaps": []}
    if not dev or chain == "solana":
        return out
    import spur
    tx0, sender0, fabrik, selektor = _erzeugung(chain, logs, token)
    deployer = {dev} | ({sender0} if sender0 else set())
    out["deployers"] = sorted(deployer)
    if fabrik:
        out["launch_contract"] = fabrik
    # 1. Geldgeber der Deployer
    gg, stati = {}, {}
    for d in deployer:
        h, st = geldgeber(chain, d)
        stati[d] = st
        if h and h.get("from"):
            gg[d] = h
    if not gg:
        if "down" in stati.values():
            out["gaps"].append("The explorer did not answer for the dev wallet, its funder could not be read. Run the scan again later.")
        else:
            out["gaps"].append("No funding payment found for the dev wallet.")
        return out
    f = (gg.get(dev) or next(iter(gg.values())))["from"]
    art = spur.name(chain, f)
    out.update(checked=True, dev_funder=f, funder_kind=art.get("kind"), funder_name=art.get("name"),
               funded_dev_proof=_link(chain, "tx", (gg.get(dev) or next(iter(gg.values()))).get("tx")),
               funded_both_deployers=len(deployer) > 1 and all(gg.get(d, {}).get("from") == f for d in deployer))
    if art.get("kind") != "wallet":
        out["note"] = f"The dev was funded from a {art.get('kind')}" + (f" ({art['name']})" if art.get("name") else "") + ", so there is no private funder to follow."
        return out
    bw = bewegungen(logs, sup)
    t_launch = zeit(block)
    # 2. Was hat der Geldgeber selbst mit dem Coin gemacht
    fb = bw.get(f)
    if fb and (fb["rein"] or fb["raus"]):
        erst_min = None
        if fb.get("erst") is not None and t_launch:
            te = zeit(fb["erst"])
            erst_min = round((te - t_launch) / 60, 1) if te else None
        kf, tr = gekauft(chain, fb)
        out["funder_traded"] = {"bought_pct": _pct(kf, sup), "received_pct": _pct(tr, sup), "sold_pct": _pct(fb["raus"], sup),
                                "holds_pct": _pct(max(0, fb["stand"]), sup), "first_in_min_after_launch": erst_min,
                                "proof": _link(chain, "tx", fb.get("tx"))}
    # 3. Kinder: alle Wallets, die der Geldgeber mit Nativ gefuellt hat
    kids, zurueck, cut = {}, 0.0, False
    liste, ok = _txlist(chain, f, "desc", 1000)
    if not ok:
        out["gaps"].append(f"The explorer did not answer for the funder {_kurz(f)}, the wallets it funded could not be listed.")
    cut = len(liste) >= 1000
    for x in liste:
        von, an = str(x.get("from") or "").lower(), str(x.get("to") or "").lower()
        v = int(x.get("value") or 0)
        if str(x.get("isError") or "0") != "0" or v <= 0:
            continue
        if von == f and an and an not in (f, token) and x.get("input") in ("0x", "", None):
            kids.setdefault(an, {"tx": x.get("hash"), "eth": 0.0})
            kids[an]["eth"] += v / 1e18
    # aus dem Scan: fruehe Kaeufer mit demselben Geldgeber
    for k in kaeufer:
        if k.get("funder") == f and k["wallet"] not in kids:
            kids[k["wallet"]] = {"tx": None, "eth": 0.0}
    for d in deployer:
        kids.pop(d, None)
    # Rueckfluss auch von Kindern, die erst unten gefunden wurden: zweiter Durchgang
    zurueck = 0.0
    for x in liste:
        von, an = str(x.get("from") or "").lower(), str(x.get("to") or "").lower()
        if an == f and (von in kids or von in deployer) and str(x.get("isError") or "0") == "0":
            zurueck += int(x.get("value") or 0) / 1e18
    out["funded_wallets"] = len(kids) + len(deployer)
    out["funded_list_cut"] = cut
    out["eth_back_to_funder"] = round(zurueck, 4)
    # 4. Kinder, die diesen Coin gehandelt haben
    gehandelt = []
    for w, info in kids.items():
        b = bw.get(w)
        if b and (b["rein"] or b["raus"]) and w not in infra:
            kf, tr = gekauft(chain, b)
            gehandelt.append({"wallet": w, "bought_pct": _pct(kf, sup), "received_pct": _pct(tr, sup), "sold_pct": _pct(b["raus"], sup),
                              "holds_pct": _pct(max(0, b["stand"]), sup),
                              "in_launch_window": b.get("erst") is not None and b["erst"] - block <= 2,
                              "funded_proof": _link(chain, "tx", (info or {}).get("tx")) if info and info.get("tx") else None,
                              "first_buy_proof": _link(chain, "tx", b.get("tx"))})
    gehandelt.sort(key=lambda x: -x["bought_pct"])
    if gehandelt:
        out["funded_wallets_traded"] = {"wallets": len(gehandelt),
                                        "bought_pct": round(sum(x["bought_pct"] for x in gehandelt), 2),
                                        "sold_pct": round(sum(x["sold_pct"] for x in gehandelt), 2),
                                        "holds_pct": round(sum(x["holds_pct"] for x in gehandelt), 2),
                                        "launch_window_bought_pct": round(sum(x["bought_pct"] for x in gehandelt if x["in_launch_window"]), 2),
                                        "list": gehandelt[:8]}
    # 5. Haben die Kinder selbst Coins gestartet? Ueber denselben Launch-Vertrag (Launchpad) oder per eigenem Deploy.
    starts, beispiele = 0, []
    alle_kids = [w for w in kids if w not in {g["wallet"] for g in gehandelt}]
    pruefen = alle_kids[:MAX_KIDS]

    def start_von(w):
        k_ = (chain, w, fabrik, selektor)
        c_ = _START.get(k_)
        if c_ and (c_[1] or time.time() - c_[0] < 6 * 3600):
            return w, c_[1]
        if time.time() - t0 > BUDGET:
            return w, None
        w, h_ = _start_lesen(w)
        if h_ is not None:
            _START[k_] = (time.time(), h_)
        return w, h_

    def _start_lesen(w):
        rows, ok_ = _txlist(chain, w, "asc", 60, versuche=2)
        if not ok_:
            return w, None
        for x in rows:
            if str(x.get("from") or "").lower() != w or str(x.get("isError") or "0") != "0":
                continue
            gleiche_funktion = selektor is None or str(x.get("input") or x.get("methodId") or "")[:10].lower() == selektor
            if (fabrik and str(x.get("to") or "").lower() == fabrik and gleiche_funktion) or x.get("contractAddress"):
                return w, x.get("hash")
        return w, False
    nicht_gelesen = 0
    if pruefen:
        with ThreadPoolExecutor(4) as ex:
            for w, h in ex.map(start_von, pruefen):
                if h is None:
                    nicht_gelesen += 1
                elif h:
                    starts += 1
                    if len(beispiele) < 5:
                        beispiele.append({"wallet": w, "proof": _link(chain, "tx", h)})
    # aus unseren eigenen Daten: Coins, deren Dev eines der Kinder ist
    symbole = []
    try:
        import intel
        ks = [intel._k(chain, w) for w in kids]
        if ks:
            rows = intel._q("SELECT DISTINCT t.symbol FROM iw_devs d LEFT JOIN iw_tokens t ON t.chain=d.chain AND t.token=d.token "
                            "WHERE d.chain=%s AND d.dev = ANY(%s) AND d.token<>%s", (chain, ks, intel._k(chain, token))) or []
            symbole = [r[0] for r in rows if r and r[0]][:8]
    except Exception:
        pass
    if starts or symbole:
        out["funded_wallets_launched"] = {"count": starts, "checked": len(pruefen) - nicht_gelesen, "of": len(alle_kids),
                                          "same_launch_contract": bool(fabrik), "known_coins": symbole, "examples": beispiele}
    if nicht_gelesen:
        out["gaps"].append(f"{nicht_gelesen} funded wallets could not be read in time, their launches are not counted.")
    if cut:
        out["gaps"].append("The funder has over 1,000 transactions, only the newest 1,000 were read.")
    # 6. Admin-Wallets (Owner, Safe-Owner) mit demselben Geldgeber
    try:
        owner, safe = _owner(chain, token)
        admins = [a for a in (safe or ([owner] if owner else [])) if a not in TOTE][:6]
        if admins:
            gleich = 0
            for a in admins:
                if a == f or a in deployer:
                    gleich += 1
                    continue
                if time.time() - t0 > BUDGET:
                    break
                h, _ = geldgeber(chain, a)
                if h and h.get("from") == f:
                    gleich += 1
            out["admins"] = {"owner": owner, "safe_owners": len(safe), "checked": len(admins), "same_funder": gleich}
    except Exception as e:
        log.debug(f"funder admins: {e}")
    out["took_s"] = round(time.time() - t0, 1)
    return out


def zeilen(fn, lb=None):
    """Warnzeilen fuer das Urteil: [(stufe, text)] mit stufe 'kritisch' oder 'schlecht'."""
    z = []
    lb = lb or {}
    if (lb.get("bought_pct") or 0) >= 3:
        b, s = lb["bought_pct"], lb.get("sold_pct") or 0
        t = f"{b:g}% of supply bought in the launch block"
        if s >= b * 0.8:
            sek = lb.get("fastest_exit_s")
            t += " and sold again" + (f" within {int(sek)} s" if sek is not None and sek <= 120 else "")
        elif s > 0:
            t += f", {s:g}% of it sold again"
        z.append(("kritisch" if b >= 10 else "schlecht", t))
    if not fn or not fn.get("checked"):
        return z
    ft = fn.get("funder_traded") or {}
    ges = (ft.get("bought_pct") or 0) + (ft.get("received_pct") or 0)
    if ges >= 0.5:
        if (ft.get("received_pct") or 0) < 0.1:
            t = f"The wallet that funded the dev bought {ft['bought_pct']:g}%"
        else:
            t = f"The wallet that funded the dev got {round(ges, 3):g}% ({ft.get('bought_pct') or 0:g}% bought)"
        t += f" and sold {ft['sold_pct']:g}%" if (ft.get("sold_pct") or 0) >= 0.1 else ", still holds it"
        z.append(("kritisch" if (ft.get("sold_pct") or 0) >= 3 else "schlecht", t))
    kt = fn.get("funded_wallets_traded") or {}
    if (kt.get("bought_pct") or 0) >= 1 and kt.get("wallets"):
        t = (f"{kt['wallets']} wallet{'s' if kt['wallets'] != 1 else ''} funded by the dev's funder bought {kt['bought_pct']:g}%"
             + (f", sold {kt['sold_pct']:g}%" if (kt.get("sold_pct") or 0) >= 0.1 else "")
             + (f" ({kt['launch_window_bought_pct']:g}% of it in the launch block)" if (kt.get("launch_window_bought_pct") or 0) >= 1 else ""))
        z.append(("kritisch" if kt["bought_pct"] >= 10 else "schlecht", t))
    kl = fn.get("funded_wallets_launched") or {}
    n = kl.get("count") or 0
    if n >= 3:
        t = (f"The dev's funder also funded {'at least ' if (kl.get('checked') or 0) < (kl.get('of') or 0) else ''}{n} other wallets that launched coins"
             + (" through the same launch contract" if kl.get("same_launch_contract") else ""))
        if (fn.get("eth_back_to_funder") or 0) >= 0.05:
            t += f", {fn['eth_back_to_funder']:g} ETH came back to it"
        z.append(("schlecht", t))
    ad = fn.get("admins") or {}
    if (ad.get("same_funder") or 0) >= 1 and (ad.get("checked") or 0) >= 1:
        z.append(("schlecht", f"{ad['same_funder']} of {ad['checked']} admin wallets were funded by the same wallet as the dev"))
    return z
