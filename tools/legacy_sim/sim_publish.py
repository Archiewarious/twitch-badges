"""Холостой прогон publish_new на копии data/: без сети, без alert.sh.

    python tools/legacy_sim/sim_publish.py <outdir> [ticks] [--data DIR]

Исходный каталог данных (по умолчанию data/ этой копии) только читается."""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, LegacySim, load_data_dir  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("outdir")
ap.add_argument("ticks", nargs="?", type=int, default=10)
ap.add_argument("--data", default=str(REPO / "data"))
a = ap.parse_args()

sim = LegacySim(Path(a.outdir), now=datetime.now(timezone.utc), **load_data_dir(Path(a.data)))
for tick in range(a.ticks):
    new = sim.tick()
    if not new:
        print(f"tick {tick}: нечего постить")
        break
    for p in new:
        print(f"tick {tick}: {p.kind} x{len(p.media)} | {p.head[:150]}")
        if len(p.caption) > 1024:
            print(f"   !!! подпись {len(p.caption)} > 1024")
for al in sim.alerts:
    if not al.cleared:
        print(f"  [ALERT {al.key}] {al.subject}")
