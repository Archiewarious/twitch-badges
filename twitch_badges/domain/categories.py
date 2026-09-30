"""Категории Twitch: имена, ссылки, наследование в кампании."""

def _cat_key(name):
    """Ключ для поиска в кэше ссылок — без регистра и ведущего артикля.

    Одну и ту же категорию источники называют по-разному: SD пишет «The Blood of
    Dawnwalker», а из описания Twitch («in The Blood of Dawnwalker category»)
    шаблон достаёт её уже без «The». Точный поиск по строке промахивался, и
    ссылка не подставлялась, хотя в кэше лежала."""
    s = (name or "").strip().lower()
    for art in ("the ", "a ", "an "):
        if s.startswith(art):
            return s[len(art):]
    return s


def category_url_for(cx, name):
    if not name:
        return None
    want = _cat_key(name)
    for k, v in cx.category_urls.items():
        if _cat_key(k) == want:
            return v
    return None


def category_href(cx, cats):
    """Ссылка на директорию категории: сначала href от SD (если вернёт его
    обратно), иначе — наш проверенный URL по имени."""
    if not cats:
        return None
    c = cats[0] or {}
    if c.get("href"):
        return c["href"]
    return category_url_for(cx, _category_name(cats))


def _category_fields(cats):
    """Первая категория окна в виде (имя, box_art).

    Формат менялся: раньше {"game": {"name", "box_art_url"}, "href": ...}, с
    27.08.2026 — плоский {"id", "name", "box_art_url", "igdb_id"}. Читаем оба,
    иначе имя категории теряется молча: у всех значков стало «игра=—», и посты
    лишились указания, ГДЕ смотреть.

    href в новом формате нет вовсе, и построить его из имени нельзя: Twitch
    отвечает 200 на любой слаг, проверить догадку невозможно, а битый URL в
    inline-кнопке роняет весь ответ (Button_url_invalid). Поэтому ссылку берём
    только из данных, а имя показываем текстом."""
    if not cats:
        return "", None
    c = cats[0] or {}
    nested = c.get("game") or {}
    name = c.get("name") or nested.get("name") or ""
    box = c.get("box_art_url") or nested.get("box_art_url")
    # Имя берём, только если категория ОДНА. У покемонов их 20 (включая Just
    # Chatting, Art, Music), и подпись «Pokémon FireRed/LeafGreen» врала бы:
    # читатель решил бы, что нужна именно эта игра. Лучше промолчать.
    if len(cats) > 1:
        return "", box
    return name, box


def _category_name(cats):
    return _category_fields(cats)[0]


def category_names(cats):
    """Все имена категорий окна. Когда их много, одну показывать нельзя (см.
    _category_fields), но и молчать неправильно: у покемонов подходит 20 категорий,
    включая Just Chatting, и без списка читатель не знает, где смотреть вообще."""
    out = []
    for c in cats or []:
        n = (c or {}).get("name") or ((c or {}).get("game") or {}).get("name")
        if n and n not in out:
            out.append(n)
    return out


def _category_box_art(cats):
    return _category_fields(cats)[1]


def inherit_group_category(records):
    """Значок без категории берёт её у соседей по кампании, если у тех она одна.

    d20 из «Dungeons & Dragons: Dungeon Masters» описан как «watching Dungeon
    Masters on Twitch» — места нет, и пост вёл на сайт D&D, где значок не дают.
    Его сосед по событию ampersand выдаётся в категории Dungeons & Dragons —
    туда и ведём."""
    games = {}
    for r in records:
        w = r.get("window") or {}
        if r.get("group") and w.get("game") and w.get("category_href"):
            games.setdefault(r["group"], set()).add((w["game"], w["category_href"]))
    for r in records:
        w = r.get("window")
        if not w or w.get("game") or len(w.get("categories") or []) > 1:
            continue
        tl = str((w.get("twitch_link") or {}).get("url") or "")
        if "twitch.tv/" in tl:                   # своя ссылка на Twitch точнее
            continue
        cands = games.get(r.get("group")) or set()
        if len(cands) == 1:
            w["game"], w["category_href"] = next(iter(cands))
    return records


def canonicalize_categories(cx, records, names):
    """Имена категорий — как их зовёт сам Twitch, и ссылка к каждой.

    Источники пишут вольно: SD — «Unknown Game (ID: 13263)» для незнакомых ему
    игр, описания — «TFT», «COD: Black Ops 7». Читатель ищет категорию на Twitch
    по тому, что видит в посте, так что показываем имя оттуда же, откуда ссылка
    (fetch_streamdb.resolve_category_urls)."""
    def canon(n):
        return names.get(n) or n
    for r in records:
        w = r.get("window")
        if not w:
            continue
        g = w.get("game")
        if g:
            if not w.get("category_href"):
                w["category_href"] = category_url_for(cx, g)
            w["game"] = canon(g)
        if w.get("categories"):
            out = []
            for n in w["categories"]:
                c = canon(n)
                if c not in out:
                    out.append(c)
                url = category_url_for(cx, n)
                if url and not category_url_for(cx, c):
                    cx.category_urls[c] = url
            w["categories"] = out
    return records
