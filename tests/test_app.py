import pytest
from fastapi.testclient import TestClient

import app as appmod
import db
import import_orders


@pytest.fixture
def client(data_dir, sample_csv, monkeypatch):
    import_orders.import_file(sample_csv)
    calls = []

    def fake_ffmpeg(src, wav):
        calls.append((src, wav))
        wav.write_bytes(b"RIFF fake wav")
    monkeypatch.setattr(appmod, "run_ffmpeg", fake_ffmpeg)
    c = TestClient(appmod.app)
    c.ffmpeg_calls = calls
    c.get("/api/r/mike/current")   # what the page does on load
    return c


def take(client, rep, order_no, dur=12.0, mime="audio/webm"):
    return client.post("/api/take", data={"rep": rep, "order_no": order_no, "duration_s": dur},
                       files={"audio": ("take.webm", b"\x1a\x45\xdf\xa3" + b"x" * 200, mime)})


def skip(client, rep, order_no, line_no, on=True):
    return client.post("/api/skip-line" if on else "/api/unskip-line", json={"rep": rep, "order_no": order_no, "line_no": line_no})


def test_pages_and_reps(client):
    assert client.get("/").status_code == 200
    assert client.get("/r/mike").status_code == 200
    assert client.get("/progress").status_code == 200
    assert [r["slug"] for r in client.get("/api/reps").json()] == ["dave", "mike"]
    assert client.get("/api/r/nobody/current").status_code == 404


def test_current_and_queue(client):
    cur = client.get("/api/r/mike/current").json()
    o = cur["order"]
    assert o["order_no"] == "SO1001" and o["section"] == "own" and o["takes"] == [] and o["completed"] is False
    assert cur["counts"]["assigned_orders"] == 1 and cur["counts"]["assigned_lines"] == 2
    assert [l["line_no"] for l in o["lines"]] == [1, 2] and o["lines"][0]["qty"] == 10.0
    assert cur["has_prev"] is False and cur["has_next"] is True
    with db.connect() as c:
        assert db.queue(c, "mike") == [("SO1001", "own"), ("SO1003", "pool"), ("SO1002", "others")]


def test_take_flow(client):
    skip(client, "mike", "SO1001", 2)
    r = take(client, "mike", "SO1001")
    assert r.status_code == 200 and r.json()["take_no"] == 1
    src = db.AUDIO_DIR / "SO1001" / "all_mike_1.webm"
    assert src.exists() and src.with_suffix(".wav").exists() and client.ffmpeg_calls[0][0] == src
    cur = client.get("/api/r/mike/current").json()
    o = cur["order"]
    assert o["completed"] is True and len(o["takes"]) == 1
    assert o["takes"][0]["duration_s"] == 12.0 and o["takes"][0]["skipped_lines"] == "2"
    assert cur["counts"] | {"orders_done": 1, "remaining_orders": 0, "lines_recorded": 1, "lines_skipped": 1, "takes": 1, "minutes": 0.2} == cur["counts"]
    assert cur["has_next"] is True
    # another read of the same order = take 2, in m4a from Safari
    assert take(client, "mike", "SO1001", mime="audio/mp4").json()["take_no"] == 2
    assert (db.AUDIO_DIR / "SO1001" / "all_mike_2.m4a").exists()
    with db.connect() as c:
        assert c.execute("SELECT wav_path FROM takes WHERE id=1").fetchone()[0] == "audio/SO1001/all_mike_1.wav"


def test_take_validation(client):
    assert take(client, "nobody", "SO1001").status_code == 404
    assert take(client, "mike", "SO9999").status_code == 404
    r = client.post("/api/take", data={"rep": "mike", "order_no": "SO1001"}, files={"audio": ("t.webm", b"x", "audio/webm")})
    assert r.status_code == 400


def test_delete_take_moves_to_trash_and_reopens(client):
    tid = take(client, "mike", "SO1001").json()["id"]
    assert client.get("/api/r/mike/current").json()["order"]["completed"] is True
    assert client.delete(f"/api/take/{tid}").status_code == 200
    assert client.delete(f"/api/take/{tid}").status_code == 404
    assert (db.AUDIO_DIR / "_trash" / "SO1001" / "all_mike_1.webm").exists()
    assert (db.AUDIO_DIR / "_trash" / "SO1001" / "all_mike_1.wav").exists()
    cur = client.get("/api/r/mike/current").json()
    assert cur["order"]["completed"] is False and cur["order"]["takes"] == [] and cur["counts"]["orders_done"] == 0
    assert take(client, "mike", "SO1001").json()["take_no"] == 2   # numbering never reuses a name


def test_skip_and_unskip_line(client):
    r = skip(client, "mike", "SO1001", 1).json()
    assert r["order"]["lines"][0]["skipped"] is True and r["counts"]["lines_skipped"] == 1
    r = skip(client, "mike", "SO1001", 1, on=False).json()
    assert r["order"]["lines"][0]["skipped"] is False and r["counts"]["lines_skipped"] == 0
    assert r["order"]["completed"] is False   # X-ing lines never completes an order; only a recording does


def test_skip_order_next_prev(client):
    r = client.post("/api/skip-order", json={"rep": "mike", "order_no": "SO1001"}).json()
    assert r["order"]["order_no"] == "SO1003" and r["order"]["section"] == "pool"
    assert r["counts"]["orders_skipped"] == 1 and r["has_prev"] is True
    r = client.post("/api/r/mike/prev").json()
    assert r["order"]["order_no"] == "SO1001" and r["order"]["skipped"] is True and r["has_prev"] is False
    r = client.post("/api/r/mike/next").json()
    assert r["order"]["order_no"] == "SO1003"
    r = client.post("/api/r/mike/next").json()
    assert r["order"]["order_no"] == "SO1002" and r["order"]["section"] == "others" and r["order"]["assigned_to"] == "dave"
    client.post("/api/skip-order", json={"rep": "mike", "order_no": "SO1002"})
    client.post("/api/skip-order", json={"rep": "mike", "order_no": "SO1003"})
    cur = client.get("/api/r/mike/current").json()
    assert cur["order"]["order_no"] == "SO1003" and cur["order"]["skipped"] is True and cur["has_next"] is False


def test_two_reps_same_order_both_kept(client):
    take(client, "mike", "SO1003")
    take(client, "dave", "SO1003")
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM takes WHERE order_no='SO1003'").fetchone()[0] == 2
    assert (db.AUDIO_DIR / "SO1003" / "all_dave_1.webm").exists()


def test_progress(client):
    take(client, "mike", "SO1001", dur=60)
    skip(client, "dave", "SO1003", 1)
    take(client, "dave", "SO1003", dur=30)
    p = client.get("/api/progress").json()
    mike = next(r for r in p["reps"] if r["slug"] == "mike")
    assert mike["orders_done"] == 1 and mike["minutes"] == 1.0 and mike["lines_recorded"] == 2 and mike["last_activity"]
    assert p["total"]["assigned_orders"] == 2 and p["total"]["takes"] == 2 and p["total"]["minutes"] == 1.5
    # SO1001 (2 lines, both read) + SO1003 (2 lines, dave X'd line 1) → 3 of 5 lines covered, 2 of 3 orders
    assert p["coverage"] == {"orders_total": 3, "orders_done": 2, "orders_pct": 66.7, "lines_total": 5, "lines_done": 3, "lines_pct": 60.0}
    csv_text = client.get("/progress.csv").text
    assert csv_text.startswith("rep,assigned_orders") and "TOTAL" in csv_text


def test_ffmpeg_failure_logged(client, monkeypatch):
    def boom(src, wav):
        raise RuntimeError("no ffmpeg")
    monkeypatch.setattr(appmod, "run_ffmpeg", boom)
    take(client, "mike", "SO1001")
    with db.connect() as c:
        assert c.execute("SELECT wav_path FROM takes WHERE id=1").fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='ffmpeg_error'").fetchone()[0] == 1
