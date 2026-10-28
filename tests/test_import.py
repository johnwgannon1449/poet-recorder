import pytest

import db
import import_orders


def _check(conn):
    assert sorted(r["slug"] for r in db.reps(conn)) == ["dave", "mike"]
    assert db.rep(conn, "mike")["name"] == "Mike"
    assert [r["order_no"] for r in conn.execute("SELECT order_no FROM orders ORDER BY seq")] == ["SO1001", "SO1002", "SO1003"]
    assert conn.execute("SELECT rep FROM orders WHERE order_no='SO1003'").fetchone()[0] is None
    lines = db.order_lines(conn, "SO1001")
    assert [(l["line_no"], l["qty"], l["uom"], l["sku"]) for l in lines] == [(1, 10.0, "EA", "PVC-90-34"), (2, 2.0, "EA", "PVC-T-34")]
    assert "customer" not in " ".join(c[1] for c in conn.execute("PRAGMA table_info(order_lines)")).lower()


def test_import_csv(data_dir, sample_csv):
    conn, n = import_orders.import_file(sample_csv)
    assert n == 5
    _check(conn)
    out = import_orders.summary(conn)
    assert "Mike" in out and "(pool)" in out


def test_import_xlsx(data_dir, sample_xlsx):
    conn, n = import_orders.import_file(sample_xlsx)
    assert n == 5
    _check(conn)


def test_reimport_keeps_takes_and_updates(data_dir, sample_csv):
    conn, _ = import_orders.import_file(sample_csv)
    conn.execute("INSERT INTO takes (rep, order_no, line_no, take_no, duration_s, status) VALUES ('mike','SO1001',1,1,2.0,'saved')")
    db.set_line_skipped(conn, "mike", "SO1001", 2, True)
    conn.commit()
    sample_csv.write_text(sample_csv.read_text().replace("3/4 PVC TEE SCH40,Mike", "3/4 PVC TEE SCH 40,Dave"))
    conn, _ = import_orders.import_file(sample_csv, extra_reps=["Luis", " "])
    assert conn.execute("SELECT description FROM order_lines WHERE order_no='SO1001' AND line_no=2").fetchone()[0] == "3/4 PVC TEE SCH 40"
    assert conn.execute("SELECT rep FROM orders WHERE order_no='SO1001'").fetchone()[0] == "dave"
    assert conn.execute("SELECT COUNT(*) FROM takes").fetchone()[0] == 1
    assert db.skipped_lines(conn, "mike", "SO1001") == {2}
    assert db.rep(conn, "luis")["name"] == "Luis"
    assert "Luis" in import_orders.summary(conn)


def test_missing_header_fails(data_dir, tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("order_no,line_no,qty,sku\nA,1,1,X\n")
    with pytest.raises(SystemExit) as e:
        import_orders.import_file(p)
    assert "description" in str(e.value)


def test_slugify():
    assert db.slugify("  Mike O'Brien ") == "mike-o-brien"
    assert db.safe_name("SO/10 01") == "SO_10_01"
