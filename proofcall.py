"""
proofcall.py: Proof of Call (seit 2.82.53). Wie gut sind die Calls eines X-Accounts wirklich?

"Check @name": Your Engine liest die Posts der letzten Tage (X API, dieselben Schluessel wie xfolge.py) und misst
jeden Call am Chart:
  Coin-Calls    jede Contract-Adresse in einem Post, auf allen Chains, die wir scannen (DexScreener findet Chain und
                Pool, GeckoTerminal liefert die Kerzen). Market Cap beim Post, Peak danach (zwei Kerzen mit Volumen,
                ein einzelner Docht zaehlt nicht), Stand jetzt.
  Markt-Calls   BTC, ETH, SOL: ein kleines Modell ordnet jeden Post als bullish, bearish oder keins ein. Gemessen wird
                der Kurs 24 h und 7 Tage nach dem Post (Coinbase), bullish ist richtig, wenn er gestiegen ist.
Ergebnis: Bilanz als Text mit Link zu jedem Post, dazu ein Bild (Markt-Calls auf dem BTC- bzw. ETH-Chart, Coin-Calls
als Liste). Geht nur an den, der fragt. Nichts wird oeffentlich gepostet.

Kosten: X liest jeden Post fuer etwa X_PROOF_POST_USD (0,005 $), dazu die Einordnung durch das Modell. Abgebucht
wird vom Guthaben wie bei follow_x, vor dem Lesen jeder Seite. Ohne Guthaben bricht es mit einer Meldung ab.
  X_PROOF_MAX_POSTS   hoechstens so viele Posts (Standard 400)
  X_PROOF_DAYS        Zeitraum in Tagen (Standard 90)
"""
import io
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests

import storage
from logger import log

API = "https://api.twitter.com/2"
UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
GT = "https://api.geckoterminal.com/api/v2"
MAJORS = {"BTC": ("BTC-USD", r"\$btc\b|\bbitcoin\b|\bbtc\b"),
          "ETH": ("ETH-USD", r"\$eth\b|\bethereum\b|\beth\b"),
          "SOL": ("SOL-USD", r"\$sol\b|\bsolana\b")}
_LAEUFT = {}


def _f(name, std):
    try:
        return float(os.getenv(name, str(std)) or std)
    except Exception:
        return std


MIN_VOL = _f("X_PROOF_MIN_CANDLE_USD", 50)


# ── X lesen ──────────────────────────────────────────────────────────────

def _posts(adr, uid, tage, max_n):
    """Posts (ohne Retweets und Antworten) der letzten tage, neueste zuerst. Bucht je Seite vorher ab."""
    import xfolge
    import x_scan
    start = datetime.fromtimestamp(time.time() - tage * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, token = [], None
    while len(out) < max_n:
        n = min(100, max(5, max_n - len(out)))
        if not xfolge._abbuchen(adr, n * _f("X_PROOF_POST_USD", 0.005)):
            return out, "balance"
        # Seit 2.82.56: Antworten zaehlen mit (viele KOLs callen im Thread), Links kommen ausgeschrieben (entities),
        # lange Posts ganz (note_tweet). X_PROOF_REPLIES=false schliesst Antworten wieder aus.
        # Seit 2.82.57 wieder ohne Antworten als Standard: bei Vielpostern waren 200 Posts sonst nach wenigen Tagen
        # verbraucht. X_PROOF_REPLIES=true nimmt sie dazu.
        ohne = "retweets" if os.getenv("X_PROOF_REPLIES", "false").lower() in ("1", "true", "yes", "on") else "retweets,replies"
        p = {"max_results": n, "tweet.fields": "created_at,public_metrics,entities,note_tweet", "exclude": ohne,
             "start_time": start}
        if token:
            p["pagination_token"] = token
        d = x_scan._get(f"{API}/users/{uid}/tweets", p)
        for x in d.get("data") or []:
            x["text"] = _volltext(x)
            out.append(x)
        token = (d.get("meta") or {}).get("next_token")
        if not token or not d.get("data"):
            break
    return out, None


def _volltext(x):
    """Ganzer Text (lange Posts), HTML-Zeichen aufgeloest, t.co-Links durch die echte Adresse ersetzt."""
    import html
    t = ((x.get("note_tweet") or {}).get("text")) or x.get("text") or ""
    ents = (((x.get("note_tweet") or {}).get("entities") or {}).get("urls") or []) + ((x.get("entities") or {}).get("urls") or [])
    for u in ents:
        kurz, lang = u.get("url"), u.get("expanded_url") or u.get("unwound_url")
        if kurz and lang:
            t = t.replace(kurz, " " + lang + " ")
    return html.unescape(t)


def _ts(post):
    try:
        return int(datetime.fromisoformat(str(post["created_at"]).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


# ── Coin-Calls ───────────────────────────────────────────────────────────

def _adressen(text):
    import scan
    import x_scan
    t = x_scan._MENTION.sub(" ", str(text or ""))
    # aus Links (pump.fun/coin/..., dexscreener.com/solana/..., gmgn, birdeye, photon ...) jeden Pfadteil und Parameter
    t = re.sub(r"https?://\S+", lambda m: " " + " ".join(re.split(r"[/?&=#]", m.group(0))) + " ", t)
    aus = []
    for roh in re.findall(r"\S+", t):
        w = roh.strip(".,;:!?()[]{}\"'")
        if scan.ist_adresse(w) and w not in aus:
            aus.append(w)
    return aus


def _markt(adresse):
    """DexScreener: Chain, Pool, Symbol, Preis, Market Cap des liquidesten Pools."""
    import scan
    try:
        d = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{adresse}", headers=UA, timeout=15).json()
    except Exception:
        return None
    paare = [p for p in d.get("pairs") or [] if (p.get("baseToken") or {}).get("address", "").lower() == adresse.lower()]
    if not paare:
        # Seit 2.82.56: oft steht im Link die Pool-Adresse (DexScreener), nicht der Token. Dann ueber die Suche den Token holen.
        try:
            s2 = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": adresse}, headers=UA, timeout=15).json()
            pool = next((p for p in s2.get("pairs") or [] if str(p.get("pairAddress") or "").lower() == adresse.lower()), None)
        except Exception:
            pool = None
        if pool and (pool.get("baseToken") or {}).get("address"):
            return _markt(pool["baseToken"]["address"]) if pool["baseToken"]["address"].lower() != adresse.lower() else None
        return None
    p = max(paare, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
    chain = next((k for k, v in scan.KETTEN.items() if v[1] == p.get("chainId")), None)
    if not chain:
        return None
    try:
        preis = float(p.get("priceUsd") or 0)
    except Exception:
        preis = 0
    return {"token": (p.get("baseToken") or {}).get("address") or adresse, "chain": chain, "gt": scan.KETTEN[chain][2], "pool": p.get("pairAddress"),
            "symbol": (p.get("baseToken") or {}).get("symbol") or "?", "price": preis,
            "mcap": float(p.get("marketCap") or p.get("fdv") or 0)}


def _kerzen(gt, pool, ab):
    """Kerzen von ab bis jetzt, Aufloesung nach Alter."""
    alter = time.time() - ab
    for art, agg, sek in (("minute", 15, 900), ("hour", 1, 3600), ("hour", 4, 14400), ("day", 1, 86400)):
        if alter / sek <= 990:
            break
    try:
        r = requests.get(f"{GT}/networks/{gt}/pools/{pool}/ohlcv/{art}",
                         params={"aggregate": agg, "limit": 1000, "currency": "usd"}, headers=UA, timeout=15)
        if r.status_code != 200:
            return None, sek
        k = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        return sorted([x for x in k if x and len(x) > 5], key=lambda x: int(x[0])), sek
    except Exception:
        return None, sek


def coin_call(adresse, ts):
    """-> dict mit mcap_at_post, peak_mcap, peak_x, now_x, hours_to_peak oder None."""
    m = _markt(adresse)
    if not m or not m["pool"] or not m["price"]:
        return None
    k, sek = _kerzen(m["gt"], m["pool"], ts)
    time.sleep(2.1)
    if not k:
        return None
    faktor = m["mcap"] / m["price"] if m["mcap"] else None
    start = next((x for x in k if int(x[0]) <= ts < int(x[0]) + sek), None)
    vorher = False
    if start is None:
        nach = [x for x in k if int(x[0]) >= ts]
        if not nach:
            return None
        if int(nach[0][0]) - ts > 3 * sek:
            vorher = True          # Post vor dem Pool (Launchpad-Kurve): Einstieg = erste Pool-Kerze
        start = nach[0]
    p0 = float(start[4])
    if not p0:
        return None
    nach = [x for x in k if int(x[0]) > int(start[0])]
    best, best_t = None, None
    for a, b in zip(nach, nach[1:]):
        try:
            if float(a[5] or 0) < MIN_VOL or float(b[5] or 0) < MIN_VOL:
                continue
            h = min(float(a[2]), float(b[2]))
        except (TypeError, ValueError):
            continue
        if best is None or h > best:
            best, best_t = h, int(b[0])
    peak_x = max(1.0, best / p0) if best else 1.0
    # Stand jetzt aus derselben Quelle wie der Einstieg (letzte Kerze). Den DexScreener-Preis nur nehmen, wenn er zur
    # Kerze passt (Faktor unter 3), sonst rechnen zwei Pools oder Einheiten gegeneinander.
    letzte = float(k[-1][4]) if k else None
    jetzt = (letzte / p0) if letzte else m["price"] / p0
    if letzte and m["price"] and 1 / 3 <= m["price"] / letzte <= 3 and time.time() - int(k[-1][0]) > 2 * sek:
        jetzt = m["price"] / p0
    if letzte and faktor and not (1 / 3 <= m["price"] / letzte <= 3):
        faktor = m["mcap"] / letzte if m["mcap"] else faktor
    verlauf = [float(start[4])] + [float(x[4]) for x in nach]
    schritt = max(1, len(verlauf) // 60)
    spark = [round(v / p0, 4) for v in verlauf[::schritt]] + [round(jetzt, 4)]
    peak_x = max(peak_x, max(v / p0 for v in verlauf) if verlauf else 1.0)   # Schlusskurse zaehlen immer
    return {"spark": spark, "chain": m["chain"], "symbol": m["symbol"], "token": m.get("token") or adresse,
            "mcap_at_post": round(p0 * faktor) if faktor else None,
            "peak_mcap": round(p0 * faktor * peak_x) if faktor else None,
            "now_mcap": round(m["mcap"]) if m["mcap"] else None,
            "peak_x": round(peak_x, 2), "now_x": round(jetzt, 3),
            "hours_to_peak": round((best_t - ts) / 3600, 1) if best and peak_x > 1.05 else None,
            "before_pool": vorher}


# ── Cashtags (seit 2.82.55) ──────────────────────────────────────────────
# Ein Cashtag ohne Contract zaehlt nur, wenn eindeutig ist, welcher Coin gemeint ist: unter allen Coins mit diesem
# Ticker, die es beim Post schon gab, hat einer mindestens X_PROOF_TICKER_SHARE (80 %) der Liquiditaet und mindestens
# X_PROOF_TICKER_MIN_LIQ (10.000 $). Sonst bleibt er draussen. Solche Calls tragen "by_ticker" und sind in Bild und Text
# als "matched by ticker" markiert. Die Liquiditaet ist die von heute (DexScreener kennt keine alte).

NICHT_TICKER = {"BTC", "ETH", "SOL", "USDT", "USDC", "USD", "DAI", "USDE", "PYUSD", "FDUSD", "EUR", "BNB", "XRP"}
_TAG = re.compile(r"(?<![A-Za-z0-9_])\$([A-Za-z][A-Za-z0-9]{1,14})\b")


def _cashtags(text):
    aus = []
    for t in _TAG.findall(str(text or "")):
        t = t.upper()
        if t not in NICHT_TICKER and t not in aus:
            aus.append(t)
    return aus


KETTEN_WORTE = (("robinhood", r"\brobinhood\b|\brh chain\b|\btorch\b|robinhood:"), ("solana", r"\bsolana\b|\bpump\.?fun\b|\bsol chain\b"),
                ("base", r"\bbase chain\b|\bon base\b|\bbase:"), ("bsc", r"\bbsc\b|\bbnb chain\b|\bfour\.meme\b"),
                ("ethereum", r"\bethereum\b|\beth mainnet\b|\bon eth\b"), ("arbitrum", r"\barbitrum\b"))


def kette_im_text(text):
    t = str(text or "").lower()
    for k, rx in KETTEN_WORTE:
        if re.search(rx, t):
            return k
    return None


def konto_kette(posts):
    """Auf welcher Chain ist der Account zu Hause? Die Chain, die in seinen Posts am oeftesten genannt wird (ab 30 %)."""
    z = {}
    for p in posts:
        k = kette_im_text(p.get("text"))
        if k:
            z[k] = z.get(k, 0) + 1
    if not z:
        return None
    k = max(z, key=z.get)
    return k if z[k] >= 0.3 * sum(z.values()) else None


def ticker_coin(tag, chain=None):
    """$Ticker -> Contract ueber den Ticker-Finder der Engine (CoinGecko, DexScreener nach echtem Handel, Torch-Kurve).
    Mehrdeutig, ein Major oder nichts gefunden: None."""
    import scan
    try:
        r = scan.ticker_finden(tag, chain)
    except Exception as e:
        log.debug(f"proofcall ticker {tag}: {e}")
        r = None
    if chain and not r:
        try:
            r = scan.ticker_finden(tag, None)
        except Exception:
            r = None
    if not r or r.get("ambiguous") or r.get("major") or not r.get("address"):
        return None
    return r["address"]


def _ticker_aufloesen(tag, ts):
    """-> Contract-Adresse oder None, wenn nicht eindeutig."""
    import scan
    try:
        d = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": tag}, headers=UA, timeout=15).json()
    except Exception:
        return None
    ketten = {v[1] for v in scan.KETTEN.values()}
    je = {}
    for p in d.get("pairs") or []:
        b = p.get("baseToken") or {}
        if str(b.get("symbol") or "").upper() != tag or p.get("chainId") not in ketten:
            continue
        if p.get("pairCreatedAt") and int(p["pairCreatedAt"]) / 1000 > ts + 3600:
            continue          # den Coin gab es beim Post noch nicht
        a = b.get("address")
        if a:
            je[a] = je.get(a, 0.0) + float((p.get("liquidity") or {}).get("usd") or 0)
    if not je:
        return None
    best = max(je, key=je.get)
    summe = sum(je.values())
    if je[best] < _f("X_PROOF_TICKER_MIN_LIQ", 10000) or je[best] < summe * _f("X_PROOF_TICKER_SHARE", 0.8):
        return None
    return best


def _coin_stimmung(adr, posts):
    """Bewirbt der Post einen Coin (bullish), warnt er (bearish) oder nichts davon? -> {post_id: stance}"""
    import ai
    import motor
    kandid = [{"id": p["id"], "text": (p.get("text") or "")[:500]} for p in posts
              if _adressen(p.get("text")) or _cashtags(p.get("text"))]
    aus = {}
    for i in range(0, len(kandid), 25):
        teil = kandid[i:i + 25]
        anweisung = ("You get X posts that name a crypto coin by contract address or $ticker. For each post decide what the "
                     "author does with that coin: 'bullish' (promotes it, says buy, ape, sending, gem, entry, holding), "
                     "'bearish' (warns, says rug, scam, avoid, sell, dumped) or 'none' (just news, a question, a joke, a "
                     "list without a view). Answer only with JSON: [{\"id\":\"...\",\"stance\":\"bullish|bearish|none\"}]")
        try:
            d = ai.anfrage(adr, None, {"model": motor.FAST, "max_tokens": 1200, "messages": [
                {"role": "system", "content": anweisung},
                {"role": "user", "content": json.dumps(teil, ensure_ascii=False)}]})
            txt = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            for x in json.loads(txt[txt.find("["):txt.rfind("]") + 1]):
                aus[str(x.get("id"))] = x.get("stance")
        except Exception as e:
            log.debug(f"proofcall stimmung: {e}")
    return aus


# ── Markt-Calls (BTC, ETH, SOL) ─────────────────────────────────────────

def _einordnen(adr, posts):
    """Modell ordnet Posts ein. -> {post_id: (coin, stance)}"""
    import ai
    import motor
    kandid = []
    for p in posts:
        t = p.get("text") or ""
        coins = [c for c, (_, rx) in MAJORS.items() if re.search(rx, t, re.I)]
        if coins:
            kandid.append({"id": p["id"], "coins": coins, "text": t[:600]})
    aus = {}
    for i in range(0, len(kandid), 25):
        teil = kandid[i:i + 25]
        anweisung = ("You get X posts by one account. For each post decide whether it is a directional market call on BTC, "
                     "ETH or SOL: 'bullish' (expects the price to go up, says buy, bottom is in, breakout) or 'bearish' "
                     "(expects down, says sell, top is in, more downside). News, jokes, questions, ads and neutral "
                     "comments are 'none'. Only count a clear view of the person. Answer only with JSON: "
                     '[{"id":"...","coin":"BTC|ETH|SOL","stance":"bullish|bearish|none"}]')
        try:
            d = ai.anfrage(adr, None, {"model": motor.FAST, "max_tokens": 1500, "messages": [
                {"role": "system", "content": anweisung},
                {"role": "user", "content": json.dumps(teil, ensure_ascii=False)}]})
            txt = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            liste = json.loads(txt[txt.find("["):txt.rfind("]") + 1])
        except Exception as e:
            log.debug(f"proofcall einordnen: {e}")
            continue
        for x in liste:
            if x.get("stance") in ("bullish", "bearish") and x.get("coin") in MAJORS:
                aus[str(x.get("id"))] = (x["coin"], x["stance"])
    return aus


def markt_call(coin, stance, ts):
    import steuer
    prod = MAJORS[coin][0]
    p0 = steuer._kerzen(prod, ts)
    if not p0:
        return None
    out = {"coin": coin, "stance": stance, "price": p0}
    for name, dt in (("d1", 86400), ("d7", 7 * 86400)):
        if time.time() >= ts + dt:
            p = steuer._kerzen(prod, ts + dt)
            if p:
                chg = p / p0 - 1
                out[name] = round(chg * 100, 1)
                out[name + "_right"] = (chg > 0) == (stance == "bullish")
    return out


# ── Bild ─────────────────────────────────────────────────────────────────

def _tages_kerzen(prod, ab):
    out = []
    ende = time.time()
    t = ab - 3 * 86400
    while t < ende:
        bis = min(ende, t + 290 * 86400)
        try:
            r = requests.get(f"https://api.exchange.coinbase.com/products/{prod}/candles",
                             params={"granularity": 86400, "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                                     "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bis))}, headers=UA, timeout=15)
            out += [[int(x[0]), float(x[3]), float(x[2]), float(x[1]), float(x[4])] for x in r.json() or []]
        except Exception:
            pass
        t = bis
    return sorted(out)   # [ts, open, high, low, close]


def _kurztext(t):
    t = re.sub(r"https?://\S+", "", str(t or ""))
    t = re.sub(r"\b(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}|T[1-9A-HJ-NP-Za-km-z]{33}|[EU]Q[A-Za-z0-9_-]{46})\b", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:140]


def _umbrechen(d, text, font, breite, zeilen):
    worte, aus, z = text.split(), [], ""
    for w in worte:
        probe = (z + " " + w).strip()
        if d.textlength(probe, font=font) <= breite:
            z = probe
        else:
            aus.append(z)
            z = w
            if len(aus) == zeilen:
                break
    if len(aus) < zeilen and z:
        aus.append(z)
    if len(aus) == zeilen and " ".join(aus) != " ".join(worte):
        aus[-1] = aus[-1][:max(0, len(aus[-1]) - 1)].rstrip() + "…"
    return aus


def _datum(ts):
    return time.strftime("%d %b %Y", time.gmtime(ts))


def _kopf(d, W, handle, untertitel):
    import rotkarte as rk
    d.text((44, 30), f"@{handle}", font=rk._font("unb900.ttf", 30), fill=rk.WS)
    d.text((46, 76), untertitel, font=rk._font("ins600.ttf", 19), fill=rk.GR)
    rk.logo(d, W - 44, 32)
    d.text((W - 44, 70), "Proof of Call", font=rk._font("ins600.ttf", 19), fill=rk.GR, anchor="ra")


def _fuss(d, W, H):
    import rotkarte as rk
    d.text((44, H - 20), "From public posts and public chart data. Past results are no guarantee of future results.",
           font=rk._font("ins600.ttf", 14), fill=rk.DIM, anchor="ls")
    d.text((W - 44, H - 20), "t.me/OddsEnginePublicBot", font=rk._font("jbm700.ttf", 15), fill=rk.GR, anchor="rs")


def bild_markt(handle, markt, tage):
    """Wie Bitcoin Receipts: der Chart, und an jeder Stelle der Post selbst als Karte mit dem Ergebnis danach.
    Bearish-Karten oben, Bullish-Karten unten, Linie zum Kurs am Tag des Posts."""
    from PIL import Image, ImageDraw
    import rotkarte as rk
    haupt = max({m["coin"] for m in markt}, key=lambda c: sum(1 for m in markt if m["coin"] == c))
    mk = sorted([m for m in markt if m["coin"] == haupt], key=lambda m: m["ts"])
    k = _tages_kerzen(MAJORS[haupt][0], mk[0]["ts"] - 6 * 86400)
    if len(k) < 3:
        return None
    W, H = 1400, 900
    im = Image.new("RGB", (W, H), rk.BG)
    d = ImageDraw.Draw(im)
    _kopf(d, W, handle, f"What they said about {haupt}, and what {haupt} did next")
    KH, CW = 150, 236                  # Kartenhoehe, Kartenbreite
    oben_y, unten_y = 112, H - 40 - KH
    y0, y1 = oben_y + KH + 30, unten_y - 30
    x0, x1 = 100, W - 40
    lo, hi = min(x[3] for x in k) * 0.985, max(x[2] for x in k) * 1.015
    ty = lambda v: y1 - (v - lo) / (hi - lo) * (y1 - y0)
    t0, t1 = k[0][0], k[-1][0] + 86400
    tx = lambda t: x0 + (t - t0) / (t1 - t0) * (x1 - x0)
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        d.line((x0, ty(v), x1, ty(v)), fill=rk.GRID)
        d.text((x0 - 8, ty(v)), f"${v / 1000:,.0f}K" if v >= 10000 else f"${v:,.0f}", font=rk._font("jbm700.ttf", 14), fill=rk.DIM, anchor="rm")
    bw = max(2, (x1 - x0) / len(k) * 0.62)
    for t, o, h, l, c in k:
        mx = tx(t + 43200)
        f = rk.G if c >= o else rk.RT
        d.line((mx, ty(h), mx, ty(l)), fill=f)
        d.rectangle((mx - bw / 2, ty(max(o, c)), mx + bw / 2, max(ty(min(o, c)), ty(max(o, c)) + 1)), fill=f)
    # Karten verteilen: je Reihe nach Zeit, ohne Ueberlappung, hoechstens 5 je Reihe
    reihen = {"bearish": [m for m in mk if m["stance"] == "bearish"][-5:], "bullish": [m for m in mk if m["stance"] == "bullish"][-5:]}
    fk, fm, fs = rk._font("ins600.ttf", 15), rk._font("jbm700.ttf", 13), rk._font("ins600.ttf", 14)
    for stance, liste in reihen.items():
        cy = oben_y if stance == "bearish" else unten_y
        pos, links = [], 40
        for m in liste:
            want = max(links, min(W - 40 - CW, tx(m["ts"]) - CW / 2))
            pos.append(want)
            links = want + CW + 12
        # passt es rechts nicht, von rechts zurueckschieben
        rechts = W - 40
        for i in range(len(pos) - 1, -1, -1):
            if pos[i] + CW > rechts:
                pos[i] = rechts - CW
            rechts = pos[i] - 12
        for m, px in zip(liste, pos):
            mx, my = tx(m["ts"]), ty(m["price"])
            erg = m.get("d7")
            richtig = m.get("d7_right")
            rand = rk.G if richtig else (rk.RT if richtig is False else rk.GR)
            karte_unten = cy + KH
            ank_y = karte_unten if stance == "bearish" else cy
            d.line((px + CW / 2, ank_y, mx, my), fill=rand, width=2)
            d.ellipse((mx - 5, my - 5, mx + 5, my + 5), fill=rand, outline=rk.BG, width=2)
            d.rounded_rectangle((px, cy, px + CW, cy + KH), radius=12, fill=(14, 22, 18), outline=rand, width=2)
            d.text((px + 12, cy + 12), f"{'BULLISH' if stance == 'bullish' else 'BEARISH'} ${haupt}", font=fm, fill=rand)
            for i, zl in enumerate(_umbrechen(d, m.get("text") or "", fs, CW - 24, 4)):
                d.text((px + 12, cy + 34 + i * 18), zl, font=fs, fill=rk.WS)
            d.text((px + CW - 12, cy + 12), _datum(m["ts"]), font=fm, fill=rk.DIM, anchor="ra")
            if erg is not None:
                sc = erg if stance == "bullish" else -erg
                lab = f"{haupt} {erg:+.1f}% in 7d"
                lab = f"{erg:+.1f}% in 7 days · {'RIGHT' if sc >= 0 else 'WRONG'}"
                d.rounded_rectangle((px + 10, cy + KH - 30, px + CW - 10, cy + KH - 8), radius=10,
                                    fill=(18, 60, 38) if sc >= 0 else (70, 20, 28))
                d.text((px + CW / 2, cy + KH - 19), lab, font=fm, fill=rk.G if sc >= 0 else rk.RT, anchor="mm")
            else:
                d.text((px + CW / 2, cy + KH - 19), "open, under 7 days old", font=fm, fill=rk.GR, anchor="mm")
    _fuss(d, W, H)
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()


def bild_coins(handle, coins, tage):
    """Jeder gepostete Contract als Karte: der Post, Market Cap beim Post, was danach passiert ist (Mini-Chart)."""
    from PIL import Image, ImageDraw
    import rotkarte as rk
    liste = sorted(coins, key=lambda c: -c["ts"])[:9]
    spalten = 3 if len(liste) > 2 else len(liste)
    W = 1400 if spalten == 3 else max(960, 88 + spalten * 440 + (spalten - 1) * 16)
    zeilen = (len(liste) + spalten - 1) // spalten
    CW, KH = (W - 88 - (spalten - 1) * 16) / spalten, 300
    H = 120 + zeilen * (KH + 16) + 40
    im = Image.new("RGB", (W, H), rk.BG)
    d = ImageDraw.Draw(im)
    _kopf(d, W, handle, "Coins they promoted, and what the coin did after the post")
    fb, fm, fs = rk._font("ins600.ttf", 22), rk._font("jbm700.ttf", 14), rk._font("ins600.ttf", 15)
    for i, c in enumerate(liste):
        px = 44 + (i % spalten) * (CW + 16)
        py = 112 + (i // spalten) * (KH + 16)
        gut = c["peak_x"] >= 2
        rand = rk.G if gut else (rk.RT if c["now_x"] < 0.5 else rk.GR)
        d.rounded_rectangle((px, py, px + CW, py + KH), radius=14, fill=(14, 22, 18), outline=rand, width=2)
        d.text((px + 16, py + 14), f"${c['symbol']}", font=fb, fill=rk.WS)
        d.text((px + CW - 16, py + 20), f"{c['chain']} · {_datum(c['ts'])}" + (" · by ticker" if c.get("by_ticker") else ""),
               font=fm, fill=rk.DIM, anchor="ra")
        for j, zl in enumerate(_umbrechen(d, "“" + (c.get("text") or "") + "”", fs, CW - 32, 2)):
            d.text((px + 16, py + 50 + j * 19), zl, font=fs, fill=rk.GR)
        # Mini-Chart: Post-Punkt links, Verlauf bis heute
        sp = c.get("spark") or [1, c["now_x"]]
        gx0, gx1, gy0, gy1 = px + 16, px + CW - 16, py + 100, py + 210
        lo, hi = min(sp + [1]) * 0.9, max(sp + [1]) * 1.05
        gy = lambda v: gy1 - (v - lo) / (hi - lo) * (gy1 - gy0) if hi > lo else gy1
        gxx = lambda j: gx0 + j / max(1, len(sp) - 1) * (gx1 - gx0)
        for xx in range(int(gx0), int(gx1), 10):
            d.line((xx, gy(1), xx + 5, gy(1)), fill=rk.DIM)
        d.line([(gxx(j), gy(v)) for j, v in enumerate(sp)], fill=rk.G if sp[-1] >= 1 else rk.RT, width=3)
        d.ellipse((gx0 - 6, gy(1) - 6, gx0 + 6, gy(1) + 6), fill=rk.G2, outline=rk.BG, width=2)
        d.text((gx0 + 10, gy(1) + (14 if gy(1) < gy1 - 24 else -22)), "post", font=fm, fill=rk.G2)
        # Zahlen
        zy = py + 236
        for j, (lab, wert, farbe) in enumerate((("at the post", rk.geld(c["mcap_at_post"]) if c.get("mcap_at_post") else "?", rk.WS),
                                                ("peak after", f"{c['peak_x']:.1f}x", rk.G if gut else rk.GR),
                                                ("today", f"{c['now_x']:.2f}x", rk.G if c["now_x"] >= 1 else rk.RT))):
            xx = px + 16 + j * (CW - 32) / 3
            d.text((xx, zy), lab, font=fm, fill=rk.DIM)
            d.text((xx, zy + 20), wert, font=rk._font("unb900.ttf", 20), fill=farbe)
    _fuss(d, W, H)
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()


# ── Bilanz ───────────────────────────────────────────────────────────────

def _median(v):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    return v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2


def bericht(handle, markt, coins, tage, gelesen, abdeckung=None):
    import rotkarte as rk
    z = [f"Proof of Call: @{handle}, last {tage} days ({gelesen} posts read" + (f", they reach back to {_datum(abdeckung)}" if abdeckung else "") + ")", ""]
    link = lambda pid: f"x.com/{handle}/status/{pid}"
    if coins:
        n = len(coins)
        nt = sum(1 for c in coins if c.get("by_ticker"))
        z.append(f"Coin calls: {n} coin{'s' if n != 1 else ''} promoted"
                 + (f" ({n - nt} by contract, {nt} by $ticker, matched to the clear main coin with that ticker)." if nt else " by contract."))
        for x in (2, 5, 10):
            z.append(f"  {sum(1 for c in coins if c['peak_x'] >= x)} of {n} did {x}x after the post")
        z.append(f"  Median peak {(_median([c['peak_x'] for c in coins]) or 1):.1f}x, median today {(_median([c['now_x'] for c in coins]) or 1):.2f}x")
        b = max(coins, key=lambda c: c["peak_x"])
        s = min(coins, key=lambda c: c["now_x"])
        z.append(f"  Best: ${b['symbol']} {b['peak_x']:.1f}x from {rk.geld(b['mcap_at_post'])} {link(b['post'])}")
        z.append(f"  Worst today: ${s['symbol']} {s['now_x']:.2f}x {link(s['post'])}")
        z.append("")
    if markt:
        fertig = [m for m in markt if m.get("d7_right") is not None]
        z.append(f"Market calls on BTC, ETH, SOL: {len(markt)} ({sum(1 for m in markt if m['stance'] == 'bullish')} bullish, "
                 f"{sum(1 for m in markt if m['stance'] == 'bearish')} bearish).")
        if fertig:
            r = sum(1 for m in fertig if m["d7_right"])
            z.append(f"  Right after 7 days: {r} of {len(fertig)} ({round(r / len(fertig) * 100)}%)")
        f1 = [m for m in markt if m.get("d1_right") is not None]
        if f1:
            z.append(f"  Right after 24 hours: {sum(1 for m in f1 if m['d1_right'])} of {len(f1)}")
        for m in sorted(markt, key=lambda m: -m["ts"])[:5]:
            erg = (f"{m['d7']:+.1f}% in 7 days, {'right' if m['d7_right'] else 'wrong'}" if m.get("d7") is not None else "still open")
            z.append(f"  {time.strftime('%d %b', time.gmtime(m['ts']))} {m['stance']} {m['coin']}: {erg} {link(m['post'])}")
        z.append("")
    if not coins and not markt:
        z.append("No measurable calls found: no contract addresses and no clear bullish or bearish posts on BTC, ETH or SOL.")
        z.append("")
    z.append("Measured from public posts and public chart data. A coin call counts from the minute of the post, "
             "the peak needs two candles in a row with volume. Past results are no guarantee of future results.")
    return "\n".join(z)


def _lauf(adr, handle, tage, max_n):
    import motor
    import xfolge
    try:
        pr = xfolge.profil(handle)
        if pr.get("error"):
            motor.senden(adr, f"Proof of Call: could not find @{handle} on X.")
            return
        if pr.get("protected"):
            motor.senden(adr, f"Proof of Call: @{pr['handle']} is a private account, its posts can't be read.")
            return
        handle = pr["handle"]
        posts, grund = _posts(adr, pr["id"], tage, max_n)
        if grund == "balance" and not posts:
            motor.senden(adr, "Proof of Call needs balance to read the posts on X. Top up with $ODDS and try again.")
            return
        coins, gesehen, tags_gesehen = [], set(), set()
        stimmung = _coin_stimmung(adr, posts)
        heimat = konto_kette(posts)
        # aelteste zuerst: der erste Post zu einem Coin ist der Call
        for p in sorted(posts, key=_ts):
            ts = _ts(p)
            st = stimmung.get(str(p["id"]))
            ziele = [(a, False) for a in _adressen(p.get("text"))]
            if st == "bullish":
                for tag in _cashtags(p.get("text")):
                    if tag in tags_gesehen:
                        continue
                    tags_gesehen.add(tag)
                    a = ticker_coin(tag, kette_im_text(p.get("text")) or heimat)
                    time.sleep(0.3)
                    if a:
                        ziele.append((a, True))
            for a, per_ticker in ziele:
                if a.lower() in gesehen or len(coins) >= 30:
                    continue
                if st == "bearish":
                    continue          # eine Warnung ist kein Call
                gesehen.add(a.lower())
                try:
                    c = coin_call(a, ts)
                except Exception as e:
                    log.debug(f"proofcall coin {a[:8]}: {e}")
                    c = None
                if c and c["token"].lower() != a.lower():
                    if c["token"].lower() in gesehen:
                        continue          # derselbe Coin, einmal als Pool-Link, einmal als Contract
                    gesehen.add(c["token"].lower())
                if c and per_ticker and c.get("before_pool"):
                    c = None              # den Coin gab es beim Post wohl noch nicht, der Ticker meinte etwas anderes
                if c:
                    c["post"], c["ts"], c["text"] = p["id"], ts, _kurztext(p.get("text"))
                    c["likes"] = (p.get("public_metrics") or {}).get("like_count")
                    c["by_ticker"] = per_ticker
                    coins.append(c)
        markt = []
        for pid, (coin, stance) in _einordnen(adr, posts).items():
            p = next((x for x in posts if str(x["id"]) == pid), None)
            if not p:
                continue
            m = markt_call(coin, stance, _ts(p))
            if m:
                m["post"], m["ts"], m["text"] = pid, _ts(p), _kurztext(p.get("text"))
                m["likes"] = (p.get("public_metrics") or {}).get("like_count")
                markt.append(m)
        text = bericht(handle, markt, coins, tage, len(posts), min((_ts(p) for p in posts), default=None))
        if grund == "balance":
            text += "\n\nStopped early: the balance ran out while reading posts."
        motor.senden(adr, text)
        if coins or markt:
            u = motor._tg_von(adr)
            for png in (bild_markt(handle, markt, tage) if markt else None,
                        bild_coins(handle, coins, tage) if coins else None):
                if png and u:
                    import base64
                    motor._foto(u["tg"], adr, None, "data:image/png;base64," + base64.b64encode(png).decode())
        try:
            storage.kv_set(f"proofcall_{handle.lower()}", json.dumps({"ts": int(time.time()), "days": tage,
                                                                       "posts": len(posts), "coins": coins, "market": markt}))
        except Exception:
            pass
    except Exception as e:
        log.warning(f"proofcall {handle}: {e}")
        motor.senden(adr, "Proof of Call stopped with an error. Try again in a few minutes.")
    finally:
        _LAEUFT.pop(adr, None)


def starten(adr, wer, tage=None):
    import xfolge
    if not xfolge.bereit():
        return {"error": "X is not connected on this server."}
    handle = xfolge.handle_aus(wer)
    if not handle:
        return {"error": "Give an X account like @name or an x.com link."}
    if _LAEUFT.get(adr):
        return {"started": False, "how_to_answer": "Say that a Proof of Call check is already running and the result comes in a moment."}
    tage = int(max(7, min(365, float(tage or _f("X_PROOF_DAYS", 90)))))
    max_n = int(_f("X_PROOF_MAX_POSTS", 400))
    _LAEUFT[adr] = time.time()
    threading.Thread(target=_lauf, args=(adr, handle, tage, max_n), daemon=True, name="proofcall").start()
    kosten = round(max_n * _f("X_PROOF_POST_USD", 0.005) + 0.03, 2)
    return {"started": True, "account": handle, "days": tage,
            "how_to_answer": f"Say in two short sentences that Proof of Call for @{handle} runs now: every coin they "
                             f"promoted (contracts and clear $tickers) and every bullish or bearish BTC, ETH or SOL post of the last {tage} days gets measured "
                             f"on the chart, the result with an image comes as its own message in 1 to 3 minutes. Costs up to "
                             f"about ${kosten} from the balance (reading posts on X). Do not invent any results."}
