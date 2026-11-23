"""Seit 2.61.8: ein Startbefehl fuer alle Dienste (Railpack statt Nixpacks).

railway.toml gilt fuer jeden Dienst im Projekt. Railway setzt RAILWAY_SERVICE_NAME,
daran erkennt dieses Skript, was zu starten ist, genau wie vorher die Procfile-Zeilen:
  web     -> uvicorn api:app
  worker  -> python main.py
  crypto  -> python krypto_main.py
START_AS ueberschreibt den Namen, falls ein Dienst anders heisst.
"""
import os
import sys

name = (os.getenv("START_AS") or os.getenv("RAILWAY_SERVICE_NAME") or "").strip().lower()
port = os.getenv("PORT", "8000")
if name == "web":
    cmd = [sys.executable, "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", port]
elif name == "crypto":
    cmd = [sys.executable, "krypto_main.py"]
elif name == "worker":
    cmd = [sys.executable, "main.py"]
elif name == "spread":   # seit 2.82.99: Spread-Test im eigenen Dienst
    cmd = [sys.executable, "-c", "import optionen_spread as s; s.dienst()"]
else:
    sys.exit(f"start.py: unknown service '{name}'. Set START_AS to web, worker, crypto or spread.")
print("start.py:", name, "->", " ".join(cmd[1:]), flush=True)
os.execv(sys.executable, cmd)
