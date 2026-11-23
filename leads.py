"""
leads.py: Locker-Leads fuer den Betreiber (seit 2.76.0).

Einmal am Tag eine Liste von Robinhood-Chain-Projekten, die vom OddsEngine Locker profitieren wuerden, als Karten in
den Betreiber-Chat (TELEGRAM_BOT_TOKEN, AI_ALERT_CHAT_ID oder TELEGRAM_PRIVATE_CHAT_ID). Jede Karte: was offen ist
(Dev-Wallet oder grosse Wallet ohne Lock; LP-Stand nur als Info, LP-Positionen sperrt unser Locker nicht), Marketcap, Liquiditaet, Alter, Scan-Urteil,
Knoepfe fuer Telegram-Gruppe, X, Dexscreener und Scan, der Pitch als Codeblock (Tippen kopiert). Wir schicken selbst nichts an die Projekte.

Kandidaten: alle Torch-Coins (Torchs eigene Liste) und die 200 Pools mit dem meisten Volumen auf GeckoTerminal (Uniswap
und andere), dann je Coin der volle Robinhood-Check (onchain.token, lplock.lp_status) und der Scan.
Raus: Rug-Zeichen (Honeypot, Mint offen, Pause/Blacklist), schon Kunde im OddsEngine Locker, in den letzten LEADS_REPEAT_DAYS schon gezeigt, nichts offen.
Jede Zahl im Pitch kommt aus diesem Check, nichts geschaetzt.

Schalter (Railway):
  LEADS_ON          true    taeglich senden (Admin kann es jederzeit mit /v1/admin/leads?run=1 anstossen)
  LEADS_HOUR_UTC    9       ab dieser Stunde wird die Tagesliste gebaut
  LEADS_N           10      Karten je Tag
  LEADS_MIN_MC      150000  Marketcap in USD
  LEADS_MIN_LIQ     15000   Liquiditaet in USD
  LEADS_MIN_OPEN    3       ab so viel Prozent beim Dev ist es ein Lead (bei einer grossen Wallet mindestens 5 %)
  LEADS_MIN_AGE_D   1       juenger als so viele Tage: zu frueh fuer einen Pitch
  LEADS_REPEAT_DAYS 14      ein Projekt erst nach so vielen Tagen wieder zeigen
"""
import html as _h
import json
import os
import threading
import time

import requests

import storage
from logger import log

GT = "https://api.geckoterminal.com/api/v2/networks/robinhood/pools"
LOCK_SEITE = "https://engine.oddsengine.live/lock/"
_LAUF = {"an": False}


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
        log.debug(f"leads kv {k}: {e}")


def _usd(v):
    v = float(v or 0)
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.1f}K" if v >= 1e3 else f"${v:.0f}"


def _pct(v):
    return f"{v:.1f}%" if v >= 1 else f"{v:.2f}%"


# ── Kandidaten ───────────────────────────────────────────────────────────

def _gt_pools(seiten=10):
    aus = {}
    for s in range(1, seiten + 1):
        try:
            r = requests.get(GT, params={"page": s, "sort": "h24_volume_usd_desc", "include": "base_token"},
                             headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}, timeout=20)
            if r.status_code == 429:
                time.sleep(3)
                continue
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            log.debug(f"leads gt {s}: {e}")
            continue
        toks = {x.get("id"): (x.get("attributes") or {}).get("address") for x in d.get("included") or []}
        for p in d.get("data") or []:
            a = p.get("attributes") or {}
            bid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")
            t = str(toks.get(bid) or "").lower()
            if t.startswith("0x"):
                aus.setdefault(t, {"mc": float(a.get("market_cap_usd") or a.get("fdv_usd") or 0)})
        time.sleep(2.5)
    return aus


def _ds_info(tokens):
    """DexScreener je Token: bestes Paar mit Marketcap, Liquiditaet, Alter, Links. 30 je Abfrage."""
    aus = {}
    tokens = list(tokens)
    for i in range(0, len(tokens), 30):
        try:
            d = requests.get("https://api.dexscreener.com/tokens/v1/robinhood/" + ",".join(tokens[i:i + 30]),
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=20).json()
        except Exception as e:
            log.debug(f"leads ds: {e}")
            continue
        for p in d if isinstance(d, list) else []:
            a = str((p.get("baseToken") or {}).get("address") or "").lower()
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if a and liq > float(((aus.get(a) or {}).get("liquidity") or {}).get("usd") or -1):
                aus[a] = p
    return aus


def _links(p):
    info = (p or {}).get("info") or {}
    out = {}
    for s in info.get("socials") or []:
        typ, url = str(s.get("type") or "").lower(), s.get("url")
        if not url:
            continue
        if typ == "telegram" and "telegram" not in out:
            out["telegram"] = url
        elif typ in ("twitter", "x") and "x" not in out:
            out["x"] = url
    for w in info.get("websites") or []:
        if w.get("url"):
            out.setdefault("web", w["url"])
    return out


# ── Pruefen ──────────────────────────────────────────────────────────────

def pruefen(t, p=None):
    """Ein Coin: was ist offen? -> Lead-Dict oder None (mit Grund im Log)."""
    import onchain
    try:
        r = onchain.token(t)
    except Exception as e:
        log.debug(f"leads token {t[:10]}: {e}")
        return None
    if r.get("error"):
        return None
    sym = r.get("symbol") or "?"
    gesperrt = sum(x.get("pct") or 0 for x in r.get("locked") or [])
    try:
        import intel
        eigen = intel.odds_locker()
    except Exception:
        eigen = None
    if eigen and any(x.get("address") == eigen for x in r.get("locked") or []):
        return None   # schon Kunde
    offen = []
    dev = r.get("creator_holds_pct") or 0
    schwelle = _f("LEADS_MIN_OPEN", 3)
    if dev >= schwelle:
        offen.append(("dev", dev, f"Dev wallet holds {_pct(dev)} of supply, not locked"))
    wal = [x for x in r.get("top_list") or [] if not x.get("pool") and not x.get("locked")
           and (not r.get("creator") or x["address"] != r["creator"])]
    # Seit 2.77.5: Vertraege (Pools, Launchpads, Bruecken, Vaults) sind keine Wallets, die ein Team sperren koennte.
    # Vorher stand z.B. bei HPHONE ein Vertrag mit 8.5% als "largest wallet".
    try:
        code = onchain._vertraege([x["address"] for x in wal[:8]])
        wal = [x for x in wal if not code.get(x["address"])]
    except Exception:
        pass
    if wal and wal[0]["pct"] >= 90:
        log.info(f"leads {t[:10]}: largest wallet {wal[0]['pct']}%, data looks wrong, skipped")
        return None   # 100 % in einer Wallet bei Millionen Marketcap: Lesefehler oder Fake-Coin, kein Lead
    if wal and wal[0]["pct"] >= max(schwelle, 5):
        offen.append(("wallet", wal[0]["pct"], f"Largest wallet holds {_pct(wal[0]['pct'])}, not locked"))
    lp = None
    try:
        import lplock
        lp = lplock.lp_status(t)
    except Exception:
        lp = None
    lp_info = None
    if lp and lp.get("positions"):
        # Nur zur Info: LP-Positionen sperrt unser Locker (noch) nicht, darum ist das kein Grund fuer einen Pitch
        sicher = (lp.get("locked_pct") or 0) + (lp.get("burned_pct") or 0)
        lp_info = ("LP held by the launchpad" if (lp.get("launchpad_pct") or 0) >= 50
                   else f"LP positions locked or burned: {_pct(sicher)}")
    if not offen:
        return None
    # Scan-Urteil nur anzeigen, nicht filtern (seit 2.77.0): eine grosse ungesperrte Wallet macht den Scan oft rot,
    # genau das loest ein Lock. Raus fliegen nur Coins mit echten Rug-Zeichen (Honeypot, Mint offen, Pause/Blacklist).
    stufe, grund = None, None
    try:
        import scan
        s = scan.analyse(f"robinhood:{t}", deep=False)
        v = (s.get("verdict") or {}) if s.get("ok") else {}
        stufe = v.get("level")
        schlecht = v.get("bad") or []
        grund = schlecht[0] if schlecht else None
        sf = s.get("safety") or {}
        if sf.get("honeypot") or sf.get("mintable") or sf.get("pausable") or sf.get("blacklist"):
            return None
    except Exception as e:
        log.debug(f"leads scan {t[:10]}: {e}")
    p = p or {}
    links = _links(p)
    alter_tage = None
    if p.get("pairCreatedAt"):
        alter_tage = int((time.time() - p["pairCreatedAt"] / 1000) // 86400)
        if alter_tage < _f("LEADS_MIN_AGE_D", 1):
            return None   # seit 2.77.5: Coins vom selben Tag sind zu frueh (viele sind Fakes oder rugs)
    return {"token": t, "symbol": sym, "name": r.get("name"), "mc": r.get("market_cap_usd") or p.get("marketCap") or p.get("fdv"),
            "liq": (p.get("liquidity") or {}).get("usd") or r.get("liquidity_usd"),
            "vol24": (p.get("volume") or {}).get("h24") or r.get("volume_24h_usd"), "age_days": alter_tage,
            "dex": p.get("dexId"), "torch": bool(r.get("launched_on_torch")), "scan": stufe, "scan_reason": grund,
            "locked_elsewhere_pct": round(gesperrt, 2) if gesperrt else 0,
            "open": [{"kind": k, "pct": round(v, 2), "text": txt} for k, v, txt in offen], "lp_info": lp_info,
            "links": links, "chart": p.get("url") or r.get("chart"),
            "scan_page": f"https://engine.oddsengine.live/#token=robinhood:{t}"}


def pitch(l):
    """Text fuer das Projekt-Team. Nur Zahlen aus dem Check, menschlich, ohne Druck."""
    sym = "$" + str(l["symbol"]).lstrip("$")
    haupt = l["open"][0]
    if haupt["kind"] == "dev":
        satz = (f"the dev wallet still holds {_pct(haupt['pct'])} of the supply, unlocked. Scanners show that to every "
                "buyer and it holds people back.")
    else:
        satz = (f"the largest wallet holds {_pct(haupt['pct'])} of the supply and nothing is locked. If that is a team or "
                "treasury wallet, scanners show it to every buyer and it holds people back.")
    zeilen = [f"Hey, {sym} is doing well on Robinhood Chain. Quick heads up from the OddsEngine team: {satz}",
              "",
              f"You can lock it or set up vesting in a few minutes on the OddsEngine Locker: {LOCK_SEITE}",
              f"After that every OddsEngine scan of {sym} shows the lock and the unlock date. The contract is verified on "
              "Blockscout, nobody can move locked tokens early, not even us. Fees are small, and if you pay in $ODDS they "
              "get burned.",
              "",
              "Happy to walk you through it if you want."]
    return "\n".join(zeilen)


def karte(l, i):
    """-> (html, knoepfe). Der Pitch steht als Codeblock in der Karte: ein Tipp kopiert ihn."""
    e = _h.escape
    sym = "$" + str(l["symbol"]).lstrip("$")
    z = [f"<b>{i}. {e(l.get('name') or sym)}</b> · {e(sym)}"]
    z.append(" · ".join(x for x in (f"Mcap {_usd(l['mc'])}" if l.get("mc") else None,
                                     f"Liquidity {_usd(l['liq'])}" if l.get("liq") else None,
                                     f"24h volume {_usd(l['vol24'])}" if l.get("vol24") else None) if x))
    z.append(" · ".join(x for x in (f"Age {l['age_days']} days" if l.get("age_days") is not None else None,
                                     "Torch launch" if l.get("torch") else (f"on {e(l['dex'].title())}" if l.get("dex") else None),
                                     {"green": "scan green", "amber": "scan amber", "red": "scan red"}.get(l.get("scan") or "", "scan pending"),
                                     "Telegram group listed" if l["links"].get("telegram") else None) if x))
    for o in l["open"]:
        z.append("🔓 " + e(o["text"]))
    if l.get("scan") == "red" and l.get("scan_reason"):
        z.append("⚠️ Scan: " + e(l["scan_reason"]))
    if l.get("lp_info"):
        z.append("ℹ️ " + e(l["lp_info"]))
    if l.get("locked_elsewhere_pct"):
        z.append(f"Already locked elsewhere: {_pct(l['locked_elsewhere_pct'])}")
    z.append("")
    z.append("Pitch (tap to copy):")
    z.append(f"<pre>{e(pitch(l))}</pre>")
    knoepfe = []
    reihe = []
    if l["links"].get("telegram"):
        reihe.append({"text": "Open TG group", "url": l["links"]["telegram"]})
    if l["links"].get("x"):
        reihe.append({"text": "X", "url": l["links"]["x"]})
    if reihe:
        knoepfe.append(reihe)
    reihe = []
    if l.get("chart"):
        reihe.append({"text": "Dexscreener", "url": l["chart"]})
    reihe.append({"text": "Scan", "url": l["scan_page"]})
    knoepfe.append(reihe)
    return "\n".join(z), knoepfe


# ── Senden ───────────────────────────────────────────────────────────────

def _tg(text, knoepfe=None, html=False):
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.getenv("LEADS_CHAT_ID", "") or os.getenv("AI_ALERT_CHAT_ID", "") or os.getenv("TELEGRAM_PRIVATE_CHAT_ID", "")).strip()
    if not tok or not chat:
        log.warning("leads: no telegram chat")
        return False
    body = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if html:
        body["parse_mode"] = "HTML"
    if knoepfe:
        body["reply_markup"] = {"inline_keyboard": knoepfe}
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=body, timeout=15)
        if r.status_code != 200:
            log.warning(f"leads tg {r.status_code}: {r.text[:160]}")
        return r.status_code == 200
    except Exception as e:
        log.warning(f"leads tg: {e}")
        return False


def bauen(n=None):
    """Die Tagesliste bauen (dauert einige Minuten). -> [lead]"""
    n = int(n or _f("LEADS_N", 10))
    gezeigt = _j("leads_shown", {})
    jetzt = time.time()
    sperre = _f("LEADS_REPEAT_DAYS", 14) * 86400
    kandidaten = {}
    try:
        import onchain
        for x in onchain._torch_liste() or []:
            a = str(x.get("address") or "").lower()
            if a.startswith("0x"):
                kandidaten[a] = {"mc": float(x.get("marketCapUsd") or 0)}
    except Exception as e:
        log.debug(f"leads torch: {e}")
    for a, v in _gt_pools().items():
        kandidaten.setdefault(a, v)
    try:
        import stocktoken
        aktien = {x["address"] for x in stocktoken.liste().values()}
    except Exception:
        aktien = set()
    raus = aktien | {"0x9c87b4cfa21266d1638f437c96dbe1012cb81111"}
    try:
        import onchain as _oc
        raus |= {str(x).lower() for x in (_oc.WETH, _oc.USDG, _oc.TORCH) if x}
    except Exception:
        pass
    # Marketcap aus Torch/GeckoTerminal fehlt oft, darum entscheidet DexScreener (Coins nur auf der Bonding Curve
    # haben dort kein Paar und sind fuer Locks ohnehin zu frueh)
    vor = [a for a, v in kandidaten.items() if a not in raus and jetzt - gezeigt.get(a, 0) > sperre]
    ds = _ds_info(vor)
    gefiltert = []
    for a in vor:
        p = ds.get(a)
        if not p:
            continue
        mc = float(p.get("marketCap") or p.get("fdv") or 0)
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if mc >= _f("LEADS_MIN_MC", 150_000) and liq >= _f("LEADS_MIN_LIQ", 15_000):
            gefiltert.append((mc, a, p))
    gefiltert.sort(key=lambda x: -x[0])
    leads = []
    for mc, a, p in gefiltert[:max(n * 4, 30)]:
        l = pruefen(a, p)
        if l:
            leads.append(l)
        if len(leads) >= n * 2:
            break
    # mit Telegram-Gruppe zuerst (dort erreicht man ein Team am schnellsten), dann nach Marketcap
    leads.sort(key=lambda l: (l.get("scan") == "red", not l["links"].get("telegram"), -(l.get("mc") or 0)))
    return leads[:n]


def runde(erzwingen=False):
    """Aus motor (Takt 10 Minuten): einmal am Tag ab LEADS_HOUR_UTC die Liste bauen und schicken, im eigenen Faden."""
    if _LAUF["an"]:
        return False
    if not erzwingen:
        if os.getenv("LEADS_ON", "true").strip().lower() in ("0", "false", "no", "off"):
            return False
        tag = time.strftime("%Y%m%d", time.gmtime())
        letzt = _j("leads_last", {})
        if time.gmtime().tm_hour < int(_f("LEADS_HOUR_UTC", 9)):
            return False
        # war die Liste heute leer, nach 2 Stunden noch einmal versuchen (neue Coins, RPC-Aussetzer)
        if letzt.get("day") == tag and (letzt.get("leads") or time.time() - letzt.get("ts", 0) < 7200):
            return False
    _LAUF["an"] = True

    def lauf():
        try:
            tag = time.strftime("%Y%m%d", time.gmtime())
            leads = bauen()
            gezeigt = _j("leads_shown", {})
            jetzt = int(time.time())
            if not leads:
                _tg("🔐 Locker leads today: none\n\nNo Robinhood Chain project above the bar has unlocked supply in the dev "
                    "wallet or a big wallet right now (or all were shown in the last days).")
            else:
                mit_tg = sum(1 for l in leads if l["links"].get("telegram"))
                _tg(f"🔐 Locker leads today: {len(leads)}\n\n"
                    f"Robinhood Chain projects over {_usd(_f('LEADS_MIN_MC', 150_000))} market cap and "
                    f"{_usd(_f('LEADS_MIN_LIQ', 15_000))} liquidity with unlocked supply in the dev wallet or a big wallet, no rug signs, not "
                    f"customers yet, not shown in the last {int(_f('LEADS_REPEAT_DAYS', 14))} days. {mit_tg} with a Telegram "
                    "group (those first).\nEach card: tap the pitch to copy it, open their group or X, paste it to an admin. "
                    "Nothing is sent by us.")
                for i, l in enumerate(leads, 1):
                    txt, kn = karte(l, i)
                    _tg(txt, kn, html=True)
                    gezeigt[l["token"]] = jetzt
                    time.sleep(1.2)
            gezeigt = {k: v for k, v in gezeigt.items() if jetzt - v < 60 * 86400}
            _set("leads_shown", gezeigt)
            _set("leads_last", {"day": tag, "ts": jetzt, "leads": leads})
        except Exception as e:
            log.warning(f"leads: {e}")
        finally:
            _LAUF["an"] = False
    threading.Thread(target=lauf, daemon=True).start()
    return True


def stand():
    l = _j("leads_last", {})
    return {"running": _LAUF["an"], "last_day": l.get("day"), "last_ts": l.get("ts"),
            "leads": [dict(x, pitch=pitch(x)) for x in l.get("leads") or []],
            "settings": {k: os.getenv(k) for k in ("LEADS_ON", "LEADS_HOUR_UTC", "LEADS_N", "LEADS_MIN_MC", "LEADS_MIN_LIQ",
                                                   "LEADS_MIN_OPEN", "LEADS_REPEAT_DAYS") if os.getenv(k)}}
