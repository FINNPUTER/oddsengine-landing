"""
deals.py: Your Engine Deals. Seit 2.75.0 fuer jedes Produkt, nicht mehr nur Uhren, Parfum und Edelmetall.

Quellen (alle oeffentlich lesbar, getestet am 03.11.2026):
  Pepper-Netz     Die grossen Deal-Communities eines Landes, alle mit gleichem Aufbau: mydealz (DE), Preisjaeger (AT),
                  hotukdeals (UK), Dealabs (FR), Chollometro (ES), Pepper.pl (PL), Pepper NL, Promodescuentos (MX).
                  Je Deal: Preis, Vergleichspreis des Posters, Community-Temperatur (Abstimmung), Haendler, Versand,
                  Gutscheincode, abgelaufen ja/nein, Link. Gelesen wird die oeffentliche Suchseite (keine offizielle API),
                  darum mit Cache und Mengengrenze je Seite.
  Slickdeals (US) oeffentlicher RSS-Suchfeed (Titel mit Preis, Link, Datum).
  gold.de         Preisvergleich fuer Muenzen und Barren: Haendler, Preis, Preis mit Versand und Aufgeld zum Spot.
                  Spot in EUR aus der Kopfzeile von gold.de, Gegencheck Hyperliquid (xyz:GOLD/SILVER, xyz:EUR).
  ParfumDreams    nur als zweiter Preis bei Parfum (DE/AT): Sitemap als Index, Preise je Groesse aus der Produktseite.
Gesperrt (403, darum bewusst nicht drin): idealo, Geizhals, Chrono24, Flaconi, Douglas, Notino, eBay.
Italien: Pepper.it ist seit August 2025 geschlossen, Nutzer dort bekommen mydealz (viele Haendler liefern in die EU).

Ehrlich bleiben: Der Vergleichspreis ist, was der Poster angibt, nicht von uns geprueft. Die Temperatur ist die
Abstimmung der Community (negativ = die Leute halten es fuer keinen guten Deal). Deals koennen jederzeit enden.
Gutscheincodes nur, wenn sie woertlich in einem aktiven Deal stehen, mit Link und Datum. Nie raten, nie testen.

Schalter:
  Feature "deals" (Admin)   Tools und Alarme fuer alle, ENGINE_BETA immer
  DEALS_POLL_SEC   1800     wie oft ein Alarm nachsieht
  DEALS_MAX_DAY    25       Deal-Nachrichten je Nutzer und Tag
  DEALS_MAX_ALERTS 15       Deal-Alarme je Nutzer
  DEALS_SITE_HOUR  120      Abrufe je Deal-Seite und Stunde (alle Nutzer zusammen)
"""
import html as _html
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import storage
from logger import log

GD = "https://www.gold.de"
PD = "https://www.parfumdreams.de"
HL = "https://api.hyperliquid.xyz/info"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
       "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}


_CACHE = {}
_LOCK = threading.Lock()


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
        log.debug(f"deals kv {k}: {e}")


def _get(url, **kw):
    r = requests.get(url, headers=_UA, timeout=kw.pop("timeout", 25), **kw)
    r.raise_for_status()
    return r


def _zahl(s):
    return float(str(s).strip().replace(".", "").replace(",", "."))


def _txt(s):
    return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s or "")))).strip()


def _norm(s):
    s = str(s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("é", "e"), ("è", "e"), ("á", "a"), ("ó", "o")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9.]+", " ", s).strip()


def _woerter(s):
    return [w for w in _norm(s).split() if w and w not in ("the", "a", "and", "und", "der", "die", "das", "mm", "ref")]


def _eur(v):
    if v is None:
        return None
    return f"€{v:,.2f}".replace(".00", "") if v < 1000 else f"€{v:,.0f}"


def _tag(ts):
    return time.strftime("%d %b %Y", time.gmtime(ts)) if ts else None


# ── Spotpreise (EUR je Feinunze) ─────────────────────────────────────────

def spot():
    """{"gold_eur","silver_eur","gold_usd","silver_usd","src","checked"} oder {}. 5 Minuten Cache."""
    c = _CACHE.get("spot")
    if c and time.time() - c[0] < 300:
        return c[1]
    out = {}
    try:
        t = _txt(_get(GD + "/kaufen/goldmuenzen/kruegerrand/").text)
        m = re.search(r"GOLD ([\d.,]+) \$/oz ([\d.,]+) €/oz.*?SILBER ([\d.,]+) \$/oz ([\d.,]+) €/oz", t)
        if m:
            out = {"gold_usd": _zahl(m.group(1)), "gold_eur": _zahl(m.group(2)),
                   "silver_usd": _zahl(m.group(3)), "silver_eur": _zahl(m.group(4)), "src": "gold.de"}
    except Exception as e:
        log.debug(f"deals spot gold.de: {e}")
    try:
        meta, ctx = requests.post(HL, json={"type": "metaAndAssetCtxs", "dex": "xyz"}, timeout=8).json()
        px = {u.get("name"): float(c.get("oraclePx") or c.get("markPx") or 0) for u, c in zip(meta.get("universe") or [], ctx or [])}
        eurusd = px.get("xyz:EUR")
        if eurusd and px.get("xyz:GOLD") and px.get("xyz:SILVER"):
            hl = {"gold_eur": px["xyz:GOLD"] / eurusd, "silver_eur": px["xyz:SILVER"] / eurusd}
            if not out:
                out = {**hl, "gold_usd": px["xyz:GOLD"], "silver_usd": px["xyz:SILVER"], "src": "hyperliquid"}
            else:
                out["cross_check_gold_eur"] = round(hl["gold_eur"], 2)
                out["cross_check_silver_eur"] = round(hl["silver_eur"], 3)
    except Exception as e:
        log.debug(f"deals spot hl: {e}")
    if out:
        out["checked"] = int(time.time())
        _CACHE["spot"] = (time.time(), out)
    return out


_GRAMM_OZ = 31.1034768


def _gewicht_oz(titel):
    """Feingewicht in Unzen aus einem Titel wie '1 Unze Silber', '1/10 oz Gold', '100 g Silber', '1 kg'."""
    t = _norm(titel).replace(",", ".")
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*(?:oz|unze|unzen)\b", str(titel).lower().replace(",", "."))
    if m:
        return int(m.group(1)) / int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(oz|unze|unzen|kg|kilo|g|gramm)\b", t)
    if not m:
        return None
    v, e = float(m.group(1)), m.group(2)
    if e in ("oz", "unze", "unzen"):
        return v
    if e in ("kg", "kilo"):
        return v * 1000 / _GRAMM_OZ
    return v / _GRAMM_OZ


def _metall(titel):
    t = _norm(titel)
    if "silber" in t or "silver" in t:
        return "silver"
    if "gold" in t:
        return "gold"
    return None


def aufgeld(titel, preis):
    """Aufschlag auf den Metallwert in Prozent, oder None. Grob: Feingewicht aus dem Titel."""
    oz, mt = _gewicht_oz(titel), _metall(titel)
    s = spot()
    if not (oz and mt and preis and s.get(mt + "_eur")):
        return None
    wert = oz * s[mt + "_eur"]
    return {"metal": mt, "fine_oz": round(oz, 4), "metal_value_eur": round(wert, 2),
            "over_metal_value_pct": round((preis / wert - 1) * 100, 1)}




# ── ParfumDreams ──

def _pd_index_bauen():
    pfade = []
    try:
        idx = _get(PD + "/Sitemaps/sitemap.de.xml").text
        karten = [u for u in re.findall(r"<loc>([^<]+)</loc>", idx) if "/Articles." in u]
        for u in karten:
            rest = ""
            with requests.get(u, headers=_UA, timeout=90, stream=True) as r:
                r.raise_for_status()
                for stueck in r.iter_content(1 << 16):
                    rest += stueck.decode("utf-8", "ignore")
                    teile = re.findall(r"<loc>https://www\.parfumdreams\.de(/[^<]+?/index_\d+\.aspx)</loc>", rest)
                    pfade += teile
                    k = rest.rfind("</loc>")
                    rest = rest[k + 6:] if k >= 0 else rest[-400:]
    except Exception as e:
        log.warning(f"deals pd index: {e}")
    pfade = list(dict.fromkeys(pfade))
    if len(pfade) > 5000:
        try:
            storage.kv_set("deals_pd_index", "\n".join(pfade))
            _set("deals_pd_meta", {"ts": int(time.time()), "n": len(pfade)})
        except Exception:
            pass
        _CACHE["pd"] = (time.time(), pfade)
    return pfade


def _pd_index():
    c = _CACHE.get("pd")
    if c and time.time() - c[0] < 3600:
        return c[1]
    meta = _j("deals_pd_meta", {})
    pfade = []
    try:
        roh = storage.kv_get("deals_pd_index")
        pfade = roh.split("\n") if roh else []
    except Exception:
        pfade = []
    if (not pfade or time.time() - meta.get("ts", 0) > 86400) and not _CACHE.get("pd_bau"):
        # Sitemap ist rund 60 MB: immer im Hintergrund, nie im Gespraech
        _CACHE["pd_bau"] = True

        def _bau():
            try:
                _pd_index_bauen()
            finally:
                _CACHE.pop("pd_bau", None)
        threading.Thread(target=_bau, daemon=True).start()
    if pfade:
        _CACHE["pd"] = (time.time(), pfade)
    return pfade


def pd_produkt(url):
    """Preise je Groesse aus der Produktseite (JSON-LD). 30 Minuten Cache."""
    url = url.split("#")[0]
    c = _CACHE.get("pdp:" + url)
    if c and time.time() - c[0] < 1800:
        return c[1]
    try:
        b = _get(url).text
    except Exception as e:
        return {"error": f"ParfumDreams did not answer ({str(e)[:60]})"}
    out = None
    for roh in re.findall(r'<script type="application/ld\+json">(.*?)</script>', b, re.S):
        try:
            d = json.loads(roh)
        except Exception:
            continue
        for g in (d if isinstance(d, list) else [d]):
            if g.get("@type") not in ("ProductGroup", "Product"):
                continue
            varianten = g.get("hasVariant") or [g]
            groessen = []
            for v in varianten:
                o = v.get("offers") or {}
                o = o[0] if isinstance(o, list) else o
                try:
                    preis = float(o.get("price"))
                except Exception:
                    continue
                groessen.append({"size": v.get("size") or o.get("size"), "price_eur": preis,
                                 "in_stock": "InStock" in str(o.get("availability") or "")})
            if groessen:
                out = {"source": "ParfumDreams", "title": g.get("name"), "brand": (g.get("brand") or {}).get("name"),
                       "url": url, "sizes": sorted(groessen, key=lambda x: x["price_eur"])}
                break
        if out:
            break
    out = out or {"error": "no price on that page"}
    _CACHE["pdp:" + url] = (time.time(), out)
    return out


def pd_suche(query, n=4):
    woerter = _woerter(query)
    if not woerter:
        return []
    treffer = []
    for p in _pd_index():
        heu = _norm(p.replace("/", " ").replace("-", " "))
        hw = set(heu.split())
        z = sum(1 for w in woerter if w in hw or (len(w) > 3 and w in heu))
        if z >= max(1, len(woerter) - (1 if len(woerter) >= 3 else 0)):
            treffer.append((z, -len(p), p))
    treffer.sort(reverse=True)
    urls = [PD + t[2] for t in treffer[:n]]
    if not urls:
        return []
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(pd_produkt, urls))
    return [r for r in res if not r.get("error")]


# ── gold.de ──────────────────────────────────────────────────────────────

_GD_MUENZEN = {
    "gold": [("kruegerrand", "goldmuenzen/kruegerrand"), ("krugerrand", "goldmuenzen/kruegerrand"),
             ("maple", "goldmuenzen/maple-leaf"), ("philharmoniker", "goldmuenzen/wiener-philharmoniker"),
             ("philharmonic", "goldmuenzen/wiener-philharmoniker"), ("kaenguru", "goldmuenzen/kaenguru"),
             ("kangaroo", "goldmuenzen/kaenguru"), ("nugget", "goldmuenzen/kaenguru"), ("eagle", "goldmuenzen/american-eagle"),
             ("britannia", "goldmuenzen/britannia"), ("vreneli", "goldmuenzen/vreneli"), ("goldeuro", "goldmuenzen/goldeuro")],
    "silver": [("kruegerrand", "silbermuenzen/kruegerrand-silber"), ("krugerrand", "silbermuenzen/kruegerrand-silber"),
               ("maple", "silbermuenzen/maple-leaf"), ("philharmoniker", "silbermuenzen/wiener-philharmoniker"),
               ("philharmonic", "silbermuenzen/wiener-philharmoniker"), ("kaenguru", "silbermuenzen/kaenguru-perth-mint"),
               ("kangaroo", "silbermuenzen/kaenguru-perth-mint"), ("eagle", "silbermuenzen/american-eagle"),
               ("britannia", "silbermuenzen/britannia"), ("beast", "silbermuenzen/the-queens-beast-silber")],
}
_GD_BARREN = {
    "gold": {1 / 31.1034768: "1-gramm", 2 / 31.1034768: "2-gramm", 2.5 / 31.1034768: "2-5-gramm", 5 / 31.1034768: "5-gramm",
             10 / 31.1034768: "10-gramm", 20 / 31.1034768: "20-gramm", 25 / 31.1034768: "25-gramm", 50 / 31.1034768: "50-gramm",
             100 / 31.1034768: "100-gramm", 250 / 31.1034768: "250-gramm", 500 / 31.1034768: "500-gramm",
             1000 / 31.1034768: "1kg", 1.0: "1-oz", 0.5: "1-2-oz"},
    "silver": {1.0: "1-oz", 20 / 31.1034768: "20-gramm", 50 / 31.1034768: "50-gramm", 100 / 31.1034768: "100-gramm",
               250 / 31.1034768: "250-gramm", 500 / 31.1034768: "500-gramm", 1000 / 31.1034768: "1000-gramm",
               5000 / 31.1034768: "5000-gramm", 15000 / 31.1034768: "15000-gramm"},
}


def _gd_pfad(produkt):
    t = _norm(produkt)
    mt = "silver" if ("silber" in t or "silver" in t) else "gold"
    for key, pfad in _GD_MUENZEN[mt]:
        if key in t:
            return pfad, mt
    oz = _gewicht_oz(produkt) or (1.0 if ("bar" in t or "barren" in t) else None)
    if oz:
        best = min(_GD_BARREN[mt], key=lambda k: abs(k - oz) / k)
        if abs(best - oz) / best < 0.02:
            return f"{'silberbarren' if mt == 'silver' else 'goldbarren'}/{_GD_BARREN[mt][best]}", mt
    return None, mt


def gd_angebote(pfad):
    c = _CACHE.get("gd:" + pfad)
    if c and time.time() - c[0] < 600:
        return c[1]
    b = _get(f"{GD}/kaufen/{pfad}/").text
    out = []
    for blk in re.split(r'<div class="pv-row', b)[1:]:
        titel = re.search(r'class="pv-titel">([^<]+)<', blk)
        einh = re.search(r'Einheit:\s*([^<]+)<', blk)
        h = re.search(r'href="https://www\.gold\.de/haendler/[^"]+"[^>]*>([^<]+)</a>', blk)
        pr = re.search(r'class="pv-preis"><span>([\d.,]+)</span>', blk)
        ges = re.search(r'class="pv_preis_gesamt"><span>([\d.,]+)</span>', blk)
        sp = re.search(r'Spotpreis</div><div class="vs_info_right">([+-]?[\d,]+)\s*%', blk)
        lz = re.search(r'Lieferzeit</div><div class="vs_info_right[^"]*">([^<]+)<', blk)
        if not (titel and h and pr):
            continue
        out.append({"product": _txt(titel.group(1)), "unit": einh and _txt(einh.group(1)), "dealer": _txt(h.group(1)),
                    "price_eur": _zahl(pr.group(1)), "with_shipping_eur": ges and _zahl(ges.group(1)),
                    "over_spot_pct": sp and _zahl(sp.group(1)), "delivery": lz and _txt(lz.group(1))})
    _CACHE["gd:" + pfad] = (time.time(), out)
    return out




# ── Pepper-Netz und Slickdeals ───────────────────────────────────────────

PEPPER = {"de": ("https://www.mydealz.de", "mydealz", "EUR"), "at": ("https://www.preisjaeger.at", "Preisjäger", "EUR"),
          "uk": ("https://www.hotukdeals.com", "hotukdeals", "GBP"), "fr": ("https://www.dealabs.com", "Dealabs", "EUR"),
          "es": ("https://www.chollometro.com", "Chollometro", "EUR"), "pl": ("https://www.pepper.pl", "Pepper.pl", "PLN"),
          "nl": ("https://nl.pepper.com", "Pepper NL", "EUR"), "mx": ("https://www.promodescuentos.com", "Promodescuentos", "MXN")}
SLICK = "https://slickdeals.net/newsearch.php"
REGIONEN = {"de": "de", "germany": "de", "deutschland": "de", "at": "at", "austria": "at", "oesterreich": "at",
            "uk": "uk", "gb": "uk", "england": "uk", "britain": "uk", "united kingdom": "uk", "fr": "fr", "france": "fr",
            "es": "es", "spain": "es", "spanien": "es", "pl": "pl", "poland": "pl", "nl": "nl", "netherlands": "nl",
            "holland": "nl", "be": "nl", "belgium": "nl", "mx": "mx", "mexico": "mx", "us": "us", "usa": "us",
            "united states": "us", "america": "us", "ch": "de", "switzerland": "de", "it": "de", "italy": "de", "eu": "de"}
TZ_REGION = {"Europe/Berlin": "de", "Europe/Vienna": "at", "Europe/Zurich": "de", "Europe/London": "uk", "Europe/Dublin": "uk",
             "Europe/Paris": "fr", "Europe/Brussels": "nl", "Europe/Madrid": "es", "Europe/Warsaw": "pl",
             "Europe/Amsterdam": "nl", "America/Mexico_City": "mx", "Europe/Rome": "de", "Europe/Lisbon": "es"}
SYM = {"EUR": "€", "GBP": "£", "PLN": "zł ", "MXN": "MX$", "USD": "$"}
_RATE = {}


def region_von(region=None, tz=None):
    r = REGIONEN.get(_norm(region)) if region else None
    if r:
        return r
    if tz:
        if tz in TZ_REGION:
            return TZ_REGION[tz]
        if tz.startswith("America/"):
            return "us"
    return "de"


def _darf_abruf(host):
    """Mengengrenze je Seite und Stunde, damit wir nirgends auffallen."""
    h = int(time.time() // 3600)
    z = _RATE.get(host)
    if not z or z[0] != h:
        z = _RATE[host] = [h, 0]
    if z[1] >= int(_f("DEALS_SITE_HOUR", 120)):
        return False
    z[1] += 1
    return True


def _geld(v, cur):
    if v is None:
        return None
    s = SYM.get(cur, cur + " ")
    return f"{s}{v:,.2f}".replace(".00", "") if v < 1000 else f"{s}{v:,.0f}"


def _pepper_lesen(region, pfad, params=None):
    """Threads einer Pepper-Seite. -> Liste (normalisiert) oder None bei Fehler/Grenze. 10 Minuten Cache."""
    basis, name, cur = PEPPER[region]
    ck = "pp:" + region + pfad + json.dumps(params or {}, sort_keys=True)
    c = _CACHE.get(ck)
    if c and time.time() - c[0] < 600:
        return c[1]
    if not _darf_abruf(basis):
        return None
    try:
        b = _get(basis + pfad, params=params, timeout=20).text
    except Exception as e:
        log.warning(f"deals {name}: {e}")
        return None
    jetzt = time.time()
    out = []
    for roh in re.findall(r"data-vue3='(\{.*?\})'", b):
        try:
            d = json.loads(_html.unescape(roh))
        except Exception:
            continue
        t = (d.get("props") or {}).get("thread")
        if not t or t.get("type") not in ("Deal", "Voucher") or t.get("status") not in (None, "Activated"):
            continue
        ende = (t.get("endDate") or {}).get("timestamp") if isinstance(t.get("endDate"), dict) else None
        preis = t.get("price") or None
        vgl = t.get("nextBestPrice") or None
        code = "" if t.get("isVoucherCodeHidden") else str(t.get("voucherCode") or "").strip()
        out.append({"title": _txt(t.get("title")), "price": float(preis) if preis else None,
                    "compare_price": float(vgl) if vgl and preis and vgl > preis else None,
                    "save_pct": round((1 - preis / vgl) * 100) if vgl and preis and vgl > preis else None,
                    "temperature": round(float(t.get("temperature") or 0)),
                    "expired": bool(t.get("isExpired")) or bool(ende and ende < jetzt),
                    "posted_ts": int(t.get("publishedAt") or 0), "ends_ts": ende,
                    "merchant": ((t.get("merchant") or {}).get("merchantName")) or t.get("linkHost"),
                    "free_shipping": bool((t.get("shipping") or {}).get("isFree")),
                    "code": code or None, "group": ((t.get("mainGroup") or {}).get("threadGroupName")),
                    "type": t.get("type"), "link": t.get("shareableLink"), "id": str(t.get("threadId") or ""),
                    "source": name, "currency": cur})
    _CACHE[ck] = (time.time(), out)
    return out


def _slick_lesen(query, mode="frontpage"):
    """Slickdeals-RSS. Ohne query (seit 2.80.5) der ganze Feed: frontpage = Startseite, popdeals = beliebte Deals."""
    ck = "sd:" + mode + ":" + (query or "")
    c = _CACHE.get(ck)
    if c and time.time() - c[0] < 600:
        return c[1]
    if not _darf_abruf("slickdeals"):
        return None
    par = {"mode": mode, "searcharea": "deals", "searchin": "first", "rss": 1}
    if query:
        par["q"] = query
    try:
        b = _get(SLICK, params=par, timeout=20).text
    except Exception as e:
        log.warning(f"deals slickdeals: {e}")
        return None
    from email.utils import parsedate_to_datetime
    out = []
    for it in re.findall(r"<item>(.*?)</item>", b, re.S):
        ti = re.search(r"<title>(.*?)</title>", it, re.S)
        li = re.search(r"<link>(.*?)</link>", it, re.S)
        da = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        if not (ti and li):
            continue
        titel = _txt(re.sub(r"<!\[CDATA\[|\]\]>", "", ti.group(1)))
        pm = re.search(r"\$\s?([\d,]+(?:\.\d\d)?)", titel)
        try:
            ts = int(parsedate_to_datetime(da.group(1).strip()).timestamp()) if da else 0
        except Exception:
            ts = 0
        out.append({"title": titel, "price": float(pm.group(1).replace(",", "")) if pm else None, "compare_price": None,
                    "save_pct": None, "temperature": None, "expired": False, "posted_ts": ts, "ends_ts": None,
                    "merchant": None, "free_shipping": "free shipping" in titel.lower(), "code": None, "group": None,
                    "type": "Deal", "link": _txt(li.group(1)), "id": _txt(li.group(1)), "source": "Slickdeals", "currency": "USD"})
    _CACHE[ck] = (time.time(), out)
    return out


def _passt_titel(d, woerter):
    if not woerter:
        return True
    heu = _norm(d["title"] + " " + str(d.get("merchant") or ""))
    hw = set(heu.split())
    z = sum(1 for w in woerter if w in hw or (len(w) > 3 and w in heu))
    return z >= len(woerter) - (1 if len(woerter) >= 4 else 0)


def _vertrag(d):
    t = _norm(d["title"] + " " + str(d.get("group") or ""))
    return any(w in t for w in ("vertraege", "vertrag", " mtl", "allnet", "contract", "sim only", "per month", "airtime",
                                "forfait", "tarifa", "abonnement"))


# Seit 2.77.0: Zubehoer und Spiele nach hinten, wenn jemand das Geraet selbst sucht ("PS5" soll die Konsole zeigen,
# nicht ein PS5-Spiel fuer 0,99 oder eine Huelle). Wer Zubehoer sucht ("PS5 controller"), bekommt es normal.
ZUBEHOER = ("huelle", "case", "cover", "kabel", "cable", "charger", "ladegeraet", "ladestation", "charging", "adapter",
            "halterung", "mount", "skin", "schutzfolie", "protector", "folie", "brush", "buerste", "ersatz", "replacement",
            "strap", "armband", "tasche", "sleeve", "dock", "aufsatz", "filter", "akku", "battery", "coque", "funda",
            "etui", "controller", "headset", "ladestation", "staender", "stand")
SPIEL = ("steam", "dlc", "psn", "season pass", "playstation store", "xbox store", "nintendo eshop", "gog", "epic games",
         "digital code", "key")
VOR = ("fuer", "for", "compatible", "kompatibel", "fits", "pour", "para", "voor", "dla", "passend", "with", "mit")


def _zubehoer(d, woerter):
    """True, wenn der Deal wahrscheinlich Zubehoer oder ein Spiel zum gesuchten Produkt ist (nicht das Produkt selbst)."""
    if not woerter:
        return False
    q = " " + " ".join(woerter) + " "
    if any(f" {w} " in q for w in ZUBEHOER + SPIEL + ("game", "spiel", "games", "spiele")):
        return False   # der Nutzer sucht selbst Zubehoer oder Spiele
    t = " " + _norm(d["title"]) + " "
    teile = t.split()
    if any(f" {z} " in t for z in SPIEL) or (" ps4 " in t and " ps5 " in t):
        return True    # Spiel oder digitaler Code
    erst = next((i for i, w in enumerate(teile) if any(w.startswith(x) for x in woerter if len(x) > 1)), None)
    if erst is None:
        return False
    vorne = teile[:erst]
    # Zubehoerwort oder "fuer/for" VOR dem gesuchten Produkt: "Huelle fuer iPhone 17", "Charging station for AirPods"
    return any(z in vorne for z in ZUBEHOER) or any(v in vorne for v in VOR)


def _aktiv(xs, woerter=None, max_price=None, min_price=None, tage=None):
    jetzt = time.time()
    return [d for d in xs if not d["expired"] and _passt_titel(d, woerter)
            and (max_price is None or (d["price"] is not None and d["price"] <= max_price))
            and (min_price is None or (d["price"] is not None and d["price"] >= min_price))
            and (tage is None or jetzt - d["posted_ts"] < tage * 86400)]


def _karte(d):
    jetzt = time.time()
    k = {"title": d["title"], "price": _geld(d["price"], d["currency"]) if d["price"] else "see deal",
         "price_value": d["price"], "currency": d["currency"], "shop": d.get("merchant"), "source": d["source"],
         "posted": _tag(d["posted_ts"]), "days_ago": round((jetzt - d["posted_ts"]) / 86400, 1) if d["posted_ts"] else None,
         "link": d["link"]}
    if d.get("compare_price"):
        k["poster_compare_price"] = _geld(d["compare_price"], d["currency"])
        k["save_pct_vs_compare"] = d["save_pct"]
    if d.get("temperature") is not None:
        k["community_temperature"] = d["temperature"]
    if d.get("code"):
        k["code_as_posted"] = d["code"]
    if _vertrag(d):
        k["with_contract"] = True
    if d.get("_acc"):
        k["accessory_or_game"] = True
    if d.get("ends_ts"):
        k["ends"] = _tag(d["ends_ts"])
    if d.get("free_shipping"):
        k["free_shipping"] = True
    if d.get("group"):
        k["category"] = d["group"]
    return k


def _lesen(region, query):
    return _slick_lesen(query) if region == "us" else _pepper_lesen(region, "/search", {"q": query})


LESEHILFE = ("price is what the deal costs now (shipping extra unless free_shipping). poster_compare_price is the comparison "
             "price the person who posted it gives (often the next best shop or the normal price), we did not check it: say "
             "'the poster compares it with X'. community_temperature is the community vote: 100+ is hot, 500+ very hot, "
             "under 0 means people think it is not a good deal, say that. Deals can end any time, always give the link. "
             "Region decides the shops (mydealz for Germany and most of the EU, hotukdeals UK, Slickdeals US ...). "
             "Never call a deal the cheapest price on the market, only what this deal costs. with_contract means the price comes with a "
             "phone or internet contract, say so. accessory_or_game means it is a case, charger, game or other add-on, not the "
             "product itself: never present it as the deal for the product; if only those are found, say no deal for the "
             "product itself is active and offer an alert. code_as_posted is the poster's text, quote it as written.")


def suchen(query=None, category=None, brand=None, max_price=None, min_price=None, region=None, tz=None):
    q = " ".join(x for x in (brand, query) if x).strip()
    if not q and category:
        q = category
    if not q:
        return {"ok": False, "error": "Tell me what you are looking for (product, brand or model)."}
    reg = region_von(region, tz)
    roh = _lesen(reg, q)
    quelle = "Slickdeals" if reg == "us" else PEPPER[reg][1]
    out = {"ok": True, "query": q, "region": reg, "source": quelle}
    if roh is None:
        out.update(ok=False, error=f"{quelle} did not answer right now. Try again in a few minutes.")
        return out
    woerter = _woerter(q)
    aktiv = _aktiv(roh, woerter, max_price, min_price, tage=120)
    # Reihenfolge der Suche (Relevanz) behalten, aber Vertrags-Bundles (Handyvertrag + Geraet) und von der Community
    # abgelehnte Deals nach hinten. Wer nach einem Vertrag fragt, bekommt sie normal.
    will_vertrag = any(w in _norm(q).split() for w in ("vertrag", "contract", "tarif", "plan", "sim"))
    aktiv.sort(key=lambda d: ((d["temperature"] or 0) < 0, _vertrag(d) and not will_vertrag, _zubehoer(d, woerter)))
    for d in aktiv:
        d["_acc"] = _zubehoer(d, woerter)
    out["active_deals"] = [_karte(d) for d in aktiv[:8]]
    out["active_found"] = len(aktiv)
    abgelaufen = [d for d in roh if d["expired"] and _passt_titel(d, woerter) and d["price"]]
    if abgelaufen:
        tief = min(abgelaufen, key=lambda d: d["price"])
        out["lowest_expired_deal"] = {"price": _geld(tief["price"], tief["currency"]), "posted": _tag(tief["posted_ts"]),
                                      "title": tief["title"], "link": tief["link"],
                                      "note": "ended, only shows what the price was at its best recently"}
    if not aktiv:
        out["note"] = (f"No active deal for this on {quelle} right now. Offer a deal alert (deal_alert) so the user hears "
                       "about the next one, and for a plain price check use web_search.")
    t = _norm(q)
    if any(w in t.split() for w in ("parfum", "perfume", "eau", "edp", "edt", "parfuem", "fragrance")) or (category and _norm(category) in ("perfume", "parfum", "fragrance")):
        if reg in ("de", "at"):
            pd = pd_suche(q, 3)
            if max_price:
                for p in pd:
                    p["sizes"] = [s_ for s_ in p["sizes"] if s_["price_eur"] <= max_price]
                pd = [p for p in pd if p["sizes"]]
            out["parfumdreams_prices"] = pd
            if pd:
                out["parfumdreams_note"] = "Prices at ParfumDreams right now, one shop, use them as a reference next to the deals."
    if _metall(q) and (_gewicht_oz(q) or any(w in t for w in ("muenze", "coin", "barren", "bar", "kruegerrand", "krugerrand", "maple", "philharmon"))):
        out["metal_hint"] = "This is bullion: call metal_price_check for the dealer comparison and the premium over spot."
    out["how_to_read"] = LESEHILFE
    return out


def heute(category=None, brand=None, max_price=None, region=None, tz=None):
    """Die heissen Deals von heute (Startseite der Community), optional gefiltert."""
    reg = region_von(region, tz)
    if reg == "us":
        # Seit 2.80.5: der Startseiten-Feed von Slickdeals ohne Suchwort. Vorher wurde nach "deal" gesucht und jeder
        # Titel ohne das Wort "deal" fiel durch den Filter, die Liste war immer leer.
        roh = _slick_lesen("", "frontpage")
        if not roh:
            roh = _slick_lesen("", "popdeals")
        if roh is None:
            return {"ok": False, "error": "Slickdeals did not answer right now. Try again in a few minutes."}
        woerter = _woerter(" ".join(x for x in (brand, category) if x)) if (brand or category) else None
        xs = _aktiv(roh, woerter, max_price, None, tage=3)
        out = {"ok": True, "region": "us", "source": "Slickdeals", "hot_today": [_karte(d) for d in xs[:10]],
               "how_to_read": LESEHILFE + " Slickdeals front page: newest first, no community vote in the feed."}
        if not roh:
            out["note"] = "The Slickdeals feed came back empty right now. Use deals_search for a specific product."
        elif (brand or category) and not xs:
            out["note"] = "Nothing on the Slickdeals front page for that. Use deals_search for a specific product."
        return out
    roh = _pepper_lesen(reg, "/")
    name = PEPPER[reg][1]
    if roh is None:
        return {"ok": False, "error": f"{name} did not answer right now. Try again in a few minutes."}
    woerter = _woerter(" ".join(x for x in (brand, category) if x)) if (brand or category) else None
    if woerter:
        roh_f = [d for d in roh if _passt_titel(d, woerter) or (d.get("group") and any(w in _norm(d["group"]) for w in woerter))]
    else:
        roh_f = roh
    xs = _aktiv(roh_f, None, max_price, None, tage=3)
    xs.sort(key=lambda d: -(d["temperature"] or 0))
    out = {"ok": True, "region": reg, "source": name, "hot_today": [_karte(d) for d in xs[:10]], "how_to_read": LESEHILFE}
    if (brand or category) and not xs:
        out["note"] = "Nothing in today's hot list for that. Use deals_search for a specific product."
    return out


def metall_check(product):
    pfad, mt = _gd_pfad(product)
    s = spot()
    out = {"spot": {"gold_eur_oz": s.get("gold_eur"), "silver_eur_oz": s.get("silver_eur"), "source": s.get("src"),
                    "cross_check_gold_eur_oz": s.get("cross_check_gold_eur"), "cross_check_silver_eur_oz": s.get("cross_check_silver_eur"),
                    "checked": s.get("checked")}}
    if pfad:
        try:
            ang = gd_angebote(pfad)
            out["gold_de"] = {"url": f"{GD}/kaufen/{pfad}/", "cheapest": ang[:6], "offers_read": len(ang)}
        except Exception as e:
            out["gold_de"] = {"error": f"gold.de did not answer ({str(e)[:60]})"}
    else:
        out["gold_de"] = {"error": "no gold.de comparison page for that product. Known: Kruegerrand, Maple Leaf, Philharmoniker, "
                                   "Kangaroo, American Eagle, Britannia, Vreneli, Goldeuro, Queen's Beasts, bars from 1 g to 1 kg (gold) "
                                   "or 1 oz to 15 kg (silver)."}
    out["how_to_read"] = ("over_spot_pct is the premium over the metal value at spot. Investment gold is VAT free in the EU, "
                          "silver coins carry VAT, so silver premiums are higher by nature. Limited or colored collector coins "
                          "cost far more than their metal: say that plainly, it is not a metal deal. Compare with_shipping_eur "
                          "when the user buys one piece. gold.de compares German dealers, prices in EUR.")
    return out


def gutscheine(shop, region=None, tz=None):
    """Gutscheincodes, die in aktiven Deals der Community stehen (mit Datum und Link)."""
    reg = region_von(region, tz)
    regel = ("Only name a code that is listed here or appears word for word in a web_search result, with its link, the date "
             "and the conditions from the deal. Codes are posted by the community and can stop working. Never guess, build "
             "or test codes. If none is listed, say so and point to the shop's newsletter or app.")
    if reg == "us":
        return {"shop": shop, "codes": [], "rule": regel,
                "hint": "No code source for the US: use web_search for '<shop> promo code' and quote only codes with a link."}
    roh = _pepper_lesen(reg, "/search", {"q": shop})
    if roh is None:
        return {"shop": shop, "error": f"{PEPPER[reg][1]} did not answer right now.", "rule": regel}
    woerter = _woerter(shop)
    jetzt = time.time()
    codes = []
    for d in roh:
        if d["expired"] or not d.get("code") or jetzt - d["posted_ts"] > 60 * 86400:
            continue
        if not (_passt_titel(d, woerter) or any(w in _norm(d.get("merchant") or "") for w in woerter)):
            continue
        codes.append({"code": d["code"], "deal": d["title"], "shop": d.get("merchant"), "posted": _tag(d["posted_ts"]),
                      "ends": _tag(d["ends_ts"]) if d.get("ends_ts") else None, "link": d["link"]})
    return {"shop": shop, "region": reg, "source": PEPPER[reg][1], "codes": codes[:8], "rule": regel,
            "note": None if codes else "No active code posted for this shop right now."}


# ── Alarme ───────────────────────────────────────────────────────────────

def alarm(adr, action="add", kind="feed", query=None, brand=None, category=None, max_price=None, target_price=None,
          url=None, alert_id=None, region=None, tz=None, min_temp=None):
    import motor
    z = motor.zustand(adr)
    liste = z.setdefault("deals", [])
    action = (action or "add").lower()
    if action == "list":
        return {"ok": True, "alerts": [{k: v for k, v in a.items() if k != "sent" and v not in (None, "")} for a in liste]}
    if action == "remove":
        vorher = len(liste)
        z["deals"] = [a for a in liste if alert_id not in (a["id"], "all") and not (query and _norm(query) in _norm(a.get("q")))]
        motor._zustand_speichern(adr, z)
        return {"ok": True, "removed": vorher - len(z["deals"])}
    if len(liste) >= int(_f("DEALS_MAX_ALERTS", 15)):
        return {"ok": False, "error": "You have the maximum number of deal alerts. Remove one first."}
    q = " ".join(x for x in (brand, query) if x).strip()
    if not q:
        return {"ok": False, "error": "Tell me which product to watch (name or model)."}
    kind = "below" if (kind == "below" or target_price) else "feed"
    if kind == "below" and not target_price:
        return {"ok": False, "error": "Tell me the price to watch for (target_price)."}
    reg = region_von(region, tz)
    a = {"id": secrets.token_hex(3), "kind": kind, "q": q, "region": reg, "max": max_price, "below": target_price,
         "min_temp": min_temp, "created": int(time.time()), "sent": {}}
    # was es jetzt schon gibt, sieht der Nutzer in der Antwort; gemeldet werden danach nur neue Deals
    jetzt_schon = []
    roh = _lesen(reg, q) or []
    for d in _aktiv(roh, _woerter(q), target_price if kind == "below" else max_price, None, tage=120):
        a["sent"][d["id"]] = d["price"]
        jetzt_schon.append(_karte(d))
    liste.append(a)
    motor._zustand_speichern(adr, z)
    quelle = "Slickdeals" if reg == "us" else PEPPER[reg][1]
    return {"ok": True, "alert": {k: v for k, v in a.items() if v not in (None, "", {}) and k != "sent"},
            "already_active_now": jetzt_schon[:3],
            "note": (f"Checked every 30 minutes on {quelle}. A message comes for every NEW matching deal"
                     + (f" at or under {_geld(target_price, PEPPER.get(reg, ('', '', 'USD'))[2])}" if kind == "below" else "")
                     + ". Reading is free, nothing is taken from the balance.")}


def _zeile(k, art):
    kopf = {"feed": "🏷️ New deal", "below": "🎯 Your price target is hit"}[art]
    z = [kopf, k["title"]]
    p = k["price"] + (f" at {k['shop']}" if k.get("shop") else "")
    if k.get("poster_compare_price"):
        p += f" (poster compares with {k['poster_compare_price']})"
    z.append(p)
    extra = []
    if k.get("community_temperature") is not None:
        extra.append(f"community {k['community_temperature']}°")
    if k.get("code_as_posted"):
        extra.append(f"code: {k['code_as_posted']}")
    if k.get("with_contract"):
        extra.append("with a contract")
    if k.get("free_shipping"):
        extra.append("free shipping")
    if extra:
        z.append(" · ".join(extra))
    z.append(f"via {k['source']}: {k['link']}")
    return "\n".join(z)


def runde():
    """Aus motor.tick. Jeder Alarm sieht hoechstens alle DEALS_POLL_SEC nach. Gleiche Suchen teilen sich einen Abruf."""
    import motor
    nutzer = []
    for adr in motor._nutzer():
        z = motor.zustand(adr)
        if z.get("deals") and motor._schalter("deals", adr):
            nutzer.append(adr)
    if not nutzer:
        return 0
    jetzt = time.time()
    tag = time.strftime("%Y%m%d", time.gmtime())
    grenze = int(_f("DEALS_MAX_DAY", 25))
    gesendet = 0
    for adr in nutzer:
        if not motor._darf(adr):
            continue
        z = motor.zustand(adr)
        zahl = z["prefs"].get("deals_n") if z["prefs"].get("deals_day") == tag else 0
        geaendert = False
        for a in z.get("deals") or []:
            if zahl >= grenze:
                break
            if not a.get("q") or jetzt - a.get("checked", 0) < _f("DEALS_POLL_SEC", 1800):
                continue
            reg = a.get("region") or "de"
            roh = _lesen(reg, a["q"])
            if roh is None:
                continue
            a["checked"] = int(jetzt)
            geaendert = True
            sent = a.setdefault("sent", {})
            grenze_preis = a.get("below") if a["kind"] == "below" else a.get("max")
            for d in _aktiv(roh, _woerter(a["q"]), grenze_preis, None, tage=3):
                if d["id"] in sent or d["posted_ts"] < a.get("created", 0) - 3600:
                    sent.setdefault(d["id"], d["price"])
                    continue
                if a.get("min_temp") is not None and (d["temperature"] or 0) < a["min_temp"]:
                    continue
                motor.senden(adr, _zeile(_karte(d), a["kind"]))
                sent[d["id"]] = d["price"]
                zahl += 1
                gesendet += 1
                if zahl >= grenze:
                    break
            if len(sent) > 400:
                a["sent"] = dict(list(sent.items())[-300:])
        if zahl or geaendert:
            z["prefs"]["deals_day"], z["prefs"]["deals_n"] = tag, zahl
            motor._zustand_speichern(adr, z)
    return gesendet
