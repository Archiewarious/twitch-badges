"""Разбор шаблонных описаний значков Twitch (Helix) и текста событий SD."""
import re

# «...to a streamer in the ELDEN RING category» — Twitch пишет это шаблонно, и
# для 56 значков из 357 это ЕДИНСТВЕННОЕ указание, где значок получать: у SD для
# них нет ни категорий, ни каналов. Без разбора пост выходил «Подписка или гифт»
# без единого слова о месте (так было у Sorcerer Rogier ELDEN RING).
# «in the X category» и «watching the X category» — второе у seegson-synthetics
# (Alien Isolation): без него пост вёл в Steam вместо категории Twitch.
CATEGORY_IN_DESC_RE = re.compile(r"\b(?:in|watching)\s+the\s+(.+?)\s+category\b", re.I)


# Второй шаблон: «gifting a sub to a Diablo streamer during BlizzCon 2026».
# Без него у Diablo вместо категории Twitch в пост шла ссылка на blizzcon.com —
# сайт мероприятия, где значок не выдают.
CATEGORY_STREAMER_RE = re.compile(r"\bto an?\s+(.{2,40}?)\s+streamer\b", re.I)


# «watching /PlaqueBoyMax during…» — канал прямо в описании.
CHANNEL_IN_DESC_RE = re.compile(r"(?:^|\s)/([A-Za-z0-9_]{3,25})\b")


# «The Festering Bloody Finger badge will be available…» — SD называет значок в
# тексте события ещё до того, как заведёт его сам. Читателю нужно именно это имя,
# а не название квестлайна: искать он будет значок.
BADGE_NAME_IN_DESC_RE = re.compile(
    r"\bThe\s+(.{2,60}?)\s+badge(?:s)?\s+(?:will\s+be|is|are|was|were)\b")


def badge_name_from_description(desc: str):
    """Имя значка из текста события, иначе None."""
    m = BADGE_NAME_IN_DESC_RE.search(desc or "")
    if not m:
        return None
    name = m.group(1).strip()
    # «Twitch global chat badges for Pichu, Bulbasaur…» — перечисление, не имя.
    return None if "," in name or len(name.split()) > 6 else name


def category_from_description(desc: str):
    """Название категории Twitch из описания значка, иначе None."""
    m = CATEGORY_IN_DESC_RE.search(desc or "")
    if m:
        return m.group(1).strip()
    m = CATEGORY_STREAMER_RE.search(desc or "")
    if m:
        name = m.group(1).strip()
        # «to a streamer in the …» ловится первым шаблоном; сюда попадают только
        # конструкции «to a <категория> streamer», но подстрахуемся.
        return None if name.lower() in ("streamer", "twitch") else name
    return None


def channel_from_description(desc: str):
    """Логин канала из описания значка («/PlaqueBoyMax»), иначе None."""
    m = CHANNEL_IN_DESC_RE.search(desc or "")
    return m.group(1) if m else None


TWITCH_URL_RE = re.compile(r"^https://(?:www\.)?twitch\.tv/")
LOGIN_RE = re.compile(r"[A-Za-z0-9_]+")


def single_channel_link(av):
    """Ссылка на канал раздачи, если он ОДИН. Yellow Party Hat давали за просмотр
    одного OldSchoolRS, SD это знал, а пост не мог сказать, куда идти: списки
    каналов мы обрезаем (trim_channels), и оставался лишь счётчик."""
    logins = av.get("channel_logins")
    if logins is None:
        logins = [((c or {}).get("user") or {}).get("login") for c in av.get("channels") or []]
    logins = [x for x in logins or [] if x]
    if len(logins) != 1 or not LOGIN_RE.fullmatch(logins[0]):
        return None
    return {"label": logins[0], "url": f"https://www.twitch.tv/{logins[0]}"}


def pick_twitch_link(page_link, av):
    """Ссылка со страницы значка; если её нет или она ведёт мимо Twitch (магазин
    игры), а канал раздачи один, — на этот канал: значок дают там, а не в Steam."""
    ch = single_channel_link(av or {})
    if ch and not TWITCH_URL_RE.match(str((page_link or {}).get("url") or "")):
        return ch
    return page_link
