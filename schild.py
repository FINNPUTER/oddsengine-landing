"""
schild.py: Group Shield von Your Engine, seit 2.51. Schutz vor Scammern in Telegram-Gruppen.

Laeuft auf jeder Gruppennachricht (Bot braucht Privacy aus oder Adminrechte), kostenlos, ohne KI:
  1. Fake-Admin: Absender ist kein Admin, sein Name oder @Name sieht aber aus wie der eines Admins
     ("Ⅾev_Team" statt "Dev_Team", "admin" angehaengt, ein Zeichen anders) -> Warnung
  2. Fake-CA: die Gruppe hat ihre offizielle CA gesetzt (/setca), jemand postet einen Coin mit
     demselben Ticker und anderer Adresse -> Warnung mit der richtigen CA
  3. Scam-Muster: Seed-Phrase oder Private-Key-Anfragen, "DM me for support", Claim-, Airdrop-,
     Wallet-Sync-Links, Domains, die der offiziellen Seite aehneln (/setsite) -> Warnung
Ist der Bot Admin und hat die Gruppe /shield delete gesetzt, loescht er klare Scam-Nachrichten.

Befehle (nur Admins der Gruppe): /setca <CA>, /setsite <domain>, /shield on|off|delete, /shield
"""
import json
import re
import time
import unicodedata

import storage
from logger import log

_ADMINS = {}
LINK = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(/\S*)?", re.I)
SEED = re.compile(r"(?i)\b(seed\s*phrase|recovery\s*phrase|secret\s*phrase|private\s*key|12\s*words|24\s*words|mnemonic)\b")
FRAGE = re.compile(r"(?i)\b(send|share|enter|type|give|paste|validate|verify|import)\b")
DM = re.compile(r"(?i)\b(dm\s*me|inbox\s*me|message\s*me\s*privately|contact\s*(our\s*)?support\s*(on|via|at)|pm\s*me)\b")
KOEDER = re.compile(r"(?i)\b(claim|airdrop|wallet\s*-?\s*connect|connect\s*your\s*wallet|sync\s*wallet|rectify|validate\s*wallet|"
                    r"migration|presale\s*bonus|free\s*mint|reward\s*portal|unlock\s*tokens)\b")
SICHER = {"t.me", "x.com", "twitter.com", "dexscreener.com", "solscan.io", "etherscan.io", "basescan.org", "bscscan.com",
          "arbiscan.io", "jup.ag", "raydium.io", "pump.fun", "torch.market", "torchpad.fun", "app.torchpad.fun",
          "robinhoodchain.blockscout.com", "oddsengine.live", "engine.oddsengine.live", "youtube.com", "youtu.be",
          "coingecko.com", "coinmarketcap.com", "birdeye.so", "geckoterminal.com", "github.com"}


def _cfg(cid):
    try:
        return json.loads(storage.kv_get(f"grp_cfg_{cid}") or "{}") or {}
    except Exception:
        return {}


def _cfg_setzen(cid, c):
    storage.kv_set(f"grp_cfg_{cid}", json.dumps(c))


def normal(s):
    """Fuer den Namensvergleich: Unicode-Tricks weg, nur Buchstaben und Ziffern, klein."""
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    ersatz = {"0": "o", "1": "l", "3": "e", "5": "s", "|": "l", "і": "i", "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x"}
    s = "".join(ersatz.get(ch, ch) for ch in s)
    s = re.sub(r"(official|admin|support|team|mod|dev|ceo|real)", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _abstand(a, b):
    if abs(len(a) - len(b)) > 2:
        return 9
    v = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        n = [i]
        for j, cb in enumerate(b, 1):
            n.append(min(v[j] + 1, n[j - 1] + 1, v[j - 1] + (ca != cb)))
        v = n
    return v[-1]


def admins(cid):
    c = _ADMINS.get(cid)
    if c and time.time() - c[0] < 3600:
        return c[1]
    import agent
    out = []
    try:
        r = agent._tg("getChatAdministrators", chat_id=cid) or {}
        for m in r.get("result") or []:
            u = m.get("user") or {}
            out.append({"id": u.get("id"), "name": " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x),
                        "user": u.get("username") or "", "bot": u.get("is_bot")})
    except Exception as e:
        log.debug(f"schild admins: {e}")
    _ADMINS[cid] = (time.time(), out)
    return out


def fake_admin(cid, von):
    """-> Name des Admins, dem der Absender aehnelt, oder None."""
    if not von or von.get("is_bot"):
        return None
    ad = admins(cid)
    if any(a["id"] == von.get("id") for a in ad):
        return None
    ich = [normal(" ".join(x for x in (von.get("first_name"), von.get("last_name")) if x)), normal(von.get("username"))]
    for a in ad:
        if a.get("bot"):
            continue
        for ziel in (normal(a["name"]), normal(a["user"])):
            if len(ziel) < 4:
                continue
            for x in ich:
                if x and (x == ziel or (len(ziel) >= 6 and _abstand(x, ziel) <= 1)):
                    return a["user"] and "@" + a["user"] or a["name"]
    return None


def _domain_aehnlich(d, offiziell):
    if not offiziell:
        return False
    a, b = d.lower().split(".")[-2], offiziell.lower().split(".")[-2] if "." in offiziell else offiziell.lower()
    return a != b and (_abstand(a, b) <= 2 or b in a) and d.lower() != offiziell.lower()


def scam_text(text, offiziell=None):
    """-> (stufe, grund) mit stufe 'hart' (loeschbar) oder 'weich', oder (None, None)."""
    t = str(text or "")
    if SEED.search(t) and FRAGE.search(t):
        return "hart", "asks for a seed phrase or private key"
    for m in LINK.finditer(t):
        d = m.group(1).lower()
        haupt = ".".join(d.split(".")[-2:])
        if d in SICHER or haupt in SICHER:
            continue
        if offiziell and (d == offiziell.lower() or haupt == ".".join(offiziell.lower().split(".")[-2:])):
            continue
        if _domain_aehnlich(haupt, offiziell):
            return "hart", f"links to {d}, a look-alike of the official {offiziell}"
        if KOEDER.search(t) or KOEDER.search(d):
            return "hart", f"links to {d} with claim, airdrop or wallet-connect bait"
    if DM.search(t) and re.search(r"(?i)\b(support|help|issue|problem|refund|admin)\b", t):
        return "weich", "asks people to DM for support (real admins never DM first)"
    return None, None


def befehl(msg):
    """Gruppenbefehle. True, wenn es ein Schild-Befehl war."""
    import agent
    text = (msg.get("text") or "").strip()
    cmd = text.split(" ")[0].split("@")[0].lower()
    if cmd not in ("/setca", "/setsite", "/shield"):
        return False
    cid = (msg.get("chat") or {}).get("id")
    von = msg.get("from") or {}
    thema = msg.get("message_thread_id")
    extra = {"message_thread_id": thema} if thema else {}
    if not any(a["id"] == von.get("id") for a in admins(cid)):
        agent._tg("sendMessage", chat_id=cid, text="Only group admins can change the shield.", **extra)
        return True
    c = _cfg(cid)
    arg = text.split(" ", 1)[1].strip() if " " in text else ""
    if cmd == "/setca":
        import scan
        r = scan.analyse(arg, deep=False) if arg else {}
        if not r.get("ok"):
            agent._tg("sendMessage", chat_id=cid, text="Send it like /setca <contract address>.", **extra)
            return True
        c.update(ca=r["address"], chain=r["chain"], symbol=(r.get("symbol") or "").upper())
        msg_t = f"Official CA set: ${c['symbol']} {r['address']}\nAnyone posting another ${c['symbol']} contract here gets flagged."
    elif cmd == "/setsite":
        d = re.sub(r"^https?://", "", arg).split("/")[0].lower()
        if "." not in d:
            agent._tg("sendMessage", chat_id=cid, text="Send it like /setsite yourproject.com", **extra)
            return True
        c["site"] = d
        msg_t = f"Official site set: {d}\nLook-alike domains posted here get flagged."
    else:
        if arg in ("on", "off", "delete"):
            c["shield"] = arg
        st = c.get("shield", "on")
        msg_t = (f"Group Shield is {'on, and deletes clear scams' if st == 'delete' else st}.\n"
                 f"Official CA: {('$' + c['symbol'] + ' ' + c['ca']) if c.get('ca') else 'not set (/setca)'}\n"
                 f"Official site: {c.get('site') or 'not set (/setsite)'}\n"
                 "It flags fake admins, fake contracts, seed phrase requests and drainer links.")
    _cfg_setzen(cid, c)
    agent._tg("sendMessage", chat_id=cid, text=msg_t, **extra)
    return True


def pruefen(msg):
    """Jede Gruppennachricht. True, wenn gewarnt wurde."""
    import agent
    cid = (msg.get("chat") or {}).get("id")
    c = _cfg(cid)
    if c.get("shield") == "off":
        return False
    von = msg.get("from") or {}
    text = msg.get("text") or msg.get("caption") or ""
    thema = msg.get("message_thread_id")
    warnung, hart = None, False
    fa = fake_admin(cid, von)
    if fa:
        warnung = f"⚠️ Careful: this account is NOT an admin. It only looks like {fa}. Admins here never DM you first."
    if not warnung:
        stufe, grund = scam_text(text, c.get("site"))
        if stufe:
            warnung, hart = f"🛑 Scam warning: this message {grund}. Never share your seed phrase, never connect your wallet to a link from chat.", stufe == "hart"
    if not warnung and c.get("ca"):
        try:
            import motor
            import scan
            q = motor._nur_ca(text)
            if q and ":" not in q and q.lower() != c["ca"].lower() and len(q) > 20:
                r = scan.analyse(q, deep=False)
                if r.get("ok") and (r.get("symbol") or "").upper() == c.get("symbol") and r["address"].lower() != c["ca"].lower():
                    warnung = (f"⚠️ This is NOT the official ${c['symbol']} contract.\nOfficial: {c['ca']}")
        except Exception as e:
            log.debug(f"schild ca: {e}")
    if not warnung:
        return False
    p = {"chat_id": cid, "text": warnung, "reply_parameters": {"message_id": msg.get("message_id"), "allow_sending_without_reply": True}}
    if thema:
        p["message_thread_id"] = thema
    agent._tg("sendMessage", **p)
    if hart and c.get("shield") == "delete":
        agent._tg("deleteMessage", chat_id=cid, message_id=msg.get("message_id"))
    try:
        import aktiv
        aktiv.merken(f"group:{cid}", "shield", warnung[:120])
    except Exception:
        pass
    return True
