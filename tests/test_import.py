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


MIS_CSV = '''"Date","Invoice Number","Line#","Code (Consolidated Customer)","Name (Consolidated Customer)","Ship Branch","Type (Price Code)","Size (Price Code)","Name (Customer Region)","Code (Product)","Name (Product)","Selling Unit (Product)","Name (Division)","Order Date","Ext Selling Price"
"10/1/2026",0100000001,1,"00000-00","FAKE CUSTOMER LLC",1,"Plumbing","Medium","NORTH BAY","FAPPT","3/4 X 10' 26GA PLUMBERS TAPE","RL","PLUMBING","9/16/2026",2.2
"10/1/2026",0100000001,4,"00000-00","FAKE CUSTOMER LLC",1,"Plumbing","Medium","NORTH BAY","UPNPT12","Q4755050 EP 1/2 PEX TEE","EA","PLUMBING","9/16/2026",44.5
'''


def test_import_mis_export_aliases(data_dir, tmp_path):
    p = tmp_path / "Export.csv"
    p.write_text(MIS_CSV, encoding="utf-8-sig")
    conn, n = import_orders.import_file(p)
    assert n == 2
    o = conn.execute("SELECT * FROM orders WHERE order_no='0100000001'").fetchone()
    assert (o["branch"], o["division"], o["cust_type"], o["cust_size"], o["region"], o["order_date"]) == ("1", "PLUMBING", "Plumbing", "Medium", "NORTH BAY", "9/16/2026")
    lines = db.order_lines(conn, "0100000001")
    assert [(l["line_no"], l["qty"], l["uom"], l["sku"]) for l in lines] == [(1, None, "RL", "FAPPT"), (4, None, "EA", "UPNPT12")]
    assert "FAKE CUSTOMER" not in open(db.DB_PATH, "rb").read().decode("latin1")
    assert "00000-00" not in open(db.DB_PATH, "rb").read().decode("latin1")


def test_assign_reps(data_dir, tmp_path):
    import assign_reps
    rows = ['order_no,line_no,sku,description,division']
    for o in range(30):
        for l in range(1, 4):
            rows.append(f"O{o},{l},SKU{l},DESC {l},{'FIRE PROTECTION' if o % 3 == 0 else 'PLUMBING'}")
    src = tmp_path / "exp.csv"
    src.write_text("\n".join(rows) + "\n")
    orders = assign_reps.load_orders(src)
    chosen = assign_reps.pick(orders, ["Eric", "John"], 5, {"Eric": ["fire"]}, 2, 15, seed=1)
    assert sum(1 for r in chosen.values() if r == "Eric") == 5 and sum(1 for r in chosen.values() if r == "John") == 5
    assert all(orders[o][0]["division"] == "FIRE PROTECTION" for o, r in chosen.items() if r == "Eric")
    out = tmp_path / "batch.xlsx"
    assign_reps.write_xlsx(out, orders, chosen)
    conn, n = import_orders.import_file(out)
    assert n == 30 and sorted(r["slug"] for r in db.reps(conn)) == ["eric", "john"]
    # a second batch never re-picks orders already in the database
    chosen2 = assign_reps.pick(orders, ["John"], 5, {}, 2, 15, seed=2)
    assert not set(chosen2) & set(chosen)


def test_nonpositive_qty_left_out_and_removed(data_dir, tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("order_no,line_no,sku,description,qty\nA,1,X,ONE,2\nA,2,Y,TWO,0\nB,1,Z,RET,-3\n")
    conn, n = import_orders.import_file(p)
    assert n == 1 and [r[0] for r in conn.execute("SELECT order_no FROM orders")] == ["A"]
    assert [l["line_no"] for l in db.order_lines(conn, "A")] == [1]
    # a line imported earlier (no qty known) disappears once the export says it was a return
    p.write_text("order_no,line_no,sku,description\nA,1,X,ONE\nA,2,Y,TWO\n")
    conn, _ = import_orders.import_file(p)
    assert [l["line_no"] for l in db.order_lines(conn, "A")] == [1, 2]
    p.write_text("order_no,line_no,sku,description,qty\nA,1,X,ONE,2\nA,2,Y,TWO,-1\n")
    conn, _ = import_orders.import_file(p)
    assert [l["line_no"] for l in db.order_lines(conn, "A")] == [1]


def test_assign_with_qty_file_and_same_as(data_dir, tmp_path):
    import assign_reps
    (tmp_path / "exp.csv").write_text("Invoice Number,Line#,Code (Product),Name (Product),Name (Division)\n"
                                      "O1,1,S1,D1,PLUMBING\nO1,2,S2,D2,PLUMBING\nO2,1,S3,D3,PLUMBING\nO2,2,S4,D4,PLUMBING\n")
    (tmp_path / "qty.csv").write_text("Invoice Number,Line#,Code (Product),Quantity Ordered\nO1,1,S1,5\nO1,2,S2,-5\nO2,1,S3,1\nO2,2,S4,3\n")
    orders = assign_reps.load_orders(tmp_path / "exp.csv", tmp_path / "qty.csv")
    assert [(l["line_no"], l["qty"]) for l in orders["O1"]] == [("1", "5")] and len(orders["O2"]) == 2
    assign_reps.write_xlsx(tmp_path / "b1.xlsx", orders, {"O2": "Eric"})
    chosen = assign_reps.same_as(tmp_path / "b1.xlsx")
    assert chosen == {"O2": "Eric"}
    conn, n = import_orders.import_file(tmp_path / "b1.xlsx")
    assert n == 2 and db.order_lines(conn, "O2")[1]["qty"] == 3.0
