"""Планировщик постов: что и о ком сказать читателю канала на этом тике.

Чистая функция без ввода-вывода:

    result = plan(records, campaigns, aliases, now=..., data_at=..., has_art=..., cfg=...)

Модель — «что знает читатель» (раздел 5.4 плана). По каждой кампании храним
окно, условие и цену ровно в том виде, в каком они ушли в последний пост, и
набор выполненных стадий. Пара (кампания, стадия) уникальна в БД, поэтому
стадия не может выйти дважды, что бы ни случилось с данными.

Перенос publish_new из bot/bot.py с исправлениями:
  A3/F8  чистка по «давно не видели живым», а не по сохранённому end;
  A4     орфан без конца окна больше не обрывает поиск замещения;
  A5     пост, в котором уже было условие, закрывает «как получить»;
  B5     замещение орфана — алиас, стадии и знание переносятся;
  B6/Q6  анонс без дат: «сроки пока неизвестны», затем «Уточнили время»;
  B7     записи считаются на каждом тике с настоящим now (это делает вызывающий);
  B8     «Стартовало»/«Последний день» по точным датам живут до 48 ч без сбора;
  Q7     «Продлили до …» после «Последнего дня»;
  F9     очередь по возрасту значка, а не по имени группы.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..timeutil import effective_end
from .captions import dedup_key, is_shown, iso, window_vague

# Виды постов
APPEARED_ACTIVE = "appeared_active"
APPEARED_UPCOMING = "appeared_upcoming"
APPEARED_NODATES = "appeared_nodates"
ACTIVE_SHORT = "active_short"
STARTED = "started"
DATES = "dates_confirmed"
COND = "cond_confirmed"
ENDING = "ending"
EXTENDED = "extended"

ANNOUNCE_KINDS = {APPEARED_ACTIVE, APPEARED_UPCOMING, APPEARED_NODATES, ACTIVE_SHORT}
# Какие посты можно слать по уже известным точным датам, пока сбор лежит (B8)
EXACT_KINDS = {STARTED, ENDING, ACTIVE_SHORT}


@dataclass(frozen=True)
class PlanConfig:
    quiet_start: int = 0            # тихие часы МСК; start == end — выключены
    quiet_end: int = 0
    fresh_hours: float = 6          # анонсы, «как получить», «уточнили время»
    exact_hours: float = 48         # «стартовало» и «последний день» по точным датам
    max_groups: int = 5             # больше групп в очереди — алерт burst
    ending_window: float = 86400    # «последний день» — до конца ≤ 24 ч
    extend_min: float = 86400       # «продлили» — конец сдвинулся больше чем на сутки
    orphan_match_days: float = 2
    cleanup_days: float = 30
    paused: bool = False


@dataclass
class Item:
    campaign_id: str
    record: dict
    stages: list                    # какие стадии закрывает этот пост
    knowledge: dict                 # что читатель узнает: start, end, vague, condition, cost
    new: bool = False               # кампании ещё нет в БД


@dataclass
class Intent:
    kind: str
    group: str | None               # имя события для шапки альбома (None — без имени)
    items: list[Item]
    sort_key: tuple = ()

    @property
    def campaign_ids(self):
        return [i.campaign_id for i in self.items]


@dataclass
class Alias:
    alias: str
    campaign_id: str
    reason: str


@dataclass
class AlertSignal:
    key: str
    active: bool
    subject: str = ""
    body: str = ""


@dataclass
class PlanResult:
    intents: list[Intent] = field(default_factory=list)
    aliases: list[Alias] = field(default_factory=list)
    seen_live: list[str] = field(default_factory=list)
    cleanup: list[str] = field(default_factory=list)
    alerts: list[AlertSignal] = field(default_factory=list)
    held: list[tuple] = field(default_factory=list)      # (campaign_id, kind, причина)


# ── вспомогательное ──

def reader_vague(w) -> bool:
    """Окно, которое читатель знает не точно. Окно без дат — тоже неточное (B6):
    старый window_vague считал его точным, и «Уточнили время» не приходило."""
    w = w or {}
    if not w.get("start") and not w.get("end"):
        return True
    return window_vague(w)


def knowledge_of(r) -> dict:
    """Что узнает читатель из поста об этой записи."""
    w = r.get("window") or {}
    return {"start": iso(w.get("start")), "end": iso(w.get("end")),
            "vague": reader_vague(w), "condition": r.get("condition") or None,
            "cost": r.get("cost")}


def _parse(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None


def in_quiet_hours(now, cfg: PlanConfig) -> bool:
    if cfg.quiet_start == cfg.quiet_end:
        return False
    h = (now + timedelta(hours=3)).hour
    if cfg.quiet_start < cfg.quiet_end:
        return cfg.quiet_start <= h < cfg.quiet_end
    return h >= cfg.quiet_start or h < cfg.quiet_end


def night_hold(kind, r, now, cfg: PlanConfig) -> bool:
    """Несрочное придерживаем в тихие часы; срочное — всегда сразу."""
    if kind in (ENDING, DATES, COND, EXTENDED, APPEARED_NODATES):
        return True
    if kind == APPEARED_UPCOMING:
        s = (r.get("window") or {}).get("start")
        return bool(s) and (s - now).total_seconds() > cfg.ending_window
    return False


def live_records(records, now) -> dict:
    """{set_id: запись} того, что показываем (как live в старом publish_new)."""
    live = {}
    for r in records:
        if not is_shown(r, now):
            continue
        key = dedup_key(r)
        if key not in live or r["status"] == "active":
            live[key] = r
    return live


def _category(r):
    return ((r.get("window") or {}).get("game") or "").strip().lower()


def find_orphan(key, r, campaigns, live, cfg: PlanConfig):
    """Кампания-орфан (анонс до появления значка), которую замещает эта запись.

    Совпадение окна: начало ±2 дня и конец ±2 дня (если конец известен у обоих).
    Плюс одно из: запись орфана уже пропала из данных (SD привязал значок к
    событию), общая группа или общая категория. В старом коде орфан без конца
    окна обрывал поиск (A4), а при замещении терялись стадии (B5)."""
    w = r.get("window") or {}
    start, end = w.get("start"), w.get("end")
    if not start:
        return None
    tol = timedelta(days=cfg.orphan_match_days)
    for cid in sorted(campaigns):
        c = campaigns[cid]
        if cid == key or not c.from_orphan or "announce" not in c.stages:
            continue
        cs, ce = _parse(c.known_start), _parse(c.known_end)
        if not cs or abs(cs - start) > tol:
            continue
        # Концы сравниваем, только если известны оба: анонс орфана часто уходит
        # без конца окна (SD его ещё не знал), и старый код на таком орфане
        # обрывал поиск целиком (A4).
        if ce is not None and end is not None and abs(ce - end) > tol:
            continue
        orphan_live = live.get(cid)
        same_group = bool(c.grp) and c.grp == r.get("group")
        same_cat = bool(_category(r)) and orphan_live is not None and _category(orphan_live) == _category(r)
        if orphan_live is None or same_group or same_cat:
            return cid
    return None


# ── главное ──

def plan(records, campaigns: dict, aliases: dict, *, now: datetime, data_at: datetime,
         has_art, cfg: PlanConfig = PlanConfig()) -> PlanResult:
    """records — записи из текущего снапшота, посчитанные с этим же now.
    campaigns — {id: store.Campaign} (со stages), aliases — {alias: id}.
    data_at — когда закоммичен снапшот. has_art(r) — есть ли настоящий арт."""
    res = PlanResult()
    live = live_records(records, now)
    aliases = dict(aliases)
    age_h = (now - data_at).total_seconds() / 3600

    # Свежесть данных (B8): > 48 ч — стоп всего и алерт; > 6 ч — только
    # «стартовало»/«последний день» по точным датам.
    if age_h > cfg.exact_hours:
        res.alerts.append(AlertSignal(
            "posting-paused-stale", True, f"данным {age_h:.0f} ч — постинг остановлен",
            "Сбор не обновлял данные больше 48 часов. Посты не уходят, чтобы не "
            "публиковать неверный жизненный цикл. Смотри алерт collector-failing."))
    elif age_h > cfg.fresh_hours:
        res.alerts.append(AlertSignal(
            "posting-paused-stale", True, f"данным {age_h:.0f} ч — анонсы остановлены",
            "Уходят только «стартовало» и «последний день» по точным датам. "
            "Смотри алерт collector-failing."))
    else:
        res.alerts.append(AlertSignal("posting-paused-stale", False, "данные снова свежие"))

    announce, ending = [], []           # [(kind, Item)]
    for key, r in sorted(live.items()):
        cid = aliases.get(key, key)
        c = campaigns.get(cid)
        if c is not None:
            res.seen_live.append(cid)
        if not has_art(r):
            # Без настоящего арта в канал не идём и кампанию не заводим: когда
            # Twitch выложит картинку, значок объявится обычным порядком.
            res.held.append((cid, None, "нет арта"))
            continue
        w = r.get("window") or {}
        end = effective_end(w)
        active = r["status"] == "active"
        short = bool(active and end and 0 <= (end - now).total_seconds() <= cfg.ending_window)
        k = knowledge_of(r)

        if c is None or "announce" not in c.stages:
            orphan = find_orphan(key, r, campaigns, live, cfg) if c is None else None
            if orphan:
                res.aliases.append(Alias(key, orphan, "замещает анонс-орфан"))
                aliases[key] = orphan
                res.seen_live.append(orphan)
                c = campaigns[orphan]
                cid = orphan
            else:
                if short:
                    kind, stages = ACTIVE_SHORT, ["announce", "started", "ending"]
                elif active:
                    kind, stages = APPEARED_ACTIVE, ["announce", "started"]
                elif not w.get("start") and not w.get("end"):
                    kind, stages = APPEARED_NODATES, ["announce"]
                else:
                    kind, stages = APPEARED_UPCOMING, ["announce"]
                announce.append((kind, Item(cid, r, stages, k, new=c is None)))
                continue

        st = c.stages
        cur_end = end
        known_end = _parse(c.known_end)
        known_eff_end = effective_end({"end": known_end, "dates_coarse": w.get("dates_coarse")}) \
            if known_end else None
        if active and "started" not in st:
            if short:
                announce.append((ACTIVE_SHORT, Item(cid, r, ["started", "ending"], k)))
            else:
                announce.append((STARTED, Item(cid, r, ["started"], k)))
        elif (r["status"] == "upcoming" and c.known_vague and not reader_vague(w)
              and "dates" not in st and w.get("start")
              and (w["start"] - now).total_seconds() > cfg.ending_window):
            announce.append((DATES, Item(cid, r, ["dates"], k)))
        elif (c.known_condition is None and r.get("condition") and "cond" not in st
              and not (end and (end - now).total_seconds() <= cfg.ending_window)):
            announce.append((COND, Item(cid, r, ["cond"], k)))
        elif ("ending" in st and cur_end and known_eff_end
              and (cur_end - known_eff_end).total_seconds() > cfg.extend_min
              and f"extended:{cur_end:%Y-%m-%d}" not in st):
            announce.append((EXTENDED, Item(cid, r, [f"extended:{cur_end:%Y-%m-%d}"], k)))
        elif active and short and "ending" not in st:
            ending.append((ENDING, Item(cid, r, ["ending"], k)))
        elif (active and short and "ending" in st
              and f"extended:{cur_end:%Y-%m-%d}" in st
              and f"ending:{cur_end:%Y-%m-%d}" not in st):
            # Кампанию продлили («Продлили до …» ушёл), и теперь подошёл новый
            # конец — читателю снова нужен «последний день».
            ending.append((ENDING, Item(cid, r, [f"ending:{cur_end:%Y-%m-%d}"], k)))

    # Свежесть: что можно слать с такими данными
    def allowed(kind, item):
        if age_h > cfg.exact_hours:
            return False
        if age_h > cfg.fresh_hours:
            return kind in EXACT_KINDS and not reader_vague(item.record.get("window"))
        return True

    for lst in (announce, ending):
        keep = []
        for kind, item in lst:
            if allowed(kind, item):
                keep.append((kind, item))
            else:
                res.held.append((item.campaign_id, kind, "данные несвежие"))
        lst[:] = keep

    if cfg.paused:
        for kind, item in announce + ending:
            res.held.append((item.campaign_id, kind, "пауза владельца"))
        announce, ending = [], []

    night = in_quiet_hours(now, cfg)
    ready = []
    for kind, item in announce:
        if night and night_hold(kind, item.record, now, cfg):
            res.held.append((item.campaign_id, kind, "тихие часы"))
        else:
            ready.append((kind, item))

    # Значки одного события — один пост-альбом; разные события — разные посты.
    buckets = {}
    for kind, item in ready:
        r = item.record
        g = (r.get("window") or {}).get("group") or r.get("group") or ""
        if g and g != "__permanent__":
            gk = g
        elif not r.get("window") and r.get("first_seen"):
            gk = f"\x00nodate:{r['first_seen'][:10]}"
        else:
            gk = f"\x00solo:{item.campaign_id}"
        buckets.setdefault((gk, kind), []).append(item)

    intents = []
    for (gk, kind), items in buckets.items():
        age = min((i.record.get("first_seen") or "~") for i in items)
        intents.append(Intent(kind, None if gk.startswith("\x00") else gk, items,
                              sort_key=(age, gk, kind)))
    intents.sort(key=lambda i: i.sort_key)

    if len(intents) > cfg.max_groups:
        names = ", ".join(sorted({i.group for i in intents if i.group})) or "—"
        res.alerts.append(AlertSignal(
            "burst", True, f"{len(intents)} новых групп значков в очереди",
            f"Группы: {names}\n\nПубликую по одной группе за тик (раз в 2 минуты), "
            "не залпом. Если это мусор от StreamDatabase и постить не надо — "
            "останови командой /pause в личке бота."))
    elif intents:
        res.alerts.append(AlertSignal("burst", False, "очередь анонсов вернулась в норму"))

    # ПОЯВЛЕНИЯ/СТАРТЫ/УТОЧНЕНИЯ — по одной группе за тик.
    if intents:
        res.intents.append(intents[0])

    # ПОСЛЕДНИЙ ДЕНЬ — все разом одним альбомом; ночью придерживаем.
    if ending:
        if night:
            for kind, item in ending:
                res.held.append((item.campaign_id, kind, "тихие часы"))
        else:
            items = [item for _, item in ending]
            res.intents.append(Intent(ENDING, None, items, sort_key=("", "", ENDING)))

    # ЧИСТКА: кампании, которых давно не видели живыми (A3, F8).
    cutoff = now - timedelta(days=cfg.cleanup_days)
    seen = set(res.seen_live)
    for cid, c in campaigns.items():
        if cid in seen:
            continue
        last = _parse(c.last_seen_live_at)
        if last and last < cutoff:
            res.cleanup.append(cid)
    return res


# ── применение (то же делает outbox в БД после успешной отправки) ──

def apply_sent(campaigns: dict, intent: Intent, now: datetime, make_campaign):
    """Обновить знание читателя и стадии после того, как пост ушёл."""
    for item in intent.items:
        c = campaigns.get(item.campaign_id)
        if c is None:
            c = make_campaign(item)
            campaigns[item.campaign_id] = c
        k = item.knowledge
        c.known_start, c.known_end = k["start"], k["end"]
        c.known_vague = k["vague"]
        c.known_condition = k["condition"]
        c.known_cost = k["cost"]
        c.last_posted_at = iso(now)
        c.first_posted_at = c.first_posted_at or iso(now)
        c.last_seen_live_at = iso(now)
        c.stages.update(item.stages)
