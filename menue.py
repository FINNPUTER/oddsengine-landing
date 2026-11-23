"""
menue.py: Das Menue von Your Engine in Telegram (seit 2.58).

/start und /help zeigen die Themen als Knoepfe. Ein Thema zeigt zwei bis drei Beispiele, ein Tipp auf ein Beispiel
schickt es an Your Engine, als haette man es selbst geschrieben. So muss niemand raten, was der Bot kann.
Die Seite engine.oddsengine.live oeffnet ein Thema direkt ueber t.me/<bot>?start=m_<thema>.
"""
THEMEN = {
    "protect": ("🛡 Protect me",
                "Your Engine watches your wallets and warns you before it is too late. Wallet Guard flags risky approvals. "
                "Send a screenshot of any signing window and you hear what it really does before you sign. "
                "If a wallet still gets drained, you get a drain report: where the money went, with proof, and what to do now.",
                [("Turn on Wallet Guard", "Turn on Wallet Guard for my wallets"),
                 ("Check my approvals", "Check my wallet for risky approvals"),
                 ("Clean my wallet", "Clean my wallet"),
                 ("Before you sign", "I am about to sign something in my wallet. How do I check it with you before I sign?"),
                 ("Drain report", "I think my wallet got drained. Make a drain report for my wallet"),
                 ("Sunday report", "Send me a weekly report for my wallet every Sunday")]),
    "check": ("🔍 Check a coin or wallet",
              "Paste any coin or wallet and Your Engine reads the chain for you: bundles, snipers, dev wallet, top holders, "
              "who bought at which market cap, and where the money goes. Every answer with proof.",
              [("What's hot on Torch", "What is trending on Torch right now?"),
               ("Top holder of $ODDS", "Who is the top holder of $ODDS?"),
               ("Who bought $ODDS where", "Who bought $ODDS at which market cap, and who is in profit?"),
               ("Trace the Bybit hack", "Where did the money go? 0x47666Fab8bd0Ac7003bce3f5C3585383F09486E2 on ethereum")]),
    "feed": ("📰 My feed",
             "Follow Telegram channels, X accounts, YouTube channels and news in one place, only with your keywords. "
             "Or get everything in one morning briefing.",
             [("Follow Watcher Guru", "Follow Watcher Guru, only BTC, ETH and Fed"),
              ("Morning briefing", "Turn on my morning briefing"),
              ("What can you follow?", "What sources can you follow for me?")]),
    "tax": ("🧾 Taxes",
            "One message and you get an Excel file for your tax advisor: every buy, sell and swap across Solana, Ethereum, "
            "Base, Arbitrum, Optimism, Robinhood Chain, TRON and TON, with the value and market cap at that time, gain or loss per sale and the holding period.",
            [("Tax export 2026", "Tax export for 2026"),
             ("Tax export 2025", "Tax export for 2025"),
             ("Any wallet", "Tax export for another wallet"),
             ("All years", "Tax export for all years")]),
    "markets": ("📈 Markets and trading",
                "Prices, chart readings, the engine's calls, and buying or selling from your own wallet. You always confirm "
                "in your wallet, Your Engine never holds your keys.",
                [("BTC right now", "BTC price and the chart for today"),
                 ("Read the ETH chart", "Read the ETH chart on 4h"),
                 ("TSLA 24/7", "What is TSLA doing on Robinhood Chain right now?"),
                 ("Today's calls", "What did the engine call today?"),
                 ("My Auto mode", "How is my Auto mode? Show my Kalshi balance and open positions"),
                 ("My week", "How was my week? Make my weekly wallet report")]),
    # Seit 2.77.0: Portfolio (eigene Wallets eintragen) und Deals
    "portfolio": ("💼 My portfolio",
                  "Tell Your Engine which wallets are yours, on any chain. You see every wallet on its own and one combined "
                  "view per coin, with what you paid and your profit or loss. Your briefing opens with it. Up to 10 wallets, "
                  "read only, it never needs your keys.",
                  [("Add a wallet", "Add a wallet to my portfolio"),
                   ("Show my portfolio", "Show my portfolio"),
                   ("Which wallets are mine?", "Which wallets are in my portfolio?"),
                   ("Remove a wallet", "Remove a wallet from my portfolio")]),
    "deals": ("🏷 Deals",
              "Ask for a deal on anything and Your Engine checks the deal community of your country: price, shop, what "
              "people think of it, and the link. Set a target price and it pings you when a deal hits it.",
              [("AirPods deal", "Find me a deal on AirPods Pro 3"),
               ("Hot today", "What are today's hottest deals?"),
               ("Price alert", "Tell me when there is a PS5 deal under 400"),
               ("Gold coin check", "Is a 1 oz Kruegerrand a good price right now?")]),
    # Seit 2.69.4: eigene Themen fuer Trading per Sprache und GTA VI (Links von der Engine-Seite: m_trade, m_gta)
    "trade": ("🎙 Trade by voice",
              "Send a voice note or a line of text: \"buy 5 dollars of this coin\" or \"sell half\". Your Engine finds the "
              "chain, gets a live quote and shows you what you get. You confirm in your own wallet, it never holds your keys.",
              [("Buy $5 of $ODDS", "Buy 5 dollars of $ODDS"),
               ("Sell half", "Sell half of my $ODDS"),
               ("My trades", "Show my trades")]),
    "gta": ("🎮 GTA VI",
            "GTA VI is out on 19 November. Your Engine shows the live Polymarket odds (another delay? $100 price? "
            "Metacritic) and tells you the moment a new GTA coin launches, with the full scan. Most of them are rugs, "
            "the scan is the point.",
            [("GTA odds now", "What are the GTA VI odds on Polymarket right now?"),
             ("New GTA coins", "How many new GTA coins launched today, and how many look like rugs?"),
             ("GTA coin radar", "Tell me when new GTA coins launch")]),
    "apps": ("🤖 Use me in ChatGPT or Claude",
             "Sit in ChatGPT or Claude all day? Add Your Engine there as a connector and ask it the same things: scan a coin, "
             "check a signature, trace a wallet. One personal link, for $ODDS holders.",
             [("Connect ChatGPT or Claude", "Connect Your Engine to ChatGPT or Claude"),
              ("Turn the link off", "Disconnect my ChatGPT and Claude link")]),
    "life": ("🗓 Everyday",
             "Reminders, images, voice notes and summaries. Send a YouTube link or a PDF and get the key points.",
             [("Remind me", "Remind me tomorrow at 9 to claim my rewards"),
              ("Promo video", "Make a promo video of my project's website"),
              ("Make an image", "Make an image of a robot reading charts at night"),
              ("Voice replies", "Answer my voice notes with a voice note")]),
}

TEXT = ("What Your Engine does for you\n\n"
        "Tap a topic, then tap an example to try it. Or just write what you need, in any language, by text or voice note.\n\n"
        "/new starts a fresh chat · /balance shows your credit")


def _apps_an():
    try:
        import features
        return features.an("agents") and features.an("ai")
    except Exception:
        return False


def _deals_an(chat_id=None):
    try:
        import features
        if features.an("deals"):
            return True
        if chat_id is not None:
            import agent
            import motor
            u = agent.laden(chat_id)
            w = (u or {}).get("wallet")
            return bool(w and motor.beta(w))
    except Exception:
        pass
    return False


def haupt_knoepfe(chat_id=None):
    apps = _apps_an()
    deals = _deals_an(chat_id)
    ks = [(v[0], f"cb:m:{k}") for k, v in THEMEN.items() if (k != "apps" or apps) and (k != "deals" or deals)]
    return [ks[i:i + 2] for i in range(0, len(ks), 2)]


def haupt(chat_id):
    import agent
    return agent._send(chat_id, TEXT, haupt_knoepfe(chat_id))


def thema(chat_id, key):
    import agent
    t = THEMEN.get(key)
    if not t:
        return haupt(chat_id)
    try:
        import video
        video_an = video.an()
    except Exception:
        video_an = False
    # seit 2.60.1: den Videoknopf nur zeigen, wenn loudscene eingerichtet ist (LOUDSCENE_API_KEY)
    knoepfe = [[(b[0], f"cb:t:{key}:{i}")] for i, b in enumerate(t[2]) if video_an or b[0] != "Promo video"] + [[("◀ All topics", "cb:m:all")]]
    return agent._send(chat_id, f"{t[0]}\n\n{t[1]}\n\nTry it:", knoepfe)


def beispiel(key, i):
    t = THEMEN.get(key)
    try:
        return t[2][int(i)][1] if t else None
    except Exception:
        return None


BEFEHLE = [("help", "What Your Engine can do for you"), ("new", "Start a fresh chat"), ("balance", "Your credit"),
           ("auto", "Your Auto mode: Kalshi balance and open positions"),
           ("voice", "Voice replies on or off"), ("language", "The language I answer in"), ("cost", "Show the cost under answers")]
_GESETZT = [False]


def befehle_setzen():
    """Das Telegram-Menue unten links, einmal je Prozess."""
    if _GESETZT[0]:
        return
    _GESETZT[0] = True
    try:
        import agent
        agent._tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in BEFEHLE])
    except Exception:
        pass
