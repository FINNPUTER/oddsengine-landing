"""
lplock.py: fremde Locker und Uniswap-V3/V4-Positionen auf Robinhood Chain, seit 2.71.0.

Zwei Fragen fuer den Scan:
  token_locks(token, dez, sup)   Tokens in Lockern, die nicht per balanceOf an einer festen Adresse liegen.
                                 Titan Locker legt jeden Lock in einen eigenen Vertrag; der Manager kennt sie alle.
  lp_status(token)               Wem gehoeren die Liquiditaets-Positionen (NFTs) in den V3/V4-Pools des Tokens?
                                 Locker mit Datum (Titan, HoodLock), Launchpad-Locker, verbrannt, Wallet, sonstiger Vertrag.

Alles nur Lese-Aufrufe am oeffentlichen RPC (Multicall, eth_getLogs in Fenstern unter 10 Mio. Bloecken).
Ergebnisse 20 Minuten im Speicher. Keine Schaetzung: was wir nicht zuordnen koennen, heisst "other contract".

Adressen (alle auf Robinhood Chain 4663 geprueft, Quelle: Titan-Doku, HoodLock-Seite, Uniswap deployments.json, Blockscout):
"""
import time

from logger import log

# Titan Locker: Manager V2.1 (aktuell), V2 (alt, Locks bleiben abholbar), Titan Vault (titan.fun Launches)
TITAN = {"0x102a70bda2c833b3483a2ee55c14c7ea0fb7a01b": "Titan Locker",
         "0x26b0654a0756dcd036d4e7215324f3d2be34d79e": "Titan Locker",
         "0x4e907349fa31f9e42f0bf0e2e2fd09e6e1e2db9d": "Titan Vault"}
# HoodLock: LP-Locker fuer V3/V4-Positionen (eigene Lock-Liste, kein Abruf je Token)
HOODLOCK_LP = "0x91aa2da1956c77f8448e18dcaf53383bcc658e67"
# Vertraege von Launchpads, die die Start-Liquiditaet als NFT halten (Namen aus dem verifizierten Code auf Blockscout).
# Ob und wann sie freigeben, steht nicht einheitlich fest, darum nie als "gesperrt bis" gezeigt, nur als Launchpad-Lock.
LAUNCHPAD = {"0x736d76699c26d0d966744cae304c000d471f7f35": "Pons",
             "0x267444d099b10fb5ed7c3cc7b7c767adca574952": "Pons",
             "0x7f03effbd7ceb22a3f80dd468f67ef27826acd85": "LaunchLocker",
             "0x99b79154ff4fc0e313549b809254b02722631ee0": "LaunchHood",
             "0x173d8370b4f67535d406f2f46168ec48aa03d26e": "LauncherLocker",
             "0x290f735f63824bb5836cde24a35f5103a5b5bc99": "Clanker",
             "0x5826fbb6201daacd924a3d292841da9142952d59": "PairPad",
             "0xeff166aaf189323c58dc27ed1206eb2c37faacdf": "Uniswap Launchpad",
             "0x7891ea889899928afe637e4cca8106188427776a": "Torch"}
TOT = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}

V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
V3_NPM = "0x73991a25c818bf1f1128deaab1492d45638de0d3"
V4_POOLMANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_POSM = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
V4_STATEVIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"

T_MINT = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
T_INCLIQ = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
T_MODLIQ = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
T_POOLCREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
T_INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"

FENSTER = 9_900_000
MAX_POS = 400
_C = {}


def _oc():
    import onchain
    return onchain


def _w(b, i):
    return int.from_bytes(b[32 * i:32 * i + 32], "big")


def _adr(b, i):
    return "0x" + b[32 * i + 12:32 * i + 32].hex()


def _int24(v):
    v &= 0xFFFFFF
    return v - (1 << 24) if v >= (1 << 23) else v


def _pad_adr(a):
    return a[2:].rjust(64, "0")


def _i24hex(v):
    return hex(v % (1 << 256))[2:].rjust(64, "0")


def _cache(k, f, ttl=1200):
    c = _C.get(k)
    if c and time.time() - c[0] < ttl:
        return c[1]
    v = f()
    _C[k] = (time.time(), v)
    if len(_C) > 3000:
        for kk in sorted(_C, key=lambda x: _C[x][0])[:1000]:
            _C.pop(kk, None)
    return v


# ── Titan ───────────────────────────────────────────────────────────────

def _titan(token):
    """Alle Titan-Locks, die diesen Token betreffen (Tokens, Vesting und Positionen mit ihm als token0/token1)."""
    oc = _oc()
    ids = []
    r = oc._multicall([(m, "0x83a1364a" + oc._pad(token)) for m in TITAN])
    for m, b in zip(TITAN, r):
        if not b or len(b) < 64:
            continue
        n = _w(b, 1)  # Antwort uint40[]: Offset, Laenge, Eintraege
        for k in range(max(0, n - 300), n):
            ids.append((m, _w(b, 2 + k)))
    if not ids:
        return []
    d = oc._multicall([(m, "0xa6817117" + hex(i)[2:].rjust(64, "0")) for m, i in ids])
    out = []
    for (m, i), b in zip(ids, d):
        if not b or len(b) < 320:
            continue
        out.append({"manager": m, "locker": TITAN[m], "id": i, "kind": _w(b, 0), "contract": _adr(b, 2), "owner": _adr(b, 3),
                    "asset": _adr(b, 4), "token_id": _w(b, 5), "unlock": _w(b, 8), "balance": _w(b, 9)})
    return out


def token_locks(token, dez, sup):
    """Titan-Token- und Vesting-Locks dieses Tokens, die noch etwas halten. Je Lock-Vertrag ein Eintrag."""
    token = token.lower()
    try:
        alle = _cache(("titan", token), lambda: _titan(token))
    except Exception as e:
        log.debug(f"lplock titan {token[:10]}: {e}")
        return []
    jetzt = time.time()
    out = []
    for x in alle:
        if x["kind"] not in (0, 3) or x["asset"] != token or not x["balance"] or not sup:
            continue
        pct = round(x["balance"] / 10 ** dez / sup * 100, 3)
        if pct <= 0:
            continue
        e = {"address": x["contract"], "locker": x["locker"], "pct": pct}
        if x["unlock"] > jetzt:
            e["unlock_ts"] = x["unlock"]
        else:
            e["unlocked"] = True  # Frist abgelaufen, Tokens liegen noch drin und sind abholbar
        out.append(e)
    return sorted(out, key=lambda e: -e["pct"])[:20]


# ── HoodLock LP ─────────────────────────────────────────────────────────

def _hoodlock_lp():
    oc = _oc()
    n = _w(oc._multicall([(HOODLOCK_LP, "0x6518a0b3")])[0] or b"\0" * 32, 0)
    ids = list(range(max(0, n - 3000), n))
    r = oc._multicall([(HOODLOCK_LP, "0xd68f4dd1" + hex(i)[2:].rjust(64, "0")) for i in ids]) if ids else []
    out = {}
    for i, b in zip(ids, r):
        if not b or len(b) < 320:
            continue
        # owner, unlockTime, collectFeeBps, isV4, hookVerified, withdrawn, feeRecipient, token0, token1, tokenId
        if _w(b, 5):
            continue
        out[(bool(_w(b, 3)), _w(b, 9))] = {"unlock": _w(b, 1), "id": i}
    return out


# ── Positionen finden ───────────────────────────────────────────────────

def _logs(adresse, topics, ab, bis):
    """eth_getLogs von Block ab bis bis, in Fenstern unter 10 Mio. Bloecken (Grenze des oeffentlichen RPC)."""
    oc = _oc()
    fenster = [(x, min(bis, x + FENSTER)) for x in range(ab, bis + 1, FENSTER + 1)]
    res = oc._rpc_batch([("eth_getLogs", [{"address": adresse, "topics": topics, "fromBlock": hex(a), "toBlock": hex(z)}])
                         for a, z in fenster], n=6)
    out = []
    for (a, z), r in zip(fenster, res):
        for versuch in range(3):
            if r is not None:
                break
            time.sleep(0.6 * (versuch + 1))
            try:
                r = oc._rpc("eth_getLogs", [{"address": adresse, "topics": topics, "fromBlock": hex(a), "toBlock": hex(z)}])
            except Exception:
                r = None
        if r is None:
            raise RuntimeError("logs incomplete")
        out += r
    return out


def _gesammelt(schluessel, abfragen, auswerten, max_n=2000):
    """Werte aus Ereignissen sammeln und mit dem letzten gelesenen Block in kv merken.
    Beim ersten Mal die ganze Kette, danach nur neue Bloecke. abfragen: [(adresse, topics)], auswerten(logs) -> Werte."""
    import json
    import storage
    try:
        st = json.loads(storage.kv_get("lpl_" + schluessel) or "null") or {}
    except Exception:
        st = {}
    werte = set(st.get("w") or [])
    ab = int(st.get("bis") or -1) + 1
    bis = _oc()._top()
    if ab <= bis:
        logs = []
        for adresse, topics in abfragen:
            logs += _logs(adresse, topics, ab, bis)
        werte |= set(auswerten(logs))
        try:
            storage.kv_set("lpl_" + schluessel, json.dumps({"bis": bis, "w": sorted(werte)[-max_n:]}))
        except Exception:
            pass
    return werte


def _pools(token):
    """Alle V3-Pools (PoolCreated der Fabrik) und V4-Pools (Initialize im PoolManager), in denen der Token steckt.
    Beide Ereignisse fuehren die Tokens als indexierte Felder, darum je Seite eine Abfrage, egal gegen welche Waehrung."""
    t = "0x" + "0" * 24 + token[2:]

    def v3(logs):
        return ["0x" + l["data"][2 + 64 + 24:2 + 128] for l in logs if len(l.get("data") or "") >= 2 + 128]

    def v4(logs):
        return [l["topics"][1] + ":0x" + l["topics"][2][-40:] for l in logs]  # Pool-Id und currency0
    p3 = _gesammelt("v3p_" + token, [(V3_FACTORY, [T_POOLCREATED, t]), (V3_FACTORY, [T_POOLCREATED, None, t])], v3, 50)
    p4 = _gesammelt("v4q_" + token, [(V4_POOLMANAGER, [T_INIT, None, t]), (V4_POOLMANAGER, [T_INIT, None, None, t])], v4, 50)
    return sorted(p3), sorted(p4)


def _v3_ids(pool):
    """tokenIds der NFT-Positionen eines V3-Pools: Mint mit owner = Positions-Manager, im selben Beleg IncreaseLiquidity."""
    oc = _oc()

    def ids(logs):
        txs = sorted({l["transactionHash"] for l in logs})[-MAX_POS:]
        rc = oc._rpc_batch([("eth_getTransactionReceipt", [t]) for t in txs], n=20)
        out = []
        for r in rc:
            for lg in (r or {}).get("logs") or []:
                if str(lg.get("address") or "").lower() == V3_NPM and lg.get("topics") and lg["topics"][0] == T_INCLIQ:
                    out.append(int(lg["topics"][1], 16))
        return out
    return _gesammelt("v3i_" + pool, [(pool, [T_MINT, "0x" + "0" * 24 + V3_NPM[2:]])], ids)


def _v4_ids(pool_id):
    """Positionen eines V4-Pools aus ModifyLiquidity. Ueber den Positions-Manager: "n:<tokenId>" (salt = tokenId).
    Direkt von einem Vertrag (z.B. die Torch-Fabrik bei der Graduation): "d:<sender>:<tickLower>:<tickUpper>:<salt>"."""
    def ids(logs):
        out = []
        for l in logs:
            d = (l.get("data") or "0x")[2:]
            if len(d) < 256:
                continue
            sender = "0x" + l["topics"][2][-40:]
            if sender == V4_POSM:
                out.append("n:%d" % int(d[192:256], 16))
            else:
                out.append("d:%s:%d:%d:%s" % (sender, _int24(int(d[0:64], 16)), _int24(int(d[64:128], 16)), d[192:256]))
        return out
    return _gesammelt("v4j_" + pool_id, [(V4_POOLMANAGER, [T_MODLIQ, pool_id])], ids, 3000)


def _mengen(liq, tl, tu, sqrtp):
    """Token-Mengen einer Position aus Liquiditaet, Tick-Grenzen und Preis (roh, als float)."""
    sa, sb, sp = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2), sqrtp / 2 ** 96
    if sp <= sa:
        return liq * (sb - sa) / (sa * sb), 0.0
    if sp < sb:
        return liq * (sb - sp) / (sp * sb), liq * (sp - sa)
    return 0.0, liq * (sb - sa)


def _rangfolge(oc, v3, v4, n=8):
    """Seit 2.77.0: die Pools mit der meisten Liquiditaet zuerst. Vorher wurden die ersten 6 nach Alphabet gelesen;
    hatte ein Token viele kleine Pools (ODDS: 18 V4-Pools), fiel der echte Pool raus und das Ergebnis war falsch."""
    def nach_liq(liste, aufruf):
        if len(liste) <= n:
            return liste
        r = oc._multicall([aufruf(x) for x in liste])
        bew = [(_w(b, 0) if b else 0, x) for b, x in zip(r, liste)]
        return [x for lq, x in sorted(bew, key=lambda z: -z[0])][:n]
    v3 = nach_liq(v3, lambda pool: (pool, "0x1a686502"))
    v4 = nach_liq(v4, lambda e: (V4_STATEVIEW, "0xfa6793d5" + e.split(":")[0][2:]))
    return v3, v4


def _positionen(token):
    oc = _oc()
    v3, v4 = _pools(token)
    v3, v4 = _rangfolge(oc, v3, v4)
    pos = []
    # V3
    for pool in v3:
        try:
            ids = sorted(_v3_ids(pool))[-MAX_POS:]
        except Exception as e:
            log.debug(f"lplock v3 ids {pool[:10]}: {e}")
            continue
        if not ids:
            continue
        kopf = oc._multicall([(pool, "0x3850c7bd"), (pool, "0x0dfe1681")])
        if not kopf[0] or not kopf[1]:
            continue
        sqrtp, t0 = _w(kopf[0], 0), _adr(kopf[1], 0)
        auf = []
        for i in ids:
            h = hex(i)[2:].rjust(64, "0")
            auf += [(V3_NPM, "0x99fbab88" + h), (V3_NPM, "0x6352211e" + h)]
        r = oc._multicall(auf)
        for k, i in enumerate(ids):
            b, o = r[2 * k], r[2 * k + 1]
            if not b or len(b) < 256:
                continue
            liq = _w(b, 7)
            if not liq:
                continue
            a0, a1 = _mengen(liq, _int24(_w(b, 5)), _int24(_w(b, 6)), sqrtp)
            pos.append({"v": 3, "id": i, "pool": pool, "owner": _adr(o, 0) if o else None,
                        "menge": a0 if t0 == token else a1})
    # V4
    for eintrag in v4:
        pid, c0 = (eintrag.split(":") + [""])[:2]
        try:
            alle = sorted(_v4_ids(pid))
        except Exception as e:
            log.debug(f"lplock v4 ids {pid[:10]}: {e}")
            continue
        nft = sorted({int(x[2:]) for x in alle if x.startswith("n:")})[-MAX_POS:]
        direkt = sorted({x for x in alle if x.startswith("d:")})[-MAX_POS:]
        if not nft and not direkt:
            continue
        s0 = oc._multicall([(V4_STATEVIEW, "0xc815641c" + pid[2:])])[0]
        if not s0:
            continue
        sqrtp = _w(s0, 0)
        seite = 0 if c0 == token else 1
        auf = []
        for i in nft:
            h = hex(i)[2:].rjust(64, "0")
            auf += [(V4_POSM, "0x1efeed33" + h), (V4_POSM, "0x7ba03aad" + h), (V4_POSM, "0x6352211e" + h)]
        for x in direkt:
            _, snd, tl, tu, salt = x.split(":")
            auf.append((V4_STATEVIEW, "0xdacf1d2f" + pid[2:] + _pad_adr(snd) + _i24hex(int(tl)) + _i24hex(int(tu)) + salt))
        r = oc._multicall(auf) if auf else []
        for k, i in enumerate(nft):
            lq, b, o = r[3 * k], r[3 * k + 1], r[3 * k + 2]
            if not lq or not b or len(b) < 192:
                continue
            liq = _w(lq, 0)
            if not liq:
                continue
            info = _w(b, 5)
            m = _mengen(liq, _int24(info >> 8), _int24(info >> 32), sqrtp)
            pos.append({"v": 4, "id": i, "pool": pid, "owner": _adr(o, 0) if o else None, "menge": m[seite]})
        for k, x in enumerate(direkt):
            b = r[3 * len(nft) + k]
            liq = _w(b, 0) if b else 0
            if not liq:
                continue
            _, snd, tl, tu, salt = x.split(":")
            m = _mengen(liq, int(tl), int(tu), sqrtp)
            pos.append({"v": 4, "id": None, "pool": pid, "owner": snd, "direct": True, "menge": m[seite]})
    return pos


def _art(p, titan_pos, hood):
    o = p["owner"]
    if not o or o in TOT:
        return "burned", None, None
    t = titan_pos.get(o)
    if t:
        return ("locked", t["locker"], t["unlock"]) if t["unlock"] > time.time() else ("expired", t["locker"], t["unlock"])
    if o == HOODLOCK_LP:
        h = hood.get((p["v"] == 4, p["id"]))
        if h:
            return ("locked", "HoodLock", h["unlock"]) if h["unlock"] > time.time() else ("expired", "HoodLock", h["unlock"])
        return "other", "HoodLock", None
    if o in LAUNCHPAD:
        return "launchpad", LAUNCHPAD[o], None
    return None, None, None


def _hook_namen(vertraege):
    """Welche dieser Vertraege sind Uniswap-V4-Hooks, die selbst Liquiditaet halten? -> {adresse: Name}.
    Doppler (Launchpad-Hook mit airlock()) und eigene Hooks eines Tokens. Ob die Liquiditaet dort entnommen werden
    kann, steht im Code des Hooks; wir sagen nur, wer sie haelt, und behaupten keinen Lock."""
    if not vertraege:
        return {}
    oc = _oc()
    r = oc._multicall([(v, "0xf78a8a3e") for v in vertraege] + [(v, "0xdc4c90d3") for v in vertraege])
    out = {}
    for i, v in enumerate(vertraege):
        airlock, pm = r[i], r[len(vertraege) + i]
        if airlock and len(airlock) >= 32 and _w(airlock, 0):
            out[v] = "Doppler hook"
        elif pm and len(pm) >= 32 and _adr(pm, 0) == V4_POOLMANAGER:
            out[v] = "Uniswap V4 hook"
    return out


def _status(token):
    oc = _oc()
    pos = _positionen(token)
    if not pos:
        return None
    titan_pos = {}
    for x in _cache(("titan", token), lambda: _titan(token)):
        if x["kind"] in (1, 2):
            titan_pos[x["contract"]] = x
    hood = _cache(("hoodlock_lp",), _hoodlock_lp, ttl=1800) if any(p["owner"] == HOODLOCK_LP for p in pos) else {}
    offen = [p for p in pos if _art(p, titan_pos, hood)[0] is None]
    codes = oc._vertraege([p["owner"] for p in offen if p["owner"]]) if offen else {}
    # Seit 2.81.1: Vertraege, die selbst Liquiditaet halten, beim Namen nennen (V4-Hooks wie Doppler). Eine direkte
    # V4-Position (ohne NFT) kann nur ein Vertrag haben, sie ist nie eine Wallet, auch wenn getCode kurz ausfiel.
    hooks = _hook_namen(sorted({p["owner"] for p in offen if p["owner"] and (p.get("direct") or codes.get(p["owner"]))}))
    summe = {"locked": 0.0, "expired": 0.0, "burned": 0.0, "launchpad": 0.0, "wallet": 0.0, "hook": 0.0, "other": 0.0}
    namen, frist, hook_namen = set(), [], {}
    for p in pos:
        art, name, bis = _art(p, titan_pos, hood)
        if art is None:
            if p["owner"] in hooks:
                art, name = "hook", hooks[p["owner"]]
                hook_namen[name] = hook_namen.get(name, 0.0) + p["menge"]
            elif p.get("direct") or codes.get(p["owner"]):
                art = "other"
            else:
                art = "wallet"
        summe[art] += p["menge"]
        if art in ("locked", "launchpad") and name:
            namen.add(name)
        if art == "locked" and bis:
            frist.append((bis, p["menge"]))
    ges = sum(summe.values())
    if ges <= 0:
        return None
    out = {k + "_pct": round(v / ges * 100, 1) for k, v in summe.items()}
    out["hooks"] = [{"name": n, "pct": round(m / ges * 100, 1)} for n, m in sorted(hook_namen.items(), key=lambda x: -x[1])]
    out.update(positions=len(pos), pools=len({p["pool"] for p in pos}), lockers=sorted(namen),
               kinds=sorted({"v3" if p["v"] == 3 else "v4" for p in pos}))
    if frist:
        out["unlock_ts"] = min(b for b, _ in frist)
        out["unlock_last_ts"] = max(b for b, _ in frist)
    return out


def lp_status(token):
    """-> {locked_pct, expired_pct, burned_pct, launchpad_pct, wallet_pct, other_pct, positions, pools, lockers, unlock_ts} oder None.
    Prozente vom Token-Anteil aller NFT-Positionen in den V3/V4-Pools des Tokens."""
    token = token.lower()
    try:
        return _cache(("lp", token), lambda: _status(token))
    except Exception as e:
        log.debug(f"lplock lp {token[:10]}: {e}")
        return None


_LAUF = {}


def lp_status_warten(token, warten=10.0):
    """Wie lp_status, aber hoechstens `warten` Sekunden. Der erste Lauf je Token liest die ganze Kette und kann laenger
    dauern; er laeuft dann im Hintergrund weiter und der naechste Scan hat das Ergebnis (danach nur neue Bloecke)."""
    import threading
    token = token.lower()
    c = _C.get(("lp", token))
    if c and time.time() - c[0] < 1200:
        return c[1]
    th = _LAUF.get(token)
    if not th or not th.is_alive():
        th = threading.Thread(target=lp_status, args=(token,), daemon=True)
        _LAUF[token] = th
        th.start()
    th.join(warten)
    c = _C.get(("lp", token))
    return c[1] if c else None


def lp_zeile(st):
    """Eine Zeile Text fuer Bot und Seite, z.B. 'LP positions: 99.7% locked (HoodLock, until 7 Nov 2026) · 0.3% in wallets'."""
    if not st:
        return None
    teile = []
    if st.get("locked_pct"):
        bis = ""
        if st.get("unlock_ts"):
            wort = "until" if st.get("unlock_last_ts") in (None, st["unlock_ts"]) else "first unlock"
            bis = f", {wort} {time.strftime('%-d %b %Y', time.gmtime(st['unlock_ts']))}"
        namen = ", ".join(n for n in st.get("lockers") or [] if n not in LAUNCHPAD.values())
        teile.append(f"{st['locked_pct']:g}% locked" + (f" ({namen}{bis})" if namen or bis else ""))
    if st.get("launchpad_pct"):
        lp = ", ".join(n for n in st.get("lockers") or [] if n in LAUNCHPAD.values())
        teile.append(f"{st['launchpad_pct']:g}% held by the launchpad" + (f" ({lp})" if lp else ""))
    if st.get("burned_pct"):
        teile.append(f"{st['burned_pct']:g}% burned")
    if st.get("expired_pct"):
        teile.append(f"{st['expired_pct']:g}% lock expired")
    for h in st.get("hooks") or []:
        if h.get("pct"):
            teile.append(f"{h['pct']:g}% held by a {h['name']} (withdraw rules are in its code)")
    if st.get("wallet_pct"):
        teile.append(f"{st['wallet_pct']:g}% in wallets, can be pulled")
    if st.get("other_pct"):
        teile.append(f"{st['other_pct']:g}% in other contracts")
    return "LP positions: " + " · ".join(teile) if teile else None
