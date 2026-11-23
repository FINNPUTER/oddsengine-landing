"""
radar.py: Narrative Radar fuer Your Engine (seit 2.68.0).

"Sag mir, wenn GTA-Coins launchen": Your Engine sucht laufend nach NEUEN Coins zu einem Thema (Namen, Orte,
Begriffe) auf Solana, Base, Ethereum, BNB Chain, Arbitrum und Robinhood Chain und schickt jeden Treffer sofort,
mit dem vollen Scan (Bundles, Dev-Wallet, Sniper) als Karte. Narrative-Coins sind meist Rugs: der Wert ist der
Scan in Sekunden, keine Kaufempfehlung.

Quellen (seit 2.69.5 drei, alle frei, ohne Schluessel): GeckoTerminal "new pools" je Chain (Solana mit pump.fun,
Base, BNB, Ethereum, Arbitrum, Robinhood), neue Torch-Launches direkt von Torch, und die DexScreener-Suche
(oeffentlich, rund 300 Abfragen je Minute erlaubt). Jeder Coin wird einmal gemeldet und einmal gescannt.
RADAR_GT / RADAR_TORCH (true) schalten eine Quelle ab, RADAR_GT_SOL_PAGES (3) Solana-Seiten je Runde,
RADAR_START_H (6) beim Start zaehlt mit, was juenger ist. Ein Thema wird fuer alle
Nutzer zusammen gelesen, Begriffe reihum (RADAR_TERMS_PER_ROUND je Runde), neue Paare juenger als
RADAR_MAX_AGE_H Stunden. Treffer nur, wenn der Begriff im Namen oder Ticker steht (Wortgrenzen); Abkuerzungen wie
GTA zaehlen nur, wenn der Name auch zum Thema passt.

Eingebaut: Thema "gta" (GTA VI: Figuren, Orte, Begriffe, dazu die Klassiker der Reihe). Eigene Themen: der
Nutzer gibt Begriffe an ("ai agents", "trump", ...).

Schalter:
  Feature "radar" (Admin)   fuer alle, ENGINE_BETA immer
  RADAR_TERMS_PER_ROUND 8   Suchbegriffe je Runde (Runde jede Minute)
  RADAR_MAX_AGE_H       6   so jung muss ein Paar sein
  RADAR_MAX_DAY         40  Treffer je Nutzer und Tag
  RADAR_SCANS_HOUR      60  volle Scans je Stunde (danach kurze Meldung mit Knopf)
"""
import json
import os
import re
import secrets
import time

import requests

import storage
from logger import log

DS = "https://api.dexscreener.com/latest/dex/search"
CHAINS = {"solana": "Solana", "base": "Base", "ethereum": "Ethereum", "bsc": "BNB Chain", "arbitrum": "Arbitrum",
          "robinhood": "Robinhood Chain"}

# Thema GTA. Stand 31.10.2026 (Trailer, Rockstar-Seiten, Presse). "suche" = Suchbegriffe bei DexScreener,
# "treffer" = was im Namen oder Ticker stehen muss, "nur_ticker" = zu allgemeine Woerter, die nur als ganzer Ticker zaehlen.
THEMEN = {
    "gta": {
        "name": "GTA VI",
        "suche": ["gta", "gta6", "gta vi", "grand theft", "vice city", "leonida", "lucia", "jason duval", "lucia caminos",
                  "cal hampton", "boobie ike", "drequan", "real dimez", "raul bautista", "brian heder", "port gellhorn",
                  "grassrivers", "ambrosia leonida", "mount kalaga", "leonida keys", "little cuba", "rockstar",
                  "vercetti", "los santos", "san andreas", "liberty city", "grove street"],
        "treffer": ["grand theft auto", "grand theft", "gta 6", "gta vi", "gta6", "gtavi", "gta", "vice city", "vicecity",
                    "leonida", "lucia caminos", "caminos", "jason duval", "duval", "cal hampton", "boobie ike", "boobie",
                    "dre quan", "drequan", "dre'quan", "real dimez", "dimez", "raul bautista", "brian heder", "heder",
                    "bae luxe", "port gellhorn", "gellhorn", "grassrivers", "mount kalaga", "kalaga", "leonida keys",
                    "little cuba", "tisha wocka", "vc port", "rockstar games", "rockstar", "tommy vercetti", "vercetti",
                    "los santos", "san andreas", "liberty city", "grove street", "niko bellic", "trevor philips",
                    "big smoke", "cj grove"],
        "nur_ticker": ["lucia", "jason", "cal", "boobie", "raul", "brian", "ambrosia", "tommy", "cj", "niko", "trevor"],
        # GTA ist auch eine beliebige Abkuerzung ("Greatest Token Alive"): als Ticker nur mit passendem Namen
        "abk": {"gta": ["grand", "theft", "auto", "gta", "vice", "leonida", "rockstar", "lucia", "jason"]},
    },
}


def _f(name, std):
    try:
        return float(os.getenv(name, str(std)).strip() or std)
    except Exception:
        return std


def _j(k, std):
    try:
        v = storage.kv_get(k)
        return json.loads(v) if v else std
    except Exception:
        return std


def _set(k, v):
    try:
        storage.kv_set(k, json.dumps(v, separators=(",", ":")))
    except Exception as e:
        log.debug(f"radar kv {k}: {e}")


def _norm(s):
    s = str(s or "").lower().replace("$", " ").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return f" {s.strip()} "


def _thema(t):
    """Eingebautes Thema oder eigene Begriffe -> {"name","suche","treffer","nur_ticker","abk"}"""
    if isinstance(t, dict):
        return t
    k = str(t or "").strip().lower().replace(" ", "")
    if k in ("gta", "gta6", "gtavi", "grandtheftauto", "gta5", "gtav"):
        return THEMEN["gta"]
    return None


def passt(thema, name, symbol):
    """Steht ein Begriff des Themas im Namen oder Ticker? -> der Begriff oder None"""
    n, s = _norm(name), _norm(symbol)
    beides = n + s
    for w in thema.get("abk", {}):
        if f" {w} " in s or s.strip().startswith(w):
            if any(f" {x}" in n for x in thema["abk"][w]):
                return w
    for b in sorted(thema["treffer"], key=len, reverse=True):
        nb = _norm(b)
        if b in thema.get("abk", {}):
            continue
        if nb in beides or nb.replace(" ", "") in beides.replace(" ", "") and len(nb) > 8:
            return b
    for b in thema.get("nur_ticker", []):
        if s.strip() == b:
            return b
    return None


def _suchen(begriff):
    try:
        r = requests.get(DS, params={"q": begriff}, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        return r.json().get("pairs") or []
    except Exception as e:
        log.debug(f"radar suche {begriff}: {e}")
        return []


def runde_thema(key, thema):
    """Ein Stueck Suchbegriffe lesen. -> neue Treffer [{chain, address, pair, name, symbol, term, age_h, ...}]"""
    st = _j("radar_t_" + key, {"i": 0, "seen": {}, "ts": 0, "runden": 0})
    jetzt = time.time()
    n = int(_f("RADAR_TERMS_PER_ROUND", 8))
    begriffe = thema["suche"]
    stueck = [begriffe[(st["i"] + k) % len(begriffe)] for k in range(min(n, len(begriffe)))]
    st["i"] = (st["i"] + n) % len(begriffe)
    # die erste volle Runde ueber alle Begriffe nur merken (was schon da ist, gilt als gesehen)
    erst = st.get("runden", 0) * n < len(begriffe)
    st["runden"] = st.get("runden", 0) + 1
    max_alt = _f("RADAR_MAX_AGE_H", 6) * 3600
    neu = []

    def pruefen(k):
        """Ein Kandidat aus irgendeiner Quelle: gleiche Regeln fuer alle (neu, Name passt, einmal je Coin)."""
        ch, adr = k["chain"], str(k.get("address") or "")
        if ch not in CHAINS or not adr:
            return
        key_t = f"{ch}:{adr.lower() if adr.startswith('0x') else adr}"
        if key_t in st["seen"]:
            return
        alt = jetzt - (k.get("created") or 0)
        if alt > max_alt or alt < 0:
            return
        term = passt(thema, k.get("name"), k.get("symbol"))
        if not term:
            return
        st["seen"][key_t] = int(jetzt)
        if erst and not k.get("frisch"):
            return   # Start: was schon da ist, gilt als gesehen (ausser ganz frische, siehe RADAR_START_H)
        _loggen(key, {"ts": int(jetzt), "chain": ch, "address": adr, "symbol": k.get("symbol"),
                      "name": k.get("name"), "term": term, "mc": k.get("mc"), "liq": k.get("liq"),
                      "url": k.get("url"), "source": k.get("source"), "created": int(k.get("created") or jetzt)})
        neu.append({"chain": ch, "address": adr, "pair": k.get("pair"), "name": k.get("name"),
                    "symbol": k.get("symbol"), "term": term, "age_h": round(alt / 3600, 1),
                    "mc": k.get("mc"), "liq": k.get("liq"), "buys_1h": k.get("buys_1h"),
                    "sells_1h": k.get("sells_1h"), "dex": k.get("dex"), "url": k.get("url"),
                    "source": k.get("source")})

    start_h = _f("RADAR_START_H", 6) * 3600
    # Quelle 1 und 2 (seit 2.69.5): alle neuen Pools je Chain (GeckoTerminal) und neue Torch-Launches.
    # Sie haengen nicht an der Suche, deshalb rutscht kein frischer Coin durch, egal wie voll ein Begriff ist.
    for k in _neue_pools() + _torch_neu():
        k["frisch"] = (jetzt - (k.get("created") or 0)) < start_h
        pruefen(k)
    # Quelle 3: DexScreener-Suche nach den Begriffen (faengt auf, was die Listen verpassen)
    for b in stueck:
        for p in _suchen(b):
            bt = p.get("baseToken") or {}
            tx = ((p.get("txns") or {}).get("h1") or {})
            c = (p.get("pairCreatedAt") or 0) / 1000
            pruefen({"chain": p.get("chainId"), "address": bt.get("address"), "pair": p.get("pairAddress"),
                     "name": bt.get("name"), "symbol": bt.get("symbol"), "created": c,
                     "mc": p.get("marketCap") or p.get("fdv"), "liq": (p.get("liquidity") or {}).get("usd"),
                     "buys_1h": tx.get("buys"), "sells_1h": tx.get("sells"), "dex": p.get("dexId"),
                     "url": p.get("url"), "source": "dexscreener", "frisch": (jetzt - c) < start_h})
    # gesehene 7 Tage behalten
    st["seen"] = {k: v for k, v in st["seen"].items() if jetzt - v < 7 * 86400}
    st["ts"] = int(jetzt)
    _set("radar_t_" + key, st)
    return neu


def _usd(v):
    try:
        v = float(v)
    except Exception:
        return "?"
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}K" if v >= 1e3 else f"${v:.0f}"


def _kurz(t, thema_name, eigen=False):
    kopf = (f"📡 New coin for your radar ({thema_name})" if eigen else f"📡 New {thema_name} coin")
    teile = [f"matched: {t['term']}", f"{t['age_h']} h old"]
    if t.get("mc"):
        teile.append(f"MC {_usd(t['mc'])}")
    if t.get("liq"):
        teile.append(f"liquidity {_usd(t['liq'])}")
    if t.get("buys_1h") is not None:
        teile.append(f"1h buys/sells {t['buys_1h']}/{t['sells_1h']}")
    return (f"{kopf}: ${str(t['symbol']).lstrip('$')} ({t['name']}) on {CHAINS[t['chain']]}\n"
            + " · ".join(teile) + f"\n{t['address']}")


_SCANS = {}


def _karte(t):
    """Voller Scan einmal je Coin (fuer alle Nutzer), gedeckelt je Stunde. -> (text, html, knoepfe) oder None"""
    k = f"{t['chain']}:{t['address']}"
    if k in _SCANS:
        return _SCANS[k]
    stunde = int(time.time() // 3600)
    zahl = sum(1 for v in _SCANS.values() if v and v[3] == stunde)
    if zahl >= int(_f("RADAR_SCANS_HOUR", 60)):
        return None
    try:
        import scan
        # DexScreener-Links liest der Scan direkt, sonst Chain-Hinweis plus Adresse (GeckoTerminal, Torch)
        u = t.get("url") or ""
        r = scan.analyse(u if "dexscreener.com" in u else f"{t['chain']}:{t['address']}")
        if not r.get("ok"):
            _SCANS[k] = None
            return None
        _SCANS[k] = (r.get("card"), r.get("card_html"), scan.knoepfe(r), stunde,
                     (r.get("verdict") or {}).get("level"))
    except Exception as e:
        log.debug(f"radar scan {k}: {e}")
        _SCANS[k] = None
    if len(_SCANS) > 3000:
        for x in list(_SCANS)[:1000]:
            _SCANS.pop(x, None)
    return _SCANS[k]


# ── Nutzer ───────────────────────────────────────────────────────────────

def einstellen(adr, action="add", theme=None, keywords=None, chains=None, alert_id=None):
    import motor
    z = motor.zustand(adr)
    liste = z.setdefault("radar", [])
    action = (action or "add").lower()
    if action == "list":
        return {"ok": True, "radars": [{k: v for k, v in r.items() if k != "custom"} for r in liste]}
    if action == "remove":
        vorher = len(liste)
        z["radar"] = [r for r in liste if alert_id not in (r["id"], "all") and not (theme and str(theme).lower() in str(r.get("name")).lower())]
        motor._zustand_speichern(adr, z)
        return {"ok": True, "removed": vorher - len(z["radar"])}
    if len(liste) >= 5:
        return {"ok": False, "error": "You have 5 radars already. Remove one first."}
    # Seit 2.69.9: der GTA-Radar ist fuer alle gratis (auch Gaeste und Probelauf), eigene Themen brauchen Guthaben
    if not _bezahlt(adr) and _thema(theme) is not THEMEN["gta"]:
        return {"ok": False, "error": "Radars with your own words unlock after a top up with $ODDS on engine.oddsengine.live. "
                                      "The GTA VI radar is free: just say 'tell me when GTA coins launch'."}
    ketten = [c for c in (chains or []) if c in CHAINS] or list(CHAINS)
    th = _thema(theme)
    if th:
        r = {"id": secrets.token_hex(3), "theme": [k for k, v in THEMEN.items() if v is th][0], "name": th["name"],
             "chains": ketten, "created": int(time.time())}
    else:
        worte = [w.strip() for w in (keywords or ([theme] if theme else [])) if w and len(w.strip()) >= 3][:15]
        if not worte:
            return {"ok": False, "error": "Tell me the theme or a few words to watch for (names, places, terms)."}
        r = {"id": secrets.token_hex(3), "theme": "custom", "name": ", ".join(worte[:3]) + ("..." if len(worte) > 3 else ""),
             "custom": {"name": worte[0], "suche": worte, "treffer": worte, "nur_ticker": [], "abk": {}},
             "chains": ketten, "created": int(time.time())}
    liste.append(r)
    motor._zustand_speichern(adr, z)
    th2 = th or r["custom"]
    return {"ok": True, "radar": {k: v for k, v in r.items() if k != "custom"},
            "watching_for": th2["treffer"][:60],
            "note": ("New coins only (launched in the last hours), on " + ", ".join(CHAINS[c] for c in ketten)
                     + ". Every hit comes to Telegram with the full scan. Reading is free. Most narrative coins are "
                       "rugs: the scan is the point, not a buy signal.")}


def runde():
    """Aus motor.tick, jede Minute. Liest jedes benutzte Thema ein Stueck weiter und meldet Treffer."""
    import motor
    nutzer = []
    for a in motor._nutzer():
        z = motor.zustand(a)
        if z.get("radar") and motor._schalter("radar", a):
            nutzer.append(a)
    themen = {}
    # Seit 2.69.3: GTA laeuft immer mit (RADAR_GTA_ALWAYS), auch ohne Nutzer. So entstehen die Zahlen fuer
    # gta_now und /v1/gta (wie viele neue GTA-Coins, wie viele davon rot) schon vor dem ersten Abo.
    if _gta_immer():
        themen["gta"] = THEMEN["gta"]
    if not nutzer and not themen:
        return 0
    for a in nutzer:
        for r in motor.zustand(a)["radar"]:
            if r["theme"] == "custom":
                k = "c_" + re.sub(r"[^a-z0-9]+", "_", "|".join(sorted(x.lower() for x in r["custom"]["suche"])))[:60]
                themen[k] = r["custom"]
            else:
                themen[r["theme"]] = THEMEN[r["theme"]]
    treffer = {k: runde_thema(k, th) for k, th in themen.items()}
    for t in treffer.get("gta") or []:
        # Urteil fuer die Statistik festhalten (der Scan ist gecacht, die Nutzer bekommen denselben)
        c = _karte(t)
        if c and len(c) > 4 and c[4]:
            _urteil_setzen("gta", t, c[4])
    if not nutzer or not any(treffer.values()):
        return 0
    tag = time.strftime("%Y%m%d", time.gmtime())
    gesendet = 0
    for a in nutzer:
        z = motor.zustand(a)
        bezahlt = motor._darf(a)
        nur_gta = all(r.get("theme") == "gta" for r in z.get("radar") or [])
        if not bezahlt and not (nur_gta and motor.offen(a)):
            continue
        zahl = z["prefs"].get("radar_n") if z["prefs"].get("radar_day") == tag else 0
        grenze = int(_f("RADAR_MAX_DAY", 40)) if bezahlt else int(_f("RADAR_FREE_MAX_DAY", 15))
        schon = set()
        for r in z["radar"]:
            k = r["theme"] if r["theme"] != "custom" else "c_" + re.sub(r"[^a-z0-9]+", "_", "|".join(sorted(x.lower() for x in r["custom"]["suche"])))[:60]
            for t in treffer.get(k) or []:
                if zahl >= grenze:
                    break
                tk = f"{t['chain']}:{t['address']}"
                if tk in schon or t["chain"] not in r["chains"]:
                    continue
                schon.add(tk)
                kopf = _kurz(t, r["name"], r["theme"] == "custom")
                c = _karte(t)
                if c and (c[1] or c[0]):
                    motor.senden(a, kopf)
                    motor.senden(a, c[1] or c[0], c[2], html=bool(c[1]))
                else:
                    motor.senden(a, kopf, [[("🔎 Scan it", motor.frage_knopf(f"Scan {t['address']}"))]]
                                 + ([[("Chart", t["url"])]] if t.get("url") else []))
                zahl += 1
                gesendet += 1
        if zahl:
            z2 = motor.zustand(a)
            z2["prefs"]["radar_day"], z2["prefs"]["radar_n"] = tag, zahl
            motor._zustand_speichern(a, z2)
    return gesendet


# ── Seit 2.69.3: Treffer-Log, GTA-Lage, Polymarket-Quoten ───────────────────────

GTA_RELEASE = os.getenv("GTA_RELEASE_DATE", "2026-11-19")


def _gta_immer():
    if os.getenv("RADAR_GTA_ALWAYS", "true").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        import features
        return features.an("radar")
    except Exception:
        return False


def _loggen(key, eintrag):
    log_ = _j("radar_log_" + key, [])
    jetzt = time.time()
    log_ = [x for x in log_ if jetzt - x.get("ts", 0) < 14 * 86400][-1999:]
    log_.append(eintrag)
    _set("radar_log_" + key, log_)


def _urteil_setzen(key, t, level):
    log_ = _j("radar_log_" + key, [])
    for x in reversed(log_):
        if x.get("chain") == t["chain"] and x.get("address") == t["address"]:
            if x.get("level") == level:
                return
            x["level"] = level
            _set("radar_log_" + key, log_)
            return


_PM = {"ts": 0, "daten": None}


def _pm_suche(q):
    try:
        r = requests.get("https://gamma-api.polymarket.com/public-search",
                         params={"q": q, "limit_per_type": 25, "events_status": "active"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        return r.json().get("events") or []
    except Exception as e:
        log.debug(f"gta polymarket {q}: {e}")
        return []


def _preis(m):
    try:
        p = m.get("outcomePrices")
        p = json.loads(p) if isinstance(p, str) else p
        return float(p[0])
    except Exception:
        return None


def gta_quoten():
    """Alle offenen GTA-VI-Maerkte auf Polymarket mit den aktuellen Quoten. 5 Minuten Cache."""
    if _PM["daten"] is not None and time.time() - _PM["ts"] < 300:
        return _PM["daten"]
    gesehen, aus = set(), []
    for q in ("GTA VI", "GTA 6", "Grand Theft Auto"):
        for ev in _pm_suche(q):
            slug = ev.get("slug")
            titel = str(ev.get("title") or "")
            if not slug or slug in gesehen or ev.get("closed"):
                continue
            if not re.search(r"\bgta\b|grand theft auto", titel.lower()):
                continue
            gesehen.add(slug)
            maerkte = []
            alle_m = ev.get("markets") or []
            for m in alle_m:
                if m.get("closed"):
                    continue
                pr = _preis(m)
                if pr is None:
                    continue
                maerkte.append({"option": m.get("groupItemTitle") or ("Yes" if len(alle_m) == 1 else m.get("question")) or "Yes",
                                "chance_pct": round(pr * 100, 1)})
            if not maerkte:
                continue
            maerkte.sort(key=lambda x: -x["chance_pct"])
            try:
                vol = round(float(ev.get("volume") or 0))
            except Exception:
                vol = 0
            aus.append({"title": titel, "volume_usd": vol, "ends": str(ev.get("endDate") or "")[:10],
                        "top": maerkte[:4], "options": len(maerkte),
                        "url": f"https://polymarket.com/event/{slug}"})
    aus.sort(key=lambda x: -x["volume_usd"])
    _PM["daten"], _PM["ts"] = aus, time.time()
    return aus


def gta_lage():
    """Alles zum GTA-VI-Hype auf einen Blick: Countdown, Polymarket-Quoten, Radar-Zahlen."""
    from datetime import date
    try:
        tage = (date.fromisoformat(GTA_RELEASE) - date.today()).days
    except Exception:
        tage = None
    log_ = _j("radar_log_gta", [])
    jetzt = time.time()

    def fenster(sek):
        xs = [x for x in log_ if jetzt - x.get("ts", 0) < sek]
        bewertet = [x for x in xs if x.get("level")]
        ketten = {}
        for x in xs:
            ketten[CHAINS.get(x["chain"], x["chain"])] = ketten.get(CHAINS.get(x["chain"], x["chain"]), 0) + 1
        return {"new_coins": len(xs), "scanned": len(bewertet),
                "by_chain": dict(sorted(ketten.items(), key=lambda kv: -kv[1]))}

    # Seit 2.69.8: Alter ab dem Launch des Coins, nicht ab dem Moment, in dem wir ihn gefunden haben
    letzte = sorted(log_, key=lambda x: -(x.get("created") or x.get("ts", 0)))[:8]
    return {"release": GTA_RELEASE, "days_left": tage,
            "radar_running": _gta_immer(),
            "radar_since": time.strftime("%Y-%m-%d", time.gmtime(min((x["ts"] for x in log_), default=jetzt))),
            "last_24h": fenster(86400), "last_7d": fenster(7 * 86400),
            "latest": [{"symbol": x.get("symbol"), "name": x.get("name"), "chain": CHAINS.get(x["chain"], x["chain"]),
                        "matched": x.get("term"), "scanned": bool(x.get("level")), "mc_usd": x.get("mc"),
                        "hours_ago": round((jetzt - (x.get("created") or x["ts"])) / 3600, 1), "address": x.get("address")} for x in letzte],
            "polymarket": gta_quoten(),
            "track_record": bilanz("gta"),
            "note": ("Radar counts NEW coins whose name or ticker matches GTA VI (characters, places, terms) on Solana, "
                     "Base, Ethereum, BNB Chain, Arbitrum and Robinhood Chain. Every coin gets a full scan with on-chain facts, no rating. None of "
                     "these coins has any link to Rockstar. Polymarket chances are live prices, not our forecast.")}


# ── Seit 2.69.5: weitere Quellen fuer den Radar ─────────────────────────────

GT = "https://api.geckoterminal.com/api/v2"
GT_NETZ = {"solana": "solana", "base": "base", "ethereum": "eth", "bsc": "bsc", "arbitrum": "arbitrum",
           "robinhood": "robinhood"}
_GTC = {"ts": 0.0, "pools": [], "runde": 0}


def _gt_zeit(s_):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s_).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def _gt_seite(netz, seite):
    try:
        r = requests.get(f"{GT}/networks/{netz}/new_pools", params={"page": seite, "include": "base_token"},
                         headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code == 429:
            log.debug("radar geckoterminal: rate limit")
            return []
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.debug(f"radar geckoterminal {netz}: {e}")
        return []
    toks = {x.get("id"): x.get("attributes") or {} for x in d.get("included") or []}
    ketten = {v: k for k, v in GT_NETZ.items()}
    aus = []
    for p in d.get("data") or []:
        a = p.get("attributes") or {}
        rel = p.get("relationships") or {}
        bid = ((rel.get("base_token") or {}).get("data") or {}).get("id")
        t = toks.get(bid) or {}
        tx = ((a.get("transactions") or {}).get("h1") or {})
        aus.append({"chain": ketten.get(netz), "address": t.get("address") or (bid or "").split("_", 1)[-1],
                    "pair": a.get("address"), "name": t.get("name"), "symbol": t.get("symbol"),
                    "created": _gt_zeit(a.get("pool_created_at")),
                    "mc": a.get("market_cap_usd") or a.get("fdv_usd"), "liq": a.get("reserve_in_usd"),
                    "buys_1h": tx.get("buys"), "sells_1h": tx.get("sells"),
                    "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
                    "url": f"https://www.geckoterminal.com/{netz}/pools/{a.get('address')}", "source": "geckoterminal"})
    return aus


def _neue_pools():
    """Neue Pools je Chain, hoechstens einmal je Minute fuer alle Themen zusammen.
    Solana jede Runde RADAR_GT_SOL_PAGES Seiten (pump.fun macht dort die meisten), die anderen Chains jede
    zweite Runde eine Seite. GeckoTerminal erlaubt frei rund 30 Abfragen je Minute, der Scan braucht auch welche."""
    if os.getenv("RADAR_GT", "true").strip().lower() in ("0", "false", "no", "off"):
        return []
    if time.time() - _GTC["ts"] < 55:
        return _GTC["pools"]
    _GTC["runde"] += 1
    pools = []
    for seite in range(1, int(_f("RADAR_GT_SOL_PAGES", 3)) + 1):
        pools += _gt_seite("solana", seite)
    if _GTC["runde"] % 2 == 1:
        for k in ("base", "bsc", "ethereum", "arbitrum", "robinhood"):
            pools += _gt_seite(GT_NETZ[k], 1)
    _GTC["ts"], _GTC["pools"] = time.time(), pools
    return pools


def _torch_neu():
    """Neue Launches direkt von Torch (Robinhood Chain), auch auf der Bonding Curve."""
    if os.getenv("RADAR_TORCH", "true").strip().lower() in ("0", "false", "no", "off"):
        return []
    try:
        import onchain
        liste = onchain._torch_liste()
    except Exception as e:
        log.debug(f"radar torch: {e}")
        return []
    aus = []
    for x in liste or []:
        a = str(x.get("address") or "")
        if not a:
            continue
        aus.append({"chain": "robinhood", "address": a, "name": x.get("name"), "symbol": x.get("symbol"),
                    "created": _gt_zeit(x.get("createdAt")), "mc": x.get("marketCapUsd"),
                    "liq": x.get("liquidityUsd"), "dex": "torch",
                    "url": "https://app.torchpad.fun/trade/robinhood/" + a, "source": "torch"})
    return aus


def _bezahlt(adr):
    """Hat das Konto Guthaben (oder ist Beta)? Gaeste und Probelauf: nein."""
    try:
        import motor
        if motor.gast(adr):
            return False
        return bool(motor.bereit(adr).get("ready"))
    except Exception:
        return False


# ── Seit 2.74.0: Nachverfolgung. Was ist aus den gefundenen Coins geworden? ─────────────
#
# Jeder Treffer im Log wird weiter beobachtet: Marketcap und Liquiditaet nach 1 h, 6 h, 24 h und 3 Tagen,
# dazwischen laufend (alle 5 Minuten am ersten Tag, danach stuendlich), dazu der Hoechststand aus den
# 15-Minuten-Kerzen von GeckoTerminal. Daraus wird je Coin ein Ergebnis:
#   runner  Hoechststand mindestens RADAR_RUNNER_X (3) mal der Fundwert und mindestens RADAR_RUNNER_MIN_MC ($50K)
#   dead    Marketcap unter RADAR_DEAD_MC ($5K), unter 10 % vom Fundwert, Liquiditaet unter $1K oder kein Pool mehr
#   flat    alles dazwischen
# Ab 24 h gilt das Ergebnis vorlaeufig, ab 72 h endgueltig. Dazu bekommt jeder Coin nach 15 Minuten ein zweites
# Scan-Urteil (level_deep), wenn der Tiefen-Scan (Buendel, Weiterleitungen) fertig ist. Beides zusammen zeigt,
# wie gut red, amber und green wirklich trennen. Rescans laufen in einem eigenen Faden, damit der Takt nicht haengt.

PUNKTE = (1, 6, 24, 72)


def _im_fenster(alter, h):
    """Messpunkt h nur nahe an h Stunden eintragen (bei alten Coins aus dem Log keine falschen 1-h-Werte)."""
    return h * 3600 <= alter <= h * 3600 + max(1800, h * 900)
_VF = {"ts": 0.0}
_NEU_SCAN = {}            # key -> (level, pending) aus dem Rescan-Faden
_RESCAN_LAEUFT = set()
_RESCAN_STUNDE = {"h": 0, "n": 0}


def _vf_key(x):
    a = str(x.get("address") or "")
    return f"{x.get('chain')}:{a.lower() if a.startswith('0x') else a}"


def _ds_stapel(chain, adressen):
    """DexScreener: bestes Paar (meiste Liquiditaet) je Token, 30 je Abfrage. -> {adresse_klein: paar} (fehlt = kein Paar)"""
    aus, ok = {}, set()
    for i in range(0, len(adressen), 30):
        teil = adressen[i:i + 30]
        try:
            r = requests.get(f"https://api.dexscreener.com/tokens/v1/{chain}/" + ",".join(teil),
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            log.debug(f"radar verfolgen ds {chain}: {e}")
            continue
        for a in teil:
            ok.add(a.lower())
        for p in d if isinstance(d, list) else []:
            a = str((p.get("baseToken") or {}).get("address") or "").lower()
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            alt = aus.get(a)
            if alt is None or liq > float((alt.get("liquidity") or {}).get("usd") or 0):
                aus[a] = p
    return aus, ok


def _pool_aus_url(u):
    u = str(u or "").rstrip("/")
    if "geckoterminal.com" in u and "/pools/" in u:
        return u.rsplit("/pools/", 1)[1]
    if "dexscreener.com" in u:
        return u.rsplit("/", 1)[-1]
    return None


def _gt_hoch(chain, pool, seit):
    """Hoechster Preis (USD) seit seit aus 15-Minuten-Kerzen. None bei Fehler oder ohne Kerzen."""
    netz = GT_NETZ.get(chain)
    if not netz or not pool:
        return None
    try:
        r = requests.get(f"{GT}/networks/{netz}/pools/{pool}/ohlcv/minute",
                         params={"aggregate": 15, "limit": 300, "currency": "usd", "token": "base"},
                         headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code == 429:
            return "limit"
        if r.status_code == 404:
            return None
        r.raise_for_status()
        kerzen = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    except Exception as e:
        log.debug(f"radar verfolgen gt {pool}: {e}")
        return None
    hoch = [float(k[2]) for k in kerzen if len(k) > 2 and k[0] >= seit - 900 and k[2]]
    return max(hoch) if hoch else None


def _ergebnis(x, jetzt):
    """runner / dead / flat, oder None solange der Coin juenger als 24 h ist und noch nichts entschieden ist."""
    alter = jetzt - (x.get("created") or x.get("ts") or jetzt)
    basis = x.get("mc") or (x.get("chk") or {}).get("1", {}).get("mc")
    nun = x.get("now") or {}
    spitze = x.get("peak") or 0
    x_min = _f("RADAR_RUNNER_X", 3)
    runner = bool(basis and spitze >= basis * x_min and spitze >= _f("RADAR_RUNNER_MIN_MC", 50_000))
    mc_nun = nun.get("mc")
    tot = nun.get("gone") or (mc_nun is not None and (mc_nun < _f("RADAR_DEAD_MC", 5_000) or (basis and mc_nun < basis * 0.1))) \
        or (nun.get("liq") is not None and nun["liq"] < 1_000 and not nun.get("curve"))
    if runner:
        return "runner"
    if alter < 24 * 3600:
        return "dead" if tot and alter >= 3600 else None
    if mc_nun is None and not nun.get("gone"):
        return None
    return "dead" if tot else "flat"


def _rescan_faden(jobs):
    import scan
    for k, ziel in jobs:
        try:
            r = scan.analyse(ziel)
            v = r.get("verdict") or {} if r.get("ok") else {}
            _NEU_SCAN[k] = (v.get("level"), bool(v.get("pending")))
        except Exception as e:
            log.debug(f"radar rescan {k}: {e}")
            _NEU_SCAN[k] = (None, None)
        finally:
            _RESCAN_LAEUFT.discard(k)


def verfolgen(key="gta"):
    """Aus motor (Radar-Takt), hoechstens alle 5 Minuten. Liest, schreibt das Log einmal zurueck."""
    jetzt = time.time()
    if jetzt - _VF["ts"] < _f("RADAR_TRACK_EVERY_S", 300):
        return 0
    _VF["ts"] = jetzt
    log_ = _j("radar_log_" + key, [])
    if not log_:
        return 0
    # 1) Ergebnisse aus dem Rescan-Faden uebernehmen
    for x in log_:
        k = _vf_key(x)
        if k in _NEU_SCAN:
            lv, pend = _NEU_SCAN.pop(k)
            if lv:
                x["level_deep"], x["deep_pending"] = lv, pend
            x["rescan"] = int(jetzt)
    # 2) faellige Coins je Chain sammeln
    faellig = {}
    for x in log_:
        if x.get("final"):
            continue
        alter = jetzt - (x.get("created") or x["ts"])
        letzt = (x.get("now") or {}).get("ts", 0)
        takt = 300 if alter < 24 * 3600 else 3600
        punkt_offen = any(_im_fenster(alter, h) and str(h) not in (x.get("chk") or {}) for h in PUNKTE)
        if punkt_offen or jetzt - letzt >= takt - 30:
            faellig.setdefault(x["chain"], []).append(x)
    geaendert = 0
    for ch, xs in faellig.items():
        adr = list({str(x["address"]) for x in xs})
        paare, gelesen = _ds_stapel(ch, adr)
        for x in xs:
            a = str(x["address"]).lower()
            if a not in gelesen:
                continue          # Abfrage fehlgeschlagen, naechste Runde
            p = paare.get(a)
            alter = jetzt - (x.get("created") or x["ts"])
            if p:
                mc = p.get("marketCap") or p.get("fdv")
                mc = float(mc) if mc not in (None, "") else None
                liq = (p.get("liquidity") or {}).get("usd")
                preis = float(p.get("priceUsd") or 0)
                nun = {"mc": mc, "liq": float(liq) if liq is not None else None, "ts": int(jetzt),
                       "curve": p.get("dexId") in ("pumpfun", "launchlab", "moonshot", "meteoradbc", "boop", "bonk", "believe")}
                if mc and preis:
                    x["supply"] = mc / preis
                x.setdefault("pools", [])
                if p.get("pairAddress") and p["pairAddress"] not in x["pools"]:
                    x["pools"].append(p["pairAddress"])
                if mc:
                    x["peak"] = max(x.get("peak") or 0, mc)
            else:
                # kein Paar mehr: nur als weg zaehlen, wenn wir den Coin vorher schon mal gelesen haben
                if not x.get("now"):
                    x["now"] = {"mc": None, "liq": None, "ts": int(jetzt), "missing": True}
                    continue
                nun = {"mc": 0.0, "liq": 0.0, "ts": int(jetzt), "gone": True}
            x["now"] = nun
            chk = x.setdefault("chk", {})
            for h in PUNKTE:
                if _im_fenster(alter, h) and str(h) not in chk:
                    chk[str(h)] = {"mc": nun.get("mc"), "liq": nun.get("liq")}
            geaendert += 1
    # 3) Hoechststand aus den Kerzen: einmal ab 6 h, einmal ab 72 h (dann ist er endgueltig)
    rest = int(_f("RADAR_GT_PEAK_ROUND", 12))
    for x in sorted(log_, key=lambda x: x["ts"]):
        if rest <= 0:
            break
        alter = jetzt - (x.get("created") or x["ts"])
        stufe = 72 if alter >= 72 * 3600 else 6 if alter >= 6 * 3600 else 0
        if not stufe or (x.get("peak_gt") or 0) >= stufe or not x.get("supply"):
            continue
        pools = list(dict.fromkeys([q for q in [_pool_aus_url(x.get("url"))] + (x.get("pools") or []) if q]))[:2]
        hoch, limit = None, False
        for q in pools:
            rest -= 1
            h = _gt_hoch(x["chain"], q, x.get("created") or x["ts"])
            if h == "limit":
                limit = True
                break
            if h:
                hoch = max(hoch or 0, h)
        if limit:
            break
        if hoch:
            x["peak"] = max(x.get("peak") or 0, hoch * x["supply"])
        x["peak_gt"] = stufe
    # 4) Ergebnis setzen, nach 72 h festschreiben
    for x in log_:
        if x.get("final"):
            continue
        e = _ergebnis(x, jetzt)
        if e:
            x["outcome"] = e
        if jetzt - (x.get("created") or x["ts"]) >= 72 * 3600 and (x.get("peak_gt") or 0) >= 72 and e:
            x["final"] = True
    # 5) Rescan nach 15 Minuten (Tiefen-Scan fertig), gedeckelt je Stunde, im eigenen Faden
    stunde = int(jetzt // 3600)
    if _RESCAN_STUNDE["h"] != stunde:
        _RESCAN_STUNDE.update(h=stunde, n=0)
    jobs = []
    for x in sorted(log_, key=lambda x: -x["ts"]):
        alter = jetzt - x["ts"]
        k = _vf_key(x)
        if x.get("rescan") or alter < 900 or alter > 6 * 3600 or k in _RESCAN_LAEUFT:
            continue
        if _RESCAN_STUNDE["n"] + len(jobs) >= int(_f("RADAR_RESCANS_HOUR", 30)):
            break
        u = x.get("url") or ""
        jobs.append((k, u if "dexscreener.com" in u else f"{x['chain']}:{x['address']}"))
    if jobs:
        import threading
        _RESCAN_STUNDE["n"] += len(jobs)
        _RESCAN_LAEUFT.update(k for k, _ in jobs)
        threading.Thread(target=_rescan_faden, args=(jobs,), daemon=True).start()
    _set("radar_log_" + key, log_)
    return geaendert


def bilanz(key="gta", tage=7, n=8):
    """Was ist aus den gefundenen Coins geworden? Nur Coins, die mindestens 24 h alt sind, zaehlen in die Quoten."""
    jetzt = time.time()
    log_ = [x for x in _j("radar_log_" + key, []) if jetzt - x.get("ts", 0) < tage * 86400]
    reif = [x for x in log_ if x.get("outcome") and jetzt - (x.get("created") or x["ts"]) >= 24 * 3600]
    zahl = lambda xs, e: sum(1 for x in xs if x.get("outcome") == e)

    def stufen(feld):
        m = {}
        for x in reif:
            lv = x.get(feld)
            if not lv:
                continue
            z = m.setdefault(lv, {"coins": 0, "dead": 0, "runner": 0, "flat": 0})
            z["coins"] += 1
            z[x["outcome"]] += 1
        for z in m.values():
            z["dead_pct"] = round(z["dead"] / z["coins"] * 100, 1)
            z["runner_pct"] = round(z["runner"] / z["coins"] * 100, 1)
        return m

    def zeile(x):
        b = x.get("mc") or 0
        nun = (x.get("now") or {}).get("mc")
        return {"symbol": x.get("symbol"), "name": x.get("name"), "chain": CHAINS.get(x["chain"], x["chain"]),
                "address": x.get("address"), "mc_found": round(b) if b else None, "mc_peak": round(x.get("peak") or 0) or None,
                "peak_x": round(x["peak"] / b, 1) if b and x.get("peak") else None,
                "mc_now": round(nun) if nun is not None else None,
                "found_days_ago": round((jetzt - x["ts"]) / 86400, 1), "final": bool(x.get("final"))}

    runner = sorted([x for x in log_ if x.get("outcome") == "runner"], key=lambda x: -((x.get("peak") or 0) / (x.get("mc") or 1)))
    return {"days": tage, "coins_found": len(log_), "coins_24h_old": len(reif),
            "tracked_since": time.strftime("%Y-%m-%d", time.gmtime(min((x["ts"] for x in log_ if x.get("now")), default=jetzt))),
            "dead": zahl(reif, "dead"), "flat": zahl(reif, "flat"), "runner": zahl(reif, "runner"),
            "dead_pct": round(zahl(reif, "dead") / len(reif) * 100, 1) if reif else None,
            "runner_pct": round(zahl(reif, "runner") / len(reif) * 100, 1) if reif else None,
            "runners": [zeile(x) for x in runner[:n]],
            "rules": {"runner": f"peak at least {_f('RADAR_RUNNER_X', 3):g}x the market cap when found and at least "
                                f"{_usd(_f('RADAR_RUNNER_MIN_MC', 50_000))}",
                      "dead": f"market cap under {_usd(_f('RADAR_DEAD_MC', 5_000))}, down 90% from found, liquidity "
                              "under $1K, or no pool left",
                      "counted": "only coins at least 24 h old count in the shares; final after 3 days"},
            "note": ("Runners are what happened after we found them, not picks. Most narrative coins die within a day. "
                     "This is a record of the radar and the scan, not buy advice.")}
