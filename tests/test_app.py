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
    return c


def take(client, rep, order_no, line_no, dur=1.5, mime="audio/webm"):
    return client.post("/api/take", data={"rep": rep, "order_no": order_no, "line_no": line_no, "duration_s": dur},
                       files={"audio": ("take.webm", b"\x1a\x45\xdf\xa3" + b"x" * 200, mime)})


def test_pages_and_reps(client):
    assert client.get("/").status_code == 200
    assert client.get("/r/mike").status_code == 200
    assert client.get("/progress").status_code == 200
    assert [r["slug"] for r in client.get("/api/reps").json()] == ["dave", "mike"]
    assert client.get("/api/r/nobody/current").status_code == 404


def test_current_and_queue(client):
    cur = client.get("/api/r/mike/current").json()
    assert cur["order"]["order_no"] == "SO1001" and cur["order"]["section"] == "own"
    assert cur["counts"]["assigned_orders"] == 1 and cur["counts"]["assigned_lines"] == 2
    assert [l["line_no"] for l in cur["order"]["lines"]] == [1, 2]
    assert cur["has_prev"] is False and cur["has_next"] is True
    # Mike's queue: own SO1001 → pool SO1003 → Dave's SO1002
    with db.connect() as c:
        assert db.queue(c, "mike") == [("SO1001", "own"), ("SO1003", "pool"), ("SO1002", "others")]


def test_take_flow(client):
    r = take(client, "mike", "SO1001", 1)
    assert r.status_code == 200 and r.json()["take_no"] == 1
    src = db.AUDIO_DIR / "SO1001" / "1_mike_1.webm"
    assert src.exists() and src.with_suffix(".wav").exists()
    assert client.ffmpeg_calls and client.ffmpeg_calls[0][0] == src
    cur = client.get("/api/r/mike/current").json()
    line1 = cur["order"]["lines"][0]
    assert len(line1["takes"]) == 1 and line1["takes"][0]["duration_s"] == 1.5
    assert cur["order"]["completed"] is False
    assert cur["counts"]["lines_recorded"] == 1 and cur["counts"]["takes"] == 1
    # second take on the same line = "another way"
    assert take(client, "mike", "SO1001", 1).json()["take_no"] == 2
    assert take(client, "mike", "SO1001", 2, mime="audio/mp4").status_code == 200
    assert (db.AUDIO_DIR / "SO1001" / "2_mike_1.m4a").exists()
    cur = client.get("/api/r/mike/current").json()
    assert cur["order"]["completed"] is True
    assert cur["counts"]["orders_done"] == 1 and cur["counts"]["remaining_orders"] == 0
    with db.connect() as c:
        assert c.execute("SELECT wav_path FROM takes WHERE id=1").fetchone()[0] == "audio/SO1001/1_mike_1.wav"


def test_take_validation(client):
    assert take(client, "nobody", "SO1001", 1).status_code == 404
    assert take(client, "mike", "SO9999", 1).status_code == 404
    r = client.post("/api/take", data={"rep": "mike", "order_no": "SO1001", "line_no": 1}, files={"audio": ("t.webm", b"x", "audio/webm")})
    assert r.status_code == 400


def test_delete_take_moves_to_trash_and_reopens(client):
    client.get("/api/r/mike/current")
    tid = take(client, "mike", "SO1001", 1).json()["id"]
    take(client, "mike", "SO1001", 2)
    assert client.get("/api/r/mike/current").json()["order"]["completed"] is True
    assert client.delete(f"/api/take/{tid}").status_code == 200
    assert client.delete(f"/api/take/{tid}").status_code == 404
    assert (db.AUDIO_DIR / "_trash" / "SO1001" / "1_mike_1.webm").exists()
    assert (db.AUDIO_DIR / "_trash" / "SO1001" / "1_mike_1.wav").exists()
    cur = client.get("/api/r/mike/current").json()
    assert cur["order"]["completed"] is False and cur["order"]["lines"][0]["takes"] == []
    # the take_no keeps counting so filenames never collide
    assert take(client, "mike", "SO1001", 1).json()["take_no"] == 2


def test_skip_line_and_unskip_by_recording(client):
    r = client.post("/api/skip-line", json={"rep": "mike", "order_no": "SO1001", "line_no": 1}).json()
    assert r["order"]["lines"][0]["skipped"] is True and r["counts"]["lines_skipped"] == 1
    r = client.post("/api/unskip-line", json={"rep": "mike", "order_no": "SO1001", "line_no": 1}).json()
    assert r["order"]["lines"][0]["skipped"] is False
    client.post("/api/skip-line", json={"rep": "mike", "order_no": "SO1001", "line_no": 1})
    take(client, "mike", "SO1001", 1)
    cur = client.get("/api/r/mike/current").json()
    assert cur["order"]["lines"][0]["skipped"] is False and cur["counts"]["lines_skipped"] == 0
    client.post("/api/skip-line", json={"rep": "mike", "order_no": "SO1001", "line_no": 2})
    assert client.get("/api/r/mike/current").json()["order"]["completed"] is True


def test_skip_order_next_prev(client):
    client.get("/api/r/mike/current")
    r = client.post("/api/skip-order", json={"rep": "mike", "order_no": "SO1001"}).json()
    assert r["order"]["order_no"] == "SO1003" and r["order"]["section"] == "pool"
    assert r["counts"]["orders_skipped"] == 1 and r["has_prev"] is True
    r = client.post("/api/r/mike/prev").json()
    assert r["order"]["order_no"] == "SO1001" and r["order"]["skipped"] is True and r["has_prev"] is False
    r = client.post("/api/r/mike/next").json()
    assert r["order"]["order_no"] == "SO1003"
    r = client.post("/api/r/mike/next").json()
    assert r["order"]["order_no"] == "SO1002" and r["order"]["section"] == "others" and r["order"]["assigned_to"] == "dave"
    # everything done or skipped → no order left
    client.post("/api/skip-order", json={"rep": "mike", "order_no": "SO1002"})
    client.post("/api/skip-order", json={"rep": "mike", "order_no": "SO1003"})
    cur = client.get("/api/r/mike/current").json()
    assert cur["order"]["order_no"] == "SO1003" and cur["order"]["skipped"] is True and cur["has_next"] is False


def test_complete_order_endpoint(client):
    r = client.post("/api/complete-order", json={"rep": "mike", "order_no": "SO1001"})
    assert r.status_code == 409
    take(client, "mike", "SO1001", 1)
    client.post("/api/skip-line", json={"rep": "mike", "order_no": "SO1001", "line_no": 2})
    assert client.post("/api/complete-order", json={"rep": "mike", "order_no": "SO1001"}).status_code == 200


def test_two_reps_same_order_both_kept(client):
    take(client, "mike", "SO1003", 1)
    take(client, "dave", "SO1003", 1)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM takes WHERE order_no='SO1003'").fetchone()[0] == 2
    assert (db.AUDIO_DIR / "SO1003" / "1_dave_1.webm").exists()


def test_progress(client):
    take(client, "mike", "SO1001", 1, dur=30)
    take(client, "mike", "SO1001", 2, dur=30)
    p = client.get("/api/progress").json()
    mike = next(r for r in p["reps"] if r["slug"] == "mike")
    assert mike["orders_done"] == 1 and mike["minutes"] == 1.0 and mike["last_activity"]
    assert p["total"]["assigned_orders"] == 2 and p["total"]["assigned_lines"] == 3 and p["total"]["takes"] == 2
    assert p["coverage"] == {"orders_total": 3, "orders_done": 1, "orders_pct": 33.3, "lines_total": 5, "lines_done": 2, "lines_pct": 40.0}
    csv_text = client.get("/progress.csv").text
    assert csv_text.startswith("rep,assigned_orders") and "TOTAL" in csv_text


def test_ffmpeg_failure_logged(client, monkeypatch):
    def boom(src, wav):
        raise RuntimeError("no ffmpeg")
    monkeypatch.setattr(appmod, "run_ffmpeg", boom)
    take(client, "mike", "SO1001", 1)
    with db.connect() as c:
        assert c.execute("SELECT wav_path FROM takes WHERE id=1").fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='ffmpeg_error'").fetchone()[0] == 1
