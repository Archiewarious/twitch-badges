"""Прокрутка времени на неизменных данных: какие посты уйдут за N дней.

    python tools/legacy_sim/sim_time.py <outdir> <шаг_часов> <дней> [--data DIR]"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, LegacySim, load_data_dir  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("outdir")
ap.add_argument("step_hours", type=float)
ap.add_argument("days", type=int)
ap.add_argument("--data", default=str(REPO / "data"))
a = ap.parse_args()

t0 = datetime.now(timezone.utc)
sim = LegacySim(Path(a.outdir), now=t0, **load_data_dir(Path(a.data)))
for p in sim.run(t0, timedelta(hours=a.step_hours), timedelta(days=a.days)):
    off = p.at - t0
    print(f"+{off.days:>2}d{off.seconds // 3600:02d}h | {p.head[:120]}")
for al in sim.alerts:
    if not al.cleared:
        print(f"  [ALERT {al.key}] {al.subject}")
print("state after:", len(sim.state or {}))
