"""Проверки test_pipeline.py (их гоняет прод раз в час) — на фикстуре, а не на
живом data/: так они воспроизводимы и не зависят от того, что сейчас в SD."""
import pytest
from conftest import T0, load_fixture

import harness
import test_pipeline as tp


@pytest.fixture
def pipeline(tmp_path):
    harness._Clock.value = T0
    tp.set_now(T0)
    # build_records читает known_windows и overrides из файлов: подкладываем пустые
    harness.site.KNOWN_WINDOWS_FILE = tmp_path / "known_windows.json"
    harness.site.OVERRIDES_FILE = tmp_path / "overrides.json"
    tp.results.clear()
    yield tp
    tp.results.clear()


def _failed():
    return [(n, d) for n, ok, d in tp.results if not ok]


def test_format_drift_detected(pipeline):
    pipeline.test_format_drift(load_fixture()["snapshot"])
    assert len(tp.results) == 13
    assert _failed() == []


def test_new_campaign_picked_up(pipeline):
    pipeline.test_new_campaign(load_fixture()["snapshot"])
    assert len(tp.results) == 4
    assert _failed() == []
