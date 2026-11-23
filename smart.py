"""
smart.py: Smart-Money-Calls von Your Engine. Seit 2.40.

Frage: kaufen gerade mehrere Wallets, die unsere eigene Bewertung (iw_wallets) unabhaengig voneinander
hoch einstuft, denselben Coin? Wenn SMART_MIN_WALLETS davon innerhalb von SMART_WINDOW_SEC auf einem Coin
landen, ist das der Call. Die Wallets haben nichts miteinander zu tun, ausser dass unsere Bewertung sie
in dieselbe Liga gesetzt hat. Gleicher Geldgeber zaehlt nur einmal.

  tick()        aus ai.waechter: Runde ueber die Beobachtungsliste, Gruppen pruefen, Calls nachverfolgen
  stand()       Admin: Liste, Runde, Sichtungen, Calls, Abfragen heute
  bilanz()      jeder Call mit Multiple seit dem Call (Trefferquote)
  setzen(adr)   Nutzer schaltet Calls fuer sich an oder aus (motor)

Schalter: 'smart' liest im Hintergrund mit und schreibt jeden Call in die Bilanz (auch bevor Your Engine
live ist, nur der Admin sieht es). 'smart_calls' schickt die Calls an Nutzer, die sie angeschaltet haben,
und an SMART_CHAT_ID (optional, ein Telegram-Kanal).

Kosten: Solana ueber HELIUS_API_KEY (eine Signaturabfrage je Wallet und Runde, eine Transaktion je neuem
Swap), Robinhood ueber BLOCKSCOUT_API_KEY. Taegliche Obergrenze SMART_DAILY_RPC.
"""
import calendar
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import storage
from logger import log

MIN_SCORE = int(os.getenv("SMART_MIN_SCORE", "36"))
LISTE_N = int(os.getenv("SMART_WATCHLIST", "150"))
FENSTER = int(os.getenv("SMART_WINDOW_SEC", "900"))
# Seit 2.45: wie oft jedes Wallet geprueft wird, unabhaengig vom Fenster. Mit Helius-Webhook kommen Solana-Kaeufe
# sofort, dann reicht fuer Solana eine langsame Sicherheitsrunde.
ABFRAGE = int(os.getenv("SMART_POLL_SEC", "120"))
ABFRAGE_MIT_HOOK = int(os.getenv("SMART_POLL_WITH_HOOK_SEC", "900"))
MIN_W = int(os.getenv("SMART_MIN_WALLETS", "3"))
MIN_SOL = float(os.getenv("SMART_MIN_SOL", "0.05"))
MIN_MCAP = float(os.getenv("SMART_MIN_MCAP", "0"))
MAX_MCAP = float(os.getenv("SMART_MAX_MCAP", "0"))
TAKT = int(os.getenv("SMART_INTERVAL_SEC", "60"))
TAGES_RPC = int(os.getenv("SMART_DAILY_RPC", "150000"))
MARGE = 0.85
KETTEN = ("solana", "robinhood")
# Seit 2.70.0: Early-Cluster. Ein Call nur, wenn die Top-Wallets FRUEH und ZUSAMMEN reingehen: der Coin ist beim
# ersten bewerteten Kauf hoechstens EARLY_MAX_AGE_MIN alt, die MIN_W Kaeufe liegen in EARLY_WINDOW_SEC, MC unter
# EARLY_MAX_MCAP. Spaete Gruppen werden weiter aufgezeichnet (level "late"), zaehlen aber nicht als Call, so sieht man
# den Unterschied in der Bilanz. SMART_MODE=classic schaltet zurueck auf "3 in 15 Minuten, egal wann".
MODUS = os.getenv("SMART_MODE", "early").strip().lower()
EARLY_MAX_AGE_MIN = float(os.getenv("SMART_EARLY_MAX_AGE_MIN", "30"))
EARLY_WINDOW = int(os.getenv("SMART_EARLY_WINDOW_SEC", "300"))
EARLY_MAX_MCAP = float(os.getenv("SMART_EARLY_MAX_MCAP", "150000"))
GT = "https://api.geckoterminal.com/api/v2"
GT_NETZ = {"solana": "solana", "robinhood": "robinhood", "base": "base", "ethereum": "eth", "bsc": "bsc", "arbitrum": "arbitrum"}
NICHT_GEZAEHLT = ("red", "small", "big", "late")
STUFEN = (2, 3, 5, 10, 25, 50, 100)
# Seit 2.82.4: Zugang. Calls bekommt nur, wer die Stufe SMART_TIER live haelt (signierte Wallet plus verknuepfte, bei
# jedem Call frisch on chain gelesen) oder einen Wochenpass hat. Der Pass wird aus dem Engine-Guthaben bezahlt
# (aufgeladen mit $ODDS oder USDG) und verlaengert sich, solange der Nutzer das will und Guthaben da ist.
ZUGANG_STUFE = os.getenv("SMART_TIER", "holder").strip().lower() or "holder"
PASS_USD = float(os.getenv("SMART_PASS_USD", "25") or 25)
PASS_TAGE = float(os.getenv("SMART_PASS_DAYS", "7") or 7)
# Seit 2.82.4: Preis in ETH (Standard 0.01 ETH je Woche). Abgebucht wird der Dollarwert zum ETH-Kurs im Moment des
# Kaufs. Ohne Kurs gilt SMART_PASS_USD. SMART_PASS_ETH=0 schaltet auf den festen Dollarpreis.
PASS_ETH = float(os.getenv("SMART_PASS_ETH", "0.01") or 0)


def pass_preis():
    """-> (usd, eth oder None)"""
    if PASS_ETH > 0:
        try:
            import portfolio
            k = portfolio._preis("ETH")
            if k and k > 0:
                return round(PASS_ETH * float(k), 2), PASS_ETH
        except Exception as e:
            log.debug(f"smart pass eth kurs: {e}")
    return PASS_USD, None

WSOL = "So11111111111111111111111111111111111111112"
STABIL_SOL = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
              "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", WSOL}
STABIL_EVM = {"0x5fc5360d0400a0fd4f2af552add042d716f1d168", "0x0bd7d308f8e1639fab988df18a8011f41eacad73"}

_T = {}
_ZEIGER = [0]
_LETZTE = {}
_GEPRUEFT = {}
_HOOK = {"ok_bis": 0, "events": 0, "letztes": 0, "fehler": None, "adressen": 0}
_LISTE = {"ts": 0, "w": []}
_SCHEMA = [False]
_LAEUFT = threading.Lock()


# ── Datenbank ────────────────────────────────────────────────────────────

def _q(sql, args=(), eins=False):
    import intel
    if not _SCHEMA[0] and intel._pg():
        intel._q("""CREATE TABLE IF NOT EXISTS iw_sight (
            chain TEXT NOT NULL, token TEXT NOT NULL, wallet TEXT NOT NULL, score INTEGER, native DOUBLE PRECISION,
            sig TEXT NOT NULL, ts BIGINT NOT NULL, funder TEXT, PRIMARY KEY (chain, wallet, sig, token))""")
        intel._q("CREATE INDEX IF NOT EXISTS ix_iw_sight_ts ON iw_sight(ts)")
        intel._q("""CREATE TABLE IF NOT EXISTS iw_calls (
            chain TEXT NOT NULL, token TEXT NOT NULL, ts BIGINT NOT NULL, symbol TEXT, wallets TEXT, n INTEGER,
            score_sum INTEGER, mcap0 DOUBLE PRECISION, peak DOUBLE PRECISION, last DOUBLE PRECISION, checked BIGINT,
            stufe INTEGER DEFAULT 0, level TEXT, sent BOOLEAN DEFAULT FALSE, recipients TEXT, PRIMARY KEY (chain, token, ts))""")
        intel._q("CREATE INDEX IF NOT EXISTS ix_iw_calls_ts ON iw_calls(ts)")
        for sql in ("ALTER TABLE iw_calls ADD COLUMN IF NOT EXISTS price0 DOUBLE PRECISION",
                    "ALTER TABLE iw_calls ADD COLUMN IF NOT EXISTS pool TEXT",
                    "ALTER TABLE iw_calls ADD COLUMN IF NOT EXISTS age_min DOUBLE PRECISION",
                    "ALTER TABLE iw_calls ADD COLUMN IF NOT EXISTS peak_x DOUBLE PRECISION",
                    "ALTER TABLE iw_calls ADD COLUMN IF NOT EXISTS spread_sec INTEGER"):
            intel._q(sql)
        _SCHEMA[0] = True
    return intel._q(sql, args, eins)


def _rpc_zaehlen(n=1):
    tag = time.strftime("%Y%m%d", time.gmtime())
    if _T.get("rpc_tag") != tag:
        _T["rpc_tag"], _T["rpc"] = tag, 0
    _T["rpc"] = _T.get("rpc", 0) + n


def _rpc_frei(n):
    tag = time.strftime("%Y%m%d", time.gmtime())
    if _T.get("rpc_tag") != tag:
        return True
    return _T.get("rpc", 0) + n <= TAGES_RPC


# ── Beobachtungsliste ────────────────────────────────────────────────────

def liste():
    if time.time() - _LISTE["ts"] < 600 and _LISTE["w"]:
        return _LISTE["w"]
    rows = _q("SELECT wallet, chain, score FROM iw_wallets WHERE score >= %s AND chain IN ('solana','robinhood') "
              "ORDER BY score DESC, best_x DESC LIMIT %s", (MIN_SCORE, LISTE_N)) or []
    w = [{"wallet": r[0], "chain": r[1], "score": int(r[2] or 0)} for r in rows]
    # Seit 2.82.6: die nachweislich erfolgreichen Wallets (Meta Rotation) werden immer mit abgefragt
    try:
        import rotation
        da = {(x["chain"], x["wallet"]) for x in w}
        w += [x for x in rotation.elite_wallets() if (x["chain"], x["wallet"]) not in da]
    except Exception as e:
        log.debug(f"smart liste elite: {e}")
    _LISTE.update(ts=time.time(), w=w)
    return _LISTE["w"]


def _runde_groesse(n):
    """So viele je Runde, dass die ganze Liste innerhalb des Fensters einmal drankommt (mit Reserve)."""
    if n <= 0:
        return 0
    runden = max(1, int(FENSTER * MARGE // max(1, TAKT)))
    return max(1, -(-n // runden))


# ── Kaeufe lesen ─────────────────────────────────────────────────────────

def _swaps_sol(w):
    """Neue Kaeufe eines Solana-Wallets seit der letzten Runde: [(token, sol, sig, ts)]."""
    import intel
    seit = int(time.time()) - FENSTER
    opt = {"limit": 15}
    if _LETZTE.get(w):
        opt["until"] = _LETZTE[w]
    _rpc_zaehlen()
    sigs = intel._sol("getSignaturesForAddress", [w, opt]) or []
    if not isinstance(sigs, list):
        return []
    if sigs:
        _LETZTE[w] = sigs[0].get("signature")
    neu = [s for s in sigs if not s.get("err") and (s.get("blockTime") or 0) >= seit]
    if not neu:
        return []
    _rpc_zaehlen(len(neu))
    txs = intel._sol_batch([intel._tx(s["signature"]) for s in neu], 10)
    out = []
    for s, tx in zip(neu, txs):
        out += _kauf_aus_tx(tx, w, s["signature"], s.get("blockTime") or 0)
    return out


def _kauf_aus_tx(tx, w, sig, ts):
    if not isinstance(tx, dict):
        return []
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    vor, nach = {}, {}
    for liste_, ziel in ((meta.get("preTokenBalances") or [], vor), (meta.get("postTokenBalances") or [], nach)):
        for b in liste_:
            if b.get("owner") != w or not b.get("mint"):
                continue
            ziel[b["mint"]] = ziel.get(b["mint"], 0) + float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    delta = {m: nach.get(m, 0) - vor.get(m, 0) for m in set(vor) | set(nach)}
    import intel
    keys = intel._schluessel(tx)
    sol = 0.0
    if w in keys:
        i = keys.index(w)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol = (pre[i] - post[i]) / 1e9
    sol += max(0.0, -delta.get(WSOL, 0))
    usd = sum(max(0.0, -delta.get(m, 0)) for m in STABIL_SOL if m != WSOL)
    gekauft = [m for m, d in delta.items() if d > 0 and m not in STABIL_SOL]
    if len(gekauft) != 1:
        return []
    einsatz = sol if sol > 0 else (usd / 150.0 if usd else 0)
    return [(gekauft[0], round(einsatz, 4), sig, ts)]


def _swaps_evm(chain, w):
    import intel
    seit = int(time.time()) - FENSTER
    _rpc_zaehlen()
    d = intel._bs(chain, f"/api/v2/addresses/{w}/token-transfers", {"type": "ERC-20", "filter": "to"}) or {}
    out = []
    for it in d.get("items") or []:
        to = ((it.get("to") or {}).get("hash") or "").lower()
        tok = (it.get("token") or {})
        adr = str(tok.get("address_hash") or tok.get("address") or "").lower()
        if to != w.lower() or not adr or adr in STABIL_EVM:
            continue
        try:
            ts = calendar.timegm(time.strptime(str(it.get("timestamp"))[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if ts < seit:
            continue
        if not (it.get("from") or {}).get("is_contract"):
            continue
        sig = it.get("transaction_hash") or it.get("tx_hash") or ""
        out.append((adr, 0.0, sig, ts))
    return out


def _runde():
    w = liste()
    if not w:
        _T["leer"] = time.time()
        return 0
    jetzt = time.time()
    hook = hook_aktiv()

    def intervall(x):
        return ABFRAGE_MIT_HOOK if (hook and x["chain"] == "solana") else ABFRAGE
    faellig = sorted([x for x in w if jetzt - _GEPRUEFT.get(x["wallet"], 0) >= intervall(x)],
                     key=lambda x: _GEPRUEFT.get(x["wallet"], 0))
    # Hoechstens so viele, wie das Tagesbudget im Schnitt hergibt (2 Abfragen je Wallet), mindestens 5
    je_runde = max(5, int(TAGES_RPC / 2 / max(1, 86400 / TAKT)))
    teil = faellig[:je_runde]
    if not teil:
        return 0
    if not _rpc_frei(len(teil) * 2):
        _T["budget_stop"] = time.time()
        return 0
    for x in teil:
        _GEPRUEFT[x["wallet"]] = jetzt

    def eins(x):
        try:
            swaps = _swaps_sol(x["wallet"]) if x["chain"] == "solana" else _swaps_evm(x["chain"], x["wallet"])
        except Exception as e:
            log.debug(f"smart {x['wallet'][:8]}: {e}")
            return 0
        neu = 0
        for tok, einsatz, sig, ts in swaps:
            if x["chain"] == "solana" and einsatz < MIN_SOL:
                continue
            _q("INSERT INTO iw_sight (chain, token, wallet, score, native, sig, ts) VALUES (%s,%s,%s,%s,%s,%s,%s) "
               "ON CONFLICT DO NOTHING", (x["chain"], tok, x["wallet"], x["score"], einsatz, sig, ts))
            neu += 1
        return neu
    with ThreadPoolExecutor(5) as ex:
        gesehen = sum(ex.map(eins, teil))
    sol_n = sum(1 for x in w if x["chain"] == "solana")
    _T["runde"] = {"ts": int(time.time()), "wallets": len(teil), "of": len(w), "sightings": gesehen, "due": len(faellig),
                   "check_every_min": round(ABFRAGE / 60, 1),
                   "solana_every_min": round((ABFRAGE_MIT_HOOK if hook else ABFRAGE) / 60, 1), "solana_wallets": sol_n}
    return gesehen


# ── Gruppen und Calls ────────────────────────────────────────────────────

def _geldgeber(chain, wallets):
    """Wer hat jedes Wallet zuerst finanziert? Gleicher Geldgeber zaehlt als ein Wallet."""
    if chain != "solana":
        return {w: w for w in wallets}
    import intel
    ks = [{"wallet": w, "ts": int(time.time())} for w in wallets]
    try:
        intel._alter_sol(ks, juenger_als=0)
    except Exception:
        pass
    return {k["wallet"]: k.get("funder") or k["wallet"] for k in ks}


def gruppen():
    fenster = max(FENSTER, EARLY_WINDOW)
    seit = int(time.time()) - fenster
    return _q("SELECT chain, token, COUNT(DISTINCT wallet), SUM(score), MIN(ts), MAX(ts) FROM iw_sight WHERE ts >= %s "
              "GROUP BY chain, token HAVING COUNT(DISTINCT wallet) >= %s", (seit, MIN_W)) or []


def _gt_info(chain, token):
    """Preis, Market Cap und groesster Pool von GeckoTerminal (kennt auch die pump.fun-Kurve). -> dict oder {}"""
    netz = GT_NETZ.get(chain)
    if not netz:
        return {}
    try:
        r = requests.get(f"{GT}/networks/{netz}/tokens/{token}", params={"include": "top_pools"},
                         headers={"Accept": "application/json"}, timeout=12)
        if r.status_code != 200:
            return {}
        d = r.json()
        a = (d.get("data") or {}).get("attributes") or {}
        pools = ((d.get("data") or {}).get("relationships") or {}).get("top_pools", {}).get("data") or []
        pool = pools[0]["id"].split("_", 1)[-1] if pools else None

        def f(x):
            try:
                return float(x) if x not in (None, "") else None
            except Exception:
                return None
        return {"price": f(a.get("price_usd")), "mcap": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")), "pool": pool}
    except Exception as e:
        log.debug(f"smart gt {token[:8]}: {e}")
        return {}


def _kette_frueh(ts_liste, k, fenster):
    """Kleinstes Zeitfenster, in dem k verschiedene Kaeufe liegen. -> (start_ts, spread_sec) oder None"""
    ts_liste = sorted(ts_liste)
    best = None
    for i in range(0, len(ts_liste) - k + 1):
        d = ts_liste[i + k - 1] - ts_liste[i]
        if d <= fenster and (best is None or d < best[1]):
            best = (ts_liste[i], d)
    return best


def _pruefen():
    import scan
    for chain, token, n, ssum, t0, t1 in gruppen():
        if _q("SELECT 1 FROM iw_calls WHERE chain=%s AND token=%s AND ts >= %s", (chain, token, int(time.time()) - 86400), eins=True):
            continue
        seit = int(time.time()) - max(FENSTER, EARLY_WINDOW)
        rows = _q("SELECT wallet, MAX(score), SUM(native), MIN(ts) FROM iw_sight WHERE chain=%s AND token=%s AND ts >= %s "
                  "GROUP BY wallet ORDER BY MAX(score) DESC", (chain, token, seit)) or []
        geld = _geldgeber(chain, [r[0] for r in rows])
        unabh = {}
        for r in rows:
            g = geld.get(r[0], r[0])
            if g not in unabh or (r[3] or 0) < (unabh[g][3] or 0):
                unabh[g] = r          # je Geldgeber der frueheste Kauf
        if len(unabh) < MIN_W:
            continue
        kauf_ts = [int(r[3] or 0) for r in unabh.values()]
        fenster = EARLY_WINDOW if MODUS == "early" else FENSTER
        eng = _kette_frueh(kauf_ts, MIN_W, fenster)
        if not eng:
            continue          # die Kaeufe liegen zu weit auseinander, das ist kein gemeinsamer Einstieg
        try:
            r = scan.analyse(f"{chain}:{token}", deep=False)
        except Exception as e:
            log.warning(f"smart analyse {token[:10]}: {e}")
            continue
        if not r.get("ok", True) or r.get("error"):
            continue
        m = r.get("market") or {}
        gt = _gt_info(chain, token)
        mc = float(m.get("mcap") or 0) or float(gt.get("mcap") or 0)
        preis = gt.get("price") or (float(m.get("price") or 0) or None)
        erst = min(kauf_ts)
        alter_min = None
        if m.get("created_ms"):
            alter_min = round(max(0.0, (erst * 1000 - float(m["created_ms"])) / 60000), 1)
        lvl = (r.get("verdict") or {}).get("level")
        jetzt = int(time.time())
        wl = [{"wallet": x[0], "score": int(x[1] or 0), "sol": round(float(x[2] or 0), 3), "ts": int(x[3] or 0)} for x in unabh.values()]
        notiz = None
        if lvl == "red":
            notiz = "red"
        elif MODUS == "early" and alter_min is not None and alter_min > EARLY_MAX_AGE_MIN:
            notiz = "late"
        elif MODUS == "early" and EARLY_MAX_MCAP and mc and mc > EARLY_MAX_MCAP:
            notiz = "late"
        elif MIN_MCAP and mc and mc < MIN_MCAP:
            notiz = "small"
        elif MAX_MCAP and mc > MAX_MCAP:
            notiz = "big"
        _q("INSERT INTO iw_calls (chain, token, ts, symbol, wallets, n, score_sum, mcap0, peak, last, checked, level, price0, pool, "
           "age_min, peak_x, spread_sec) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
           (chain, token, jetzt, r.get("symbol"), json.dumps(wl), len(wl), sum(x["score"] for x in wl), mc or None, mc or None,
            mc or None, jetzt, notiz or lvl, preis, gt.get("pool"), alter_min, 1.0 if preis else None, int(eng[1])))
        if notiz:
            log.info(f"smart: {r.get('symbol')} {len(wl)} wallets, not sent ({notiz}, age {alter_min} min)")
            continue
        _senden_call(chain, token, jetzt, r, wl, t0)


def _text(r, wl, t0):
    import scan
    mins = max(1, round((max(x["ts"] for x in wl) - min(x["ts"] for x in wl)) / 60))
    import html as H
    z = [f"🟢 <b>Smart money:</b> {len(wl)} rated wallets bought ${H.escape(str(r.get('symbol') or scan.kurz(r['address'])))} within {mins} min", ""]
    for x in sorted(wl, key=lambda x: -x["score"])[:6]:
        z.append(f"{scan.kurz(x['wallet'])}  score {x['score']}" + (f" · {x['sol']} SOL" if x.get("sol") else ""))
    z += ["", "Different funders, no shared history we could find. Our own scoring rated each of them for being early in coins that ran.", ""]
    return "\n".join(z) + scan.karte(r, html=True)


def _kv_json(k, d=None):
    try:
        roh = storage.kv_get(k)
        return json.loads(roh) if roh else d
    except Exception:
        return d


def pass_stand(adr):
    """Wochenpass: bis wann, verlaengert er sich."""
    import holder
    adr = holder.norm(adr)
    p = _kv_json("smart_pass_" + adr, {}) or {}
    bis = float(p.get("until") or 0)
    return {"active": bis > time.time(), "until": int(bis) or None, "renew": bool(p.get("renew", True)),
            "price_usd": pass_preis()[0], "price_eth": PASS_ETH or None, "days": PASS_TAGE}


def zugang(adr, frisch=True):
    """-> (art, info). art: 'holder', 'pass' oder None."""
    import holder
    adr = holder.norm(adr)
    try:
        st = holder.status(adr, frisch=frisch)
    except TypeError:
        st = holder.status(adr)
    except Exception as e:
        log.debug(f"smart zugang {adr[:8]}: {e}")
        st = {"tier": "public"}
    stufe = st.get("tier") or "public"
    if holder.RANG.get(stufe, 0) >= holder.RANG.get(ZUGANG_STUFE, 1):
        return "holder", {"tier": stufe}
    p = pass_stand(adr)
    if p["active"]:
        return "pass", {"tier": stufe, "until": p["until"]}
    return None, {"tier": stufe, "needs": ZUGANG_STUFE, "pass": p}


def pass_kaufen(adr, renew=True):
    """Eine Woche Pass aus dem Engine-Guthaben. Verlaengert ab dem bisherigen Ende."""
    import ai
    import holder
    adr = holder.norm(adr)
    usd, eth = pass_preis()
    try:
        ai.konto(adr)
        if not ai._reservieren(adr, usd):
            k = ai.konto(adr, anlegen=False) or {}
            return {"ok": False, "why": "not enough balance", "balance_usd": round(k.get("credit") or 0, 2),
                    "price_usd": usd, "price_eth": eth}
        ai._ausgleichen(adr, usd, usd)
    except Exception as e:
        log.warning(f"smart pass {adr[:8]}: {e}")
        return {"ok": False, "why": "payment failed"}
    p = _kv_json("smart_pass_" + adr, {}) or {}
    ab = max(time.time(), float(p.get("until") or 0))
    p.update(until=ab + PASS_TAGE * 86400, renew=bool(renew), paid=round(float(p.get("paid") or 0) + usd, 2))
    storage.kv_set("smart_pass_" + adr, json.dumps(p))
    storage.kv_set("smart_pass_note_" + adr, "")
    return {"ok": True, "until": int(p["until"]), "price_usd": usd, "price_eth": eth, "renew": p["renew"]}


def pass_verlaengern_setzen(adr, renew):
    import holder
    adr = holder.norm(adr)
    p = _kv_json("smart_pass_" + adr, {}) or {}
    p["renew"] = bool(renew)
    storage.kv_set("smart_pass_" + adr, json.dumps(p))
    return pass_stand(adr)


def _darf(adr):
    """Beim Senden: Holder, aktiver Pass, oder Pass verlaengern. Sonst einmal Bescheid geben."""
    art, info = zugang(adr)
    if art:
        return True
    p = info.get("pass") or {}
    if p.get("until") and p.get("renew"):
        if pass_kaufen(adr, True).get("ok"):
            return True
    try:
        if p.get("until") and storage.kv_get("smart_pass_note_" + adr) != "1":
            import motor
            motor.senden(adr, "Your Smart Money pass ran out and your balance did not cover the next week. "
                              f"Top up or hold the {ZUGANG_STUFE} tier of $ODDS to keep getting the calls.")
            storage.kv_set("smart_pass_note_" + adr, "1")
    except Exception:
        pass
    return False


def _senden_call(chain, token, ts, r, wl, t0):
    import features
    if not features.an("smart_calls"):
        return
    import motor
    import scan
    text, knoepfe = _text(r, wl, t0), scan.knoepfe(r)
    an = []
    for adr in motor._nutzer():
        try:
            if not motor.offen(adr) or (motor.zustand(adr).get("prefs") or {}).get("smart") is not True:
                continue
            if not _darf(adr):
                continue
            if motor.senden(adr, text, knoepfe, html=True):
                an.append(adr)
        except Exception as e:
            log.debug(f"smart senden {adr[:8]}: {e}")
    kanal = os.getenv("SMART_CHAT_ID", "").strip()
    if kanal:
        try:
            import agent
            agent._send(kanal, text, knoepfe, html=True)
            an.append("chat:" + kanal)
        except Exception as e:
            log.warning(f"smart kanal: {e}")
    _q("UPDATE iw_calls SET sent=TRUE, recipients=%s WHERE chain=%s AND token=%s AND ts=%s", (json.dumps(an), chain, token, ts))


# ── Nachverfolgen und Bilanz ─────────────────────────────────────────────

def _hoch_seit(chain, pool, seit_ts):
    """Hoechster Preis seit seit_ts aus den Kerzen (Hoch, nicht Momentaufnahme). -> float oder None"""
    netz = GT_NETZ.get(chain)
    if not (netz and pool):
        return None
    alter = time.time() - seit_ts
    if alter <= 16 * 3600:
        art, agg = "minute", 1
    elif alter <= 7 * 86400:
        art, agg = "minute", 15
    else:
        art, agg = "hour", 1
    try:
        r = requests.get(f"{GT}/networks/{netz}/pools/{pool}/ohlcv/{art}", params={"aggregate": agg, "limit": 1000,
                         "currency": "usd"}, headers={"Accept": "application/json"}, timeout=15)
        if r.status_code != 200:
            return None
        liste_ = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    except Exception as e:
        log.debug(f"smart ohlcv {pool[:8]}: {e}")
        return None
    # Seit 2.82.2: ein Hoch zaehlt nur, wenn zwei aufeinanderfolgende Kerzen mit Volumen es erreichen.
    # Einzelne Ausreisser-Dochte (Fehltrade, leerer Pool) machten sonst 30.000x aus einem 5x.
    k_ = sorted([k for k in liste_ if k and len(k) > 5 and int(k[0]) + agg * 60 > seit_ts], key=lambda k: int(k[0]))
    best = None
    for k1, k2 in zip(k_, k_[1:]):
        try:
            if float(k1[5] or 0) <= 0 or float(k2[5] or 0) <= 0:
                continue
            h = min(float(k1[2]), float(k2[2]))
        except (TypeError, ValueError):
            continue
        best = h if best is None else max(best, h)
    return best


# Seit 2.82.76: unmoegliche Multiples (z. B. 46.213x, Peak 358 Mio. aus einer falschen Menge) zaehlen nicht.
SMART_MAX_X = float(os.getenv("SMART_MAX_X", "2000"))
SMART_MAX_PEAK = float(os.getenv("SMART_MAX_PEAK_MC", "5e9"))


def _x_ok(x, peak_mc=None):
    try:
        return x is not None and 0 < float(x) <= SMART_MAX_X and (not peak_mc or float(peak_mc) <= SMART_MAX_PEAK)
    except (TypeError, ValueError):
        return False


def _nachverfolgen(nur_frisch=False):
    """Seit 2.70.0: Hoechststand aus den Kerzen seit dem Call, Multiple = hoechster Preis / Preis beim Call.
    Frische Calls (unter 2 h) alle 2 Minuten, aeltere alle 10 Minuten, bis 7 Tage."""
    import scan
    jetzt = int(time.time())
    grenze = jetzt - (2 * 3600 if nur_frisch else 7 * 86400)
    rows = _q("SELECT chain, token, ts, symbol, mcap0, peak, stufe, sent, recipients, price0, pool, peak_x FROM iw_calls "
              "WHERE ts >= %s", (grenze,)) or []
    alte = []
    for (c, tok, ts, sym, m0, peak, stufe, sent, emp, p0, pool, px) in rows:
        if not (p0 and pool):
            alte.append((c, tok, ts, sym, m0, peak, stufe, sent, emp))
            continue
        h = _hoch_seit(c, pool, ts)
        if not h:
            continue
        x = max(1.0, h / p0)       # seit 2.82.2 jedes Mal neu aus den bestaetigten Kerzen, alte Ausreisser fallen raus
        neu = max([st for st in STUFEN if x >= st] or [0])
        peak_mc = (m0 * x) if m0 else None
        if not _x_ok(x, peak_mc):
            continue
        _q("UPDATE iw_calls SET peak_x=%s, peak=COALESCE(%s, peak), checked=%s, stufe=%s WHERE chain=%s AND token=%s AND ts=%s",
           (round(x, 3), peak_mc, jetzt, max(stufe or 0, neu), c, tok, ts))
        if sent and neu > (stufe or 0):
            _nachricht(c, tok, sym, m0, peak_mc or m0, neu, emp, ts=ts, pool=pool, p0=p0)
        time.sleep(2.1)       # GeckoTerminal frei: rund 30 Abfragen je Minute
    # Alte Calls ohne Preis und Pool: wie bisher ueber DexScreener-Momentaufnahmen
    je = {}
    for r in alte:
        if r[4]:
            je.setdefault(r[0], []).append(r)
    for chain, liste_ in je.items():
        dex = scan.KETTEN.get(chain, (None, None))[1]
        for i in range(0, len(liste_), 30):
            teil = liste_[i:i + 30]
            try:
                d = requests.get(f"https://api.dexscreener.com/tokens/v1/{dex}/" + ",".join(x[1] for x in teil), timeout=15).json()
            except Exception:
                continue
            mc = {}
            for p in d if isinstance(d, list) else []:
                a = (p.get("baseToken") or {}).get("address") or ""
                a = a if chain in ("solana", "tron", "ton") else a.lower()
                mc[a] = max(mc.get(a, 0), float(p.get("marketCap") or p.get("fdv") or 0))
            for (c, tok, ts, sym, m0, peak, stufe, sent, emp) in teil:
                v = mc.get(tok)
                if not v:
                    continue
                if not _x_ok(v / m0, v):
                    continue
                peak = max(peak or 0, v)
                x = peak / m0
                neu = max([st for st in STUFEN if x >= st] or [0])
                _q("UPDATE iw_calls SET peak=%s, last=%s, checked=%s, stufe=%s, peak_x=%s WHERE chain=%s AND token=%s AND ts=%s",
                   (peak, v, int(time.time()), max(stufe or 0, neu), round(x, 3), c, tok, ts))
                if sent and neu > (stufe or 0):
                    _nachricht(c, tok, sym, m0, peak, neu, emp)


def _bild(chain, token, sym, m0, peak, ts, pool, p0):
    """Seit 2.82.51: Chart vom Call bis jetzt, Peak markiert. PNG oder None."""
    if not (ts and pool and p0 and m0):
        return None
    try:
        import rotation
        import rotkarte
        k = rotation._kerzen_fenster(chain, pool, ts)
        if not k:
            return None
        return rotkarte.karte(sym or "?", k, ts, float(m0), float(m0) / float(p0), float(peak),
                              untertitel="market cap since the smart money call", marke="Smart Money", wort="peak")
    except Exception as e:
        log.debug(f"smart bild {token[:8]}: {e}")
        return None


def _nachricht(chain, token, sym, m0, peak, stufe, emp, ts=None, pool=None, p0=None):
    import scan
    from config import cfg
    text = f"🟢 ${sym or scan.kurz(token)} is {stufe}x since the smart money call (MC {scan.usd(m0)} to {scan.usd(peak)})."
    png = _bild(chain, token, sym, m0, peak, ts, pool, p0)
    if png and ts:
        try:
            import rotation
            import rotkarte
            text = (f"🟢 ${sym or scan.kurz(token)} hit {stufe}x since the smart money call\n\n"
                    f"Called at {rotkarte.geld(m0)}. Peak {rotkarte.geld(peak)}, {rotation._worte(time.time() - ts)} after the call.\n\n"
                    f"{token}\n\nPast results are no guarantee of future results.")
        except Exception:
            pass
    try:
        empf = json.loads(emp or "[]")
    except Exception:
        empf = []
    import motor
    for a in empf:
        try:
            chat = a[5:] if a.startswith("chat:") else ((motor._tg_von(a) or {}).get("tg"))
            if png and chat:
                r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendPhoto",
                                  data={"chat_id": chat, "caption": text[:1000]},
                                  files={"photo": ("oddsengine-smart-money.png", png, "image/png")}, timeout=60)
                if r.ok and r.json().get("ok"):
                    continue
            if a.startswith("chat:"):
                import agent
                agent._send(a[5:], text)
            else:
                motor.senden(a, text)
        except Exception:
            pass


def bilanz(tage=30, n=50):
    """Seit 2.70.0: Multiple aus peak_x (Kerzen-Hoch seit dem Call). Early-Calls und spaete Gruppen getrennt."""
    seit = int(time.time()) - int(tage) * 86400
    rows = _q("SELECT chain, token, ts, symbol, n, mcap0, peak, last, level, sent, peak_x, age_min, spread_sec, price0 FROM iw_calls "
              "WHERE ts >= %s ORDER BY ts DESC LIMIT %s", (seit, int(n))) or []
    calls = []
    for r in rows:
        px = r[10] if r[10] else (round(r[6] / r[5], 2) if r[5] and r[6] else None)
        kaputt = bool(px) and not _x_ok(px, r[6])
        if kaputt:
            px = None        # seit 2.82.76: unmoeglicher Wert, wird als "no price" gezeigt
        calls.append({"chain": r[0], "token": r[1], "ts": r[2], "symbol": r[3], "wallets": r[4], "mcap_at_call": r[5],
                      "peak_mcap": None if kaputt else r[6], "mcap_now": r[7], "peak_x": round(px, 2) if px else None,
                      "verdict": r[8], "sent": bool(r[9]), "age_min": r[11], "spread_sec": r[12],
                      "measured": "candles" if r[13] else ("snapshots" if r[5] else "none")})
    alle = _q("SELECT level, peak_x, mcap0, peak FROM iw_calls WHERE ts >= %s", (seit,)) or []

    def quote(gruppe):
        xs = []
        for lvl, px, m0, pk in gruppe:
            x = px if px else ((pk / m0) if m0 and pk else None)
            if x and _x_ok(x, pk):
                xs.append(x)
        xs.sort()
        if not xs:
            return {"counted": 0, "hit_2x": None, "hit_5x": None, "median_peak_x": None}
        return {"counted": len(xs), "hit_2x": round(sum(1 for x in xs if x >= 2) / len(xs) * 100),
                "hit_5x": round(sum(1 for x in xs if x >= 5) / len(xs) * 100), "median_peak_x": round(xs[len(xs) // 2], 2)}
    calls_q = quote([a for a in alle if a[0] not in NICHT_GEZAEHLT])
    spaet_q = quote([a for a in alle if a[0] == "late"])
    out = dict(calls_q)
    out.update({"calls": calls, "late_groups": spaet_q, "unmeasured": sum(1 for a in alle if not a[1] and not a[2]),
                "mode": MODUS, "rule": {"wallets": MIN_W, "window_sec": EARLY_WINDOW if MODUS == "early" else FENSTER,
                                        "max_age_min": EARLY_MAX_AGE_MIN, "max_mcap": EARLY_MAX_MCAP},
                "meaning": "Every call with the price at the call and the highest candle since. Late groups (top wallets "
                           "buying a coin that is already old or big) are recorded for comparison, never sent. Nothing is removed."})
    return out


# ── Walk-forward-Backtest (seit 2.70.0) ─────────────────────────────────
# Frage: Wenn K gute Wallets innerhalb von W Sekunden frueh in denselben Coin kaufen, wie oft laeuft der Coin danach
# auf 2x oder 5x, gemessen ab dem Einstieg des K-ten Kaeufers? "Gut" wird NUR aus den aelteren 70 % der Coins
# gelernt, getestet wird auf den neueren 30 %, die das Modell nie gesehen hat. Sonst misst man sich selbst.
# Coins, die nur aus dem Import stammen (Liste fertiger Runner), sind getrennt ausgewiesen, weil dort nur
# Gewinner drin sind (Survivorship). Zaehlt nur, was in "own" gut aussieht.
BT_KV = "smart_backtest"
_BT = {"laeuft": False}


def _bt_quote(xs):
    xs = sorted(x for x in xs if x and x > 0)
    if not xs:
        return {"n": 0, "hit_2x": None, "hit_5x": None, "median_x": None}
    return {"n": len(xs), "hit_2x": round(sum(1 for x in xs if x >= 2) / len(xs) * 100, 1),
            "hit_5x": round(sum(1 for x in xs if x >= 5) / len(xs) * 100, 1), "median_x": round(xs[len(xs) // 2], 2)}


def backtest(split=0.7):
    import intel
    t_start = time.time()
    tok = intel._q("SELECT chain, token, first_seen, peak_mcap, multiple FROM iw_tokens WHERE first_seen IS NOT NULL "
                   "AND peak_mcap IS NOT NULL ORDER BY first_seen") or []
    if len(tok) < 200:
        return {"error": f"only {len(tok)} coins with a peak, need 200+"}
    grenze = tok[int(len(tok) * split)][2]
    peak = {(c, t): float(pk) for c, t, fs, pk, m in tok}
    mult = {(c, t): float(m or 0) for c, t, fs, pk, m in tok}
    train = {(c, t) for c, t, fs, pk, m in tok if fs < grenze}
    test = {(c, t) for c, t, fs, pk, m in tok if fs >= grenze}

    # 1) Wallet-Qualitaet nur aus dem Training
    gute = {}
    for R in (20, 50):
        rows = intel._q("SELECT e.chain, e.wallet, COUNT(*), SUM(CASE WHEN t.multiple >= 2 THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN t.multiple >= 5 THEN 1 ELSE 0 END) FROM iw_early e JOIN iw_tokens t "
                        "ON t.chain=e.chain AND t.token=e.token WHERE t.first_seen < %s AND e.rank <= %s AND e.flag IS NULL "
                        "GROUP BY e.chain, e.wallet HAVING COUNT(*) >= 3", (grenze, R)) or []
        for q in (0.2, 0.35, 0.5):
            gute[(R, q)] = {(c, w) for c, w, n, w2, w5 in rows if (w2 or 0) / n >= q and (w5 or 0) >= 1}
    alle_guten = set().union(*gute.values()) if gute else set()

    # 2) Fruehe Kaeufe der Test-Coins (Rang 50), nur fuer Wallets, die irgendwo als gut gelten
    rows = intel._q("SELECT e.chain, e.token, e.wallet, e.rank, e.buy_ts, e.entry_mcap, e.src FROM iw_early e JOIN iw_tokens t "
                    "ON t.chain=e.chain AND t.token=e.token WHERE t.first_seen >= %s AND e.rank <= 50 AND e.flag IS NULL",
                    (grenze,)) or []
    je = {}
    start_ts = {}
    quelle = {}
    for c, t, w, rk, ts, em, src in rows:
        if ts:
            ts = int(ts) // 1000 if ts > 1e12 else int(ts)
            start_ts[(c, t)] = min(start_ts.get((c, t), ts), ts)
        if src == "own":
            quelle[(c, t)] = "own"
        else:
            quelle.setdefault((c, t), "import")
        if (c, w) in alle_guten and ts:
            je.setdefault((c, t), []).append((ts, w, int(rk or 99), float(em) if em else None))

    # 3) Grundlinien: jeder Test-Coin, Einstieg zum Kurs des K-ten fruehen Kaeufers
    basis = {}
    for K in (2, 3, 4):
        xa, xo = [], []
        for c, t, w, rk, ts, em, src in rows:
            if rk == K and em and peak.get((c, t)):
                x = peak[(c, t)] / float(em)
                xa.append(x)
                if quelle.get((c, t)) == "own":
                    xo.append(x)
        basis[f"any coin, buy as early buyer #{K}"] = {"all": _bt_quote(xa), "own": _bt_quote(xo)}

    # 4) Raster
    raster = []
    for (R, q), g in gute.items():
        for K in (2, 3, 4):
            for W in (120, 300, 900, 3600):
                for A in (30, None):
                    xa, xo, ohne_mc, treffer = [], [], 0, 0
                    for key, kaeufe in je.items():
                        k2 = sorted((ts, w, rk, em) for ts, w, rk, em in kaeufe if (key[0], w) in g and rk <= R)
                        # je Wallet nur der erste Kauf
                        gesehen, k3 = set(), []
                        for x in k2:
                            if x[1] not in gesehen:
                                gesehen.add(x[1])
                                k3.append(x)
                        if len(k3) < K:
                            continue
                        sig = None
                        for i in range(len(k3) - K + 1):
                            if k3[i + K - 1][0] - k3[i][0] <= W:
                                sig = k3[i + K - 1]
                                break
                        if not sig:
                            continue
                        if A is not None and key in start_ts and sig[0] - start_ts[key] > A * 60:
                            continue
                        treffer += 1
                        if not sig[3]:
                            ohne_mc += 1
                            continue
                        x = peak[key] / sig[3]
                        xa.append(x)
                        if quelle.get(key) == "own":
                            xo.append(x)
                    raster.append({"rank_max": R, "wallet_hit_min": q, "wallets": K, "window_sec": W,
                                   "max_age_min": A, "signals": treffer, "no_mcap": ohne_mc,
                                   "all": _bt_quote(xa), "own": _bt_quote(xo)})

    def guete(r):
        o = r["own"]
        return ((o["hit_5x"] or 0) if o["n"] >= 15 else -1, o["hit_2x"] or 0, o["n"])
    raster.sort(key=guete, reverse=True)
    return {"ts": int(time.time()), "secs": round(time.time() - t_start, 1), "coins": len(tok), "train": len(train),
            "test": len(test), "split_ts": grenze, "good_wallets": {f"R{R} q{q}": len(g) for (R, q), g in gute.items()},
            "baseline": basis, "best": raster[:25], "grid_size": len(raster),
            "meaning": "Wallet quality learned on the older 70% of coins, tested on the newer 30%. x = peak market cap "
                       "divided by the market cap when the K-th good wallet bought. 'own' are coins our scanner found itself, "
                       "'all' includes imported runner lists (winners only, so it looks better than reality). Funder "
                       "independence is not checked here, the live rule does that on top."}


def backtest_starten():
    if _BT["laeuft"]:
        return False

    def lauf():
        _BT["laeuft"] = True
        try:
            r = backtest()
        except Exception as e:
            log.warning(f"smart backtest: {e}")
            r = {"error": str(e)[:300], "ts": int(time.time())}
        finally:
            _BT["laeuft"] = False
        try:
            storage.kv_set(BT_KV, json.dumps(r))
        except Exception as e:
            log.warning(f"smart backtest speichern: {e}")
    threading.Thread(target=lauf, daemon=True).start()
    return True


def backtest_stand():
    try:
        r = json.loads(storage.kv_get(BT_KV) or "null")
    except Exception:
        r = None
    return {"running": _BT["laeuft"], "result": r}


# ── Helius-Webhook: Solana-Kaeufe in Sekunden statt per Abfrage (seit 2.45) ──────────

def _hook_url():
    u = os.getenv("SMART_HOOK_URL", "").strip()
    if u:
        return u
    dom = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{dom}/v1/hooks/helius" if dom else None


def hook_geheimnis():
    import hashlib
    import hmac
    k = (os.getenv("AUTH_SECRET", "") or os.getenv("HELIUS_API_KEY", "")).encode()
    return hmac.new(k, b"oddsengine-helius-hook", hashlib.sha256).hexdigest()[:40]


def hook_aktiv():
    """Webhook steht und ist frisch abgeglichen. Dann wird Solana nur noch langsam nachgeprueft."""
    return time.time() < _HOOK["ok_bis"]


def hook_sync():
    """Webhook bei Helius anlegen oder auf die aktuelle Beobachtungsliste bringen (alle 10 Minuten)."""
    key = os.getenv("HELIUS_API_KEY", "").strip()
    url = _hook_url()
    if not key or not url or os.getenv("SMART_HOOK", "1") == "0":
        _HOOK["ok_bis"] = 0
        return
    adressen = set()
    try:
        import features
        if features.an("smart"):
            adressen |= {x["wallet"] for x in liste() if x["chain"] == "solana"}
    except Exception:
        pass
    try:
        import wache
        if wache._an():
            adressen |= set(wache.sol_wallets())  # Wallet Guard haengt am selben Webhook
    except Exception:
        pass
    adressen = sorted(adressen)
    if not adressen:
        return
    basis = os.getenv("HELIUS_API_BASE", "https://api.helius.xyz").rstrip("/")
    body = {"webhookURL": url, "transactionTypes": ["ANY"], "accountAddresses": adressen,
            "webhookType": "raw", "txnStatus": "success", "authHeader": hook_geheimnis()}
    # Seit 2.62.8: Antwort pruefen, bevor sie als JSON gelesen wird. Vorher stand im Log nur "Expecting value",
    # jetzt der Status und der Anfang der Antwort (z. B. 401 falscher Key, 403 Plan ohne Webhooks, 429 Limit).
    g = requests.get(f"{basis}/v0/webhooks", params={"api-key": key}, timeout=15)
    if g.status_code != 200:
        raise RuntimeError(f"helius webhooks {g.status_code}: {g.text[:160]}")
    try:
        alle = g.json()
    except Exception:
        raise RuntimeError(f"helius webhooks answered no JSON: {g.text[:160]!r}")
    vorhanden = [h for h in (alle if isinstance(alle, list) else []) if h.get("webhookURL") == url]
    if vorhanden:
        h = vorhanden[0]
        if sorted(h.get("accountAddresses") or []) != adressen or h.get("webhookType") != "raw":
            r = requests.put(f"{basis}/v0/webhooks/{h['webhookID']}", params={"api-key": key}, json=body, timeout=20)
            r.raise_for_status()
    else:
        r = requests.post(f"{basis}/v0/webhooks", params={"api-key": key}, json=body, timeout=20)
        r.raise_for_status()
    _HOOK.update(ok_bis=time.time() + 1500, adressen=len(adressen), fehler=None)


def hook_empfangen(daten):
    """Rohe Transaktionen von Helius: Kaeufe der beobachteten Wallets sofort als Sichtung speichern und pruefen."""
    txs = daten if isinstance(daten, list) else [daten]
    try:
        import wache
        if wache._an():
            for tx in txs:
                if isinstance(tx, dict):
                    wache.sol_tx(tx)
    except Exception as e:
        log.warning(f"smart hook wache: {e}")
    beob = {x["wallet"]: x for x in liste() if x["chain"] == "solana"}
    neu = 0
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        import intel
        keys = intel._schluessel(tx)
        sig = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
        ts = int(tx.get("blockTime") or time.time())
        for w in [k for k in keys if k in beob]:
            for tok, einsatz, s_, t_ in _kauf_aus_tx(tx, w, sig, ts):
                if einsatz < MIN_SOL:
                    continue
                _q("INSERT INTO iw_sight (chain, token, wallet, score, native, sig, ts) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                   "ON CONFLICT DO NOTHING", ("solana", tok, w, beob[w]["score"], einsatz, s_, t_))
                neu += 1
    _HOOK["events"] += len(txs)
    _HOOK["letztes"] = int(time.time())
    if neu and _LAEUFT.acquire(blocking=False):
        try:
            _pruefen()
        except Exception as e:
            log.warning(f"smart hook pruefen: {e}")
        finally:
            _LAEUFT.release()
    return neu


def stand():
    w = liste()
    s = _q("SELECT COUNT(*), COUNT(DISTINCT token) FROM iw_sight WHERE ts >= %s", (int(time.time()) - 86400,), eins=True)
    c = _q("SELECT COUNT(*), COUNT(*) FILTER (WHERE sent) FROM iw_calls", (), eins=True)
    try:
        import features
        an, senden_ = features.an("smart"), features.an("smart_calls")
    except Exception:
        an, senden_ = False, False
    return {"on": an, "sending": senden_, "watchlist": len(w), "min_score": MIN_SCORE, "window_min": FENSTER // 60,
            "min_wallets": MIN_W, "mode": MODUS, "early_window_sec": EARLY_WINDOW,
            "early_max_age_min": EARLY_MAX_AGE_MIN, "early_max_mcap": EARLY_MAX_MCAP, "last_round": _T.get("runde"), "rpc_today": _T.get("rpc", 0), "rpc_cap": TAGES_RPC,
            "budget_stopped": bool(_T.get("budget_stop") and time.time() - _T["budget_stop"] < 300),
            "sightings_24h": s[0] if s else 0, "tokens_24h": s[1] if s else 0,
            "calls": c[0] if c else 0, "calls_sent": c[1] if c else 0,
            "helius": bool(os.getenv("HELIUS_API_KEY", "").strip()),
            "webhook": {"active": hook_aktiv(), "events": _HOOK["events"], "last_event": _HOOK["letztes"] or None,
                        "addresses": _HOOK["adressen"], "error": _HOOK["fehler"]},
            "blockscout": bool(os.getenv("BLOCKSCOUT_API_KEY", "").strip()),
            "record": {k: v for k, v in bilanz().items() if k != "calls"},
            "hint": None if w else f"No wallet scores {MIN_SCORE} or more yet. Run the wallet import in the admin, or lower SMART_MIN_SCORE."}


def tick():
    try:
        import features
        if not features.an("smart"):
            return
    except Exception:
        return
    if not _LAEUFT.acquire(blocking=False):
        return
    try:
        jetzt = time.time()
        if jetzt - _T.get("r", 0) >= TAKT:
            _T["r"] = jetzt
            _runde()
            _pruefen()
            try:
                import rotation
                rotation.tick()
                rotation.nachverfolgen()
            except Exception as e:
                log.warning(f"rotation tick: {e}")
        if jetzt - _T.get("h", 0) >= 600:
            _T["h"] = jetzt
            try:
                hook_sync()
            except Exception as e:
                _HOOK["fehler"] = str(e)[:160]
                log.warning(f"smart hook sync: {e}")
        if jetzt - _T.get("ff", 0) >= 120:
            _T["ff"] = jetzt
            _nachverfolgen(nur_frisch=True)
        if jetzt - _T.get("f", 0) >= 600:
            _T["f"] = jetzt
            _nachverfolgen()
            _q("DELETE FROM iw_sight WHERE ts < %s", (int(jetzt) - 3 * 86400,))
    except Exception as e:
        log.warning(f"smart tick: {e}")
    finally:
        _LAEUFT.release()


def schleife():
    """Eigener Takt (aus ai_api beim Start), damit eine Runde wirklich alle SMART_INTERVAL_SEC laeuft."""
    time.sleep(30)
    while True:
        tick()
        time.sleep(max(15, TAKT // 4))
