import csv

import pytest

import db
import export
import import_orders


def _rows(p):
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def filled(data_dir, sample_csv):
    conn, _ = import_orders.import_file(sample_csv)
    (db.AUDIO_DIR / "SO1001").mkdir(parents=True)
    (db.AUDIO_DIR / "SO1001" / "all_mike_1.wav").write_bytes(b"RIFF")
    db.set_line_skipped(conn, "mike", "SO1001", 2, True)
    conn.execute("INSERT INTO takes (rep, order_no, line_no, take_no, recorded_at, duration_s, src_path, wav_path, status, skipped_lines) "
                 "VALUES ('mike','SO1001',0,1,'2026-01-01T00:00:00+00:00',22.5,'audio/SO1001/all_mike_1.webm','audio/SO1001/all_mike_1.wav','saved','2')")
    conn.execute("INSERT INTO takes (rep, order_no, line_no, take_no, duration_s, status) VALUES ('mike','SO1001',0,2,1.0,'deleted')")
    conn.execute("INSERT INTO takes (rep, order_no, line_no, take_no, duration_s, status, skipped_lines) VALUES ('dave','SO1001',0,1,18.0,'saved','')")
    db.refresh_order_status(conn, "mike", "SO1001")
    db.refresh_order_status(conn, "dave", "SO1001")
    db.set_line_skipped(conn, "dave", "SO1003", 1, True)
    db.skip_order(conn, "dave", "SO1003")
    conn.commit()
    return conn


def test_export_plain(filled, tmp_path):
    r = export.export(tmp_path / "out")
    assert r == {"takes": 2, "lines": 5, "orders": 3, "missing_wav": 1}
    takes = _rows(tmp_path / "out" / "takes.csv")
    assert takes[0]["rep"] == "Dave" and takes[0]["lines_read"] == "1;2" and takes[0]["lines_skipped"] == ""
    mike = takes[1]
    assert mike["order_no"] == "SO1001" and mike["order_hash"] == "" and mike["wav_path"] == "audio/SO1001/all_mike_1.wav"
    assert mike["n_lines"] == "2" and mike["lines_read"] == "1" and mike["lines_skipped"] == "2" and mike["transcript"] == ""
    lines = {(l["order_no"], l["line_no"]): l for l in _rows(tmp_path / "out" / "lines.csv")}
    assert lines[("SO1001", "1")]["n_takes"] == "2" and lines[("SO1001", "2")]["n_takes"] == "1"
    assert lines[("SO1001", "2")]["skipped_by"] == "Mike" and lines[("SO1001", "1")]["description"] == "3/4 PVC 90 ELBOW SCH40"
    assert lines[("SO1003", "1")]["assigned_rep"] == "" and lines[("SO1003", "1")]["skipped_by"] == "Dave"
    orders = {o["order_no"]: o for o in _rows(tmp_path / "out" / "orders.csv")}
    assert orders["SO1001"]["completed_by"] == "Dave;Mike" and orders["SO1001"]["n_lines"] == "2" and orders["SO1001"]["n_takes"] == "2"
    assert orders["SO1003"]["skipped_by"] == "Dave" and "division" in orders["SO1003"]


def test_export_hashed_and_audio(filled, tmp_path, monkeypatch):
    monkeypatch.setenv("ORDER_HASH_SALT", "pepper")
    export.export(tmp_path / "out", hash_orders=True, copy_audio=True)
    takes = _rows(tmp_path / "out" / "takes.csv")
    assert all(t["order_no"] == "" and len(t["order_hash"]) == 64 for t in takes)
    assert "SO1001" not in (tmp_path / "out" / "takes.csv").read_text()
    mike = next(t for t in takes if t["rep"] == "Mike")
    assert mike["wav_path"] == f"audio/{mike['take_id']}.wav" and (tmp_path / "out" / mike["wav_path"]).exists()
    assert _rows(tmp_path / "out" / "orders.csv")[0]["order_hash"] == mike["order_hash"]


def test_export_hash_needs_salt(filled, tmp_path, monkeypatch):
    monkeypatch.delenv("ORDER_HASH_SALT", raising=False)
    with pytest.raises(SystemExit):
        export.export(tmp_path / "out", hash_orders=True)
