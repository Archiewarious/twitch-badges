#!/usr/bin/env python3
"""Снимает фикстуры для тестов с копии data/ (только чтение исходника).

    ./venv/bin/python tools/make_fixtures.py <data_dir> <tag>

Пишет в tests/fixtures/:
  snapshot_<tag>.json.gz   — снапшот SD без логинов модераторов (availability[].user);
  published_<tag>.json     — состояние публикаций;
  known_windows_<tag>.json — память окон (её читает build_records);
  media_<tag>.json         — какие картинки и карточки лежали на диске (только имена:
                             старая логика смотрит лишь на наличие файла).
"""
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures"


def strip_users(obj):
    """availability[].user — логин и аватар модератора SD: коду не нужен."""
    if isinstance(obj, dict):
        return {k: strip_users(v) for k, v in obj.items() if k != "user"}
    if isinstance(obj, list):
        return [strip_users(v) for v in obj]
    return obj


def main():
    data, tag = Path(sys.argv[1]), sys.argv[2]
    OUT.mkdir(parents=True, exist_ok=True)
    snap = strip_users(json.loads((data / "streamdb_latest.json").read_text()))
    raw = json.dumps(snap, ensure_ascii=False, sort_keys=True).encode()
    # mtime=0 — одинаковые данные дают одинаковый файл
    (OUT / f"snapshot_{tag}.json.gz").write_bytes(gzip.compress(raw, 9, mtime=0))
    for name in ("published", "known_windows"):
        src = json.loads((data / f"{name}.json").read_text())
        (OUT / f"{name}_{tag}.json").write_text(
            json.dumps(src, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    media = {d: sorted(p.stem for p in (data / d).glob("*.png")) for d in ("images", "cards")}
    (OUT / f"media_{tag}.json").write_text(json.dumps(media, indent=1) + "\n")


if __name__ == "__main__":
    main()
