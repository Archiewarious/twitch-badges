"""Проверки формата данных StreamDatabase — канарейка внутри сбора (D6).

Перенос check_format.py. Каждое поле, которое мы читаем, проверяется явно;
проверка результата — без памяти окон. Запускается в каждом полном сборе."""
from ..domain.catalog import image_cache_key
from ..domain.categories import _category_name
from ..domain.conditions import _step_ru
from ..domain.records import RecordsContext, build_records
from ..timeutil import parse_dt
from .sd_parse import _badge_added_at

# Ниже этих чисел — точно поломка, а не «кончились раздачи». Взяты с большим
# запасом от реальных значений на 31.08.2026 (484 значка, 14 событий, 15 показываем).
MIN_BADGES = 300


MIN_EVENTS = 5


MIN_SHOWN = 3


MIN_WITH_CONDITION = 0.7      # доля показываемых, у которых есть условие


MAX_NO_LINK = 0.1             # доля показываемых с категорией, но без ссылки на неё


# Формат каталога с ~27.09.2026 (30.09: 523 значка, added_at у 297, даты у 189).
MIN_ADDED_SHARE = 0.3         # доля значков, у которых известна дата появления


MIN_CATALOG_DATED = 50        # значков с датами прямо в каталоге


CATALOG_KEYS = ("added", "added_at", "cost")   # читаем у каждого значка


# С 02.10.2026 SD не пишет cancelled/system, пока они false (раньше — false у каждого
# значка, из-за чего требование ключа у 90% дало тревогу при 0 из 526). Флаг опционален:
# нет ключа = не отменена; если появился — обязан быть булевым.
CATALOG_OPTIONAL_FLAGS = ("cancelled",)


MIN_KEY_SHARE = 0.9


CATALOG_COSTS = {None, "free", "paid"}


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
    problems.check(image_cache_key(ver.get("image_url_4x") or "") is not None,
                   "каталог: URL картинки не разбирается IMG_UUID_RE — Twitch сменил "
                   "схему CDN, посыплются картинки и карточки")
    check_catalog_fields(problems, badges)


def _sd_datetime_ok(date_s, time_s):
    """Пустое — норма (дат нет); заполненное обязано разбираться."""
    if not date_s and not time_s:
        return True
    return bool(date_s) and parse_dt(date_s, time_s) is not None


def check_catalog_fields(problems, badges):
    """Поля каталога, которые мы читаем с ~27.09.2026: дата появления, окно, цена,
    отмена. Раньше SD сменил history → added_at, и это молча отключило монитор
    слепых зон и половину фолбэков, а проверки были зелёными."""
    n = len(badges)
    for key in CATALOG_KEYS:
        have = sum(1 for b in badges if key in b)
        problems.check(have >= MIN_KEY_SHARE * n,
                       f"каталог: поле {key} есть лишь у {have} из {n} значков — "
                       "формат каталога сменился")
    for key in CATALOG_OPTIONAL_FLAGS:
        odd = [b[key] for b in badges if key in b and not isinstance(b[key], bool)]
        if odd:
            problems.append(f"каталог: {key} у {len(odd)} значков не булево "
                            f"(например, {odd[0]!r}) — сменился смысл поля")
    added = sum(1 for b in badges if _badge_added_at(b))
    problems.check(added >= MIN_ADDED_SHARE * n,
                   f"каталог: дата появления (added_at/history) есть лишь у {added} из {n} — "
                   "перестанут работать слепые зоны, фолбэк «без дат» и сканирование страниц")
    dated = [b for b in badges if b.get("start_at_date") or b.get("end_at_date")]
    problems.check(len(dated) >= MIN_CATALOG_DATED,
                   f"каталог: даты прямо на значке у {len(dated)} (ожидали ≥{MIN_CATALOG_DATED}) — "
                   "SD снова перенёс окна, значки только из каталога пропадут")
    bad = [f"{(b.get('current') or {}).get('set_id')}: "
           f"{b.get('start_at_date')} {b.get('start_at_time')!r} → "
           f"{b.get('end_at_date')} {b.get('end_at_time')!r}"
           for b in dated
           if not (_sd_datetime_ok(b.get("start_at_date"), b.get("start_at_time"))
                   and _sd_datetime_ok(b.get("end_at_date"), b.get("end_at_time")))]
    problems.check(not bad, f"каталог: даты не разбираются у {len(bad)} значков "
                            f"(например, {'; '.join(bad[:3])}) — сменился формат даты/времени")
    costs = {b.get("cost") for b in badges} - CATALOG_COSTS
    problems.check(not costs, f"каталог: незнакомые значения cost {sorted(map(str, costs))} — "
                              "цена в постах станет «не указана»")


def check_events(problems, events):
    problems.check(len(events) >= MIN_EVENTS,
                   f"события: {len(events)}, ожидали ≥{MIN_EVENTS}")
    for ev in events:
        for key in ("title", "start_at_date", "end_at_date", "twitch_global_badges"):
            if key not in ev:
                problems.append(f"события: у «{ev.get('title', '?')}» нет ключа {key}")
                break
    # Время событий и их availability — HH:MM. Незнакомый вид parse_dt превратит
    # в «даты нет», и окна тихо пропадут.
    bad = []
    for ev in events:
        rows = [ev] + [av for b in ev.get("twitch_global_badges") or []
                       for av in b.get("availability") or []]
        for row in rows:
            for f in ("start", "end"):
                d, t = row.get(f"{f}_at_date"), row.get(f"{f}_at_time")
                if not _sd_datetime_ok(d, t):
                    bad.append(f"«{ev.get('title', '?')}» {f}: {d} {t!r}")
    problems.check(not bad, f"события: даты не разбираются ({len(bad)}; например, "
                            f"{'; '.join(bad[:3])}) — сменился формат даты/времени")


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
    # Поле переименовывали: steps → objectives (13.09.2026). Проверка поймала это
    # сразу; принимаем любое из имён, но хотя бы одно обязано быть.
    problems.check(any(av.get("objectives") or av.get("steps") for av in avs),
                   "availability: нигде нет objectives/steps — условия снова обеднеют до "
                   "плоских полей (потеряется «в N разных дней» и порядок этапов)")
    # Категории меняли формат: было {"game": {...}}, стало плоское {"name": ...}
    cats = [c for av in avs for c in (av.get("categories") or [])]
    if cats:
        # Именно ВСЕ, а не «хоть одна»: при смене формата часть данных какое-то
        # время приходит по-старому, и проверка на any() пропустила бы поломку.
        bad = [c for c in cats if not _category_name([c])]
        problems.check(not bad,
                       f"категории: у {len(bad)} из {len(cats)} не читается имя — "
                       "формат сменился (было {'game': {'name'}}, стало плоское "
                       "{'name'}), в постах пропадёт указание, где смотреть")


def check_steps(problems, events, page_avail):
    """Все ли типы шагов нам знакомы. Незнакомый обнуляет разбор условия целиком."""
    steps = [av.get("objectives") or av.get("steps")
             for ev in events
             for b in ev.get("twitch_global_badges") or []
             for av in b.get("availability") or []]
    steps += [av.get("objectives") or av.get("steps")
              for lst in (page_avail or {}).values() for av in lst]
    unknown = set()
    for st in steps:
        for stage in st or []:
            for step in stage or []:
                if _step_ru(step or {}) is None:
                    unknown.add((step or {}).get("type") or "?")
    problems.check(not unknown,
                   f"steps: незнакомые типы шагов {sorted(unknown)} — условие таких "
                   "значков не разберётся, нужно дописать _step_ru")


def check_records(problems, snapshot, now):
    """Здоровье результата: доходят ли данные до того, что увидит читатель."""
    try:
        # Без памяти окон: иначе потерянные источником даты known_windows
        # подставляет ещё 14 дней, и проверка их пропажи не видит.
        records = build_records(snapshot, RecordsContext(now=now))
    except Exception as e:  # noqa: BLE001 — любая поломка разбора и есть дрейф формата
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
    # Ссылки. В сентябре 2026 Twitch убрал og:title, прежняя проверка ссылок
    # молча отвергала всё, и посты неделю выходили без ссылок — узнали по
    # скриншоту конкурента. Теперь это видно здесь, в течение часа.
    with_game = [r for r in shown if (r.get("window") or {}).get("game")]
    no_link = [r["set_id"] for r in with_game
               if not str((r.get("window") or {}).get("category_href") or "")
               .startswith("https://www.twitch.tv/")]
    if with_game:
        problems.check(len(no_link) / len(with_game) <= MAX_NO_LINK,
                       f"категория без ссылки у {len(no_link)} из {len(with_game)} значков "
                       f"({', '.join(no_link[:5])}) — резолв категорий сломался "
                       "(fetch_streamdb.resolve_category_urls)")


def check_snapshot(snapshot, now) -> list[str]:
    """Все проверки снапшота. Пусто — формат в порядке."""
    p = Problems()
    badges, events = snapshot.get("badges") or [], snapshot.get("events") or []
    check_catalog(p, badges)
    check_events(p, events)
    check_availability(p, events, snapshot.get("page_availability"))
    check_steps(p, events, snapshot.get("page_availability"))
    check_records(p, snapshot, now)
    return list(p)
