"""
features.py: der Schaltpult. Seit 2.21.

Jedes Update ist fertig im Code und sammelt im Hintergrund schon Daten.
Sichtbar wird es erst, wenn der Betreiber den Schalter umlegt: auf der
Admin-Seite der App, ohne Deploy. Zurueckschalten geht genauso.

Gespeichert im kv-Speicher (Postgres) unter "features". Ohne Datenbank gelten
die Standardwerte aus den Variablen FEATURE_<NAME>=true|false.
"""
import json
import os
import time

import storage

# name -> (Anzeigename, was es tut, Standard)
LISTE = {
    "torch":   ("Torch launch",
                "Landing and app turn green, '$ODDS is live on Torch' appears, the "
                "contract address shows and every Buy button goes to Torch. Needs "
                "ODDS_MINT on the web service. Before this is on, the address stays "
                "hidden even if ODDS_MINT is set.", False),
    "agent":   ("Your Engine",
                "Personal agent in Telegram: calls that fit each holder's rules, "
                "sized to their stake, own record, weekly report. Linking and the "
                "holder group work either way.", False),
    "station": ("Station Live",
                "Live readings from the resolution stations: high so far, last "
                "reading, which brackets are already out.", False),
    "crowd":   ("Engine vs Crowd",
                "The market's own favourite block next to the engine's pick, "
                "both scored in public.", False),
    "beat":    ("Beat the Engine",
                "Daily game for holders: pick the bracket in three cities before "
                "the engine locks its own pick. 1 point for a hit, 3 when the "
                "engine missed, 2 bonus for all three. Weekly and all time board. "
                "No stakes, no prizes.", False),
    "crypto":  ("Crypto Terminal",
                "terminal.oddsengine.live: Up/Down markets for BTC, ETH, SOL, XRP, "
                "DOGE, BNB and HYPE in 5 minute, 15 minute and 4 hour windows. Engine "
                "odds against the order book, five modules with a public "
                "record. Needs the crypto service running.", False),
    "engine":  ("Engine page: Your Engine tab",
                "engine.oddsengine.live: Your Engine opens (chat with chain, market and web tools, "
                "Telegram text and voice, morning briefing, reminders, price alerts, watched pages, "
                "memory, email and calendar, money alerts for crowd vs engine and options edges; pay per answer "
                "from the balance). Off: teaser. ENGINE_BETA wallets get it anyway.", False),
    "ai":      ("Engine page: AI tab",
                "engine.oddsengine.live: OddsEngine AI tab with playground, API keys, deposits "
                "and model list, paid in $ODDS. Also opens the public API "
                "(/v1/chat/completions). Needs OPENROUTER_API_KEY on the web service. "
                "Off: the tab shows a teaser, keys stop working.", False),
    "agents":  ("AI for agents",
                "API keys with a daily limit and allowed models, keys created by a wallet "
                "signature without a browser (POST /v1/agents/key) and the MCP server at "
                "/mcp for Claude, Cursor and other agents. Needs the ai switch too.", False),
    "x402":    ("x402 payments",
                "Agents pay per request in $ODDS without an account: /v1/chat/completions "
                "without a key answers 402 with the price, the agent signs a permit, the "
                "server settles it on chain. Needs X402_GAS_KEY (gas only wallet) on the web "
                "service and the ai switch.", False),
    "x402usdc": ("x402 in USDC (Base)",
                "Agents pay with standard x402 in USDC on Base, no custom code needed. Our margin "
                "buys $ODDS on Robinhood Chain, half is burned, half goes to revenue, all public on "
                "/v1/x402/buybacks. Needs the x402 and ai switches, X402_GAS_KEY and a little ETH on "
                "Base in the gas wallet.", False),
    "x402scan": ("x402 pay per scan",
                "Agents buy a full token risk scan per call, no account and no key: GET /v1/x402/scan?q=<contract or $TICKER> "
                "answers 402 with the price (X402_SCAN_USD, default $0.01 plus $0.01 gas), the agent pays with x402 (USDC on Base "
                "or a $ODDS permit) and gets the scan as JSON: verdict, bundles, launch block buys, clusters, dev, funder "
                "network, locks, proof links. Waits up to 75 s for the deep scan. Needs x402 or x402usdc and X402_GAS_KEY.", False),
    "x402sol": ("x402 in USDC on Solana",
                "Agents pay with USDC on Solana (x402 exact). Settled through a public facilitator (X402_SOL_FACILITATOR, default "
                "PayAI), which pays the Solana fee. Needs X402_SOL_PAYTO: our Solana wallet for the USDC, send it a few cents of "
                "USDC once so its USDC account exists. Shows up in every 402 offer and on /v1/x402/resources.", False),
    "x402usdg": ("x402 in USDG on Robinhood Chain",
                "Agents pay with USDG on Robinhood Chain (x402 exact, EIP-3009), settled by the gas wallet like USDC on Base. "
                "Only offered when the USDG contract answers authorizationState and its EIP-712 domain is readable, so it never "
                "promises a way that cannot work. Needs X402_GAS_KEY with a little ETH on Robinhood Chain.", False),
    "lows":    ("Lowest temperature",
                "Calls for the lowest temperature of the day, same cities and stations as "
                "the highs. Off: the engine still makes and scores these calls in the "
                "background, only you see them (on this page, below the switches). On: calls "
                "and results go to the holder channel and /v1/lows shows them.", False),
    "sentiment": ("Sentiment (exchanges)",
                "Terminal section 'What the exchanges say': funding, open interest, long/short, aggressive "
                "buyers, order book pressure and liquidations from Hyperliquid, OKX, Coinbase, Kraken and "
                "Binance next to the Polymarket crowd, plus /v1/sentiment. Off: recorded in the background, "
                "only you see it. Needs the crypto service.", False),
    "trade":   ("Your Engine: trading by chat",
                "Users say 'buy $50 of <CA>' or 'sell half of $XYZ' and confirm in their own wallet on "
                "engine.oddsengine.live. Non-custodial, routed through Relay on Robinhood Chain, capped by "
                "TRADE_MAX_USD (1000). Together with 'scan': every chain, Solana through Jupiter with Phantom or "
                "Solflare. Off: only ENGINE_BETA wallets can trade.", False),
    "scan":    ("Your Engine: CA report on every chain",
                "Paste any contract, mint or chart link from Solana, Base, Ethereum, BNB Chain, Arbitrum or Robinhood Chain: "
                "verdict, safety (honeypot, tax, mint/freeze, LP lock), supply (top 10, bundles, fresh wallets, clusters, "
                "snipers), dev wallet and earlier launches, smart wallets among the first buyers. Card with buttons in "
                "Telegram (Full report, Chart, Watch it, Refresh), the live desktop page on engine.oddsengine.live and "
                "live watches with alarms on dev sells, LP pulls and top holder dumps. With 'trade' on, trading works on "
                "all these chains including Solana (Phantom, Solflare). Own deep scan (bundles, snipers, fresh wallets, "
                "same-funder clusters) and own wallet scores, no outside service; Solana is best with HELIUS_API_KEY, "
                "Robinhood with BLOCKSCOUT_API_KEY. Off: only Robinhood Chain like before. ENGINE_BETA wallets get it anyway.", False),
    "smart":   ("Smart money: read in the background",
                "Watches the wallets our own scoring rates highest (real hit rate, misses count, snipers and bundles never score; "
                "SMART_MIN_SCORE 36, best SMART_WATCHLIST 150) on Solana and Robinhood Chain, every wallet checked about every 2 "
                "minutes, Solana buys in seconds over the Helius webhook. When SMART_MIN_WALLETS (3) with different funders buy "
                "the same coin inside SMART_WINDOW_SEC (900), it becomes a call with the full token report and goes into the track "
                "record. On its own nothing is sent: only you see the calls in the admin. Needs HELIUS_API_KEY (Solana) and "
                "BLOCKSCOUT_API_KEY (Robinhood); SMART_DAILY_RPC (150000) caps the reads a day. Run the wallet import first, "
                "without scored wallets the watchlist stays empty.", False),
    "smart_calls": ("Smart money: send calls",
                "Together with 'smart': users who turn calls on (/smart on in Telegram, the switch on the page, or by asking) get "
                "every call in Telegram with the report and buttons, plus follow ups at 2x, 3x, 5x, 10x and up. Optional "
                "SMART_CHAT_ID posts them to a Telegram channel too. Red verdicts are recorded, never sent. The public track "
                "record is at /v1/engine/smart/record. ENGINE_BETA wallets get it anyway.", False),
    "crypto_picks": ("Crypto Edition: engine picks to the holder channel",
                     "Every engine pick on the 5 min Up/Down markets goes live to the holder channel (TELEGRAM_PAID_CHANNEL_ID) "
                     "with side, price, time left and a button to the market, and the result as a reply when the window closes. "
                     "Runs in the crypto service, so that service needs TELEGRAM_BOT_TOKEN and TELEGRAM_PAID_CHANNEL_ID. "
                     "CRYPTO_PICKS_SKIP=eth leaves coins out. Roughly 15 to 20 picks a day.", False),
    "rotation": ("Meta Rotation: send alerts (holders only)",
                "Together with 'smart': when 3 or more proven wallets (bought a coin at 500K market cap or less that later ran "
                "20x or more, checked on the chart) move into a new theme, every verified $ODDS holder with Your Engine gets the "
                "alert in Telegram and in the app, automatically (they can switch it off). The weekly pass does not unlock it. "
                "Off: everything is still detected and logged in /v1/admin/rotation, and only ENGINE_BETA testers get the alerts. On also "
                "opens the public proof at /v1/rotation/record (market cap at alert and peak after, no wallets). "
                "Tune with ROTATION_ELITE_X, ROTATION_ELITE_ENTRY_MC, ROTATION_MIN_WALLETS, ROTATION_TIER.", False),
    "guard":   ("Your Engine: Wallet Guard",
                "Users who turn it on (chat, the switch on the page) get a Telegram alert within seconds when their wallet approves "
                "a spender we do not know (ERC-20 approve, NFT approve-all, Permit2) or gets a Solana delegate or owner change, "
                "when someone else moves tokens out with an old approval, and when tokens leave without a swap. Every alert has "
                "a Revoke button their own wallet signs. Plus a wallet check of all open approvals (all five EVM chains and Solana). "
                "Live alerts: Robinhood and Arbitrum out of the box, Base, Ethereum and BNB once RPC_BASE, RPC_ETHEREUM or "
                "RPC_BSC point to your own node (public nodes refuse the query). Solana over the Helius webhook.", False),
    "group_scan": ("Your Engine: scans in Telegram groups",
                "Add the bot to any Telegram group or community. When someone posts a contract, a chart link or /scan <CA>, the "
                "bot answers in the same topic with the full report card, free, and fills in the deep scan in the same message "
                "when it is ready. Buttons: Full report, Chart, Ask Your Engine (opens the bot privately). Set BotFather "
                "/setprivacy to Disable (or make the bot admin) so it can see the messages. GROUP_SCAN_PER_HOUR (60) per group.", False),
    "voice":   ("Your Engine: voice replies",
                "A voice note to the bot gets a voice note back (text below it), /voice on|off, and a play button on every "
                "answer in the web chat with the head's ring moving to the voice. Needs ELEVENLABS_API_KEY or OPENAI_API_KEY "
                "on the web service. Costs VOICE_USD_PER_1K (0.12) per 1000 characters from the balance. ENGINE_BETA wallets "
                "get it anyway.", False),
    "calls":   ("Your Engine: phone calls",
                "Your Engine calls the user's confirmed phone number when Wallet Guard sees a drain or a dangerous approval, and when a "
                "coin they hold gets rugged (LP pulled, dev sells, top holder dumps), in the Gideon voice. Users can also say 'call me'. "
                "Numbers are confirmed with a 4 digit code spoken in a call. Needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM "
                "and PUBLIC_API_URL on web and worker, voice from ELEVENLABS_API_KEY. CALL_USD_PER_MIN (0.50) per started minute from the "
                "balance. ENGINE_BETA wallets get it anyway.", False),
    "metals":  ("Crypto Edition: gold and silver",
                "Polymarket Up/Down on gold (XAU) and silver (XAG), 15 minutes and hourly. The engine reads them all the "
                "time (Pyth prices, own model, learning lab with indicators) but only internally. On: they show on "
                "terminal.oddsengine.live. Turn on only when the lab report shows an edge out of sample.", False),
    "aivideo": ("Your Engine: AI video (Higgsfield)",
                "Text or photo to video with Kling 4.0, Kling 3.0, Seedance 2.5, Hailuo 2.3 and Wan 3.0 over the Higgsfield API. Your Engine "
                "shows the exact price first and starts only after a yes, paid from the balance with the same holder markup as the "
                "AI models, failed videos cost nothing. Needs HIGGSFIELD_API_KEY on web and a "
                "funded balance on open.higgsfield.ai. AIVIDEO_MAX_USD (15), AIVIDEO_DAY_MAX (10). ENGINE_BETA wallets get it anyway.", False),
    "deals":   ("Your Engine: Deals (any product, per country)",
                "Bargain hunting in chat and Telegram for any product: live deals from the deal community of the user's country "
                "(mydealz DE/EU, Preisjaeger AT, hotukdeals UK, Dealabs FR, Chollometro ES, Pepper PL/NL, Promodescuentos MX, "
                "Slickdeals US) with price, shop, community vote and codes, today's hot list, gold.de dealer comparison with "
                "premium over spot, ParfumDreams prices for perfume, and deal alerts (every new deal or a target price) every "
                "30 minutes. Reading is free. DEALS_POLL_SEC (1800), DEALS_MAX_DAY (25), DEALS_SITE_HOUR (120). ENGINE_BETA "
                "wallets get it anyway.", False),
    "radar":   ("Your Engine: Narrative Radar (GTA and more)",
                "Users say 'tell me when GTA coins launch' (or any theme with their own words) and get every NEW coin "
                "on Solana, Base, Ethereum, BNB Chain, Arbitrum and Robinhood Chain whose name or ticker matches, with "
                "the full scan card. GTA VI is built in (characters, places, classics). Source: DexScreener search, free. "
                "RADAR_MAX_DAY (40) hits per user and day, RADAR_SCANS_HOUR (30) full scans per hour. ENGINE_BETA wallets get it anyway.", False),
    "lang":    ("Your Engine: language setting",
                "Your Engine saves the user's language (asked at the start, /language, or a setting on the page) and sends "
                "everything in it: answers, briefing, alerts, check-ins, watches and OddsEngine news, translated "
                "automatically. Off: it answers in the language the user writes in, proactive messages stay English.", False),
    "options": ("Above X (options)",
                "Terminal section 'Above X' (since 2.82.26 the public sees the record, the days and settled picks only; live gaps, the board per market and the method stay with you). Internally: Deribit options turned into a "
                "chance for every 'BTC/ETH above X' strike, tracked picks with a public record, plus /v1/options. "
                "Off: picks are still made and scored, only you see them.", False),
    "options_calls": ("Above X calls to the channel",
                "Only the strongest Above X picks (Kalshi, big edge, price 20c to 80c, one per market close, max 24 a day) go "
                "live to OPT_CALLS_CHAT (default TELEGRAM_PAID_CHANNEL_ID) with question, side, price, engine fair price and a "
                "button, and the result as a reply. Runs in the crypto service, so that service needs TELEGRAM_BOT_TOKEN and the "
                "channel. Off: calls are only logged in /v1/admin/options/calls. On also opens /v1/options/calls/record "
                "(only calls that were sent). Tune with OPT_CALLS_EDGE, OPT_CALLS_DAY, OPT_CALLS_SKIP=eth.", False),
    "options_autotrade": ("Above X auto trading (internal beta)",
                "Every new Above X call is bought automatically in the own Kalshi account of each tester in KALSHI_AUTOTRADE "
                "(crypto service), limit order at the call price plus 1c, immediate or cancel, within each tester's usd_per_call and "
                "usd_per_day. The engine never holds money. Works even when 'options_calls' is off. Status: /v1/admin/options/autotrade. "
                "Also opens the 'Auto mode' card on engine.oddsengine.live: verified holders enter their own Kalshi key and limits there "
                "(needs AUTOTRADE_SECRET in web and crypto). They go live once invited (/v1/admin/options/autotrade/allow?wallet=0x..&on=1) "
                "or approved (.../approve?wallet=0x..&ok=1). AUTOTRADE_OPEN=1 lets every verified holder in without approval. "
                "Since 2.82.24 each wallet has a plan (admin page): team (no fee), holder (fee AUTOTRADE_FEE, default 20% of profit) "
                "or paid (fee AUTOTRADE_FEE_PAID, default 35%, no holding needed). Team needs no holding either (since 2.82.36). Holder needs AUTOTRADE_MIN_PCT (default 1%) "
                "of the $ODDS supply, checked on chain before every buy: below it Auto mode pauses at once. Fees come out of the "
                "$ODDS balance (ai_accounts), buying needs a balance of fee rate x daily limit.", False),
    "elysium": ("Elysium scanner",
                "Scan tokens and wallets on Elysium (Kinetiq L2 on HyperEVM, chain 99901): contract, dev and who funded the dev, "
                "holders, first buyers, same-block groups, funding clusters, and a match of every address against everything the "
                "engine knows (early buys and devs on other chains, smart wallets, wallet book, Torch launches, Robinhood activity). "
                "Your Engine tools elysium_scan and elysium_wallet, API /v1/elysium/scan?q= and /v1/elysium/wallet?a=. Off: admin and beta only. "
                "Settings ELYSIUM_RPC, ELYSIUM_API, ELYSIUM_EXPLORER.", False),
    "xscan":   ("X: scan replies under our posts",
                "When someone posts a contract under one of our X posts or mentions us with one, the account answers with "
                "the scan as a reply. Off: nothing is answered on X, takes effect within 30 seconds, no deploy. Also checked "
                "right before every single reply. X_SCAN_PER_DAY (20), X_SCAN_PER_POST (5). Needs the X keys on the web service.",
                os.getenv("X_SCAN_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")),
    "launchboard": ("Launch Board (every Torch launch, facts only)",
                "engine.oddsengine.live/launches goes live: every new Torch launch with the facts from our scan (dev share and "
                "sells, launch block buys, bundles, top 10, holders, liquidity), each with a proof link. No rating, no colors. "
                "On: a background job scans up to 3 new coins every 90 seconds (BRETT_PRO_RUNDE, BRETT_TAKT_S). Off: page says "
                "'coming soon' and nothing is scanned.", False),
    "balloonmap": ("BalloonMap button (KYLL holder map)",
                "Robinhood Chain scans get a button '🎈 Holder map by KYLL' that opens the free BalloonMap from kyll.fun for that "
                "coin (their page, their wording, their rate limit of 300 per hour per viewer). Nothing is embedded on our pages. "
                "Off: no button.", True),
    "locker":  ("Locker (token locks on Robinhood Chain)",
                "engine.oddsengine.live/lock goes live: anyone can lock tokens or set up vesting in the OddsEngine Locker, "
                "fee in ETH or in $ODDS (burned). Needs the locker address below (after you deployed the contract). "
                "Off: the page says 'goes live shortly'. The scan shows our locks either way once the address is saved.", False),
    "kalshi":  ("Kalshi",
                "Kalshi Edition: the engine on every Kalshi weather market (highs and lows, "
                "20+ US cities, at Kalshi's own stations), calls with the Kalshi fee included, "
                "public in the Kalshi tab of the weather app and in Your Engine. Off: calls are "
                "still made and scored, visible only in admin.", False),
    "torchwall": ("Torch wall in Engine HQ",
                "A flame with the Torch logo for every new Torch launch on the back wall of Engine HQ (data from /v1/launches, "
                "facts only, gold flame once a coin graduated). Keep off until Torch agreed to the use of their logo; admin can preview.", False),
    "world":   ("World (3D)",
                "engine.oddsengine.live/world: walk through a 3D world as The Engine, every zone opens Your Engine "
                "tools (weather, crypto, scan, money trail, portfolio, trading, studio, daily desk, burn and lock, GTA radar). "
                "Talks to the same /v1/engine/ask as the web chat, same balance, same login. On: the World tab shows in the app. "
                "Off: the page says 'opens soon'; admin can still walk in.", False),
}

_stand = {"ts": 0.0, "werte": None}


def _standard(name):
    roh = os.getenv(f"FEATURE_{name.upper()}")
    if roh is None:
        return LISTE[name][2]
    return roh.strip().lower() in ("1", "true", "yes", "on")


def alle(frisch=False):
    """{name: bool} fuer alle bekannten Schalter. 30 Sekunden Cache."""
    if not frisch and _stand["werte"] is not None and time.time() - _stand["ts"] < 30:
        return dict(_stand["werte"])
    gespeichert = {}
    try:
        roh = storage.kv_get("features")
        gespeichert = json.loads(roh) if roh else {}
    except Exception:
        gespeichert = {}
    werte = {n: bool(gespeichert[n]) if n in gespeichert else _standard(n) for n in LISTE}
    _stand["werte"], _stand["ts"] = werte, time.time()
    return dict(werte)


def an(name):
    return bool(alle().get(name, False))


def setzen(name, wert):
    if name not in LISTE:
        raise KeyError(name)
    jetzt = alle(frisch=True)
    jetzt[name] = bool(wert)
    ok = storage.kv_set("features", json.dumps(jetzt))
    _stand["werte"], _stand["ts"] = jetzt, time.time()
    return ok, jetzt


def beschreibung():
    w = alle()
    return [{"name": n, "title": t, "about": a, "on": w.get(n, False)}
            for n, (t, a, _) in LISTE.items()]
