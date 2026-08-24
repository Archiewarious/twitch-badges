#!/usr/bin/env python3
"""
Лёгкий сторож изменений StreamDatabase.

Зачем. Полный refresh.sh тяжёлый (тянет страницы значков, качает картинки,
рендерит карточки, деплоит сайт), поэтому ходил раз в 30 минут — и ровно на
столько мы отставали. NASA Roman появился у SD в 19:00, конкуренты
опубликовали в 19:15, а наш следующий сбор был только в 19:26.

Что делает. Три дешёвых запроса (build_id + каталог + события, ~0.7 с и 336 КБ),
считает сигнатуру того, что вообще способно поменять наш вывод, и сравнивает с
последним снапшотом. Отличается — немедленно запускает полный refresh; совпадает —
молча выходит. Так частый опрос стоит копейки, а тяжёлая работа делается только
по факту новости.

Сигнатура намеренно НЕ покрывает весь ответ SD: user_count у значков тикает
постоянно, и снимок «всё целиком» давал бы refresh на каждый чих.

Коды выхода: 0 — изменений нет либо refresh запущен успешно; 1 — сбой.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import fetch_streamdb as collector  # noqa: E402

LATEST = ROOT / "data" / "streamdb_latest.json"
REFRESH_UNIT = "twitch-badges-refresh.service"


def badge_signature(badge):
    """То, что у значка влияет на наш пост: сам факт значка и его окно/условие.
    user_count и history сознательно игнорируем — они меняются постоянно."""
    cur = badge.get("current") or {}
    ver = cur.get("version") or {}
    avs = []
    for av in badge.get("availability") or []:
        avs.append([
            av.get("start_at_date"), av.get("start_at_time"),
            av.get("end_at_date"), av.get("end_at_time"),
            bool(av.get("subscription")), bool(av.get("subscription_gift")),
            bool(av.get("bits")), bool(av.get("watch")), av.get("watch_minutes"),
            sorted(av.get("costs") or []),
            [(c.get("game") or {}).get("name") for c in av.get("categories") or []],
        ])
    return [cur.get("set_id"), ver.get("title"), ver.get("image_url_4x"),
            bool(badge.get("added")), avs]


def event_signature(ev):
    return [
        ev.get("title"), ev.get("content"),
        ev.get("start_at_date"), ev.get("start_at_time"),
        ev.get("end_at_date"), ev.get("end_at_time"),
        bool(ev.get("hidden")),
        sorted((b.get("current") or {}).get("set_id") or ""
               for b in ev.get("twitch_global_badges") or []),
    ]


def _key(sig):
    """Полный сериализованный ключ сортировки.

    Сортировать по одному set_id нельзя: у значка столько записей, сколько
    версий (у «bits» — 28 порогов), все с ОДНИМ set_id. Порядок таких записей
    между ответами SD не гарантирован, и сигнатура прыгала бы туда-обратно,
    вызывая refresh на пустом месте."""
    return json.dumps(sig, ensure_ascii=False, sort_keys=True)


def signature(badges, events):
    return {
        "badges": sorted((badge_signature(b) for b in badges), key=_key),
        "events": sorted((event_signature(e) for e in events), key=_key),
    }


def describe_diff(old, new):
    """Человеческое «что именно изменилось» — уходит в лог юнита, чтобы потом
    было видно, ПОЧЕМУ случился внеочередной refresh."""
    if not old:
        return "первый запуск (сигнатуры ещё не было)"

    # Группируем по set_id: у одного значка несколько версий-строк, и сравнивать
    # их поштучно нельзя — сравниваем набор версий целиком.
    def by_id(sigs):
        out = {}
        for s in sigs:
            out.setdefault(s[0], []).append(s)
        return {k: sorted(v, key=_key) for k, v in out.items()}

    old_b, new_b = by_id(old.get("badges", [])), by_id(new["badges"])
    bits = []
    if set(new_b) - set(old_b):
        bits.append("новые значки: " + ", ".join(sorted(set(new_b) - set(old_b))))
    if set(old_b) - set(new_b):
        bits.append("исчезли: " + ", ".join(sorted(set(old_b) - set(new_b))))
    changed = sorted(i for i in (set(new_b) & set(old_b)) if old_b[i] != new_b[i])
    if changed:
        bits.append("изменились значки: " + ", ".join(changed[:8]))
    old_ev = {e[0]: e for e in old.get("events", [])}
    new_ev = {e[0]: e for e in new["events"]}
    ev_new = sorted(set(new_ev) - set(old_ev))
    ev_chg = sorted(t for t in (set(new_ev) & set(old_ev)) if old_ev[t] != new_ev[t])
    if ev_new:
        bits.append("новые события: " + ", ".join(ev_new[:5]))
    if ev_chg:
        bits.append("изменились события: " + ", ".join(ev_chg[:5]))
    return "; ".join(bits) or "изменения в полях, не попавших в краткий разбор"


def load_previous():
    """Сигнатура ПОСЛЕДНЕГО ЗАКОММИЧЕННОГО снапшота — единственный источник правды.

    Отдельный файл-состояние «что мы уже видели» здесь вреден: пометив изменение
    увиденным до того, как refresh реально отработал, мы потеряли бы новость
    навсегда, если бы тот упал. Сравнение с самим снапшотом самолечится — пока
    refresh не довёл данные до latest.json, следующий опрос увидит ту же разницу
    и попробует снова."""
    try:
        snap = json.loads(LATEST.read_text())
    except (OSError, ValueError):
        return None
    return signature(snap.get("badges") or [], snap.get("events") or [])


MAX_PAGE_PROBES = 6


def pending_badges(snapshot):
    """Значки, которые мы УЖЕ показываем, но условия или ссылки у них нет.

    Именно их описание SD дописывает позже: текст на странице значка пишет живой
    модератор (contexts[] у nasa-roman был пуст в первые часы). Каталог и события
    при этом не меняются, поэтому сигнатура выше такого дополнения не заметит —
    и мы бы узнали о нём только со следующим получасовым refresh, продолжая
    висеть в канале с «Условия уточняются»."""
    import generate_site as site
    from datetime import datetime, timedelta, timezone

    # Только те, чьи страницы полный сбор ВООБЩЕ пересканирует (collect_badge_pages
    # смотрит назад на PAGE_SCAN_DAYS). Иначе получается вечный цикл: у «egg»
    # (заведён 27 дней назад) описание на странице есть, в page_info его нет и не
    # будет — сбор туда не ходит, — и опрос каждые две минуты видел «новое
    # описание» и заново дёргал refresh, который ничего не менял.
    cutoff = datetime.now(timezone.utc) - timedelta(days=collector.PAGE_SCAN_DAYS)
    fresh = set()
    for b in snapshot.get("badges") or []:
        sid = (b.get("current") or {}).get("set_id")
        ts = collector._badge_added_at(b)
        if not sid or not ts:
            continue
        try:
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff:
                fresh.add(sid)
        except ValueError:
            continue

    out = []
    for r in site.build_records(snapshot):
        if r["set_id"] not in fresh:
            continue
        if r["status"] not in ("active", "upcoming") or r.get("group") == "__permanent__":
            continue
        w = r.get("window") or {}
        if r.get("condition") and w.get("twitch_link"):
            continue
        # Значок с ручным описанием в manual/overrides.json уже разобран человеком —
        # дёргать его страницу каждые две минуты незачем.
        if w.get("from_manual"):
            continue
        if r["set_id"]:
            out.append((r.get("first_seen") or "", r["set_id"]))
    # Свежие вперёд: описание дописывают именно новым значкам, а у старожилов
    # вроде FFXIV его нет месяцами. Без сортировки лимит съедали они, и самый
    # горячий значок (nasa-roman, заведён час назад) до проверки не доходил.
    out.sort(reverse=True)
    return [sid for _, sid in out[:MAX_PAGE_PROBES]]


def page_changed(build_id, snapshot, set_ids):
    """Появилось ли на странице значка то, чего у нас ещё нет. Сравниваем не сырой
    текст, а РАЗОБРАННЫЙ результат: правка опечатки в описании нам не новость,
    а вот возникшее условие или ссылка — да.

    Разбирать ОБЯЗАТЕЛЬНО с той же датой появления значка, что использует
    collect_badge_pages: она чинит год в датах (_fix_stale_year). Без неё разбор
    даёт другой результат, чем лежит в снапшоте, «изменение» находится каждый
    раз, и опрос дёргает refresh каждые две минуты вечно."""
    page_info = snapshot.get("page_info") or {}
    links = snapshot.get("twitch_links") or {}
    added_by_id = {}
    for b in snapshot.get("badges") or []:
        sid = (b.get("current") or {}).get("set_id")
        if sid and sid not in added_by_id:
            added_by_id[sid] = collector._badge_added_at(b)
    for sid in set_ids:
        try:
            text = collector.fetch_badge_page_text(build_id, sid)
        except Exception:
            continue
        if not text:
            continue
        info = collector.parse_badge_page_text(text, added_by_id.get(sid))
        link = collector.extract_link_from_text(text)
        if info and info != page_info.get(sid):
            return f"у значка {sid} появилось описание с датами"
        if link and link != links.get(sid):
            return f"у значка {sid} появилась ссылка"
    return None


def main() -> int:
    try:
        build_id = collector.get_build_id()
        catalog = collector.fetch_next_data(build_id, "twitch/global-badges")
        events_raw = collector.fetch_next_data(build_id, "events")
    except Exception as e:
        # Сеть моргнула — не наша авария: полный refresh по таймеру всё равно
        # придёт, а свой OnFailure-алерт тут только зашумит.
        print(f"опрос не удался: {e}", file=sys.stderr)
        return 0

    badges = collector.find_badge_list(catalog.get("pageProps") or {}) or []
    pp = events_raw.get("pageProps") or {}
    events = pp.get("initialEvents") or pp.get("initialData") or []

    # Тот же guard, что в fetch_streamdb: пустой каталог = сменилась вёрстка,
    # а не «значки кончились». Молча принять его за изменение и дёрнуть refresh
    # значило бы устроить шторм пустых прогонов.
    if not badges or not events:
        print("пустой каталог/события — похоже, SD сменил формат; пропускаю",
              file=sys.stderr)
        return 0

    new = signature(badges, events)
    old = load_previous()

    reason = None
    if old != new:
        reason = describe_diff(old, new)
    else:
        # Каталог тот же — но у показываемых значков могло появиться описание.
        try:
            snap = json.loads(LATEST.read_text())
            reason = page_changed(build_id, snap, pending_badges(snap))
        except Exception as e:
            print(f"проверка страниц не удалась: {e}", file=sys.stderr)

    if not reason:
        return 0

    print(f"ИЗМЕНЕНИЯ: {reason}")

    if os.environ.get("POLL_DRY_RUN"):
        print("POLL_DRY_RUN — refresh не запускаю")
        return 0

    # Через systemd, а не прямым вызовом refresh.sh: юнит уже один на всю
    # систему, поэтому параллельный прогон с таймерным не устроит гонку за
    # incoming.json и rsync.
    r = subprocess.run(["sudo", "-n", "systemctl", "start", "--no-block", REFRESH_UNIT],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"не смог запустить {REFRESH_UNIT}: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"{REFRESH_UNIT} запущен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
