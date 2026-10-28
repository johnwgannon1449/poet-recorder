"""Import the orders spreadsheet.  python import_orders.py orders.xlsx [--reps "Mike, Dave"]"""
import argparse
import csv
import sys
from pathlib import Path

import db

REQUIRED = ["order_no", "line_no", "sku", "description"]
OPTIONAL = ["qty", "uom", "rep", "branch", "division", "cust_type", "cust_size", "region", "order_date"]
# Column names as the MIS counter-sales export spells them → ours. Customer name/account columns are never read.
ALIASES = {"invoice number": "order_no", "line#": "line_no", "code (product)": "sku", "name (product)": "description",
           "selling unit (product)": "uom", "quantity": "qty", "qty shipped": "qty", "ship branch": "branch",
           "name (division)": "division", "type (price code)": "cust_type", "size (price code)": "cust_size",
           "name (customer region)": "region", "order date": "order_date"}


def read_rows(path):
    """Yield dicts keyed by lower-cased header. Unknown columns are dropped (so customer data never gets in)."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        rows = ws.iter_rows(values_only=True)
        header = [norm(h) for h in next(rows, [])]
        raw = (dict(zip(header, r)) for r in rows)
    else:
        f = open(path, newline="", encoding="utf-8-sig")
        reader = csv.reader(f)
        header = [norm(h) for h in next(reader, [])]
        raw = (dict(zip(header, r)) for r in reader)
    missing = [h for h in REQUIRED if h not in header]
    if missing:
        sys.exit(f"ERROR: {path.name} is missing required column(s): {', '.join(missing)}\n"
                 f"Found: {', '.join(h for h in header if h)}\nRequired: {', '.join(REQUIRED)}; optional: {', '.join(OPTIONAL)}")
    for r in raw:
        if all(v in (None, "") for v in r.values()):
            continue
        yield {k: r.get(k) for k in REQUIRED + OPTIONAL}


def norm(h):
    h = str(h or "").strip().lower()
    return ALIASES.get(h, h)


def text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def import_file(path, extra_reps=()):
    conn = db.connect()
    n = 0
    with conn:
        for name in extra_reps:
            if name.strip():
                db.upsert_rep(conn, name)
        for i, r in enumerate(read_rows(path), start=2):
            order_no, sku, desc = text(r["order_no"]), text(r["sku"]), text(r["description"])
            if not order_no:
                sys.exit(f"ERROR: row {i}: order_no is blank")
            try:
                line_no, qty = int(float(text(r["line_no"]))), (float(text(r.get("qty"))) if text(r.get("qty")) else None)
            except ValueError:
                sys.exit(f"ERROR: row {i} (order {order_no}): line_no must be an integer and qty a number")
            rep_name = text(r.get("rep"))
            rep_slug = db.upsert_rep(conn, rep_name) if rep_name else None
            meta = {m: text(r.get(m)) for m in db.ORDER_META}
            db.upsert_line(conn, order_no, line_no, qty, sku, desc, text(r.get("uom")), rep_slug, i, meta)
            n += 1
    return conn, n


def summary(conn):
    lines = [f"{'rep':<20}{'orders':>8}{'lines':>8}"]
    for r in conn.execute("""SELECT COALESCE(p.name,'(pool)') AS name, COUNT(DISTINCT o.order_no) AS orders, COUNT(l.line_no) AS lines
                             FROM orders o LEFT JOIN reps p ON p.slug=o.rep LEFT JOIN order_lines l USING (order_no)
                             GROUP BY o.rep ORDER BY o.rep IS NULL, name"""):
        lines.append(f"{r['name']:<20}{r['orders']:>8}{r['lines']:>8}")
    for r in conn.execute("SELECT name FROM reps WHERE slug NOT IN (SELECT rep FROM orders WHERE rep IS NOT NULL) ORDER BY name"):
        lines.append(f"{r['name']:<20}{0:>8}{0:>8}")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="orders .xlsx or .csv")
    ap.add_argument("--reps", default="", help='comma list of reps with no assignments yet, e.g. "Mike, Dave"')
    a = ap.parse_args()
    conn, n = import_file(a.file, [s for s in a.reps.split(",")])
    print(f"Imported {n} line(s) from {a.file} into {db.DB_PATH}\n")
    print(summary(conn))
