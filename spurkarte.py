"""
spurkarte.py: Die Geldspur als Bild (PNG) fuer Telegram, seit 2.63.1.

Aus dem Ergebnis von verfolger.vorwaerts (oder rueckwaerts) wird eine Karte: Spalten = Hops, Kaesten = Wallets,
Boersen, Bridges, Mixer; Pfeile mit Betraegen. Gruen: Wallet haelt noch, Gelb: Boerse/Bridge, Rot: Start oder Mixer,
Blau-Schild: vom Herausgeber gesperrte Stablecoins. Unten Zeitpunkt, Chains und Quelle.

  karte(ergebnis, titel=None, rueck=False) -> PNG-Bytes oder None (ohne Pillow)
"""
import io
import os

from logger import log

W, H = 1920, 1080
BG = (4, 10, 7)
G = (62, 240, 140)
WS = (240, 245, 242)
GR = (140, 160, 150)
AM = (250, 190, 60)
RT = (255, 96, 96)
BL = (120, 170, 255)
LI = (9, 20, 14)
_ORDNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def _font(name, size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(os.path.join(_ORDNER, name), size)
    except Exception:
        try:
            return ImageFont.load_default(size)
        except Exception:
            return ImageFont.load_default()


def _kurz(a):
    a = str(a or "")
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def _usd(v):
    v = float(v or 0)
    if v >= 1e9:
        return f"${v / 1e9:,.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:,.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:,.0f}K"
    return f"${v:,.0f}"


def _farbe(kind, start=False):
    if start:
        return RT
    return {"exchange": AM, "bridge": BL, "mixer": RT, "burn": GR, "service": AM, "contract": GR, "lending": AM}.get(kind, G)


def _kind_text(kind, name):
    if name:
        return str(name)[:30]
    return {"exchange": "EXCHANGE", "bridge": "BRIDGE", "mixer": "MIXER", "burn": "BURN", "contract": "CONTRACT",
            "lending": "LENDING", "service": "SERVICE", "wallet": "WALLET"}.get(kind, str(kind or "WALLET").upper())


def _passend(d, text, font, breite):
    """Kuerzt Text mit … bis er in die Breite passt."""
    text = str(text or "")
    if d.textlength(text, font=font) <= breite:
        return text
    while text and d.textlength(text + "…", font=font) > breite:
        text = text[:-1]
    return text.rstrip(" ·") + "…"


def karte(erg, titel=None, rueck=False):
    try:
        from PIL import Image, ImageDraw
    except Exception:
        log.warning("spurkarte: Pillow missing, no map image")
        return None
    if not erg or erg.get("error"):
        return None
    kanten = [k for k in (erg.get("edges") or erg.get("flows") or []) if (k.get("to_kind") or k.get("from_kind")) not in ("dex",)]
    start = erg.get("start") or erg.get("wallet")
    halten = {(e.get("chain"), e.get("wallet")): e for e in erg.get("endpoints") or [] if e.get("holds_usd") is not None}
    # Knoten: (chain, adresse) -> info
    knoten = {}

    def knoten_dazu(ch, a, hop, kind, name, wert):
        k = (ch, a)
        n = knoten.get(k)
        if not n:
            knoten[k] = {"chain": ch, "adr": a, "hop": hop, "kind": kind, "name": name, "wert": wert or 0}
        else:
            n["hop"] = min(n["hop"], hop)
            n["wert"] = max(n["wert"], wert or 0)
    for ch in {k["chain"] for k in kanten} or {"ethereum"}:
        knoten_dazu(ch, start, 0, "wallet", None, 0)
    for k in kanten:
        if rueck:
            knoten_dazu(k["chain"], k["from"], k["hop"] + 1, k.get("from_kind"), k.get("from_name"), k.get("usd"))
        else:
            knoten_dazu(k["chain"], k["to"], k["hop"] + 1, k.get("to_kind"), k.get("to_name"), k.get("usd"))
    # Spalten, groesste zuerst, hoechstens 6 je Spalte
    spalten = {}
    for k, n in knoten.items():
        spalten.setdefault(min(n["hop"], 5), []).append(n)
    weg = 0
    for s in spalten:
        spalten[s].sort(key=lambda n: -(halten.get((n["chain"], n["adr"]), {}).get("holds_usd") or n["wert"]))
        weg += max(0, len(spalten[s]) - 6)
        spalten[s] = spalten[s][:6]
    # leere Spalten zusammenschieben
    spalten = {i: spalten[k] for i, k in enumerate(sorted(spalten))}
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    for x in range(0, W, 60):
        d.line((x, 0, x, H), fill=LI)
    for y in range(0, H, 60):
        d.line((0, y, W, y), fill=LI)
    d.text((60, 40), titel or ("WHERE THE MONEY CAME FROM" if rueck else "WHERE THE MONEY WENT"), font=_font("Inter-700.ttf", 46), fill=WS)
    d.text((60, 100), f"{_kurz(start)} · {', '.join(erg.get('chains') or [])} · as of {erg.get('as_of', '')}",
           font=_font("Inter-700.ttf", 24), fill=GR)
    ncol = max(spalten) + 1 if spalten else 1
    bw, bh = (300 if ncol <= 5 else 270), 118
    schritt = min(520, (W - 120 - bw) / max(1, ncol - 1))
    links = (W - (bw + schritt * (ncol - 1))) / 2
    xs = [links + i * schritt for i in range(ncol)]
    pos = {}
    for s, liste in spalten.items():
        n = len(liste)
        gap = (H - 300 - n * bh) / max(1, n - 1) if n > 1 else 0
        gap = min(gap, 40)
        hoehe = n * bh + (n - 1) * gap
        y0 = 160 + (H - 300 - hoehe) / 2
        for i, nd in enumerate(liste):
            x, y = xs[s] if not rueck else xs[ncol - 1 - s], y0 + i * (bh + gap)
            pos[(nd["chain"], nd["adr"])] = (x, y)
    # Pfeile zuerst
    for k in kanten:
        a, b = ((k["chain"], k["to"]), (k["chain"], k["from"])) if rueck else ((k["chain"], k["from"]), (k["chain"], k["to"]))
        if a not in pos or b not in pos:
            continue
        (x1, y1), (x2, y2) = pos[a], pos[b]
        col = _farbe(k.get("to_kind") if not rueck else k.get("from_kind"))
        if abs(x1 - x2) < 1:
            # gleiche Spalte (tiefe Hops zusammengefasst): Klammer an der rechten Kante
            xr = x1 + bw + 16
            d.line(((x1 + bw, y1 + bh / 2), (xr, y1 + bh / 2), (xr, y2 + bh / 2), (x2 + bw, y2 + bh / 2)), fill=col, width=3)
            d.ellipse((x2 + bw - 6, y2 + bh / 2 - 6, x2 + bw + 6, y2 + bh / 2 + 6), fill=col)
            continue
        p1 = (x1 + bw, y1 + bh / 2) if x2 > x1 else (x1, y1 + bh / 2)
        p2 = (x2, y2 + bh / 2) if x2 > x1 else (x2 + bw, y2 + bh / 2)
        d.line((p1, p2), fill=col, width=3)
        d.ellipse((p2[0] - 6, p2[1] - 6, p2[0] + 6, p2[1] + 6), fill=col)
    for (ch, a), (x, y) in pos.items():
        nd = knoten[(ch, a)]
        ist_start = a == start
        col = _farbe(nd["kind"], ist_start)
        d.rounded_rectangle((x, y, x + bw, y + bh), 14, fill=(10, 22, 16), outline=col, width=3)
        oben = "START" if ist_start else _kind_text(nd["kind"], nd.get("name"))
        f18, f24, f19 = _font("JBMono-Bold.ttf", 18), _font("Inter-700.ttf", 24), _font("Inter-700.ttf", 19)
        h = halten.get((ch, a))
        abz = ("FROZEN " + "+".join(h["frozen_by_issuer"])) if h and h.get("frozen_by_issuer") else ""
        platz = bw - 32
        if abz:
            f14 = _font("JBMono-Bold.ttf", 14)
            bb = d.textlength(abz, font=f14)
            d.rounded_rectangle((x + bw - 24 - bb, y + 10, x + bw - 8, y + 34), 7, fill=(20, 34, 60), outline=BL, width=2)
            d.text((x + bw - 16 - bb, y + 14), abz, font=f14, fill=BL)
            platz -= bb + 28
        if platz >= d.textlength(oben[:6], font=f18):
            d.text((x + 16, y + 12), _passend(d, oben, f18, platz), font=f18, fill=col)
        d.text((x + 16, y + 40), f"{_kurz(a)}", font=f24, fill=WS)
        if h:
            unten = f"holds {_usd(h['holds_usd'])}"
        else:
            unten = f"{'sent' if rueck else 'got'} {_usd(nd['wert'])}" if not ist_start else ""
        d.text((x + 16, y + 76), _passend(d, (unten + f" · {ch}") if unten else ch, f19, bw - 32), font=f19, fill=GR)
    fuss = "On-chain data only, no claims about who is behind a wallet. Every amount has a transaction behind it."
    if weg:
        fuss = f"{weg} smaller wallet(s) not shown. " + fuss
    d.text((60, H - 70), fuss, font=_font("Inter-700.ttf", 22), fill=GR)
    d.text((W - 60, H - 70), "Your Engine · t.me/OddsEnginePublicBot", font=_font("Inter-700.ttf", 22), fill=G, anchor="ra")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()
