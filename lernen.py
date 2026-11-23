"""
lernen.py: Krypto-Lernlabor. Seit 2.37. Nur intern: handelt nichts, zeigt nichts oeffentlich.

Ziel: herausfinden, ob die Krypto-Engine mit den Boersendaten (Orderbuecher, Funding, Open
Interest, aggressive Kaeufe, Liquidationen) und dem Optionsmarkt einen echten Vorsprung vor
dem Polymarket-Preis bekommt. Nicht "oefter treffen", sondern besser kalibriert sein als der
Preis und nur kaufen, wenn der Vorsprung nach Gebuehren reicht.

    punkt()        In jedem Up/Down-Fenster an 10, 20, ... 90 % der Laufzeit eine Zeile mit allen
                   Merkmalen, Marktpreis, Asks und Engine-Zahl in krypto_lern (Postgres).
    aufloesen()    Nach dem Ausgang: y (1 = Up) eintragen, Schattentrades abrechnen.
    trainieren()   Alle 6 Stunden: logistische Regression je Zeitfenster (5m, 15m, 4h).
                   Walk-forward: jeder Testtag wird nur mit den Tagen davor trainiert.
                   Bericht (Brier Modell gegen Markt gegen Engine, Kalibrierung, Papier-Ergebnis
                   je Schwelle) in kv krypto_lab. Das Modell auf allen Daten in kv krypto_ml_model.
    schatten       Sobald ein Modell da ist: einmal je Fenster ein Schattentrade, wenn
                   Modell - Ask - Gebuehr >= Schwelle. Echte Out-of-sample-Bilanz in kv krypto_ml_live.

Seit 2.66: technische Indikatoren als weitere Merkmale (RSI 14, EMA 9/21, Bollinger %B, MACD-Histogramm,
Momentum 15 min, Schwankungs-Regime, Abstand zum Fensterhoch/-tief, Handelssitzung) und bei Gold/Silber der
Gleichlauf des anderen Metalls. Die Regression gewichtet sie selbst; was nichts bringt, bekommt ~0.
Gold und Silber haben eigene Modelle ("metal:15m", "metal:1h"), damit sie die Krypto-Zahlen nicht verwaschen.
Seit 2.67.1 bei Gold/Silber zusaetzlich: Dollar (EUR/USD 5 min), Vorlauf des Futures-Preises vor dem Spot (60 s),
Gold/Silber-Ratio gegen die letzte Stunde, Makro-Termin im Fenster (dort kein Schattentrade), London-Fix,
Comex-Eroeffnung und letzte Handelsstunde.
Seit 2.69.1 fuer alle Werte: Ichimoku (Tenkan/Kijun, Kreuzung, Lage zur Wolke) als Test.

Nichts davon fliesst in die oeffentlichen Module oder ins Terminal. Live geht es erst, wenn der
Betreiber es nach Wochen mit stabil positiven Zahlen entscheidet.
"""
import json
import math
import os
import threading
import time
from datetime import datetime, timezone

import storage
from logger import log

LERN_CHECKS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
HALTEN_TAGE = int(os.getenv("LAB_KEEP_DAYS", "40"))
MIN_TRAIN = int(os.getenv("LAB_MIN_TRAIN", "1500"))
TEST_TAGE = int(os.getenv("LAB_TEST_DAYS", "14"))
SCHWELLEN = (0.02, 0.03, 0.05, 0.08, 0.12)
L2 = float(os.getenv("LAB_L2", "3.0"))
FEE_RATE = float(os.getenv("CRYPTO_FEE_RATE", "0.07"))
TAKT_H = float(os.getenv("LAB_TRAIN_HOURS", "6"))

MERKMALE = ("lm", "lr", "z", "f", "sig", "r60", "r300", "spread", "overround",
            "b10", "b50", "tk5", "tk15", "fund", "oi15", "ls", "liq", "prem", "opt", "has_ex", "has_opt",
            # seit 2.66: Indikatoren
            "rsi", "ema", "bb", "macd", "r900", "vr", "hl", "us", "asia", "peer",
            # seit 2.67.1: nur Gold/Silber (bei Krypto immer 0)
            "usd", "lead", "ratio", "makro", "fix", "nyo", "nyc",
            # seit 2.69.1: Ichimoku (Test, ob es etwas bringt): Tenkan gegen Kijun, frische Kreuzung, Lage zur Wolke
            "ich_tk", "ich_x", "ich_cl")
METALL_MERKMALE = ("usd", "lead", "ratio", "makro", "fix", "nyo", "nyc")
TFS_LAB = ("5m", "15m", "4h", "metal:15m", "metal:1h")
MAX_ASK = float(os.getenv("LAB_MAX_ASK", "0.75"))       # seit 2.66.7: teure Favoriten nicht mehr (ein Verlust frisst dort viele Gewinne)
ENG5_EDGE = float(os.getenv("LAB_ENG5_EDGE", "0.05"))   # Schattenspur der Engine-Zahl auf 5 Minuten

_SCHEMA = [False]
_LOCK = threading.Lock()
_MODELL = {"ts": 0.0, "m": None}


def _gebuehr(p):
    p = max(0.0, min(1.0, float(p)))
    return FEE_RATE * p * (1 - p)


def _logit(p):
    p = min(0.995, max(0.005, float(p)))
    return math.log(p / (1 - p))


def _pg():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS krypto_lern (
            slug TEXT NOT NULL, chk TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            a TEXT, tf TEXT, x JSONB, mid DOUBLE PRECISION, ask_u DOUBLE PRECISION,
            ask_d DOUBLE PRECISION, p_eng DOUBLE PRECISION, p_ml DOUBLE PRECISION, y SMALLINT,
            PRIMARY KEY (slug, chk))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_krypto_lern_ts ON krypto_lern(ts)")
        _SCHEMA[0] = True
    return pg


# ── Merkmale ─────────────────────────────────────────────────────────────

_BOERSE = {"ts": 0.0, "d": {}}


def _boerse():
    if time.time() - _BOERSE["ts"] > 20:
        try:
            _BOERSE["d"] = json.loads(storage.kv_get("boersen_live") or "{}")
        except Exception:
            _BOERSE["d"] = {}
        _BOERSE["ts"] = time.time()
    return _BOERSE["d"]


def _zahl(v, d=0.0, lo=-50.0, hi=50.0):
    try:
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return d
        return max(lo, min(hi, v))
    except Exception:
        return d


def merkmale(a, tf, m, pr, mid_u, ask_u, ask_d, bid_u, w, jetzt):
    import krypto
    sig = krypto.sigma(a)
    r60, r300 = krypto.rendite(a, 60, jetzt), krypto.rendite(a, 300, jetzt)
    x = {"lm": _logit(mid_u), "lr": _logit(pr), "z": _zahl((m["mu"] - w["s0"]) / m["sd"] if m["sd"] else 0, 0, -8, 8),
         "f": m["f"], "sig": _zahl(sig * 1e4, 0, 0, 50),
         "r60": _zahl(r60 / (sig * math.sqrt(60)) if r60 is not None and sig else 0, 0, -10, 10),
         "r300": _zahl(r300 / (sig * math.sqrt(300)) if r300 is not None and sig else 0, 0, -10, 10),
         "spread": _zahl((ask_u - bid_u) if (ask_u is not None and bid_u is not None) else 0.02, 0.02, 0, 1),
         "overround": _zahl(((ask_u or 0) + (ask_d or 0)) - 1, 0, -1, 1)}
    b = _boerse()
    c = (b.get("coins") or {}).get(a) or {}
    frisch = c and time.time() - float(b.get("ts") or 0) < 240
    if frisch:
        liq = c.get("liquidations") or {}
        lo, sh = float(liq.get("long_15m") or 0), float(liq.get("short_15m") or 0)
        ls = (c.get("long_short") or {}).get("now")
        x.update({
            "b10": _zahl((c.get("book") or {}).get("pressure_10bp"), 0, -1, 1),
            "b50": _zahl((c.get("book") or {}).get("pressure_50bp"), 0, -1, 1),
            "tk5": _zahl(((c.get("taker_buy") or {}).get("5m") or 0.5) - 0.5, 0, -0.5, 0.5),
            "tk15": _zahl(((c.get("taker_buy") or {}).get("15m") or 0.5) - 0.5, 0, -0.5, 0.5),
            "fund": _zahl((c.get("funding") or {}).get("annual"), 0, -3, 3),
            "oi15": _zahl((c.get("oi") or {}).get("chg_15m"), 0, -0.2, 0.2),
            "ls": _zahl(math.log(float(ls)) if ls else 0, 0, -3, 3),
            "liq": _zahl((sh - lo) / (sh + lo + 1e5), 0, -1, 1),
            "prem": _zahl(((c.get("basis") or {}).get("perp_premium") or 0) * 1e3, 0, -20, 20),
            "has_ex": 1.0})
    else:
        x.update({k: 0.0 for k in ("b10", "b50", "tk5", "tk15", "fund", "oi15", "ls", "liq", "prem")})
        x["has_ex"] = 0.0
    try:
        x.update(indikatoren(a, w, jetzt))
    except Exception as e:
        log.debug(f"lernen indikatoren: {e}")
    x.update({k: 0.0 for k in METALL_MERKMALE})
    try:
        import krypto
        if a in krypto.METALLE:
            x.update(metall_merkmale(a, w, jetzt))
    except Exception as e:
        log.debug(f"lernen metall: {e}")
    x["opt"], x["has_opt"] = 0.0, 0.0
    try:
        import optionen
        sym = (optionen.COINS.get(a) or (None,))[0]
        fl = optionen._FL.get(sym) if sym else None
        if fl and time.time() - fl.get("_ts", 0) < 900 and tf == "4h":
            # Optionsmarkt: P(Kurs am Fensterende > Startpreis). Fuer 4 h tragfaehig, kuerzer zu ungenau.
            o = optionen.wahrscheinlichkeit(fl, w["s0"], w["start"] + w["dauer"])
            if o and o.get("p") is not None:
                x["opt"] = _zahl(_logit(o["p"]) - x["lm"], 0, -6, 6)
                x["has_opt"] = 1.0
    except Exception:
        pass
    return x


def _minuten(a, jetzt, n=60):
    """Schlusskurse je Minute aus den Live-Preisen (letzter Preis jeder Minute)."""
    import krypto
    out = {}
    for t, p in krypto.PREISE[a]:
        if t >= jetzt - n * 60 - 60:
            out[int(t // 60)] = p
    ks = sorted(out)
    return [out[k] for k in ks]


def _ema(xs, n):
    if not xs:
        return None
    k = 2 / (n + 1)
    e = xs[0]
    for v in xs[1:]:
        e = v * k + e * (1 - k)
    return e


def indikatoren(a, w, jetzt):
    """Klassische Indikatoren auf 1-Minuten-Kursen, alle normiert, damit die Regression sie mischen kann."""
    import krypto
    c = _minuten(a, jetzt, 60)
    sig = krypto.sigma(a) or 1e-4
    x = {k: 0.0 for k in ("rsi", "ema", "bb", "macd", "r900", "vr", "hl", "us", "asia", "peer")}
    if len(c) >= 15:
        gw = [max(0.0, c[i] - c[i - 1]) for i in range(len(c) - 14, len(c))]
        vl = [max(0.0, c[i - 1] - c[i]) for i in range(len(c) - 14, len(c))]
        g, v = sum(gw) / 14, sum(vl) / 14
        rsi = 100.0 if v == 0 else 100 - 100 / (1 + g / v)
        x["rsi"] = _zahl((rsi - 50) / 50, 0, -1, 1)
    if len(c) >= 26:
        p = c[-1]
        skala = p * sig * math.sqrt(60)
        x["ema"] = _zahl((_ema(c[-30:], 9) - _ema(c[-30:], 21)) / skala if skala else 0, 0, -10, 10)
        macd = [(_ema(c[:i], 12) - _ema(c[:i], 26)) for i in range(len(c) - 8, len(c) + 1)]
        sigl = _ema(macd, 9)
        x["macd"] = _zahl((macd[-1] - sigl) / skala if skala else 0, 0, -10, 10)
    if len(c) >= 20:
        r = c[-20:]
        m = sum(r) / 20
        sd = math.sqrt(sum((v - m) ** 2 for v in r) / 19) or 1e-12
        x["bb"] = _zahl((c[-1] - m) / (2 * sd), 0, -3, 3)
    r900 = krypto.rendite(a, 900, jetzt)
    if r900 is not None and sig:
        x["r900"] = _zahl(r900 / (sig * math.sqrt(900)), 0, -10, 10)
    s5, s30 = krypto.sigma(a, 300), krypto.sigma(a, 1800)
    if s5 and s30:
        x["vr"] = _zahl(math.log(s5 / s30), 0, -3, 3)
    seit = [p for t, p in krypto.PREISE[a] if t >= w["start"]]
    if len(seit) >= 3:
        hi, lo = max(seit), min(seit)
        x["hl"] = _zahl((2 * seit[-1] - hi - lo) / (hi - lo) if hi > lo else 0, 0, -1, 1)
    std = datetime.fromtimestamp(jetzt, timezone.utc).hour + datetime.fromtimestamp(jetzt, timezone.utc).minute / 60
    x["us"] = 1.0 if 13.5 <= std < 20 else 0.0
    x["asia"] = 1.0 if 0 <= std < 7 else 0.0
    try:
        x.update(ichimoku(a, jetzt, sig))
    except Exception as e:
        log.debug(f"lernen ichimoku: {e}")
    partner = {"xau": "xag", "xag": "xau"}.get(a)
    if partner:
        rp, sp = krypto.rendite(partner, 300, jetzt), krypto.sigma(partner)
        if rp is not None and sp:
            x["peer"] = _zahl(rp / (sp * math.sqrt(300)), 0, -10, 10)
    return x


def ichimoku(a, jetzt, sig):
    """Seit 2.69.1, auf 1-Minuten-Schlusskursen (Hoch/Tief je Periode aus den Schlusskursen):
    Tenkan = (Hoch9 + Tief9) / 2, Kijun = (Hoch26 + Tief26) / 2, Span A = (Tenkan + Kijun) / 2,
    Span B = (Hoch52 + Tief52) / 2, beide 26 Perioden nach vorn geschoben (also die Wolke von vor 26 Minuten).
    ich_tk = Abstand Tenkan zu Kijun (normiert), ich_x = +1/-1 bei einer Kreuzung in den letzten 3 Minuten,
    ich_cl = Abstand des Kurses zur Wolke (0 in der Wolke). Ob das etwas bringt, sagt nur der Walk-forward."""
    out = {"ich_tk": 0.0, "ich_x": 0.0, "ich_cl": 0.0}
    c = _minuten(a, jetzt, 90)
    if len(c) < 30:
        return out
    hl = lambda xs: (max(xs) + min(xs)) / 2
    p = c[-1]
    skala = p * (sig or 1e-4) * math.sqrt(60)
    if not skala:
        return out
    tk = lambda i: hl(c[max(0, i - 9):i]) - hl(c[max(0, i - 26):i])
    jetzt_d = tk(len(c))
    out["ich_tk"] = _zahl(jetzt_d / skala, 0, -10, 10)
    for k in range(1, 4):
        vorher = tk(len(c) - k)
        if vorher <= 0 < jetzt_d:
            out["ich_x"] = 1.0
            break
        if vorher >= 0 > jetzt_d:
            out["ich_x"] = -1.0
            break
    if len(c) >= 78:
        i = len(c) - 26
        ten, kij = hl(c[i - 9:i]), hl(c[i - 26:i])
        a_, b_ = (ten + kij) / 2, hl(c[i - 52:i])
        oben, unten = max(a_, b_), min(a_, b_)
        out["ich_cl"] = _zahl(((p - oben) if p > oben else (p - unten) if p < unten else 0.0) / skala, 0, -10, 10)
    return out


# ── Gold und Silber: Dollar, Futures-Vorlauf, Ratio, Makro-Termine, Uhrzeiten (seit 2.67.1) ──

FF_URL = os.getenv("MAKRO_CAL_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
_KAL = {"ts": 0.0, "liste": None}


def makro_termine():
    """Zeitpunkte (Unix) wichtiger US-Termine (High Impact: CPI, Zinsentscheid, Arbeitsmarkt, BIP, PCE ...).
    Wochenkalender von ForexFactory, alle 2 Stunden neu, im kv gesammelt (ueberlebt Neustarts und Wochenwechsel)."""
    if _KAL["liste"] is not None and time.time() - _KAL["ts"] < 7200:
        return _KAL["liste"]
    alt = []
    try:
        alt = json.loads(storage.kv_get("makro_kal") or "[]")
    except Exception:
        alt = []
    neu = []
    try:
        import requests
        r = requests.get(FF_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        for e in r.json():
            if e.get("country") == "USD" and e.get("impact") == "High" and e.get("date"):
                try:
                    neu.append([int(datetime.fromisoformat(e["date"]).timestamp()), str(e.get("title") or "")[:60]])
                except Exception:
                    pass
    except Exception as e:
        log.debug(f"lernen makro kalender: {e}")
    grenze = time.time() - 21 * 86400
    zusammen = {t: n for t, n in alt + neu if t >= grenze}
    liste = sorted([t, n] for t, n in zusammen.items())
    if neu:
        try:
            storage.kv_set("makro_kal", json.dumps(liste))
        except Exception:
            pass
    _KAL.update(ts=time.time() if neu else time.time() - 6600, liste=liste)   # ohne Abruf in 10 Minuten neu versuchen
    return liste


def _ueberlappt(s, e, a, b):
    return s < b and a < e


def _ortszeit_fenster(s, e, zone, punkte, breite_min):
    """1.0, wenn das Fenster [s, e] einen der Uhrzeitpunkte (h, m) der Zone +- breite_min beruehrt."""
    from zoneinfo import ZoneInfo
    z = ZoneInfo(zone)
    for t in (s, e):
        tag = datetime.fromtimestamp(t, z)
        for h, m in punkte:
            p = tag.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
            if _ueberlappt(s, e, p - breite_min * 60, p + breite_min * 60):
                return 1.0
    return 0.0


def metall_merkmale(a, w, jetzt):
    """Nur fuer xau/xag. Alles normiert, damit die Regression es mit den anderen Merkmalen mischen kann."""
    import krypto
    x = {k: 0.0 for k in METALL_MERKMALE}
    sig = krypto.sigma(a) or 1e-4
    # 1) Dollar: EUR/USD-Bewegung der letzten 5 Minuten in Basispunkten (schwacher Dollar = EUR/USD steigt)
    r = krypto.extra_rendite("eur", 300, jetzt)
    if r is not None:
        x["usd"] = _zahl(r * 1e4, 0, -30, 30)
    # 2) Vorlauf: gehandelter Futures-Preis gegen den Spot (Abrechnung laeuft ueber den Spot), 60 Sekunden
    rm, rs = krypto.extra_rendite(a + "_mark", 60, jetzt), krypto.rendite(a, 60, jetzt)
    if rm is not None and rs is not None:
        x["lead"] = _zahl((rm - rs) / (sig * math.sqrt(60)), 0, -10, 10)
    # 3) Gold/Silber-Ratio: Abstand zum Schnitt der letzten Stunde in Standardabweichungen.
    #    Ausgerichtet aufs Metall: hohe Ratio = Silber billig -> fuer Silber positiv, fuer Gold negativ.
    g, sv = _minuten("xau", jetzt, 60), _minuten("xag", jetzt, 60)
    n = min(len(g), len(sv))
    if n >= 30:
        rr = [math.log(g[-n + i] / sv[-n + i]) for i in range(n)]
        m = sum(rr) / n
        sd = math.sqrt(sum((v - m) ** 2 for v in rr) / (n - 1)) or 1e-9
        z = _zahl((rr[-1] - m) / sd, 0, -5, 5)
        x["ratio"] = z if a == "xag" else -z
    # 4) Makro-Termin im Fenster (oder 5 Minuten davor): dort ist die Bewegung Zufall
    S, E = w["start"], w["start"] + w["dauer"]
    for t, _ in makro_termine():
        if S - 300 <= t <= E:
            x["makro"] = 1.0
            break
    # 5) Uhrzeiten: London-Fix (10:30 und 15:00 London), Comex-Eroeffnung (8:20 bis 9:30 New York), letzte Handelsstunde
    try:
        x["fix"] = _ortszeit_fenster(S, E, "Europe/London", ((10, 30), (15, 0)), 10)
        x["nyo"] = _ortszeit_fenster(S, E, "America/New_York", ((8, 20), (8, 55), (9, 30)), 15)
        x["nyc"] = _ortszeit_fenster(S, E, "America/New_York", ((16, 0), (16, 30)), 30)
    except Exception as e:
        log.debug(f"lernen uhrzeit: {e}")
    return x


# ── Datensammlung ────────────────────────────────────────────────────────

def punkt(w, a, tf, m, pr, mid_u, ask_u, ask_d, bid_u, jetzt):
    """Aus krypto.tick fuer jedes Fenster mit Modell und Buch. Billig, wenn kein Checkpunkt dran ist."""
    if not (m and w.get("full") and mid_u is not None and ask_u and ask_d and pr is not None):
        return
    done = w.setdefault("lern", [])
    checks = LERN_CHECKS if tf != "5m" else (0.2, 0.5, 0.8)
    faellig = [c for c in checks if str(c) not in done and m["f"] >= c and m["f"] < c + 0.08]
    for c in checks:
        if str(c) not in done and m["f"] >= c + 0.08:
            done.append(str(c))   # verpasst (z. B. Neustart): auslassen statt spaet nachtragen
    if not faellig:
        return
    c = faellig[-1]
    done.append(str(c))
    x = merkmale(a, tf, m, pr, mid_u, ask_u, ask_d, bid_u, w, jetzt)
    import krypto
    if a in krypto.METALLE:
        tf = "metal:" + tf
        w["lab_tf"] = tf
    w["makro"] = bool(x.get("makro"))
    p_ml = vorhersage(tf, x)
    try:
        pg = _pg()
        if pg:
            pg.execute("""INSERT INTO krypto_lern (slug, chk, a, tf, x, mid, ask_u, ask_d, p_eng, p_ml)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                       (w["slug"], str(c), a, tf, json.dumps(x), mid_u, ask_u, ask_d, m["p_up"], p_ml))
    except Exception as e:
        log.debug(f"lernen punkt: {e}")
    _schatten(w, tf, p_ml, ask_u, ask_d, m["f"])
    if tf == "5m" and not w.get("eng5") and 0.2 <= m["f"] <= 0.95:
        p = m["p_up"]
        eu = p - ask_u - _gebuehr(ask_u)
        ed = (1 - p) - ask_d - _gebuehr(ask_d)
        seite, ask, e = ("up", ask_u, eu) if eu >= ed else ("down", ask_d, ed)
        if e >= ENG5_EDGE and 0.05 <= ask <= MAX_ASK:
            w["eng5"] = {"side": seite, "price": round(ask, 4), "p": round(p if seite == "up" else 1 - p, 4),
                         "edge": round(e, 4), "f": round(m["f"], 3)}


def _schatten(w, tf, p, ask_u, ask_d, f):
    if p is None or w.get("ml") or not (0.2 <= f <= 0.95):
        return
    if w.get("makro") and str(tf).startswith("metal:"):
        return   # seit 2.67.1: Gold/Silber spielen nicht in Fenstern mit wichtigem US-Termin
    mod = (_MODELL.get("m") or {}).get(tf) or {}
    schwelle = mod.get("edge") or float(os.getenv("LAB_EDGE", "0.05"))
    eu = p - ask_u - _gebuehr(ask_u)
    ed = (1 - p) - ask_d - _gebuehr(ask_d)
    seite, ask, e = ("up", ask_u, eu) if eu >= ed else ("down", ask_d, ed)
    if e >= schwelle and 0.05 <= ask <= MAX_ASK:
        w["ml"] = {"side": seite, "price": round(ask, 4), "p": round(p if seite == "up" else 1 - p, 4),
                   "edge": round(e, 4), "f": round(f, 3)}


def aufloesen(w, sieger):
    y = 1 if sieger == "up" else 0
    try:
        pg = _pg()
        if pg and w.get("lern"):
            pg.execute("UPDATE krypto_lern SET y=%s WHERE slug=%s", (y, w["slug"]))
    except Exception as e:
        log.debug(f"lernen aufloesen: {e}")
    e5 = w.get("eng5")
    if e5:
        with _LOCK:
            try:
                st5 = json.loads(storage.kv_get("krypto_eng5_live") or "{}")
            except Exception:
                st5 = {}
            k5 = e5["price"] * 10 + _gebuehr(e5["price"]) * 10
            p5 = (10.0 if e5["side"] == sieger else 0.0) - k5
            for k in ("5m", w["a"]):
                s5 = st5.setdefault(k, {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0})
                s5["n"] += 1
                s5["win"] += 1 if e5["side"] == sieger else 0
                s5["cost"] = round(s5["cost"] + k5, 4)
                s5["pnl"] = round(s5["pnl"] + p5, 4)
            l5 = st5.setdefault("last", [])
            l5.insert(0, {"a": w["a"], "side": e5["side"], "price": e5["price"], "p": e5["p"], "edge": e5["edge"],
                          "won": e5["side"] == sieger, "pnl": round(p5, 3), "end": int(w["start"] + w["dauer"])})
            del l5[40:]
            st5.setdefault("since", int(time.time()))
            st5["rules"] = {"edge": ENG5_EDGE, "max_ask": MAX_ASK}
            storage.kv_set("krypto_eng5_live", json.dumps(st5))
    t = w.get("ml")
    if not t:
        return
    with _LOCK:
        try:
            st = json.loads(storage.kv_get("krypto_ml_live") or "{}")
        except Exception:
            st = {}
        kosten = t["price"] * 10 + _gebuehr(t["price"]) * 10
        pnl = (10.0 if t["side"] == sieger else 0.0) - kosten
        for k in (w.get("lab_tf") or w["tf"], "*" if not w.get("lab_tf") else "metal:*"):
            s = st.setdefault(k, {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0})
            s["n"] += 1
            s["win"] += 1 if t["side"] == sieger else 0
            s["cost"] = round(s["cost"] + kosten, 4)
            s["pnl"] = round(s["pnl"] + pnl, 4)
        letzte = st.setdefault("last", [])
        letzte.insert(0, {"a": w["a"], "tf": w["tf"], "side": t["side"], "price": t["price"], "p": t["p"],
                          "won": t["side"] == sieger, "pnl": round(pnl, 3), "end": int(w["start"] + w["dauer"])})
        del letzte[40:]
        st.setdefault("since", int(time.time()))
        storage.kv_set("krypto_ml_live", json.dumps(st))


# ── Modell ───────────────────────────────────────────────────────────────

def _lade_modell():
    if time.time() - _MODELL["ts"] > 300:
        try:
            _MODELL["m"] = json.loads(storage.kv_get("krypto_ml_model") or "null")
        except Exception:
            _MODELL["m"] = None
        _MODELL["ts"] = time.time()
    return _MODELL["m"]


def _p(mod, x):
    s = mod["b"]
    for k, wv, mu, sd in zip(MERKMALE, mod["w"], mod["mu"], mod["sd"]):
        s += wv * ((float(x.get(k, 0.0)) - mu) / sd)
    s = max(-30, min(30, s))
    return 1 / (1 + math.exp(-s))


def _kal(mod, p):
    """Seit 2.66.7: Nachkalibrierung aus dem Walk-forward (Stuetzpunkte p -> echte Quote), linear dazwischen."""
    pts = mod.get("cal") or []
    if len(pts) < 3:
        return p
    if p <= pts[0][0]:
        return pts[0][1]  # an den Raendern nicht weiter extrapolieren: genau dort war das Modell zu selbstsicher
    for (a, ya), (b, yb) in zip(pts, pts[1:]):
        if p <= b:
            return ya + (yb - ya) * (p - a) / (b - a) if b > a else yb
    return pts[-1][1]


def vorhersage(tf, x):
    m = _lade_modell()
    mod = (m or {}).get(tf)
    if not mod:
        return None
    try:
        return round(min(0.99, max(0.01, _kal(mod, _p(mod, x)))), 4)
    except Exception:
        return None


def _kal_fit(ps, ys):
    """Stuetzpunkte fuer die Nachkalibrierung: 20 Baender, monoton gemacht (pool adjacent violators)."""
    import numpy as np
    pts = []
    for i in range(20):
        lo, hi = i / 20, (i + 1) / 20
        m = (ps >= lo) & (ps < hi) if i < 19 else (ps >= lo)
        if m.sum() >= 80:
            pts.append([float(ps[m].mean()), float(ys[m].mean()), int(m.sum())])
    blk = []
    for p, y, n in pts:
        blk.append([p * n, y * n, n])
        while len(blk) > 1 and blk[-2][1] / blk[-2][2] > blk[-1][1] / blk[-1][2]:
            a, b = blk.pop(), blk.pop()
            blk.append([a[0] + b[0], a[1] + b[1], a[2] + b[2]])
    return [[round(p / n, 4), round(y / n, 4)] for p, y, n in blk]


def _fit(X, y, l2=L2, iters=30):
    """L2-logistische Regression mit Newton (IRLS). X ohne Achsenabschnitt, schon standardisiert."""
    import numpy as np
    n, d = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(d + 1)
    w[0] = math.log(max(y.mean(), 1e-3) / max(1 - y.mean(), 1e-3))
    reg = np.eye(d + 1) * l2
    reg[0, 0] = 0
    for _ in range(iters):
        z = np.clip(Xb @ w, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = Xb.T @ (p - y) + reg @ w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + reg
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        w -= step
        if np.max(np.abs(step)) < 1e-6:
            break
    return w


def _trainiere(X, y):
    import numpy as np
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    w = _fit((X - mu) / sd, y)
    return {"b": float(w[0]), "w": [float(v) for v in w[1:]], "mu": [float(v) for v in mu], "sd": [float(v) for v in sd]}


def _p_vec(mod, X):
    import numpy as np
    z = mod["b"] + ((X - np.array(mod["mu"])) / np.array(mod["sd"])) @ np.array(mod["w"])
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def _brier(ps, ys):
    import numpy as np
    return float(np.mean((ps - ys) ** 2)) if len(ys) else None


def _gebuehr_vec(p):
    return FEE_RATE * p * (1 - p)


def _sim(D, ps, schwelle):
    """Einmal je Fenster kaufen, beim ersten Punkt (ab 20 %) mit genug Vorsprung. 10 Anteile, echter Ask, Gebuehr."""
    import numpy as np
    au, ad, y, chk, fen = D["ask_u"], D["ask_d"], D["y"], D["chk"], D["win"]
    eu = ps - au - _gebuehr_vec(au)
    ed = (1 - ps) - ad - _gebuehr_vec(ad)
    up = eu >= ed
    ask = np.where(up, au, ad)
    e = np.where(up, eu, ed)
    ok = (e >= schwelle) & (ask >= 0.05) & (ask <= MAX_ASK) & (chk >= 0.2)
    idx = np.where(ok)[0]
    if not len(idx):
        return {"n": 0, "win": 0, "hit": None, "cost": 0.0, "pnl": 0.0, "roi": None}
    reihen = idx[np.lexsort((chk[idx], fen[idx]))]
    _, erste = np.unique(fen[reihen], return_index=True)
    sel = reihen[erste]
    kosten = ask[sel] * 10 + _gebuehr_vec(ask[sel]) * 10
    gewonnen = (up[sel] & (y[sel] == 1)) | (~up[sel] & (y[sel] == 0))
    pnl = np.where(gewonnen, 10 - kosten, -kosten)
    n, k = len(sel), float(kosten.sum())
    return {"n": int(n), "win": int(gewonnen.sum()), "hit": round(float(gewonnen.mean()), 4),
            "cost": round(k, 2), "pnl": round(float(pnl.sum()), 2), "roi": round(float(pnl.sum()) / k, 4) if k else None}


def _kalibrierung(ps, ys):
    import numpy as np
    out = []
    b = np.minimum(9, (ps * 10).astype(int))
    for i in range(10):
        m = b == i
        if m.any():
            out.append({"from": i / 10, "to": (i + 1) / 10, "n": int(m.sum()), "real": round(float(ys[m].mean()), 3)})
    return out


def _laden(pg, tf, limit):
    import numpy as np
    spalten = ", ".join(f"COALESCE((x->>'{k}')::float8, 0)" for k in MERKMALE)
    rows = pg.execute(f"""SELECT slug, chk, to_char(ts AT TIME ZONE 'UTC','YYYY-MM-DD'), mid, ask_u, ask_d, p_eng, y, {spalten}
                          FROM krypto_lern WHERE tf=%s AND y IS NOT NULL AND mid IS NOT NULL
                          AND ask_u IS NOT NULL AND ask_d IS NOT NULL AND p_eng IS NOT NULL
                          ORDER BY ts DESC LIMIT %s""", (tf, limit)).fetchall()
    if not rows:
        return None
    rows.reverse()
    slugs = {}
    D = {"win": np.array([slugs.setdefault(r[0], len(slugs)) for r in rows]),
         "chk": np.array([float(r[1]) for r in rows]),
         "day": np.array([r[2] for r in rows]),
         "mid": np.array([float(r[3]) for r in rows]), "ask_u": np.array([float(r[4]) for r in rows]),
         "ask_d": np.array([float(r[5]) for r in rows]), "p_eng": np.array([float(r[6]) for r in rows]),
         "y": np.array([int(r[7]) for r in rows]),
         "X": np.array([[float(v) for v in r[8:]] for r in rows], dtype=float)}
    return D


def trainieren():
    import numpy as np
    t0 = time.time()
    pg = _pg()
    if not pg:
        return None
    try:
        pg.execute("DELETE FROM krypto_lern WHERE ts < now() - make_interval(days => %s)", (HALTEN_TAGE,))
    except Exception:
        pass
    offen = pg.execute("SELECT count(*) FROM krypto_lern WHERE y IS NULL").fetchone()[0]
    bericht = {"ts": int(time.time()), "unresolved": int(offen), "tf": {}, "features": list(MERKMALE),
               "note": "Internal. Walk-forward: each test day is predicted by a model trained only on earlier days."}
    modelle, gesamt = {}, 0
    for tf in TFS_LAB:
        D = _laden(pg, tf, int(os.getenv("LAB_MAX_ROWS", "150000")))
        if D is None:
            bericht["tf"][tf] = {"rows": 0, "verdict": f"collecting data: needs {MIN_TRAIN} settled rows"}
            continue
        n = len(D["y"])
        gesamt += n
        tage = sorted(set(D["day"].tolist()))
        info = {"rows": n, "windows": int(len(set(D["win"].tolist()))), "days": len(tage), "from": tage[0], "to": tage[-1]}
        tp, tm = [], []
        for d in tage[-TEST_TAGE:]:
            tr = D["day"] < d
            te = D["day"] == d
            if tr.sum() < MIN_TRAIN or not te.any():
                continue
            mod = _trainiere(D["X"][tr], D["y"][tr])
            tp.append(_p_vec(mod, D["X"][te]))
            tm.append(te)
        if tp:
            maske = np.zeros(n, dtype=bool)
            ps_voll = np.zeros(n)
            for p_, m_ in zip(tp, tm):
                ps_voll[m_] = p_
                maske |= m_
            T = {k: (v[maske] if hasattr(v, "__len__") and len(v) == n else v) for k, v in D.items()}
            ps, ys = ps_voll[maske], T["y"]
            wf = {"rows": int(maske.sum()), "windows": int(len(set(T["win"].tolist()))), "test_days": len(tp),
                  "brier_model": round(_brier(ps, ys), 5), "brier_market": round(_brier(T["mid"], ys), 5),
                  "brier_engine": round(_brier(T["p_eng"], ys), 5), "calibration": _kalibrierung(ps, ys),
                  "paper": {str(s): _sim(T, ps, s) for s in SCHWELLEN},
                  "engine_paper": {str(s): _sim(T, T["p_eng"], s) for s in SCHWELLEN}}
            info["walk_forward"] = wf
            besser = wf["brier_model"] < wf["brier_market"]
            beste = max(((s, v) for s, v in wf["paper"].items() if v["n"] >= 50), key=lambda t: t[1]["pnl"], default=None)
            info["best_edge"] = float(beste[0]) if beste and beste[1]["pnl"] > 0 else None
            info["verdict"] = ("promising: beats the market price out of sample and the paper result is positive"
                               if besser and info["best_edge"] else
                               "beats the market price, but no threshold makes money yet" if besser else
                               "not better than the market price yet")
        else:
            info["verdict"] = f"collecting data: needs {MIN_TRAIN} settled rows before the first test day"
        cal = None
        if tp:
            cal = _kal_fit(ps, ys)
            info["walk_forward"]["calibrated_points"] = cal
        if n >= MIN_TRAIN:
            mod = _trainiere(D["X"], D["y"])
            if cal:
                mod["cal"] = cal
            mod["edge"] = info.get("best_edge") or float(os.getenv("LAB_EDGE", "0.05"))
            mod["n"] = n
            modelle[tf] = mod
            info["weights"] = sorted(([k, round(wv, 4)] for k, wv in zip(MERKMALE, mod["w"])), key=lambda t: -abs(t[1]))[:10]
        bericht["tf"][tf] = info
    bericht["rows"] = gesamt
    bericht["seconds"] = round(time.time() - t0, 1)
    storage.kv_set("krypto_lab", json.dumps(bericht, default=str))
    if modelle:
        storage.kv_set("krypto_ml_model", json.dumps(modelle))
        _MODELL["ts"] = 0.0
    log.info(f"lernen: {gesamt} rows, {len(modelle)} models, {bericht['seconds']} s")
    return bericht


def schleife():
    time.sleep(600)
    while True:
        try:
            trainieren()
        except Exception as e:
            log.warning(f"lernen trainieren: {e}")
        time.sleep(TAKT_H * 3600)


def bericht():
    try:
        b = json.loads(storage.kv_get("krypto_lab") or "{}")
    except Exception:
        b = {}
    try:
        live = json.loads(storage.kv_get("krypto_ml_live") or "{}")
    except Exception:
        live = {}
    zeilen = None
    try:
        pg = _pg()
        if pg:
            r = pg.execute("SELECT count(*), count(y), min(ts) FROM krypto_lern").fetchone()
            zeilen = {"total": r[0], "settled": r[1], "since": r[2].isoformat() if r[2] else None}
    except Exception:
        pass
    try:
        e5 = json.loads(storage.kv_get("krypto_eng5_live") or "{}")
    except Exception:
        e5 = {}
    try:
        mc = (json.loads(storage.kv_get("krypto_book") or "{}") or {}).get("metal_cheap") or {}
        for v in mc.values():
            v["roi"] = round(v["pnl"] / v["cost"], 4) if v.get("cost") else None
            v["hit"] = round(v["win"] / v["n"], 4) if v.get("n") else None
    except Exception:
        mc = {}
    try:
        import krypto as _kr
        g3 = _kr.g3_bericht(json.loads(storage.kv_get("krypto_book") or "{}") or {"g3": {}})
    except Exception:
        g3 = {}
    return {"report": b, "shadow": live, "engine_5m_track": e5, "metal_cheap": mc, "gabagool3": g3, "rows": zeilen,
            "rules": {"max_ask": MAX_ASK, "eng5_edge": ENG5_EDGE}}
