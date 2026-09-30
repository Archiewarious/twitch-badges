"""C05: старая логика (legacy_sim) против новой (planner) на одних данных.

Расхождения допускаются только из списка намеренных исправлений ALLOWED.
Сценарии с изменением данных по ходу времени (продление, орфан, условие
со стартом) — в test_planner.py: там новая логика по определению другая.
"""
import json
from datetime import timedelta

import mutations as m
import pytest
from conftest import T0, load_fixture
from newsim import NewSim

from harness import LegacySim

# (мутация, смещение от T0, что было у старой логики, что стало у новой)
ALLOWED = {
    # Q6/B6: анонс значка без дат больше не обещает «скоро»
    ("nodate", timedelta(0), ("appeared_upcoming", ("test-nodate",)),
     ("appeared_nodates", ("test-nodate",))),
}


def norm_legacy(posts):
    agg = {}
    for p in posts:
        agg.setdefault((p.at, p.post_kind), []).extend(p.keys)
    return {(at, k, tuple(sorted(v))) for (at, k), v in agg.items()}


def norm_new(posts):
    return {(p.at, p.kind, tuple(sorted(p.keys))) for p in posts}


MUTATIONS = {
    "none": lambda s: [],
    "new": lambda s: [m.new_badge(s, "test-new", "Test New", start=T0 + m.days(2),
                                  end=T0 + m.days(9), added_at=T0 - m.hours(1))],
    "tiers": lambda s: m.new_campaign(
        s, "Test Tiers", [(f"test-tier-{i}", f"Test Tier {i}") for i in (1, 2, 3)],
        start=T0 - m.hours(1), end=T0 + m.days(5), added_at=T0 - m.hours(2)),
    "nodate": lambda s: [m.no_date_badge(
        s, "test-nodate", "Test No Date", added_at=T0 - m.hours(5),
        description="This badge was earned by watching Arc Raiders for 30 minutes")],
    "bits": lambda s: [m.bits_page_badge(s, "test-bits", "Test Bits", start=T0 + m.days(2),
                                         end=T0 + m.days(9), added_at=T0 - m.hours(4))],
    "short": lambda s: [m.new_badge(s, "test-short", "Test Short", start=T0 - m.hours(2),
                                    end=T0 + m.hours(10), added_at=T0 - m.hours(3))],
    "nocond": lambda s: (m.new_badge(s, "test-nc", "Test NC", start=T0 + m.days(1),
                                     end=T0 + m.days(9), added_at=T0 - m.hours(1)),
                         m.clear_condition(s, "test-nc"), [m.image_key("test-nc")])[2],
}


def _pair(name, span, step):
    fx = load_fixture()
    fx["media"]["images"] += MUTATIONS[name](fx["snapshot"])
    return fx, span, step


@pytest.mark.slow
@pytest.mark.parametrize("name, span, step", [
    ("none", timedelta(days=40), timedelta(hours=1)),
    *[(k, timedelta(days=12), timedelta(hours=1)) for k in MUTATIONS if k != "none"],
])
def test_differential(tmp_path, name, span, step):
    fx, span, step = _pair(name, span, step)
    legacy = LegacySim(tmp_path / "legacy", now=T0, **json.loads(json.dumps(fx)))
    old = norm_legacy(legacy.run(T0, step, span))
    new = norm_new(NewSim(now=T0, **json.loads(json.dumps(fx))).run(T0, step, span))
    diff = set()
    for at, kind, keys in old - new:
        match = next(((ok, nk) for (mn, off, ok, nk) in ALLOWED
                      if mn == name and at - T0 == off and ok == (kind, keys)), None)
        if match and (at, *match[1]) in new:
            continue
        diff.add(("старая", at - T0, kind, keys))
    for at, kind, keys in new - old:
        if any(mn == name and at - T0 == off and nk == (kind, keys) for (mn, off, ok, nk) in ALLOWED):
            continue
        diff.add(("новая", at - T0, kind, keys))
    assert diff == set()
    if name == "none":
        assert len(new) == 16          # 17 постов старой логики: один альбом из двух частей
