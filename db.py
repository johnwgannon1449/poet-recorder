"""SQLite layer for the POET Counter Recorder. Everything lives under DATA_DIR."""
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("RECORDER_DATA", Path(__file__).parent / "data"))
DB_PATH = DATA_DIR / "recorder.db"
AUDIO_DIR = DATA_DIR / "audio"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reps (slug TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS orders (order_no TEXT PRIMARY KEY, rep TEXT, seq INTEGER, branch TEXT, division TEXT,
  cust_type TEXT, cust_size TEXT, region TEXT, order_date TEXT);
CREATE TABLE IF NOT EXISTS order_lines (
  order_no TEXT, line_no INTEGER, qty REAL, uom TEXT, sku TEXT, description TEXT, seq INTEGER,
  PRIMARY KEY (order_no, line_no));
CREATE TABLE IF NOT EXISTS takes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rep TEXT, order_no TEXT, line_no INTEGER, take_no INTEGER,
  recorded_at TEXT, duration_s REAL, src_path TEXT, wav_path TEXT, status TEXT DEFAULT 'saved', user_agent TEXT, skipped_lines TEXT);
CREATE INDEX IF NOT EXISTS takes_line ON takes (order_no, line_no, rep, status);
CREATE TABLE IF NOT EXISTS line_status (rep TEXT, order_no TEXT, line_no INTEGER, skipped INTEGER DEFAULT 0,
  PRIMARY KEY (rep, order_no, line_no));
CREATE TABLE IF NOT EXISTS order_status (rep TEXT, order_no TEXT, completed_at TEXT, skipped INTEGER DEFAULT 0,
  PRIMARY KEY (rep, order_no));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, rep TEXT, kind TEXT,
  order_no TEXT, line_no INTEGER, take_id INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS nav (id INTEGER PRIMARY KEY AUTOINCREMENT, rep TEXT, order_no TEXT);
"""


def configure(data_dir):
    """Point the module at another data folder (tests do this)."""
    global DATA_DIR, DB_PATH, AUDIO_DIR
    DATA_DIR = Path(data_dir)
    DB_PATH = DATA_DIR / "recorder.db"
    AUDIO_DIR = DATA_DIR / "audio"


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for table, col in [("orders", c) for c in ("branch", "division", "cust_type", "cust_size", "region", "order_date")] + [("takes", "skipped_lines")]:
        if col not in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]:   # upgrade an older data folder
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "rep"


def safe_name(s):
    """Filesystem-safe version of an order number or similar."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)) or "x"


def log(conn, rep, kind, order_no=None, line_no=None, take_id=None, detail=None):
    conn.execute("INSERT INTO events (ts, rep, kind, order_no, line_no, take_id, detail) VALUES (?,?,?,?,?,?,?)",
                 (now(), rep, kind, order_no, line_no, take_id, detail))


# ---------- reps / orders ----------

def upsert_rep(conn, name):
    slug = slugify(name)
    conn.execute("INSERT INTO reps (slug, name, created_at) VALUES (?,?,?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name",
                 (slug, name.strip(), now()))
    return slug


def reps(conn):
    return [dict(r) for r in conn.execute("SELECT slug, name FROM reps ORDER BY name")]


def rep(conn, slug):
    r = conn.execute("SELECT slug, name FROM reps WHERE slug=?", (slug,)).fetchone()
    return dict(r) if r else None


ORDER_META = ("branch", "division", "cust_type", "cust_size", "region", "order_date")


def upsert_line(conn, order_no, line_no, qty, sku, description, uom, rep_slug, seq, meta=None):
    meta = meta or {}
    conn.execute(f"""INSERT INTO orders (order_no, rep, seq, {", ".join(ORDER_META)}) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(order_no) DO UPDATE SET rep=excluded.rep, seq=MIN(orders.seq, excluded.seq),
                    {", ".join(f"{m}=COALESCE(excluded.{m}, orders.{m})" for m in ORDER_META)}""",
                 (order_no, rep_slug, seq, *[meta.get(m) or None for m in ORDER_META]))
    conn.execute("""INSERT INTO order_lines (order_no, line_no, qty, uom, sku, description, seq) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(order_no, line_no) DO UPDATE SET qty=excluded.qty, uom=excluded.uom, sku=excluded.sku,
                    description=excluded.description, seq=excluded.seq""",
                 (order_no, line_no, qty, uom, sku, description, seq))


def order_lines(conn, order_no):
    return [dict(r) for r in conn.execute(
        "SELECT order_no, line_no, qty, uom, sku, description FROM order_lines WHERE order_no=? ORDER BY seq, line_no", (order_no,))]


# ---------- per-rep state ----------

def is_order_complete(conn, slug, order_no):
    """The rep has read this order at least once (a saved whole-order take)."""
    return conn.execute("SELECT COUNT(*) FROM takes WHERE rep=? AND order_no=? AND line_no=0 AND status='saved'",
                        (slug, order_no)).fetchone()[0] > 0


def refresh_order_status(conn, slug, order_no):
    """Keep order_status.completed_at in step with the takes and skips."""
    row = conn.execute("SELECT completed_at FROM order_status WHERE rep=? AND order_no=?", (slug, order_no)).fetchone()
    done = is_order_complete(conn, slug, order_no)
    if done and not (row and row["completed_at"]):
        conn.execute("INSERT INTO order_status (rep, order_no, completed_at, skipped) VALUES (?,?,?,0) "
                     "ON CONFLICT(rep, order_no) DO UPDATE SET completed_at=excluded.completed_at", (slug, order_no, now()))
        log(conn, slug, "order_complete", order_no)
    elif not done and row and row["completed_at"]:
        conn.execute("UPDATE order_status SET completed_at=NULL WHERE rep=? AND order_no=?", (slug, order_no))
        log(conn, slug, "order_reopened", order_no)


def order_state(conn, slug, order_no):
    r = conn.execute("SELECT completed_at, skipped FROM order_status WHERE rep=? AND order_no=?", (slug, order_no)).fetchone()
    return {"completed": bool(r and r["completed_at"]), "skipped": bool(r and r["skipped"])}


def queue(conn, slug):
    """[(order_no, section)] in the order this rep should see them: own → pool → others' (least recorded first)."""
    own = [r[0] for r in conn.execute("SELECT order_no FROM orders WHERE rep=? ORDER BY seq", (slug,))]
    pool = [r[0] for r in conn.execute("SELECT order_no FROM orders WHERE rep IS NULL OR rep='' ORDER BY seq")]
    others = [r[0] for r in conn.execute("""
        SELECT o.order_no FROM orders o WHERE o.rep IS NOT NULL AND o.rep != '' AND o.rep != ?
        ORDER BY (SELECT COUNT(*) FROM order_status s WHERE s.order_no=o.order_no AND s.completed_at IS NOT NULL),
                 (SELECT COUNT(*) FROM takes t WHERE t.order_no=o.order_no AND t.status='saved'), o.seq""", (slug,))]
    return [(o, "own") for o in own] + [(o, "pool") for o in pool] + [(o, "others") for o in others]


def first_pending(conn, slug, exclude=()):
    for order_no, section in queue(conn, slug):
        if order_no in exclude:
            continue
        st = order_state(conn, slug, order_no)
        if not st["completed"] and not st["skipped"]:
            return order_no, section
    return None, None


def section_of(conn, slug, order_no):
    for o, section in queue(conn, slug):
        if o == order_no:
            return section
    return "own"


def nav_tail(conn, slug):
    r = conn.execute("SELECT order_no FROM nav WHERE rep=? ORDER BY id DESC LIMIT 1", (slug,)).fetchone()
    return r["order_no"] if r else None


def current_order(conn, slug):
    """The order this rep is looking at (tail of nav), opening the first pending one if nothing is open."""
    order_no = nav_tail(conn, slug)
    if order_no:
        return order_no
    order_no, _ = first_pending(conn, slug)
    if order_no:
        open_order(conn, slug, order_no)
    return order_no


def open_order(conn, slug, order_no):
    conn.execute("INSERT INTO nav (rep, order_no) VALUES (?,?)", (slug, order_no))
    log(conn, slug, "order_open", order_no)


def next_order(conn, slug):
    cur = nav_tail(conn, slug)
    if cur is None:
        return current_order(conn, slug)
    order_no, _ = first_pending(conn, slug, exclude={cur})
    if order_no:
        open_order(conn, slug, order_no)
    return order_no


def prev_order(conn, slug):
    rows = conn.execute("SELECT id FROM nav WHERE rep=? ORDER BY id DESC LIMIT 2", (slug,)).fetchall()
    if len(rows) == 2:
        conn.execute("DELETE FROM nav WHERE id=?", (rows[0]["id"],))
        log(conn, slug, "order_back", current_order(conn, slug))
    return current_order(conn, slug)


def has_prev(conn, slug):
    return conn.execute("SELECT COUNT(*) FROM nav WHERE rep=?", (slug,)).fetchone()[0] > 1


def set_line_skipped(conn, slug, order_no, line_no, skipped):
    conn.execute("INSERT INTO line_status (rep, order_no, line_no, skipped) VALUES (?,?,?,?) "
                 "ON CONFLICT(rep, order_no, line_no) DO UPDATE SET skipped=excluded.skipped",
                 (slug, order_no, line_no, 1 if skipped else 0))
    log(conn, slug, "line_skip" if skipped else "line_unskip", order_no, line_no)


def skip_order(conn, slug, order_no):
    conn.execute("INSERT INTO order_status (rep, order_no, skipped) VALUES (?,?,1) "
                 "ON CONFLICT(rep, order_no) DO UPDATE SET skipped=1", (slug, order_no))
    log(conn, slug, "order_skip", order_no)


def next_take_no(conn, slug, order_no, line_no=0):
    r = conn.execute("SELECT COALESCE(MAX(take_no),0) FROM takes WHERE rep=? AND order_no=? AND line_no=?",
                     (slug, order_no, line_no)).fetchone()
    return r[0] + 1


def takes_for_order(conn, slug, order_no):
    return [dict(r) for r in conn.execute("SELECT id, take_no, duration_s, recorded_at, skipped_lines FROM takes "
                                          "WHERE rep=? AND order_no=? AND line_no=0 AND status='saved' ORDER BY take_no", (slug, order_no))]


def skipped_lines(conn, slug, order_no):
    return {r[0] for r in conn.execute("SELECT line_no FROM line_status WHERE rep=? AND order_no=? AND skipped=1", (slug, order_no))}


# ---------- counts ----------

RECORDED_LINES = """FROM order_lines l WHERE EXISTS (SELECT 1 FROM takes t WHERE t.rep=? AND t.order_no=l.order_no AND t.line_no=0 AND t.status='saved')
    AND NOT EXISTS (SELECT 1 FROM line_status s WHERE s.rep=? AND s.order_no=l.order_no AND s.line_no=l.line_no AND s.skipped=1)"""


def rep_counts(conn, slug):
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    c = {
        "assigned_orders": q("SELECT COUNT(*) FROM orders WHERE rep=?", slug),
        "orders_done": q("SELECT COUNT(*) FROM order_status WHERE rep=? AND completed_at IS NOT NULL", slug),
        "orders_skipped": q("SELECT COUNT(*) FROM order_status WHERE rep=? AND skipped=1 AND completed_at IS NULL", slug),
        "assigned_lines": q("SELECT COUNT(*) FROM order_lines l JOIN orders o USING (order_no) WHERE o.rep=?", slug),
        "lines_recorded": q("SELECT COUNT(*) " + RECORDED_LINES, slug, slug),
        "lines_skipped": q("SELECT COUNT(*) FROM line_status WHERE rep=? AND skipped=1", slug),
        "takes": q("SELECT COUNT(*) FROM takes WHERE rep=? AND status='saved'", slug),
        "minutes": round((q("SELECT COALESCE(SUM(duration_s),0) FROM takes WHERE rep=? AND status='saved'", slug)) / 60, 1),
        "last_activity": q("SELECT MAX(ts) FROM events WHERE rep=? AND kind IN ('take','take_delete','line_skip','order_skip')", slug),
    }
    c["remaining_orders"] = q("""SELECT COUNT(*) FROM orders o WHERE o.rep=? AND NOT EXISTS
        (SELECT 1 FROM order_status s WHERE s.rep=o.rep AND s.order_no=o.order_no AND (s.completed_at IS NOT NULL OR s.skipped=1))""", slug)
    c["remaining_lines"] = q("""SELECT COUNT(*) FROM order_lines l JOIN orders o USING (order_no) WHERE o.rep=? AND NOT EXISTS
        (SELECT 1 FROM order_status s WHERE s.rep=o.rep AND s.order_no=o.order_no AND (s.completed_at IS NOT NULL OR s.skipped=1))""", slug)
    return c


def coverage(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    orders_total, lines_total = q("SELECT COUNT(*) FROM orders"), q("SELECT COUNT(*) FROM order_lines")
    orders_done = q("SELECT COUNT(DISTINCT order_no) FROM order_status WHERE completed_at IS NOT NULL")
    lines_done = q("""SELECT COUNT(*) FROM order_lines l WHERE EXISTS (SELECT 1 FROM takes t WHERE t.order_no=l.order_no AND t.line_no=0
        AND t.status='saved' AND NOT EXISTS (SELECT 1 FROM line_status s WHERE s.rep=t.rep AND s.order_no=l.order_no AND s.line_no=l.line_no AND s.skipped=1))""")
    pct = lambda a, b: round(100 * a / b, 1) if b else 0
    return {"orders_total": orders_total, "orders_done": orders_done, "orders_pct": pct(orders_done, orders_total),
            "lines_total": lines_total, "lines_done": lines_done, "lines_pct": pct(lines_done, lines_total)}
