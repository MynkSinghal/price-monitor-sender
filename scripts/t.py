from pathlib import Path
p = Path(__file__).resolve().parent.parent / "t_probe.txt"
p.write_text("START\n")
import sys
p.write_text("step1: after import sys\n")
import json
p.write_text("step2: after import json\n")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
p.write_text("step3: path inserted\n")
from src.config_loader import PriceGroupDef
p.write_text("step4: imported PriceGroupDef\n")
p.write_text("END OK\n")
