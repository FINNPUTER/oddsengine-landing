"""
optionen_kalib.py: Kalibrierung der Above-X-Wahrscheinlichkeit (seit 2.82.54).

Die Optionsflaeche von Deribit enthaelt einen Versicherungsaufschlag: grosse Bewegungen sind dort teurer, als sie
wirklich vorkommen. Liest man daraus Wahrscheinlichkeiten ab, sind die billigen Seiten auf Kalshi zu optimistisch
geschaetzt. Statt das zu raten, lernt die Engine aus ihren eigenen abgerechneten Picks, was wirklich passiert ist:

    logit(p_echt) = a + b * logit(p_modell) + c * logit(preis) + d * ln(stunden bis Schluss)

(logistische Regression mit kleiner Glaettung). Der Kalshi-Preis steckt mit drin, weil die Wahrheit meist zwischen
Modell und Markt liegt. Gelernt wird aus optionen_picks (Kalshi, abgerechnet).

Pruefung ohne Selbstbetrug: trainiert auf den aelteren 70 %, gemessen auf den neuesten 30 %, die das Modell nie
gesehen hat. Verglichen werden Brier-Wert (Treffergenauigkeit) und das Ergebnis der Calls (Vorsprung ab 8 Cent,
Preis 20 bis 80 Cent, gekauft einen Cent ueber der Liste wie im Buch) einmal mit der rohen und einmal mit der
kalibrierten Zahl. Nur wenn die kalibrierte Zahl auf den ungesehenen Daten besser ist, nutzt die Live-Engine sie
(OPT_LIVE_CAL=auto). OPT_LIVE_CAL=1 erzwingt sie, 0 schaltet sie fuer die Kaeufe ab. Seit 2.82.65 ist "auto" Standard: die Engine
waehlt Kalibrierung und Vorsprung-Schwelle selbst (auto_regel). Mitgeschrieben wird sie immer (p_cal und cal_would_buy je Live-Call).
Neu gelernt wird alle 6 Stunden, gespeichert in kv optionen_kalib.
"""
import json
import math
import os
import time

import storage
from logger import log

_C = {"ts": 0, "m": None}
NEU_H = 6


def _logit(p):
    p = min(0.995, max(0.005, float(p)))
    return math.log(p / (1 - p))


def _zeilen(pg):
    rows = pg.execute("""SELECT p.p_model, p.price, p.won, EXTRACT(EPOCH FROM (p.t_resolve - p.created)) / 3600.0,
                                p.created, f.avg_60, p.coin, p.side, p.iv, EXTRACT(EPOCH FROM p.created)
                           FROM optionen_picks p LEFT JOIN optionen_fill f ON f.pick_id = p.id
                          WHERE p.venue='kalshi' AND p.won IS NOT NULL AND p.p_model IS NOT NULL AND p.price > 0 AND p.price < 1
                          ORDER BY p.created""").fetchall()
    out = []
    _ECHT.clear()
    _META.clear()
    for p, pr, w, h, c, a60, coin, side, iv, ts in rows:
        try:
            out.append((float(p), float(pr), 1.0 if w else 0.0, max(0.05, float(h or 1))))
            _ECHT.append(float(a60) if a60 is not None else None)
            _META.append((coin, float(ts), side, float(iv) if iv is not None else None))
        except Exception:
            continue
    return out


_META = []   # seit 2.82.68: (coin, ts, side, iv) je Zeile, fuer die Chart-Merkmale


_ECHT = []   # seit 2.82.65: echter Preis der Kaeufer in den 60 s nach dem Pick, gleiche Reihenfolge wie _zeilen


def _x(p, pr, h):
    return [1.0, _logit(p), _logit(pr), math.log(h)]


def _fit(daten, l2=1.0, schritte=60, feats=None):
    """Logistische Regression (Newton-Verfahren) mit L2. -> Gewichte. feats: je Zeile Liste der Chart-Merkmale
    (seit 2.82.68), dann haengen deren Gewichte hinten an und starten bei 0 (ohne Wirkung)."""
    import numpy as np
    if feats is None:
        X = np.array([_x(p, pr, h) for p, pr, _, h in daten])
    else:
        X = np.array([_x(p, pr, h) + list(f) for (p, pr, _, h), f in zip(daten, feats)])
    y = np.array([w for _, _, w, _ in daten])
    k = X.shape[1]
    w0 = np.zeros(k)
    w0[1] = 1.0
    w = w0.copy()
    R = np.eye(k) * l2
    R[0, 0] = 0
    for _ in range(schritte):
        z = np.clip(X @ w, -30, 30)
        mu = 1 / (1 + np.exp(-z))
        g = X.T @ (mu - y) + R @ (w - w0)
        H = (X * (mu * (1 - mu))[:, None]).T @ X + R
        try:
            schritt = np.linalg.solve(H, g)
        except Exception:
            break
        w = w - schritt
        if float(np.abs(schritt).max()) < 1e-7:
            break
    return [float(v) for v in w]


def _p(w, p, pr, h, f=None):
    x = _x(p, pr, h) + (list(f) if f is not None and len(w) > 4 else [])
    z = sum(a * b for a, b in zip(w, x))
    z = max(-30.0, min(30.0, z))
    return 1 / (1 + math.exp(-z))


def _gebuehr(p):
    import optionen
    return optionen.gebuehr(p)


def _calls(daten, prob, edge=0.08, lo=0.20, hi=0.80, aufschlag=0.01, echt=None):
    """Ergebnis der Calls, wenn mit dieser Wahrscheinlichkeit entschieden wird (10 Kontrakte, Preis + 1 Cent).
    echt: Liste echter Kaufpreise (60 s danach). Dann wird zu diesem Preis entschieden und gerechnet, Picks ohne
    echten Preis fallen raus."""
    n = hits = 0
    pnl = kosten = 0.0
    for i, ((p, pr, won, h), q) in enumerate(zip(daten, prob)):
        if not (lo <= pr <= hi):
            continue
        if echt is not None:
            if i >= len(echt) or echt[i] is None or not (0 < echt[i] < 1):
                continue
            kauf = echt[i]
        else:
            kauf = min(0.99, pr + aufschlag)
        if q - kauf - _gebuehr(kauf) < edge:
            continue
        n += 1
        hits += int(won)
        k = 10 * (kauf + _gebuehr(kauf))
        kosten += k
        pnl += (10 if won else 0) - k
    return {"n": n, "hit": round(hits / n, 3) if n else None, "pnl": round(pnl, 2),
            "roi": round(pnl / kosten, 4) if kosten else None,
            "avg_price": None}


def _brier(daten, prob):
    return round(sum((q - w) ** 2 for (_, _, w, _), q in zip(daten, prob)) / max(1, len(daten)), 5)


def _eimer(daten, prob):
    """Was die Zahl gesagt hat gegen was passiert ist, in 10-Prozent-Eimern."""
    e = {}
    for (_, _, w, _), q in zip(daten, prob):
        k = min(9, int(q * 10))
        x = e.setdefault(k, [0, 0.0, 0.0])
        x[0] += 1
        x[1] += q
        x[2] += w
    return [{"said": f"{k * 10}-{k * 10 + 10}%", "n": v[0], "avg_said": round(v[1] / v[0], 3),
             "happened": round(v[2] / v[0], 3)} for k, v in sorted(e.items()) if v[0] >= 10]


def lernen(pg):
    daten = _zeilen(pg)
    if len(daten) < 300:
        return {"ok": False, "n": len(daten), "note": "needs at least 300 settled Kalshi picks"}
    cut = int(len(daten) * 0.7)
    alt, neu = daten[:cut], daten[cut:]
    w_test = _fit(alt)
    roh = [p for p, _, _, _ in neu]
    kal = [_p(w_test, p, pr, h) for p, pr, _, h in neu]
    echt_neu = _ECHT[cut:]
    # Seit 2.82.65: Vorsprung-Schwellen von 2 bis 10 Cent, roh und kalibriert, zum Listenpreis+1c und zum echten Preis
    by_edge = []
    for e in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
        by_edge.append({"edge_c": int(round(e * 100)),
                        "raw_list": _calls(neu, roh, edge=e), "cal_list": _calls(neu, kal, edge=e),
                        "raw_real": _calls(neu, roh, edge=e, echt=echt_neu), "cal_real": _calls(neu, kal, edge=e, echt=echt_neu)})
    # Seit 2.82.68: zweites Modell mit Chart-Merkmalen (Trend 60 und 15 min, echte gegen erwartete Schwankung,
    # Tageszeit). Verglichen auf GENAU denselben ungesehenen Picks (nur die mit Kerzen), zum echten Preis.
    feat_test, w_ext = None, None
    try:
        import optionen_merkmale as om
        fv = []
        for (coin, ts, side, iv) in _META:
            f = om.merkmale(pg, coin, ts, side, iv) if iv else None
            fv.append(om.vektor(f))
        ia = [i for i in range(cut) if fv[i] is not None]
        ib = [i for i in range(cut, len(daten)) if fv[i] is not None]
        if len(ia) >= 300 and len(ib) >= 100:
            alt_f, neu_f = [daten[i] for i in ia], [daten[i] for i in ib]
            we_test = _fit(alt_f, feats=[fv[i] for i in ia])
            wb_sub = _fit(alt_f)
            p_base = [_p(wb_sub, p, pr, h) for p, pr, _, h in neu_f]
            p_ext = [_p(we_test, p, pr, h, fv[i]) for (p, pr, _, h), i in zip(neu_f, ib)]
            echt_f = [_ECHT[i] for i in ib]
            be = []
            for e in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
                be.append({"edge_c": int(round(e * 100)), "base_real": _calls(neu_f, p_base, edge=e, echt=echt_f),
                           "ext_real": _calls(neu_f, p_ext, edge=e, echt=echt_f)})
            feat_test = {"n_train": len(ia), "n_test": len(ib), "brier_base": _brier(neu_f, p_base),
                         "brier_ext": _brier(neu_f, p_ext), "by_edge": be,
                         "weights": [round(v, 4) for v in we_test[4:]], "names": list(om.NAMEN)}
            alle = ia + ib
            w_ext = _fit([daten[i] for i in alle], feats=[fv[i] for i in alle])
    except Exception as e:
        log.warning(f"options calibration features: {e}")
    test = {"n": len(neu), "brier_raw": _brier(neu, roh), "brier_calibrated": _brier(neu, kal), "by_edge": by_edge,
            "features": feat_test,
            "calls_raw": _calls(neu, roh), "calls_calibrated": _calls(neu, kal),
            "buckets_raw": _eimer(neu, roh), "buckets_calibrated": _eimer(neu, kal)}
    # Seit 2.82.55: Rendite je Call statt Dollar-Summe. Die kalibrierte Zahl macht weniger Calls, die Summe war deshalb
    # fast immer kleiner, obwohl jeder Call besser war. Mindestens 20 Calls, sonst zaehlt es nicht.
    cr, cc = test["calls_raw"], test["calls_calibrated"]
    besser = (test["brier_calibrated"] < test["brier_raw"] and cc["n"] >= 20
              and (cc["roi"] or -1) > (cr["roi"] or -1) and (cc["pnl"] or 0) > 0)
    w = _fit(daten)
    m = {"ok": True, "w": w, "w_ext": w_ext, "n": len(daten), "trained": int(time.time()), "test": test,
         "better_on_unseen": besser, "since": None}
    try:
        storage.kv_set("optionen_kalib", json.dumps(m))
    except Exception as e:
        log.debug(f"kalib speichern: {e}")
    _C.update(ts=time.time(), m=m)
    log.info(f"options calibration: n={len(daten)} brier {test['brier_raw']} -> {test['brier_calibrated']}, better={besser}")
    return m


def _job(**kw):
    try:
        storage.kv_set("optionen_kalib_job", json.dumps(dict(kw, ts=int(time.time()))))
    except Exception:
        pass


def lern_faden_starten():
    """Seit 2.82.71: Neu lernen in einem eigenen Faden (Dienst crypto), nicht mehr nebenbei in modell().
    Vorher entschied der Zwischenspeicher, wer zuerst fragt, und das Neulernen blieb haengen."""
    if _C.get("faden"):
        return
    _C["faden"] = True
    import threading

    def lauf():
        import optionen
        time.sleep(90)       # erst Kerzen laden lassen
        while True:
            try:
                m = json.loads(storage.kv_get("optionen_kalib") or "null")
                faellig = (not m or not m.get("ok") or "features" not in (m.get("test") or {})
                           or time.time() - float(m.get("trained") or 0) > NEU_H * 3600)
                if faellig:
                    pg = optionen._pg()
                    if pg:
                        _job(phase="learning")
                        t0 = time.time()
                        neu = lernen(pg)
                        _job(phase="done", seconds=round(time.time() - t0, 1), ok=bool(neu and neu.get("ok")),
                             note=(neu or {}).get("note"))
            except Exception as e:
                import traceback
                _job(phase="error", error=str(e)[:200], where=traceback.format_exc()[-600:])
                log.warning(f"options calibration thread: {e}")
            time.sleep(300)
    threading.Thread(target=lauf, daemon=True, name="options-calibration").start()


def modell(pg=None):
    if _C["m"] and time.time() - _C["ts"] < 120:
        return _C["m"]
    try:
        m = json.loads(storage.kv_get("optionen_kalib") or "null")
    except Exception:
        m = None
    # Seit 2.82.71 lernt der eigene Faden (lern_faden_starten). Hier nur noch, wenn es gar kein Modell gibt.
    if pg is not None and not m:
        try:
            m = lernen(pg)
        except Exception as e:
            log.warning(f"options calibration: {e}")
            try:   # seit 2.82.70: Fehler sichtbar machen (Admin), sonst rechnet man im Dunkeln
                import traceback
                storage.kv_set("optionen_kalib_err", json.dumps({"ts": int(time.time()), "error": str(e)[:200],
                                                                  "where": traceback.format_exc()[-600:]}))
            except Exception:
                pass
    _C.update(ts=time.time(), m=m)
    return m


def auto_regel(m=None):
    """Seit 2.82.65 (ohne Variablen): die Engine waehlt selbst, an den ungesehenen Picks zum ECHTEN Preis.
    Gibt es eine Vorsprung-Schwelle, bei der die kalibrierte Zahl mit mindestens 20 Calls im Plus liegt, gilt
    Kalibrierung AN mit der Schwelle mit dem hoechsten Ergebnis. Sonst Kalibrierung AN mit 8 Cent (schuetzt, kauft
    wenig). Ohne Modell bleibt alles wie bisher."""
    m = m or modell()
    if not m or not m.get("ok"):
        return {"cal": False, "edge": None, "why": "no model yet"}
    beste = None
    for b in (m.get("test") or {}).get("by_edge") or []:
        c = b.get("cal_real") or {}
        if (c.get("n") or 0) >= 20 and (c.get("roi") or -1) > 0 and (c.get("pnl") or 0) > 0:
            if beste is None or c["pnl"] > beste[1]["pnl"]:
                beste = (b["edge_c"], c)
    # Seit 2.82.68: das Modell mit Chart-Merkmalen nur, wenn es auf denselben ungesehenen Picks zum echten Preis
    # BESSER ist als das bisherige (genauer UND mehr Ergebnis, mindestens 20 Calls im Plus). Sonst bleibt alles wie es war.
    ft = (m.get("test") or {}).get("features") or {}
    if m.get("w_ext") and ft.get("by_edge") and (ft.get("brier_ext") or 1) < (ft.get("brier_base") or 0):
        def bestes(schl):
            b_ = None
            for b in ft["by_edge"]:
                c = b.get(schl) or {}
                if (c.get("n") or 0) >= 20 and (c.get("roi") or -1) > 0 and (c.get("pnl") or 0) > 0:
                    if b_ is None or c["pnl"] > b_[1]["pnl"]:
                        b_ = (b["edge_c"], c)
            return b_
        ex, ba = bestes("ext_real"), bestes("base_real")
        if ex and (ba is None or ex[1]["pnl"] > ba[1]["pnl"]):
            return {"cal": True, "edge": ex[0] / 100.0, "ext": True,
                    "why": f"calibrated with chart signals, edge from {ex[0]}c: +{round(ex[1]['roi'] * 100, 1)}% on {ex[1]['n']} unseen calls at the real price"}
    if beste:
        return {"cal": True, "edge": beste[0] / 100.0, "ext": False,
                "why": f"calibrated, edge from {beste[0]}c: +{round(beste[1]['roi'] * 100, 1)}% on {beste[1]['n']} unseen calls at the real price"}
    return {"cal": True, "edge": 0.08, "ext": False,
            "why": "no edge level is green at the real price, calibrated with 8c to protect"}


def ext_aktiv(m=None):
    """Nutzt die Live Engine gerade das Modell mit Chart-Merkmalen?"""
    m = m or modell()
    if not m or not m.get("ok") or os.getenv("OPT_LIVE_CAL", "auto").strip().lower() != "auto":
        return False
    return bool(auto_regel(m).get("ext"))


def aktiv(m=None):
    modus = os.getenv("OPT_LIVE_CAL", "auto").strip().lower()
    if modus in ("0", "off", "false", "no"):
        return False
    m = m or modell()
    if not m or not m.get("ok"):
        return False
    if modus in ("1", "on", "true", "yes"):
        return True
    return bool(auto_regel(m)["cal"])


def p(p_model, preis, tau_h, m=None, feat=None):
    """Kalibrierte Wahrscheinlichkeit fuer eine Seite. Ohne aktives Modell die rohe Zahl.
    feat: Chart-Merkmale (Liste). Nur genutzt, wenn das Modell mit Merkmalen den Test gewonnen hat."""
    m = m or modell()
    if not aktiv(m):
        return p_model
    try:
        if feat is not None and m.get("w_ext") and ext_aktiv(m):
            return _p(m["w_ext"], p_model, preis, max(0.05, tau_h), feat)
        return _p(m["w"], p_model, preis, max(0.05, tau_h))
    except Exception:
        return p_model


def p_immer(p_model, preis, tau_h, m=None):
    """Was die kalibrierte Zahl sagen wuerde, auch wenn sie aus ist (zum Mitschreiben). None ohne Modell."""
    m = m or modell()
    if not m or not m.get("ok"):
        return None
    try:
        return _p(m["w"], p_model, preis, max(0.05, tau_h))
    except Exception:
        return None


def stand():
    m = modell() or {}
    return {"active": aktiv(m), "mode": os.getenv("OPT_LIVE_CAL", "auto"), "auto": auto_regel(m) if m.get("ok") else None,
            "n": m.get("n"), "trained": m.get("trained"),
            "better_on_unseen": m.get("better_on_unseen"), "test": m.get("test"), "note": m.get("note"),
            "weights": m.get("w"), "learn_error": _fehler(), "learn_job": _jobstand()}


def _jobstand():
    try:
        return json.loads(storage.kv_get("optionen_kalib_job") or "null")
    except Exception:
        return None


def _fehler():
    try:
        return json.loads(storage.kv_get("optionen_kalib_err") or "null")
    except Exception:
        return None
