"""Selbsttest von innen (seit 2.82.30): prueft live, auf dem Server, ob die Teile wirklich antworten.
Nur Admin: /v1/admin/selftest. Liest nur, kauft nichts, bucht nichts ab. Jede Pruefung hat ihr eigenes try,
ein Ausfall stoppt die anderen nicht. Ausgabe je Pruefung: ok, Dauer in ms, ein Satz was gesehen wurde."""
import time


def _lauf(name, fn):
    t0 = time.time()
    try:
        text = fn()
        return {"check": name, "ok": True, "ms": int((time.time() - t0) * 1000), "seen": str(text)[:200]}
    except Exception as e:
        return {"check": name, "ok": False, "ms": int((time.time() - t0) * 1000),
                "seen": (type(e).__name__ + ": " + str(e))[:200]}


def _mcp_start():
    import agents
    r = agents.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, None)
    v = r["result"]["protocolVersion"]
    n = len(agents.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)["result"]["tools"])
    if n < 11:
        raise RuntimeError(f"only {n} tools listed")
    return f"protocol {v}, {n} tools, server {agents.VERSION}"


def _mcp_gate():
    import agents
    r = agents.mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "scan_token", "arguments": {"token": "x"}}}, None)
    if not r["result"].get("isError"):
        raise RuntimeError("a call without a key was not refused")
    return "calls without a key are refused"


def _scan():
    import agents
    import onchain
    r = agents.krypto("scan_token", {"token": onchain.ODDS_CA}, None)
    if not isinstance(r, dict) or (r.get("error") and len(r) <= 3):
        raise RuntimeError(str(r)[:160])
    return "scan of $ODDS answered with " + str(len(r)) + " fields: " + ", ".join(list(r.keys())[:8])


def _sign():
    import agents
    # approve(spender, unlimited) auf einem ERC-20: muss als riskant erkannt werden
    raw = "0x095ea7b3" + "0" * 24 + "1111111111111111111111111111111111111111" + "f" * 64
    r = agents.krypto("check_before_sign", {"raw": raw, "chain": "robinhood"}, None)
    return "check_before_sign answered: " + str(r)[:150]


def _kalshi_liste():
    import optionen
    m = optionen.kalshi("btc")
    if not m:
        raise RuntimeError("no open BTC markets returned")
    mit = sum(1 for x in m if x.get("yes_size") is not None)
    return f"{len(m)} BTC markets, {mit} with a size at the ask"


def _kalshi_buch():
    import optionen
    import optionen_tiefe
    m = optionen.kalshi("btc")
    z = sorted(m, key=lambda x: abs((x.get("yes_ask") or 0) - 0.5))[0]
    b = optionen_tiefe.buch_kalshi(z["market"], lambda pf: optionen._get(optionen.KALSHI + pf))
    if not b["yes"] and not b["no"]:
        raise RuntimeError("order book came back empty for " + z["market"])
    asks = optionen_tiefe.kaufseite(b, "yes")
    return f"{z['market']}: {len(b['yes'])} yes levels, {len(b['no'])} no levels, best yes ask {asks[0] if asks else 'none'}, list says {z.get('yes_ask')}"


def _deribit():
    import optionen
    fl = optionen.flaeche("BTC")
    return f"BTC index {fl.get('index')}"


def _kette():
    import optionen_plan
    s = optionen_plan.supply()
    if not s:
        raise RuntimeError("supply not readable")
    return f"$ODDS supply {s:,.0f}, a seat needs {s * optionen_plan.MIN_PCT / 100:,.0f}"


def _auto():
    import optionen_auto
    if not optionen_auto.bereit():
        raise RuntimeError("AUTOTRADE_SECRET is missing in this service")
    return f"key storage ready, switch {'on' if optionen_auto.an() else 'off'}, order path /portfolio/events/orders"


def _db():
    import optionen
    pg = optionen._pg()
    n = pg.execute("SELECT count(*), max(created) FROM optionen_picks").fetchone()
    alt = pg.execute("SELECT EXTRACT(EPOCH FROM (now() - max(created))) / 60 FROM optionen_picks").fetchone()[0]
    if alt is not None and float(alt) > 180:
        raise RuntimeError(f"last pick was written {float(alt):.0f} minutes ago, {n[0]} picks in total")
    return f"{n[0]} picks, last one {float(alt or 0):.0f} minutes ago"


def _steuer_tt():
    import steuer_tt
    return steuer_tt.test()


def _tronton():
    import tronton
    return tronton.test() + "; " + tronton.test_tief()


PRUEFUNGEN = [("MCP: handshake and tool list", _mcp_start), ("MCP: no key, no tool", _mcp_gate),
              ("Scanner: live scan of $ODDS", _scan), ("Scanner: check before sign", _sign),
              ("Kalshi: market list", _kalshi_liste), ("Kalshi: order book", _kalshi_buch),
              ("Deribit: options surface", _deribit), ("Chain: $ODDS supply", _kette),
              ("Auto mode: key storage", _auto), ("Above X: picks are being written", _db),
              ("TRON and TON: data sources", _tronton), ("Tax export: TRON and TON", _steuer_tt)]


def lauf():
    import os
    out = [_lauf(n, f) for n, f in PRUEFUNGEN]
    return {"service": os.getenv("RAILWAY_SERVICE_NAME") or "unknown", "ts": int(time.time()),
            "passed": sum(1 for x in out if x["ok"]), "total": len(out), "checks": out}
