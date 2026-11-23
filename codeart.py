"""codeart.py: Wallet oder Vertrag? (seit 2.52.3)

EIP-7702: normale Wallets (Robinhood Wallet, MetaMask Smart Account, viele neue Wallets) haben seit Pectra einen
kleinen "Code": 0xef0100 + die Adresse der Logik, die sie nutzen. eth_getCode liefert dann nicht "0x", obwohl es ein
ganz normales Wallet mit Schluessel ist. Vorher hat der Scan solche Wallets als Vertrag gezaehlt und aus Haltern,
Top 10, frühen Kaeufern und Bundles herausgenommen.

  norm(code)  -> "0x" fuer Wallets (auch 7702), sonst der Code
"""


def ist_7702(code):
    c = str(code or "").lower()
    return c.startswith("0xef0100") and len(c) == 48


def norm(code):
    if code is None:
        return None
    return "0x" if ist_7702(code) else code
