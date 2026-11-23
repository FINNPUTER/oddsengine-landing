"""
rotkarte.py: Chart-Bild fuer "ist gelaufen" Meldungen der Meta Rotation (seit 2.82.51).

karte(ticker, kerzen, call_ts, mcap_call, faktor, mcap_jetzt) -> PNG (bytes) oder None
  kerzen: [[ts, o, h, l, c, vol], ...] in USD-Preis, faktor rechnet Preis in Market Cap um (mcap_call / preis_call).
  Zeitachse relativ zum Call ("call", "+40m", "-2h"), keine Uhrzeiten im Bild.
"""
import io
import os

W, H = 1280, 720
BG = (5, 9, 7)
PANEL = (10, 17, 13)
GRID = (26, 38, 31)
G = (34, 240, 138)
G2 = (125, 255, 189)
RT = (255, 84, 104)
WS = (236, 245, 239)
GR = (140, 160, 150)
DIM = (80, 98, 88)
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


def logo(d, x_rechts, y, groesse=22, farbe=None):
    """OddsEngine-Logo (gruenes App-Icon aus fonts/logo-green.png) plus Schriftzug, rechtsbuendig an x_rechts.
    Fehlt die Datei, wird der Ring gezeichnet."""
    from PIL import Image
    f = _font("unb900.ttf", groesse)
    w = d.textlength("OddsEngine", font=f)
    s = int(groesse * 1.9)
    x_icon = int(x_rechts - w - 12 - s)
    y_icon = int(y + groesse * 0.55 - s / 2)
    try:
        ic = Image.open(os.path.join(_ORDNER, "logo-green.png")).convert("RGBA").resize((s, s), Image.LANCZOS)
        maske = Image.new("L", (s, s), 0)
        from PIL import ImageDraw
        ImageDraw.Draw(maske).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.24), fill=255)
        d._image.paste(ic, (x_icon, y_icon), maske)
    except Exception:
        r = groesse * 0.62
        cx, cy = x_rechts - w - 14 - r, y + groesse * 0.62
        d.arc((cx - r, cy - r, cx + r, cy + r), -36, 307, fill=farbe or G, width=max(4, int(groesse * 0.3)))
    d.text((x_rechts, y), "OddsEngine", font=f, fill=WS, anchor="ra")


def geld(v):
    v = float(v or 0)
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K" if v < 1e5 else f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def dauer(s):
    s = max(0, int(s))
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < 2 * 86400:
        h, m = s // 3600, (s % 3600) // 60
        return f"{h}h" if not m or h >= 10 else f"{h}h {m}m"
    return f"{s // 86400}d"


def _pill(d, xy, text, font, fill, fg, anchor="lm"):
    x, y = xy
    w = d.textlength(text, font=font)
    h = font.size + 14
    if anchor == "rm":
        x -= w + 28
    d.rounded_rectangle((x, y - h / 2, x + w + 28, y + h / 2), radius=h / 2, fill=fill)
    d.text((x + 14, y), text, font=font, fill=fg, anchor="lm")
    return x, x + w + 28


def karte(ticker, kerzen, call_ts, mcap_call, faktor, mcap_jetzt, untertitel="market cap since the OddsEngine call",
          marke="Meta Rotation", wort="now"):
    from PIL import Image, ImageDraw
    k = [x for x in kerzen if x and len(x) > 4]
    if len(k) < 2 or not faktor or not mcap_call:
        return None
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    t_b, t_k, t_m = _font("unb900.ttf", 34), _font("ins600.ttf", 20), _font("jbm700.ttf", 17)
    d.text((48, 40), f"${ticker}", font=t_b, fill=WS)
    d.text((50, 92), untertitel, font=t_k, fill=GR)
    logo(d, W - 48, 42)
    d.text((W - 48, 80), marke, font=t_k, fill=GR, anchor="ra")
    x0, x1, y0, y1 = 120, W - 48, 150, H - 80
    d.rounded_rectangle((x0 - 70, y0 - 20, x1 + 20, y1 + 50), radius=24, fill=PANEL)
    werte = [float(v) * faktor for x in k for v in (x[2], x[3]) if v is not None] + [mcap_jetzt or 0, mcap_call]
    lo, hi = 0.0, max(werte) * 1.12
    if min(werte) > hi * 0.35:
        lo = min(werte) * 0.85
    ty = lambda v: y1 - (float(v) - lo) / (hi - lo) * (y1 - y0) if hi > lo else y1
    # Raster und Achse
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = ty(v)
        d.line((x0, y, x1, y), fill=GRID, width=1)
        d.text((x0 - 14, y), geld(v), font=t_m, fill=DIM, anchor="rm")
    ts0, ts1 = int(k[0][0]), int(k[-1][0])
    sek = (int(k[1][0]) - int(k[0][0])) if len(k) > 1 else 300
    tx = lambda t: x0 + (t - ts0) / max(1, ts1 + sek - ts0) * (x1 - x0)
    breite = max(3, min(46, (x1 - x0) / len(k) * 0.62))
    # Bereich nach dem Call leicht hinterlegt
    cx = tx(call_ts)
    d.rectangle((cx, y0 - 4, x1, y1), fill=(9, 26, 18))
    for x in k:
        t, o, h, l, c = int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])
        mx = tx(t) + (x1 - x0) / len(k) / 2
        farbe = G if c >= o else RT
        d.line((mx, ty(h * faktor), mx, ty(l * faktor)), fill=farbe, width=2)
        oben, unten = ty(max(o, c) * faktor), ty(min(o, c) * faktor)
        d.rectangle((mx - breite / 2, oben, mx + breite / 2, max(unten, oben + 2)), fill=farbe)
    # Call-Linie und Punkt
    d.line((cx, y0 - 4, cx, y1), fill=G2, width=2)
    cy = ty(mcap_call)
    d.ellipse((cx - 7, cy - 7, cx + 7, cy + 7), fill=G2, outline=BG, width=3)
    lx = cx + 14 if cx < x0 + (x1 - x0) * 0.55 else cx - 14
    _pill(d, (lx, min(y1 - 24, cy + 40)), f"OddsEngine call  ·  {geld(mcap_call)}", t_m, (18, 40, 29), G2,
          "lm" if lx > cx else "rm")
    # Jetzt-Linie
    if mcap_jetzt:
        jy = ty(mcap_jetzt)
        for xx in range(int(x0), int(x1), 14):
            d.line((xx, jy, xx + 7, jy), fill=G, width=2)
        x_mult = mcap_jetzt / mcap_call
        _pill(d, (x1 - (x1 - x0) * 0.14, max(y0 + 12, jy - 26)), f"{x_mult:.1f}x {wort}  ·  {geld(mcap_jetzt)}", t_m, G, (2, 19, 10), "rm")
    # Zeitachse relativ zum Call
    for i in range(5):
        t = ts0 + (ts1 + sek - ts0) * i / 4
        dt = t - call_ts
        lab = "call" if abs(dt) < sek else (("+" if dt > 0 else "-") + dauer(abs(dt)))
        d.text((tx(t), y1 + 26), lab, font=t_m, fill=DIM, anchor="mm")
    d.text((48, H - 22), "Past results are no guarantee of future results.", font=_font("ins600.ttf", 15), fill=DIM, anchor="ls")
    d.text((W - 48, H - 22), "t.me/OddsEnginePublicBot", font=t_m, fill=GR, anchor="rs")
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()
