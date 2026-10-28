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

    def ident(order_no):
        h = hashlib.sha256((salt + order_no).encode()).hexdigest() if hash_orders else ""
        return {"order_no": "" if hash_orders else order_no, "order_hash": h}

    def write(name, cols, rows):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    takes, missing_wav = [], 0
    if copy_audio:
        (out / "audio").mkdir(exist_ok=True)
    for t in conn.execute("""SELECT t.*, l.qty, l.uom, l.sku, l.description FROM takes t
                             JOIN order_lines l ON l.order_no=t.order_no AND l.line_no=t.line_no
                             WHERE t.status='saved' ORDER BY t.order_no, t.line_no, t.rep, t.take_no"""):
        wav = t["wav_path"] or ""
        if copy_audio and wav and (db.DATA_DIR / wav).exists():
            shutil.copy2(db.DATA_DIR / wav, out / "audio" / f"{t['id']}.wav")
            wav = f"audio/{t['id']}.wav"
        elif not wav or not (db.DATA_DIR / wav).exists():
            missing_wav += 1
        takes.append({"take_id": t["id"], "rep": rep_name(t["rep"]), **ident(t["order_no"]), "line_no": t["line_no"],
                      "take_no": t["take_no"], "qty": t["qty"], "uom": t["uom"], "sku": t["sku"], "description": t["description"],
                      "duration_s": t["duration_s"], "wav_path": wav, "recorded_at": t["recorded_at"], "transcript": ""})
    write("takes.csv", ["take_id", "rep", "order_no", "order_hash", "line_no", "take_no", "qty", "uom", "sku", "description",
                        "duration_s", "wav_path", "recorded_at", "transcript"], takes)

    lines = []
    for l in conn.execute("SELECT l.*, o.rep AS assigned_rep FROM order_lines l JOIN orders o USING (order_no) ORDER BY o.seq, l.seq"):
        n = conn.execute("SELECT COUNT(*) FROM takes WHERE order_no=? AND line_no=? AND status='saved'", (l["order_no"], l["line_no"])).fetchone()[0]
        skipped = [rep_name(r[0]) for r in conn.execute("""SELECT s.rep FROM line_status s WHERE s.order_no=? AND s.line_no=? AND s.skipped=1
            AND NOT EXISTS (SELECT 1 FROM takes t WHERE t.rep=s.rep AND t.order_no=s.order_no AND t.line_no=s.line_no AND t.status='saved')
            ORDER BY s.rep""", (l["order_no"], l["line_no"]))]
        lines.append({**ident(l["order_no"]), "line_no": l["line_no"], "qty": l["qty"], "uom": l["uom"], "sku": l["sku"],
                      "description": l["description"], "assigned_rep": rep_name(l["assigned_rep"]), "n_takes": n, "skipped_by": ";".join(skipped)})
    write("lines.csv", ["order_no", "order_hash", "line_no", "qty", "uom", "sku", "description", "assigned_rep", "n_takes", "skipped_by"], lines)

    orders = []
    for o in conn.execute("SELECT o.order_no, o.rep, (SELECT COUNT(*) FROM order_lines l WHERE l.order_no=o.order_no) AS n FROM orders o ORDER BY o.seq"):
        done = [rep_name(r[0]) for r in conn.execute("SELECT rep FROM order_status WHERE order_no=? AND completed_at IS NOT NULL ORDER BY rep", (o["order_no"],))]
        skipped = [rep_name(r[0]) for r in conn.execute("SELECT rep FROM order_status WHERE order_no=? AND skipped=1 AND completed_at IS NULL ORDER BY rep", (o["order_no"],))]
        orders.append({**ident(o["order_no"]), "n_lines": o["n"], "assigned_rep": rep_name(o["rep"]),
                       "completed_by": ";".join(done), "skipped_by": ";".join(skipped)})
    write("orders.csv", ["order_no", "order_hash", "n_lines", "assigned_rep", "completed_by", "skipped_by"], orders)
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
