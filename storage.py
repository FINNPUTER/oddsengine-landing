"""
Append-only snapshot store. Spec section 12: historical signal values are
never modified after the fact.

JSONL by default. If DATABASE_URL is set (Railway Postgres), rows also go
to a table with no UPDATE path.

Railway ephemeral storage resets on redeploy -> attach a Volume and set
DATA_DIR=/app/data, or set DATABASE_URL. Otherwise your track record dies
with the next deploy. That is exactly how the droplet was lost.
"""
import json
import os
from datetime import datetime, timezone

from config import cfg
from logger import log

_pg = None


_pg_versuch = {"ts": 0.0}


class _Ergebnis:
    """Fertig gelesenes Ergebnis einer Abfrage (wie ein Cursor nach execute): fetchone, fetchall, Iteration."""
    def __init__(self, cur):
        self.description = cur.description
        self.rowcount = cur.rowcount
        self._rows = cur.fetchall() if cur.description is not None else []
        self._i = 0

    def fetchone(self):
        if self._i < len(self._rows):
            self._i += 1
            return self._rows[self._i - 1]
        return None

    def fetchall(self):
        r = self._rows[self._i:]
        self._i = len(self._rows)
        return r

    def fetchmany(self, n=1):
        r = self._rows[self._i:self._i + n]
        self._i += len(r)
        return r

    def __iter__(self):
        return iter(self.fetchall())


class _Pool:
    """Seit 2.80.3: mehrere Verbindungen statt einer einzigen fuer den ganzen Dienst.
    Vorher teilten sich alle Threads EINE Postgres-Verbindung; eine langsame Abfrage liess Hunderte Threads
    warten (am 05.11.2026: 990 Threads, 980 davon standen in intel._q und kv_get). Tiefen-Scans blieben haengen.
    Gleiche Schnittstelle wie die alte Verbindung: execute(sql, args) gibt ein fertig gelesenes Ergebnis zurueck,
    cursor() fuer executemany. autocommit wie vorher."""
    def __init__(self, url, groesse):
        import queue
        import threading
        self.url, self.groesse = url, groesse
        self.frei = queue.LifoQueue()
        self.anzahl = 0
        self.lock = threading.Lock()
        self.closed = False
        self.broken = False

    def _neu(self):
        import psycopg
        return psycopg.connect(self.url, autocommit=True, connect_timeout=5)

    def _holen(self):
        import queue
        try:
            return self.frei.get_nowait()
        except queue.Empty:
            pass
        with self.lock:
            if self.anzahl < self.groesse:
                self.anzahl += 1
                try:
                    return self._neu()
                except Exception:
                    self.anzahl -= 1
                    raise
        return self.frei.get(timeout=float(os.getenv("PG_WAIT_SEC", "30")))

    def _zurueck(self, c, kaputt=False):
        if kaputt or c.closed or getattr(c, "broken", False):
            try:
                c.close()
            except Exception:
                pass
            with self.lock:
                self.anzahl -= 1
            return
        self.frei.put(c)

    def execute(self, sql, args=None):
        c = self._holen()
        kaputt = False
        try:
            return _Ergebnis(c.execute(sql, args))
        except Exception as e:
            import psycopg
            kaputt = isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError))
            raise
        finally:
            self._zurueck(c, kaputt)

    def cursor(self):
        pool = self

        class _Ctx:
            def __enter__(self_):
                self_.c = pool._holen()
                self_.cur = self_.c.cursor()
                return self_.cur

            def __exit__(self_, typ, wert, tb):
                try:
                    self_.cur.close()
                except Exception:
                    pass
                pool._zurueck(self_.c, kaputt=typ is not None and "Operational" in getattr(typ, "__name__", ""))
                return False
        return _Ctx()

    def close(self):
        import queue
        while True:
            try:
                self.frei.get_nowait().close()
            except queue.Empty:
                break
            except Exception:
                pass
        self.closed = True


def _verbindung_tot(c):
    """Antwortet die Datenbank? (Nach einem Postgres-Neustart sind alte Verbindungen kaputt; der Pool wirft
    kaputte Verbindungen selbst weg, dieser Test bleibt als Sicherung.)"""
    try:
        if c.closed:
            return True
        c.execute("SELECT 1").fetchone()
        return False
    except Exception:
        return True


def _init_pg():
    global _pg
    import time as _t
    if not cfg.database_url:
        return None
    if _pg is not None:
        if _t.time() - _pg_versuch["ts"] < 15:
            return _pg
        _pg_versuch["ts"] = _t.time()
        if not _verbindung_tot(_pg):
            return _pg
        log.warning("postgres: Verbindung verloren, baue neu auf")
        try:
            _pg.close()
        except Exception:
            pass
        _pg = None
    if _t.time() - _pg_versuch.get("fehl", 0.0) < 15:
        return None
    try:
        p = _Pool(cfg.database_url, int(os.getenv("PG_POOL", "8")))
        p.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id BIGSERIAL PRIMARY KEY,
                ts TIMESTAMPTZ NOT NULL,
                slug TEXT NOT NULL,
                city TEXT,
                market_date DATE,
                signal BOOLEAN NOT NULL,
                mode TEXT,
                payload JSONB NOT NULL
            )""")
        p.execute("CREATE INDEX IF NOT EXISTS ix_snap_slug ON snapshots(slug)")
        p.execute("CREATE INDEX IF NOT EXISTS ix_snap_ts ON snapshots(ts)")
        p.execute("""CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY, v TEXT, ts TIMESTAMPTZ DEFAULT now())""")
        _pg = p
        _pg_versuch["ts"] = _t.time()
        log.info(f"postgres snapshot store ready (pool {p.groesse})")
    except Exception as e:
        log.warning(f"postgres unavailable, JSONL only: {e}")
        _pg = None
        _pg_versuch["fehl"] = _t.time()
    return _pg


def kv_get(key):
    """Kleiner Schluessel-Wert-Speicher. Gebraucht fuer die ID der
    angehefteten Live-Tafel, die bei jedem Scan ueberschrieben wird."""
    pg = _init_pg()
    if not pg:
        return None
    try:
        r = pg.execute("SELECT v FROM kv WHERE k=%s", (key,)).fetchone()
        return r[0] if r else None
    except Exception as e:
        log.debug(f"kv_get {key}: {e}")
        return None


def kv_alive():
    """True, wenn die Datenbank gerade antwortet. Leeres kv_get heisst sonst
    sowohl "nichts gespeichert" als auch "Datenbank weg"."""
    pg = _init_pg()
    if not pg:
        return False
    try:
        pg.execute("SELECT 1").fetchone()
        return True
    except Exception as e:
        log.debug(f"kv_alive: {e}")
        return False


def kv_set(key, value):
    pg = _init_pg()
    if not pg:
        return False
    try:
        pg.execute("""INSERT INTO kv (k, v, ts) VALUES (%s, %s, now())
                      ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, ts=now()""",
                   (key, str(value)))
        return True
    except Exception as e:
        log.debug(f"kv_set {key}: {e}")
        return False


def save(snap):
    # Den neuen Eintrag an den Zwischenspeicher ANHAENGEN statt ihn zu
    # verwerfen. Geschrieben wird bei jedem Markt — wuerde man verwerfen,
    # laedt der naechste Markt die ganze Tabelle erneut, und der Speicher
    # brächte nichts.
    if _cache["rows"] is not None:
        try:
            _cache["rows"].append(snap)
        except Exception:
            _cache["rows"] = None
    """Append one snapshot. Never updates, never deletes."""
    os.makedirs(cfg.data_dir, exist_ok=True)
    path = os.path.join(cfg.data_dir, "snapshots.jsonl")
    try:
        with open(path, "a") as f:
            f.write(json.dumps(snap, default=str) + "\n")
    except Exception as e:
        log.error(f"jsonl write failed: {e}")

    pg = _init_pg()
    if pg:
        try:
            pg.execute(
                "INSERT INTO snapshots (ts, slug, city, market_date, signal, mode, payload)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                # slug ist NOT NULL. Eintraege ohne Markt (z.B. die
                # Bilanzmeldung) bekommen ihre Art als slug, sonst scheitert
                # das Schreiben still und die Meldung geht jede Stunde neu raus.
                (snap.get("ts"), snap.get("slug") or snap.get("kind") or "none",
                 snap.get("city"),
                 snap.get("date"), snap.get("signal"), snap.get("mode"),
                 json.dumps(snap, default=str)))
        except Exception as e:
            log.error(f"postgres insert failed: {e}")


_cache = {"ts": 0.0, "rows": None}


def load_all(max_alter_s=90):
    """Read every snapshot back. Prefers Postgres so every service sees the
    same data (the worker writes, the web service reads); falls back to the
    local JSONL only when there is no database.

    Kurz zwischengespeichert. Ein Scan ruft das bis zu dreimal je Markt auf —
    fuer den Cooldown, fuer die zuletzt veroeffentlichte Fassung und fuer die
    Mindestpause. Bei 120 Maerkten waeren das ueber 300 vollstaendige
    Lesevorgaenge pro Durchlauf ueber eine Tabelle, die stuendlich waechst.
    Die Fragen dahinter bewegen sich in Stunden, 90 Sekunden Alter schaden
    also nicht.
    """
    import time as _t
    if (_cache["rows"] is not None
            and _t.time() - _cache["ts"] < max_alter_s):
        return _cache["rows"]
    rows_out = _load_all_frisch()
    _cache["rows"], _cache["ts"] = rows_out, _t.time()
    return rows_out


def _load_all_frisch():
    pg = _init_pg()
    if pg:
        try:
            rows = pg.execute("SELECT payload FROM snapshots ORDER BY ts").fetchall()
            out = []
            for (payload,) in rows:
                if isinstance(payload, dict):
                    out.append(payload)
                else:
                    try:
                        out.append(json.loads(payload))
                    except Exception:
                        continue
            return out
        except Exception as e:
            log.error(f"postgres read failed, falling back to jsonl: {e}")
    path = os.path.join(cfg.data_dir, "snapshots.jsonl")
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def posted_recently(slug, hours):
    """Avoid spamming the channel with the same market every scan."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    for s in reversed(load_all()):
        if s.get("slug") != slug or not s.get("posted"):
            continue
        try:
            ts = datetime.fromisoformat(s["ts"]).timestamp()
        except Exception:
            continue
        if ts > cutoff:
            return True
    return False


def last_posted(slug):
    """Der zuletzt VEROEFFENTLICHTE Schnappschuss dieses Marktes, sonst None.

    Wird gebraucht, um zu erkennen, ob sich seither etwas geaendert hat:
    ein anderer Favorit, oder ein deutlich gefallener Preis.
    """
    for s in reversed(load_all()):
        if s.get("slug") == slug and s.get("posted"):
            return s
    return None


def posted_today():
    """How many signals already went out in the last 24h. Feeds the daily cap."""
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    n = 0
    for s in load_all():
        if not s.get("posted"):
            continue
        try:
            if datetime.fromisoformat(s["ts"]).timestamp() > cutoff:
                n += 1
        except Exception:
            continue
    return n


def record_posted_today():
    """The record goes out once a day, not once per scan."""
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    for s in load_all():
        if s.get("kind") != "record_post":
            continue
        try:
            if datetime.fromisoformat(s["ts"]).timestamp() > cutoff:
                return True
        except Exception:
            continue
    return False


def last_record_n():
    """Highest settled-signal count at which a record post has already gone out.
    Lets the caller fire only once per 10-signal milestone."""
    best = 0
    for s in load_all():
        if s.get("kind") == "record_post":
            try:
                best = max(best, int(s.get("n", 0)))
            except Exception:
                continue
    return best
