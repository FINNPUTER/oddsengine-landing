"""
video.py: Promo-Videos ueber loudscene (seit 2.60, Zahlung aus der eigenen Wallet seit 2.61).

loudscene liest eine Website, eine CA oder einen Torch-/DexScreener-Link (Name, Logo, Farben, Story) und macht daraus
ein Video. Your Engine schickt das fertige MP4 direkt in Telegram.

Bezahlt wird aus der Wallet des Nutzers auf Robinhood Chain, in ETH, mit zwei Transaktionen auf der Bestaetigungsseite:
    1. ETH -> $LOUD, direkt an die loudscene-Wallet (LOUDSCENE_PAY_WALLET), (100 - LOUD_BURN_PCT) % des Preises
    2. ETH -> $ODDS, direkt an 0x...dEaD (verbrannt), LOUD_BURN_PCT % des Preises
Getauscht wird ueber Relay, die Tokens gehen ohne Umweg an den Empfaenger. Your Engine haelt nie Geld und braucht
keinen privaten Schluessel. Nach dem Senden prueft der Server die Belege on-chain und startet erst dann das Video.

API loudscene (https://www.loudscene.com/api/v1, Bearer LOUDSCENE_API_KEY):
    1. POST /videos {url, brief, format, durationSec}       -> Video, status "generating"
    2. GET  /videos/{id} bis status "ready" (oder "failed")
    3. POST /videos/{id}/renders {format, resolution, fps}  -> Render
    4. GET  /renders/{id} bis "succeeded", dann url          -> signierter MP4-Link (4 h gueltig)

Variablen (web und worker):
    LOUDSCENE_API_KEY      Key aus loudscene Settings -> API
    LOUDSCENE_PAY_WALLET   Wallet von loudscene fuer die LOUD-Zahlung
    VIDEO_PRICE_USD        Preis fuer den Nutzer, Standard 8 (davon LOUD_BURN_PCT als ODDS-Burn)
    LOUD_BURN_PCT          Anteil fuer den ODDS-Burn, Standard 15
    VIDEO_FREE_FOR         Komma-Liste von Wallets, die ohne Zahlung testen (die Gratis-Videos der Testwoche)
    LOUDSCENE_TX_FIELD     Feldname fuer den Zahlungs-Tx bei POST /videos, sobald loudscene ihn prueft (leer = nicht senden)
    LOUDSCENE_SECONDS      Laenge, Standard 20
"""
import json
import os
import re
import secrets
import threading
import time
import uuid

import requests

import storage
from logger import log

API = "https://www.loudscene.com/api/v1"
FORMATE = ("16:9", "9:16", "1:1", "4:5")
LOUD = "0xcd291fa4d3e2eb43e4639625092d31ae10e61111"
ODDS = "0x9c87b4cfa21266d1638f437c96dbe1012cb81111"
DEAD = "0x000000000000000000000000000000000000dead"
NATIVE = "0x0000000000000000000000000000000000000000"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PUFFER = 1.05  # Route-Gebuehr und Kursbewegung: etwas mehr ETH, damit loudscene sicher den vollen Anteil bekommt
_LAEUFT = {}


def _key():
    return os.getenv("LOUDSCENE_API_KEY", "").strip()


def _zahl_wallet():
    w = os.getenv("LOUDSCENE_PAY_WALLET", "").strip()
    return w if re.fullmatch(r"0x[0-9a-fA-F]{40}", w) else ""


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


def an():
    return bool(_key())


def preis():
    return _f("VIDEO_PRICE_USD", 8)


def _gratis(adr):
    liste = {x.strip().lower() for x in os.getenv("VIDEO_FREE_FOR", "").split(",") if x.strip()}
    return str(adr or "").lower() in liste


# ── loudscene API ────────────────────────────────────────────────────────

def _kopf(idem=None):
    h = {"Authorization": "Bearer " + _key(), "Content-Type": "application/json", "Accept": "application/json"}
    if idem:
        h["Idempotency-Key"] = idem
    return h


def _anfrage(methode, pfad, body=None, idem=None, timeout=30):
    for versuch in range(4):
        r = requests.request(methode, API + pfad, json=body, headers=_kopf(idem), timeout=timeout)
        if r.status_code == 429:
            try:
                warte = float(r.headers.get("retry-after") or 2)
            except Exception:
                warte = 2.0
            time.sleep(min(30.0, warte))
            continue
        if r.status_code >= 400:
            try:
                d = r.json()
                fehler = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else d.get("error") or d.get("message")
            except Exception:
                fehler = r.text[:200]
            raise RuntimeError(f"loudscene {r.status_code}: {str(fehler)[:200]}")
        return r.json()
    raise RuntimeError("loudscene is busy right now (rate limit)")


def _warten(pfad, fertig, fehl, max_s, schritt=6, melden=None):
    ende = time.time() + max_s
    while time.time() < ende:
        d = _anfrage("GET", pfad)
        st = str(d.get("status") or "").lower()
        if melden:
            try:
                melden(d)
            except Exception:
                pass
        if st in fertig:
            return d
        if st in fehl:
            raise RuntimeError(f"loudscene {st}: {str(d.get('error') or (d.get('job') or {}).get('error') or '')[:160]}")
        time.sleep(schritt)
    raise RuntimeError("loudscene took too long")


def erzeugen(url, brief=None, format="16:9", sekunden=None, zahlung_tx=None, stufe=None, merken=None, vid=None, rid=None):
    """Synchron: Quelle -> signierter MP4-Link. stufe(nr, antwort): Fortschritt fuer die Statusnachricht (seit 2.63).
    Seit 2.69.2: merken(vid=..., rid=...) speichert die loudscene-Ids, vid/rid setzen nach einem Neustart fort."""
    stufe = stufe or (lambda nr, d=None: None)
    merken = merken or (lambda **k: None)
    format = format if format in FORMATE else "16:9"
    if not vid:
        body = {"url": url, "format": format, "durationSec": int(sekunden or os.getenv("LOUDSCENE_SECONDS", "20") or 20)}
        if brief:
            body["brief"] = str(brief)[:800]
        feld = os.getenv("LOUDSCENE_TX_FIELD", "").strip()
        if feld and zahlung_tx:
            body[feld] = zahlung_tx
        v = _anfrage("POST", "/videos", body, idem=str(uuid.uuid4()), timeout=60)
        vid = v.get("id")
        if not vid:
            raise RuntimeError("loudscene returned no video id")
        merken(vid=vid)
    if not rid:
        stufe(1)
        _warten(f"/videos/{vid}", ("ready",), ("failed",), 15 * 60, melden=lambda d: stufe(1, d))
        stufe(2)
        rd = _anfrage("POST", f"/videos/{vid}/renders", {"format": format, "resolution": 1080, "fps": 30}, idem=str(uuid.uuid4()))
        rid = rd.get("id")
        merken(rid=rid)
    else:
        stufe(2)
        rd = {}
    if rd.get("status") != "succeeded" or not rd.get("url"):
        rd = _warten(f"/renders/{rid}", ("succeeded",), ("failed", "canceled"), 15 * 60, melden=lambda d: stufe(2, d))
    stufe(3)
    return {"video_id": vid, "render_id": rid, "url": rd.get("url"), "poster": rd.get("poster_url")}


def _senden(chat_id, mp4_url, text):
    from config import cfg
    try:
        r = requests.get(mp4_url, timeout=120)
        daten = r.content if r.ok else None
    except Exception:
        daten = None
    if daten and len(daten) < 48 * 1024 * 1024:
        r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendVideo",
                          data={"chat_id": chat_id, "caption": text[:1000], "supports_streaming": "true"},
                          files={"video": ("promo.mp4", daten, "video/mp4")}, timeout=180)
        if r.ok:
            return True
    requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage",
                  data={"chat_id": chat_id, "text": f"{text}\n\nDownload (valid for 4 hours): {mp4_url}"}, timeout=30)
    return True


# ── Quelle erkennen: Website, CA oder Link ───────────────────────────────

_AUTO = {}


def _auto_brief(r):
    """Seit 2.63.1: Brief aus den Scan-Daten, damit das Video stimmt, auch wenn die Website Bots blockt (z. B. Cloudflare)."""
    sym = str(r.get("symbol") or "").strip()
    name = str(r.get("name") or "").strip()
    wo = " on ".join(x for x in [r.get("chain_name") and f"{r['chain_name']}", None] if x)
    teile = [f"Promo video for the crypto coin ${sym}" + (f" ({name})" if name and name.lower() != sym.lower() else "")
             + (f" on {wo}" if wo else "") + (f", trading on {r['venue']}" if r.get("venue") else "") + "."]
    teile.append(f"Show the ticker ${sym} big and clear" + (", and use the coin logo" if r.get("image") else "") + ".")
    if r.get("image"):
        teile.append(f"Logo: {r['image']}")
    l = r.get("links") or {}
    m = (r.get("market") or {}).get("links") or {}
    wohin = [f"{k}: {u}" for k, u in [("Buy", l.get("trade_torch") or l.get("chart")), ("Website", l.get("website") or m.get("website")),
                                       ("X", m.get("twitter")), ("Telegram", m.get("telegram"))] if u]
    if wohin:
        teile.append("Links: " + " | ".join(wohin))
    teile.append("Energetic, short sentences, crypto native tone. End with where to buy. "
                 "No price predictions, no promises of profit, no financial advice.")
    return " ".join(teile)[:800]


def quelle(eingabe):
    """-> (url fuer loudscene, Anzeige) oder (None, Fehlertext). loudscene nimmt Websites, CAs, Torch- und DexScreener-Links."""
    e = str(eingabe or "").strip()
    if not e:
        return None, "Send me the website, the contract address or the Torch link of the project."
    if re.match(r"^https?://", e, re.I):
        return e, re.sub(r"^https?://(www\.)?", "", e).rstrip("/")[:60]
    try:
        import scan
        hinweis, adr = scan.lesen(e)
        if adr and scan.ist_adresse(adr) or e.startswith("$"):
            r = scan.analyse(e if e.startswith("$") else ((hinweis + ":" + adr) if hinweis else adr), deep=False)
            if r.get("ok"):
                l = r.get("links") or {}
                # Seit 2.69.7: der Token-Link zuerst, nicht die Website. loudscene liest daraus die Website und baut das
                # Brand-Kit (Logo, Farben) notfalls aus dem Token selbst, wenn die Seite Bots blockt. Mit der Website als
                # Quelle fiel genau dieser Rueckweg weg, und das Video kam in Standardfarben.
                ziel = l.get("trade_torch") or l.get("chart") or l.get("website")
                if ziel:
                    try:
                        _AUTO[ziel] = _auto_brief(r)
                    except Exception as ex:
                        log.debug(f"video auto brief: {ex}")
                    return ziel, "$" + str(r.get("symbol") or "")
            return None, "I could not find that coin. Send me the project's website instead."
    except Exception as ex:
        log.debug(f"video quelle: {ex}")
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$", e, re.I):
        return "https://" + e, e.rstrip("/")[:60]
    return None, "Send me the website, the contract address or the Torch link of the project."


# ── Auftrag, Bestaetigungsseite, Pruefung ────────────────────────────────

def _betraege():
    import handel
    p = preis()
    pct = max(0.0, min(50.0, _f("LOUD_BURN_PCT", 15)))
    loud_usd, burn_usd = round(p * (1 - pct / 100), 2), round(p * pct / 100, 2)
    eth = handel._eth_usd()
    if not eth:
        return None
    return {"price": p, "pct": pct, "loud_usd": loud_usd, "burn_usd": burn_usd, "eth_usd": eth,
            "wei_loud": int(loud_usd / eth * PUFFER * 1e18), "wei_burn": int(burn_usd / eth * PUFFER * 1e18) if burn_usd else 0}


def _kasse_bereit(b):
    """Seit 2.63: Zahlung aus dem Guthaben. Die Gas-Wallet (X402_GAS_KEY) tauscht dann ETH auf Robinhood Chain in $LOUD
    fuer loudscene und $ODDS zum Verbrennen. Dafuer braucht sie genug ETH als Vorrat. -> (bereit, grund)"""
    if os.getenv("VIDEO_BALANCE_PAY", "1") == "0":
        return False, "off"
    try:
        import usdc
        import handel
        k = usdc.kasse()
        if not k:
            return False, "no gas wallet"
        saldo = handel._nativ_saldo("robinhood", k)
    except Exception:
        return False, "no gas wallet"
    reserve = int(_f("VIDEO_FLOAT_MIN_ETH", 0.0005) * 1e18)
    if saldo is None or saldo < (b["wei_loud"] + b["wei_burn"]) * 1.05 + reserve:
        return False, "float low"
    tag = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        n = int(storage.kv_get("video_bal_" + tag) or 0)
    except Exception:
        n = 0
    if n >= int(_f("VIDEO_BALANCE_DAY_MAX", 30)):
        return False, "day limit"
    return True, None


def _guthaben_ok(adr, b):
    try:
        import ai
        k = ai.konto(adr, anlegen=False) or {}
        return float(k.get("credit") or 0) >= b["price"]
    except Exception:
        return False


def _aus_guthaben(adr, chat_id, url, anzeige, brief, fmt, b):
    """Guthaben belasten, mit der Gas-Wallet LOUD fuer loudscene und ODDS zum Verbrennen kaufen, dann das Video starten."""
    import ai
    import handel
    import usdc
    if not ai._reservieren(adr, b["price"]):
        return {"started": False, "error": "Your balance is too low for a video now."}
    tag = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        storage.kv_set("video_bal_" + tag, str(int(storage.kv_get("video_bal_" + tag) or 0) + 1))
    except Exception:
        pass
    _LAEUFT[adr] = time.time()

    def kaufen(token, wei, an_):
        q = handel._quote(usdc.kasse(), NATIVE, token, wei, chain="robinhood", empfaenger=an_)
        if not q.get("ok"):
            raise RuntimeError(q.get("error") or "no route")
        h = None
        for t in q["txs"]:
            h, _ = usdc._senden("robinhood", t["to"], t["data"], int(t["value"], 16))
        return h

    def lauf():
        import motor
        try:
            tx_loud = kaufen(LOUD, b["wei_loud"], _zahl_wallet())
        except Exception as e:
            log.warning(f"video guthaben {adr[:8]}: LOUD {e}")
            ai._ausgleichen(adr, b["price"], 0)
            _LAEUFT.pop(adr, None)
            motor.senden(adr, "The payment for the video did not go through, nothing was taken from your balance. Try again in a few minutes.")
            return
        ai._ausgleichen(adr, b["price"], b["price"])
        try:
            if b["wei_burn"]:
                kaufen(ODDS, b["wei_burn"], DEAD)
        except Exception as e:
            log.warning(f"video guthaben {adr[:8]}: burn {e}")
        try:
            import aktiv
            aktiv.merken(adr, "video", f"{anzeige} from balance", b["price"])
        except Exception:
            pass
        _LAEUFT.pop(adr, None)
        _rendern(adr, chat_id, url, anzeige, brief, fmt, tx_loud)
    threading.Thread(target=lauf, daemon=True).start()
    return {"started": True, "video_for": anzeige, "format": fmt, "paid": f"${b['price']:.2f} from the balance",
            "note": "Paid from the balance: part buys $LOUD for loudscene, part buys and burns $ODDS. It usually takes 10 to 20 "
                    "minutes, the progress shows in the chat."}


def starten(adr, eingabe, brief=None, format=None, pay=None):
    """Werkzeug make_video: legt den Auftrag an und gibt den Link zur Bestaetigung zurueck. Testwallets: sofort, ohne Zahlung.
    Seit 2.63: pay='balance' zahlt aus dem Your-Engine-Guthaben. Niemand braucht $LOUD: getauscht wird im Hintergrund."""
    if not an():
        return {"started": False, "error": "Video making is not switched on yet."}
    url, anzeige = quelle(eingabe)
    if not url:
        return {"started": False, "error": anzeige}
    fmt = format if format in FORMATE else "16:9"
    import motor
    u = motor._tg_von(adr)
    if not u or not u.get("tg"):
        return {"started": False, "error": "The video comes in Telegram. Connect Telegram first."}
    if time.time() - _LAEUFT.get(adr, 0) < 20 * 60:
        return {"started": False, "error": "A video is already being made for you. It arrives here when it is ready."}
    if _gratis(adr) or _guthaben_video(adr):
        _guthaben_video(adr, nehmen=True)
        _rendern(adr, u["tg"], url, anzeige, brief, fmt, None)
        return {"started": True, "video_for": anzeige, "format": fmt, "paid": "test or re-render, no payment",
                "note": "Tell the user it takes a few minutes and the MP4 arrives here in Telegram."}
    if not _zahl_wallet():
        return {"started": False, "error": "Video payments are not set up yet."}
    b = _betraege()
    if not b:
        return {"started": False, "error": "Could not read the ETH price right now. Try again in a minute."}
    kasse_ok, _ = _kasse_bereit(b)
    guthaben = kasse_ok and _guthaben_ok(adr, b)
    if pay == "balance":
        if not kasse_ok:
            return {"started": False, "error": "Paying from the balance is not available right now. Pay in ETH from your wallet instead."}
        if not guthaben:
            return {"started": False, "error": f"Your balance is below ${b['price']:.2f}. Top it up with $ODDS on engine.oddsengine.live, "
                                               "or pay in ETH from your wallet."}
        return _aus_guthaben(adr, u["tg"], url, anzeige, brief, fmt, b)
    wallet_da = bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", str(adr or "")))
    import handel
    saldo = handel._nativ_saldo("robinhood", adr) if wallet_da else None
    noetig = b["wei_loud"] + b["wei_burn"]
    if not wallet_da or (saldo is not None and saldo < noetig * 1.02):
        warum = (f"Your wallet has {saldo / 1e18:.5f} ETH on Robinhood Chain, a video needs about {noetig / 1e18:.5f} ETH "
                 f"(${b['price']:.2f}) plus a little gas." if wallet_da and saldo is not None else
                 "Paying in ETH needs your wallet linked to Your Engine (Robinhood Chain).")
        if guthaben:
            return {"started": False, "pay_from_balance": True, "video_for": anzeige, "format": fmt, "price_usd": b["price"],
                    "error": warum + f" You can pay the ${b['price']:.2f} from your Your Engine balance instead.",
                    "note": "Nobody needs $LOUD: Your Engine swaps in the background. Offer to pay from the balance; if they say "
                            "yes, call make_video again with pay='balance'."}
        return {"started": False, "video_for": anzeige, "price_usd": b["price"],
                "error": warum + " Ways to pay: get a little ETH on Robinhood Chain (buy ETH in your wallet app or bridge it, "
                                 "for example with relay.link), or top up your Your Engine balance with $ODDS on engine.oddsengine.live "
                                 "and pay from the balance. You never need $LOUD, the swap happens in the background."}
    tid = secrets.token_hex(8)
    a = {"id": tid, "kind": "video", "adr": adr, "chain": "robinhood", "side": "video", "symbol": "VIDEO", "token": LOUD,
         "source": url, "label": anzeige, "brief": (str(brief)[:800] if brief else None), "format": fmt,
         "price": b["price"], "pct": b["pct"], "loud_usd": b["loud_usd"], "burn_usd": b["burn_usd"],
         "text_in": f"{noetig / 1e18:.5f} ETH (about ${b['price']:.2f})", "ts": int(time.time()), "status": "pending"}
    storage.kv_set("trade_" + tid, json.dumps(a))
    import handel as h
    return {"started": False, "needs_payment": True, "video_for": anzeige, "format": fmt, "price_usd": b["price"],
            "split": f"{100 - b['pct']:g}% buys $LOUD for loudscene, {b['pct']:g}% buys $ODDS and burns it",
            "confirm_url": f"{h.SEITE}/#trade={tid}", "valid_minutes": h.GUELTIG // 60,
            "pay_from_balance": guthaben,
            "note": "Show what you understood (video for X, format, price) and the button. Nothing is paid until they confirm "
                    "in their own wallet. The video starts automatically once the payment is on chain."}


def seite(a):
    """Bestaetigungsseite: zwei frische Angebote, beide Tokens gehen direkt an den Empfaenger."""
    import handel
    b = _betraege()
    if not b:
        return {"ok": False, "error": "Could not read the ETH price. Try again in a minute."}
    q1 = handel._quote(a["adr"], NATIVE, LOUD, b["wei_loud"], None, "robinhood", empfaenger=_zahl_wallet())
    if not q1.get("ok"):
        return q1
    txs, fees = list(q1["txs"]), float(q1.get("fees_usd") or 0)
    if b["wei_burn"]:
        q2 = handel._quote(a["adr"], NATIVE, ODDS, b["wei_burn"], None, "robinhood", empfaenger=DEAD)
        if not q2.get("ok"):
            return q2
        txs += q2["txs"]
        fees += float(q2.get("fees_usd") or 0)
    cid, exp, rpc, nat, name = handel._evm("robinhood")
    a["loud_usd"], a["burn_usd"] = b["loud_usd"], b["burn_usd"]
    storage.kv_set("trade_" + a["id"], json.dumps(a))
    return {"ok": True, "kind": "video", "id": a["id"], "chain": "robinhood", "chain_name": name, "native": nat,
            "side": "video", "symbol": "VIDEO", "wallet": a["adr"], "video_for": a.get("label"), "format": a.get("format"),
            "price_usd": b["price"], "loud_usd": b["loud_usd"], "burn_usd": b["burn_usd"], "burn_pct": b["pct"],
            "you_pay": f"{(b['wei_loud'] + b['wei_burn']) / 1e18:.5f} ETH (about ${b['price'] * PUFFER:.2f} incl. route fees)",
            "fees_usd": round(fees, 4), "txs": txs, "chain_id_hex": hex(cid), "rpc": rpc, "explorer": exp}


def _beleg(tx):
    import handel
    for _ in range(6):
        try:
            rc = handel._rpc("robinhood", "eth_getTransactionReceipt", [tx])
            if rc:
                return rc
        except Exception:
            pass
        time.sleep(5)
    return None


def _zugang(rc, token, an_adr):
    roh = 0
    for l in (rc or {}).get("logs") or []:
        t = l.get("topics") or []
        if (str(l.get("address") or "").lower() == token and t and t[0] == TRANSFER and len(t) > 2
                and t[2].lower().endswith(an_adr.lower()[2:])):
            roh += int(l.get("data") or "0x0", 16)
    return roh / 1e18


def _preis_usd(token):
    try:
        import scan
        return float(scan.zahlen("robinhood", token).get("price") or 0)
    except Exception:
        return 0.0


def nach_zahlung(a, hashes, ok, fehler=None):
    """Nach der Bestaetigungsseite: Belege pruefen, dann Video starten. Laeuft im Hintergrund."""
    def lauf():
        import motor
        adr = a["adr"]
        u = motor._tg_von(adr) or {}
        loud = burn = 0.0
        tx_loud = tx_burn = None
        for h in hashes or []:
            rc = _beleg(h)
            if not rc or str(rc.get("status")) not in ("0x1", "1"):
                continue
            x = _zugang(rc, LOUD, _zahl_wallet())
            if x:
                loud, tx_loud = loud + x, h
            y = _zugang(rc, ODDS, DEAD)
            if y:
                burn, tx_burn = burn + y, h
        p_loud = _preis_usd(LOUD)
        wert = loud * p_loud if p_loud else None
        bezahlt = loud > 0 and (wert is None or wert >= float(a.get("loud_usd") or 0) * 0.92)
        if not bezahlt:
            if hashes:
                motor.senden(adr, "I could not find the $LOUD payment for your video on chain"
                             + (f" ({fehler})" if fehler else "") + ". Nothing was started. If ETH left your wallet, "
                             "send me the transaction link and I check it.")
            return
        a.update(paid_loud=loud, burned_odds=burn, tx_loud=tx_loud, tx_burn=tx_burn)
        storage.kv_set("trade_" + a["id"], json.dumps(a))
        try:
            liste = json.loads(storage.kv_get("video_burns") or "[]")
        except Exception:
            liste = []
        liste.append({"ts": int(time.time()), "id": a["id"], "loud": round(loud, 2), "odds_burned": round(burn, 2),
                      "tx_loud": tx_loud, "tx_burn": tx_burn})
        storage.kv_set("video_burns", json.dumps(liste[-500:]))
        try:
            import ai
            ai._an_betreiber(f"Video bezahlt ({a.get('label')}): {loud:,.0f} LOUD an loudscene, {burn:,.0f} ODDS verbrannt.\n"
                             f"https://robinhoodchain.blockscout.com/tx/{tx_loud}"
                             + (f"\nBurn: https://robinhoodchain.blockscout.com/tx/{tx_burn}" if tx_burn else
                                "\nKein Burn: der Nutzer hat den zweiten Schritt nicht gesendet."))
        except Exception:
            pass
        if u.get("tg"):
            motor.senden(adr, f"🎬 Payment confirmed: {loud:,.0f} $LOUD to loudscene"
                         + (f", {burn:,.0f} $ODDS burned" if burn else "") + ". Your video is being made, it usually takes 10 to 20 minutes. The progress shows right below.")
            _rendern(adr, u["tg"], a["source"], a.get("label"), a.get("brief"), a.get("format"), tx_loud)
    threading.Thread(target=lauf, daemon=True).start()


def _guthaben_video(adr, nehmen=False):
    """Ein bezahltes Video, dessen Render scheiterte: der naechste Versuch kostet nichts."""
    k = "video_frei_" + str(adr).lower()
    try:
        n = int(storage.kv_get(k) or 0)
    except Exception:
        n = 0
    if nehmen and n > 0:
        storage.kv_set(k, str(n - 1))
    return n > 0


ERWARTET_MIN = float(os.getenv("VIDEO_EXPECT_MIN", "15"))
STUFEN = {1: "Reading the page and writing the story", 2: "Rendering the video in 1080p", 3: "Sending it to you"}


class _Status:
    """Seit 2.63: eine Statusnachricht, die waehrend der Herstellung mitlaeuft (Schritt, Balken, Minuten).
    Vorher kam nach 'arrives in a few minutes' bis zu 20 Minuten nichts, und es sah kaputt aus."""

    def __init__(self, chat_id, anzeige):
        self.chat, self.anzeige, self.mid, self.t0, self.letzt, self.nr = chat_id, anzeige, None, time.time(), 0, 1
        self._zeigen(force=True)

    def _text(self, fertig=False, fehler=None):
        minuten = int((time.time() - self.t0) / 60)
        if fertig:
            anteil = 1.0
        else:
            # Schaetzung nach Zeit, je Schritt begrenzt, damit der Balken nie bei 100 % haengt
            grenze = {1: 0.7, 2: 0.95, 3: 0.98}[self.nr]
            anteil = min(grenze, max({1: 0.03, 2: 0.7, 3: 0.95}[self.nr], (time.time() - self.t0) / 60 / ERWARTET_MIN))
        voll = int(anteil * 12)
        balken = "▰" * voll + "▱" * (12 - voll)
        if fehler:
            return f"🎬 Video for {self.anzeige}: stopped after {minuten} min."
        if fertig:
            return f"🎬 Video for {self.anzeige}: done in {max(1, minuten)} min. ✅"
        return (f"🎬 Making the video for {self.anzeige}\n\n{balken} {int(anteil * 100)}%\n"
                f"Step {self.nr} of 3: {STUFEN[self.nr]}\n"
                f"Running for {minuten} min · usually {int(ERWARTET_MIN * 0.7)} to {int(ERWARTET_MIN * 4 / 3)} min\n\n"
                "Nothing is stuck. You can close the chat, the video lands here when it is ready.")

    def _zeigen(self, force=False, **k):
        if not force and time.time() - self.letzt < 50:
            return
        self.letzt = time.time()
        try:
            import agent
            if self.mid:
                agent._tg("editMessageText", chat_id=self.chat, message_id=self.mid, text=self._text(**k))
            else:
                r = agent._tg("sendMessage", chat_id=self.chat, text=self._text(**k), disable_notification=True) or {}
                self.mid = (r.get("result") or {}).get("message_id")
        except Exception as e:
            log.debug(f"video status: {e}")

    def stufe(self, nr, d=None):
        neu = nr != self.nr
        self.nr = nr
        self._zeigen(force=neu)

    def ende(self, fehler=None):
        self._zeigen(force=True, fertig=not fehler, fehler=fehler)


# ── Seit 2.69.2: laufende Videos ueberleben einen Neustart des Servers (Deploy) ──
BOOT = secrets.token_hex(4)
_JOBS_KEY = "video_laeufe"
_JOB_LOCK = threading.Lock()


def _jobs():
    try:
        return json.loads(storage.kv_get(_JOBS_KEY) or "{}")
    except Exception:
        return {}


def _job_setzen(jid, werte=None, weg=False):
    with _JOB_LOCK:
        j = _jobs()
        if weg:
            j.pop(jid, None)
        else:
            j.setdefault(jid, {}).update(werte or {}, boot=BOOT)
        j = {k: v for k, v in j.items() if time.time() - (v.get("ts") or 0) < 3 * 3600}
        try:
            storage.kv_set(_JOBS_KEY, json.dumps(j))
        except Exception:
            pass


def fortsetzen():
    """Aus motor.tick: Videos, deren Server-Lauf durch einen Neustart abgebrochen ist, weiterverfolgen."""
    for jid, j in list(_jobs().items()):
        if j.get("boot") == BOOT or not j.get("vid"):
            if not j.get("vid") and j.get("boot") != BOOT and time.time() - (j.get("ts") or 0) > 600:
                _job_setzen(jid, weg=True)   # vor dem Neustart nie bei loudscene angekommen
            continue
        _job_setzen(jid, {"boot": BOOT})
        log.info(f"video: setze {jid} nach Neustart fort ({j.get('anzeige')})")
        _rendern(j["adr"], j["chat"], j["url"], j.get("anzeige"), j.get("brief"), j.get("fmt"), j.get("tx"),
                 jid=jid, vid=j.get("vid"), rid=j.get("rid"), t0=j.get("ts"))


def _rendern(adr, chat_id, url, anzeige, brief, fmt, zahlung_tx, jid=None, vid=None, rid=None, t0=None):
    _LAEUFT[adr] = time.time()
    jid = jid or secrets.token_hex(5)
    if not vid:
        _job_setzen(jid, {"adr": adr, "chat": chat_id, "url": url, "anzeige": anzeige, "brief": brief, "fmt": fmt,
                          "tx": zahlung_tx, "ts": int(time.time())})

    def lauf():
        import motor
        t0_ = time.time()
        st = _Status(chat_id, anzeige)
        if t0:
            st.t0 = float(t0)
        try:
            erg = erzeugen(url, brief or _AUTO.get(url), fmt, zahlung_tx=zahlung_tx, stufe=st.stufe,
                           merken=lambda **k: _job_setzen(jid, k), vid=vid, rid=rid)
            st.ende()
            _senden(chat_id, erg["url"], f"🎬 Your video for {anzeige} is ready. Made with loudscene.")
            log.info(f"video {adr[:8]} {url}: {time.time() - t0_:.0f}s")
        except Exception as e:
            log.warning(f"video {adr[:8]} {url}: {e}")
            st.ende(fehler=str(e))
            if zahlung_tx:
                k = "video_frei_" + str(adr).lower()
                try:
                    storage.kv_set(k, str(int(storage.kv_get(k) or 0) + 1))
                except Exception:
                    pass
            try:
                motor.senden(adr, "The video did not finish. "
                             + ("Your payment is kept: ask me again and the next try is free." if zahlung_tx
                                else "Try again in a few minutes.")
                             # Seit 2.62.8: Testwallets sehen den Grund, damit die Einrichtung schnell geht
                             + (f"\n\nReason (only testers see this): {str(e)[:300]}" if _gratis(adr) else ""))
            except Exception:
                pass
        finally:
            _LAEUFT.pop(adr, None)
            _job_setzen(jid, weg=True)
    threading.Thread(target=lauf, daemon=True).start()
