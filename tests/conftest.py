import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
for p in (REPO, REPO / "tools" / "legacy_sim", REPO / "tools" / "legacy_sim" / "legacy",
          REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

TAG = "20260930"
# Снапшот снят в 17:04:56Z, состояние — в 17:18Z. Все тесты на фикстуре идут от
# этого момента, а не от настоящего «сейчас»: иначе результат гулял бы с часами.
T0 = datetime(2026, 9, 30, 17, 20, tzinfo=timezone.utc)

_raw = {}


def _read(name):
    if name not in _raw:
        p = FIXTURES / name
        _raw[name] = gzip.decompress(p.read_bytes()) if name.endswith(".gz") else p.read_bytes()
    return json.loads(_raw[name])


def load_fixture(tag=TAG):
    """Свежая (глубокая) копия всех данных фикстуры — мутировать можно."""
    return {
        "snapshot": _read(f"snapshot_{tag}.json.gz"),
        "published": _read(f"published_{tag}.json"),
        "known_windows": _read(f"known_windows_{tag}.json"),
        "media": _read(f"media_{tag}.json"),
    }


@pytest.fixture
def fx():
    return load_fixture()


@pytest.fixture
def legacy(tmp_path):
    """Фабрика LegacySim на каталоге теста: legacy(**load_fixture(), now=...)."""
    from harness import LegacySim

    n = [0]

    def make(now=T0, **kw):
        n[0] += 1
        return LegacySim(tmp_path / f"sim{n[0]}", now=now, **kw)

    return make
