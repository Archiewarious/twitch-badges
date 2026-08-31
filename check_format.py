#!/usr/bin/env python3
"""
Проверки формата данных StreamDatabase.

Зачем. За трое суток конца августа 2026 SD четырежды поменял структуру, и КАЖДЫЙ
раз мы узнавали об этом постфактум и по косвенным признакам:
  · availability вынесли в отдельную вкладку, заведя черновики с hidden=true —
    семь значков замолчали как «нет дат»;
  · каталог завернули в {"twitchGlobalBadge": {...}} — refresh падал полчаса;
  · категории сделали плоскими и убрали href — у всех значков пропала игра;
  · условия переехали в steps — мы месяцами читали бы обеднённые плоские поля.
Разбор молчит, когда поле просто исчезло: `.get()` вернёт None, и данные тихо
обеднеют. Эти проверки говорят вслух.

Что проверяем: структуру ответа (ключи, вложенность) и «здоровье» результата
(сколько значков вообще доходит до показа, у скольких есть условие и даты).
Порог не «всё идеально», а «не хуже, чем было» — SD живой источник, часть данных
у него отсутствует штатно.

Запуск: ./venv/bin/python check_format.py [--snapshot] — по умолчанию ходит в
сеть; с флагом проверяет последний снапшот, не трогая SD.

Коды выхода: 0 — норма, 1 — что-то изменилось (алерт через OnFailure).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import fetch_streamdb as collector  # noqa: E402
import generate_site as site  # noqa: E402

LATEST = ROOT / "data" / "streamdb_latest.json"

# Ниже этих чисел — точно поломка, а не «кончились раздачи». Взяты с большим
# запасом от реальных значений на 31.08.2026 (484 значка, 14 событий, 15 показываем).
MIN_BADGES = 300
MIN_EVENTS = 5
MIN_SHOWN = 3
MIN_WITH_CONDITION = 0.7      # доля показываемых, у которых есть условие


class Problems(list):
    def check(self, ok, message):
        if not ok:
            self.append(message)
        return ok


def check_catalog(problems, badges):
    problems.check(len(badges) >= MIN_BADGES,
                   f"каталог: {len(badges)} значков, ожидали ≥{MIN_BADGES} — "
                   "сменился формат ответа или обвалился источник")
    if not badges:
        return
    sample = badges[0]
    problems.check("current" in sample,
                   "каталог: у значка нет ключа 'current' — изменилась вложенность "
                   "(в августе 2026 SD заворачивал значки в 'twitchGlobalBadge')")
    ver = (sample.get("current") or {}).get("version") or {}
    problems.check("set_id" in (sample.get("current") or {}),
                   "каталог: у значка нет current.set_id")
    problems.check(bool(ver.get("image_url_4x")),
                   "каталог: нет version.image_url_4x — не сможем скачать картинки")
    problems.check(collector.image_cache_key(ver.get("image_url_4x") or "") is not None,
                   "каталог: URL картинки не разбирается IMG_UUID_RE — Twitch сменил "
                   "схему CDN, посыплются картинки и карточки")


def check_events(problems, events):
    problems.check(len(events) >= MIN_EVENTS,
                   f"события: {len(events)}, ожидали ≥{MIN_EVENTS}")
    for ev in events:
        for key in ("title", "start_at_date", "end_at_date", "twitch_global_badges"):
            if key not in ev:
                problems.append(f"события: у «{ev.get('title', '?')}» нет ключа {key}")
                break


def check_availability(problems, events, page_avail):
    """Структура availability: она несёт окна и условия, и именно её SD перекраивал."""
    avs = [av
           for ev in events
           for b in ev.get("twitch_global_badges") or []
           for av in b.get("availability") or []]
    avs += [av for lst in (page_avail or {}).values() for av in lst]
    if not problems.check(avs, "availability: не нашли НИ ОДНОЙ записи — "
                               "перестали видеть окна и условия"):
        return
    problems.check(any("hidden" in av for av in avs),
                   "availability: пропало поле hidden — не отличим черновик "
                   "модератора от опубликованных данных")
    problems.check(any(av.get("steps") for av in avs),
                   "availability: нигде нет steps — условия снова обеднеют до "
                   "плоских полей (потеряется «в N разных дней» и порядок этапов)")
    # Категории меняли формат: было {"game": {...}}, стало плоское {"name": ...}
    cats = [c for av in avs for c in (av.get("categories") or [])]
    if cats:
        # Именно ВСЕ, а не «хоть одна»: при смене формата часть данных какое-то
        # время приходит по-старому, и проверка на any() пропустила бы поломку.
        bad = [c for c in cats if not site._category_name([c])]
        problems.check(not bad,
                       f"категории: у {len(bad)} из {len(cats)} не читается имя — "
                       "формат сменился (было {'game': {'name'}}, стало плоское "
                       "{'name'}), в постах пропадёт указание, где смотреть")


def check_steps(problems, events, page_avail):
    """Все ли типы шагов нам знакомы. Незнакомый обнуляет разбор условия целиком."""
    steps = [av.get("steps")
             for ev in events
             for b in ev.get("twitch_global_badges") or []
             for av in b.get("availability") or []]
    steps += [av.get("steps") for lst in (page_avail or {}).values() for av in lst]
    unknown = set()
    for st in steps:
        for stage in st or []:
            for step in stage or []:
                if site._step_ru(step or {}) is None:
                    unknown.add((step or {}).get("type") or "?")
    problems.check(not unknown,
                   f"steps: незнакомые типы шагов {sorted(unknown)} — условие таких "
                   "значков не разберётся, нужно дописать _step_ru")


def check_records(problems, snapshot):
    """Здоровье результата: доходят ли данные до того, что увидит читатель."""
    try:
        records = site.build_records(snapshot)
    except Exception as e:
        problems.append(f"build_records упал: {e}")
        return
    shown = [r for r in records
             if r["status"] in ("active", "upcoming") and r.get("group") != "__permanent__"]
    if not problems.check(len(shown) >= MIN_SHOWN,
                          f"показываем всего {len(shown)} значков (ожидали ≥{MIN_SHOWN}) — "
                          "похоже, окна перестали строиться"):
        return
    with_cond = sum(1 for r in shown if r.get("condition"))
    share = with_cond / len(shown)
    problems.check(share >= MIN_WITH_CONDITION,
                   f"условие есть лишь у {with_cond} из {len(shown)} показываемых "
                   f"({share:.0%}, ожидали ≥{MIN_WITH_CONDITION:.0%}) — разбор условий сломался")
    problems.check(all(r.get("window") for r in shown),
                   "у части показываемых значков нет окна — классификация поехала")


def main() -> int:
    use_snapshot = "--snapshot" in sys.argv
    problems = Problems()

    if use_snapshot:
        try:
            snapshot = json.loads(LATEST.read_text())
        except (OSError, ValueError) as e:
            print(f"не прочитать снапшот: {e}", file=sys.stderr)
            return 1
        badges = snapshot.get("badges") or []
        events = snapshot.get("events") or []
    else:
        try:
            build_id = collector.get_build_id()
            catalog = collector.fetch_next_data(build_id, "twitch/global-badges")
            events_raw = collector.fetch_next_data(build_id, "events")
        except Exception as e:
            # Сеть/недоступность — не наша поломка формата; молчим, чтобы не
            # превращать проверку структуры в детектор перебоев связи.
            print(f"источник недоступен ({e}) — проверку пропускаю", file=sys.stderr)
            return 0
        badges = collector.find_badge_list(catalog.get("pageProps") or {}) or []
        pp = events_raw.get("pageProps") or {}
        events = pp.get("initialEvents") or pp.get("initialData") or []
        # Полный снапшот из сети не собрать (страницы значков дороги) — здоровье
        # результата проверяем на последнем сохранённом.
        try:
            snapshot = json.loads(LATEST.read_text())
        except (OSError, ValueError):
            snapshot = None

    check_catalog(problems, badges)
    check_events(problems, events)
    if use_snapshot or snapshot:
        src = snapshot if snapshot else {}
        check_availability(problems, src.get("events") or events,
                           src.get("page_availability"))
        check_steps(problems, src.get("events") or events, src.get("page_availability"))
        check_records(problems, src)

    if problems:
        print(f"НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for p in problems:
            print(f"  · {p}")
        return 1
    print(f"формат в порядке: {len(badges)} значков, {len(events)} событий")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
