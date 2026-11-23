"""
intel.py: die eigene Tiefe von Your Engine. Seit 2.39.1. Kein fremder Dienst.

  tiefe(chain, token, markt)   Tiefen-Scan aus dem Cache oder im Hintergrund starten
  scannen(chain, token, ...)   der Scan selbst: erste Kaeufer, Bundles, Sniper, frische Wallets,
                               gleiche Geldgeber (Cluster), Anteile, Halter, Dev, bewertete Kaeufer
  wallet(adr)                  was wir ueber ein Wallet wissen
  laeufer(stunden)             Coins aus unseren Reports, die danach gelaufen sind
  verfolgen()                  aus motor.tick: Coins nachverfolgen, Gewinner-Wallets bewerten
  importieren(url)             einmalige Uebernahme einer bestehenden Wallet-Datenbank (nur lesen)

Solana: normaler RPC (HELIUS_API_KEY, sonst SOLANA_RPC, sonst oeffentlich). Etwa 150 bis 250 Abfragen
je Tiefen-Scan, mit dem kostenlosen Helius-Plan also einige tausend Scans im Monat.
EVM (Robinhood zuerst, Base, Ethereum, Arbitrum): Transfer-Logs ab dem Launch ueber den RPC, Geldgeber
ueber den freien Blockscout der Chain. Kein Schluessel noetig.

Eigene Tabellen: iw_wallets (Bewertung), iw_early (fruehe Kaeufer je Coin), iw_tokens (verfolgte Coins),
iw_devs (wer was gelauncht hat).
"""
import json
import re
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import storage
import weiter
from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
SYSTEM = "11111111111111111111111111111111"
# Seit 2.62: Pool-Autoritaeten der grossen Solana-Launchpads und DEXe. Sie halten die Tokens im Pool oder in der
# Bonding Curve, sind aber keine Holder. Meteora DBC fehlte: die Curve stand als "One wallet holds 53%" im Scan.
POOL_AUTH = {
    "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM": "Meteora DBC (bonding curve)",
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC": "Meteora DAMM v2",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium AMM",
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL": "Raydium CPMM",
    "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh": "Raydium LaunchLab (bonding curve)",
}
TOKEN_PROGS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
FRUEH = int(os.getenv("INTEL_EARLY_BUYERS", "40"))
CACHE_S = int(os.getenv("INTEL_CACHE_MIN", "30")) * 60
FEHL_S = int(os.getenv("INTEL_FAIL_RETRY_SEC", "180"))
LAUF_X = float(os.getenv("INTEL_RUNNER_X", "5"))
BLOCKSCOUT = {"robinhood": "https://robinhoodchain.blockscout.com", "base": "https://base.blockscout.com",
              "ethereum": "https://eth.blockscout.com", "arbitrum": "https://arbitrum.blockscout.com",
              "optimism": "https://optimism.blockscout.com", "polygon": "https://polygon.blockscout.com"}
# Token-Locker auf Solana (Programm-Ids aus den Doku-Seiten der Anbieter)
LOCKER_SOL = {"strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m": "Streamflow", "strmdbLr6w7QNmsiEXyFwWg3VSfg1GiELgU27P8aCGw": "Streamflow",
              "strn1sS2qKxs7SgJ1xx4trPKSWdqxFim6HFG9ETXiCL": "Streamflow", "9qWEUss4PnL8kTPrnG96BpMqWZTrRo38RsqKapuSsmJK": "Streamflow",
              "aSTRM2NKoKxNnkmLWk9sz3k74gKBk9t7bpPrTGxMszH": "Streamflow", "strmTRD9k8gA3vCgADVR6rg4h9KyTY5fsjvbT8R3vnP": "Streamflow",
              "STAKEvGqQTtzJZH6BWDcbpzXXn2BBerPAgQ3EGLN2GH": "Streamflow staking",
              "LocpQgucEQHbqNABEYvBvwoxCPsSbG91A1QaQhQQqjn": "Jupiter Lock"}
# Token-Locker auf EVM-Ketten: Adresse -> Name. Der Robinhood-Locker meldet Locked(id, owner, token, amount, unlockTime).
# Seit 2.71.0: der "RobinhoodLocker" ist der Token-Locker von HoodLock (hoodlock.tech), dazu deren Vesting-Vertrag.
# Titan Locker und alle LP-Positionen liest lplock.py (Titan legt jeden Lock in einen eigenen Vertrag).
LOCKER_EVM = {"robinhood": {"0xd0f7d8c6e9f6d80c297bebe4f7fd1b9c8125c32f": "HoodLock",
                            "0x910e19bcc4bce46999994ed7297e0fc4431ec72e": "HoodLock Vesting",
                            # seit 2.77.0: verifizierter Vesting-Vertrag mit Vesting-NFTs (GWOOD, SLEUTH, RKST ...)
                            "0xb31eaefa2a0bdc53df6d7a7f0f289b6ee1a8aaf3": "VestingManager"}}
# Seit 2.70.1: unser eigener Locker (OddsLocker), Adresse aus Railway ODDS_LOCKER nach dem Deploy
# Seit 2.70.2 auch ohne Railway: die Adresse kann auf der Admin-Seite gespeichert werden (kv "odds_locker").
ODDS_LOCKER = ""
_LOCKER_TS = [0.0]


def odds_locker():
    """Adresse unseres Lockers (klein), aus ODDS_LOCKER oder aus der Admin-Seite. 60 s Cache."""
    global ODDS_LOCKER
    if time.time() - _LOCKER_TS[0] < 60:
        return ODDS_LOCKER
    _LOCKER_TS[0] = time.time()
    a = os.getenv("ODDS_LOCKER", "").strip().lower()
    if not a:
        try:
            import storage as _st
            a = str(_st.kv_get("odds_locker") or "").strip().lower()
        except Exception:
            a = ""
    if a.startswith("0x") and len(a) == 42:
        if ODDS_LOCKER and ODDS_LOCKER != a:
            LOCKER_EVM["robinhood"].pop(ODDS_LOCKER, None)
        ODDS_LOCKER = a
        LOCKER_EVM["robinhood"][a] = "OddsEngine Locker"
    return ODDS_LOCKER
LOCK_TOPICS = {"0x1cb39d6ecc8f823fa6183785ee795ddcb2873e92effbff9b9a377ef761035676"}
TOTE = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}
BOERSEN_EVM = {"0x28c6c06298d514db089934071355e5743bf21d60", "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
               "0xdfd5293d8e347dfe59e90efd55b2956a1343963d", "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",
               "0x9696f59e4d72e237be84ffd425dcad154bf96976", "0x4976a4a02f38326660d17bf34b431dc6e2eb2327",
               "0x71660c4005ba85c37ccec55d0c4493e66fe775d3", "0x503828976d22510aad0201ac7ec88293211d23da",
               "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740", "0x3cd751e6b0078be393132286c442345e5dc49699",
               "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43", "0x77696bb39917c91a0c3908d577d5e322095425ca",
               "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b", "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3",
               "0x2910543af39aba0cd09dbb2d50200b3e800a63d2", "0xa83b11093c858c86321fbc4c20fe82cdbd58e09e",
               "0xf89d7b9c864f589bbf53a82105107622b35eaa40", "0x0d0707963952f2fba59dd06f2b425ace40b492fe",
               "0x2b5634c42055806a59e9107ed44d43c426e58258", "0x876eabf441b2ee5b5b0554fd502a8e0600950cfa",
               "0x6262998ced04146fa42253a5c0af90ca02dfd2a3", "0x8731d54e9d02c286767d56ac03e8037c07e01e98",
               "0x3154cf16ccdb4c6d922629664174b904d80f2c35", "0x49048044d57e1c92a77f79988d21fa8faf74e97e",
               "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad", "0x1111111254eeb25477b68fb85ed929f73a960582",
               "0xdef1c0ded9bec7f1a1670819833240f027b25eff"}
BOERSEN_SOL = {"5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
               "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S", "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
               "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2", "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w",
               "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE", "6QJzieMYfp7yr3EdrePaQoG3Ghxs2wM98xSLRu8Xh56U",
               "39BEQuKehPQZDbqjWpsxDsL1sD6mrPnPQQMkkjcy7BLQ"}


# ── Datenbank ────────────────────────────────────────────────────────────

_SCHEMA = [False]
_LOCK = threading.Lock()


def _pg():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_wallets (
            wallet TEXT NOT NULL, chain TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0, label TEXT,
            winners INTEGER DEFAULT 0, calls INTEGER DEFAULT 0, best_x DOUBLE PRECISION DEFAULT 0,
            total_x DOUBLE PRECISION DEFAULT 0, win_rate DOUBLE PRECISION, avg_entry_mcap DOUBLE PRECISION,
            source TEXT DEFAULT 'own', updated BIGINT NOT NULL, PRIMARY KEY (wallet, chain))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_iw_wallets_score ON iw_wallets(score DESC)")
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_early (
            chain TEXT NOT NULL, token TEXT NOT NULL, wallet TEXT NOT NULL, rank INTEGER, buy_ts BIGINT,
            entry_mcap DOUBLE PRECISION, PRIMARY KEY (chain, token, wallet))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_iw_early_wallet ON iw_early(wallet)")
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_tokens (
            chain TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT, first_mcap DOUBLE PRECISION,
            peak_mcap DOUBLE PRECISION, multiple DOUBLE PRECISION, first_seen BIGINT, checked BIGINT,
            credited BOOLEAN DEFAULT FALSE, PRIMARY KEY (chain, token))""")
        # Seit 2.45: Herkunft und Markierung je frueher Kauf, Import-Score getrennt vom eigenen
        for sql in ("ALTER TABLE iw_early ADD COLUMN IF NOT EXISTS flag TEXT",
                    "ALTER TABLE iw_early ADD COLUMN IF NOT EXISTS src TEXT",
                    "ALTER TABLE iw_wallets ADD COLUMN IF NOT EXISTS import_score INTEGER"):
            pg.execute(sql)
        pg.execute("UPDATE iw_early SET src = CASE WHEN entry_mcap IS NOT NULL THEN 'import' ELSE 'own' END WHERE src IS NULL")
        pg.execute("UPDATE iw_wallets SET import_score = score WHERE source = 'import' AND import_score IS NULL")
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_devs (
            chain TEXT NOT NULL, dev TEXT NOT NULL, token TEXT NOT NULL, ts BIGINT, PRIMARY KEY (chain, dev, token))""")
        _SCHEMA[0] = True
    return pg


def _q(sql, args=(), eins=False):
    pg = _pg()
    if not pg:
        return None if eins else []
    try:
        # seit 2.80.3 ohne globale Sperre: storage hat jetzt einen Verbindungs-Pool
        cur = pg.execute(sql, args)
        if cur.description is None:
            return None
        return cur.fetchone() if eins else cur.fetchall()
    except Exception as e:
        log.debug(f"intel sql: {e}")
        return None if eins else []


def _k(chain, a):
    return a if chain in ("solana", "tron", "ton") else str(a).lower()


# ── Solana RPC ───────────────────────────────────────────────────────────

_OHNE_HELIUS = [False]


def _sol_url():
    k = os.getenv("HELIUS_API_KEY", "").strip()
    if k:
        return "https://mainnet.helius-rpc.com/?api-key=" + k
    if not _OHNE_HELIUS[0] and not os.getenv("SOLANA_RPC", "").strip():
        _OHNE_HELIUS[0] = True
        log.warning("HELIUS_API_KEY fehlt in diesem Service: Solana laeuft ueber den oeffentlichen RPC, Tiefen-Scans "
                    "scheitern dann oft (429). Variable in Railway bei web UND worker setzen.")
    return os.getenv("SOLANA_RPC", "").strip() or "https://api.mainnet-beta.solana.com"


def _sol(m, p, timeout=20):
    for versuch in range(3):
        try:
            d = requests.post(_sol_url(), json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, headers=UA, timeout=timeout).json()
            if "result" in d:
                return d["result"]
            if "429" in str(d.get("error")) or "rate" in str(d.get("error")).lower():
                time.sleep(0.6 * (versuch + 1))
                continue
            return None
        except Exception:
            time.sleep(0.4)
    return None


PAR = int(os.getenv("INTEL_PARALLEL", "8"))


def _sol_batch(calls, n=20):
    """[(methode, params)] -> [result|None], in Paketen. Seit 2.40.2 laufen die Pakete parallel (INTEL_PARALLEL)."""
    out = [None] * len(calls)

    def paket(i):
        teil = calls[i:i + n]
        for versuch in range(3):
            try:
                d = requests.post(_sol_url(), json=[{"jsonrpc": "2.0", "id": i + j, "method": m, "params": p}
                                                    for j, (m, p) in enumerate(teil)], headers=UA, timeout=30).json()
                if isinstance(d, list):
                    for x in d:
                        if isinstance(x, dict) and "result" in x:
                            out[int(x["id"])] = x["result"]
                    break
            except Exception:
                pass
            time.sleep(0.6 * (versuch + 1))
    with ThreadPoolExecutor(max(1, PAR)) as ex:
        list(ex.map(paket, range(0, len(calls), n)))
    return out


def _start_sigs(mint, max_seiten=int(os.getenv("INTEL_SIG_PAGES", "30"))):
    """Signaturen vom Launch an (aelteste zuerst) oder None, wenn der Coin zu gross ist.
    Seit 2.51.1 mit Zeitbudget (INTEL_SIG_BUDGET_SEC, Standard 10): das Blaettern laeuft nacheinander, bei Coins mit
    viel Handel dauerte es sonst Minuten. Reicht das Budget nicht, prueft der Scan die groessten Halter (schnell)."""
    vor, letzte, davor = None, [], []
    # Seit 2.60.6: 35 statt 10 Sekunden. Junge Coins mit viel Handel (pump.fun, 1-2 h alt) haben schnell 5.000 bis
    # 10.000 Signaturen, die passten nicht in 10 s und der Tiefen-Scan fiel auf die Halter zurueck.
    bis = time.time() + float(os.getenv("INTEL_SIG_BUDGET_SEC", "35"))
    for _ in range(max_seiten):
        if time.time() > bis:
            log.info(f"intel sol {mint[:8]}: launch too far back for the time budget, checking holders instead")
            return None
        opt = {"limit": 1000}
        if vor:
            opt["before"] = vor
        r = _sol("getSignaturesForAddress", [mint, opt], timeout=8)
        if r is None:
            return None
        if not r:
            break
        davor, letzte = letzte, r
        vor = r[-1].get("signature")
        if len(r) < 1000:
            break
    else:
        return None
    alle = [x for x in (davor + letzte) if x.get("signature") and not x.get("err")]
    return list(reversed(alle))


def _tx(sig):
    return ("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": int(os.getenv("SOL_TX_VERSION", "1")), "commitment": "confirmed"}])


def _schluessel(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    return [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]


def _token_delta(tx, mint):
    """{owner: rohe Aenderung} fuer diesen Mint in dieser Transaktion."""
    meta = (tx or {}).get("meta") or {}
    vor, nach = {}, {}
    for liste, ziel in ((meta.get("preTokenBalances") or [], vor), (meta.get("postTokenBalances") or [], nach)):
        for b in liste:
            if b.get("mint") != mint or not b.get("owner"):
                continue
            ziel[b["owner"]] = ziel.get(b["owner"], 0) + int(((b.get("uiTokenAmount") or {}).get("amount")) or 0)
    return {o: nach.get(o, 0) - vor.get(o, 0) for o in set(vor) | set(nach)}


def _geldgeber_aus(tx, wallet):
    """Wer hat diesem Wallet in dieser Transaktion SOL geschickt? (System-Transfer oder Bilanz)."""
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    gruppen = [msg.get("instructions") or []] + [i.get("instructions") or [] for i in meta.get("innerInstructions") or []]
    for g in gruppen:
        for ix in g:
            p = (ix or {}).get("parsed") or {}
            if not isinstance(p, dict) or p.get("type") not in ("transfer", "transferChecked", "createAccount"):
                continue
            info = p.get("info") or {}
            src, dst = info.get("source"), info.get("destination") or info.get("newAccount")
            if dst == wallet and src and src != wallet and int(info.get("lamports") or 0) > 5_000_000:
                return src
    namen = _schluessel(tx)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if wallet not in namen or len(pre) != len(namen) or len(post) != len(namen):
        return None
    i = namen.index(wallet)
    if post[i] - pre[i] <= 5_000_000:
        return None
    best, verlust = None, 0
    for j, n in enumerate(namen):
        if j != i and pre[j] - post[j] > verlust:
            best, verlust = n, pre[j] - post[j]
    return best if verlust > 5_000_000 else None


def _boerse_sol(adr):
    if adr in BOERSEN_SOL:
        return True
    r = _sol("getSignaturesForAddress", [adr, {"limit": 200}])
    if isinstance(r, list) and len(r) >= 200:
        zeiten = [x.get("blockTime") for x in r if x.get("blockTime")]
        if zeiten and max(zeiten) - min(zeiten) < 6 * 3600:
            return True
    return False


def _scan_sol(mint):
    t0 = time.time()
    sigs = _start_sigs(mint)
    ohne_start = not sigs
    txs = []
    if sigs:
        txs = [t for t in _sol_batch([_tx(s["signature"]) for s in sigs[:140]], 20) if t]
        ohne_start = not txs
    if ohne_start:
        # Alter oder grosser Coin: der Launch liegt zu weit zurueck. Dann die groessten Halter pruefen
        # (frisch? gleicher Geldgeber?), ohne Bundles und Sniper.
        sup = int((((_sol("getTokenSupply", [mint]) or {}).get("value")) or {}).get("amount") or 0) or _supply_rugcheck(mint)
        halter = _halter_sol(mint, sup)
        jetzt = int(time.time())
        kaeufer = [{"wallet": h["address"], "slot": 0, "ts": jetzt, "raw": 0} for h in halter if not h.get("pool")][:20]
        if not kaeufer:
            return {"measured": False, "why": "history too long and no holders read"}
        bestand = {h["address"]: int(h["pct"] / 100 * sup) for h in halter}
        _alter_sol(kaeufer, juenger_als=7 * 86400)
        erg = _auswerten("solana", mint, dev_sol(mint), 0, kaeufer, bestand, sup, halter, set(),
                         boerse=_boerse_sol, slot_key="slot", sniper_spanne=-1, ohne_start=True)
        return erg
    txs.sort(key=lambda t: (t.get("slot") or 0))
    erste = txs[0]
    dev = (_schluessel(erste) or [None])[0]
    start_slot = erste.get("slot") or 0
    kaeufer, verkaeufer = [], set()
    gesehen = set()
    for t in txs:
        for o, d in _token_delta(t, mint).items():
            if d < 0:
                verkaeufer.add(o)
            elif d > 0 and o not in gesehen:
                gesehen.add(o)
                kaeufer.append({"wallet": o, "slot": t.get("slot") or 0, "ts": t.get("blockTime") or 0, "raw": d,
                                "payer": (_schluessel(t) or [None])[0], "konto": weiter.konto_aus_tx(t, mint, o)})
    # Programme (Kurve, Pools) heraus: echte Wallets gehoeren dem System-Programm
    kand = [k["wallet"] for k in kaeufer[:FRUEH * 2]]
    besitzer = {}
    for i in range(0, len(kand), 100):
        r = _sol("getMultipleAccounts", [kand[i:i + 100], {"encoding": "base64"}]) or {}
        for a, v in zip(kand[i:i + 100], r.get("value") or []):
            besitzer[a] = (v or {}).get("owner")
    kaeufer = [k for k in kaeufer if besitzer.get(k["wallet"]) in (SYSTEM, None) and k["wallet"] not in (mint, dev)][:FRUEH]
    if not kaeufer:
        return {"measured": False, "why": "no early buyers found"}
    t1 = time.time()
    # Geldgeber und Alter der Kaeufer parallel zu Bestaenden und Haltern (seit 2.40.2)
    sup = int((((_sol("getTokenSupply", [mint]) or {}).get("value")) or {}).get("amount") or 0)
    wer = [k["wallet"] for k in kaeufer] + ([dev] if dev else [])
    # Seit 2.50.13: die Weiterleitungen laufen gleichzeitig mit Alter, Bestaenden und Haltern, nicht mehr danach
    dev_konto = next((weiter.konto_aus_tx(t, mint, dev) for t in txs[:5] if weiter.konto_aus_tx(t, mint, dev)), None) if dev else None
    schnappschuss = [dict(k) for k in kaeufer]
    with ThreadPoolExecutor(4) as ex:
        f_halter = ex.submit(_halter_sol, mint, sup)

        def _fwd():
            t_ = time.time()
            try:
                return weiter.sol(mint, schnappschuss, dev, dev_konto, sup, f_halter.result())
            except Exception as e:
                log.warning(f"intel forwards sol {mint[:8]}: {e}")
                return {"checked": False}
            finally:
                log.info(f"intel sol {mint[:8]}: forwards {time.time() - t_:.1f}s")
        f_fwd = ex.submit(_fwd)
        f_alter = ex.submit(_alter_sol, kaeufer)
        f_best = ex.submit(_sol_batch, [("getTokenAccountsByOwner", [w, {"mint": mint}, {"encoding": "jsonParsed"}]) for w in wer], 20)
        f_alter.result()
        best = f_best.result()
        halter = f_halter.result()
        vorwaerts = f_fwd.result()
    bestand = {}
    for w, r in zip(wer, best):
        try:
            bestand[w] = sum(int(x["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for x in (r or {}).get("value") or [])
        except Exception:
            bestand[w] = None
    log.info(f"intel sol {mint[:8]}: history {t1 - t0:.1f}s, wallets {time.time() - t1:.1f}s")
    erg = _auswerten("solana", mint, dev, start_slot, kaeufer, bestand, sup, halter, verkaeufer,
                     boerse=_boerse_sol, slot_key="slot", sniper_spanne=2)
    # Weiterleitungen (seit 2.46): Tokens ohne Verkauf an andere Wallets geschickt, seit 2.50.13 parallel gerechnet
    erg["forwards"] = vorwaerts
    _bundles_weiter(erg, sup)
    _haltedauer_sol(erg, kaeufer, bestand, sup)
    return erg


def _haltedauer_sol(erg, kaeufer, bestand, sup):
    """Seit 2.63: auch auf Solana, wann die fruehen Kaeufer komplett raus waren. Wer heute (fast) nichts mehr haelt:
    die letzte Transaktion seines Token-Kontos ist der Ausstieg (letzter Verkauf oder das Schliessen danach)."""
    try:
        rest = sup * 0.00002 if sup else 0
        konto = {k["wallet"]: k.get("konto") for k in kaeufer if k.get("konto")}
        raus = [w for w in konto if (bestand.get(w) or 0) <= rest]
        if not raus:
            return
        sigs = _sol_batch([("getSignaturesForAddress", [konto[w], {"limit": 1}]) for w in raus], 20)
        ende = {}
        for w, r in zip(raus, sigs):
            if isinstance(r, list) and r and r[0].get("blockTime"):
                ende[w] = int(r[0]["blockTime"])
        for b in erg.get("buyers") or []:
            if b.get("ts"):
                b["buy_ts"] = int(b["ts"])
            if b["wallet"] in ende and b.get("buy_ts") and ende[b["wallet"]] >= b["buy_ts"]:
                b["exit_ts"] = ende[b["wallet"]]
                b["held_h"] = round((b["exit_ts"] - b["buy_ts"]) / 3600, 2)
    except Exception as e:
        log.debug(f"intel haltedauer sol: {e}")


def _alter_sol(kaeufer, juenger_als=2 * 86400):
    """Je Kaeufer: wie alt ist das Wallet beim Kauf (frisch?), und wer hat es zuerst mit SOL gefuellt."""
    def alter(k):
        r = _sol("getSignaturesForAddress", [k["wallet"], {"limit": 100}]) or []
        aelteste = r[-1] if r else None
        zeit = (aelteste or {}).get("blockTime")
        k["tx_count"] = len(r)
        k["fresh"] = bool(len(r) < 100 and zeit and k["ts"] and (k["ts"] - zeit) < juenger_als)
        k["first_sig"] = (aelteste or {}).get("signature") if len(r) < 100 else None
        # Seit 2.49.9: wie viel hat das Wallet VOR dem Kauf gemacht, und wie alt war es da (fuer "prepared wallets")
        if k["ts"] and (len(r) < 100 or (zeit and zeit < k["ts"])):
            k["vorher"] = sum(1 for x in r if x.get("blockTime") and x["blockTime"] < k["ts"])
        if len(r) < 100 and zeit and k["ts"]:
            k["alter_tage"] = round((k["ts"] - zeit) / 86400, 2)
        return k
    with ThreadPoolExecutor(max(6, PAR * 2)) as ex:
        kaeufer[:] = list(ex.map(alter, kaeufer))
    erste_tx = _sol_batch([_tx(k["first_sig"]) for k in kaeufer if k.get("first_sig")], 20)
    i = 0
    for k in kaeufer:
        if k.get("first_sig"):
            k["funder"] = _geldgeber_aus(erste_tx[i], k["wallet"])
            i += 1


_RC = {}


def _rugcheck(mint):
    """RugCheck-Report, 5 Minuten im Speicher (Halter und Supply aus einer Abfrage)."""
    c = _RC.get(mint)
    if c and time.time() - c[0] < 300:
        return c[1]
    d = {}
    for versuch in range(3):  # seit 2.60.6: RugCheck braucht fuer neue Coins manchmal laenger, mit Wiederholung
        try:
            r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", headers=UA, timeout=30)
            if r.status_code == 200:
                d = r.json()
                break
            log.info(f"rugcheck {mint[:8]}: http {r.status_code}")
        except Exception as e:
            log.info(f"rugcheck {mint[:8]}: {e}")
        time.sleep(2 * (versuch + 1))
    if d:
        _RC[mint] = (time.time(), d)
    return d


def _supply_rugcheck(mint):
    try:
        return int(((_rugcheck(mint).get("token") or {}).get("supply")) or 0)
    except Exception:
        return 0


def _halter_rugcheck(mint):
    """Seit 2.59.3: Ersatz, wenn der Solana-RPC getTokenLargestAccounts verweigert (oeffentlicher RPC: 429 fuer genau
    diese Abfrage). RugCheck liefert die 20 groessten Halter mit Eigentuemer und kennzeichnet Pools (AMM)."""
    d = _rugcheck(mint)
    if not d:
        return []
    bekannt = d.get("knownAccounts") or {}
    out = {}
    for h in d.get("topHolders") or []:
        o = h.get("owner") or h.get("address")
        if not o or not h.get("pct"):
            continue
        x = out.setdefault(o, {"address": o, "pct": 0.0, "konto": h.get("address"),
                               "pool": str((bekannt.get(o) or {}).get("type") or "").upper() in ("AMM", "LOCKER") or o in POOL_AUTH})
        x["pct"] = round(x["pct"] + float(h["pct"]), 3)
    return sorted(out.values(), key=lambda x: -x["pct"])[:20]


def _halter_sol(mint, sup):
    r = _sol("getTokenLargestAccounts", [mint]) or {}
    konten = [x for x in (r.get("value") or []) if int(x.get("amount") or 0) > 0][:20]
    if not konten:
        # seit 2.61.1: RugCheck auch ohne Supply (der oeffentliche RPC lehnt dann oft beides ab, die Prozente kommen von RugCheck)
        return _halter_rugcheck(mint)
    if not sup:
        return []
    info = _sol("getMultipleAccounts", [[k["address"] for k in konten], {"encoding": "jsonParsed"}]) or {}
    owner = []
    for v in info.get("value") or []:
        try:
            owner.append(v["data"]["parsed"]["info"]["owner"])
        except Exception:
            owner.append(None)
    progs = {}
    eigner = [o for o in owner if o]
    if eigner:
        r2 = _sol("getMultipleAccounts", [eigner, {"encoding": "base64"}]) or {}
        for o, v in zip(eigner, r2.get("value") or []):
            progs[o] = (v or {}).get("owner")
    sum_, konto = {}, {}
    for k, o in zip(konten, owner):
        if not o:
            continue
        sum_[o] = sum_.get(o, 0) + int(k.get("amount") or 0)
        konto.setdefault(o, k["address"])
    gesperrt = _locker_sol(sum_, konto, progs, sup)
    out = []
    import solkurve
    for o, v in sorted(sum_.items(), key=lambda x: -x[1]):
        # Pool oder Programmkonto: gehoert einem Programm, oder ist eine Programm-Adresse (PDA, nicht auf der Kurve)
        # ohne eigenes Konto. Echte Wallets liegen immer auf der Kurve (seit 2.49.4).
        p = progs.get(o)
        # Seit 2.62 zusaetzlich die bekannten Pool-Autoritaeten: die haben oft SOL bekommen und damit ein System-Konto,
        # dann griff die PDA-Pruefung nicht (Squads-Tresore sind auch PDAs mit System-Konto und bleiben Holder).
        x = {"address": o, "pct": round(v / sup * 100, 3),
             "pool": (o in POOL_AUTH) or (p not in (SYSTEM, None)) or (p is None and not solkurve.auf_kurve(o)),
             "konto": konto.get(o)}
        if o in POOL_AUTH:
            x["pool_name"] = POOL_AUTH[o]
        if gesperrt.get(o):
            x.update(locked=True, locker=gesperrt[o], pool=False)
        out.append(x)
    return out[:20]


def _locker_sol(sum_, konto, progs, sup):
    """{owner: 'Streamflow'|'Jupiter Lock'} fuer Bestaende in einem Token-Locker.
    Jupiter Lock: das Escrow-Konto gehoert dem Lock-Programm. Streamflow: der Eigentuemer ist eine PDA ohne
    eigenes Konto, dann zeigt die erste Transaktion des Token-Kontos, welches Programm es angelegt hat."""
    out = {}
    pruefen = []
    for o, v in sum_.items():
        p = progs.get(o)
        if p in LOCKER_SOL:
            out[o] = LOCKER_SOL[p]
        elif p is None and sup and v / sup >= 0.005:
            pruefen.append(o)
    if not pruefen:
        return out
    sigs = _sol_batch([("getSignaturesForAddress", [konto[o], {"limit": 50}]) for o in pruefen[:8]], 8)
    erste = []
    for o, r in zip(pruefen, sigs):
        if isinstance(r, list) and r and len(r) < 50:
            erste.append((o, r[-1].get("signature")))
    txs = _sol_batch([_tx(sg) for _, sg in erste], 8) if erste else []
    for (o, _), tx in zip(erste, txs):
        keys = set(_schluessel(tx))
        for ix in (((tx or {}).get("transaction") or {}).get("message") or {}).get("instructions") or []:
            if isinstance(ix, dict) and ix.get("programId"):
                keys.add(ix["programId"])
        for pid, name in LOCKER_SOL.items():
            if pid in keys:
                out[o] = name
                break
    return out


PUMP_PROGRAMM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def dev_sol(mint):
    """Ersteller eines pump.fun-Coins aus seiner Bonding Curve (auch nach dem Wechsel zu PumpSwap).
    Die Kurve speichert den Ersteller ab Byte 49. None, wenn es kein pump.fun-Coin ist. Seit 2.49.4."""
    import base64
    import solkurve
    try:
        kurve, _ = solkurve.pda([b"bonding-curve", solkurve.b58dec(mint)], PUMP_PROGRAMM)
    except Exception:
        return None
    r = _sol("getAccountInfo", [kurve, {"encoding": "base64"}]) or {}
    v = r.get("value") or {}
    if v.get("owner") != PUMP_PROGRAMM:
        return None
    try:
        d = base64.b64decode(v["data"][0])
    except Exception:
        return None
    if len(d) < 81:
        return None
    ersteller = solkurve.b58enc(d[49:81])
    return None if ersteller == SYSTEM else ersteller


def bestand_sol(wallet, mint):
    """Wie viele Rohtokens eines Mints ein Wallet haelt, ueber alle seine Token-Konten (beide Token-Programme)."""
    r = _sol("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
    try:
        return sum(int(x["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for x in r.get("value") or [])
    except Exception:
        return None


def holder_zahl_sol(mint):
    """Nur mit Helius (DAS). Bis 3000 genau, darueber '3000+'."""
    if not os.getenv("HELIUS_API_KEY", "").strip():
        return None
    eigner = set()
    for seite in range(1, 4):
        r = _sol("getTokenAccounts", {"mint": mint, "limit": 1000, "page": seite}) or {}
        ks = r.get("token_accounts") or []
        for k in ks:
            if int(k.get("amount") or 0) > 0:
                eigner.add(k.get("owner"))
        if len(ks) < 1000:
            return len(eigner)
    return f"{len(eigner)}+"


# ── EVM ──────────────────────────────────────────────────────────────────

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _evm(chain, m, p):
    import scan
    return scan._evm(chain, m, p)


def _evm_batch(chain, calls):
    if chain == "robinhood":
        import onchain
        return onchain._rpc_batch(calls)
    import scan
    url = os.getenv("RPC_" + chain.upper(), "").strip() or scan.RPC.get(chain)
    out = [None] * len(calls)

    def paket(i):
        teil = calls[i:i + 40]
        for versuch in range(3):
            try:
                d = requests.post(url, json=[{"jsonrpc": "2.0", "id": i + j, "method": m, "params": p} for j, (m, p) in enumerate(teil)],
                                  headers=UA, timeout=30).json()
                if isinstance(d, list):
                    for x in d:
                        if "result" in x:
                            out[int(x["id"])] = x["result"]
                    return
            except Exception:
                pass
            time.sleep(0.6 * (versuch + 1))
    # Seit 2.56: die Pakete laufen parallel statt nacheinander
    with ThreadPoolExecutor(max(1, min(4, (len(calls) + 39) // 40))) as ex:
        list(ex.map(paket, range(0, len(calls), 40)))
    return out


# Seit 2.62.5: Adressen, die Tokens fuer viele Pools halten (Uniswap V4 PoolManager je Chain). Nie ein Holder.
POOLS_EVM = {"robinhood": ("0x8366a39cc670b4001a1121b8f6a443a643e40951",),
             "ethereum": ("0x000000000004444c5dc75cb358380d2e3de08a90",),
             "base": ("0x498581ff718922c3f8e6a244956af099b2652b2b",),
             "arbitrum": ("0x360e68faccca8ca495c1b759fd9eee466db9fb32",),
             "bsc": ("0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",)}


BS_IDS = {"robinhood": 4663, "base": 8453, "ethereum": 1, "arbitrum": 42161, "optimism": 10, "polygon": 137}


ES_IDS = {"ethereum": 1, "base": 8453, "arbitrum": 42161, "bsc": 56, "avalanche": 43114, "optimism": 10, "polygon": 137}
# Seit 2.63.1: Routescan (frei, Etherscan-kompatibel): Avalanche C-Chain, und Ethereum als Ersatz, wenn Blockscout drosselt
RS_IDS = {"avalanche": 43114, "ethereum": 1}
_BS_FEHLER = {}
_BS_TAKT = {"pro": float(os.getenv("BS_PRO_SEC", "0.4")), "frei": 0.3, "routescan": 0.5, "etherscan": 0.36}
_BS_LETZT = {}
_BS_LOCK = threading.Lock()
BS_TIMEOUT = float(os.getenv("BS_TIMEOUT_SEC", "10"))
_BS_ZUVIEL = re.compile(r"too many requests|rate limit|max rate|max calls|throttl|429", re.I)


def _bs_takt(name_):
    """Haelt den Abstand zwischen zwei Abfragen an denselben Weg ein (gemeinsam fuer alle Threads)."""
    with _BS_LOCK:
        jetzt = time.time()
        frei_ab = max(jetzt, _BS_LETZT.get(name_, 0) + _BS_TAKT.get(name_, 0.2))
        _BS_LETZT[name_] = frei_ab
    if frei_ab > jetzt:
        time.sleep(frei_ab - jetzt)


def _bs_gueltig(d, pfad):
    if isinstance(d, list):
        # v2-Endpunkte wie token-balances liefern eine Liste (seit 2.63.1 gueltig, vorher als Fehler verworfen)
        return pfad != "/api"
    if not isinstance(d, dict):
        return False
    if pfad == "/api":
        r = d.get("result")
        if isinstance(r, list):
            return True
        # "No transactions found" kommt mit status 0 und leerer Liste, das ist gueltig. Ein Text in result ist ein Fehler.
        return False
    t = str(d.get("error") or d.get("message") or "")
    return not re.search(r"api key|x402|rate limit|too many requests|forbidden|unauthori", t, re.I)


def _bs(chain, pfad, params=None):
    """Blockscout. Seit 2.63.1 mit Ersatzwegen der Reihe nach, bis einer eine gueltige Antwort gibt:
    1. Blockscout Pro (BLOCKSCOUT_API_KEY), 2. freier Explorer der Kette, 3. Routescan (Avalanche), 4. Etherscan V2
    (ETHERSCAN_API_KEY, nur die Etherscan-kompatible /api). Vorher gab es mit gesetztem Pro-Schluessel keinen Ersatz.
    Ebenfalls seit 2.63.1: Takt je Weg und bei "Too many requests" warten und neu fragen. Vorher galt eine
    Ratenbegrenzung als leere Antwort, die Spur hielt grosse Wallets dann faelschlich fuer "zu lange Geschichte" und brach ab."""
    wege = []
    key = os.getenv("BLOCKSCOUT_API_KEY", "").strip()
    cid = BS_IDS.get(chain)
    # Seit 2.77.3: Base und Polygon brauchen bei Blockscout einen bezahlten Plan (402). Ohne BLOCKSCOUT_PAID gar nicht erst fragen.
    if key and cid and not (chain in ("base", "polygon") and not os.getenv("BLOCKSCOUT_PAID", "").strip()):
        q = dict(params or {}, apikey=key)
        if pfad == "/api":
            wege.append(("pro", "https://api.blockscout.com/v2/api", dict(q, chain_id=cid)))
        else:
            wege.append(("pro", f"https://api.blockscout.com/{cid}{pfad}", q))
    if BLOCKSCOUT.get(chain):
        wege.append(("frei", BLOCKSCOUT[chain] + pfad, dict(params or {})))
    if pfad == "/api" and RS_IDS.get(chain):
        wege.append(("routescan", f"https://api.routescan.io/v2/network/mainnet/evm/{RS_IDS[chain]}/etherscan/api", dict(params or {})))
    es = os.getenv("ETHERSCAN_API_KEY", "").strip()
    if es and pfad == "/api" and ES_IDS.get(chain):
        wege.append(("etherscan", "https://api.etherscan.io/v2/api", dict(params or {}, chainid=ES_IDS[chain], apikey=es)))
    letzte = None
    for name_, url, p in wege:
        # ein Weg, der gerade dauerhaft scheitert, wird kurz uebersprungen (ausser er ist der letzte)
        if time.time() - _BS_FEHLER.get((chain, name_), 0) < 120 and name_ != wege[-1][0]:
            continue
        netz_fehler = 0
        for versuch in range(4):
            _bs_takt(name_)
            kaputt = False
            try:
                antwort = requests.get(url, params=p, headers=UA, timeout=BS_TIMEOUT)
                d = antwort.json() if antwort.status_code != 429 else {"message": "Too many requests", "result": None}
            except Exception:
                d, kaputt = None, True
            if _bs_gueltig(d, pfad):
                _BS_FEHLER.pop((chain, name_), None)
                return d
            if kaputt:
                # Seit 2.78.1: Zeitueberschreitung oder Verbindungsfehler. Vorher bis zu 4 Versuche zu je 20 s plus Pausen
                # (fast 2 Minuten je Abfrage), und erst danach wurde der Weg gesperrt: alle Threads hingen gleichzeitig.
                # Jetzt sofort fuer die anderen Threads sperren und hoechstens einmal neu versuchen.
                _BS_FEHLER[(chain, name_)] = time.time()
                netz_fehler += 1
                if netz_fehler >= 2:
                    break
                continue
            dd = d if isinstance(d, dict) else {}
            text = str(dd.get("message") or "") + " " + str(dd.get("result") or dd.get("error") or "")
            if d is None or _BS_ZUVIEL.search(text):
                time.sleep(min(6.0, 1.5 * (2 ** versuch)))
                continue
            break
        _BS_FEHLER[(chain, name_)] = time.time()
        letzte = d if isinstance(d, dict) else letzte
    return letzte


_LAUNCHER = {}


def _start_evm(chain, token):
    """(Launch-Block, Ersteller) ueber Torch-Launches oder den Explorer."""
    if chain == "robinhood":
        try:
            import onchain
            l = next((x for x in onchain.launches() if x["token"] == token), None)
            if l:
                return l["block"], l["creator"]
        except Exception:
            pass
        # Seit 2.63.1: Coins von anderen Launchpads (PONS, Uniswap direkt) ueber den ersten Mint im RPC. Blockscout steht
        # hinter Cloudflare und lieferte fuer solche Coins oft nichts, dann brach der Tiefen-Scan mit "launch not found" ab.
        b, d_ = _start_mint(chain, token)
        if b:
            # Seit 2.81: Wurde der Coin ueber einen Factory-/Launcher-Vertrag erzeugt, nennt der Explorer diesen Vertrag als
            # Ersteller, der erste Mint aber die Wallet, die ihn aufgerufen hat. Beides zeigen, damit kein Scanner-Vergleich
            # zwei verschiedene "Devs" liefert.
            try:
                cr = str((_bs(chain, f"/api/v2/addresses/{token}") or {}).get("creator_address_hash") or "").lower()
                if cr and d_ and cr != d_:
                    code = _evm(chain, "eth_getCode", [cr, "latest"])
                    if code and code != "0x":
                        _LAUNCHER[(chain, token)] = cr
            except Exception:
                pass
            return b, d_
    d = _bs(chain, f"/api/v2/addresses/{token}") or {}
    tx = d.get("creation_transaction_hash") or d.get("creation_tx_hash")
    dev = str(d.get("creator_address_hash") or "").lower() or None
    if tx:
        try:
            r = _evm(chain, "eth_getTransactionReceipt", [tx])
            return int(r["blockNumber"], 16), dev
        except Exception:
            pass
    return None, dev


def _start_mint(chain, token):
    """(Block, Absender) des ersten Mints (Transfer von 0x0) ueber eth_getLogs in Fenstern zu knapp 10 Mio. Bloecken."""
    try:
        top = int(_evm(chain, "eth_blockNumber", []), 16)
    except Exception:
        return None, None
    null = "0x" + "0" * 64
    w, b, fehler = 9_999_999, 0, 0
    while b <= top and fehler < 20:
        try:
            r = _evm(chain, "eth_getLogs", [{"address": token, "fromBlock": hex(b), "toBlock": hex(min(top, b + w)),
                                             "topics": [TRANSFER, null]}])
        except Exception as e:
            fehler += 1
            if _ist_429(e):
                time.sleep(min(3.0, 0.5 * fehler))
            else:
                w = max(100_000, w // 4)
            continue
        if r:
            erst = min(r, key=_log_ordnung)
            dev = None
            try:
                tx = _evm(chain, "eth_getTransactionByHash", [erst["transactionHash"]]) or {}
                dev = str(tx.get("from") or "").lower() or None
            except Exception:
                pass
            return int(erst["blockNumber"], 16), dev
        b += w + 1
    return None, None


def _logs(chain, token, von, max_logs=40000):
    """Alle Transfer-Logs ab dem Launch bis jetzt (seit 2.40 bis zum aktuellen Block, fuer Holder und Locks).
    Gibt (logs, vollstaendig) zurueck."""
    fenster = int(os.getenv("INTEL_RH_WINDOW", "5000000")) if chain == "robinhood" else 50_000  # seit 2.63.1 (vorher 500K: 170 Anfragen bei 3 Monate alten Coins, viele 429)
    try:
        top = int(_evm(chain, "eth_blockNumber", []), 16)
    except Exception:
        return [], False
    # Seit 2.56 parallel. Seit 2.59.2 mit Wiederholung je Fenster: der oeffentliche Robinhood-RPC antwortet bei mehreren
    # Anfragen gleichzeitig oft mit 429. Vorher fiel dann der ganze Lauf auf die langsame Einzelschleife zurueck, die bei
    # 429 das Fenster immer kleiner machte und nach 12 Fehlern mit "no transfers read" aufgab (Tiefen-Scan haengt).
    fenster_liste = [(b, min(top, b + fenster)) for b in range(von, top + 1, fenster + 1)]
    if 1 < len(fenster_liste) <= 600:
        def hol(ab):
            for versuch in range(6):
                try:
                    r = _evm(chain, "eth_getLogs", [{"address": token, "fromBlock": hex(ab[0]), "toBlock": hex(ab[1]), "topics": [TRANSFER]}])
                    if isinstance(r, list):
                        return r
                except Exception as e:
                    if not _ist_429(e):
                        return None
                time.sleep(0.4 * (versuch + 1))
            return _logs_bs(chain, token, ab[0], ab[1])  # seit 2.59.3: Ersatzweg ueber Blockscout Pro
        with ThreadPoolExecutor(3) as ex:
            teile = list(ex.map(hol, fenster_liste))
        if all(t is not None for t in teile):
            out = [l for t in teile for l in t]
            return (out[:max_logs], False) if len(out) >= max_logs else (out, True)
        # Nur die gescheiterten Fenster gehen unten einzeln, die anderen bleiben
        fertig = [l for t in teile if t is not None for l in t]
        offen = [ab for ab, t in zip(fenster_liste, teile) if t is None]
        for ab in offen:
            teil, ok = _logs_bereich(chain, token, ab[0], ab[1], fenster)
            fertig += teil
            if not ok:
                fertig.sort(key=_log_ordnung)
                return fertig[:max_logs], False
        fertig.sort(key=_log_ordnung)  # die Reihenfolge zaehlt: die ersten Kaeufer kommen zuerst
        return (fertig[:max_logs], False) if len(fertig) >= max_logs else (fertig, True)
    out, b, fehler = [], von, 0
    while b <= top:
        bis = min(top, b + fenster)
        try:
            r = _evm(chain, "eth_getLogs", [{"address": token, "fromBlock": hex(b), "toBlock": hex(bis), "topics": [TRANSFER]}]) or []
        except Exception as e:
            fehler += 1
            if _ist_429(e) and fehler <= 30:
                time.sleep(min(3.0, 0.4 * fehler))
                continue
            fenster //= 4
            if fenster < 500 or fehler > 30:
                return out, False
            continue
        out += r
        if len(out) >= max_logs:
            return out, False
        b = bis + 1
    return out, True


def _log_ordnung(l):
    try:
        return int(l.get("blockNumber") or "0x0", 16), int(l.get("logIndex") or "0x0", 16)
    except Exception:
        return 0, 0


def _ist_429(e):
    t = str(e).lower()
    return "429" in t or "too many requests" in t or "rate limit" in t


def _logs_bs(chain, token, von, bis):
    """Transfer-Logs eines Blockbereichs ueber Blockscout Pro (BLOCKSCOUT_API_KEY), seitenweise zu 1000.
    Ersatz, wenn der RPC der Kette dauerhaft mit 429 antwortet. Ohne Schluessel: None."""
    if not os.getenv("BLOCKSCOUT_API_KEY", "").strip() or chain not in BS_IDS:
        return None
    out, seite = [], 1
    while seite <= 60:
        d = None
        for versuch in range(3):
            d = _bs(chain, "/api", {"module": "logs", "action": "getLogs", "address": token, "fromBlock": von, "toBlock": bis,
                                    "topic0": TRANSFER, "page": seite, "offset": 1000})
            if isinstance(d, dict) and isinstance(d.get("result"), list):
                break
            time.sleep(0.5 * (versuch + 1))
        if not isinstance(d, dict) or not isinstance(d.get("result"), list):
            return None if not out else out
        teil = [l for l in d["result"] if isinstance(l, dict) and l.get("topics")]
        for l in teil:
            for k in ("blockNumber", "logIndex"):
                v = l.get(k)
                if isinstance(v, int) or (isinstance(v, str) and v and not v.startswith("0x")):
                    l[k] = hex(int(v))
            l["topics"] = [t for t in l["topics"] if t]
        out += teil
        if len(d["result"]) < 1000:
            return out
        seite += 1
    return out


def _logs_bereich(chain, token, von, bis_block, fenster):
    """Einen Bereich einzeln lesen, mit Warten bei 429 und kleineren Fenstern bei echten Bereichsfehlern."""
    ersatz = _logs_bs(chain, token, von, bis_block)
    if ersatz is not None:
        return ersatz, True
    out, b, fehler = [], von, 0
    while b <= bis_block:
        bis = min(bis_block, b + fenster)
        try:
            r = _evm(chain, "eth_getLogs", [{"address": token, "fromBlock": hex(b), "toBlock": hex(bis), "topics": [TRANSFER]}]) or []
        except Exception as e:
            fehler += 1
            if fehler > 30:
                return out, False
            if _ist_429(e):
                time.sleep(min(3.0, 0.4 * fehler))
            else:
                fenster = max(500, fenster // 4)
            continue
        out += r
        b = bis + 1
    return out, True


def _geldgeber_evm(chain, w):
    d = _bs(chain, "/api", {"module": "account", "action": "txlist", "address": w, "sort": "asc", "page": 1, "offset": 5}) or {}
    liste = d.get("result") if isinstance(d.get("result"), list) else []
    erste = liste[0] if liste else None
    funder = None
    for x in liste:
        if str(x.get("to") or "").lower() == w and int(x.get("value") or 0) > 0:
            funder = str(x.get("from") or "").lower()
            break
    return (int(erste["timeStamp"]) if erste and erste.get("timeStamp") else None), funder


def _boerse_evm(chain, adr):
    if adr in BOERSEN_EVM:
        return True
    d = _bs(chain, f"/api/v2/addresses/{adr}/counters") or {}
    try:
        return int(d.get("transactions_count") or 0) > 5000
    except Exception:
        return False


def _entsperrung(chain, locker, txs):
    """Fruehestes Entsperrdatum aus den Lock-Ereignissen der Transaktionen, die Tokens zum Locker gebracht haben."""
    zeiten = []
    for tx in list(txs)[:6]:
        try:
            rc = _evm(chain, "eth_getTransactionReceipt", [tx]) or {}
        except Exception:
            continue
        for lg in rc.get("logs") or []:
            if str(lg.get("address") or "").lower() != locker or not lg.get("topics") or lg["topics"][0] not in LOCK_TOPICS:
                continue
            roh = (lg.get("data") or "0x")[2:]
            for i in range(0, len(roh), 64):
                try:
                    v = int(roh[i:i + 64], 16)
                except Exception:
                    continue
                if time.time() - 400 * 86400 < v < time.time() + 30 * 365 * 86400:
                    zeiten.append(v)
    return min(zeiten) if zeiten else None


def _scan_evm(chain, token):
    uhr = [time.time()]
    schritte = []

    def mess(name):
        jetzt = time.time()
        schritte.append(f"{name} {jetzt - uhr[0]:.1f}s")
        uhr[0] = jetzt
        fortschritt_setzen(chain, token, name)
    try:
        return _scan_evm_lauf(chain, token, mess)
    finally:
        log.info(f"intel evm {chain} {token[:10]} steps: " + ", ".join(schritte))


def _scan_evm_lauf(chain, token, mess):
    token = token.lower()
    block, dev = _start_evm(chain, token)
    mess("start")
    if not block:
        return {"measured": False, "why": "launch not found"}
    logs, voll = _logs(chain, token, block)
    mess(f"logs({len(logs)})")
    if not logs:
        return {"measured": False, "why": "no transfers read"}
    try:
        sup = int(_evm(chain, "eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), 16)
    except Exception:
        sup = 0
    if not sup:
        try:
            sup = int((_evm_batch(chain, [("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"])]) or [None])[0] or "0x0", 16)
        except Exception:
            sup = 0
    klein = sup * 0.0002 if sup else 0
    infra = {token} | TOTE
    if chain == "robinhood":
        try:
            import onchain
            infra |= {onchain.FABRIK.lower()} | {a.lower() for a in onchain._pool_adressen()}
        except Exception:
            pass
    ts_block, netto, zum_locker = {}, {}, {}
    lock = LOCKER_EVM.get(chain, {})
    infra |= set(lock)
    kaeufer, verkaeufer, gesehen = [], set(), set()
    for l in logs:
        try:
            von = "0x" + l["topics"][1][-40:]
            an = "0x" + l["topics"][2][-40:]
            menge = int(l.get("data") or "0x0", 16)
            b = int(l["blockNumber"], 16)
        except Exception:
            continue
        netto[von] = netto.get(von, 0) - menge
        netto[an] = netto.get(an, 0) + menge
        if an in lock:
            zum_locker.setdefault(an, set()).add(l.get("transactionHash"))
        if von not in TOTE:
            verkaeufer.add(von)
        if len(kaeufer) >= FRUEH * 3 or an in infra or menge <= klein or an in gesehen:
            continue
        gesehen.add(an)
        kaeufer.append({"wallet": an, "slot": b, "raw": menge, "tx": l.get("transactionHash")})
    # Vertraege (Pools, Kurve, Router, Locker) heraus. Unklare Antworten einzeln nachfragen, nie als Wallet zaehlen.
    codes = _evm_batch(chain, [("eth_getCode", [k["wallet"], "latest"]) for k in kaeufer])
    for i, (k, c) in enumerate(zip(kaeufer, codes)):
        if c is None:
            try:
                codes[i] = _evm(chain, "eth_getCode", [k["wallet"], "latest"])
            except Exception:
                codes[i] = "?"
    import codeart
    codes = [codeart.norm(c) for c in codes]
    kaeufer = [k for k, c in zip(kaeufer, codes) if c in ("0x", "0x0") and k["wallet"] != dev][:FRUEH]
    mess("codes")
    if not kaeufer:
        return {"measured": False, "why": "no early buyers found"}
    bl = sorted({k["slot"] for k in kaeufer} | {block})
    zeiten = _evm_batch(chain, [("eth_getBlockByNumber", [hex(b), False]) for b in bl])
    for b, z in zip(bl, zeiten):
        if z and z.get("timestamp"):
            ts_block[b] = int(z["timestamp"], 16)
    for k in kaeufer:
        k["ts"] = ts_block.get(k["slot"], 0)

    def info(k):
        erst, funder = _geldgeber_evm(chain, k["wallet"])
        k["fresh"] = bool(erst and k["ts"] and (k["ts"] - erst) < 2 * 86400)
        k["funder"] = funder
        if erst and k["ts"]:
            k["alter_tage"] = round((k["ts"] - erst) / 86400, 2)
        return k
    if BLOCKSCOUT.get(chain):
        with ThreadPoolExecutor(4) as ex:   # seit 2.77.3: Free-Plan 5 Abfragen je Sekunde
            kaeufer = list(ex.map(info, kaeufer))
    else:
        noncen = _evm_batch(chain, [("eth_getTransactionCount", [k["wallet"], "latest"]) for k in kaeufer])
        for k, n in zip(kaeufer, noncen):
            k["fresh"] = bool(n and int(n, 16) <= 3)
    mess("funders")
    # Seit 2.49.9: Nonce = alle ausgehenden Transaktionen bis heute, eine Obergrenze fuer die Vorgeschichte
    try:
        nn = _evm_batch(chain, [("eth_getTransactionCount", [k["wallet"], "latest"]) for k in kaeufer])
        for k, n in zip(kaeufer, nn):
            if n:
                k["vorher"] = int(n, 16)
    except Exception:
        pass
    # Bestaende: aus den Logs, wenn sie vollstaendig sind, sonst einzeln abfragen
    wer = [k["wallet"] for k in kaeufer] + ([dev] if dev else [])
    # Seit 2.81: immer der echte Bestand per balanceOf. Die Summe aus den Logs nur, wo der Aufruf scheitert und die Logs
    # vollstaendig sind. Vorher konnten unvollstaendige Logs bei grossen Coins "0 % halten noch" ergeben.
    try:
        best = _evm_batch(chain, [("eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + w[2:]}, "latest"]) for w in wer])
    except Exception:
        best = [None] * len(wer)
    bestand = {}
    for w, b in zip(wer, best):
        try:
            bestand[w] = int(b, 16) if b and b != "0x" else None
        except Exception:
            bestand[w] = None
        if bestand[w] is None and voll:
            bestand[w] = max(0, netto.get(w, 0))
    # Holder-Karte und Anzahl aus den Logs
    halter, anzahl = [], None
    if voll and sup:
        positiv = {a: v for a, v in netto.items() if v > 0 and a not in TOTE}
        anzahl = len(positiv)
        top = sorted(positiv.items(), key=lambda x: -x[1])[:25]
        import codeart
        roh_cs = _evm_batch(chain, [("eth_getCode", [a, "latest"]) for a, _ in top])
        # Seit 2.62.5: fehlende Antworten (429 im Paket) einzeln nachholen. Vorher galt ein Vertrag ohne Antwort als
        # Wallet, so stand der Uniswap-V4-PoolManager als "One wallet holds 63%" im Scan.
        for i, ((a, _), c) in enumerate(zip(top, roh_cs)):
            if c is None:
                try:
                    roh_cs[i] = _evm(chain, "eth_getCode", [a, "latest"])
                except Exception:
                    pass
        cs = [codeart.norm(c) for c in roh_cs]
        bekannt = {str(x).lower() for x in POOLS_EVM.get(chain, ())}
        try:
            if chain == "robinhood":
                import onchain
                bekannt |= onchain._pool_adressen()
        except Exception:
            pass
        for (a, v), c in zip(top, cs):
            x = {"address": a, "pct": round(v / sup * 100, 3),
                 "pool": (bool(c and c not in ("0x", "0x0")) or a in bekannt) and a not in lock}
            if a in lock:
                x.update(locked=True, locker=lock[a])
                u = _entsperrung(chain, a, zum_locker.get(a, set()))
                if u:
                    x["unlock_ts"] = u
            halter.append(x)
    mess("holders")
    erg = _auswerten(chain, token, dev, block, kaeufer, bestand, sup, halter, verkaeufer,
                     boerse=lambda a: _boerse_evm(chain, a), slot_key="slot", sniper_spanne=2)
    mess("evaluate")
    if anzahl:
        erg["holders_count"] = anzahl
    if _LAUNCHER.get((chain, token)):
        erg["launcher"] = _LAUNCHER[(chain, token)]
    try:
        erg["forwards"] = weiter.evm(chain, token, logs, infra, sup, netto, [k["wallet"] for k in kaeufer], dev, voll)
        _haltedauer_evm(chain, erg, logs, sup, block)
    except Exception as e:
        log.warning(f"intel forwards {chain} {token[:10]}: {e}")
        erg["forwards"] = {"checked": False}
    mess("forwards")
    _bundles_weiter(erg, sup)
    # Seit 2.78.0: Launch-Block nach Kaufmenge (nicht nach heutigem Bestand) und das Funder-Netz um den Dev.
    # Vorher fiel ein Wallet, das im Creation-Block 10% kaufte und nach Sekunden verkaufte, durch: es hielt nichts mehr.
    try:
        import funder

        def zeit(bl):
            if bl not in ts_block:
                try:
                    z = _evm(chain, "eth_getBlockByNumber", [hex(bl), False]) or {}
                    ts_block[bl] = int(z.get("timestamp") or "0x0", 16) or None
                except Exception:
                    ts_block[bl] = None
            return ts_block[bl]
        erg["launch_buys"] = funder.startkaeufe(chain, block, logs, sup, kaeufer, zeit)
        mess("launch_buys")
        if dev and os.getenv("FUNDER_NET", "1") != "0":
            # Seit 2.78.1 als zweite Phase: der Tiefen-Scan ist fertig und sichtbar, das Funder-Netz kommt danach dazu
            # (scannen startet es nach dem Speichern). Vorher wartete die ganze Karte auf bis zu 45 s Explorer-Abfragen.
            _FUNDER_JOB[(chain, token)] = (dev, block, logs, sup, kaeufer, zeit, infra)
            erg["funder_net"] = {"state": "running"}
    except Exception as e:
        log.warning(f"intel funder {chain} {token[:10]}: {e}")
    return erg


def _haltedauer_evm(chain, erg, logs, sup, start):
    """Seit 2.63: wann jeder fruehe Kaeufer gekauft hat und wann er komplett raus war (Bestand auf fast 0).
    Zeiten aus der Blocknummer, linear zwischen Launch-Block und dem letzten Block (zwei Abfragen statt hunderte)."""
    try:
        ks = {b["wallet"] for b in erg.get("buyers") or []}
        if not ks or not logs:
            return
        stand, rein, raus = {}, {}, {}
        rest = sup * 0.00002 if sup else 0
        for l in logs:
            try:
                von = "0x" + l["topics"][1][-40:]
                an = "0x" + l["topics"][2][-40:]
                m = int(l.get("data") or "0x0", 16)
                bl = int(l["blockNumber"], 16)
            except Exception:
                continue
            if an in ks:
                stand[an] = stand.get(an, 0) + m
                rein.setdefault(an, bl)
                raus.pop(an, None)
            if von in ks:
                stand[von] = stand.get(von, 0) - m
                if stand[von] <= rest and von in rein:
                    raus[von] = bl
        letzt = max(int(l["blockNumber"], 16) for l in logs)
        t0 = int((_evm(chain, "eth_getBlockByNumber", [hex(start), False]) or {}).get("timestamp") or "0x0", 16)
        t1 = int((_evm(chain, "eth_getBlockByNumber", [hex(letzt), False]) or {}).get("timestamp") or "0x0", 16)
        if not t0 or not t1 or letzt <= start:
            return
        zeit = lambda bl: int(t0 + (bl - start) * (t1 - t0) / (letzt - start))
        for b in erg.get("buyers") or []:
            w = b["wallet"]
            if w in rein:
                b["buy_ts"] = zeit(rein[w])
            if w in raus:
                b["exit_ts"] = zeit(raus[w])
                b["held_h"] = round((b["exit_ts"] - b.get("buy_ts", b["exit_ts"])) / 3600, 2)
    except Exception as e:
        log.debug(f"intel haltedauer: {e}")


def _bundles_weiter(erg, sup):
    """Seit 2.63: Bundles, die ihre Tokens ueber mehrere Stationen an neue Wallets weitergereicht haben, zaehlen mit.
    bundled_supply_pct = was die Bundle-Wallets selbst noch halten; dazu kommt, was am Ende ihrer Weiterleitungsketten
    (bis weiter.MAX_HOPS Stationen) heute noch liegt."""
    try:
        import weiter
        fw = erg.get("forwards") or {}
        if not fw.get("origins") or not sup:
            return
        buendel = [b["wallet"] for b in erg.get("buyers") or [] if b.get("bundled")]
        weg_b = weiter.versteckt_von(fw, buendel) / sup * 100
        erg["bundled_forwarded_pct"] = round(weg_b, 2)
        erg["bundled_supply_incl_forwarded_pct"] = round((erg.get("bundled_supply_pct") or 0) + weg_b, 2)
        if erg.get("dev"):
            erg["dev_forwarded_still_held_pct"] = round(weiter.versteckt_von(fw, [erg["dev"]]) / sup * 100, 2)
        frueh = [b["wallet"] for b in erg.get("buyers") or []]
        erg["early_forwarded_still_held_pct"] = round(weiter.versteckt_von(fw, frueh) / sup * 100, 2)
        erg["forward_max_hops"] = fw.get("max_hops")
    except Exception as e:
        log.debug(f"intel bundles weiter: {e}")


# ── Auswertung (fuer beide Welten gleich) ────────────────────────────────

def _halte_quote(kaeufer, bestand):
    """Anteil der fruehen Kaeufer, die noch halten. Nur ueber Wallets mit bekanntem Bestand; None, wenn weniger als die
    Haelfte bekannt ist (seit 2.81, vorher zaehlte ein unbekannter Bestand als 0)."""
    bek = [k for k in kaeufer if bestand.get(k["wallet"]) is not None]
    if not kaeufer or len(bek) * 2 < len(kaeufer):
        return None
    return round(sum(1 for k in bek if bestand[k["wallet"]] > 0) / len(bek) * 100, 1)


def _auswerten(chain, token, dev, start, kaeufer, bestand, sup, halter, verkaeufer, boerse, slot_key, sniper_spanne, ohne_start=False):
    pct = lambda w: (bestand.get(w) or 0) / sup * 100 if sup else 0.0
    # Bundles: mehrere fruehe Kaeufer im selben Block/Slot; Sniper: im Launch-Block oder direkt danach
    je_slot = {}
    for k in kaeufer if not ohne_start else []:
        je_slot.setdefault(k[slot_key], []).append(k["wallet"])
    gebuendelt = {w for ws in je_slot.values() if len(ws) >= 2 for w in ws}
    sniper = {k["wallet"] for k in kaeufer if k[slot_key] - start <= sniper_spanne} if not ohne_start else set()
    gebuendelt |= sniper
    # Gleiche Geldgeber
    gruppen = {}
    for k in kaeufer:
        f = k.get("funder")
        if f and f != k["wallet"]:
            gruppen.setdefault(f, []).append(k["wallet"])
    if dev:
        for f, ws in gruppen.items():
            if f == dev:
                ws.append(dev) if dev not in ws else None
    cluster, boersen = [], set()
    kandidaten = [f for f, ws in gruppen.items() if len(ws) >= 2]

    def pruef(f):
        try:
            return f, bool(boerse(f))
        except Exception:
            return f, False
    with ThreadPoolExecutor(max(1, min(8, len(kandidaten) or 1))) as ex:
        ist_b = dict(ex.map(pruef, kandidaten))
    for f, ws in sorted(gruppen.items(), key=lambda x: -len(x[1])):
        if len(ws) < 2:
            continue
        ist_boerse = ist_b.get(f, False)
        if ist_boerse:
            boersen.add(f)
            continue
        cluster.append({"type": "funding", "funder": f, "members": ws, "wallets": len(ws),
                        "supply_pct": round(sum(pct(w) for w in ws), 2)})
    for s, ws in sorted(je_slot.items()):
        if len(ws) >= 3 and not any(set(ws) <= set(c["members"]) for c in cluster):
            cluster.append({"type": "timing", "funder": None, "members": ws, "wallets": len(ws),
                            "supply_pct": round(sum(pct(w) for w in ws), 2)})
    for i, c in enumerate(cluster[:12], 1):
        c["id"] = i
    cluster = cluster[:12]
    in_cluster = {w for c in cluster for w in c["members"]}
    frisch = [k for k in kaeufer if k.get("fresh")]
    vorb = _vorbereitet(kaeufer, dev, boerse, ist_b, pct)
    bewertet = _bewertete([k["wallet"] for k in kaeufer], chain)
    dev_hist = _dev_historie(chain, dev, token) if dev else {}
    out = {
        "measured": True, "chain": chain, "token": token, "ts": int(time.time()), "dev": dev,
        "basis": "top holders (launch too far back)" if ohne_start else "first buyers",
        "dev_holds_pct": round(pct(dev), 3) if dev and bestand.get(dev) is not None else None,
        "early_buyers": len(kaeufer),
        "bundled_supply_pct": round(sum(pct(w) for w in gebuendelt), 2),
        "fresh_wallet_pct": round(len(frisch) / len(kaeufer) * 100, 1),
        "snipers": len(sniper), "clusters": len(cluster),
        "same_funder_groups": sum(1 for c in cluster if c["type"] == "funding"),
        "cluster_supply_pct": round(sum(pct(w) for w in in_cluster), 2),
        "still_holding_pct": _halte_quote(kaeufer, bestand),
        "cluster_map": cluster, "holders_map": halter or [], "prepared": vorb,
        "rated": bewertet, "dev_history": dev_hist,
        "buyers": [{"wallet": k["wallet"], "slot": k[slot_key], "ts": k.get("ts"), "fresh": bool(k.get("fresh")),
                    "pct": round(pct(k["wallet"]), 3), "bundled": k["wallet"] in gebuendelt,
                    "cluster": next((c["id"] for c in cluster if k["wallet"] in c["members"]), None)} for k in kaeufer],
    }
    return out


PREP_MAX_TX = int(os.getenv("PREP_MAX_TX", "5"))
PREP_MAX_AGE = float(os.getenv("PREP_MAX_AGE_DAYS", "45"))


def _vorbereitet(kaeufer, dev, boerse, schon, pct):
    """Seit 2.49.9: "prepared wallets". Gekaufte oder vorbereitete Wallets, die Bubble Maps und den Frisch-Check umgehen
    sollen: kaum Vorgeschichte vor dem Kauf, und entweder von einer Boerse aufgeladen (dann gibt es keine gemeinsame
    Quelle, die eine Karte verbinden koennte) oder erst Tage bis wenige Wochen alt mit hoechstens 2 Transaktionen."""
    duenn = [k for k in kaeufer if k.get("vorher") is not None and k["vorher"] <= PREP_MAX_TX and k["wallet"] != dev]
    if not duenn:
        return {"checked": True, "count": 0, "of": len(kaeufer)}
    offen = list({k["funder"] for k in duenn if k.get("funder") and k["funder"] not in schon})[:20]

    def pruef(f):
        try:
            return f, bool(boerse(f))
        except Exception:
            return f, False
    ist = dict(schon)
    if offen:
        with ThreadPoolExecutor(min(8, len(offen))) as ex:
            ist.update(dict(ex.map(pruef, offen)))
    treffer, von_boerse = [], 0
    for k in duenn:
        b = bool(k.get("funder") and ist.get(k["funder"]))
        alt = k.get("alter_tage")
        gealtert = alt is not None and 1 <= alt <= PREP_MAX_AGE and k["vorher"] <= 2
        if b or gealtert:
            treffer.append(k)
            von_boerse += 1 if b else 0
    return {"checked": True, "count": len(treffer), "of": len(kaeufer),
            "pct_of_buyers": round(len(treffer) / len(kaeufer) * 100, 1) if kaeufer else 0,
            "exchange_funded": von_boerse, "supply_pct": round(sum(pct(k["wallet"]) for k in treffer), 2),
            "wallets": [{"wallet": k["wallet"], "tx_before": k.get("vorher"), "age_days": k.get("alter_tage"),
                         "funder": k.get("funder"), "exchange": bool(k.get("funder") and ist.get(k["funder"])),
                         "pct": round(pct(k["wallet"]), 3)} for k in treffer[:12]]}


def _bewertete(wallets, chain):
    if not wallets:
        return {"found": 0, "checked": 0}
    ks = [_k(chain, w) for w in wallets]
    rows = _q("SELECT wallet, score, winners, best_x FROM iw_wallets WHERE chain=%s AND wallet = ANY(%s) AND score > 0 ORDER BY score DESC",
              (chain, ks)) or []
    stark = float(os.getenv("SMART_MIN_SCORE", "50"))
    return {"found": len(rows), "strong": sum(1 for r in rows if (r[1] or 0) >= stark), "min_score": stark,
            "checked": len(wallets), "best": rows[0][1] if rows else None,
            "wallets": [{"wallet": r[0], "score": r[1], "winners": r[2], "best_x": r[3]} for r in rows[:8]]}


def _dev_historie(chain, dev, token):
    rows = _q("SELECT d.token, t.multiple, t.symbol FROM iw_devs d LEFT JOIN iw_tokens t ON t.chain=d.chain AND t.token=d.token "
              "WHERE d.chain=%s AND d.dev=%s AND d.token<>%s", (chain, _k(chain, dev), _k(chain, token))) or []
    gelaufen = [r for r in rows if (r[1] or 0) >= LAUF_X]
    out = {"seen": len(rows), "ran": len(gelaufen), "examples": [r[2] for r in rows if r[2]][:5]}
    if chain == "robinhood":
        try:
            import onchain
            n = sum(1 for l in onchain.launches() if l.get("creator") == _k(chain, dev) and l["token"] != _k(chain, token))
            out["seen"] = max(out["seen"], n)
        except Exception:
            pass
    return out


def _merken(chain, token, ergebnis, mcap, symbol):
    """Fruehe Kaeufer, Dev und den Coin speichern: daraus wird spaeter die Bewertung."""
    if not ergebnis.get("measured"):
        return
    jetzt = int(time.time())
    tk = _k(chain, token)
    _q("INSERT INTO iw_tokens (chain, token, symbol, first_mcap, peak_mcap, multiple, first_seen, checked) "
       "VALUES (%s,%s,%s,%s,%s,1,%s,%s) ON CONFLICT (chain, token) DO NOTHING",
       (chain, tk, symbol, mcap, mcap, jetzt, jetzt))
    if ergebnis.get("dev"):
        _q("INSERT INTO iw_devs (chain, dev, token, ts) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
           (chain, _k(chain, ergebnis["dev"]), tk, jetzt))
    dev = _k(chain, ergebnis.get("dev") or "")
    dev_gruppe = {_k(chain, w) for c in ergebnis.get("cluster_map") or [] if c.get("funder") == ergebnis.get("dev")
                  for w in c.get("members") or []}
    for i, b in enumerate(ergebnis.get("buyers") or [], 1):
        w = _k(chain, b["wallet"])
        if dev and w == dev:
            continue
        # Bundles und Sniper (gleicher Block, Launch-Block) und die Gruppe des Devs bekommen nie Punkte
        flag = "bundle" if b.get("bundled") else ("dev_group" if w in dev_gruppe else None)
        _q("INSERT INTO iw_early (chain, token, wallet, rank, buy_ts, entry_mcap, flag, src) VALUES (%s,%s,%s,%s,%s,%s,%s,'own') "
           "ON CONFLICT DO NOTHING", (chain, tk, w, i, b.get("ts"), None, flag))


# ── Oeffentliche Wege ────────────────────────────────────────────────────

_LAEUFT = {}

# Seit 2.60.2: Fortschritt des laufenden Tiefen-Scans, damit Telegram zwischendurch sagen kann, was gerade passiert.
SCHRITTE = {"start": "found the launch", "logs": "read every transfer since launch", "codes": "checked which wallets are contracts",
            "funders": "traced who funded the first buyers", "holders": "mapped the holders",
            "evaluate": "scored bundles and snipers", "forwards": "followed forwarded tokens"}


def fortschritt_setzen(chain, token, schritt):
    try:
        storage.kv_set(f"deep_prog_{chain}_{_k(chain, token)}", json.dumps({"step": schritt, "ts": int(time.time())}))
    except Exception:
        pass


def fortschritt(chain, token):
    """-> (fertige Schritte, Gesamtschritte, lesbarer Text) oder None"""
    try:
        d = json.loads(storage.kv_get(f"deep_prog_{chain}_{_k(chain, token)}") or "null")
    except Exception:
        d = None
    if not d or time.time() - d.get("ts", 0) > 900:
        return None
    s = str(d.get("step") or "")
    m = re.match(r"(\w+)(?:\((\d+)\))?", s)
    key = m.group(1) if m else s
    reihe = list(SCHRITTE)
    n = reihe.index(key) + 1 if key in reihe else 1
    text = SCHRITTE.get(key, "working")
    if key == "logs" and m and m.group(2):
        text = f"read {int(m.group(2)):,} transfers since launch"
    return n, len(reihe), text


# Seit 2.60.2: beliebte Coins vorab scannen, damit der Tiefen-Scan beim Nutzer sofort da ist.
VORAB_MAX = int(os.getenv("DEEP_PREWARM_MAX", "2"))


def vorab_waermen(coins):
    """coins: [(chain, token, symbol, mcap)]. Startet hoechstens VORAB_MAX Scans gleichzeitig fuer Coins ohne frischen Cache."""
    laufend = sum(1 for t in _LAEUFT.values() if time.time() - t < 600)
    gestartet = 0
    for chain, token, sym, mcap in coins:
        if laufend + gestartet >= VORAB_MAX:
            break
        c = aus_cache(chain, token)
        alter = time.time() - ((c or {}).get("ts") or 0)
        if c and c.get("measured") and alter < CACHE_S - 5 * 60:
            continue
        if (chain, _k(chain, token)) in _LAEUFT:
            continue
        tiefe(chain, token, mcap, sym, starten=True)
        gestartet += 1
    return gestartet


def _cache_key(chain, token):
    return "iw_scan_" + chain + "_" + _k(chain, token)


def aus_cache(chain, token):
    try:
        d = json.loads(storage.kv_get(_cache_key(chain, token)) or "null")
    except Exception:
        d = None
    return d


def scannen(chain, token, mcap=None, symbol=None):
    t0 = time.time()
    try:
        if chain == "solana":
            erg = _scan_sol(token)
        elif chain in ("robinhood", "base", "ethereum", "arbitrum", "bsc"):
            erg = _scan_evm(chain, token)
        elif chain in ("tron", "ton"):   # seit 2.82.35
            import tronton
            erg = tronton.tief_tron(token) if chain == "tron" else tronton.tief_ton(token)
        else:
            erg = {"measured": False, "why": "chain not covered"}
    except Exception as e:
        log.warning(f"intel scan {chain} {token[:10]}: {e}")
        erg = {"measured": False, "why": "scan failed"}
    erg["took_s"] = round(time.time() - t0, 1)
    log.info(f"intel scan {chain} {token[:10]}: {erg['took_s']}s, measured={erg.get('measured')}")
    erg["ts"] = int(time.time())
    try:
        storage.kv_set(_cache_key(chain, token), json.dumps(erg, default=str))
    except Exception:
        pass
    job = _FUNDER_JOB.pop((chain, str(token).lower()), None)
    if job:
        try:
            threading.Thread(target=_funder_phase, args=(chain, str(token).lower(), erg, job), daemon=True).start()
        except RuntimeError as e:
            log.warning(f"intel funder phase not started: {e}, threads={threading.active_count()}")
    try:
        _merken(chain, token, erg, mcap, symbol)
    except Exception as e:
        log.debug(f"intel merken: {e}")
    try:  # seit 2.63: Wallet-Gedaechtnis ueber alle Coins
        import muster
        muster.merken(chain, token, erg, symbol)
        if mcap:
            muster.letzter(chain, _k(chain, token), mcap)
    except Exception as e:
        log.debug(f"intel muster: {e}")
    return erg


_FUNDER_JOB = {}


def _funder_phase(chain, token, erg, job):
    """Seit 2.78.1: Funder-Netz nach dem fertigen Tiefen-Scan rechnen und in denselben Cache-Eintrag schreiben."""
    t_ = time.time()
    try:
        import funder
        dev, block, logs, sup, kaeufer, zeit, infra = job
        fn = funder.netz(chain, token, dev, block, logs, sup, kaeufer, zeit, infra)
    except Exception as e:
        log.warning(f"intel funder phase {chain} {token[:10]}: {e}")
        fn = {"checked": False, "gaps": ["The funder check did not finish. Run the scan again later."]}
    try:
        c = aus_cache(chain, token) or erg
        if c.get("ts") and erg.get("ts") and c["ts"] < erg["ts"]:
            c = erg
        c["funder_net"] = fn
        storage.kv_set(_cache_key(chain, token), json.dumps(c, default=str))
    except Exception as e:
        log.debug(f"intel funder phase save: {e}")
    log.info(f"intel funder {chain} {token[:10]}: {time.time() - t_:.1f}s, checked={fn.get('checked')}")


def tiefe(chain, token, mcap=None, symbol=None, starten=True):
    """Fertiger Scan aus dem Cache ({'state':'ready', ...}), sonst im Hintergrund starten ({'state':'running'})."""
    c = aus_cache(chain, token)
    alter = time.time() - ((c or {}).get("ts") or 0)
    # Seit 2.58: ein fehlgeschlagener Tiefen-Scan bleibt nur 3 Minuten im Speicher, danach neuer Versuch (vorher 30 Minuten)
    if c and alter < CACHE_S and (c.get("measured") or alter < FEHL_S or not starten):
        return dict(c, state="ready" if c.get("measured") else "unavailable", age_min=int(alter / 60))
    key = (chain, _k(chain, token))
    if key in _LAEUFT and time.time() - _LAEUFT[key] < 600:  # seit 2.59.3: grosse Coins brauchen bis zu ein paar Minuten
        return {"state": "running"}
    if not starten:
        return {"state": "none"}
    _LAEUFT[key] = time.time()

    def lauf():
        t_ = time.time()
        try:
            scannen(chain, token, mcap, symbol)
        finally:
            _LAEUFT.pop(key, None)
            log.info(f"intel deep {chain} {str(token)[:8]}: {time.time() - t_:.1f}s total")
            # Seit 2.51.1: Ergebnis sofort an wartende Chats, nicht erst beim naechsten Takt
            try:
                import motor
                motor.scan_sofort(chain, token)
            except Exception as e:
                log.debug(f"intel deep sofort: {e}")
    try:
        threading.Thread(target=lauf, daemon=True).start()
    except RuntimeError as e:
        # Seit 2.80.2: kein Thread mehr frei. Vorher blieb der Coin 10 Minuten als "running" haengen, ohne dass etwas lief.
        _LAEUFT.pop(key, None)
        log.warning(f"intel deep {chain} {str(token)[:8]}: could not start ({e}), threads={threading.active_count()}")
        return {"state": "unavailable", "why": "busy, try again in a minute"}
    return {"state": "running"}


def wallet(adr):
    a = str(adr or "").strip()
    if not a:
        return {"error": "Which wallet?"}
    chain_k = a.lower() if a.startswith("0x") else a
    rows = _q("SELECT chain, score, label, winners, calls, best_x, total_x, win_rate, avg_entry_mcap, source FROM iw_wallets "
              "WHERE wallet=%s ORDER BY score DESC", (chain_k,)) or []
    frueh = _q("SELECT e.chain, e.token, t.symbol, t.multiple, e.rank FROM iw_early e LEFT JOIN iw_tokens t "
               "ON t.chain=e.chain AND t.token=e.token WHERE e.wallet=%s ORDER BY COALESCE(t.multiple,0) DESC LIMIT 15", (chain_k,)) or []
    if not rows and not frueh:
        return {"wallet": a, "known": False,
                "meaning": "Not in our scored set. We have not seen it early in a coin that ran. That does not make it bad."}
    rang = None
    if rows:
        r = _q("SELECT COUNT(*) FROM iw_wallets WHERE score > %s", (rows[0][1],), eins=True)
        gesamt = _q("SELECT COUNT(*) FROM iw_wallets", (), eins=True)
        rang = {"rank": (r[0] + 1) if r else None, "of": gesamt[0] if gesamt else None}
    return {"wallet": a, "known": bool(rows), "standing": rang,
            "scores": [{"chain": x[0], "score": x[1], "label": x[2], "winners": x[3], "calls": x[4], "best_x": x[5],
                        "win_rate": x[7]} for x in rows],
            "early_in": [{"chain": x[0], "token": x[1], "symbol": x[2], "token_multiple": round(x[3], 1) if x[3] else None,
                          "buyer_rank": x[4]} for x in frueh]}


def laeufer(stunden=72, n=10):
    seit = int(time.time() - float(stunden or 72) * 3600)
    rows = _q("SELECT chain, token, symbol, first_mcap, peak_mcap, multiple, first_seen FROM iw_tokens "
              "WHERE first_seen >= %s AND multiple >= 2 AND NOT EXISTS (SELECT 1 FROM iw_early s WHERE s.chain=iw_tokens.chain "
              "AND s.token=iw_tokens.token AND s.src='scout') ORDER BY multiple DESC LIMIT %s", (seit, int(n or 10))) or []
    return {"runners": [{"chain": r[0], "token": r[1], "symbol": r[2], "mcap_when_first_checked": r[3], "peak_mcap": r[4],
                         "multiple": round(r[5], 1) if r[5] else None} for r in rows],
            "meaning": "Coins someone checked with Your Engine that went on to run, with the multiple from the first check to the peak."}


# ── Nachverfolgen und bewerten ───────────────────────────────────────────

def _punkte(winners, best_x, total_x, calls):
    """0 bis 99: oft frueh in Gewinnern, und wie gross die Gewinner waren."""
    if not winners:
        return 0
    s = 35 + 16 * math.log2(1 + winners) + 9 * math.log10(max(1.0, best_x)) + 4 * math.log10(max(1.0, total_x))
    if calls and winners / calls < 0.15:
        s -= 10
    return int(max(1, min(99, round(s))))


HALBWERT_TAGE = float(os.getenv("INTEL_HALF_LIFE_DAYS", "30"))


def _punkte2(a_w, w_w, wins, best_x):
    """Seit 2.45: echte Trefferquote statt nur Treffer. a_w und w_w sind zeitgewichtete Versuche und Treffer
    (alte zaehlen weniger). Ein einzelner Glueckstreffer bleibt unter 36, erst wiederholte Treffer zaehlen."""
    if wins < 1 or a_w <= 0:
        return 0
    quote = (w_w + 1) / (a_w + 6)          # vorsichtige Quote: wenige Versuche zaehlen nicht als 100 %
    s = 20 + 250 * (quote - 0.15) + 5 * math.log2(1 + w_w) + 6 * math.log10(max(1.0, best_x))
    if wins < 2 or w_w < 1:
        s = min(s, 35)  # ein Treffer oder nur alte Treffer: nie auf der Beobachtungsliste
    return int(max(1, min(99, round(s))))


def neu_bewerten():
    """Alle eigenen Bewertungen neu berechnen (stuendlich): Versuche = jeder Coin, bei dem das Wallet unter den
    ersten 30 Kaeufern war und dessen Ausgang feststeht (aelter als 24 h). Nieten zaehlen mit, Bundles, Sniper und
    die Gruppe des Devs zaehlen nie, alte Coins verlieren mit der Halbwertszeit INTEL_HALF_LIFE_DAYS an Gewicht.
    Importierte Wallets behalten ihren Import-Score, bis genug eigene Daten da sind (5 Versuche)."""
    jetzt = int(time.time())
    rows = _q("""SELECT e.chain, e.wallet,
                   SUM(power(0.5, GREATEST(0, %s - t.first_seen) / 86400.0 / %s)),
                   SUM(CASE WHEN t.multiple >= %s THEN power(0.5, GREATEST(0, %s - t.first_seen) / 86400.0 / %s) ELSE 0 END),
                   COUNT(*), COUNT(*) FILTER (WHERE t.multiple >= %s), MAX(t.multiple)
                 FROM iw_early e JOIN iw_tokens t ON t.chain = e.chain AND t.token = e.token
                 WHERE e.rank <= 30 AND e.flag IS NULL AND COALESCE(e.src, 'own') = 'own' AND t.first_seen < %s
                 GROUP BY e.chain, e.wallet
                 HAVING COUNT(*) FILTER (WHERE t.multiple >= %s) >= 1
                    OR e.wallet IN (SELECT wallet FROM iw_wallets WHERE score > 0)""",
              (jetzt, HALBWERT_TAGE, LAUF_X, jetzt, HALBWERT_TAGE, LAUF_X, jetzt - 86400, LAUF_X)) or []
    n = 0
    for chain, w, a_w, w_w, a, wins, best in rows:
        eigen = _punkte2(float(a_w or 0), float(w_w or 0), int(wins or 0), float(best or 0))
        alt = _q("SELECT import_score FROM iw_wallets WHERE wallet=%s AND chain=%s", (w, chain), eins=True)
        imp = int(alt[0] or 0) if alt and alt[0] is not None else None
        neu = eigen if (imp is None or a >= 5) else max(eigen, imp)
        _q("INSERT INTO iw_wallets (wallet, chain, score, winners, calls, best_x, win_rate, updated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
           "ON CONFLICT (wallet, chain) DO UPDATE SET score=EXCLUDED.score, winners=EXCLUDED.winners, calls=EXCLUDED.calls, "
           "best_x=GREATEST(iw_wallets.best_x, EXCLUDED.best_x), win_rate=EXCLUDED.win_rate, updated=EXCLUDED.updated",
           (w, chain, neu, int(wins or 0), int(a or 0), float(best or 0), round(int(wins or 0) / max(1, int(a or 0)), 3), jetzt))
        n += 1
    log.info(f"intel: rescored {n} wallets")
    return n


def import_automatisch():
    """Seit 2.82.53: Ist INTEL_IMPORT_URL gesetzt, holt die Engine die FINNPUTER-Daten (Wallet-Bewertungen, Runner,
    fruehe Kaeufer) alle INTEL_IMPORT_EVERY_H Stunden neu (Standard 24, 0 = nur von Hand). Nur lesen, Upsert."""
    url = os.getenv("INTEL_IMPORT_URL", "").strip()
    try:
        alle_h = float(os.getenv("INTEL_IMPORT_EVERY_H", "24") or 24)
    except Exception:
        alle_h = 24
    if not url or alle_h <= 0 or import_laeuft():
        return False
    try:
        st = json.loads(storage.kv_get("iw_import") or "null") or {}
    except Exception:
        st = {}
    if st and time.time() - float(st.get("start") or 0) < alle_h * 3600:
        return False
    storage.kv_set("iw_import", json.dumps({"start": int(time.time()), "ts": int(time.time()), "phase": "queued",
                                            "wallets": 0, "tokens": 0, "early": 0, "done": False, "error": None, "auto": True}))
    threading.Thread(target=importieren, args=(url,), daemon=True, name="intel-import-auto").start()
    log.info("intel import: automatic refresh started")
    return True


def verfolgen(max_n=60):
    """Coins aus den letzten 14 Tagen nachsehen; ab LAUF_X die fruehen Kaeufer bewerten."""
    try:
        import_automatisch()
    except Exception as e:
        log.debug(f"intel import auto: {e}")
    seit = int(time.time() - 14 * 86400)
    rows = _q("SELECT chain, token, first_mcap, peak_mcap, credited FROM iw_tokens WHERE first_seen >= %s "
              "AND first_mcap > 0 ORDER BY COALESCE(checked,0) ASC LIMIT %s", (seit, int(max_n))) or []
    import scan
    je_chain = {}
    for r in rows:
        je_chain.setdefault(r[0], []).append(r)
    for chain, liste in je_chain.items():
        dex = scan.KETTEN.get(chain, (None, None))[1]
        for i in range(0, len(liste), 30):
            teil = liste[i:i + 30]
            try:
                d = requests.get(f"https://api.dexscreener.com/tokens/v1/{dex}/" + ",".join(x[1] for x in teil), headers=UA, timeout=15).json()
            except Exception:
                continue
            mc = {}
            for p in d if isinstance(d, list) else []:
                a = _k(chain, (p.get("baseToken") or {}).get("address") or "")
                v = float(p.get("marketCap") or p.get("fdv") or 0)
                mc[a] = max(mc.get(a, 0), v)
            jetzt = int(time.time())
            for (c, tok, first, peak, credited) in teil:
                v = mc.get(tok)
                if not v:
                    _q("UPDATE iw_tokens SET checked=%s WHERE chain=%s AND token=%s", (jetzt, c, tok))
                    continue
                try:
                    import muster
                    muster.letzter(c, tok, v)
                except Exception:
                    pass
                peak = max(peak or 0, v)
                mult = peak / first if first else 1
                _q("UPDATE iw_tokens SET peak_mcap=%s, multiple=%s, checked=%s WHERE chain=%s AND token=%s",
                   (peak, mult, jetzt, c, tok))
                if mult >= LAUF_X and not credited:
                    _gutschreiben(c, tok, mult)


def _gutschreiben(chain, token, mult):
    frueh = _q("SELECT wallet FROM iw_early WHERE chain=%s AND token=%s AND rank <= 30 AND flag IS NULL", (chain, token)) or []
    jetzt = int(time.time())
    for (w,) in frueh:
        alt = _q("SELECT winners, calls, best_x, total_x, score FROM iw_wallets WHERE wallet=%s AND chain=%s", (w, chain), eins=True)
        win, calls, best, total, score = alt if alt else (0, 0, 0.0, 0.0, 0)
        win, calls, best, total = (win or 0) + 1, (calls or 0) + 1, max(best or 0, mult), (total or 0) + mult
        neu = max(score or 0, _punkte(win, best, total, calls))
        _q("INSERT INTO iw_wallets (wallet, chain, score, winners, calls, best_x, total_x, updated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
           "ON CONFLICT (wallet, chain) DO UPDATE SET score=EXCLUDED.score, winners=EXCLUDED.winners, calls=EXCLUDED.calls, "
           "best_x=EXCLUDED.best_x, total_x=EXCLUDED.total_x, updated=EXCLUDED.updated",
           (w, chain, neu, win, calls, best, total, jetzt))
    _q("UPDATE iw_tokens SET credited=TRUE WHERE chain=%s AND token=%s", (chain, token))
    log.info(f"intel: {chain} {token[:10]} ran {mult:.1f}x, {len(frueh)} early wallets credited")


# ── Einmalige Uebernahme einer Wallet-Datenbank ─────────────────────────

def import_laeuft():
    try:
        st = json.loads(storage.kv_get("iw_import") or "null") or {}
    except Exception:
        st = {}
    return bool(st and not st.get("done") and not st.get("error") and time.time() - st.get("ts", 0) < 120)


def _import_schreiben(sql, zeilen):
    """Ein Stapel in einem Rutsch statt Zeile fuer Zeile."""
    pg = _pg()
    if not pg or not zeilen:
        return
    with _LOCK:
        with pg.cursor() as c:
            c.executemany(sql, zeilen)


def _sek(v):
    """Zeitpunkt aus der Fremd-Datenbank als Unix-Sekunden (Zahl, Millisekunden oder datetime)."""
    if v is None:
        return None
    try:
        if hasattr(v, "timestamp"):
            return int(v.timestamp())
        v = float(v)
        return int(v / 1000) if v > 1e11 else int(v)
    except Exception:
        return None


def importieren(url):
    """Liest wallet_scores, runners und smart_wallets einer anderen Postgres-Datenbank (nur lesen) und
    uebernimmt sie in die eigenen Tabellen. Fortschritt im kv unter iw_import, mit Herzschlag (ts) und Phase.
    Seit 2.49.1: lesende Transaktion statt autocommit (benannte Cursor brauchen eine Transaktion),
    Stapel-Schreiben, klare Fehlermeldung bei internen Railway-Adressen."""
    import psycopg
    stand = {"start": int(time.time()), "ts": int(time.time()), "phase": "connecting", "wallets": 0, "tokens": 0,
             "early": 0, "done": False, "error": None}

    def melden(phase=None):
        if phase:
            stand["phase"] = phase
        stand["ts"] = int(time.time())
        storage.kv_set("iw_import", json.dumps(stand))
    melden()
    url = str(url or "").strip()
    if ".railway.internal" in url:
        stand["error"] = ("INTEL_IMPORT_URL uses a private Railway address (railway.internal). That only works inside the same "
                          "Railway project. Use the public one: in the source database open Variables and copy DATABASE_PUBLIC_URL.")
        stand["end"] = int(time.time())
        melden()
        return stand
    try:
        src = psycopg.connect(url, connect_timeout=15)
        src.read_only = True
        jetzt = int(time.time())
        log.info("intel import: connected")
        with src.transaction():
            melden("wallets")
            cur = src.cursor(name="iw_w")
            cur.itersize = 2000
            cur.execute("SELECT wallet, COALESCE(chain,'solana'), puter_score, label, winners, calls, best_call, total_call, "
                        "win_rate, avg_entry_mcap FROM wallet_scores")
            sql = ("INSERT INTO iw_wallets (wallet, chain, score, label, winners, calls, best_x, total_x, win_rate, avg_entry_mcap, source, updated, import_score) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'import',%s,%s) ON CONFLICT (wallet, chain) DO UPDATE SET "
                   "import_score=EXCLUDED.import_score, score=GREATEST(iw_wallets.score, EXCLUDED.score), winners=GREATEST(iw_wallets.winners, EXCLUDED.winners), "
                   "calls=GREATEST(iw_wallets.calls, EXCLUDED.calls), best_x=GREATEST(iw_wallets.best_x, EXCLUDED.best_x), "
                   "total_x=GREATEST(iw_wallets.total_x, EXCLUDED.total_x), win_rate=EXCLUDED.win_rate, "
                   "avg_entry_mcap=EXCLUDED.avg_entry_mcap, label=EXCLUDED.label, updated=EXCLUDED.updated")
            while True:
                rows = cur.fetchmany(2000)
                if not rows:
                    break
                _import_schreiben(sql, [(_k(r[1] or "solana", r[0]), r[1] or "solana", int(r[2] or 0), r[3], r[4] or 0, r[5] or 0,
                                         r[6] or 0, r[7] or 0, r[8], r[9], jetzt, int(r[2] or 0)) for r in rows if r[0]])
                stand["wallets"] += len(rows)
                melden()
            cur.close()
            melden("coins")
            cur = src.cursor(name="iw_t")
            cur.execute("SELECT mint, COALESCE(chain,'solana'), symbol, first_mcap, peak_mcap, multiple, detected_at FROM runners")
            sql = ("INSERT INTO iw_tokens (chain, token, symbol, first_mcap, peak_mcap, multiple, first_seen, checked, credited) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT (chain, token) DO UPDATE SET "
                   # seit 2.82.7: ein neuer Import zieht hoehere Peaks nach (vorher blieb der alte Stand stehen)
                   "symbol=COALESCE(iw_tokens.symbol, EXCLUDED.symbol), "
                   "peak_mcap=GREATEST(COALESCE(iw_tokens.peak_mcap,0), COALESCE(EXCLUDED.peak_mcap,0)), "
                   "multiple=GREATEST(COALESCE(iw_tokens.multiple,0), COALESCE(EXCLUDED.multiple,0))")
            while True:
                rows = cur.fetchmany(2000)
                if not rows:
                    break
                _import_schreiben(sql, [(r[1] or "solana", _k(r[1] or "solana", r[0]), r[2], r[3], r[4], r[5], r[6], jetzt)
                                        for r in rows if r[0]])
                stand["tokens"] += len(rows)
                melden()
            cur.close()
        melden("early buys")
        try:
            with src.transaction():
                cur = src.cursor(name="iw_e")
                cur.execute("SELECT sw.wallet, sw.mint, COALESCE(r.chain,'solana'), sw.rank, sw.buy_time, sw.entry_mcap "
                            "FROM smart_wallets sw LEFT JOIN runners r ON r.mint = sw.mint")
                sql = ("INSERT INTO iw_early (chain, token, wallet, rank, buy_ts, entry_mcap, src) VALUES (%s,%s,%s,%s,%s,%s,'import') "
                       "ON CONFLICT (chain, token, wallet) DO UPDATE SET "
                       "entry_mcap=COALESCE(iw_early.entry_mcap, EXCLUDED.entry_mcap), buy_ts=COALESCE(iw_early.buy_ts, EXCLUDED.buy_ts)")
                while True:
                    rows = cur.fetchmany(5000)
                    if not rows:
                        break
                    _import_schreiben(sql, [(r[2] or "solana", _k(r[2] or "solana", r[1]), _k(r[2] or "solana", r[0]), r[3], _sek(r[4]), r[5])
                                            for r in rows if r[0] and r[1]])
                    stand["early"] += len(rows)
                    melden()
                cur.close()
        except Exception as e:
            stand["early_note"] = str(e)[:160]
        src.close()
        stand["done"] = True
        log.info(f"intel import done: {stand['wallets']} wallets, {stand['tokens']} coins, {stand['early']} early buys")
    except Exception as e:
        stand["error"] = str(e)[:240]
        log.warning(f"intel import failed: {stand['error']}")
    stand["end"] = int(time.time())
    melden("finished")
    return stand


def stand():
    try:
        imp = json.loads(storage.kv_get("iw_import") or "null")
    except Exception:
        imp = None
    w = _q("SELECT COUNT(*), COUNT(*) FILTER (WHERE score > 0), COUNT(*) FILTER (WHERE source='own') FROM iw_wallets", (), eins=True)
    t = _q("SELECT COUNT(*), COUNT(*) FILTER (WHERE multiple >= %s) FROM iw_tokens", (LAUF_X,), eins=True)
    e = _q("SELECT COUNT(*) FROM iw_early", (), eins=True)
    return {"wallets": w[0] if w else 0, "scored": w[1] if w else 0, "own": w[2] if w else 0,
            "tokens": t[0] if t else 0, "runners": t[1] if t else 0, "early_rows": e[0] if e else 0,
            "import": imp, "helius": bool(os.getenv("HELIUS_API_KEY", "").strip())}


_BS_STAT = {"ts": 0.0, "d": None}


def bs_status():
    """Seit 2.77.2: Ist der Blockscout-Schluessel da und antwortet er je Chain? Ohne den Schluessel selbst zu zeigen.
    10 Minuten Cache. Fuer /v1/status/blockscout."""
    if _BS_STAT["d"] and time.time() - _BS_STAT["ts"] < 600:
        return _BS_STAT["d"]
    key = os.getenv("BLOCKSCOUT_API_KEY", "").strip()
    probe = "0x000000000000000000000000000000000000dead"
    out = {"key_set": bool(key), "chains": {}}
    for ch, cid in BS_IDS.items():
        z = {}
        if key:
            try:
                r = requests.get(f"https://api.blockscout.com/{cid}/api/v2/addresses/{probe}/token-balances",
                                 params={"apikey": key}, headers=UA, timeout=20)
                z["pro"] = r.status_code
                if r.status_code != 200:
                    z["pro_message"] = re.sub(r"[A-Za-z0-9_-]{24,}", "…", r.text[:160])
            except Exception as e:
                z["pro"] = f"error {str(e)[:60]}"
        if BLOCKSCOUT.get(ch):
            try:
                r = requests.get(BLOCKSCOUT[ch] + f"/api/v2/addresses/{probe}/token-balances", headers=UA, timeout=20)
                z["free"] = r.status_code if "Just a moment" not in r.text[:400] else "cloudflare"
            except Exception as e:
                z["free"] = f"error {str(e)[:60]}"
        out["chains"][ch] = z
    out["checked"] = int(time.time())
    _BS_STAT.update(ts=time.time(), d=out)
    return out
