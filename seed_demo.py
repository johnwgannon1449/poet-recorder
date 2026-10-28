"""Load 3 fake reps and 6 fake orders so the UI can be clicked through.  python seed_demo.py"""
import tempfile
from pathlib import Path

import import_orders

DEMO = """order_no,line_no,qty,uom,sku,description,rep
D1001,1,10,EA,PVC-90-34,3/4 PVC 90 ELBOW SCH40,Mike
D1001,2,2,EA,PVC-T-34,3/4 PVC TEE SCH40,Mike
D1001,3,100,FT,PVC-PIPE-34,3/4 PVC PIPE SCH40 20FT,Mike
D1002,1,4,EA,BV-34-FP,3/4 BALL VALVE FULL PORT LEAD FREE,Mike
D1002,2,1,EA,PRV-34,3/4 PRESSURE REDUCING VALVE,Mike
D1003,1,100,FT,CU-TYPE-L-12,1/2 TYPE L COPPER TUBE HARD,Dave
D1003,2,20,EA,CU-90-12,1/2 COPPER 90 ELBOW C X C,Dave
D1003,3,6,EA,CU-CPLG-12,1/2 COPPER COUPLING C X C,Dave
D1004,1,1,EA,WH-50-NG,50 GAL NATURAL GAS WATER HEATER,Luis
D1004,2,2,EA,FLEX-34-18,3/4 X 18 SS WATER HEATER CONNECTOR,Luis
D1004,3,1,EA,TP-34,3/4 T&P RELIEF VALVE,Luis
D1005,1,3,EA,CI-NH-90-4,4 NO HUB CAST IRON 90 ELBOW,
D1005,2,10,EA,NH-CPLG-4,4 NO HUB COUPLING,
D1006,1,12,EA,PEX-A-12-100,1/2 PEX A TUBING 100FT RED,
D1006,2,25,EA,PEX-ELB-12,1/2 PEX A EXPANSION 90 ELBOW,
"""

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "demo.csv"
        p.write_text(DEMO)
        conn, n = import_orders.import_file(p)
    print(f"Seeded {n} demo lines.\n")
    print(import_orders.summary(conn))
