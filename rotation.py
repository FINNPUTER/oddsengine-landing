"""
rotation.py: Meta Rotation. Seit 2.82.6.

Nur nachweislich erfolgreiche Wallets zaehlen ("Elite"): ein Wallet, das frueh in einen Coin gekauft hat
(Einstieg hoechstens ROTATION_ELITE_ENTRY_MC, Standard 500K Market Cap) und dieser Coin ist danach mindestens
ROTATION_ELITE_X (Standard 5x, bis 2.82.65 20x) gelaufen. Seit 2.82.7 zaehlt das erst, wenn wir es selbst am Chart nachgerechnet
haben (GeckoTerminal-Kerzen ab dem Kaufzeitpunkt), egal ob der Kauf aus eigenen Scans oder aus dem FINNPUTER-Import kommt. Bundles, Sniper und die Gruppe des Devs zaehlen nie (flag in iw_early).

Rotation: mindestens ROTATION_MIN_WALLETS Elite-Wallets mit verschiedenen Geldgebern kaufen in den letzten
ROTATION_RECENT_H Stunden in ein Thema, in dem sie in den ROTATION_BASE_H Stunden davor nicht unterwegs waren.
Dann gibt es eine Meldung: von welchem Thema in welches, welche Coins, und als Beleg der beste fruehe Einstieg
eines der Wallets. Wallet-Adressen gehen nie raus.

Schattenbetrieb: Schalter "rotation" aus (Standard) schreibt nur ins Protokoll (Admin: /v1/admin/rotation).
Schalter an schickt die Meldungen nur an verifizierte Holder: signierte Wallet plus verknuepfte Wallets halten
live on chain mindestens die Stufe ROTATION_TIER (Standard wie SMART_TIER, also "holder"). Der Wochenpass reicht hier
nicht. Beweis: jede Meldung wird mit der Market Cap jedes Coins beim Senden gespeichert und danach verfolgt
(Peak). /v1/rotation/record zeigt das oeffentlich, ohne Wallets.
"""
import json
import os
import re
import time

import requests

import storage
from logger import log


def _f(name, std):
    try:
        return float(os.getenv(name, str(std)) or std)
    except Exception:
        return std


ELITE_X = _f("ROTATION_ELITE_X", 5)     # seit 2.82.66: 5x statt 20x, auf Wunsch
ELITE_ENTRY = _f("ROTATION_ELITE_ENTRY_MC", 500_000)
MIN_W = int(_f("ROTATION_MIN_WALLETS", 3))
RECENT_H = _f("ROTATION_RECENT_H", 6)
BASE_H = _f("ROTATION_BASE_H", 48)
SPERRE_H = _f("ROTATION_COOLDOWN_H", 12)
TAKT = _f("ROTATION_INTERVAL_SEC", 600)
ELITE_N = int(_f("ROTATION_ELITE_MAX", 150))
JE_COIN = int(_f("ROTATION_PER_COIN", 25))   # seit 2.82.73: hoechstens so viele Wallets je Beleg-Coin
# Seit 2.82.12: Plausibilitaet. Ein Beleg mit Peak ueber MAX_PEAK_MC (Standard 5 Mrd.) oder ueber MAX_X ist ein
# Datenfehler (z. B. 1,27 Mrd.-fach bei "GTA 6"), Kerzen brauchen echtes Volumen in Dollar, und Wallets mit demselben
# Beleg-Coin zaehlen bei einer Rotation als ein einziges Wallet (Bundle- und Team-Gruppen).
MAX_PEAK_MC = _f("ROTATION_MAX_PEAK_MC", 5e9)
MAX_X = _f("ROTATION_MAX_X", 20000)
MIN_KERZE_USD = _f("ROTATION_MIN_CANDLE_USD", 1000)
MIN_MC_ALERT = _f("ROTATION_MIN_MCAP", 30000)
PRUEF_V = 3   # seit 2.82.13: Kaeufe auf der pump.fun-Kurve vor dem Pool werden konservativ mitgeprueft
# Seit 2.82.13: Elite-Scout. Sucht selbst Coins, die nachweislich gelaufen sind, und holt deren erste Kaeufer.
SCOUT_TAG = int(_f("ROTATION_SCOUT_PER_DAY", 8))        # Tiefen-Scans pro Tag (kosten RPC), 0 = aus
SCOUT_MAX_TAGE = _f("ROTATION_SCOUT_MAX_DAYS", 4)       # nur Pools, die hoechstens so alt sind (Kerzen reichen bis zum Start)
SCOUT_MIN_MC = _f("ROTATION_SCOUT_MIN_MCAP", 300000)    # heutige Market Cap mindestens
SCOUT_TAKT = _f("ROTATION_SCOUT_INTERVAL_SEC", 3600)
VOR_POOL_S = 3 * 86400
STUFE = (os.getenv("ROTATION_TIER", "") or os.getenv("SMART_TIER", "holder")).strip().lower() or "holder"

# Themen: Woerter im Namen oder Ticker. Reihenfolge = Vorrang.
THEMEN = [
    ("ai", "AI", ["ai", "agent", "agents", "gpt", "llm", "neural", "bot", "robot", "agi", "claude", "grok", "openai",
                  "deepseek", "terminal", "compute", "gpu", "brain", "cyborg", "android"]),
    ("gta", "GTA", ["gta", "gta6", "gtavi", "vice", "vicecity", "leonida", "rockstar", "grand theft", "lucia", "duval"]),
    ("dog", "Dogs", ["dog", "doge", "shib", "inu", "pup", "puppy", "wif", "bonk", "doggo", "corgi", "shiba", "pug", "woof"]),
    ("cat", "Cats", ["cat", "kitty", "kitten", "meow", "popcat", "mew", "nyan", "neko", "catwif", "pussy"]),
    ("frog", "Frogs", ["pepe", "frog", "toad", "ribbit", "kek", "brett", "apu"]),
    ("politics", "Politics", ["trump", "maga", "biden", "elon", "musk", "vance", "melania", "barron", "kamala", "potus",
                              "president", "election", "doge dept"]),
    ("stocks", "Stocks & RWA", ["stock", "stocks", "nasdaq", "spy", "tsla", "nvda", "aapl", "robinhood", "hood", "rwa",
                                "etf", "wallstreet", "wall street"]),
    ("gaming", "Gaming", ["game", "gaming", "play", "arcade", "pixel", "quest", "rpg", "mario", "pokemon", "nintendo"]),
    ("anime", "Anime", ["anime", "waifu", "chan", "kun", "senpai", "manga", "otaku"]),
    ("animal", "Other animals", ["monkey", "ape", "penguin", "hippo", "bear", "bull", "goat", "fish", "shark", "whale",
                                 "duck", "chicken", "hamster", "rat", "mouse", "pig", "cow", "horse", "bird", "owl",
                                 "panda", "fox", "wolf", "lion", "tiger", "moodeng", "capy", "capybara"]),
    ("food", "Food", ["pizza", "burger", "taco", "sushi", "bread", "toast", "coffee", "beer", "banana", "milk", "cheese"]),
    ("celebrity", "Celebs & culture", ["kanye", "ye", "drake", "taylor", "swift", "mrbeast", "tate", "kardashian", "diddy",
                                       "snoop", "tiktok", "viral", "meme", "skibidi", "rizz", "sigma"]),
]
NAMEN = {k: n for k, n, _ in THEMEN}
GT = "https://api.geckoterminal.com/api/v2"
GT_NETZ = {"solana": "solana", "robinhood": "robinhood"}
_T = {}
_ELITE = {"ts": 0, "w": {}}


def _ok_beleg(r):
    p = r.get("proof") or {}
    return float(p.get("x") or 0) <= MAX_X and float(p.get("peak") or 0) <= MAX_PEAK_MC


def _kv(k, std):
    try:
        v = storage.kv_get(k)
        return json.loads(v) if v else std
    except Exception:
        return std


def _kv_set(k, v):
    try:
        storage.kv_set(k, json.dumps(v, separators=(",", ":")))
    except Exception as e:
        log.debug(f"rotation kv {k}: {e}")


def _q(sql, args=(), eins=False):
    import intel
    return intel._q(sql, args, eins)


def thema(text):
    """Name oder Ticker -> Themen-Schluessel oder None."""
    t = " " + re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()) + " "
    kompakt = t.replace(" ", "")
    for k, _, woerter in THEMEN:
        for w in woerter:
            if " " in w:
                if w in t:
                    return k
            elif f" {w} " in t:
                return k
            elif len(w) >= 4 and w in kompakt:
                return k
    return None


# ── Elite ───────────────────────────────────────────────────────────────

def kandidaten(limit=20000):
    """Alle fruehen Kaeufe, die laut Datenbank die Elite-Regel erfuellen (noch ungeprueft).
    Importierte Kaeufe (FINNPUTER) haben oft keinen Rang, deshalb gilt der Rang nur fuer eigene Scans.
    Seit 2.82.60: vorher LIMIT 3000 nach behauptetem Multiple. Die Liste war voll mit unmoeglichen Import-Zahlen
    (Einstieg fast 0, also riesiges x), die der Chart-Check dann verwarf. Die Kaeufer der Scout-Coins (z. B. $HOGE)
    kamen nie dran. Jetzt: unmoegliche Werte schon in der Abfrage raus, Scout und eigene Scans zuerst."""
    rows = _q("""
        SELECT e.chain, e.wallet, e.token, t.symbol, e.buy_ts, COALESCE(e.src, 'own'),
               COALESCE(NULLIF(e.entry_mcap, 0), t.first_mcap) AS einstieg, t.peak_mcap,
               t.peak_mcap / NULLIF(COALESCE(NULLIF(e.entry_mcap, 0), t.first_mcap), 0) AS x
          FROM iw_early e JOIN iw_tokens t ON t.chain = e.chain AND t.token = e.token
         WHERE e.flag IS NULL AND (COALESCE(e.src, 'own') = 'import' OR e.rank <= 30)
           AND e.chain IN ('solana', 'robinhood')
           AND COALESCE(NULLIF(e.entry_mcap, 0), t.first_mcap) > 0
           AND COALESCE(NULLIF(e.entry_mcap, 0), t.first_mcap) <= %s
           AND t.peak_mcap / NULLIF(COALESCE(NULLIF(e.entry_mcap, 0), t.first_mcap), 0) >= %s
           AND t.peak_mcap / NULLIF(COALESCE(NULLIF(e.entry_mcap, 0), t.first_mcap), 0) <= %s
           AND t.peak_mcap <= %s
         ORDER BY CASE COALESCE(e.src, 'own') WHEN 'scout' THEN 0 WHEN 'own' THEN 1 ELSE 2 END, x DESC
         LIMIT %s""", (ELITE_ENTRY, ELITE_X, MAX_X, MAX_PEAK_MC, limit)) or []
    return [{"chain": r[0], "wallet": r[1], "token": r[2], "symbol": r[3], "buy_ts": int(r[4] or 0) or None,
             "src": r[5], "entry": float(r[6] or 0), "peak": float(r[7] or 0), "x": float(r[8] or 0)} for r in rows]


def _vkey(k):
    return f"{k['chain']}:{k['token']}:{k['wallet']}"


def elite(frisch=False):
    """{(chain, wallet): beleg}. Seit 2.82.7 zaehlt nur ein Einstieg, den wir selbst am Chart nachgeprueft haben:
    Preis in der Kerze des Kaufs (deren Hoch, also zu Ungunsten des Wallets) gegen das spaetere Hoch mit Volumen.
    Beleg = bester gepruefter Einstieg."""
    if not frisch and time.time() - _ELITE["ts"] < 900 and _ELITE["w"]:
        return _ELITE["w"]
    gep = _kv("rotation_verified", {})
    w = {}
    for k in kandidaten():
        v = gep.get(_vkey(k))
        if not v or v.get("status") != "ok" or int(v.get("v") or 1) < PRUEF_V or float(v.get("x") or 0) < ELITE_X:
            continue
        if float(v["x"]) > MAX_X or k["entry"] * float(v["x"]) > MAX_PEAK_MC:
            continue
        # Anzeige vorsichtig: der kleinere Wert aus Datenbank-Peak und Chart-Rechnung
        peak = min(k["peak"], k["entry"] * float(v["x"])) if k["peak"] else k["entry"] * float(v["x"])
        if peak > MAX_PEAK_MC:
            continue
        b = {"symbol": k["symbol"], "token": k["token"], "entry": k["entry"], "peak": round(peak),
             "x": round(peak / k["entry"], 1) if k["entry"] else round(float(v["x"]), 1), "src": k["src"], "checked": "chart"}
        if b["x"] < ELITE_X:
            continue
        alt = w.get((k["chain"], k["wallet"]))
        if not alt or b["x"] > alt["x"]:
            w[(k["chain"], k["wallet"])] = b
    # die besten zuerst, gedeckelt (sie muessen auch abgefragt werden).
    # Seit 2.82.73 ausgewogen: vorher nahmen die besten 150 nach Multiple nur noch Robinhood-Wallets, und viele
    # davon aus einem einzigen Coin. Jetzt hoechstens JE_COIN Wallets je Beleg-Coin, und jede Kette bekommt
    # zuerst ihren Anteil (ELITE_N geteilt durch die Zahl der Ketten), der Rest geht wieder nach Multiple.
    sortiert = sorted(w.items(), key=lambda kv: -kv[1]["x"])
    je_coin, gefiltert = {}, []
    for k, b in sortiert:
        ck = (k[0], b.get("token"))
        if je_coin.get(ck, 0) >= JE_COIN:
            continue
        je_coin[ck] = je_coin.get(ck, 0) + 1
        gefiltert.append((k, b))
    ketten = sorted({k[0] for k, _ in gefiltert})
    anteil = ELITE_N // max(1, len(ketten))
    wahl, n_kette = [], {}
    for k, b in gefiltert:
        if n_kette.get(k[0], 0) < anteil:
            wahl.append((k, b))
            n_kette[k[0]] = n_kette.get(k[0], 0) + 1
    rest = [kb for kb in gefiltert if kb not in wahl]
    wahl += rest[:max(0, ELITE_N - len(wahl))]
    w = dict(sorted(wahl, key=lambda kv: -kv[1]["x"]))
    _ELITE.update(ts=time.time(), w=w)
    return w


def _kerzen(chain, pool, buy_ts):
    netz = GT_NETZ.get(chain)
    if not (netz and pool):
        return None
    alter = time.time() - buy_ts
    art, agg = ("minute", 15) if alter <= 9 * 86400 else (("hour", 1) if alter <= 40 * 86400 else ("day", 1))
    try:
        r = None
        for v in range(3):
            r = requests.get(f"{GT}/networks/{netz}/pools/{pool}/ohlcv/{art}",
                             params={"aggregate": agg, "limit": 1000, "currency": "usd"},
                             headers={"Accept": "application/json"}, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(4 * (v + 1))
                continue
            break
        if r.status_code == 429 or r.status_code >= 500:
            return "ERR"                 # seit 2.82.62: Limit/Serverfehler ist KEIN fehlender Chart, spaeter nochmal
        if r.status_code != 200:
            return None
        k = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        sek = {"minute": 60, "hour": 3600, "day": 86400}[art] * agg
        return sorted([x for x in k if x and len(x) > 5], key=lambda x: int(x[0])), sek
    except Exception as e:
        log.debug(f"rotation ohlcv {pool[:8]}: {e}")
        return "ERR"


def _pool(chain, token):
    """Groesster Pool laut GeckoTerminal. -> (pool, fehler). fehler=True heisst: Limit oder Netz, spaeter nochmal."""
    netz = GT_NETZ.get(chain)
    if not netz:
        return None, False
    for v in range(3):
        try:
            r = requests.get(f"{GT}/networks/{netz}/tokens/{token}", params={"include": "top_pools"},
                             headers={"Accept": "application/json"}, timeout=12)
        except Exception:
            time.sleep(3)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(4 * (v + 1))
            continue
        if r.status_code != 200:
            return None, False
        try:
            pools = (((r.json().get("data") or {}).get("relationships") or {}).get("top_pools") or {}).get("data") or []
            return (pools[0]["id"].split("_", 1)[-1] if pools else None), False
        except Exception:
            return None, False
    return None, True


def _kurve(chain, token):
    """Solana-Launchpad-Coin (pump.fun, letsbonk, Meteora DBC, Raydium LaunchLab): vor dem Pool lief er auf einer
    Kurve, der Umzug in den Pool passiert am oberen Ende der Kurve. Seit 2.82.52 fuer alle Solana-Coins, vorher nur
    Adressen auf "pump" (bei $AURA, $Sakura, $inuhao fand der Scout deshalb 0 fruehe Kaeufer)."""
    return chain == "solana"


def pruefen_x(chain, pool, buy_ts, cache=None, token=None):
    """Multiple am Chart: Hoch der Kaufkerze -> spaeteres Hoch (zwei Kerzen mit Volumen). -> (status, x)
    Seit 2.82.13: Kauf auf der pump.fun-Kurve vor der ersten Pool-Kerze (hoechstens 3 Tage davor) zaehlt mit dem Hoch
    der ersten Pool-Kerze als Einstieg. Das Wallet hat billiger gekauft, das echte Multiple ist also eher hoeher."""
    if not buy_ts:
        return "no_time", None
    key = (chain, pool, int(time.time() - buy_ts) // 86400 > 40, int(time.time() - buy_ts) // 86400 > 9)
    if cache is not None and key in cache:
        k = cache[key]
    else:
        k = _kerzen(chain, pool, buy_ts)
        if cache is not None:
            cache[key] = k
    if k == "ERR":
        return "error", None
    if not k:
        return "no_chart", None
    kerzen, sek = k
    kauf = None
    if not kerzen:
        return "too_old", None
    if int(kerzen[0][0]) > buy_ts:
        if _kurve(chain, token) and int(kerzen[0][0]) - buy_ts <= VOR_POOL_S:
            kauf = kerzen[0]
        else:
            return "too_old", None        # der Kauf liegt vor den Kerzen, die wir bekommen
    for x in ([] if kauf else kerzen):
        if int(x[0]) <= buy_ts < int(x[0]) + sek:
            kauf = x
            break
    if not kauf:
        return "no_candle", None
    try:
        ein = float(kauf[2])              # Hoch der Kaufkerze: der schlechteste Preis, den das Wallet bekommen konnte
    except (TypeError, ValueError):
        return "no_candle", None
    nach = [x for x in kerzen if int(x[0]) > int(kauf[0])]
    best = None
    for a, b in zip(nach, nach[1:]):
        try:
            if float(a[5] or 0) < MIN_KERZE_USD or float(b[5] or 0) < MIN_KERZE_USD:
                continue
            h = min(float(a[2]), float(b[2]))
        except (TypeError, ValueError):
            continue
        best = h if best is None else max(best, h)
    if not ein or not best:
        return "no_volume", None
    return "ok", best / ein


def _nochmal(v):
    """Seit 2.82.62: "no_chart" kam oft vom GeckoTerminal-Limit, nicht von einem fehlenden Chart. Solche Eintraege
    werden bis zu 3 Mal neu geprueft, frühestens 6 Stunden nach dem letzten Versuch. Echte Fehler ("error") sofort."""
    st = v.get("status")
    if st == "error":
        return True
    return st == "no_chart" and int(v.get("tries") or 1) < 3 and time.time() - int(v.get("ts") or 0) > 6 * 3600


def pruefrunde(max_n=50):
    """Ungepruefte Kandidaten am Chart nachrechnen. Pro Runde hoechstens max_n Coins (GeckoTerminal-Limit)."""
    gep = _kv("rotation_verified", {})
    offen = [k for k in kandidaten() if _vkey(k) not in gep or int(gep[_vkey(k)].get("v") or 1) < PRUEF_V
             or _nochmal(gep[_vkey(k)])]
    if not offen:
        return 0
    pools, cache, n, coins = {}, {}, 0, set()
    for k in offen:
        ct = (k["chain"], k["token"])
        if ct not in coins and len(coins) >= max_n:
            continue
        coins.add(ct)
        if ct not in pools:
            pools[ct] = _pool(k["chain"], k["token"])
            time.sleep(2.1)
        pool, fehler = pools[ct]
        if fehler:
            continue                      # Limit: nicht als "kein Chart" speichern, naechste Runde nochmal
        st, x = pruefen_x(k["chain"], pool, k["buy_ts"], cache, k["token"])
        if len(cache) and st != "no_time":
            time.sleep(0.2)
        if st == "error":
            continue
        alt = gep.get(_vkey(k)) or {}
        gep[_vkey(k)] = {"status": st, "x": round(x, 2) if x else None, "db_x": round(k["x"], 1), "v": PRUEF_V,
                         "src": k["src"], "ts": int(time.time()),
                         "tries": int(alt.get("tries") or 1) + (1 if alt.get("status") == "no_chart" else 0)}
        n += 1
    if len(gep) > 20000:
        gep = dict(sorted(gep.items(), key=lambda kv: -kv[1].get("ts", 0))[:15000])
    _kv_set("rotation_verified", gep)
    _ELITE["ts"] = 0
    return n


def pruefbericht():
    """Getrennte Uebersicht: was die Datenbank behauptet, was der Chart bestaetigt, nach Herkunft."""
    gep = _kv("rotation_verified", {})
    ks = kandidaten()
    stat = {}
    for k in ks:
        v = gep.get(_vkey(k))
        if not v or int(v.get("v") or 1) < PRUEF_V or v.get("status") == "error":
            s = "pending"
        elif v.get("status") != "ok":
            s = v.get("status")
        elif float(v.get("x") or 0) > MAX_X or k["entry"] * float(v.get("x") or 0) > MAX_PEAK_MC:
            s = "implausible"
        else:
            s = "confirmed" if float(v.get("x") or 0) >= ELITE_X else "weaker_than_claimed"
        stat.setdefault(k["src"], {}).setdefault(s, 0)
        stat[k["src"]][s] += 1
    abw = []
    for k in ks:
        v = gep.get(_vkey(k))
        if v and v.get("status") == "ok" and float(v.get("x") or 0) < ELITE_X:
            abw.append({"chain": k["chain"], "symbol": k["symbol"], "token": k["token"], "src": k["src"],
                        "claimed_x": round(k["x"], 1), "chart_x": v.get("x")})
    gesamt = {}
    for q in stat.values():
        for s_, n_ in q.items():
            gesamt[s_] = gesamt.get(s_, 0) + n_
    return {"candidates": len(ks), "wallets_claimed": len({(k["chain"], k["wallet"]) for k in ks}), "outcomes": gesamt,
            "wallets_confirmed": len(elite(frisch=True)), "by_source": stat,
            "biggest_gaps": sorted(abw, key=lambda a: -a["claimed_x"])[:25]}


def elite_wallets():
    """Fuer smart.liste(): diese Wallets werden immer mit abgefragt."""
    try:
        return [{"wallet": w, "chain": c, "score": 99} for (c, w) in elite().keys()]
    except Exception as e:
        log.debug(f"rotation elite: {e}")
        return []


# ── Namen der Coins ─────────────────────────────────────────────────────

def _namen(paare):
    """{(chain, token): "name symbol"} aus iw_tokens, Cache und GeckoTerminal."""
    cache = _kv("rotation_names", {})
    aus, fehlt = {}, []
    for c, t in paare:
        k = f"{c}:{t}"
        if k in cache:
            aus[(c, t)] = cache[k]
        else:
            fehlt.append((c, t))
    if fehlt:
        for c, t, sym in _q("SELECT chain, token, symbol FROM iw_tokens WHERE (chain, token) IN (" +
                            ",".join(["(%s,%s)"] * len(fehlt)) + ")", tuple(x for p in fehlt for x in p)) or []:
            if sym:
                aus[(c, t)] = sym
    for c, t in fehlt[:30]:
        if (c, t) in aus or c not in GT_NETZ:
            continue
        try:
            r = requests.get(f"{GT}/networks/{GT_NETZ[c]}/tokens/{t}", headers={"Accept": "application/json"}, timeout=10)
            if r.status_code == 200:
                a = (r.json().get("data") or {}).get("attributes") or {}
                aus[(c, t)] = f"{a.get('name') or ''} {a.get('symbol') or ''}".strip()
            time.sleep(0.3)
        except Exception:
            pass
    for (c, t), v in aus.items():
        cache[f"{c}:{t}"] = v
    if len(cache) > 6000:
        cache = dict(list(cache.items())[-4000:])
    _kv_set("rotation_names", cache)
    return aus


def _ticker(name):
    teile = str(name or "").split()
    return teile[-1].upper() if teile else "?"


# ── Erkennen ────────────────────────────────────────────────────────────

def erkennen():
    """-> Liste von Rotationen (noch nicht gefiltert nach Sperre)."""
    import smart
    el = elite()
    if not el:
        return []
    jetzt = int(time.time())
    ab_neu = jetzt - int(RECENT_H * 3600)
    ab_alt = ab_neu - int(BASE_H * 3600)
    rows = _q("SELECT chain, token, wallet, MIN(ts) FROM iw_sight WHERE ts >= %s GROUP BY chain, token, wallet", (ab_alt,)) or []
    rows = [r for r in rows if (r[0], r[2]) in el]
    if not rows:
        return []
    namen = _namen({(r[0], r[1]) for r in rows})
    vorher, jetzt_k = {}, {}
    for c, t, w, ts in rows:
        th = thema(namen.get((c, t)))
        if not th:
            continue
        ziel = jetzt_k if ts >= ab_neu else vorher
        ziel.setdefault((c, w), []).append((th, t, ts))
    rein = {}
    for wk, kaeufe in jetzt_k.items():
        alt = vorher.get(wk) or []
        if not alt:
            continue                       # ohne Vorgeschichte ist es kein Wechsel
        alte = {}
        for th, _, _ in alt:
            alte[th] = alte.get(th, 0) + 1
        haupt = max(alte, key=alte.get)
        for th, t, ts in kaeufe:
            if th in alte:
                continue
            rein.setdefault(th, {}).setdefault(wk, {"from": haupt, "tokens": set()})["tokens"].add((wk[0], t))
    aus = []
    for th, wallets in rein.items():
        if len(wallets) < MIN_W:
            continue
        # gleiche Geldgeber zaehlen als ein Wallet
        unabh, belege = {}, set()
        for (c, w), v in wallets.items():
            g = smart._geldgeber(c, [w]).get(w, w)
            bt = (el.get((c, w)) or {}).get("token") or w
            if bt in belege:
                continue           # gleicher Beleg-Coin: eine Gruppe, zaehlt einmal
            belege.add(bt)
            unabh.setdefault(g, ((c, w), v))
        if len(unabh) < MIN_W:
            continue
        von = {}
        coins = {}
        for (cw, v) in unabh.values():
            von[v["from"]] = von.get(v["from"], 0) + 1
            for ct in v["tokens"]:
                coins[ct] = coins.get(ct, 0) + 1
        beweis = max((el[cw] for cw, _ in unabh.values()), key=lambda b: b["x"])
        aus.append({
            "theme": th, "theme_name": NAMEN.get(th, th), "from": max(von, key=von.get),
            "from_name": NAMEN.get(max(von, key=von.get), "other"), "wallets": len(unabh),
            "coins": [{"chain": c, "token": t, "ticker": _ticker(namen.get((c, t))), "wallets": n}
                      for (c, t), n in sorted(coins.items(), key=lambda kv: -kv[1])[:5]],
            "proof": beweis, "ts": jetzt})
    return aus


def _geld(v):
    v = float(v or 0)
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def text(r):
    p = r["proof"]
    zeilen = [f"Meta rotation: {r['wallets']} proven wallets moving from {r['from_name']} into {r['theme_name']}.",
              "",
              f"Proof they know what they are doing: one of them bought ${p['symbol'] or '?'} at {_geld(p['entry'])} "
              f"market cap, it peaked at {_geld(p['peak'])}.",
              "",
              "What they are buying now:"]
    for c in r["coins"]:
        zeilen.append(f"${c['ticker']} ({c['chain']})  {c['token']}")
    zeilen += ["", "Not financial advice. Wallets stay private."]
    return "\n".join(zeilen)


# ── Elite-Scout (seit 2.82.13) ──────────────────────────────────────────

def _gt(pfad, params=None):
    try:
        r = requests.get(GT + pfad, params=params or {}, headers={"Accept": "application/json"}, timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        log.debug(f"rotation scout gt {pfad}: {e}")
        return {}


def _zahl(x):
    try:
        return float(x) if x not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def laeufer_finden(chain):
    """Kandidaten von GeckoTerminal: trendende und umsatzstarke Pools, jung genug, heute noch mit Groesse."""
    netz = GT_NETZ.get(chain)
    if not netz:
        return []
    from datetime import datetime
    out = {}
    for pfad, params in ((f"/networks/{netz}/trending_pools", {"duration": "24h"}),
                         (f"/networks/{netz}/trending_pools", {"duration": "6h"}),
                         (f"/networks/{netz}/pools", {"sort": "h24_volume_usd_desc"})):
        d = _gt(pfad, params)
        time.sleep(2.2)
        for p in d.get("data") or []:
            a = p.get("attributes") or {}
            basis = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
            token = basis.split("_", 1)[-1]
            preis = _zahl(a.get("base_token_price_usd"))
            mc = _zahl(a.get("market_cap_usd")) or _zahl(a.get("fdv_usd"))
            try:
                alter_h = (time.time() - datetime.fromisoformat(str(a.get("pool_created_at")).replace("Z", "+00:00")).timestamp()) / 3600
            except Exception:
                continue
            if not token or not preis or mc < SCOUT_MIN_MC or not (6 <= alter_h <= SCOUT_MAX_TAGE * 24):
                continue
            if token in out:
                continue
            out[token] = {"chain": chain, "token": token, "pool": str(p.get("id") or "").split("_", 1)[-1],
                          "supply": mc / preis, "mcap": mc, "age_h": alter_h,
                          "symbol": str(a.get("name") or "").split(" / ")[0][:20]}
    return list(out.values())


def lauf_messen(chain, kand):
    """Ist der Coin am Chart wirklich gelaufen? Tiefster Stand (mit Volumen) vor dem Hoch, Hoch ueber zwei Kerzen.
    -> {"entry": tiefste MC, "peak": Hoch-MC, "x"} oder None"""
    k = _kerzen(chain, kand["pool"], time.time() - kand["age_h"] * 3600)
    if not k:
        return None
    kerzen, _ = k
    sup = kand["supply"]
    tief, best = None, None
    for a, b in zip(kerzen, kerzen[1:]):
        try:
            if float(a[5] or 0) >= MIN_KERZE_USD:
                lo = float(a[3]) * sup
                if lo > 0:
                    tief = lo if tief is None else min(tief, lo)
            if tief and float(a[5] or 0) >= MIN_KERZE_USD and float(b[5] or 0) >= MIN_KERZE_USD:
                h = min(float(a[2]), float(b[2])) * sup
                if tief <= ELITE_ENTRY and (best is None or h / tief > best["x"]):
                    best = {"entry": tief, "peak": h, "x": h / tief}
        except (TypeError, ValueError):
            continue
    if not best or best["x"] < ELITE_X or best["peak"] > MAX_PEAK_MC or best["x"] > MAX_X:
        return None
    return best


def _einstiege(chain, tk, kand):
    """Einstiegs-Market-Cap je fruehem Kaeufer aus den Kerzen (Hoch der Kaufkerze, also zu Ungunsten des Wallets)."""
    k = _kerzen(chain, kand["pool"], time.time() - kand["age_h"] * 3600)
    if not k:
        return 0
    kerzen, sek = k
    sup = kand["supply"]
    n = 0
    for (w, ts) in _q("SELECT wallet, buy_ts FROM iw_early WHERE chain=%s AND token=%s AND buy_ts > 0", (chain, tk)) or []:
        ts = int(ts)
        kerze = None
        if kerzen and int(kerzen[0][0]) > ts:
            if _kurve(chain, kand["token"]) and int(kerzen[0][0]) - ts <= VOR_POOL_S:
                kerze = kerzen[0]
        else:
            kerze = next((x for x in kerzen if int(x[0]) <= ts < int(x[0]) + sek), None)
        if not kerze:
            continue
        try:
            mc = float(kerze[2]) * sup
        except (TypeError, ValueError):
            continue
        _q("UPDATE iw_early SET entry_mcap=%s, src='scout' WHERE chain=%s AND token=%s AND wallet=%s", (mc, chain, tk, w))
        n += 1
    _q("UPDATE iw_early SET src='scout' WHERE chain=%s AND token=%s AND COALESCE(src, 'own') = 'own'", (chain, tk))
    return n


def scout_runde():
    """Laeufer finden, am Chart bestaetigen, dann die ersten Kaeufer per Tiefen-Scan holen. Hoechstens
    ROTATION_SCOUT_PER_DAY Tiefen-Scans am Tag. Danach prueft pruefrunde() jedes Wallet einzeln am Chart."""
    if SCOUT_TAG <= 0:
        return 0
    import intel
    st = _kv("rotation_scout", {})
    tag = time.strftime("%Y-%m-%d", time.gmtime())
    if st.get("day") != tag:
        st.update(day=tag, scans=0, checked=0)
    st.setdefault("seen", {})
    st.setdefault("found", [])
    if st["scans"] >= SCOUT_TAG:
        return 0
    neu = 0
    for chain in ("solana", "robinhood"):
        for kand in laeufer_finden(chain):
            if st["scans"] >= SCOUT_TAG:
                break
            key = f"{chain}:{kand['token']}"
            if key in st["seen"]:
                continue
            st["seen"][key] = int(time.time())
            tk = intel._k(chain, kand["token"])
            if _q("SELECT 1 FROM iw_early WHERE chain=%s AND token=%s LIMIT 1", (chain, tk), eins=True):
                continue
            time.sleep(2.2)
            st["checked"] = st.get("checked", 0) + 1
            lauf = lauf_messen(chain, kand)
            if not lauf:
                continue
            # credited=TRUE: Scout-Coins schreiben keine Smart-Money-Punkte gut (sonst waechst die abgefragte Liste)
            _q("INSERT INTO iw_tokens (chain, token, symbol, first_mcap, peak_mcap, multiple, first_seen, checked, credited) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT (chain, token) DO UPDATE SET "
               "peak_mcap=GREATEST(iw_tokens.peak_mcap, EXCLUDED.peak_mcap)",
               (chain, tk, kand["symbol"], lauf["entry"], lauf["peak"], lauf["x"], int(time.time()), int(time.time())))
            st["scans"] += 1
            try:
                erg = intel.scannen(chain, kand["token"], lauf["entry"], kand["symbol"])
            except Exception as e:
                log.warning(f"rotation scout scan {kand['token'][:8]}: {e}")
                continue
            n = _einstiege(chain, tk, kand) if erg.get("measured") else 0
            gesamt = (_q("SELECT COUNT(*) FROM iw_early WHERE chain=%s AND token=%s", (chain, tk), eins=True) or [0])[0]
            st["found"].append({"chain": chain, "symbol": kand["symbol"], "token": kand["token"],
                                "low_mcap": round(lauf["entry"]), "peak_mcap": round(lauf["peak"]),
                                "x": round(lauf["x"], 1), "buyers": n, "scanned": int(gesamt or 0),
                                "measured": bool(erg.get("measured")), "ts": int(time.time())})
            neu += 1
            log.info(f"rotation scout: {kand['symbol']} ran {lauf['x']:.0f}x, {n} early buyers stored")
            _kv_set("rotation_scout", st)
    if len(st["seen"]) > 4000:
        st["seen"] = dict(sorted(st["seen"].items(), key=lambda kv: -kv[1])[:3000])
    st["found"] = st["found"][-100:]
    _kv_set("rotation_scout", st)
    if neu:
        _ELITE["ts"] = 0
    return neu


def _pruef_thread():
    try:
        try:
            gelaufen()
        except Exception as e:
            log.warning(f"rotation gelaufen: {e}")
        if time.time() - _T.get("a", 0) >= 1800:
            _T["a"] = time.time()
            try:
                auswerten()
            except Exception as e:
                log.warning(f"rotation auswerten: {e}")
        n = pruefrunde()
        if n:
            log.info(f"rotation: {n} early buys checked on the chart")
        if time.time() - _T.get("s", 0) >= SCOUT_TAKT:
            _T["s"] = time.time()
            scout_runde()
    except Exception as e:
        log.warning(f"rotation pruefen: {e}")
    finally:
        _T["laeuft"] = False


def tick():
    # Seit 2.82.7: Kandidaten im Hintergrund am Chart pruefen, damit die Smart-Runde nicht wartet
    if not _T.get("laeuft") and time.time() - _T.get("v", 0) >= TAKT:
        import threading
        _T["v"], _T["laeuft"] = time.time(), True
        threading.Thread(target=_pruef_thread, daemon=True, name="rotation-check").start()
    if time.time() - _T.get("r", 0) < TAKT:
        return
    _T["r"] = time.time()
    try:
        funde = erkennen()
    except Exception as e:
        log.warning(f"rotation: {e}")
        return
    if not funde:
        return
    sperre = _kv("rotation_lock", {})
    protokoll = _kv("rotation_log", [])
    live = ist_live()
    for r in funde:
        if time.time() - float(sperre.get(r["theme"], 0)) < SPERRE_H * 3600:
            continue
        sperre[r["theme"]] = time.time()
        r["sent"] = 0
        r["beta_sent"] = 0
        for c in r["coins"]:
            try:
                import smart
                c["mcap0"] = smart._gt_info(c["chain"], c["token"]).get("mcap")
            except Exception:
                c["mcap0"] = None
            c["peak"] = c["mcap0"]
        r["coins"] = [c for c in r["coins"] if (c.get("mcap0") or 0) >= MIN_MC_ALERT]
        if not r["coins"]:
            continue
        try:
            r["base"] = _vergleich(r)
        except Exception as e:
            log.debug(f"rotation vergleich: {e}")
            r["base"] = []
        if live:
            r["sent"] = _senden(text(r))
        else:
            r["beta_sent"] = _senden(text(r), nur_beta=True)
        protokoll.append(r)
        log.info(f"rotation: {r['from']} -> {r['theme']} ({r['wallets']} wallets) live={live}")
    _kv_set("rotation_lock", sperre)
    _kv_set("rotation_log", protokoll[-LOG_MAX:])


def ist_live():
    try:
        import features
        return features.an("rotation")
    except Exception:
        return False


def holder_ok(adr):
    """Nur verifizierte Holder: Bestand live on chain, signierte plus verknuepfte Wallets."""
    import holder
    try:
        st = holder.status(holder.norm(adr), frisch=True)
    except Exception as e:
        log.debug(f"rotation holder {adr[:8]}: {e}")
        return False
    return holder.RANG.get(st.get("tier") or "public", 0) >= holder.RANG.get(STUFE, 1)


def gewollt(adr):
    """Seit 2.82.8: Holder bekommen Meta Rotation automatisch. Nur wer es abschaltet (prefs.rotation False), nicht."""
    import motor
    return (motor.zustand(adr).get("prefs") or {}).get("rotation") is not False


def zugang(adr):
    import motor
    try:
        if motor.beta(adr):
            return True
    except Exception:
        pass
    return holder_ok(adr)


def _senden(nachricht, nur_beta=False):
    import motor
    n = 0
    try:
        nutzer = list(motor._nutzer())
    except Exception:
        nutzer = []
    for adr in nutzer:
        try:
            if nur_beta and not motor.beta(adr):
                continue
            if not motor.offen(adr) or not gewollt(adr):
                continue
            if not zugang(adr):
                continue
            motor.senden(adr, nachricht)
            n += 1
        except Exception as e:
            log.debug(f"rotation senden {adr[:8]}: {e}")
    return n


def nachverfolgen():
    """Peak jedes gemeldeten Coins bis 7 Tage nach der Meldung (alle 30 Minuten)."""
    if time.time() - _T.get("p", 0) < 1800:
        return
    _T["p"] = time.time()
    import smart
    prot = _kv("rotation_log", [])
    geaendert = False
    for r in prot:
        if time.time() - float(r.get("ts") or 0) > 7 * 86400:
            continue
        for c in r.get("coins") or []:
            if not c.get("mcap0"):
                continue
            try:
                mc = smart._gt_info(c["chain"], c["token"]).get("mcap")
            except Exception:
                mc = None
            if mc and mc > float(c.get("peak") or 0):
                c["peak"] = mc
                geaendert = True
            time.sleep(0.3)
    if geaendert:
        _kv_set("rotation_log", prot)


def bilanz(oeffentlich=True):
    """Beweis: jede Meldung mit Market Cap beim Senden und Peak danach. Ohne Wallets."""
    prot = [r for r in _kv("rotation_log", []) if _ok_beleg(r)]
    zeilen, treffer, gezaehlt = [], 0, 0
    for r in reversed(prot):
        coins = []
        best = None
        for c in r.get("coins") or []:
            x = (float(c["peak"]) / float(c["mcap0"])) if c.get("mcap0") and c.get("peak") else None
            coins.append({"ticker": c.get("ticker"), "chain": c.get("chain"), "token": c.get("token"),
                          "mcap_at_alert": c.get("mcap0"), "peak_mcap": c.get("peak"),
                          "multiple": round(x, 2) if x else None})
            if x and (best is None or x > best):
                best = x
        alt = time.time() - float(r.get("ts") or 0) > 86400
        if best is not None and alt:
            gezaehlt += 1
            treffer += 1 if best >= 2 else 0
        zeilen.append({"ts": r.get("ts"), "from": r.get("from_name"), "to": r.get("theme_name"),
                       "wallets": r.get("wallets"), "proof": r.get("proof"), "coins": coins,
                       "best_multiple": round(best, 2) if best else None, "live": bool(r.get("sent"))})
    return {"rotations": len(zeilen), "settled": gezaehlt, "hit_2x": treffer,
            "hit_rate": round(treffer / gezaehlt, 3) if gezaehlt else None,
            "rule": "A rotation counts as a hit when one of its coins did 2x or more from the market cap at the alert "
                    "within 7 days. Counted after 24 hours. Peak based.",
            "list": zeilen[:50]}


def fuer_app(adr=None):
    """Fuer die App. Holder (und Tester) sehen die Meldungen komplett, alle anderen nur die Bilanz.
    Ist der Schalter aus, sehen nur Tester die Meldungen (zum Pruefen), sonst niemand."""
    live = ist_live()
    darf = bool(adr) and zugang(adr)
    try:
        import motor
        tester = bool(adr) and motor.beta(adr)
    except Exception:
        tester = False
    out = {"live": live, "access": darf, "tier": STUFE, "on": bool(adr) and gewollt(adr)}
    if live:
        out["record"] = bilanz(True)
    if darf and (live or tester):
        prot = [r for r in _kv("rotation_log", []) if _ok_beleg(r) and (live is False or r.get("sent"))]
        out["items"] = [{"ts": r.get("ts"), "from": r.get("from_name"), "to": r.get("theme_name"),
                         "wallets": r.get("wallets"), "proof": r.get("proof"),
                         "coins": [{"ticker": c.get("ticker"), "chain": c.get("chain"), "token": c.get("token"),
                                    "mcap_at_alert": c.get("mcap0"), "peak_mcap": c.get("peak")}
                                   for c in r.get("coins") or []]}
                        for r in reversed(prot[-20:])]
    return out


def bericht():
    el = elite()
    return {"live": ist_live(),
            "rules": {"elite_x": ELITE_X, "elite_entry_mcap": ELITE_ENTRY, "min_wallets": MIN_W,
                      "recent_h": RECENT_H, "base_h": BASE_H, "cooldown_h": SPERRE_H},
            "elite_wallets": len(el),
            "elite_top": sorted([{"chain": c, "proof": b} for (c, _), b in el.items()], key=lambda x: -x["proof"]["x"])[:15],
            "tier": STUFE, "check": pruefbericht(), "record": bilanz(False), "log": _kv("rotation_log", [])[-30:],
            "scout": _scout_stand()}


def _scout_stand():
    st = _kv("rotation_scout", {})
    return {"per_day": SCOUT_TAG, "today": st.get("day"), "deep_scans_today": st.get("scans", 0),
            "runner_checks_today": st.get("checked", 0), "found": list(reversed(st.get("found", [])))[:25]}


# ── Auswertung am Chart (seit 2.82.50) ──────────────────────────────────
# Jeder gemeldete Coin wird ab dem Zeitpunkt der Meldung an den Kerzen gemessen: Einstieg = Schluss der Kerze mit der
# Meldung, Peak nach derselben Regel wie bei den Elite-Wallets (zwei Kerzen mit Volumen), dazu der Kurs nach 1 h, 6 h,
# 24 h und 7 Tagen und der tiefste Stand vor dem Peak. Zum Vergleich bekommt jede neue Meldung bis zu drei andere Coins
# desselben Themas, die im selben Fenster gekauft wurden, aber nicht von den Elite-Wallets (r["base"]). Gemessen wird
# genauso. So sieht man, ob die Rotation besser ist als das Thema selbst.

LOG_MAX = int(_f("ROTATION_LOG_MAX", 300))
EVAL_MAX = int(_f("ROTATION_EVAL_PER_RUN", 20))
PUNKTE = (("x_1h", 3600), ("x_6h", 6 * 3600), ("x_24h", 86400), ("x_7d", 7 * 86400))


def _vergleich(r):
    """Bis zu drei Coins desselben Themas, im Meldefenster gekauft, nicht unter den gemeldeten."""
    import random
    import smart
    ab = int(time.time() - RECENT_H * 3600)
    schon = {(c["chain"], c["token"]) for c in r["coins"]}
    rows = _q("SELECT DISTINCT chain, token FROM iw_sight WHERE ts >= %s LIMIT 400", (ab,)) or []
    paare = [(c, t) for c, t in rows if (c, t) not in schon and c in GT_NETZ]
    namen = _namen(set(paare[:120]))
    passt = [(c, t) for (c, t) in paare if thema(namen.get((c, t))) == r["theme"]]
    random.shuffle(passt)
    aus = []
    for c, t in passt[:8]:
        if len(aus) >= 3:
            break
        try:
            info = smart._gt_info(c, t)
        except Exception:
            info = {}
        time.sleep(1.2)
        if (info.get("mcap") or 0) >= MIN_MC_ALERT:
            aus.append({"chain": c, "token": t, "ticker": _ticker(namen.get((c, t))), "mcap0": info["mcap"],
                        "pool": info.get("pool")})
    return aus


def _messen(chain, pool, ts):
    """Kerzen ab ts -> dict mit Multiples, oder None."""
    k = _kerzen(chain, pool, ts)
    if not k or not k[0]:
        return None
    kerzen, sek = k
    start = None
    for x in kerzen:
        if int(x[0]) <= ts < int(x[0]) + sek:
            start = x
            break
    if start is None:
        nach0 = [x for x in kerzen if int(x[0]) >= ts]
        if not nach0 or int(nach0[0][0]) - ts > 2 * sek:
            return None
        start = nach0[0]
    try:
        p0 = float(start[4])
    except (TypeError, ValueError):
        return None
    if not p0:
        return None
    nach = [x for x in kerzen if int(x[0]) > int(start[0])]
    out = {"candle_s": sek}
    best, best_t = None, None
    for a, b in zip(nach, nach[1:]):
        try:
            if float(a[5] or 0) < MIN_KERZE_USD or float(b[5] or 0) < MIN_KERZE_USD:
                continue
            h = min(float(a[2]), float(b[2]))
        except (TypeError, ValueError):
            continue
        if best is None or h > best:
            best, best_t = h, int(b[0])
    if best:
        out["peak_x"] = round(best / p0, 3)
        out["peak_after_h"] = round((best_t - ts) / 3600, 1)
        tief = [float(x[3]) for x in nach if int(x[0]) <= best_t and x[3]]
        out["low_before_peak_x"] = round(min(tief) / p0, 3) if tief else None
    else:
        out["peak_x"] = 1.0
    for name, dt in PUNKTE:
        z = ts + dt
        if time.time() < z:
            continue
        bis = [x for x in kerzen if int(x[0]) <= z]
        if bis and z - int(bis[-1][0]) <= max(sek * 4, 3 * 3600):
            try:
                out[name] = round(float(bis[-1][4]) / p0, 3)
            except (TypeError, ValueError):
                pass
        elif bis:
            out[name] = round(float(bis[-1][4]) / p0, 3)   # danach kein Handel mehr: letzter Kurs gilt
    try:
        out["now_x"] = round(float(kerzen[-1][4]) / p0, 3)
    except (TypeError, ValueError):
        pass
    return out


_EVAL_LOCK = __import__("threading").Lock()


def auswerten(max_n=None):
    """Offene Meldungen am Chart messen. Neu gemessen wird alle 6 h, bis die Meldung 7 Tage und 1 h alt ist."""
    if not _EVAL_LOCK.acquire(blocking=False):
        return 0
    try:
        return _auswerten(max_n)
    finally:
        _EVAL_LOCK.release()


def _auswerten(max_n=None):
    import smart
    max_n = max_n or EVAL_MAX
    prot = _kv("rotation_log", [])
    n = 0
    for r in reversed(prot):
        ts = int(r.get("ts") or 0)
        alter = time.time() - ts
        if alter < 3600 or alter > 40 * 86400:
            continue
        for gruppe in ("coins", "base"):
            for c in r.get(gruppe) or []:
                if n >= max_n:
                    break
                ev = c.get("eval") or {}
                if ev.get("final") or (ev and time.time() - float(ev.get("at") or 0) < 6 * 3600):
                    continue
                if not c.get("pool"):
                    try:
                        c["pool"] = smart._gt_info(c["chain"], c["token"]).get("pool")
                    except Exception:
                        c["pool"] = None
                    time.sleep(2.2)
                m = _messen(c["chain"], c.get("pool"), ts) if c.get("pool") else None
                time.sleep(2.2)
                n += 1
                if m is None:
                    c["eval"] = {"at": int(time.time()), "status": "no_chart", "final": alter > 8 * 86400}
                    continue
                m.update(at=int(time.time()), status="ok", final=alter > 7 * 86400 + 3600)
                c["eval"] = m
                if c.get("mcap0") and m.get("peak_x"):
                    c["peak"] = max(float(c.get("peak") or 0), float(c["mcap0"]) * m["peak_x"])
    if n:
        # frisch lesen und nur die Messwerte eintragen: tick() und nachverfolgen() schreiben dasselbe Protokoll
        neu = {}
        for r in prot:
            for gruppe in ("coins", "base"):
                for c in r.get(gruppe) or []:
                    if c.get("eval"):
                        neu[(r.get("ts"), gruppe, c.get("token"))] = (c["eval"], c.get("pool"), c.get("peak"))
        frisch = _kv("rotation_log", [])
        for r in frisch:
            for gruppe in ("coins", "base"):
                for c in r.get(gruppe) or []:
                    k = (r.get("ts"), gruppe, c.get("token"))
                    if k in neu:
                        c["eval"], c["pool"] = neu[k][0], neu[k][1] or c.get("pool")
                        if neu[k][2]:
                            c["peak"] = max(float(c.get("peak") or 0), float(neu[k][2]))
        _kv_set("rotation_log", frisch[-LOG_MAX:])
    return n


def _median(v):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return round(v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2, 3)


def _stat(liste):
    """Kennzahlen fuer eine Liste gemessener Coins."""
    ok = [c["eval"] for c in liste if (c.get("eval") or {}).get("status") == "ok"]
    out = {"coins": len(liste), "measured": len(ok)}
    if not ok:
        return out
    for x in (2, 5, 10):
        out[f"hit_{x}x"] = round(sum(1 for e in ok if (e.get("peak_x") or 0) >= x) / len(ok), 3)
    out["median_peak_x"] = _median([e.get("peak_x") for e in ok])
    out["median_hours_to_peak"] = _median([e.get("peak_after_h") for e in ok if (e.get("peak_x") or 0) >= 2])
    for name, _ in PUNKTE:
        v = [e.get(name) for e in ok if e.get(name) is not None]
        if v:
            out[f"median_{name}"] = _median(v)
            out[f"avg_return_{name}"] = round(sum(v) / len(v) - 1, 3)
            out[f"n_{name}"] = len(v)
    tp = [min(e["peak_x"], 2.0) if (e.get("peak_x") or 0) >= 2 else e.get("x_24h") for e in ok]
    tp = [x for x in tp if x is not None]
    if tp:
        out["avg_return_sell_2x_or_24h"] = round(sum(tp) / len(tp) - 1, 3)
    return out


def auswertung():
    """Admin: jede Meldung mit allen Messwerten, Kennzahlen und dem Vergleich mit dem Thema."""
    prot = [r for r in _kv("rotation_log", []) if _ok_beleg(r)]
    alle_c = [c for r in prot for c in r.get("coins") or []]
    alle_b = [c for r in prot for c in r.get("base") or []]
    themen = {}
    for r in prot:
        themen.setdefault(r.get("theme_name") or r.get("theme"), []).extend(r.get("coins") or [])
    zeilen = []
    for r in reversed(prot):
        def zeile(c, rolle):
            e = c.get("eval") or {}
            return {"role": rolle, "ticker": c.get("ticker"), "chain": c.get("chain"), "token": c.get("token"),
                    "elite_wallets": c.get("wallets"), "mcap_at_alert": c.get("mcap0"),
                    "peak_mcap": (float(c["mcap0"]) * e["peak_x"]) if c.get("mcap0") and e.get("peak_x") else c.get("peak"),
                    **{k: e.get(k) for k in ("status", "peak_x", "peak_after_h", "low_before_peak_x", "x_1h", "x_6h",
                                             "x_24h", "x_7d", "now_x", "final")}}
        zeilen.append({"ts": r.get("ts"), "from": r.get("from_name"), "to": r.get("theme_name"), "wallets": r.get("wallets"),
                       "sent": r.get("sent") or 0, "beta_sent": r.get("beta_sent") or 0,
                       "proof": {k: (r.get("proof") or {}).get(k) for k in ("symbol", "entry", "peak", "x")},
                       "coins": [zeile(c, "alert") for c in r.get("coins") or []] +
                                [zeile(c, "compare") for c in r.get("base") or []]})
    return {"live": ist_live(), "rotations": len(prot), "first": prot[0].get("ts") if prot else None,
            "alerts": _stat(alle_c), "compare": _stat(alle_b),
            "by_theme": {k: _stat(v) for k, v in sorted(themen.items(), key=lambda kv: -len(kv[1]))},
            "scout_found": _scout_stand().get("found"),
            "proven_wallets": _elite_stand(),
            "rules_now": {"elite_x": ELITE_X, "elite_entry_mcap": ELITE_ENTRY, "min_wallets": MIN_W,
                          "recent_h": RECENT_H, "base_h": BASE_H},
            "rules": "Entry = close of the candle with the alert. Peak = the lower high of two candles in a row with "
                     "real volume, so a single wick does not count. Compare = up to 3 coins of the same theme bought in "
                     "the same hours, but not by the proven wallets.",
            "list": zeilen}


def auswertung_csv():
    import csv
    import io
    b = io.StringIO()
    w = csv.writer(b)
    sp = ["alert_utc", "from", "to", "proven_wallets", "sent_to", "role", "ticker", "chain", "token", "mcap_at_alert",
          "peak_mcap", "peak_x", "hours_to_peak", "low_before_peak_x", "x_1h", "x_6h", "x_24h", "x_7d", "now_x", "status"]
    w.writerow(sp)
    for r in auswertung()["list"]:
        for c in r["coins"]:
            w.writerow([time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"] or 0)), r["from"], r["to"], r["wallets"],
                        r["sent"] or r["beta_sent"], c["role"], c["ticker"], c["chain"], c["token"], c["mcap_at_alert"],
                        c["peak_mcap"], c["peak_x"], c["peak_after_h"], c["low_before_peak_x"], c["x_1h"], c["x_6h"],
                        c["x_24h"], c["x_7d"], c["now_x"], c["status"]])
    return b.getvalue()


# ── "Ist gelaufen" mit Chart-Bild (seit 2.82.51) ────────────────────────
# Laeuft ein gemeldeter Coin auf 2x, 3x, 5x, 10x ... seit der Meldung, bekommen dieselben Empfaenger wie bei der
# Meldung (Holder, bei Schalter aus nur Tester) eine Nachricht mit Chart: Call-Punkt, Kerzen bis jetzt, Multiple.
# Pro Coin hoechstens eine solche Nachricht je Stunde und je Stufe nur einmal. Keine Uhrzeiten, nur Dauer.

STUFEN = (2, 3, 5, 10, 20, 50, 100)
RAN_TAGE = _f("ROTATION_RAN_DAYS", 7)
KETTE = {"solana": "Solana", "robinhood": "Robinhood Chain"}


def _kerzen_fenster(chain, pool, call_ts):
    """Kerzen von etwas vor dem Call bis jetzt, Aufloesung passend zur Dauer."""
    netz = GT_NETZ.get(chain)
    if not (netz and pool):
        return None
    jetzt = time.time()
    lauf = max(600, jetzt - call_ts)
    start = call_ts - max(lauf * 0.5, 1800)
    gesamt = jetzt - start
    for art, agg, sek in (("minute", 1, 60), ("minute", 5, 300), ("minute", 15, 900), ("hour", 1, 3600), ("hour", 4, 14400)):
        if gesamt / sek <= 90:
            break
    try:
        r = requests.get(f"{GT}/networks/{netz}/pools/{pool}/ohlcv/{art}",
                         params={"aggregate": agg, "limit": min(1000, int(gesamt / sek) + 3), "currency": "usd"},
                         headers={"Accept": "application/json"}, timeout=15)
        if r.status_code != 200:
            return None
        k = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        return sorted([x for x in k if x and len(x) > 5 and int(x[0]) >= start - sek], key=lambda x: int(x[0]))
    except Exception as e:
        log.debug(f"rotation fenster {pool[:8]}: {e}")
        return None


def lauf_bild(c, call_ts, mcap_jetzt):
    """PNG fuer einen Coin, oder None."""
    import rotkarte
    k = _kerzen_fenster(c["chain"], c.get("pool"), call_ts)
    if not k:
        return None
    sek = (int(k[1][0]) - int(k[0][0])) if len(k) > 1 else 60
    p_call = None
    for x in k:
        if int(x[0]) <= call_ts < int(x[0]) + sek:
            p_call = float(x[4])
    if p_call is None:
        nach = [x for x in k if int(x[0]) >= call_ts]
        p_call = float(nach[0][1]) if nach else None
    if not p_call:
        return None
    return rotkarte.karte(c.get("ticker") or "?", k, call_ts, float(c["mcap0"]), float(c["mcap0"]) / p_call, mcap_jetzt)


def _worte(s):
    s = max(60, int(s))
    if s < 3600:
        m = s // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    if s < 2 * 86400:
        h = round(s / 3600)
        return f"{h} hour{'s' if h != 1 else ''}"
    t = round(s / 86400)
    return f"{t} days"


def lauf_text(c, call_ts, mcap_jetzt):
    import rotkarte
    x = mcap_jetzt / float(c["mcap0"])
    return (f"🟢 ${c.get('ticker') or '?'} is up {x:.1f}x since the Meta Rotation call\n\n"
            f"Called at {rotkarte.geld(c['mcap0'])} on {KETTE.get(c['chain'], c['chain'])}.\n"
            f"Now {rotkarte.geld(mcap_jetzt)}, {_worte(time.time() - call_ts)} later.\n\n"
            f"{c['token']}\n\nPast results are no guarantee of future results.")


def _foto_an_alle(text, png, nur_beta):
    import motor
    from config import cfg
    n = 0
    try:
        nutzer = list(motor._nutzer())
    except Exception:
        nutzer = []
    for adr in nutzer:
        try:
            if nur_beta and not motor.beta(adr):
                continue
            if not motor.offen(adr) or not gewollt(adr) or not zugang(adr):
                continue
            u = motor._tg_von(adr)
            if u and png:
                r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendPhoto",
                                  data={"chat_id": u["tg"], "caption": text[:1000]},
                                  files={"photo": ("oddsengine-meta-rotation.png", png, "image/png")}, timeout=60)
                if r.ok and r.json().get("ok"):
                    n += 1
                    continue
            motor.senden(adr, text)
            n += 1
        except Exception as e:
            log.debug(f"rotation foto {adr[:8]}: {e}")
    return n


def gelaufen():
    """Gemeldete Coins der letzten Tage pruefen und bei einer neuen Stufe die Nachricht mit Bild schicken."""
    import smart
    if not _EVAL_LOCK.acquire(blocking=False):
        return 0
    try:
        prot = _kv("rotation_log", [])
        neu, n = {}, 0
        for r in prot:
            ts = int(r.get("ts") or 0)
            if time.time() - ts > RAN_TAGE * 86400 or not (r.get("sent") or r.get("beta_sent")):
                continue
            for c in r.get("coins") or []:
                if not c.get("mcap0") or time.time() - float(c.get("ran_ts") or 0) < 3600:
                    continue
                info = smart._gt_info(c["chain"], c["token"])
                time.sleep(1.5)
                mc = info.get("mcap")
                if not mc:
                    continue
                c["pool"] = c.get("pool") or info.get("pool")
                if mc > float(c.get("peak") or 0):
                    c["peak"] = mc
                stufe = max([s for s in STUFEN if mc >= s * float(c["mcap0"])], default=0)
                if stufe <= int(c.get("ran") or 0):
                    continue
                png = None
                try:
                    png = lauf_bild(c, ts, mc)
                except Exception as e:
                    log.warning(f"rotation bild {c.get('ticker')}: {e}")
                gesendet = _foto_an_alle(lauf_text(c, ts, mc), png, nur_beta=not r.get("sent"))
                c["ran"], c["ran_ts"] = stufe, int(time.time())
                c.setdefault("ran_log", []).append({"x": stufe, "mcap": mc, "ts": int(time.time()), "to": gesendet})
                neu[(ts, c["token"])] = {k: c.get(k) for k in ("ran", "ran_ts", "ran_log", "pool", "peak")}
                n += 1
                log.info(f"rotation ran: {c.get('ticker')} {stufe}x, sent to {gesendet}")
        if neu:
            frisch = _kv("rotation_log", [])
            for r in frisch:
                for c in r.get("coins") or []:
                    k = (int(r.get("ts") or 0), c.get("token"))
                    if k in neu:
                        c.update({a: b for a, b in neu[k].items() if b is not None})
            _kv_set("rotation_log", frisch[-LOG_MAX:])
        return n
    finally:
        _EVAL_LOCK.release()


def _elite_stand():
    """Wie viele bewiesene Wallets gibt es, und wie viele davon kaufen gerade (Grundlage jeder Rotation)."""
    try:
        el = elite(frisch=True)
        pb = pruefbericht()
        ab = int(time.time() - (RECENT_H + BASE_H) * 3600)
        aktiv = 0
        if el:
            rows = _q("SELECT DISTINCT chain, wallet FROM iw_sight WHERE ts >= %s", (ab,)) or []
            aktiv = sum(1 for c, w in rows if (c, w) in el)
        by = {}
        for (c, _), b in el.items():
            by[c] = by.get(c, 0) + 1
        return {"confirmed": len(el), "by_chain": by, "active_last_54h": aktiv,
                "candidates": pb.get("candidates"), "wallets_claimed": pb.get("wallets_claimed"),
                "by_source": pb.get("by_source"), "outcomes": pb.get("outcomes")}
    except Exception as e:
        return {"error": str(e)[:120]}
