"""POET Counter Recorder server.  uvicorn app:app --host 0.0.0.0 --port 8000"""
import csv
import io
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

import db

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="POET Counter Recorder")

PROGRESS_COLS = ["assigned_orders", "orders_done", "orders_skipped", "remaining_orders", "assigned_lines",
                 "lines_recorded", "lines_skipped", "remaining_lines", "takes", "minutes", "last_activity"]


@contextmanager
def conn():
    c = db.connect()
    try:
        with c:
            yield c
    finally:
        c.close()


def need_rep(c, slug):
    r = db.rep(c, slug)
    if not r:
        raise HTTPException(404, f"no rep '{slug}'")
    return r


def run_ffmpeg(src, wav):
    """Original → 16 kHz mono 16-bit PCM WAV. Replaced in tests."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                   check=True, timeout=120)


def transcode(take_id, src, wav):
    try:
        run_ffmpeg(src, wav)
    except Exception as e:  # keep the original; the export reports missing WAVs
        with conn() as c:
            db.log(c, None, "ffmpeg_error", take_id=take_id, detail=str(e)[:500])
        return
    with conn() as c:
        c.execute("UPDATE takes SET wav_path=? WHERE id=?", (str(wav.relative_to(db.DATA_DIR)), take_id))


# ---------- pages ----------

@app.get("/")
@app.get("/r/{slug}")
def rep_page(slug: str = ""):
    return FileResponse(STATIC / "rep.html")


@app.get("/progress")
def progress_page():
    return FileResponse(STATIC / "progress.html")


# ---------- rep API ----------

@app.get("/api/reps")
def api_reps():
    with conn() as c:
        return db.reps(c)


def current_payload(c, slug):
    rep = need_rep(c, slug)
    order_no = db.current_order(c, slug)
    out = {"rep": rep, "counts": db.rep_counts(c, slug), "order": None, "has_prev": db.has_prev(c, slug),
           "has_next": db.first_pending(c, slug, exclude={order_no})[0] is not None}
    if order_no:
        takes, skipped = db.takes_for_order(c, slug, order_no), db.skipped_lines(c, slug, order_no)
        lines = db.order_lines(c, order_no)
        for l in lines:
            l["takes"] = takes.get(l["line_no"], [])
            l["skipped"] = l["line_no"] in skipped
        out["order"] = {"order_no": order_no, "section": db.section_of(c, slug, order_no), "lines": lines,
                        "assigned_to": c.execute("SELECT rep FROM orders WHERE order_no=?", (order_no,)).fetchone()[0],
                        **db.order_state(c, slug, order_no)}
    return out


@app.get("/api/r/{slug}/current")
def api_current(slug: str):
    with conn() as c:
        return current_payload(c, slug)


@app.post("/api/r/{slug}/next")
def api_next(slug: str):
    with conn() as c:
        need_rep(c, slug)
        db.next_order(c, slug)
        return current_payload(c, slug)


@app.post("/api/r/{slug}/prev")
def api_prev(slug: str):
    with conn() as c:
        need_rep(c, slug)
        db.prev_order(c, slug)
        return current_payload(c, slug)


@app.post("/api/take")
def api_take(request: Request, bg: BackgroundTasks, audio: UploadFile = File(...), rep: str = Form(...),
             order_no: str = Form(...), line_no: int = Form(...), duration_s: float = Form(0.0)):
    ext = "m4a" if "mp4" in (audio.content_type or "") or (audio.filename or "").endswith(".m4a") else "webm"
    data = audio.file.read()
    if len(data) < 100:
        raise HTTPException(400, "empty recording")
    with conn() as c:
        need_rep(c, rep)
        if not c.execute("SELECT 1 FROM order_lines WHERE order_no=? AND line_no=?", (order_no, line_no)).fetchone():
            raise HTTPException(404, "no such order line")
        take_no = db.next_take_no(c, rep, order_no, line_no)
        folder = db.AUDIO_DIR / db.safe_name(order_no)
        folder.mkdir(parents=True, exist_ok=True)
        src = folder / f"{line_no}_{rep}_{take_no}.{ext}"
        src.write_bytes(data)
        cur = c.execute("INSERT INTO takes (rep, order_no, line_no, take_no, recorded_at, duration_s, src_path, status, user_agent) "
                        "VALUES (?,?,?,?,?,?,?,'saved',?)",
                        (rep, order_no, line_no, take_no, db.now(), duration_s, str(src.relative_to(db.DATA_DIR)),
                         request.headers.get("user-agent", "")[:300]))
        take_id = cur.lastrowid
        db.log(c, rep, "take", order_no, line_no, take_id, f"{duration_s:.1f}s")
        if line_no in db.skipped_lines(c, rep, order_no):
            db.set_line_skipped(c, rep, order_no, line_no, False)   # recording un-skips
        db.refresh_order_status(c, rep, order_no)
    bg.add_task(transcode, take_id, src, src.with_suffix(".wav"))
    return {"id": take_id, "take_no": take_no, "duration_s": duration_s}


@app.delete("/api/take/{take_id}")
def api_delete_take(take_id: int):
    with conn() as c:
        t = c.execute("SELECT * FROM takes WHERE id=? AND status='saved'", (take_id,)).fetchone()
        if not t:
            raise HTTPException(404, "no such take")
        trash = db.AUDIO_DIR / "_trash" / db.safe_name(t["order_no"])
        trash.mkdir(parents=True, exist_ok=True)
        for p in (t["src_path"], t["wav_path"]):
            if p and (db.DATA_DIR / p).exists():
                shutil.move(db.DATA_DIR / p, trash / Path(p).name)
        c.execute("UPDATE takes SET status='deleted' WHERE id=?", (take_id,))
        db.log(c, t["rep"], "take_delete", t["order_no"], t["line_no"], take_id)
        db.refresh_order_status(c, t["rep"], t["order_no"])
        return {"ok": True}


async def body(request):
    try:
        return await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required: {rep, order_no[, line_no]}")


@app.post("/api/skip-line")
@app.post("/api/unskip-line")
async def api_skip_line(request: Request):
    b = await body(request)
    with conn() as c:
        need_rep(c, b["rep"])
        db.set_line_skipped(c, b["rep"], b["order_no"], int(b["line_no"]), request.url.path.endswith("/skip-line"))
        return current_payload(c, b["rep"])


@app.post("/api/skip-order")
async def api_skip_order(request: Request):
    b = await body(request)
    with conn() as c:
        need_rep(c, b["rep"])
        db.skip_order(c, b["rep"], b["order_no"])
        db.next_order(c, b["rep"])
        return current_payload(c, b["rep"])


@app.post("/api/complete-order")
async def api_complete_order(request: Request):
    b = await body(request)
    with conn() as c:
        need_rep(c, b["rep"])
        db.refresh_order_status(c, b["rep"], b["order_no"])
        if not db.is_order_complete(c, b["rep"], b["order_no"]):
            raise HTTPException(409, "order still has lines without a take or skip")
        return current_payload(c, b["rep"])


# ---------- progress ----------

def progress_rows(c):
    rows = [{"rep": r["name"], "slug": r["slug"], **db.rep_counts(c, r["slug"])} for r in db.reps(c)]
    total = {"rep": "TOTAL", "slug": ""}
    for k in PROGRESS_COLS:
        vals = [r[k] for r in rows if r[k] is not None]
        total[k] = max(vals, default=None) if k == "last_activity" else round(sum(vals), 1)
    return rows, total


@app.get("/api/progress")
def api_progress():
    with conn() as c:
        rows, total = progress_rows(c)
        return {"reps": rows, "total": total, "coverage": db.coverage(c), "columns": PROGRESS_COLS}


@app.get("/progress.csv")
def progress_csv():
    with conn() as c:
        rows, total = progress_rows(c)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["rep"] + PROGRESS_COLS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows + [total])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")
