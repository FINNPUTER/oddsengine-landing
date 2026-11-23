"""
adressen.py: gekuerzte Wallet-Adressen in KI-Antworten pruefen (seit 2.60.6).

Das Modell kuerzt Adressen selbst ("0xa525…e9d8") und erfindet dabei manchmal Zeichen ("0xa525…e9be8").
fix() vergleicht jede gekuerzte Adresse der Antwort mit den vollen Adressen aus den Tool-Daten des Gespraechs.
Passt sie zu keiner, aber eindeutig zu einer ueber Anfang oder Ende, wird sie durch die richtige Kurzform ersetzt.
Volle Adressen, die es in den Daten nicht gibt, aber zu einer echten fast passen, werden ebenso korrigiert.
"""
import re

_EVM_VOLL = re.compile(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_SOL_VOLL = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_EVM_KURZ = re.compile(r"0x([0-9a-fA-F]{1,10})(?:…|\.\.\.?)([0-9a-fA-F]{2,10})(?![0-9a-fA-F])")
_SOL_KURZ = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])([1-9A-HJ-NP-Za-km-z]{3,10})(?:…|\.\.\.?)([1-9A-HJ-NP-Za-km-z]{3,10})(?![1-9A-HJ-NP-Za-km-z])")


def _sol_echt(s):
    # base58-Woerter ohne Ziffern sind meist normaler Text
    return any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s)


def volle(daten):
    evm = {a.lower() for a in _EVM_VOLL.findall(daten or "")}
    sol = {a for a in _SOL_VOLL.findall(daten or "") if _sol_echt(a)}
    return evm, sol


def _kurz(a, vorne, hinten):
    return a[:vorne] + "…" + a[-hinten:]


def _passend(kandidaten, anfang, ende, gross_klein=False):
    def norm(x):
        return x if gross_klein else x.lower()
    a, e = norm(anfang), norm(ende)
    exakt = [k for k in kandidaten if norm(k).startswith(a) and norm(k).endswith(e)]
    if exakt:
        return exakt[0], True
    vorne = [k for k in kandidaten if len(a) >= 4 and norm(k).startswith(a)]
    hinten = [k for k in kandidaten if len(e) >= 4 and norm(k).endswith(e)]
    eins = {k for k in vorne + hinten}
    if len(eins) == 1:
        return eins.pop(), False
    if len(vorne) == 1:
        return vorne[0], False
    if len(hinten) == 1:
        return hinten[0], False
    return None, False


def fix(text, daten):
    """Antworttext mit korrigierten Adressen. daten: alles, was die KI gesehen hat (Tool-Ergebnisse als Text)."""
    if not text or not daten:
        return text
    evm, sol = volle(daten)
    hashes = {h.lower() for h in _HASH.findall(daten)}
    if not evm and not sol:
        return text

    def evm_kurz(m):
        if not evm:
            return m.group(0)
        vorne, hinten = m.group(1), m.group(2)
        if any(h.startswith("0x" + vorne.lower()) and h.endswith(hinten.lower()) for h in hashes):
            return m.group(0)
        echt, gut = _passend(evm, "0x" + vorne, hinten)
        if not echt or gut:
            return m.group(0)
        return _kurz(echt, 2 + max(2, len(vorne)), 4)

    def sol_kurz(m):
        if not sol or not (_sol_echt(m.group(1) + m.group(2)) or len(m.group(1)) >= 4):
            return m.group(0)
        echt, gut = _passend(sol, m.group(1), m.group(2), True)
        if not echt or gut:
            return m.group(0)
        return _kurz(echt, max(4, len(m.group(1))), 4)

    def evm_voll(m):
        a = m.group(0).lower()
        if a in evm:
            return m.group(0)
        echt, _ = _passend(evm, a[:8], a[-6:])
        return echt if echt else m.group(0)

    text = _EVM_VOLL.sub(evm_voll, text)
    text = _EVM_KURZ.sub(evm_kurz, text)
    text = _SOL_KURZ.sub(sol_kurz, text)
    return text
