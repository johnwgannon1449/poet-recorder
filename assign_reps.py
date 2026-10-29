"""Pick a batch of orders from the MIS counter-sales export and assign them to reps.

    python assign_reps.py Export.csv --reps "Eric, John" --per-rep 20 --out batch1.xlsx
    python assign_reps.py Export.csv --reps "Eric, John" --per-rep 20 --focus "Eric=FIRE PROTECTION|WATER WORK UTILITIES" --out batch2.xlsx
    python assign_reps.py Export.csv --qty Export-qty.xlsx --same-as batch1.xlsx --out batch1.xlsx   # re-cut a batch with quantities added

Writes a spreadsheet ready for import_orders.py (only the chosen orders, with a rep column). Orders already in the
database are never picked again. Each rep gets a spread across divisions; --focus narrows a rep to the divisions
he knows best. Lines per order are kept between --min-lines and --max-lines so nobody gets 196-line orders.
"""
import argparse
import random
import sys
from collections import defaultdict

import openpyxl

import db
import import_orders

COLS = ["order_no", "line_no", "qty", "uom", "sku", "description", "rep", "branch", "division", "cust_type", "cust_size", "region", "order_date"]


def load_orders(path, qty_file=None):
    """Orders → lines from the MIS export. --qty merges a (Invoice Number, Line#, Code (Product), Quantity Ordered) export;
    lines with qty <= 0 are left out."""
    qty = {}
    if qty_file:
        for r in import_orders.read_rows(qty_file, required=["order_no", "line_no", "sku", "qty"]):
            qty[(import_orders.text(r["order_no"]), import_orders.text(r["line_no"]), import_orders.text(r["sku"]))] = import_orders.text(r["qty"])
    orders = defaultdict(list)
    for r in import_orders.read_rows(path):
        line = {k: import_orders.text(r.get(k)) for k in COLS if k != "rep"}
        if qty_file:
            line["qty"] = qty.get((line["order_no"], line["line_no"], line["sku"]), "")
        if line["qty"] and float(line["qty"]) <= 0:
            continue
        orders[line["order_no"]].append(line)
    return orders


def same_as(path):
    """Reuse the rep assignments of an earlier batch file: {order_no: rep}."""
    return {import_orders.text(r["order_no"]): import_orders.text(r["rep"]) for r in import_orders.read_rows(path, required=["order_no", "rep"])}


def pick(orders, reps, per_rep, focus, min_lines, max_lines, seed):
    rng = random.Random(seed)
    already = set()
    if db.DB_PATH.exists():
        already = {r[0] for r in db.connect().execute("SELECT order_no FROM orders")}
    pool = {o: ls for o, ls in orders.items() if o not in already and min_lines <= len(ls) <= max_lines}
    by_div = defaultdict(list)
    for o, ls in pool.items():
        by_div[ls[0]["division"]].append(o)
    for v in by_div.values():
        rng.shuffle(v)
    chosen = {}
    for rep in reps:
        if rep in focus:   # exact division names first ("PLUMBING" ≠ "FINISH PLUMBING"); fall back to substring
            wanted = [f.lower() for f in focus[rep]]
            divs = [d for d in by_div if d.lower() in wanted] or [d for d in by_div if any(f in d.lower() for f in wanted)]
        else:
            divs = list(by_div)
        if not divs:
            sys.exit(f"ERROR: no divisions match --focus for {rep}: {focus[rep]}\nDivisions: {sorted(by_div)}")
        weights = [len(by_div[d]) for d in divs]
        n = 0
        while n < per_rep and any(by_div[d] for d in divs):
            d = rng.choices(divs, weights=[len(by_div[x]) for x in divs])[0] if sum(weights) else divs[0]
            if by_div[d]:
                chosen[by_div[d].pop()] = rep
                n += 1
            weights = [len(by_div[x]) for x in divs]
    return chosen


def write_xlsx(path, orders, chosen):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "orders"
    ws.append(COLS)
    for o in sorted(chosen, key=lambda o: (chosen[o], o)):
        for l in sorted(orders[o], key=lambda l: int(float(l["line_no"]))):
            ws.append([l.get(c, "") if c != "rep" else chosen[o] for c in COLS])
    wb.save(path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="MIS export (.csv or .xlsx)")
    ap.add_argument("--reps", default="", help='"Eric, John"')
    ap.add_argument("--qty", help="MIS quantity export to merge (Invoice Number, Line#, Code (Product), Quantity Ordered)")
    ap.add_argument("--same-as", help="earlier batch .xlsx: keep its orders and rep assignments, just refresh the lines")
    ap.add_argument("--per-rep", type=int, default=20)
    ap.add_argument("--focus", default="", help='"Eric=WATER WORK UTILITIES; John=PLUMBING"  (exact division name, else substring)')
    ap.add_argument("--min-lines", type=int, default=2)
    ap.add_argument("--max-lines", type=int, default=15)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="batch.xlsx")
    a = ap.parse_args()
    reps = [r.strip() for r in a.reps.split(",") if r.strip()]
    focus = {k.strip(): [d.strip() for d in v.split("|")] for k, v in
             (item.split("=", 1) for item in a.focus.split(";") if "=" in item)}
    orders = load_orders(a.file, a.qty)
    if a.same_as:
        chosen = {o: r for o, r in same_as(a.same_as).items() if o in orders}
        reps = sorted(set(chosen.values()))
    elif reps:
        chosen = pick(orders, reps, a.per_rep, focus, a.min_lines, a.max_lines, a.seed)
    else:
        sys.exit("ERROR: give --reps to pick a new batch, or --same-as to re-cut an old one")
    write_xlsx(a.out, orders, chosen)
    print(f"{len(orders)} orders in {a.file}; picked {len(chosen)} → {a.out}\n")
    for rep in reps:
        mine = [o for o in chosen if chosen[o] == rep]
        divs = defaultdict(int)
        for o in mine:
            divs[orders[o][0]["division"]] += 1
        print(f"{rep}: {len(mine)} orders, {sum(len(orders[o]) for o in mine)} lines — " + ", ".join(f"{d} {n}" for d, n in sorted(divs.items(), key=lambda x: -x[1])))
