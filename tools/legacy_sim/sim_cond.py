"""Сценарий A5: условие стало известно одновременно со стартом.

    python tools/legacy_sim/sim_cond.py <outdir> <set_id> [--data DIR]"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, LegacySim, load_data_dir  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("outdir")
ap.add_argument("set_id")
ap.add_argument("--data", default=str(REPO / "data"))
a = ap.parse_args()

args = load_data_dir(Path(a.data))
args["published"][a.set_id].update(started=False, cond_vague=True, cond_known=False, ending=False)
sim = LegacySim(Path(a.outdir), now=datetime.now(timezone.utc), **args)
for _ in range(3):
    for p in sim.tick():
        print(("POST:" if p.kind == "photo" else "ALBUM:"), " / ".join(p.lines)[:230])
