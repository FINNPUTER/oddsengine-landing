"""
live.py: Live-Preis-Knopf unter jeder Scan-Karte in Telegram (seit 2.69.6).

Wie damals bei FINNPUTER: die oberste Knopfreihe zeigt Preis, Market Cap und die Aenderung seit dem Scan,
und der Bot aktualisiert nur diesen Knopf (editMessageReplyMarkup) jede Minute. Der Text der Karte bleibt
unveraendert. Ein Tippen auf den Knopf aktualisiert sofort.

Quelle: DexScreener (bis 30 Adressen je Abfrage, frei), fuer Torch-Coins auf der Bonding Curve die Torch-Liste.
Kostet nichts ausser Telegram-Aufrufen, die frei sind.

  LIVE_CARD          true   aus: kein Live-Knopf
  LIVE_CARD_H        24     so lange laeuft eine Karte live
  LIVE_CARD_MAX      300    hoechstens so viele Karten gleichzeitig (die neuesten)
  LIVE_CARD_SEC      60     Takt
"""
import json
import os
import threading
import time

import requests

import storage
from logger import log

KV = "live_karten"
DS = "https://api.dexscreener.com/tokens/v1"
_LOCK = threading.Lock()


def _zahl(name, std):
    try:
        return float(os.getenv(name, str(std)).strip() or std)
    except Exception:
        return std


def an():
    return os.getenv("LIVE_CARD", "true").strip().lower() not in ("0", "false", "no", "off")


def _usd(v):
    try:
        v = float(v)
    except Exception:
        return "?"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def _preis(p):
    try:
        p = float(p)
    except Exception:
        return "?"
    if p >= 1:
        return f"${p:,.2f}"
    if p >= 0.01:
        return f"${p:.4f}"
    s = f"{p:.12f}".rstrip("0")
    # 4 gueltige Stellen nach den fuehrenden Nullen
    i = 2
    while i < len(s) and s[i] == "0":
        i += 1
    return "$" + s[:i + 4]


def text(preis, mc, aenderung=None, seit_scan=False):
    t = f"📈 {_preis(preis)} · MC {_usd(mc)}"
    if aenderung is not None:
        try:
            a = float(aenderung)
            t += f" · {'▲' if a >= 0 else '▼'}{abs(a):.1f}%" + (" since scan" if seit_scan else " 24h")
        except Exception:
            pass
    return t[:60]


def knopf(r, kurzid):
    """Die oberste Knopfreihe einer Scan-Karte, oder None ohne Preis."""
    if not an():
        return None
    m = r.get("market") or {}
    preis = m.get("price_usd") or m.get("price")
    mc = m.get("market_cap") or m.get("mcap") or m.get("fdv")
    if not preis:
        return None
    ch = (m.get("change") or {}).get("h24")
    return (text(preis, mc, ch), f"cb:lp:{kurzid}")


# ── Merken, was live laufen soll ─────────────────────────────────────────

def _laden():
    try:
        return json.loads(storage.kv_get(KV) or "[]")
    except Exception:
        return []


def _speichern(liste):
    try:
        storage.kv_set(KV, json.dumps(liste, separators=(",", ":")))
    except Exception as e:
        log.debug(f"live speichern: {e}")


def _lp_id(markup):
    for reihe in (markup or {}).get("inline_keyboard") or []:
        for k in reihe:
            d = str(k.get("callback_data") or "")
            if d.startswith("lp:"):
                return d[3:]
    return None


def merken(antwort, markup):
    """Aus agent._tg: eine gesendete Nachricht mit Live-Knopf eintragen."""
    if not an():
        return
    try:
        if not (antwort and antwort.get("ok")):
            return
        res = antwort.get("result") or {}
        mid, chat = res.get("message_id"), (res.get("chat") or {}).get("id")
        i = _lp_id(markup)
        if not (mid and chat and i):
            return
        import scan
        info = scan.aus_id(i)
        if not info:
            return
        with _LOCK:
            liste = [x for x in _laden() if not (x["chat"] == chat and x["mid"] == mid)]
            liste.append({"chat": chat, "mid": mid, "id": i, "chain": info["chain"], "address": info["address"],
                          "ts": int(time.time()), "p0": None, "markup": markup, "txt": None})
            liste = liste[-int(_zahl("LIVE_CARD_MAX", 300)):]
            _speichern(liste)
    except Exception as e:
        log.debug(f"live merken: {e}")


# ── Preise holen ─────────────────────────────────────────────────────────

def _preise(eintraege):
    """{(chain, address_lower): {"preis", "mc", "ch24"}} fuer alle Eintraege, gebuendelt je Chain."""
    je = {}
    for x in eintraege:
        je.setdefault(x["chain"], set()).add(x["address"])
    aus = {}
    for chain, adressen in je.items():
        adressen = sorted(adressen)
        for s in range(0, len(adressen), 30):
            teil = adressen[s:s + 30]
            try:
                r = requests.get(f"{DS}/{chain}/{','.join(teil)}", timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                paare = r.json() if r.ok else []
            except Exception as e:
                log.debug(f"live dexscreener {chain}: {e}")
                paare = []
            beste = {}
            for p in paare if isinstance(paare, list) else []:
                a = str((p.get("baseToken") or {}).get("address") or "")
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if a and (a not in beste or liq > beste[a][0]):
                    beste[a] = (liq, p)
            for a, (_, p) in beste.items():
                aus[(chain, a.lower())] = {"preis": p.get("priceUsd"), "mc": p.get("marketCap") or p.get("fdv"),
                                           "ch24": (p.get("priceChange") or {}).get("h24")}
        if chain == "robinhood":
            fehlt = [a for a in adressen if (chain, a.lower()) not in aus]
            if fehlt:
                try:
                    import onchain
                    torch = {str(t.get("address") or "").lower(): t for t in onchain._torch_liste() or []}
                    for a in fehlt:
                        t = torch.get(a.lower())
                        if t and t.get("priceUsd"):
                            aus[(chain, a.lower())] = {"preis": t.get("priceUsd"), "mc": t.get("marketCapUsd"),
                                                       "ch24": t.get("change24hPercent")}
                except Exception as e:
                    log.debug(f"live torch: {e}")
    return aus


def _markup_mit(markup, neu):
    m = json.loads(json.dumps(markup))
    for reihe in m.get("inline_keyboard") or []:
        for k in reihe:
            if str(k.get("callback_data") or "").startswith("lp:"):
                k["text"] = neu
    return m


def _aktualisieren(eintraege, alle=None):
    """Knoepfe neu schreiben. Gibt die Eintraege zurueck, die weiter leben."""
    import agent
    preise = _preise(eintraege)
    bleiben = []
    for x in eintraege:
        d = preise.get((x["chain"], x["address"].lower()))
        if not d or not d.get("preis"):
            bleiben.append(x)
            continue
        try:
            p = float(d["preis"])
        except Exception:
            bleiben.append(x)
            continue
        if not x.get("p0"):
            x["p0"] = p
            neu = text(p, d.get("mc"), d.get("ch24"))
        else:
            neu = text(p, d.get("mc"), (p / x["p0"] - 1) * 100, seit_scan=True)
        if neu == x.get("txt"):
            bleiben.append(x)
            continue
        r = agent._tg("editMessageReplyMarkup", chat_id=x["chat"], message_id=x["mid"],
                      reply_markup=_markup_mit(x["markup"], neu))
        if r and r.get("ok"):
            x["txt"] = neu
            bleiben.append(x)
        else:
            beschr = str((r or {}).get("description") or "")
            if "not modified" in beschr:
                x["txt"] = neu
                bleiben.append(x)
            elif any(w in beschr for w in ("not found", "can't be edited", "chat not found", "blocked", "kicked")):
                continue   # Nachricht weg oder Chat zu: nicht mehr pflegen
            else:
                bleiben.append(x)
        time.sleep(0.05)
    return bleiben


def runde():
    if not an():
        return 0
    with _LOCK:
        liste = _laden()
    grenze = time.time() - _zahl("LIVE_CARD_H", 24) * 3600
    aktiv = [x for x in liste if x.get("ts", 0) >= grenze]
    if not aktiv:
        if liste:
            with _LOCK:
                _speichern([])
        return 0
    neu = _aktualisieren(aktiv)
    with _LOCK:
        # Was waehrend der Runde dazukam, behalten
        jetzt = _laden()
        bekannt = {(x["chat"], x["mid"]) for x in aktiv}
        dazu = [x for x in jetzt if (x["chat"], x["mid"]) not in bekannt]
        _speichern(neu + dazu)
    return len(neu)


def tippen(chat, mid):
    """Knopf gedrueckt: diese eine Karte sofort aktualisieren."""
    with _LOCK:
        liste = _laden()
    x = next((e for e in liste if e["chat"] == chat and e["mid"] == mid), None)
    if not x:
        return
    x["txt"] = None
    neu = _aktualisieren([x])
    with _LOCK:
        jetzt = [e for e in _laden() if not (e["chat"] == chat and e["mid"] == mid)]
        _speichern(jetzt + neu)


def schleife():
    time.sleep(30)
    while True:
        try:
            runde()
        except Exception as e:
            log.warning(f"live: {e}")
        time.sleep(max(20, _zahl("LIVE_CARD_SEC", 60)))
