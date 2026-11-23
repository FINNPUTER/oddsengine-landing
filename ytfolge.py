"""
ytfolge.py: Your Engine folgt YouTube-Kanaelen und fasst jedes neue Video sofort in 2 bis 3 Saetzen zusammen (seit 2.51).

"Folge dem Kanal und sag mir, was im neuen Video gesagt wird": Your Engine liest den oeffentlichen RSS-Feed des Kanals
(youtube.com/feeds/videos.xml?channel_id=UC...), kostenlos und ohne Schluessel. Ein Kanal wird fuer alle Nutzer zusammen
nur einmal gelesen. Bei einem neuen Video holt es die Untertitel (motor.youtube_video) und das Modell schreibt
die Kurzfassung in der Sprache des Nutzers. Gibt es keine Untertitel (oder sperrt YouTube den Server), wird aus Titel
und Videobeschreibung zusammengefasst und das klar gesagt.

Kosten: nur das Modell fuer die Kurzfassung, vom Guthaben des Nutzers (wie jede Anfrage). Lesen des Feeds kostet nichts.
Ohne Guthaben pausiert es wie alles andere im Hintergrund.

Schalter:
  YT_FOLLOW_POLL_SEC   wie oft je Kanal nachgesehen wird (Standard 300)
  YT_FOLLOW_MAX        Kanaele je Nutzer (Standard 10)
  YT_FOLLOW_MAX_DAY    zugestellte Videos je Nutzer und Tag (Standard 30)
  YT_PROXY             wie bei youtube_video, falls YouTube den Server sperrt
"""
import json
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET

import requests

import storage
from logger import log

FEED = "https://www.youtube.com/feeds/videos.xml"
_UC = re.compile(r"(UC[A-Za-z0-9_-]{22})")
_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
       "media": "http://search.yahoo.com/mrss/"}
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept-Language": "en-US,en;q=0.9"}


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
    storage.kv_set(k, json.dumps(v))


def _hol(url, **kw):
    """Erst direkt (kostet nichts), nur bei Sperre ueber den Proxy. Seit 2.52.2: der Feed wird alle paar Minuten gelesen,
    ueber den Proxy wuerde das das bezahlte Datenvolumen aufbrauchen."""
    # Zustimmungsseite in der EU umgehen
    std = dict(headers=_UA, cookies={"CONSENT": "YES+1", "SOCS": "CAI"}, timeout=15)
    try:
        r = requests.get(url, **std, **kw)
        if r.status_code not in (403, 429):
            return r
    except Exception:
        r = None
    proxy = os.getenv("YT_PROXY", "").strip()
    ws_user, ws_pass = os.getenv("WEBSHARE_PROXY_USER", "").strip(), os.getenv("WEBSHARE_PROXY_PASS", "").strip()
    if ws_user and ws_pass:
        proxy = f"http://{ws_user}-rotate:{ws_pass}@p.webshare.io:80"
    if not proxy:
        if r is None:
            raise RuntimeError("youtube not reachable")
        return r
    return requests.get(url, proxies={"http": proxy, "https": proxy}, **std, **kw)


def kanal_aus(text):
    """@name, Kanal-Link, Video-Link oder UC-Id -> ("uc", id) / ("handle", name) / ("video", id) / None"""
    t = str(text or "").strip()
    m = re.search(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})", t) or (re.fullmatch(r"(UC[A-Za-z0-9_-]{22})", t))
    if m:
        return ("uc", m.group(1))
    m = re.search(r"youtube\.com/@([A-Za-z0-9._-]{2,100})", t)
    if m:
        return ("handle", m.group(1))
    import motor
    m = motor._YT.search(t)
    if m:
        return ("video", m.group(1))
    m = re.search(r"youtube\.com/(?:c|user)/([A-Za-z0-9._-]{2,100})", t)
    if m:
        return ("path", m.group(0).split("youtube.com/")[1])
    if re.fullmatch(r"@?[A-Za-z0-9._-]{2,100}", t):
        return ("handle", t.lstrip("@"))
    return None


def _feed(uc):
    """-> {"title", "videos": [{"id","title","published","description"}]} neueste zuerst, oder {"error"}"""
    try:
        r = _hol(FEED, params={"channel_id": uc})
        if r.status_code == 404:
            return {"error": "not found"}
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        return {"error": str(e)[:160]}
    out = {"title": (root.findtext("a:title", default="", namespaces=_NS) or "").strip(), "videos": []}
    for e in root.findall("a:entry", _NS):
        vid = e.findtext("yt:videoId", default="", namespaces=_NS)
        if not vid:
            continue
        link = (e.find("a:link", _NS).get("href") if e.find("a:link", _NS) is not None else "") or ""
        out["videos"].append({"id": vid, "title": (e.findtext("a:title", default="", namespaces=_NS) or "").strip(),
                              "published": e.findtext("a:published", default="", namespaces=_NS),
                              "short": "/shorts/" in link,
                              "description": (e.findtext("media:group/media:description", default="", namespaces=_NS) or "").strip()})
    return out


def aufloesen(wer):
    """Kanal-Id und Name finden. -> {"uc","name"} oder {"error"}"""
    k = kanal_aus(wer)
    if not k:
        return {"error": f"'{wer}' is not a YouTube channel. Send the channel link, @name or a video of the channel."}
    art, wert = k
    c = _j("yt_res_" + f"{art}:{wert}".lower(), None)
    if c and c.get("uc"):
        return c
    uc = wert if art == "uc" else None
    if art == "video":
        try:
            o = _hol("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={wert}", "format": "json"}).json()
            seite = o.get("author_url") or ""
        except Exception:
            seite = ""
        if not seite:
            return {"error": "I could not find the channel of that video."}
        m = _UC.search(seite)
        uc = m.group(1) if m else None
        if not uc:
            art, wert = "url", seite
    if not uc:
        url = wert if art == "url" else (f"https://www.youtube.com/@{wert}" if art == "handle" else f"https://www.youtube.com/{wert}")
        try:
            html = _hol(url).text
        except Exception as e:
            return {"error": f"YouTube did not answer ({str(e)[:60]})."}
        m = (re.search(r'"channelId":"(UC[A-Za-z0-9_-]{22})"', html) or re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"', html)
             or re.search(r'"externalId":"(UC[A-Za-z0-9_-]{22})"', html) or re.search(r'itemprop="(?:identifier|channelId)" content="(UC[A-Za-z0-9_-]{22})"', html))
        if not m:
            return {"error": "I could not find that channel on YouTube."}
        uc = m.group(1)
    f = _feed(uc)
    if f.get("error"):
        return {"error": "I could not read that channel's videos right now."}
    out = {"uc": uc, "name": f.get("title") or uc}
    _set("yt_res_" + f"{k[0]}:{k[1]}".lower(), out)
    return out


def folgen(adr, wer, shorts=None):
    import motor
    z = motor.zustand(adr)
    liste = z.setdefault("ytfollow", [])
    r = aufloesen(wer)
    if r.get("error"):
        return {"ok": False, "error": r["error"]}
    alt = next((x for x in liste if x["uc"] == r["uc"]), None)
    if alt:
        if shorts is not None:
            alt["shorts"] = bool(shorts)
            motor._zustand_speichern(adr, z)
        return {"ok": True, "already": True, "channel": alt["name"], "shorts": alt.get("shorts", False)}
    if len(liste) >= int(_f("YT_FOLLOW_MAX", 10)):
        return {"ok": False, "error": "You follow the maximum number of YouTube channels. Remove one first."}
    liste.append({"id": secrets.token_hex(3), "uc": r["uc"], "name": r["name"], "shorts": bool(shorts)})
    motor._zustand_speichern(adr, z)
    q = _j("yt_src_" + r["uc"], None)
    if not q:
        f = _feed(r["uc"])
        # Ab jetzt: alles, was schon im Feed steht, gilt als gesehen
        _set("yt_src_" + r["uc"], {"name": r["name"], "seen": [v["id"] for v in f.get("videos") or []][:30], "checked": int(time.time())})
    letzte = None
    try:
        v = (_feed(r["uc"]).get("videos") or [None])[0]
        letzte = v and {"title": v["title"], "url": f"https://www.youtube.com/watch?v={v['id']}"}
    except Exception:
        pass
    return {"ok": True, "channel": r["name"], "latest_video": letzte,
            "note": "Every new video gets a 2 to 3 sentence summary in Telegram within a few minutes. "
                    "Only the model cost of each summary comes from the balance, reading the channel is free. "
                    "If the user wants the latest video summarized now, call youtube_video with latest_video.url."}


def entfolgen(adr, wer):
    import motor
    z = motor.zustand(adr)
    liste = z.setdefault("ytfollow", [])
    w = str(wer or "").strip().lstrip("@").lower()
    k = kanal_aus(wer)
    uc = k[1] if k and k[0] == "uc" else None
    vorher = len(liste)
    z["ytfollow"] = [x for x in liste if not (x["id"] == w or x["uc"] == uc or x["name"].lower() == w
                                              or x["name"].lower().replace(" ", "") == w.replace(" ", ""))]
    if len(z["ytfollow"]) == vorher and w:
        r = aufloesen(wer)
        if r.get("uc"):
            z["ytfollow"] = [x for x in liste if x["uc"] != r["uc"]]
    motor._zustand_speichern(adr, z)
    return {"ok": True, "removed": vorher - len(z["ytfollow"])}


def liste(adr):
    import motor
    return [{"id": x["id"], "channel": x["name"], "shorts": x.get("shorts", False)} for x in motor.zustand(adr).get("ytfollow", [])]


def kurzfassung(adr, kanal, video):
    """2 bis 3 Saetze in der Sprache des Nutzers. -> (text, aus_untertiteln) oder (None, False)"""
    import ai
    import motor
    yt = motor.youtube_video(f"https://www.youtube.com/watch?v={video['id']}")
    quelle = yt.get("transcript")
    aus_ut = bool(quelle)
    if not quelle:
        quelle = f"(No captions available. Title and description only.)\nTitle: {video.get('title')}\nDescription: {video.get('description') or ''}"
    sprache = None
    try:
        sprache = motor._sprache(adr)
    except Exception:
        pass
    anweisung = ("A YouTube channel the user follows just posted a new video. Summarize what is said in it in 2 to 3 short plain "
                 "sentences: the main point, the key claims, numbers, coins or dates. If only the title and description are given, "
                 "summarize those and say in a few words that the video had no captions. No markdown, no emojis, no dashes, no advice."
                 + (f" Write in {sprache}." if sprache else ""))
    try:
        d = ai.anfrage(adr, None, {"model": motor.FAST, "max_tokens": 220, "messages": [
            {"role": "system", "content": anweisung},
            {"role": "user", "content": f"Channel: {kanal}\nVideo: {video.get('title')}\n\n{quelle[:160000]}"}]})
        k = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return (re.sub(r"\s*[—–]\s*", ", ", k).strip() or None), aus_ut
    except Exception as e:
        log.debug(f"ytfolge kurz: {e}")
        return None, False


def runde():
    """Aus motor.tick. Liest jeden gefolgten Kanal hoechstens alle YT_FOLLOW_POLL_SEC."""
    import motor
    nutzer = []
    for a in motor._nutzer():
        z = motor.zustand(a)
        if z.get("ytfollow"):
            nutzer.append((a, z))
    if not nutzer:
        return 0
    kanaele = {x["uc"] for _, z in nutzer for x in z["ytfollow"]}
    takt = _f("YT_FOLLOW_POLL_SEC", 300)
    jetzt = time.time()
    frisch = {}
    for uc in kanaele:
        q = _j("yt_src_" + uc, None) or {"seen": None, "checked": 0}
        if jetzt - q.get("checked", 0) < takt:
            continue
        q["checked"] = int(jetzt)
        f = _feed(uc)
        if f.get("error"):
            log.warning(f"ytfolge {uc}: {f['error']}")
            _set("yt_src_" + uc, q)
            continue
        ids = [v["id"] for v in f["videos"]]
        if q.get("seen") is None:
            q["seen"] = ids[:30]
            _set("yt_src_" + uc, q)
            continue
        neu = [v for v in f["videos"] if v["id"] not in q["seen"]]
        q["seen"] = (ids + [s for s in q["seen"] if s not in ids])[:60]
        _set("yt_src_" + uc, q)
        if neu:
            # aelteste zuerst; nach einer langen Pause hoechstens 3 auf einmal
            frisch[uc] = (f.get("title"), list(reversed(neu))[-3:])
    if not frisch:
        return 0
    tag = time.strftime("%Y%m%d", time.gmtime())
    grenze = int(_f("YT_FOLLOW_MAX_DAY", 30))
    gesendet = 0
    for a, z in nutzer:
        if not motor._darf(a):
            continue
        zahl = z["prefs"].get("yt_n") if z["prefs"].get("yt_day") == tag else 0
        for x in z["ytfollow"]:
            kanal, videos = frisch.get(x["uc"]) or (None, [])
            for v in videos:
                if v.get("short") and not x.get("shorts"):
                    continue
                if zahl >= grenze:
                    break
                kurz, aus_ut = kurzfassung(a, kanal or x["name"], v)
                motor.senden(a, f"▶️ New video from {kanal or x['name']}\n{v['title']}\n\n"
                                + (f"{kurz}\n\n" if kurz else "")
                                + f"https://www.youtube.com/watch?v={v['id']}")
                zahl += 1
                gesendet += 1
        if zahl:
            z2 = motor.zustand(a)
            z2["prefs"]["yt_day"], z2["prefs"]["yt_n"] = tag, zahl
            motor._zustand_speichern(a, z2)
    return gesendet
