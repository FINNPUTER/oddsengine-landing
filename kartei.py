"""
kartei.py: die Wallet-Kartei von OddsEngine (seit 2.55). Alles, was Trace, Scans und Wallet Guard ueber Wallets
herausfinden, wird hier gespeichert und beim naechsten Mal sofort genutzt.

Gespeichert werden nur oeffentliche Chain-Fakten mit Beleg, nie Personen:
  wl_wallets  Art und Name (Boerse, Dienst, Locker, Vertrag, Wallet) mit Quelle; Geldgeber mit Transaktion und Zeit
  wl_flags    Markierungen mit Beleg-Transaktion:
                drainer       hat Tokens eines Your-Engine-Nutzers mit einer alten Freigabe abgezogen (aus Wallet Guard)
                drain_dest    hat gestohlene Tokens aus einem Drain bekommen
                poison        Doppelgaenger-Adresse aus gefaelschten Token-Transfers (Address Poisoning)
                serial_dev    hat 3 oder mehr Coins gestartet (aus unseren Scans und Torch-Launches)
Gelesen wird ueberall: Trace zeigt die Markierungen, Wallet Guard stuft eine Freigabe an einen bekannten Drainer
sofort als kritisch ein, und Geschwister (gleicher Geldgeber) kommen aus der Kartei statt aus neuen Abfragen.
"""
import time

from logger import log

_SCHEMA = [False]
STARK = ("drainer", "drain_dest", "poison")


def _q(sql, args=(), eins=False):
    import intel
    if not _SCHEMA[0]:
        pg = intel._pg()
        if not pg:
            return None if eins else []
        try:
            with intel._LOCK:
                pg.execute("""CREATE TABLE IF NOT EXISTS wl_wallets (
                    chain TEXT NOT NULL, wallet TEXT NOT NULL, kind TEXT, name TEXT, source TEXT,
                    funder TEXT, funder_chain TEXT, funded_tx TEXT, funded_ts BIGINT, updated BIGINT,
                    PRIMARY KEY (chain, wallet))""")
                pg.execute("CREATE INDEX IF NOT EXISTS ix_wl_funder ON wl_wallets(funder)")
                pg.execute("""CREATE TABLE IF NOT EXISTS wl_flags (
                    chain TEXT NOT NULL, wallet TEXT NOT NULL, flag TEXT NOT NULL, detail TEXT, proof TEXT NOT NULL DEFAULT '',
                    ts BIGINT, PRIMARY KEY (chain, wallet, flag, proof))""")
                pg.execute("CREATE INDEX IF NOT EXISTS ix_wl_flags_wallet ON wl_flags(wallet)")
            _SCHEMA[0] = True
        except Exception as e:
            log.debug(f"kartei schema: {e}")
            return None if eins else []
    return intel._q(sql, args, eins)


def _k(chain, a):
    return a if chain in ("solana", "tron", "ton") else str(a or "").lower()


def art_merken(chain, w, kind, name=None, source=None):
    if not w or kind in (None, "wallet") and not name:
        return
    _q("INSERT INTO wl_wallets (chain, wallet, kind, name, source, updated) VALUES (%s,%s,%s,%s,%s,%s) "
       "ON CONFLICT (chain, wallet) DO UPDATE SET kind=EXCLUDED.kind, name=COALESCE(EXCLUDED.name, wl_wallets.name), "
       "source=COALESCE(EXCLUDED.source, wl_wallets.source), updated=EXCLUDED.updated",
       (chain, _k(chain, w), kind, name, source, int(time.time())))


def art(chain, w):
    r = _q("SELECT kind, name, source, updated FROM wl_wallets WHERE chain=%s AND wallet=%s AND kind IS NOT NULL",
           (chain, _k(chain, w)), eins=True)
    if not r or not r[0]:
        return None
    # Namen aus Listen und Tags bleiben, reine Aktivitaets-Einstufung wird nach 30 Tagen neu geprueft
    if r[2] == "activity" and time.time() - (r[3] or 0) > 30 * 86400:
        return None
    return {"kind": r[0], "name": r[1], "source": (r[2] or "") + " (OddsEngine records)"}


def geldgeber_merken(chain, w, funder, tx, ts, funder_chain=None):
    if not w or not funder:
        return
    _q("INSERT INTO wl_wallets (chain, wallet, funder, funder_chain, funded_tx, funded_ts, updated) VALUES (%s,%s,%s,%s,%s,%s,%s) "
       "ON CONFLICT (chain, wallet) DO UPDATE SET funder=EXCLUDED.funder, funder_chain=EXCLUDED.funder_chain, "
       "funded_tx=EXCLUDED.funded_tx, funded_ts=EXCLUDED.funded_ts, updated=EXCLUDED.updated",
       (chain, _k(chain, w), _k(funder_chain or chain, funder), funder_chain or chain, tx, ts, int(time.time())))


def geldgeber(chain, w):
    r = _q("SELECT funder, funder_chain, funded_tx, funded_ts FROM wl_wallets WHERE chain=%s AND wallet=%s AND funder IS NOT NULL",
           (chain, _k(chain, w)), eins=True)
    return {"from": r[0], "chain": r[1], "tx": r[2], "ts": r[3]} if r else None


def geschwister(chain, funder, ohne, n=10):
    rows = _q("SELECT wallet, funded_tx FROM wl_wallets WHERE funder=%s AND wallet<>%s LIMIT %s",
              (_k(chain, funder), _k(chain, ohne), n)) or []
    return [{"wallet": r[0], "funded_tx": r[1]} for r in rows]


def markieren(chain, w, flag, detail=None, proof=""):
    if not w:
        return
    _q("INSERT INTO wl_flags (chain, wallet, flag, detail, proof, ts) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
       (chain, _k(chain, w), flag, (detail or "")[:300], proof or "", int(time.time())))


def flags(chain, w):
    rows = _q("SELECT flag, detail, proof, ts FROM wl_flags WHERE chain=%s AND wallet=%s ORDER BY ts DESC LIMIT 20",
              (chain, _k(chain, w))) or []
    out = {}
    for f, d, p, ts in rows:
        e = out.setdefault(f, {"flag": f, "count": 0, "detail": d, "proofs": [], "first": ts})
        e["count"] += 1
        e["first"] = min(e["first"] or ts, ts or e["first"])
        if p and len(e["proofs"]) < 3:
            e["proofs"].append(p)
    return list(out.values())


def drainer(chain, w):
    """Ist das ein Wallet, das schon einmal Tokens eines Nutzers abgezogen hat?"""
    r = _q("SELECT proof FROM wl_flags WHERE chain=%s AND wallet=%s AND flag='drainer' LIMIT 1", (chain, _k(chain, w)), eins=True)
    return r[0] if r else None


def stand():
    a = _q("SELECT COUNT(*) FROM wl_wallets", eins=True)
    f = _q("SELECT flag, COUNT(*) FROM wl_flags GROUP BY flag") or []
    return {"wallets": a[0] if a else 0, "flags": {x[0]: x[1] for x in f}}
