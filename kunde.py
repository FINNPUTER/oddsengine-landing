"""
kunde.py: Wer ist das, und was hat er gemacht? Fuer den Betreiber. Seit 2.62.

  /who @handle | 12345678 | 0xWALLET | tg:12345678     im Betreiber-Chat mit dem Bot
  Antwort auf eine weitergeleitete Nachricht mit #id... und nur /who: dieser Nutzer

Zeigt: Name, Telegram, Wallet, seit wann verknuepft, Guthaben, Einzahlungen, Gratis-Nachrichten,
die letzten Nachrichten im Gespraech (Frage und Antwort gekuerzt) und offene Support-Tickets.
"""
import json
import re
import time

import storage
from logger import log


def gast_merken(chat_id, von):
    """Name und Handle eines Gastes (ohne Wallet) merken. True beim allerersten Mal."""
    k = "gast_" + str(chat_id)
    neu = not storage.kv_get(k)
    try:
        storage.kv_set(k, json.dumps({"name": " ".join(x for x in ((von or {}).get("first_name"), (von or {}).get("last_name")) if x),
                                      "handle": (von or {}).get("username") or "", "seit": int(time.time()) if neu else
                                      (json.loads(storage.kv_get(k) or "{}").get("seit") or int(time.time()))}))
    except Exception:
        pass
    if neu:
        try:
            ids = json.loads(storage.kv_get("gast_ids") or "[]")
            ids.append(str(chat_id))
            storage.kv_set("gast_ids", json.dumps(ids[-5000:]))
        except Exception:
            pass
    return neu


def gast(chat_id):
    try:
        return json.loads(storage.kv_get("gast_" + str(chat_id)) or "null")
    except Exception:
        return None


def finden(q):
    """-> (tg_id, agent-Nutzer oder None, gast oder None)"""
    import agent
    q = str(q or "").strip()
    m = re.search(r"#id(\d+)", q)
    if m:
        q = m.group(1)
    if q.lower().startswith("tg:"):
        q = q[3:]
    if q.isdigit():
        return q, agent.laden(q), gast(q)
    h = q.lstrip("@").lower()
    w = q.lower()
    for u in agent.alle():
        if (h and str(u.get("handle") or "").lower() == h) or (w.startswith("0x") and str(u.get("wallet") or "").lower() == w) \
                or (u.get("wallet") and u.get("wallet") == q):
            return str(u["tg"]), u, gast(u["tg"])
    try:
        for gid in json.loads(storage.kv_get("gast_ids") or "[]"):
            g = gast(gid) or {}
            if h and str(g.get("handle") or "").lower() == h:
                return gid, None, g
    except Exception:
        pass
    return None, None, None


def _kurz(t, n):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def text(q, n_msgs=10):
    import ai
    import motor
    tg, u, g = finden(q)
    if not tg:
        return f"No user found for '{q}'. Try @handle, the Telegram id, the wallet or reply to their message with /who."
    wallet = (u or {}).get("wallet")
    name = (u or {}).get("name") or (g or {}).get("name") or "?"
    handle = (u or {}).get("handle") or (g or {}).get("handle")
    z = [f"👤 {name}" + (f" (@{handle})" if handle else "") + f"  #id{tg}"]
    if wallet:
        z.append(f"Wallet: {wallet}")
        if u.get("linked"):
            z.append(f"Linked: {str(u['linked'])[:16].replace('T', ' ')} UTC")
        try:
            kt = ai.konto(wallet, anlegen=False) or {}
            z.append(f"Balance ${float(kt.get('credit') or 0):,.2f} · topped up ${float(kt.get('deposited') or 0):,.2f} · "
                     f"spent ${float(kt.get('spent') or 0):,.2f}" + (" · team account" if kt.get("free") else ""))
        except Exception:
            pass
        try:
            for e in ai.einzahlungen(wallet, 3):
                z.append(f"  + {e.get('odds') or 0:,.0f} {e.get('asset') or 'ODDS'} (${e.get('usd') or 0:,.2f}) {str(e.get('ts') or '')[:10]}")
        except Exception:
            pass
    else:
        z.append("No wallet linked yet (guest).")
        if g and g.get("seit"):
            z.append("First message: " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(g["seit"])))
    try:
        rest = motor.probe_rest(wallet or f"tg:{tg}")
        z.append(f"Free messages left: {rest}")
    except Exception:
        pass
    # Gespraech: als Gast und mit Wallet getrennt gespeichert, beides zeigen
    hist = []
    for a in ([f"tg:{tg}"] + ([wallet] if wallet else [])):
        try:
            hist += [dict(h, _src=a) for h in motor.verlauf(a)]
        except Exception:
            pass
    hist = [h for h in hist if h.get("role") in ("user", "assistant")][-n_msgs * 2:]
    if hist:
        z.append("")
        z.append("Last messages:")
        for h in hist:
            z.append(("🗣 " if h["role"] == "user" else "🤖 ") + _kurz(h.get("content"), 220 if h["role"] == "user" else 160))
    else:
        z.append("\nNo messages with Your Engine yet.")
    try:
        import support
        offen = support.offene_von(tg) if hasattr(support, "offene_von") else []
        if offen:
            z.append("")
            z.append(f"Open support tickets: " + ", ".join(f"#{t}" for t in offen[:5]))
    except Exception:
        pass
    z.append("\nReply to this message to write to them.")
    return "\n".join(z)[:4000]


def melden_neu(chat_id, von, erste):
    """Erster Kontakt eines Gastes: kurz an den Betreiber, mit #id zum Antworten und /who fuer mehr."""
    try:
        import ai
        wer = " ".join(x for x in ((von or {}).get("first_name"), (von or {}).get("last_name")) if x) or "someone"
        h = (von or {}).get("username")
        ai._an_betreiber(f"🆕 New guest in Your Engine: {wer}" + (f" (@{h})" if h else "") + f"  #id{chat_id}\n\n"
                         f"{_kurz(erste, 300)}\n\nYour Engine answers them (free trial). Reply here to write to them yourself, "
                         f"/who #id{chat_id} shows the whole chat.")
    except Exception as e:
        log.debug(f"kunde melden: {e}")
