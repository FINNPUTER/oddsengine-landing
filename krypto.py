"""
krypto.py: die Engine liest Krypto. Seit 2.27.

Polymarket hat Up/Down-Maerkte fuer BTC, ETH, SOL, XRP, DOGE, BNB und HYPE in
Fenstern von 5 Minuten, 15 Minuten und 4 Stunden. Seit 2026 entscheidet nicht
mehr der Preis am Ende, sondern der Durchschnitt ueber das ganze Fenster
(Chainlink TWAP): Up, wenn der Durchschnitt >= Startpreis.

Das macht den Ausgang mitten im Fenster gut rechenbar:

    Endschnitt = f * A + (1 - f) * Mittel(rest)
    f  = vergangener Anteil, A = Schnitt bisher, P = Preis jetzt
    Mittel(rest) ~ Normal(P, P * sigma * sqrt(tau / 3))      (Brownsche Bewegung)
    P(Up) = Phi((f*A + (1-f)*P - S0) / sd)

Seit 2.27.2 (Engine v2): Startpreis und Schnitt wie bei Chainlink (60-s-TWAP-Stream,
also Trapez-Gewichte statt flach), Basis Hyperliquid/Chainlink realistisch, und die
Engine-Zahl wird an den Markt gekoppelt: P = Markt + k * (Modell - Markt), k je
Zeitfenster gelernt aus den abgerechneten Fenstern. engine und late nur noch dort,
wo sie tragen (CRYPTO_ENGINE_TFS, CRYPTO_LATE_TFS), late nur, wenn auch der Markt
den Favoriten sieht. cheap ersetzt durch sniper (strenger lag), both heisst Gabagool.

Fuenf Module laufen als Papierhandel mit, jedes mit eigener oeffentlicher Bilanz.
Gekauft wird immer zum echten Ask aus dem Orderbuch, die Taker-Gebuehr
(0.07 * p * (1 - p) je Anteil) geht ab. Es wird NICHTS echt gehandelt.

    lag      starke Bewegung an der Boerse, Orderbuch hinkt hinterher (alter crypto_lag v3)
    engine   Modell gegen Markt: Einstieg, wenn Modell - Ask - Gebuehr >= EDGE
    both     beide Seiten, wenn Ask(Up) + Ask(Down) + Gebuehren < 1
    sniper   wie lag, aber strenger, nur 15 min und 4 h (ersetzt cheap seit 2.27.2)
    late     Favorit kurz vor Schluss, Modell >= 90 %, Ask <= 95 Cent

Dazu die Kalibrierung: an festen Punkten im Fenster (25, 50, 75, 90 %) wird die
Engine-Wahrscheinlichkeit und der Marktpreis festgehalten und nach dem Ausgang
gewertet. So sieht jeder, ob die Engine besser liegt als der Markt.

Laeuft als eigener Railway-Dienst `crypto` (Procfile: crypto: python krypto_main.py),
damit der Wetter-Worker unberuehrt bleibt. Speicher: kv-Tabelle (Postgres).

    krypto_live    Momentaufnahme aller Maerkte, alle paar Sekunden
    krypto_open    offene Fenster mit Einstiegen und Kalibrierpunkten
    krypto_book    Bilanzen, Kalibrierung, Tage, letzte Trades

Seit 2.66: Gold (XAU) und Silber (XAG). Polymarket hat seit 29.10.2026 Up/Down-Maerkte auf Gold und
Silber (15 Minuten, dazu stuendlich), Slug wie bei Krypto: xau-updown-15m-<start>. Abrechnung ueber
den Chainlink-Stream XAU/USD bzw. XAG/USD. Unsere Preisquelle ist Pyth (Hermes, frei, dieselben
Forex-Preise), Kerzen aus den Pyth-Benchmarks. Metalle laufen erst NUR INTERN: Live-Lesung,
Modell und Lernlabor (eigene Modelle "metal:15m", "metal:1h"), aber keine Papier-Module, keine
Kalibrierung in der oeffentlichen Bilanz und nichts im Terminal, bis der Schalter "metals" an ist.
Ob ein Fenster ueber den Schnitt oder den Endpreis entscheidet, liest die Engine aus den
Marktregeln (Wort "average"/"TWAP"), bei Metallen ohne Angabe: Endpreis.
"""
import bisect
import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 (OddsEngine crypto)", "Accept": "application/json"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HL = "https://api.hyperliquid.xyz/info"

ASSETS = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP",
          "doge": "DOGE", "bnb": "BNB", "hype": "HYPE", "xau": "XAU", "xag": "XAG"}
NAMES = {"btc": "Bitcoin", "eth": "Ethereum", "sol": "Solana", "xrp": "XRP",
         "doge": "Dogecoin", "bnb": "BNB", "hype": "Hyperliquid", "xau": "Gold", "xag": "Silver"}
TFS = {"5m": 300, "15m": 900, "4h": 14400}
METALLE = ("xau", "xag")
TFS_METALL = {"15m": 900, "1h": 3600}
PYTH = "https://hermes.pyth.network"
PYTH_BENCH = "https://benchmarks.pyth.network/v1/shims/tradingview/history"
PYTH_SYM = {"xau": "Metal.XAU/USD", "xag": "Metal.XAG/USD"}
_PYTH_ID = {}


def tfs(a):
    """Zeitfenster je Wert: Krypto 5m/15m/4h, Metalle 15m und 1h."""
    return TFS_METALL if a in METALLE else TFS


def dauer_von(tf):
    return TFS.get(tf) or TFS_METALL.get(tf)


def metalle_oeffentlich():
    try:
        import features
        return features.an("metals")
    except Exception:
        return False

FEE_RATE = float(os.getenv("CRYPTO_FEE_RATE", "0.07"))
EDGE = float(os.getenv("CRYPTO_EDGE", "0.05"))            # engine: Mindestvorsprung je Anteil
SHARES = 10.0                                              # Papiergroesse je Einstieg
BASIS = float(os.getenv("CRYPTO_BASIS", "0.0003"))         # Abweichung Referenz zu Chainlink
MODEL_V = "2.27.2"                                          # Engine v2, fuer das interne Aenderungslog
K_DEFAULT = float(os.getenv("CRYPTO_K", "0.35"))           # Kopplung an den Markt, bis gelernt
K_MIN_N = int(os.getenv("CRYPTO_K_MIN_N", "150"))
# Seit 2.82.2: Engine-Modul auf 5 Minuten. Laborbericht (Walk-forward, nur ungesehene Tage): die Engine-Zahl
# bringt auf 5m mit mindestens 5 Cent Vorsprung Plus, auf 15m deutlich Minus. Ask hoechstens 75 Cent wie im Labor.
ENGINE_TFS = [x.strip() for x in os.getenv("CRYPTO_ENGINE_TFS", "5m").split(",") if x.strip()]
ENGINE_MAX_ASK = float(os.getenv("CRYPTO_ENGINE_MAX_ASK", "0.75"))
# Seit 2.82.2: Gold/Silber "billige Seite" als reine Schattenspur (intern). Im Labor gewann die Seite unter
# 35 Cent auf 15m oefter, als ihr Preis sagt. Je Fenster hoechstens ein Papierkauf, zum echten Ask mit Gebuehr.
MCHEAP_MIN = float(os.getenv("METAL_CHEAP_MIN", "0.10"))
MCHEAP_MAX = float(os.getenv("METAL_CHEAP_MAX", "0.35"))
MCHEAP_F = (0.25, 0.75)
LATE_TFS = [x.strip() for x in os.getenv("CRYPTO_LATE_TFS", "15m,4h").split(",") if x.strip()]
CHECKS = (0.25, 0.5, 0.75, 0.9)
LATE = {"5m": 60, "15m": 120, "1h": 300, "4h": 900}                   # Sekunden vor Schluss fuer "late"
MODULES = ("engine", "lag", "sniper", "both", "leg", "late")
# Gabagool 2 (seit 2.81.3): zeitversetzt. Erst eine Seite in Richtung der letzten Minute, die Gegenseite
# erst, wenn beide zusammen mit Gebuehren hoechstens LEG_CAP kosten. Kommt die Gegenseite nicht, bleibt
# die erste Seite offen und zaehlt voll (gewonnen oder verloren). Nur Papier.
LEG_CAP = float(os.getenv("CRYPTO_LEG_CAP", "0.97"))
LEG1_MIN = float(os.getenv("CRYPTO_LEG1_MIN", "0.40"))
LEG1_MAX = float(os.getenv("CRYPTO_LEG1_MAX", "0.60"))
LEG1_F = (0.10, 0.50)
# Gabagool 3 (seit 2.82.3, nur Schatten): erste Seite wie Gabagool 2. Gegenseite kommt entweder billig
# (Paar <= LEG_CAP, Gewinn fest) oder als Stop-Hedge, sobald beide zusammen mit Gebuehren die Stufe
# erreichen (kleiner fester Verlust statt Totalverlust). Jede Stufe wird getrennt gemessen.
G3_STOPS = sorted(float(x) for x in os.getenv("CRYPTO_G3_STOPS", "1.08,1.12,1.18").split(",") if x.strip())                                      # Anteil des Fensters fuer den ersten Kauf
MODULE_INFO = {
    "engine": ("Engine edge", "Buys the side the engine rates at least 5 cents higher than the "
               "ask after fees. Once per window."),
    "both":   ("Gabagool", "Buys Up and Down together when both asks plus fees cost less "
               "than one dollar. One of the two sides pays one dollar."),
    "leg":    ("Gabagool 2", "Buys one side first, in the direction of the last minute, then the "
               "other side once both together cost 97 cents or less after fees. Pair complete: "
               "profit locked. Second side never cheap enough: the first side rides alone."),
    "sniper": ("Lag sniper", "Same idea as Crypto lag, only stricter: a bigger move, confirmed "
               "over five minutes, and the book at least 4 cents behind. 15 minute and 4 hour "
               "windows only."),
    "cheap":  ("Cheap side", "Buys the side that trades at 10 cents or less. Small stake, "
               "big payout when it lands."),
    "lag":    ("Crypto lag", "The coin moves hard on the exchange, the Polymarket price has not "
               "caught up yet. Buys the side of the move while the book still lags. Once per window."),
    "late":   ("Late favourite", "In the last stretch of the window, buys the side the engine "
               "rates 90 % or higher when the ask is 95 cents or less."),
}

_s = requests.Session()
_s.headers.update(UA)

# Speicher im Prozess
PREISE = {a: deque(maxlen=4000) for a in ASSETS}            # (ts, preis) alle paar Sekunden
_MKT = {}       # slug -> {"up": token, "down": token, "cid": .., "ok": bool, "ts": abruf}
_OFFEN = {}     # slug -> Fensterzustand (siehe _fenster)
_BUCH = None    # Bilanz, aus kv geladen


# ── kleine Helfer ──────────────────────────────────────────────────────

def gebuehr(p):
    """Taker-Gebuehr je Anteil zum Preis p."""
    p = max(0.0, min(1.0, float(p)))
    return FEE_RATE * p * (1 - p)


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _j(url, **kw):
    r = _s.get(url, timeout=kw.pop("timeout", 8), **kw)
    r.raise_for_status()
    return r.json()


def _jetzt():
    return time.time()


def _tag(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ── Preise (Hyperliquid, ein Aufruf fuer alle) ─────────────────────────

def preise_holen():
    """Mittelkurse aller sieben Coins. Hyperliquid liefert alle in einem Aufruf."""
    try:
        r = _s.post(HL, json={"type": "allMids"}, timeout=6)
        r.raise_for_status()
        mids = r.json()
    except Exception as e:
        log.debug(f"krypto: allMids {e}")
        return {}
    out = {}
    for a, coin in ASSETS.items():
        if a in METALLE:
            continue
        try:
            v = float(mids.get(coin))
            if v > 0:
                out[a] = v
        except Exception:
            pass
    out.update(metall_preise())
    return out


PYTH_KEY_HOST = "https://pyth.dourolabs.app/hermes"
HL_METALL = {"xau": "xyz:GOLD", "xag": "xyz:SILVER"}


def _pyth_basis():
    """Seit 2.67.0: Pyth Hermes verlangt seit 26.08.2026 einen API-Key (sonst 401). Mit PYTH_API_KEY ueber den
    Key-Host, ohne Key wird Pyth gar nicht erst gefragt und Hyperliquid (HIP-3 xyz:GOLD/xyz:SILVER) liefert."""
    k = os.getenv("PYTH_API_KEY", "").strip()
    return (PYTH_KEY_HOST, {"Authorization": f"Bearer {k}"}) if k else (None, None)


def _pyth_ids():
    """Feed-IDs von Pyth (Metal.XAU/USD, Metal.XAG/USD). Env PYTH_XAU_ID / PYTH_XAG_ID geht vor."""
    basis, kopf = _pyth_basis()
    for a in METALLE:
        if a in _PYTH_ID:
            continue
        env = os.getenv(f"PYTH_{a.upper()}_ID", "").strip()
        if env:
            _PYTH_ID[a] = env.lower().replace("0x", "")
            continue
        if not basis:
            continue
        try:
            for f in _j(f"{basis}/v2/price_feeds", params={"query": a.upper(), "asset_type": "metal"}, headers=kopf):
                sym = ((f.get("attributes") or {}).get("symbol") or "")
                if sym.upper() == PYTH_SYM[a].upper():
                    _PYTH_ID[a] = str(f["id"]).lower().replace("0x", "")
                    break
        except Exception as e:
            log.debug(f"krypto: pyth ids {e}")
    return _PYTH_ID


_METALL_STAND = {}


def _metall_pyth():
    basis, kopf = _pyth_basis()
    ids = _pyth_ids() if basis else {}
    if not ids:
        return {}
    try:
        q = "&".join(f"ids[]={i}" for i in ids.values())
        d = _j(f"{basis}/v2/updates/price/latest?{q}&parsed=true", timeout=6, headers=kopf)
    except Exception as e:
        log.debug(f"krypto: pyth preise {e}")
        return {}
    rueck = {v: k for k, v in ids.items()}
    out = {}
    for p in d.get("parsed") or []:
        a = rueck.get(str(p.get("id") or "").lower().replace("0x", ""))
        pr = p.get("price") or {}
        try:
            v = float(pr["price"]) * (10 ** int(pr["expo"]))
            alt = time.time() - float(pr.get("publish_time") or 0)
        except Exception:
            continue
        _METALL_STAND[a] = {"price": v, "age": alt, "src": "pyth"}
        if a and v > 0 and alt < 90:
            out[a] = v
    return out


EXTRA = {"eur": deque(maxlen=4000), "xau_mark": deque(maxlen=4000), "xag_mark": deque(maxlen=4000)}  # seit 2.67.1 fuers Labor


def extra_rendite(name, sek, jetzt=None):
    """Log-Rendite einer Zusatzreihe (EUR/USD, Futures-Mark von Gold/Silber) ueber sek Sekunden, None ohne Daten."""
    jetzt = jetzt or _jetzt()
    pts = EXTRA.get(name) or ()
    if len(pts) < 3:
        return None
    alt = [p for t, p in pts if t <= jetzt - sek]
    if not alt or jetzt - pts[0][0] < sek * 0.8:
        return None
    try:
        return math.log(pts[-1][1] / alt[-1])
    except Exception:
        return None


def _metall_hl():
    """Hyperliquid HIP-3 (dex xyz): Orakelpreis von Gold und Silber, alle paar Sekunden neu, frei und ohne Key.
    Seit 2.67.1 auch EUR/USD (Dollar-Bewegung) und der gehandelte Futures-Preis (Mark) fuers Labor."""
    try:
        r = _s.post(HL, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=6)
        r.raise_for_status()
        meta, ctx = r.json()
    except Exception as e:
        log.debug(f"krypto: hl metalle {e}")
        return {}
    rueck = {v: k for k, v in HL_METALL.items()}
    out = {}
    jetzt = time.time()
    for u, c in zip(meta.get("universe") or [], ctx or []):
        a = rueck.get(u.get("name"))
        try:
            if u.get("name") == "xyz:EUR" and float(c.get("oraclePx") or 0) > 0:
                EXTRA["eur"].append((jetzt, float(c["oraclePx"])))
            if a and float(c.get("markPx") or 0) > 0:
                EXTRA[a + "_mark"].append((jetzt, float(c["markPx"])))
        except Exception:
            pass
        if not a:
            continue
        try:
            v = float(c.get("oraclePx") or c.get("markPx"))
        except Exception:
            continue
        if v > 0:
            _METALL_STAND[a] = {"price": v, "age": 0, "src": "hyperliquid"}
            out[a] = v
    return out


def metall_preise():
    """Gold und Silber. Pyth mit Key (PYTH_API_KEY), sonst oder bei Ausfall Hyperliquid xyz. Nur frische Preise."""
    hl = _metall_hl()   # immer: liefert auch EUR/USD und den Futures-Mark fuers Labor
    out = _metall_pyth()
    if len(out) < len(METALLE):
        for a in METALLE:
            if a not in out and a in hl:
                out[a] = hl[a]
    return out


def kerzen(a, start_ms, ende_ms):
    """1-Minuten-Kerzen fuer den Rueckblick nach einem Neustart. Metalle seit 2.67.0 von Hyperliquid xyz
    (die Pyth-Benchmarks antworten ohne Key nicht mehr)."""
    coin = HL_METALL.get(a) or ASSETS[a]
    try:
        r = _s.post(HL, json={"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1m",
            "startTime": int(start_ms), "endTime": int(ende_ms)}}, timeout=8)
        r.raise_for_status()
        return [(int(k["t"]) / 1000.0, float(k["o"]), float(k["c"])) for k in r.json()]
    except Exception as e:
        log.debug(f"krypto: kerzen {a} {e}")
        return []


# Crypto lag (aus dem alten Bot, crypto_lag v3): starke Bewegung ueber 60 s,
# bestaetigt durch 5 Minuten, deutlich ueber dem Rauschen, und das Orderbuch
# hinkt noch hinterher.
LAG_MOVE_60 = {"5m": 0.0012, "15m": 0.0008, "4h": 0.0008}
LAG_MOVE_300 = 0.0010
LAG_Z = {"5m": 2.0, "15m": 1.5, "4h": 1.5}
LAG_EDGE = float(os.getenv("CRYPTO_LAG_EDGE", "0.02"))


def rendite(a, sek, jetzt=None):
    """Log-Rendite der letzten sek Sekunden, None ohne Daten."""
    jetzt = jetzt or _jetzt()
    pts = PREISE[a]
    if len(pts) < 3:
        return None
    alt = [p for p in pts if p[0] <= jetzt - sek]
    if not alt or jetzt - sek - alt[-1][0] > 20:
        return None
    return math.log(pts[-1][1] / alt[-1][1])


def sigma(a, fenster_s=1800):
    """Schwankung je Sekunde (Log-Rendite) aus den letzten 30 Minuten."""
    pts = [p for p in PREISE[a] if p[0] >= _jetzt() - fenster_s]
    if len(pts) < 20:
        return {"btc": 2.2e-4, "eth": 2.8e-4, "xau": 4e-5, "xag": 8e-5}.get(a, 3.5e-4)
    rs, dts = [], []
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0 or dt > 60:
            continue
        rs.append(math.log(p1 / p0) / math.sqrt(dt))
    if len(rs) < 10:
        return {"xau": 4e-5, "xag": 8e-5}.get(a, 3e-4)
    m = sum(rs) / len(rs)
    v = sum((r - m) ** 2 for r in rs) / (len(rs) - 1)
    return max(math.sqrt(v), 5e-6 if a in METALLE else 2e-5)


# ── Maerkte und Orderbuecher ───────────────────────────────────────────

def slug_von(a, tf, start):
    return f"{a}-updown-{tf}-{int(start)}"


def markt(slug):
    """Token-IDs eines Fensters. Einmal je Fenster, Fehlschlag 60 s gemerkt."""
    c = _MKT.get(slug)
    if c and (c["ok"] or _jetzt() - c["ts"] < 60):
        return c if c["ok"] else None
    try:
        ev = _j(f"{GAMMA}/events", params={"slug": slug})
        m = ev[0]["markets"][0]
        toks = json.loads(m["clobTokenIds"])
        outs = [o.lower() for o in json.loads(m.get("outcomes") or '["Up","Down"]')]
        up = toks[outs.index("up")] if "up" in outs else toks[0]
        dn = toks[outs.index("down")] if "down" in outs else toks[1]
        regel = f"{m.get('description') or ''} {ev[0].get('description') or ''}".lower()
        schnitt = ("average" in regel or "twap" in regel) if regel.strip() else None
        c = {"up": up, "down": dn, "cid": m.get("conditionId"), "ok": True, "ts": _jetzt(),
             "title": ev[0].get("title") or "", "avg": schnitt}
    except Exception as e:
        log.debug(f"krypto: markt {slug} {e}")
        c = {"ok": False, "ts": _jetzt()}
    _MKT[slug] = c
    if len(_MKT) > 600:
        for k in sorted(_MKT, key=lambda k: _MKT[k]["ts"])[:200]:
            _MKT.pop(k, None)
    return c if c["ok"] else None


def buecher(tokens):
    """Bestes Bid/Ask mit Menge fuer viele Tokens in einem Aufruf."""
    out = {}
    if not tokens:
        return out
    try:
        r = _s.post(f"{CLOB}/books", json=[{"token_id": t} for t in tokens], timeout=8)
        r.raise_for_status()
        for b in r.json():
            bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids") or []]
            asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks") or []]
            bb = max(bids, default=(None, 0))
            ba = min(asks, default=(None, 0))
            out[str(b.get("asset_id"))] = {"bid": bb[0], "bid_size": bb[1],
                                           "ask": ba[0], "ask_size": ba[1]}
    except Exception as e:
        log.debug(f"krypto: books {e}")
    return out


# ── Modell ──────────────────────────────────────────────────────────────

def _gewicht(u, S, E):
    """Gewicht eines Zeitpunkts im Chainlink-Schnitt: Mittel des 60-s-TWAP-Streams
    ueber [S, E] = Preis ueber [S-60, E] mit Rampe am Anfang und am Ende."""
    if u < S - 60 or u > E:
        return 0.0
    if u < S:
        return (u - (S - 60)) / 60.0
    if u > E - 60:
        return (E - u) / 60.0
    return 1.0


def _preis_bei(ts, ps, u):
    i = bisect.bisect_left(ts, u)
    if i <= 0:
        return ps[0]
    if i >= len(ts):
        return ps[-1]
    t0, t1 = ts[i - 1], ts[i]
    if t1 <= t0:
        return ps[i]
    return ps[i - 1] + (ps[i] - ps[i - 1]) * (u - t0) / (t1 - t0)


def modell(a, start, dauer, s0, jetzt=None):
    """P(Up) fuer ein Fenster (roh, ohne Markt). None, wenn Daten fehlen."""
    jetzt = jetzt or _jetzt()
    S, E = start, start + dauer
    now = min(max(jetzt, S), E)
    pts = [p for p in PREISE[a] if S - 75 <= p[0] <= now]
    if s0 is None or not pts:
        return None
    ts = [p[0] for p in pts]
    ps = [p[1] for p in pts]
    p_now = ps[-1]
    # bekannter Teil
    t0 = S - 60
    n1 = max(1, int((now - t0) / max(1.0, (now - t0) / 600)))
    h1 = (now - t0) / n1 if now > t0 else 0.0
    bek, wk = 0.0, 0.0
    for i in range(n1):
        u = t0 + (i + 0.5) * h1
        g = _gewicht(u, S, E)
        if g:
            bek += g * _preis_bei(ts, ps, u) * h1
            wk += g * h1
    # offener Teil: Erwartung p_now, Varianz ueber W(s) = Gewicht ab s bis Ende
    tau = E - now
    wf, var_int = 0.0, 0.0
    if tau > 0:
        n2 = max(1, int(tau / max(1.0, tau / 600)))
        h2 = tau / n2
        gs = [_gewicht(now + (i + 0.5) * h2, S, E) for i in range(n2)]
        W = 0.0
        for g in reversed(gs):
            W += g * h2
            var_int += W * W * h2
        wf = W
    D = wk + wf
    if D <= 0:
        return None
    mu = (bek + p_now * wf) / D
    sd_rest = p_now * sigma(a) * math.sqrt(var_int) / D
    sd = math.sqrt(sd_rest ** 2 + (p_now * BASIS) ** 2)
    avg = bek / wk if wk > 0 else p_now
    pu = _phi((mu - s0) / sd) if sd > 0 else (1.0 if mu >= s0 else 0.0)
    return {"p_up": max(0.003, min(0.997, pu)), "avg": avg, "now": p_now,
            "f": (now - S) / dauer, "mu": mu, "sd": sd}


def modell_ende(a, start, dauer, s0, jetzt=None):
    """P(Up), wenn der Preis am Fensterende (Stream-Wert, kaum geglaettet) gegen den Startpreis zaehlt."""
    jetzt = jetzt or _jetzt()
    S, E = start, start + dauer
    pts = [p for p in PREISE[a] if p[0] <= min(max(jetzt, S), E)]
    if s0 is None or not pts:
        return None
    p_now = pts[-1][1]
    tau = max(0.0, E - jetzt)
    sd = math.sqrt((p_now * sigma(a) * math.sqrt(max(tau, 1.0))) ** 2 + (p_now * BASIS_M) ** 2)
    seit = [p[1] for p in pts if p[0] >= S]
    avg = sum(seit) / len(seit) if seit else p_now
    pu = _phi((p_now - s0) / sd) if sd > 0 else (1.0 if p_now >= s0 else 0.0)
    return {"p_up": max(0.003, min(0.997, pu)), "avg": avg, "now": p_now,
            "f": (min(max(jetzt, S), E) - S) / dauer, "mu": p_now, "sd": sd}


BASIS_M = float(os.getenv("METAL_BASIS", "0.00008"))     # Pyth gegen Chainlink bei Gold/Silber


_K = {"ts": 0.0, "k": {}}


def k_wert(tf):
    """Kopplung an den Markt je Zeitfenster, gelernt aus den abgerechneten Fenstern:
    das k aus 0..1, mit dem Markt + k * (Modell - Markt) den kleinsten Brier hat."""
    if _jetzt() - _K["ts"] > 600:
        neu = {}
        for t, rows in (buch().get("fit") or {}).items():
            if len(rows) < K_MIN_N:
                continue
            best = None
            for i in range(21):
                k = i / 20
                br = sum((m + k * (r - m) - y) ** 2 for r, m, y in rows) / len(rows)
                if best is None or br < best[1]:
                    best = (k, br)
            neu[t] = best[0]
        _K["k"], _K["ts"] = neu, _jetzt()
    return _K["k"].get(tf, K_DEFAULT)


def gekoppelt(roh, mid, tf):
    if mid is None:
        return roh
    return max(0.003, min(0.997, mid + k_wert(tf) * (roh - mid)))


# ── Buchfuehrung ────────────────────────────────────────────────────────

def _leer_buch():
    return {"stats": {}, "days": {}, "calib": {}, "trades": [], "windows": 0,
            "resolved": 0, "since": _jetzt(), "fit": {}, "changes": []}


def _aenderung_vermerken():
    """Internes Aenderungslog: wann welche Engine-Version live ging (nicht oeffentlich)."""
    b = buch()
    ch = b.setdefault("changes", [])
    if not any(c.get("v") == MODEL_V for c in ch):
        ch.append({"v": MODEL_V, "ts": round(_jetzt()), "windows": b.get("windows", 0),
                   "resolved": b.get("resolved", 0),
                   "note": "Engine v2: Chainlink-TWAP-Gewichte, Startpreis 60-s-Schnitt, "
                           "Basis 3 bp, Kopplung an den Markt, engine/late nur "
                           + ",".join(ENGINE_TFS) + ", late nur mit Markt, cheap -> sniper, "
                           "both -> Gabagool"})
    if not any(c.get("v") == "2.82.2-eng5" for c in ch):
        ch.append({"v": "2.82.2-eng5", "ts": round(_jetzt()), "windows": b.get("windows", 0),
                   "resolved": b.get("resolved", 0),
                   "note": "Engine-Modul auf " + ",".join(ENGINE_TFS) + ", Ask bis " + str(ENGINE_MAX_ASK)
                           + " (Laborbericht: 5m Plus, 15m Minus); Metalle billige Seite als Schattenspur"})
    if not any(c.get("v") == "2.82.3-g3" for c in ch):
        ch.append({"v": "2.82.3-g3", "ts": round(_jetzt()), "windows": b.get("windows", 0),
                   "resolved": b.get("resolved", 0),
                   "note": "Gabagool 3 als Schatten: Stop-Hedge-Stufen " + ",".join(f"{x:.2f}" for x in G3_STOPS)
                           + "; Gabagool 2 je Fenster gezaehlt"})
    if not any(c.get("v") == "2.81.3-leg" for c in ch):
        ch.append({"v": "2.81.3-leg", "ts": round(_jetzt()), "windows": b.get("windows", 0),
                   "resolved": b.get("resolved", 0), "note": "Gabagool 2 (leg) als Papiermodul dazu"})
        log.info(f"krypto: Engine {MODEL_V} live, im Aenderungslog vermerkt")


def _paare(p):
    """Gabagool 2 je Fenster: wie oft das Paar komplett wurde und was es brachte."""
    if not p or not p.get("windows"):
        return p
    q = dict(p)
    q["open"] = p["windows"] - p.get("locked", 0)
    q["locked_rate"] = round(p.get("locked", 0) / p["windows"], 4)
    q["pnl"] = round(p.get("locked_pnl", 0) + p.get("open_pnl", 0), 2)
    q["pnl_per_window"] = round(q["pnl"] / p["windows"], 4)
    return q


def g3_bericht(b=None):
    """Gabagool 3 Schattenbuecher mit ROI je Stufe und Zeitfenster."""
    b = b or buch()
    aus = {}
    for key, v in (b.get("g3") or {}).items():
        v = dict(v)
        v["roi"] = round(v["pnl"] / v["cost"], 4) if v.get("cost") else None
        v["pnl_per_window"] = round(v["pnl"] / v["windows"], 4) if v.get("windows") else None
        aus[key] = v
    return aus


def buch():
    global _BUCH
    if _BUCH is None:
        try:
            roh = storage.kv_get("krypto_book")
            _BUCH = json.loads(roh) if roh else _leer_buch()
        except Exception:
            _BUCH = _leer_buch()
        try:
            roh = storage.kv_get("krypto_open")
            for k, v in (json.loads(roh) if roh else {}).items():
                _OFFEN.setdefault(k, v)
        except Exception:
            pass
    return _BUCH


# ── Seit 2.82.9: Engine-Picks auf 5m live in den Holder-Kanal ─────────────────
# Schalter 'crypto_picks'. Braucht im crypto-Dienst TELEGRAM_BOT_TOKEN und TELEGRAM_PAID_CHANNEL_ID.
# CRYPTO_PICKS_TFS (Standard 5m) und CRYPTO_PICKS_SKIP (z. B. "eth") steuern, was rausgeht.
PICK_TFS = [x.strip() for x in os.getenv("CRYPTO_PICKS_TFS", "5m").split(",") if x.strip()]
PICK_SKIP = {x.strip().lower() for x in os.getenv("CRYPTO_PICKS_SKIP", "").split(",") if x.strip()}


def _picks_an():
    try:
        import features
        return features.an("crypto_picks")
    except Exception:
        return False


def _tg(text, buttons=None, reply_to=None):
    """Direkt in den Holder-Kanal. -> message_id oder None"""
    import requests as _rq
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_PAID_CHANNEL_ID", "").strip()
    if not (tok and chat):
        log.warning("crypto picks: TELEGRAM_BOT_TOKEN or TELEGRAM_PAID_CHANNEL_ID missing in this service")
        return None
    text = text.replace("\u2014", ",").replace("\u2013", ",")
    d = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if buttons:
        d["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]}
    if reply_to:
        d["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
    try:
        r = _rq.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=d, timeout=15)
        if r.status_code != 200:
            log.warning(f"crypto picks telegram {r.status_code}: {r.text[:160]}")
            return None
        return r.json()["result"]["message_id"]
    except Exception as e:
        log.warning(f"crypto picks telegram: {e}")
        return None


def _pick_senden(w, seite, ask):
    if not _picks_an() or w["tf"] not in PICK_TFS or w["a"] in PICK_SKIP or w.get("pick"):
        return
    rest = max(0, int(w["start"] + w["dauer"] - _jetzt()))
    mins = max(1, round(rest / 60))
    txt = (f"Engine pick: {NAMES.get(w['a'], w['a'].upper())} {w['tf'].replace('m', ' min')}\n\n"
           f"{'UP' if seite == 'up' else 'DOWN'} at {round(ask * 100)}c\n"
           f"Window closes in about {mins} min\n\n"
           f"Most picks are cheap and miss, the hits pay many times the price. Small size. Not financial advice.")
    w["pick"] = {"side": seite, "price": round(ask, 4), "msg": None}
    url = f"https://polymarket.com/event/{w['slug']}"

    def _los():
        w["pick"]["msg"] = _tg(txt, [("Open on Polymarket", url)])
    import threading
    threading.Thread(target=_los, daemon=True).start()


def _pick_ergebnis(w, sieger):
    pk = w.get("pick")
    if not pk or not _picks_an():
        return
    name = f"{NAMES.get(w['a'], w['a'].upper())} {w['tf'].replace('m', ' min')}"
    if pk["side"] == sieger:
        x = 1 / pk["price"] if pk["price"] else 0
        txt = f"Hit. {name} {pk['side'].upper()} bought at {round(pk['price'] * 100)}c paid {x:.1f}x."
    else:
        txt = f"Miss. {name} {pk['side'].upper()} at {round(pk['price'] * 100)}c."
    import threading
    threading.Thread(target=lambda: _tg(txt, reply_to=pk.get("msg")), daemon=True).start()


def _einstieg(w, modul, seite, ask, p_modell=None, groesse=SHARES):
    """Papier-Einstieg, einmal je Modul und Fenster (both: zwei Seiten)."""
    w["trades"].append({"m": modul, "side": seite, "price": round(ask, 4), "v": MODEL_V,
                        "shares": groesse, "fee": round(gebuehr(ask) * groesse, 5),
                        "p": None if p_modell is None else round(p_modell, 4),
                        "t": round(_jetzt() - w["start"])})
    w["done"].append(modul)


def _werten(w, sieger, quelle):
    """Fenster abschliessen: Trades und Kalibrierung ins Buch."""
    b = buch()
    b["resolved"] = b.get("resolved", 0) + 1
    mc = w.get("mcheap")
    if mc:
        kosten = mc["price"] + mc["fee"]
        ertrag = 1.0 if mc["side"] == sieger else 0.0
        for key in (f"{w['tf']}|{w['a']}", f"{w['tf']}|*", "*"):
            s_ = b.setdefault("metal_cheap", {}).setdefault(key, {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0})
            s_["n"] += 1
            s_["win"] += 1 if ertrag > 0 else 0
            s_["cost"] = round(s_["cost"] + kosten, 4)
            s_["pnl"] = round(s_["pnl"] + ertrag - kosten, 4)
    try:
        _pick_ergebnis(w, sieger)
    except Exception as e:
        log.warning(f"crypto pick result: {e}")
    tag = _tag(w["start"] + w["dauer"])
    for t in w["trades"]:
        kosten = t["price"] * t["shares"] + t["fee"]
        ertrag = t["shares"] if t["side"] == sieger else 0.0
        pnl = ertrag - kosten
        keys = [f"{t['m']}|{w['tf']}|{w['a']}", f"{t['m']}|{w['tf']}|*", f"{t['m']}|*|*"]
        if t.get("v") == MODEL_V:
            keys += ["v2:" + k for k in keys]
        for key in keys:
            s = b["stats"].setdefault(key, {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0, "fee": 0.0})
            s["n"] += 1
            s["win"] += 1 if ertrag > 0 else 0
            s["cost"] = round(s["cost"] + kosten, 4)
            s["pnl"] = round(s["pnl"] + pnl, 4)
            s["fee"] = round(s["fee"] + t["fee"], 4)
        d = b["days"].setdefault(tag, {})
        for dk in ([t["m"], "v2:" + t["m"]] if t.get("v") == MODEL_V else [t["m"]]):
            dm = d.setdefault(dk, {"n": 0, "pnl": 0.0, "cost": 0.0})
            dm["n"] += 1
            dm["pnl"] = round(dm["pnl"] + pnl, 4)
            dm["cost"] = round(dm["cost"] + kosten, 4)
        b["trades"].insert(0, {"m": t["m"], "a": w["a"], "tf": w["tf"], "side": t["side"],
                               "price": t["price"], "p": t["p"], "won": ertrag > 0,
                               "pnl": round(pnl, 3), "cost": round(kosten, 3),
                               "end": int(w["start"] + w["dauer"]), "src": quelle,
                               "v": t.get("v", "2.27.0")})
    del b["trades"][150:]
    try:
        _picks_merken(w, sieger, quelle)
    except Exception as e:
        log.debug(f"krypto picks log: {e}")
    # Gabagool 2: wie oft wurde das Paar komplett (Gewinn fest) und was brachten offene Einzelseiten
    legs = [t for t in w["trades"] if t["m"] == "leg"]
    if legs:
        kosten = sum(t["price"] * t["shares"] + t["fee"] for t in legs)
        ertrag = sum(t["shares"] for t in legs if t["side"] == sieger)
        for key in (f"leg|{w['tf']}", "leg|*"):
            s = b.setdefault("pairs", {}).setdefault(key, {"windows": 0, "locked": 0, "locked_pnl": 0.0,
                                                            "open_pnl": 0.0, "open_win": 0})
            s["windows"] += 1
            if len(legs) >= 2:
                s["locked"] += 1
                s["locked_pnl"] = round(s["locked_pnl"] + ertrag - kosten, 4)
            else:
                s["open_pnl"] = round(s["open_pnl"] + ertrag - kosten, 4)
                s["open_win"] += 1 if ertrag > 0 else 0
    # Gabagool 3: jede Stop-Stufe als eigenes Schattenbuch, gezaehlt je Fenster
    g1 = w.get("leg1")
    if g1:
        sh = SHARES
        k1 = (g1["price"] + gebuehr(g1["price"])) * sh
        for st3 in G3_STOPS:
            k3 = f"{st3:.2f}"
            z = (w.get("g3") or {}).get(k3)
            kosten = k1
            ertrag = sh if g1["side"] == sieger else 0.0
            how = "open"
            if z:
                how = z["how"]
                kosten += (z["price"] + gebuehr(z["price"])) * sh
                ertrag = sh
            pnl3 = ertrag - kosten
            for key in (f"{k3}|{w['tf']}", f"{k3}|*"):
                s3 = b.setdefault("g3", {}).setdefault(key, {
                    "windows": 0, "lock": 0, "stop": 0, "open": 0, "open_win": 0, "cost": 0.0,
                    "pnl": 0.0, "lock_pnl": 0.0, "stop_pnl": 0.0, "open_pnl": 0.0})
                s3["windows"] += 1
                s3[how] += 1
                s3["cost"] = round(s3["cost"] + kosten, 4)
                s3["pnl"] = round(s3["pnl"] + pnl3, 4)
                s3[how + "_pnl"] = round(s3[how + "_pnl"] + pnl3, 4)
                if how == "open" and ertrag > 0:
                    s3["open_win"] += 1
    for k, c in w["calib"].items():
        ck = f"{w['tf']}|{k}"
        s = b["calib"].setdefault(ck, {"n": 0, "eng_hit": 0, "mkt_hit": 0,
                                        "eng_brier": 0.0, "mkt_brier": 0.0})
        y = 1.0 if sieger == "up" else 0.0
        s["n"] += 1
        s["eng_hit"] += 1 if (c["e"] >= 0.5) == (y == 1.0) else 0
        s["mkt_hit"] += 1 if (c["m"] >= 0.5) == (y == 1.0) else 0
        s["eng_brier"] = round(s["eng_brier"] + (c["e"] - y) ** 2, 5)
        s["mkt_brier"] = round(s["mkt_brier"] + (c["m"] - y) ** 2, 5)
        if "r" in c:
            fit = b.setdefault("fit", {}).setdefault(w["tf"], [])
            fit.append([c["r"], c["m"], y])
            del fit[:-3000]
    for alt in sorted(b["days"])[:-90]:
        b["days"].pop(alt, None)


# ── Seit 2.82.22: jeder Modul-Pick einzeln in Postgres, dazu Module oeffentlich oder nur intern ──────
_PICKS_SCHEMA = [False]
_VERBORGEN = {"ts": 0.0, "v": set()}


def _pg_picks():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _PICKS_SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS krypto_picks (
            id BIGSERIAL PRIMARY KEY, modul TEXT, coin TEXT, tf TEXT, side TEXT, price NUMERIC, shares NUMERIC,
            fee NUMERIC, p_model NUMERIC, t_in INTEGER, dauer INTEGER, won BOOLEAN, pnl NUMERIC, cost NUMERIC,
            t_end TIMESTAMPTZ, v TEXT, src TEXT, created TIMESTAMPTZ DEFAULT now())""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_krypto_picks_end ON krypto_picks(t_end)")
        _PICKS_SCHEMA[0] = True
    return pg


def _picks_merken(w, sieger, quelle):
    if not w.get("trades"):
        return
    pg = _pg_picks()
    if not pg:
        return
    ende = w["start"] + w["dauer"]
    for t in w["trades"]:
        kosten = t["price"] * t["shares"] + t["fee"]
        ertrag = t["shares"] if t["side"] == sieger else 0.0
        pg.execute("""INSERT INTO krypto_picks (modul, coin, tf, side, price, shares, fee, p_model, t_in, dauer, won, pnl, cost,
                                                t_end, v, src)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s,%s)""",
                   (t["m"], w["a"], w["tf"], t["side"], t["price"], t["shares"], t["fee"], t.get("p"), t.get("t"),
                    int(w["dauer"]), ertrag > 0, round(ertrag - kosten, 4), round(kosten, 4), ende, t.get("v"), quelle))


def picks_csv(tage=30):
    """Jeder mitgeschriebene Modul-Pick als CSV (nur Admin). Gibt es erst ab 2.82.22, aeltere Picks stehen nur als Summen im Buch."""
    import csv
    import io
    pg = _pg_picks()
    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(["end_utc", "module", "coin", "tf", "side", "price", "shares", "fee", "p_engine", "sec_into_window",
                 "window_sec", "won", "pnl", "cost", "version"])
    if pg:
        for r in pg.execute("""SELECT t_end, modul, coin, tf, side, price, shares, fee, p_model, t_in, dauer, won, pnl, cost, v
                               FROM krypto_picks WHERE t_end > now() - (%s || ' days')::interval ORDER BY t_end""",
                            (str(int(tage)),)).fetchall():
            wr.writerow([r[0].isoformat(), r[1], r[2], r[3], r[4], float(r[5]), float(r[6]), float(r[7] or 0),
                         "" if r[8] is None else float(r[8]), "" if r[9] is None else int(r[9]), int(r[10] or 0),
                         1 if r[11] else 0, float(r[12]), float(r[13]), r[14] or ""])
    return out.getvalue()


def verborgen():
    """Module, die nur intern laufen (kv 'krypto_hidden'). 30 Sekunden Cache."""
    if time.time() - _VERBORGEN["ts"] > 30:
        try:
            _VERBORGEN["v"] = {m for m in json.loads(storage.kv_get("krypto_hidden") or "[]") if m in MODULES}
        except Exception:
            _VERBORGEN["v"] = set()
        _VERBORGEN["ts"] = time.time()
    return set(_VERBORGEN["v"])


def modul_sichtbar(modul, oeffentlich=True):
    if modul not in MODULES:
        return {"ok": False, "error": "unknown module"}
    v = verborgen()
    v.discard(modul) if oeffentlich else v.add(modul)
    storage.kv_set("krypto_hidden", json.dumps(sorted(v)))
    _VERBORGEN["v"], _VERBORGEN["ts"] = v, time.time()
    return {"ok": True, "hidden": sorted(v)}


def modul_bericht():
    """Admin: jedes Modul nach Zeitfenster und Coin, mit und ohne Gebuehren, dazu die letzten Tage."""
    try:
        b = json.loads(storage.kv_get("krypto_book") or "{}") or _leer_buch()
    except Exception:
        b = _leer_buch()
    st = b.get("stats", {})
    v = verborgen()

    def q(s):
        x = _quote(s)
        if x:
            x["pnl_before_fees"] = round(x["pnl"] + x["fees"], 2)
            x["avg_price"] = round((s["cost"] - s.get("fee", 0.0)) / s["n"] / SHARES, 3)
        return x
    tage = sorted(b.get("days", {}).items())
    out = []
    for m in MODULES:
        zellen = {f"{tf}|{a}": q(st.get(f"v2:{m}|{tf}|{a}")) for tf in TFS for a in ASSETS}
        verlauf = [{"day": t, "n": d["v2:" + m]["n"], "pnl": round(d["v2:" + m]["pnl"], 2)} for t, d in tage if "v2:" + m in d]
        letzte = verlauf[-7:]
        out.append({"id": m, "name": MODULE_INFO[m][0], "public": m not in v, "all": q(st.get(f"v2:{m}|*|*")),
                    "tf": {tf: q(st.get(f"v2:{m}|{tf}|*")) for tf in TFS},
                    "cells": {k: z for k, z in zellen.items() if z},
                    "days": verlauf[-21:], "last7": {"n": sum(x["n"] for x in letzte), "pnl": round(sum(x["pnl"] for x in letzte), 2),
                                                     "green": sum(1 for x in letzte if x["pnl"] > 0), "days": len(letzte)}})
    geloggt = None
    try:
        pg = _pg_picks()
        if pg:
            r = pg.execute("SELECT count(*), min(t_end) FROM krypto_picks").fetchone()
            geloggt = {"picks": int(r[0]), "since": r[1].isoformat() if r[1] else None}
    except Exception:
        pass
    return {"modules": out, "hidden": sorted(v), "pick_log": geloggt, "fee_rate": FEE_RATE}


def ausgang(slug):
    """'up' / 'down' sobald Polymarket aufgeloest hat, sonst None."""
    try:
        ev = _j(f"{GAMMA}/events", params={"slug": slug})
        m = ev[0]["markets"][0]
        preise = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        outs = [o.lower() for o in json.loads(m.get("outcomes") or '["Up","Down"]')]
        if len(preise) == 2 and max(preise) >= 0.99 and min(preise) <= 0.01:
            return outs[preise.index(max(preise))]
    except Exception as e:
        log.debug(f"krypto: ausgang {slug} {e}")
    return None


# ── ein Durchlauf ───────────────────────────────────────────────────────

def _fenster(a, tf, start):
    slug = slug_von(a, tf, start)
    w = _OFFEN.get(slug)
    if w is None:
        w = {"slug": slug, "a": a, "tf": tf, "start": start, "dauer": dauer_von(tf),
             "s0": None, "full": False, "trades": [], "done": [], "calib": {},
             "min_sum": None}
        _OFFEN[slug] = w
        buch()["windows"] = buch().get("windows", 0) + 1
    return w


def _startpreis(a, w):
    """Startpreis wie bei Chainlink: der 60-s-TWAP zum Fensterbeginn, also der
    Schnitt ueber die Minute davor. Eigene Messung, wenn wir dabei waren, sonst Kerze."""
    if w["s0"] is not None:
        return w["s0"]
    S = w["start"]
    nah = [p for p in PREISE[a] if S - 62 <= p[0] <= S + 2]
    if len(nah) >= 4 and nah[0][0] <= S - 45 and nah[-1][0] >= S - 8:
        fl, zt = 0.0, 0.0
        for (t0, p0), (t1, p1) in zip(nah, nah[1:]):
            fl += (p0 + p1) / 2 * (t1 - t0)
            zt += t1 - t0
        w["s0"] = fl / zt if zt > 0 else nah[-1][1]
        w["full"] = True
        return w["s0"]
    if _jetzt() - S > 8:
        ks = kerzen(a, (S - 60) * 1000, _jetzt() * 1000)
        if ks:
            vor = [k for k in ks if abs(k[0] - (S - 60)) < 1]
            w["s0"] = (vor[0][1] + vor[0][2]) / 2 if vor else ks[0][1]
            vorhanden = {round(p[0]) for p in PREISE[a]}
            for t, o, c in ks:
                if round(t + 59) not in vorhanden and t + 59 < _jetzt() - 5:
                    PREISE[a].append((t + 59, c))
            PREISE[a] = deque(sorted(PREISE[a]), maxlen=4000)
            w["full"] = False
    return w["s0"]


def _spark(a, start, n=60):
    pts = [round(p[1], 6) for p in PREISE[a] if p[0] >= start]
    schritt = max(1, len(pts) // n)
    out = pts[::schritt]
    if pts and out[-1] != pts[-1]:
        out.append(pts[-1])
    return out[-(n + 1):]


def _rueckblick(a, jetzt):
    """Nach einem Neustart: die letzten 4 Stunden aus Minutenkerzen nachladen."""
    ks = kerzen(a, (jetzt - TFS["4h"] - 120) * 1000, jetzt * 1000)
    for t, o, c in ks:
        if t + 59 < jetzt - 5:
            PREISE[a].append((t + 59, c))


def tick():
    """Ein Durchlauf: Preise, Fenster, Orderbuecher, Modell, Module, Auswertung."""
    jetzt = _jetzt()
    _aenderung_vermerken()
    px = preise_holen()
    for a, v in px.items():
        if not PREISE[a]:
            _rueckblick(a, jetzt)
        PREISE[a].append((jetzt, v))

    aktiv = []
    for a in ASSETS:
        for tf, dauer in tfs(a).items():
            start = int(jetzt - jetzt % dauer)
            mk = markt(slug_von(a, tf, start))
            if not mk:
                continue
            w = _fenster(a, tf, start)
            aktiv.append((a, tf, w, mk))

    bk = buecher([t for _, _, _, mk in aktiv for t in (mk["up"], mk["down"])])
    live = []
    for a, tf, w, mk in aktiv:
        up, dn = bk.get(str(mk["up"]), {}), bk.get(str(mk["down"]), {})
        s0 = _startpreis(a, w)
        metall = a in METALLE
        ende_modus = metall and not mk.get("avg")
        if a not in px:
            m = None
        elif ende_modus:
            m = modell_ende(a, w["start"], w["dauer"], s0, jetzt)
        else:
            m = modell(a, w["start"], w["dauer"], s0, jetzt)
        rest = w["start"] + w["dauer"] - jetzt
        ask_u, ask_d = up.get("ask"), dn.get("ask")
        bid_u, bid_d = up.get("bid"), dn.get("bid")
        mid_u = None
        if bid_u is not None and ask_u is not None:
            mid_u = (bid_u + ask_u) / 2
        elif ask_d is not None and bid_d is not None:
            mid_u = 1 - (bid_d + ask_d) / 2

        signal = None
        pr = None
        if m:
            pr = m["p_up"]
            m["p_up"] = gekoppelt(pr, mid_u, ("metal:" + tf) if metall else tf)
        if m and ask_u and ask_d:
            pu = m["p_up"]
            f = m["f"]
            edge_u = pu - ask_u - gebuehr(ask_u)
            edge_d = (1 - pu) - ask_d - gebuehr(ask_d)
            if edge_u >= edge_d and edge_u > 0:
                signal = {"side": "up", "edge": round(edge_u, 4)}
            elif edge_d > 0:
                signal = {"side": "down", "edge": round(edge_d, 4)}
            # Lernlabor (seit 2.37, intern): Merkmale an festen Punkten, Schattentrades
            try:
                import lernen
                lernen.punkt(w, a, tf, m, pr, mid_u, ask_u, ask_d, bid_u, jetzt)
            except Exception as e:
                log.debug(f"krypto lernen: {e}")
            if metall:
                # Metalle: erst nur intern (Lernlabor), keine Papier-Module, keine oeffentliche Kalibrierung.
                # Schattenspur billige Seite (seit 2.82.2): einmal je Fenster, die guenstigere Seite im Band.
                if "mcheap" not in w and MCHEAP_F[0] <= f <= MCHEAP_F[1] and ask_u and ask_d:
                    seite, ask = ("up", ask_u) if ask_u <= ask_d else ("down", ask_d)
                    if MCHEAP_MIN <= ask <= MCHEAP_MAX:
                        w["mcheap"] = {"side": seite, "price": round(ask, 4), "fee": round(gebuehr(ask), 5),
                                       "f": round(f, 3)}
            for c in (() if metall else CHECKS):
                k = str(c)
                if k not in w["calib"] and f >= c and mid_u is not None and w["full"]:
                    w["calib"][k] = {"e": round(pu, 4), "r": round(pr, 4), "m": round(mid_u, 4)}
            summe = ask_u + ask_d + gebuehr(ask_u) + gebuehr(ask_d)
            w["min_sum"] = round(summe if w["min_sum"] is None else min(w["min_sum"], summe), 4)

            if w["full"] and not metall:
                # engine
                ask_sig = signal and (ask_u if signal["side"] == "up" else ask_d)
                if "engine" not in w["done"] and tf in ENGINE_TFS and 0.2 <= f <= 0.95 and signal \
                        and signal["edge"] >= EDGE and 0.05 <= ask_sig <= ENGINE_MAX_ASK:
                    ask = ask_sig
                    _einstieg(w, "engine", signal["side"], ask,
                              pu if signal["side"] == "up" else 1 - pu)
                    try:
                        _pick_senden(w, signal["side"], ask)
                    except Exception as e:
                        log.warning(f"crypto pick: {e}")
                # both
                if "both" not in w["done"] and summe < 0.995 and f <= 0.95:
                    n = max(1.0, min(SHARES, up.get("ask_size") or 0, dn.get("ask_size") or 0))
                    _einstieg(w, "both", "up", ask_u, None, n)
                    w["done"].remove("both")
                    _einstieg(w, "both", "down", ask_d, None, n)
                # Gabagool 2: zeitversetzt (seit 2.81.3)
                l1 = w.get("leg1")
                if l1 is None and "leg" not in w["done"] and LEG1_F[0] <= f <= LEG1_F[1]:
                    r60 = rendite(w["a"], 60, jetzt)
                    if r60:
                        seite = "up" if r60 > 0 else "down"
                        ask = ask_u if seite == "up" else ask_d
                        menge = (up if seite == "up" else dn).get("ask_size") or 0
                        if LEG1_MIN <= ask <= LEG1_MAX and menge >= SHARES:
                            _einstieg(w, "leg", seite, ask)
                            w["leg1"] = {"side": seite, "price": round(ask, 4), "zu": False}
                elif l1 and not l1.get("zu") and f <= 0.97:
                    gs = "down" if l1["side"] == "up" else "up"
                    ask = ask_d if gs == "down" else ask_u
                    menge = (dn if gs == "down" else up).get("ask_size") or 0
                    if ask and menge >= SHARES and \
                            l1["price"] + ask + gebuehr(l1["price"]) + gebuehr(ask) <= LEG_CAP:
                        if "leg" in w["done"]:
                            w["done"].remove("leg")
                        _einstieg(w, "leg", gs, ask)
                        l1["zu"] = True
                # Gabagool 3: Schatten mit Stop-Hedge (ab dem Takt nach dem ersten Kauf)
                g1 = w.get("leg1")
                if g1 and f <= 0.97:
                    if not g1.get("g3"):
                        g1["g3"] = True
                    else:
                        g3 = w.setdefault("g3", {})
                        gs3 = "down" if g1["side"] == "up" else "up"
                        ask3 = ask_d if gs3 == "down" else ask_u
                        menge3 = (dn if gs3 == "down" else up).get("ask_size") or 0
                        if ask3 and menge3 >= SHARES:
                            summe3 = g1["price"] + ask3 + gebuehr(g1["price"]) + gebuehr(ask3)
                            for st3 in G3_STOPS:
                                k3 = f"{st3:.2f}"
                                if k3 in g3:
                                    continue
                                if summe3 <= LEG_CAP:
                                    g3[k3] = {"how": "lock", "price": round(ask3, 4), "sum": round(summe3, 4)}
                                elif summe3 >= st3:
                                    g3[k3] = {"how": "stop", "price": round(ask3, 4), "sum": round(summe3, 4)}
                # lag
                if "lag" not in w["done"] and 0.1 <= f <= 0.9:
                    r60, r300 = rendite(w["a"], 60, jetzt), rendite(w["a"], 300, jetzt)
                    if r60 is not None and r300 is not None and r60 * r300 > 0:
                        z = abs(r60) / (sigma(w["a"]) * math.sqrt(60))
                        if abs(r60) >= LAG_MOVE_60[tf] and abs(r300) >= LAG_MOVE_300 \
                                and z >= LAG_Z[tf]:
                            seite = "up" if r60 > 0 else "down"
                            p = pr if seite == "up" else 1 - pr
                            ask = ask_u if seite == "up" else ask_d
                            if 0.05 <= ask <= 0.92 and p - ask - gebuehr(ask) >= LAG_EDGE:
                                _einstieg(w, "lag", seite, ask, p)
                # sniper: strenger lag, nur 15 min und 4 h
                if "sniper" not in w["done"] and tf in ("15m", "4h") and 0.1 <= f <= 0.85:
                    r60, r300 = rendite(w["a"], 60, jetzt), rendite(w["a"], 300, jetzt)
                    if r60 is not None and r300 is not None and r60 * r300 > 0:
                        z = abs(r60) / (sigma(w["a"]) * math.sqrt(60))
                        if abs(r60) >= 2 * LAG_MOVE_60[tf] and abs(r300) >= 1.5 * LAG_MOVE_300 \
                                and z >= LAG_Z[tf] + 1.0:
                            seite = "up" if r60 > 0 else "down"
                            p = pr if seite == "up" else 1 - pr
                            ask = ask_u if seite == "up" else ask_d
                            if 0.05 <= ask <= 0.90 and p - ask - gebuehr(ask) >= 2 * LAG_EDGE:
                                _einstieg(w, "sniper", seite, ask, p)
                # late
                if "late" not in w["done"] and tf in LATE_TFS and 0 < rest <= LATE[tf] \
                        and mid_u is not None:
                    for seite, p, ask, mk_s in (("up", pu, ask_u, mid_u),
                                                ("down", 1 - pu, ask_d, 1 - mid_u)):
                        if p >= 0.90 and mk_s >= 0.80 and ask <= 0.95 \
                                and p - ask - gebuehr(ask) >= 0.01:
                            _einstieg(w, "late", seite, ask, p)
                            break

        live.append({
            "slug": w["slug"], "a": a, "name": NAMES[a], "sym": ASSETS[a], "tf": tf,
            "start": w["start"], "end": w["start"] + w["dauer"], "s0": s0,
            "now": px.get(a), "avg": m and round(m["avg"], 6),
            "p_up": m and round(m["p_up"], 4), "p_raw": None if pr is None else round(pr, 4),
            "f": m and round(m["f"], 4),
            "ask_up": ask_u, "ask_down": ask_d, "bid_up": bid_u, "bid_down": bid_d,
            "mid_up": None if mid_u is None else round(mid_u, 4),
            "signal": signal, "full": w["full"],
            "spark": _spark(a, w["start"]),
            "entries": [{"m": t["m"], "side": t["side"], "price": t["price"]} for t in w["trades"]],
        })

    # abgelaufene Fenster werten
    for slug, w in list(_OFFEN.items()):
        ende = w["start"] + w["dauer"]
        if jetzt < ende + 45:
            continue
        if not w["trades"] and not w["calib"] and not w.get("lern") and not w.get("ml"):
            _OFFEN.pop(slug, None)
            continue
        if w.get("next_try", 0) > jetzt:
            continue
        sieger = ausgang(slug)
        quelle = "polymarket"
        if sieger is None and jetzt > ende + 1800:
            # Notfall: eigener Schnitt, als Schaetzung markiert
            m = (modell_ende if w["a"] in METALLE else modell)(w["a"], w["start"], w["dauer"], w["s0"], ende)
            sieger = None if not m else ("up" if m["mu"] >= w["s0"] else "down")
            quelle = "estimate"
        if sieger is None:
            if jetzt > ende + 7200:
                _OFFEN.pop(slug, None)
            else:
                w["next_try"] = jetzt + 30
            continue
        _werten(w, sieger, quelle)
        if quelle == "polymarket":
            try:
                import lernen
                lernen.aufloesen(w, sieger)
            except Exception as e:
                log.debug(f"krypto lernen aufloesen: {e}")
        _OFFEN.pop(slug, None)
        log.info(f"krypto: {slug} -> {sieger} ({quelle}), {len(w['trades'])} trades")

    zustand = {"ts": jetzt, "markets": live, "assets": {a: px.get(a) for a in ASSETS}}
    storage.kv_set("krypto_live", json.dumps(zustand, separators=(",", ":")))
    storage.kv_set("krypto_open", json.dumps(_OFFEN, separators=(",", ":")))
    storage.kv_set("krypto_book", json.dumps(buch(), separators=(",", ":")))
    return zustand


# ── fuer die API ────────────────────────────────────────────────────────

def _quote(s):
    if not s or not s.get("n"):
        return None
    return {"n": s["n"], "wins": s["win"], "win_rate": round(s["win"] / s["n"], 4),
            "cost": round(s["cost"], 2), "pnl": round(s["pnl"], 2),
            "roi": round(s["pnl"] / s["cost"], 4) if s["cost"] else 0.0,
            "fees": round(s.get("fee", 0.0), 2)}


def lage(alles=False):
    """Alles fuer das Terminal in einem Aufruf. alles=True (Admin) zeigt auch die Module, die nur intern laufen."""
    weg = set() if alles else verborgen()
    try:
        live = json.loads(storage.kv_get("krypto_live") or "{}")
    except Exception:
        live = {}
    try:
        b = json.loads(storage.kv_get("krypto_book") or "{}") or _leer_buch()
    except Exception:
        b = _leer_buch()
    st = b.get("stats", {})
    seit_v2 = next((c["ts"] for c in b.get("changes", []) if c.get("v") == MODEL_V), None)

    def minus(a_, b_):
        if not a_:
            return None
        if not b_:
            return a_
        return {k: a_.get(k, 0) - b_.get(k, 0) for k in ("n", "win", "cost", "pnl", "fee")}

    mods = []
    for m in MODULES:
        if m in weg:
            continue
        name, info = MODULE_INFO[m]
        tage = sorted(b.get("days", {}).items())
        kurve, summe = [], 0.0
        for tag, d in tage:
            if "v2:" + m in d:
                summe += d["v2:" + m]["pnl"]
            kurve.append({"day": tag, "pnl": round(summe, 2)})
        mods.append({"id": m, "name": name, "info": info, "since": seit_v2,
                     "all": _quote(st.get(f"v2:{m}|*|*")),
                     "prev": _quote(minus(st.get(f"{m}|*|*"), st.get(f"v2:{m}|*|*"))),
                     "tf": {tf: _quote(st.get(f"v2:{m}|{tf}|*")) for tf in TFS},
                     "assets": {}, "curve": kurve[-60:],
                     "pairs": _paare((b.get("pairs") or {}).get("leg|*")) if m == "leg" else None})
    # pro Coin ueber alle tf, seit v2
    for mod in mods:
        for a in ASSETS:
            agg = {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0, "fee": 0.0}
            for tf in TFS:
                s = st.get(f"v2:{mod['id']}|{tf}|{a}")
                if s:
                    for k in agg:
                        agg[k] += s.get(k, 0)
            mod["assets"][a] = _quote(agg)
    picks = sum(v.get("n", 0) for k, v in st.items() if k.endswith("|*|*") and not k.startswith("v2:")
                and k.split("|")[0] not in weg)
    calib = {}
    for k, s in b.get("calib", {}).items():
        tf, c = k.split("|")
        if not s["n"]:
            continue
        calib.setdefault(tf, {})[c] = {
            "n": s["n"], "engine_hit": round(s["eng_hit"] / s["n"], 4),
            "market_hit": round(s["mkt_hit"] / s["n"], 4),
            "engine_brier": round(s["eng_brier"] / s["n"], 4),
            "market_brier": round(s["mkt_brier"] / s["n"], 4)}
    alter = time.time() - float(live.get("ts") or 0)
    oeff = metalle_oeffentlich()
    maerkte = [m for m in live.get("markets", []) if oeff or m.get("a") not in METALLE]
    if weg:
        maerkte = [dict(m, entries=[e for e in (m.get("entries") or []) if e.get("m") not in weg]) for m in maerkte]
    preise_ = {k: v for k, v in (live.get("assets") or {}).items() if oeff or k not in METALLE}
    return {"updated": live.get("ts"), "stale": alter > 60,
            "markets": maerkte, "prices": preise_,
            "modules": mods, "gabagool3": {"stops": G3_STOPS, "cap": LEG_CAP, "books": g3_bericht(b)}, "calibration": calib,
            "trades": [t for t in b.get("trades", []) if t.get("m") not in weg][:40],
            "retired": [{"id": m, "name": MODULE_INFO[m][0]} for m in MODULES if m in weg],
            "windows": b.get("windows", 0), "resolved": b.get("resolved", 0),
            "since": b.get("since"), "since_v2": seit_v2, "picks": picks,
            "fee_rate": FEE_RATE, "edge": EDGE,
            "assets": [{"id": a, "sym": ASSETS[a], "name": NAMES[a]} for a in ASSETS if oeff or a not in METALLE],
            "timeframes": list(TFS) + (["1h"] if oeff else [])}
