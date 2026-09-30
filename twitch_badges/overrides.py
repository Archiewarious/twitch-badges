"""manual/overrides.json: ручные данные о значках со схемой (B12).

Раньше `link` строкой вместо объекта ронял inline и пост значка
(AttributeError). Теперь плохая запись пропускается целиком, а владелец
получает алерт с причиной."""
from __future__ import annotations

import json
import re
from pathlib import Path

FIELDS = {"group", "condition", "cost", "start", "end", "link", "source"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{1,2}:\d{2})?$")


def validate_entry(v) -> list[str]:
    if not isinstance(v, dict):
        return ["запись не объект"]
    errs = [f"незнакомое поле {k}" for k in v if k not in FIELDS]
    for k in ("group", "condition", "source"):
        if k in v and v[k] is not None and not isinstance(v[k], str):
            errs.append(f"{k} должен быть строкой")
    if "cost" in v and v["cost"] not in (None, "free", "paid"):
        errs.append("cost — free или paid")
    for k in ("start", "end"):
        if k in v and v[k] is not None and not (isinstance(v[k], str) and DATE_RE.match(v[k])):
            errs.append(f"{k} — 'YYYY-MM-DD' или 'YYYY-MM-DDTHH:MM' (UTC), либо null")
    if "link" in v and v["link"] is not None:
        link = v["link"]
        if not (isinstance(link, dict) and isinstance(link.get("url"), str)
                and link["url"].startswith("https://") and isinstance(link.get("label", ""), str)):
            errs.append("link — объект {label, url} с https-ссылкой")
    return errs


def load(path: Path):
    """(чистые overrides {set_id: dict}, проблемы [str]). Нет файла — ({}, [])."""
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError) as e:
        return {}, [f"файл не читается: {e}"]
    if not isinstance(data, dict):
        return {}, ["в корне должен быть объект"]
    clean, problems = {}, []
    for k, v in data.items():
        if k.startswith("_"):
            continue
        errs = validate_entry(v)
        if errs:
            problems.append(f"{k}: {'; '.join(errs)}")
        else:
            clean[k] = v
    return clean, problems
