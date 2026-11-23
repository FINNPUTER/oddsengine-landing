"""
coach.py: Exit Coach von Your Engine, seit 2.50. Baut auf der Bag-Wache (beutel.py) auf.

Die Bag-Wache sagt, wenn bei einem Coin etwas Schlimmes passiert (Dev verkauft, LP weg). Der Exit Coach sagt, wann
es fuer DEINE Position Zeit fuer eine Entscheidung ist:
  Gewinnmarken   +50, +100, +200, +500, +1000 % seit dem Einstand (einmal je Marke, mit einem Plan-Vorschlag)
  Verlustmarken  -30, -50, -75 % seit dem Einstand
  Giveback       war mindestens 1.5x im Plus und hat die Haelfte des Gewinns wieder abgegeben
Positionen kommen aus denselben Wallets wie die Bag-Wache (verknuepfte 0x-Wallets, Solana-Wallet, Guard-Wallets),
egal ob MetaMask, Phantom oder anderes: gelesen wird nur die Adresse on-chain.

Einstand: Preis, als Your Engine die Position zum ersten Mal gesehen hat. Kauft der Nutzer nach, wird gemittelt.
Seit 2.68.2: Kauf ueber Your Engine -> Einstand ist der Kaufpreis (mit schon gehaltenen gemittelt). Jede Meldung bringt
Verkaufs-Knoepfe (25 / 50 / alles) aus der eigenen Wallet, auch bei Torch-Coins.
Er kann den Einstand selbst setzen ("my entry for SIL was 0.00015"). Your Engine handelt nicht, es meldet nur.
Keine KI, keine Kosten fuer den Nutzer. Laeuft nur mit Wallet Guard an.

  runde()                            aus motor.tick
  status(adr)                        Positionen mit x seit Einstand, Hoch, naechste Marke
  einstellen(adr, ...)               an/aus, Einstand setzen, Marken aendern
"""
import os
import time

from logger import log

TAKT = int(os.getenv("COACH_POLL_SEC", "120"))
STUFEN = [1.5, 2.0, 3.0, 6.0, 11.0]   # wie im Bot: +50, +100, +200, +500, +1000 %
STOPS = [-30.0, -50.0, -75.0]
GB_MIN = float(os.getenv("COACH_GB_MIN_X", "1.5"))
GB_ANTEIL = float(os.getenv("COACH_GB_SHARE", "0.5"))
GB_NEU = float(os.getenv("COACH_GB_REARM", "1.25"))
_ZULETZT = {}

PLAN = {1.5: "Good moment to set your plan: where you take profit, and where you get out if it turns.",
        2.0: "Selling half here takes your starting money out. The rest rides for free.",
        3.0: "Taking another part off locks in the win if it turns.",
        6.0: "Runners like this often give a lot back. Taking some here is never wrong.",
        11.0: "Over 10x. Most never see one. Make sure some of it ends up in your pocket."}


def _p(adr):
    import motor
    z = motor.zustand(adr)
    c = z["prefs"].setdefault("coach", {})
    c.setdefault("pos", {})
    return z, c


def _x(v):
    return f"{v:.2f}x" if v < 10 else f"{v:.0f}x"


def _gehalten(adr):
    """[(chain, token, symbol, usd, amount)] aus dem Speicher der Bag-Wache."""
    import beutel
    beutel._coins(adr)
    return beutel.gehalten(adr)


def _knoepfe(chain, token, adr=None, verkaufen=False):
    """Seit 2.59.1 mit Verkaufen: Torch-Coins auf Torch (Gebuehren bleiben im Torch-Oekosystem), sonst aus der eigenen
    Wallet zum Unterschreiben, wenn der Handel an ist, oder ueber Jupiter."""
    import beutel
    try:
        info = beutel._info(chain, token) or {}
    except Exception:
        info = {}
    reihen = []
    if info.get("chart"):
        reihen.append([("Full report", info["chart"])])
    handel_an = False
    if verkaufen and adr:
        try:
            import motor
            handel_an = motor._handel_an(adr)
        except Exception:
            handel_an = False
    if handel_an:
        try:
            import scan
            i = scan._kurzid({"chain": chain, "address": token, "symbol": info.get("sym")})
            reihen.append([("Sell 25%", f"cb:s:{i}:25"), ("Sell 50%", f"cb:s:{i}:50"), ("Sell all", f"cb:s:{i}:100")])
        except Exception:
            pass
    elif chain == "solana" and not info.get("torch"):
        reihen.append([("Sell on Jupiter", f"https://jup.ag/swap/{token}-SOL")])
    if info.get("torch"):
        # seit 2.68.2: bei Torch-Coins zusaetzlich zum Verkauf aus der eigenen Wallet (ein Tippen, die Wallet unterschreibt)
        reihen.append([("🔥 Sell on Torch", info["torch"])])
    return reihen or None


def kauf(adr, chain, token, sym, usd, menge):
    """Seit 2.68.2: Kauf ueber Your Engine bestaetigt -> Einstand ist dieser Kauf (bei schon gehaltenen Coins gemittelt)."""
    import motor
    try:
        usd, menge = float(usd or 0), float(menge or 0)
    except Exception:
        return
    if usd <= 0 or menge <= 0:
        return
    preis = usd / menge
    t = token.lower() if str(token).startswith("0x") else token
    z, c = _p(adr)
    k = f"{chain}:{t}"
    pos = c["pos"].get(k)
    jetzt = int(time.time())
    if pos and pos.get("entry") and pos.get("amt"):
        alt = float(pos["amt"])
        pos.update(entry=(alt * pos["entry"] + menge * preis) / (alt + menge), amt=alt + menge, src="set", set_ts=jetzt,
                   bought=True)
        pos["peak"] = max(pos.get("peak") or preis, preis)
        pos["marks"] = [m for m in pos.get("marks") or [] if isinstance(m, (int, float)) and m < 0]
    else:
        c["pos"][k] = {"sym": sym, "entry": preis, "amt": menge, "peak": preis, "marks": [], "gb": 0, "src": "set",
                       "set_ts": jetzt, "bought": True}
    motor._zustand_speichern(adr, z)


def pruefen(pos, px, menge, stufen, stops):
    """Reine Logik, ohne Netz. pos wird angepasst. -> Liste von (art, text-teil)"""
    out = []
    if not px or px <= 0:
        return out
    if not pos.get("entry"):
        pos.update(entry=px, amt=menge, peak=px, marks=[], gb=0, src="seen", since=int(time.time()))
        return out
    alt = pos.get("amt") or 0
    if menge and alt and menge > alt * 1.1:
        # nachgekauft: Einstand mitteln, Marken ueber dem neuen Stand wieder scharf
        pos["entry"] = (alt * pos["entry"] + (menge - alt) * px) / menge
        pos["marks"] = [m for m in pos.get("marks") or [] if isinstance(m, (int, float)) and m < 0 and (px / pos["entry"] - 1) * 100 <= m]
        pos["peak"] = max(px, pos["entry"])
    if menge is not None:
        pos["amt"] = menge
    e = pos["entry"]
    x = px / e
    pos["peak"] = max(pos.get("peak") or px, px)
    marken = pos.setdefault("marks", [])
    hoch = [s for s in stufen if x >= s and s not in marken]
    if hoch:
        s = max(hoch)
        marken += hoch
        out.append(("profit", s, x))
    pct = (x - 1) * 100
    tief = [s for s in stops if pct <= s and s not in marken]
    if tief:
        s = min(tief)
        marken += tief
        out.append(("loss", s, x))
    px_hoch = pos["peak"]
    hx = px_hoch / e
    if hx >= GB_MIN and px_hoch > e:
        abgegeben = (px_hoch - px) / (px_hoch - e)
        if abgegeben >= GB_ANTEIL and px_hoch > (pos.get("gb") or 0) * GB_NEU:
            pos["gb"] = px_hoch
            out.append(("giveback", hx, x))
    return out


def _stand(pos, e, jetzt):
    """Seit 2.66.2: Market Cap statt Preis, wenn wir die Menge der Token kennen (MC / Preis)."""
    import motor
    n = pos.get("supply")
    if n:
        return f"MC {motor._usd_kurz(e * n)} → {motor._usd_kurz(jetzt * n)}"
    return None  # ohne Market Cap lieber keine Zahl als einen Preis


def text(sym, art, wert, x, pos, usd):
    import motor
    e = pos["entry"]
    jetzt = e * x
    bag = f" Your bag: about {motor._usd_kurz(usd)}." if usd else ""
    st = _stand(pos, e, jetzt)
    st = f" ({st})" if st else ""
    if art == "profit":
        stand = f"is up {(x - 1) * 100:.0f}%" if x < 2 else f"is at {_x(x)}"
        return (f"🟢 ${sym} {stand} since your entry{st}.{bag}\n\n"
                f"Plan idea: {PLAN.get(wert, PLAN[11.0])}\n\nYour call. The button opens the sale, you confirm it yourself.")
    if art == "loss":
        return (f"🔻 ${sym} is down {abs(wert):.0f}% from your entry{st}.{bag}\n\n"
                "Check if the reason you bought still holds. The full report shows what changed.")
    return (f"⚠️ ${sym} was up {_x(wert)}, now {_x(x)}{st}. About half of the gain is gone.{bag}\n\n"
            "Decide now whether you want to keep the rest, before the market decides for you.")


def runde():
    import beutel
    import motor
    import scan
    if not beutel._an():
        return 0
    preise, gesendet = {}, 0
    jetzt = time.time()
    for adr in motor._nutzer():
        try:
            z = motor.zustand(adr)
            p = z["prefs"]
            if p.get("guard") is not True or (p.get("coach") or {}).get("on") is False or not motor._darf(adr):
                continue
            if jetzt - _ZULETZT.get(adr, 0) < TAKT:
                continue
            _ZULETZT[adr] = jetzt
            z, c = _p(adr)
            stufen = c.get("steps") or STUFEN
            stops = c.get("stops") or STOPS
            gehalten = _gehalten(adr)
            da = set()
            for ch, t, sym, usd, menge in gehalten:
                k = f"{ch}:{t}"
                da.add(k)
                if (ch, t) not in preise:
                    try:
                        zz = scan.zahlen(ch, t)
                        preise[(ch, t)] = (zz.get("price"), zz.get("mc"))
                    except Exception:
                        preise[(ch, t)] = (None, None)
                px, mc = preise[(ch, t)]
                pos = c["pos"].setdefault(k, {"sym": sym})
                pos["sym"] = sym or pos.get("sym")
                if px and mc:
                    pos["supply"] = mc / px
                for art, wert, x in pruefen(pos, px, menge, stufen, stops):
                    wert_jetzt = menge * px if menge and px else usd
                    motor.senden(adr, text(pos["sym"], art, wert, x, pos, wert_jetzt), _knoepfe(ch, t, adr, True))
                    gesendet += 1
                    try:
                        import aktiv
                        aktiv.merken(adr, "coach", f"{k} {art} {wert}")
                    except Exception:
                        pass
            # Verkaufte Positionen vergessen (eigene Einstaende bleiben 30 Tage, falls sie wiederkommen)
            for k in list(c["pos"]):
                if k not in da and c["pos"][k].get("src") != "set":
                    c["pos"].pop(k, None)
                elif k not in da and jetzt - (c["pos"][k].get("set_ts") or 0) > 30 * 86400:
                    c["pos"].pop(k, None)
            motor._zustand_speichern(adr, z)
        except Exception as e:
            log.debug(f"coach {adr[:8]}: {e}")
    return gesendet


def status(adr):
    import scan
    z, c = _p(adr)
    out = []
    for ch, t, sym, usd, menge in _gehalten(adr):
        pos = c["pos"].get(f"{ch}:{t}") or {}
        try:
            px = scan.zahlen(ch, t).get("price")
        except Exception:
            px = None
        e = pos.get("entry")
        x = px / e if px and e else None
        n = pos.get("supply")
        stufen = c.get("steps") or STUFEN
        naechste = next((s for s in stufen if x is not None and s > x and s not in (pos.get("marks") or [])), None)
        wert = menge * px if menge and px else usd
        out.append({"coin": f"${sym}", "chain": ch, "value_usd": round(wert, 2) if wert else None,
                    "entry": e, "entry_from": "set by you" if pos.get("src") == "set" else "first seen in your wallet",
                    "price": px, "entry_mc": round(e * n) if e and n else None, "mc": round(px * n) if px and n else None,
                    "x_since_entry": round(x, 2) if x else None,
                    "peak_x": round(pos["peak"] / e, 2) if e and pos.get("peak") else None,
                    "next_mark": f"{_x(naechste)}" if naechste else None})
    return {"on": c.get("on", True), "profit_marks": [_x(s) for s in (c.get("steps") or STUFEN)],
            "loss_marks": [f"{s:.0f}%" for s in (c.get("stops") or STOPS)], "positions": out,
            "note": "Talk in market cap (entry_mc, mc), not in price, whenever they are there. Entry is the price when Your Engine first saw the coin in the wallet, unless the user set it. "
                    "Your Engine only alerts, it never sells."}


def einstellen(adr, on=None, token=None, entry=None, steps=None, stops=None):
    import motor
    z, c = _p(adr)
    if on is not None:
        c["on"] = bool(on)
    if steps:
        try:
            w = sorted({float(str(s).lower().replace("x", "").strip()) for s in str(steps).split(",") if str(s).strip()})
            w = [s for s in w if 1.1 <= s <= 1000][:6]
            if w:
                c["steps"] = w
        except Exception:
            return {"ok": False, "error": "Marks look like 2x, 3x, 5x."}
    if stops:
        try:
            w = sorted({-abs(float(str(s).replace("%", "").strip())) for s in str(stops).split(",") if str(s).strip()}, reverse=True)
            w = [s for s in w if -95 <= s <= -5][:4]
            if w:
                c["stops"] = w
        except Exception:
            return {"ok": False, "error": "Loss marks look like -30, -50."}
    if token and entry is not None:
        roh = str(entry).lower().replace("$", "").replace(",", "").replace("mc", "").replace("market cap", "").strip()
        faktor = 1.0
        if roh.endswith("k"):
            faktor, roh = 1e3, roh[:-1]
        elif roh.endswith("m"):
            faktor, roh = 1e6, roh[:-1]
        elif roh.endswith("b"):
            faktor, roh = 1e9, roh[:-1]
        try:
            zahl = float(roh.strip()) * faktor
        except Exception:
            return {"ok": False, "error": "Give the entry as a market cap, e.g. 50K or 1.2M."}
        t = str(token).strip().lstrip("$").lower()
        treffer = [(ch, tk, s) for ch, tk, s, _, _ in _gehalten(adr) if tk.lower() == t or str(s).lower() == t]
        if not treffer:
            return {"ok": False, "error": "That coin is not in your linked wallets."}
        ch, tk, s = treffer[0]
        pos = c["pos"].setdefault(f"{ch}:{tk}", {"sym": s})
        # Seit 2.66.2: Einstand als Market Cap (50K, 1.2M, oder jede Zahl ab 1000), sonst als Preis
        if faktor > 1 or zahl >= 1000:
            try:
                import scan
                zz = scan.zahlen(ch, tk)
                n = zz["mc"] / zz["price"]
            except Exception:
                return {"ok": False, "error": "Could not read the market cap of that coin right now. Try again in a minute."}
            pos["supply"] = n
            preis = zahl / n
        else:
            preis = zahl
        pos.update(entry=preis, peak=preis, marks=[], gb=0, src="set", set_ts=int(time.time()))
    motor._zustand_speichern(adr, z)
    return dict(status(adr), ok=True)
