"""Сбор данных: один процесс вместо poll_changes.py + refresh.sh (D3, C08).

Запуск раз в 2 минуты (tb-collector.timer), под flock, с дедлайном 8 минут:
  1. Опрос SD: buildId, каталог, события (3 запроса) → сигнатура.
  2. Полный сбор, если сигнатура изменилась, у свежего значка появилось что-то
     на странице, прошло 30 мин с успешного сбора или снапшота нет.
  3. Полный сбор: страницы значков → Helix → категории → проверки формата →
     записи → картинки → карточки → ОДНА транзакция: снапшот + память окон +
     кэш категорий + запись о прогоне.
Ошибки → runs(error_kind) и backoff 2→60 мин с джиттером. Внешнего хостинга нет:
сбор никуда ничего не заливает и не просит повышенных прав, карточки в Telegram
отдаёт бот.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import alerts, db, store
from . import overrides as ov
from .cards import sync_cards
from .domain.descriptions import category_from_description
from .domain.records import RecordsContext, build
from .domain.windows import remember_windows
from .images import sync_images
from .publisher.captions import is_shown
from .sources import gql
from .sources.format import check_pages, check_snapshot
from .sources.helix import HelixAuthError
from .sources.http import DeadlineExceeded, HttpError
from .sources.sd_parse import (badge_page_text, extract_link_from_text, page_availability,
                               pages_to_scan, parse_badge_page_text, trim_channels)
from .sources.streamdb import SourceFormatError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectorConfig:
    full_every: timedelta = timedelta(minutes=30)
    backoff_min: float = 2          # минуты
    backoff_max: float = 60
    probe_max: int = 6
    helix_auth_alert_after: int = 3
    collapse_ratio: float = 0.5     # каталог меньше половины прежнего — не коммитим
    known_windows_keep: timedelta = timedelta(days=60)


@dataclass
class Sources:
    sd: object                      # sources.streamdb.StreamDB
    helix: object | None            # sources.helix.Helix или None (нет ключей)
    categories_lookup: object       # callable(names) -> {name: {url,name}|None}
    fetch_image: object             # callable(url) -> (status, content_type, bytes)


@dataclass
class RunResult:
    action: str                     # skip-backoff | no-change | collected | failed
    reason: str = ""
    stats: dict = field(default_factory=dict)


# ── сигнатура опроса (перенос poll_changes) ──

def badge_signature(badge):
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
    catalog = [badge.get(f) for f in ("start_at_date", "start_at_time", "end_at_date",
                                      "end_at_time", "cost")]
    catalog += [bool(badge.get("cancelled")), bool(badge.get("time_limited"))]
    return [cur.get("set_id"), ver.get("title"), ver.get("image_url_4x"),
            bool(badge.get("added")), avs, catalog]


def event_signature(ev):
    avs = []
    for b in ev.get("twitch_global_badges") or []:
        for av in b.get("availability") or []:
            avs.append([(b.get("current") or {}).get("set_id"), bool(av.get("hidden")),
                        av.get("start_at_date"), av.get("start_at_time"),
                        av.get("end_at_date"), av.get("end_at_time"),
                        json.dumps(av.get("objectives") or av.get("steps"), sort_keys=True),
                        sorted(av.get("costs") or []),
                        [c.get("name") or (c.get("game") or {}).get("name")
                         for c in av.get("categories") or []]])
    return [ev.get("title"), ev.get("content"), ev.get("start_at_date"), ev.get("start_at_time"),
            ev.get("end_at_date"), ev.get("end_at_time"), bool(ev.get("hidden")),
            sorted(avs, key=json.dumps)]


def signature(badges, events) -> str:
    key = lambda s: json.dumps(s, ensure_ascii=False, sort_keys=True)  # noqa: E731
    sig = {"badges": sorted((badge_signature(b) for b in badges), key=key),
           "events": sorted((event_signature(e) for e in events), key=key)}
    return hashlib.sha256(key(sig).encode()).hexdigest()


# ── состояние ──

def _state(conn) -> dict:
    return db.kv_get(conn, "collector", "state", {}) or {}


def _save_state(conn, st, now):
    db.kv_set(conn, "collector", "state", st, now)


def _run_start(conn, now) -> int:
    with db.tx(conn):
        return conn.execute("INSERT INTO runs(job, started_at) VALUES('collector', ?)",
                            (db.ts(now),)).lastrowid


def _run_finish(conn, rid, now, ok, stats, error_kind=None, error=None):
    conn.execute("UPDATE runs SET finished_at=?, ok=?, error_kind=?, error=?, stats=? WHERE id=?",
                 (db.ts(now), int(ok), error_kind, (error or "")[:1000] or None,
                  json.dumps(stats, ensure_ascii=False, default=str), rid))
    conn.execute("DELETE FROM runs WHERE id < ? - 5000", (rid,))


# ── шаги ──

def _probe_pages(conn, src, snap, badges, events, now, cfg, overrides) -> str | None:
    """Появилось ли на странице свежего показываемого значка то, чего у нас нет
    (условие, даты, ссылка). Только значки из pages_to_scan — ровно те, чьи
    страницы полный сбор и так смотрит (иначе вечный цикл)."""
    scan = pages_to_scan(events, badges, now)
    built = build(snap.data, RecordsContext(now=now, known_windows=db.kv_all(conn, "known_windows"),
                                            overrides=overrides))
    cand = []
    for r in built.records:
        if r["set_id"] not in scan or not is_shown(r, now):
            continue
        w = r.get("window") or {}
        if (r.get("condition") and w.get("twitch_link")) or w.get("from_manual"):
            continue
        cand.append((r.get("first_seen") or "", r["set_id"]))
    page_info = snap.data.get("page_info") or {}
    links = snap.data.get("twitch_links") or {}
    for _, sid in sorted(cand, reverse=True)[:cfg.probe_max]:
        text = badge_page_text(src.sd.badge_page(sid))
        if not text:
            continue
        info = parse_badge_page_text(text, *scan[sid])
        link = extract_link_from_text(text)
        if info and info != page_info.get(sid):
            return f"у значка {sid} изменилось описание"
        if link and link != links.get(sid):
            return f"у значка {sid} появилась ссылка"
    return None


def _collect_pages(src, events, badges, now, prev):
    links, infos, avails = {}, {}, {}
    scan = pages_to_scan(events, badges, now)
    failed, shapeless = [], []
    for sid, (added_iso, catalog_end) in scan.items():
        badge = src.sd.badge_page(sid)
        if badge is None:
            failed.append(sid)
        elif not ("contexts" in badge and "availability" in badge):
            # Ответила, но без contexts/availability — SD перекроил страницу
            # (02.10.2026 так молча опустели все 21 страница). Как и при сбое,
            # держим прошлое; format-drift скажет, что пора править разбор.
            shapeless.append(sid)
            badge = None
        if badge is None:
            # Страница не ответила — берём прошлое, а не теряем данные молча (C4)
            for mine, key in ((links, "twitch_links"), (infos, "page_info"),
                              (avails, "page_availability")):
                if sid in (prev.get(key) or {}):
                    mine[sid] = prev[key][sid]
            continue
        avs = page_availability(badge)
        if avs:
            avails[sid] = avs
        text = badge_page_text(badge)
        if not text:
            continue
        link = extract_link_from_text(text)
        if link:
            links[sid] = link
        info = parse_badge_page_text(text, added_iso, catalog_end)
        if info:
            infos[sid] = info
    return links, infos, avails, {"pages": len(scan), "pages_failed": failed,
                                  "pages_shapeless": shapeless}


def _category_names(events, page_avail, helix):
    names = set()
    for ev in events:
        for b in ev.get("twitch_global_badges") or []:
            for av in b.get("availability") or []:
                for c in av.get("categories") or []:
                    names.add(c.get("name") or (c.get("game") or {}).get("name"))
    for lst in page_avail.values():
        for av in lst:
            for c in av.get("categories") or []:
                names.add(c.get("name") or (c.get("game") or {}).get("name"))
    for info in (helix or {}).values():
        cat = category_from_description(info.get("description"))
        if cat:
            names.add(cat)
    for ev in events:
        cat = category_from_description(ev.get("content"))
        if cat:
            names.add(cat)
    return sorted(n for n in names if n)


def _helix(conn, src, prev, now, cfg, stats):
    if src.helix is None:
        return {}
    try:
        data = src.helix.collect()
    except HelixAuthError as e:
        n = (db.kv_get(conn, "collector", "helix_auth_fails", 0) or 0) + 1
        with db.tx(conn):
            db.kv_set(conn, "collector", "helix_auth_fails", n, now)
            if n >= cfg.helix_auth_alert_after:
                alerts.raise_alert(conn, "helix-auth", "Twitch не принимает ключи приложения",
                                   f"{n} сборов подряд Helix отвечает отказом ({e}). Описания и арт "
                                   "Twitch не обновляются. Проверь TWITCH_CLIENT_ID/SECRET на "
                                   "dev.twitch.tv (приложение не удалено, секрет не сброшен).", now)
        stats["helix_error"] = f"auth: {e}"
        return prev.get("helix") or {}
    except (HttpError, ValueError, KeyError) as e:
        stats["helix_error"] = str(e)[:200]
        return prev.get("helix") or {}
    with db.tx(conn):
        db.kv_set(conn, "collector", "helix_auth_fails", 0, now)
        alerts.clear_alert(conn, "helix-auth", "Helix снова принимает ключи", now)
    return data


def run_once(conn, src: Sources, *, now: datetime, images_dir: Path, cards_dir: Path,
             overrides_path: Path | None = None, cfg: CollectorConfig = CollectorConfig(),
             force: bool = False) -> RunResult:
    st = _state(conn)
    if not force and st.get("next_try_at") and now < datetime.fromisoformat(st["next_try_at"]):
        return RunResult("skip-backoff", f"пауза после ошибок до {st['next_try_at']}")
    rid = _run_start(conn, now)
    stats = {}
    try:
        overrides, ov_problems = ov.load(overrides_path) if overrides_path else ({}, [])
        with db.tx(conn):
            db.kv_set(conn, "overrides", "data", overrides, now)
            if ov_problems:
                alerts.raise_alert(conn, "overrides-invalid", "ошибка в manual/overrides.json",
                                   "Эти записи пропущены:\n" + "\n".join(ov_problems[:10]), now)
            else:
                alerts.clear_alert(conn, "overrides-invalid", "overrides.json снова в порядке", now)

        snap = store.current_snapshot(conn)
        badges = src.sd.catalog()
        events = src.sd.events()
        sig = signature(badges, events)
        last_ok = datetime.fromisoformat(st["last_ok_at"]) if st.get("last_ok_at") else None
        reason = None
        if snap is None:
            reason = "снапшота ещё нет"
        elif snap.signature != sig:
            reason = "изменились каталог или события"
        elif force:
            reason = "принудительно"
        elif last_ok is None or now - last_ok >= cfg.full_every:
            reason = "плановый сбор"
        else:
            reason = _probe_pages(conn, src, snap, badges, events, now, cfg, overrides)
        if not reason:
            with db.tx(conn):
                _run_finish(conn, rid, now, True, {"action": "no-change"})
                st.update(fail_count=0, next_try_at=None, last_poll_at=now.isoformat())
                _save_state(conn, st, now)
            return RunResult("no-change")

        prev = snap.data if snap else {}
        if prev.get("badges") and len(badges) < cfg.collapse_ratio * len(prev["badges"]):
            raise SourceFormatError(f"обвал каталога: {len(badges)} значков против "
                                    f"{len(prev['badges'])} — частичный ответ SD, не коммичу")
        links, infos, avails, pstats = _collect_pages(src, events, badges, now, prev)
        stats.update(pstats)
        helix = _helix(conn, src, prev, now, cfg, stats)
        cache, urls, names, gql_err = gql.resolve(
            _category_names(events, avails, helix), db.kv_all(conn, "category_urls"),
            src.categories_lookup, now)
        if gql_err:
            stats["gql_error"] = gql_err[:200]
        trim_channels(events)
        snapshot = {"fetched_at": db.ts(now), "build_id": src.sd.build_id, "badges": badges,
                    "events": events, "twitch_links": links, "page_info": infos,
                    "page_availability": avails, "category_urls": urls,
                    "category_names": names, "helix": helix}

        problems = check_pages(pstats) + check_snapshot(snapshot, now)
        stats["format_problems"] = problems
        known = db.kv_all(conn, "known_windows")
        built = build(snapshot, RecordsContext(now=now, known_windows=known, overrides=overrides))
        known = remember_windows(known, built.records)
        cutoff = (now - cfg.known_windows_keep).strftime("%Y-%m-%dT%H:%M")
        known = {k: v for k, v in known.items() if (v.get("end") or v.get("start") or "") >= cutoff}
        stats["images"] = sync_images(built.records, images_dir, src.fetch_image)
        stats["cards"] = {k: v for k, v in sync_cards(built.records, images_dir, cards_dir).items()
                          if k != "cards"}

        with db.tx(conn):
            store.put_snapshot(conn, snapshot, signature=sig, committed_at=now)
            db.kv_replace_ns(conn, "known_windows", known, now)
            db.kv_replace_ns(conn, "category_urls", cache, now)
            if problems:
                alerts.raise_alert(conn, "format-drift", "StreamDatabase сменил формат данных",
                                   "Что не так:\n" + "\n".join(f"· {p}" for p in problems[:10]) +
                                   "\n\nДанные сохранены, но часть значков может выпасть. "
                                   "Нужна правка кода сбора.", now)
            else:
                alerts.clear_alert(conn, "format-drift", "формат StreamDatabase снова в порядке", now)
            stats.update(action="collected", reason=reason, badges=len(badges), events=len(events),
                         records=len(built.records))
            _run_finish(conn, rid, now, True, stats)
            st.update(fail_count=0, next_try_at=None, last_ok_at=now.isoformat(),
                      last_poll_at=now.isoformat())
            _save_state(conn, st, now)
        log.info("сбор: %s — %d значков, %d событий, %d записей", reason, len(badges),
                 len(events), len(built.records))
        return RunResult("collected", reason, stats)
    except Exception as e:  # noqa: BLE001 — любая ошибка прогона: запись, backoff, выход
        kind = ("deadline" if isinstance(e, DeadlineExceeded) else
                "source_http" if isinstance(e, HttpError) else
                "source_format" if isinstance(e, SourceFormatError) else "internal")
        n = int(st.get("fail_count") or 0) + 1
        delay = min(cfg.backoff_max, cfg.backoff_min * 2 ** (n - 1)) * random.uniform(0.8, 1.2)
        st.update(fail_count=n, next_try_at=(now + timedelta(minutes=delay)).isoformat(),
                  last_error={"at": now.isoformat(), "kind": kind, "error": str(e)[:500]})
        with db.tx(conn):
            _run_finish(conn, rid, now, False, stats, kind, f"{e.__class__.__name__}: {e}")
            _save_state(conn, st, now)
        if kind == "internal":
            log.exception("сбор упал")
        else:
            log.warning("сбор не удался (%s): %s — следующая попытка через %.0f мин", kind, e, delay)
        return RunResult("failed", f"{kind}: {e}", stats)
