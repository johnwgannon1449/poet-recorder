"""Export clean CSVs (+ WAVs) for Ryan.  python export.py --out export/ [--hash-orders] [--copy-audio]"""
import argparse
import csv
import hashlib
import os
import shutil
import sys
from pathlib import Path

import db


def load_salt():
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ORDER_HASH_SALT="):
                return line.split("=", 1)[1].strip().strip('"\'')
    return os.environ.get("ORDER_HASH_SALT", "")


def export(out, hash_orders=False, copy_audio=False):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    salt = load_salt() if hash_orders else ""
    if hash_orders and not salt:
        sys.exit("ERROR: --hash-orders needs ORDER_HASH_SALT in .env (see README)")
    conn = db.connect()
    names = {r["slug"]: r["name"] for r in db.reps(conn)}
    rep_name = lambda s: names.get(s, s or "")
    line_nos = {}
    for r in conn.execute("SELECT order_no, line_no FROM order_lines ORDER BY seq, line_no"):
        line_nos.setdefault(r["order_no"], []).append(r["line_no"])

    def ident(order_no):
        h = hashlib.sha256((salt + order_no).encode()).hexdigest() if hash_orders else ""
        return {"order_no": "" if hash_orders else order_no, "order_hash": h}

    def write(name, cols, rows):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    # takes.csv — one row per whole-order read; which lines were read vs X'd is the snapshot taken at record time
    takes, missing_wav, read_by_line = [], 0, {}
    if copy_audio:
        (out / "audio").mkdir(exist_ok=True)
    for t in conn.execute("SELECT * FROM takes WHERE status='saved' ORDER BY order_no, rep, take_no"):
        skipped = {int(x) for x in (t["skipped_lines"] or "").split(";") if x}
        read = [n for n in line_nos.get(t["order_no"], []) if n not in skipped]
        for n in read:
            read_by_line[(t["order_no"], n)] = read_by_line.get((t["order_no"], n), 0) + 1
        wav = t["wav_path"] or ""
        if copy_audio and wav and (db.DATA_DIR / wav).exists():
            shutil.copy2(db.DATA_DIR / wav, out / "audio" / f"{t['id']}.wav")
            wav = f"audio/{t['id']}.wav"
        elif not wav or not (db.DATA_DIR / wav).exists():
            missing_wav += 1
        takes.append({"take_id": t["id"], "rep": rep_name(t["rep"]), **ident(t["order_no"]), "take_no": t["take_no"],
                      "n_lines": len(line_nos.get(t["order_no"], [])), "lines_read": ";".join(map(str, read)),
                      "lines_skipped": ";".join(map(str, sorted(skipped))), "duration_s": t["duration_s"], "wav_path": wav,
                      "recorded_at": t["recorded_at"], "transcript": ""})
    write("takes.csv", ["take_id", "rep", "order_no", "order_hash", "take_no", "n_lines", "lines_read", "lines_skipped",
                        "duration_s", "wav_path", "recorded_at", "transcript"], takes)

    lines = []
    for l in conn.execute("SELECT l.*, o.rep AS assigned_rep FROM order_lines l JOIN orders o USING (order_no) ORDER BY o.seq, l.seq"):
        skipped = [rep_name(r[0]) for r in conn.execute("SELECT rep FROM line_status WHERE order_no=? AND line_no=? AND skipped=1 ORDER BY rep",
                                                        (l["order_no"], l["line_no"]))]
        lines.append({**ident(l["order_no"]), "line_no": l["line_no"], "qty": l["qty"], "uom": l["uom"], "sku": l["sku"],
                      "description": l["description"], "assigned_rep": rep_name(l["assigned_rep"]),
                      "n_takes": read_by_line.get((l["order_no"], l["line_no"]), 0), "skipped_by": ";".join(skipped)})
    write("lines.csv", ["order_no", "order_hash", "line_no", "qty", "uom", "sku", "description", "assigned_rep", "n_takes", "skipped_by"], lines)

    orders = []
    for o in conn.execute("SELECT o.*, (SELECT COUNT(*) FROM order_lines l WHERE l.order_no=o.order_no) AS n, "
                          "(SELECT COUNT(*) FROM takes t WHERE t.order_no=o.order_no AND t.status='saved') AS n_takes FROM orders o ORDER BY o.seq"):
        done = [rep_name(r[0]) for r in conn.execute("SELECT rep FROM order_status WHERE order_no=? AND completed_at IS NOT NULL ORDER BY rep", (o["order_no"],))]
        skipped = [rep_name(r[0]) for r in conn.execute("SELECT rep FROM order_status WHERE order_no=? AND skipped=1 AND completed_at IS NULL ORDER BY rep", (o["order_no"],))]
        orders.append({**ident(o["order_no"]), "n_lines": o["n"], "assigned_rep": rep_name(o["rep"]), "completed_by": ";".join(done),
                       "skipped_by": ";".join(skipped), "n_takes": o["n_takes"], **{m: o[m] or "" for m in db.ORDER_META}})
    write("orders.csv", ["order_no", "order_hash", "n_lines", "assigned_rep", "completed_by", "skipped_by", "n_takes", *db.ORDER_META], orders)
    return {"takes": len(takes), "lines": len(lines), "orders": len(orders), "missing_wav": missing_wav}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="export")
    ap.add_argument("--hash-orders", action="store_true", help="replace order_no with sha256(SALT + order_no); use for anything leaving PACE")
    ap.add_argument("--copy-audio", action="store_true", help="copy WAVs into <out>/audio/{take_id}.wav")
    a = ap.parse_args()
    r = export(a.out, a.hash_orders, a.copy_audio)
    print(f"Wrote {a.out}/takes.csv ({r['takes']} takes), lines.csv ({r['lines']}), orders.csv ({r['orders']})")
    if r["missing_wav"]:
        print(f"WARNING: {r['missing_wav']} take(s) have no WAV yet (ffmpeg still running or failed; see events table)")
