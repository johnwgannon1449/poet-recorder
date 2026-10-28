import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402

SAMPLE_CSV = """Order_No,Line_No,Qty,UOM,SKU,Description,Rep,Customer Name
SO1001,1,10,EA,PVC-90-34,3/4 PVC 90 ELBOW SCH40,Mike,Acme Plumbing
SO1001,2,2,EA,PVC-T-34,3/4 PVC TEE SCH40,Mike,Acme Plumbing
SO1002,1,100,FT,CU-TYPE-L-12,1/2 TYPE L COPPER TUBE,Dave,Bob's Pipes
SO1003,1,5,EA,BV-34-FP,3/4 BALL VALVE FULL PORT,,Walk-in
SO1003,2,1,EA,PRV-34,3/4 PRESSURE REDUCING VALVE,,Walk-in
"""


@pytest.fixture
def data_dir(tmp_path):
    db.configure(tmp_path / "data")
    return tmp_path / "data"


@pytest.fixture
def sample_csv(tmp_path):
    p = tmp_path / "orders.csv"
    p.write_text(SAMPLE_CSV)
    return p


@pytest.fixture
def sample_xlsx(tmp_path):
    import csv, io, openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in csv.reader(io.StringIO(SAMPLE_CSV)):
        ws.append(row)
    p = tmp_path / "orders.xlsx"
    wb.save(p)
    return p
