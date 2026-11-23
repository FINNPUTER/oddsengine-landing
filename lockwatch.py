"""
lockwatch.py: Erinnerungen fuer eigene Locks im OddsEngine Locker, seit 2.70.7.

Fuer jeden Nutzer von Your Engine mit verknuepften EVM-Wallets: alle Locks, die diesen Wallets gehoeren.
  3 Tage vor der ersten Freigabe   "Your lock #12 of 100K $ODDS unlocks in 3 days"
  sobald etwas abholbar ist        "Lock #12: 100K $ODDS are ready to withdraw"
Jede Meldung genau einmal (je Lock und Freigabedatum, verlaengert man, kommt sie fuer das neue Datum wieder).
Holder bekommen ihre Meldung ueber die Bag-Wache (beutel.py): der Scan liest unseren Locker, 48 h vorher und bei Ablauf.

Kosten: nur Lese-Aufrufe an die Chain (Multicall), keine KI. Laeuft alle 30 Minuten aus motor.
Nur mit gespeicherter Locker-Adresse und Schalter 'locker'.
"""
import json
import time

import storage
from logger import log

KV = "lockwatch_sent"
SEITE = "https://engine.oddsengine.live/lock/?tab=mine"
VORLAUF = 3 * 86400


def _wort(b, i):
    return int.from_bytes(b[32 * i:32 * i + 32], "big")


def _ids(locker, wallet):
    import onchain
    from eth_abi import decode
    daten = "0x4bd42656" + onchain._pad(wallet.lower()) + hex(0)[2:].rjust(64, "0") + hex(200)[2:].rjust(64, "0")
    b = onchain._multicall([(locker, daten)])[0]
    if not b:
        return []
    return list(decode(["uint256[]"], b)[0])


def _menge(v, dez):
    n = v / 10 ** dez
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e3 else f"{n:,.2f}"


def _tag(ts):
    return time.strftime("%b %-d, %Y", time.gmtime(ts))


def runde():
    try:
        import features
        if not features.an("locker"):
            return
    except Exception:
        return
    import intel
    import motor
    import onchain
    locker = intel.odds_locker()
    if not locker:
        return
    try:
        gesendet = json.loads(storage.kv_get(KV) or "{}")
    except Exception:
        gesendet = {}
    jetzt = int(time.time())
    neu = False
    for adr in motor._nutzer():
        try:
            if not motor.offen(adr):
                continue
            wallets = [w for w in motor._wallets(adr) if str(w).startswith("0x")][:4]
        except Exception:
            continue
        for w in wallets:
            try:
                ids = _ids(locker, w)
            except Exception as e:
                log.debug(f"lockwatch ids {w[:8]}: {e}")
                continue
            if not ids:
                continue
            aufrufe = []
            for i in ids:
                aufrufe += [(locker, "0xd68f4dd1" + hex(i)[2:].rjust(64, "0")), (locker, "0xd1d58b25" + hex(i)[2:].rjust(64, "0"))]
            r = onchain._multicall(aufrufe)
            locks = []
            for k, i in enumerate(ids):
                b, c = r[2 * k], r[2 * k + 1]
                if not b or len(b) < 192:
                    continue
                locks.append({"id": i, "start": _wort(b, 1), "end": _wort(b, 2), "token": "0x" + b[108:128].hex(),
                              "amount": _wort(b, 4), "claimed": _wort(b, 5), "claimable": _wort(c, 0) if c else 0})
            meta = onchain._meta(sorted({x["token"] for x in locks})) if locks else {}
            for x in locks:
                if x["claimed"] >= x["amount"]:
                    continue
                m = meta.get(x["token"]) or {}
                sym, dez = m.get("symbol") or "?", int(m.get("decimals") or 18)
                rest = x["amount"] - x["claimed"]
                text = None
                k3 = f"{x['id']}:3d:{x['start']}"
                kr = f"{x['id']}:ready:{x['start']}"
                if 0 < x["start"] - jetzt <= VORLAUF and k3 not in gesendet:
                    tage = max(1, round((x["start"] - jetzt) / 86400))
                    art = "starts unlocking" if x["end"] > x["start"] else "unlocks"
                    text = (f"🔒 Your lock #{x['id']} of {_menge(rest, dez)} ${sym} {art} in {tage} day{'s' if tage != 1 else ''} "
                            f"({_tag(x['start'])}). You can extend it any time before that.")
                    gesendet[k3] = jetzt
                elif x["claimable"] > 0 and kr not in gesendet:
                    text = (f"🔓 Lock #{x['id']}: {_menge(x['claimable'], dez)} ${sym} are ready to withdraw. "
                            f"Only the lock owner can take them out.")
                    gesendet[kr] = jetzt
                if text:
                    neu = True
                    try:
                        motor.senden(adr, text, [[("Open My locks", SEITE)]])
                    except Exception as e:
                        log.debug(f"lockwatch senden: {e}")
    if neu:
        if len(gesendet) > 5000:
            gesendet = dict(sorted(gesendet.items(), key=lambda kv: kv[1])[-3000:])
        try:
            storage.kv_set(KV, json.dumps(gesendet))
        except Exception:
            pass
