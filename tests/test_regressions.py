"""Случаи из живого прода после переключения."""
import mutations as m
from conftest import T0, load_fixture

from twitch_badges.domain.records import RecordsContext, build
from twitch_badges.publisher.captions import channel_buttons, channel_caption


def test_nodate_badge_uses_sd_category_not_steam():
    """01.10.2026, TheDragonsDogma: дат нет, описание Twitch категорию не называет,
    у SD категория Twitch есть на странице значка — пост должен вести туда, а не
    в Steam (twitch_links со страницы)."""
    snap = load_fixture()["snapshot"]
    sid = "thedragonsdogma"
    snap["badges"].append(m.catalog_badge(sid, "TheDragonsDogma", added_at=T0 - m.hours(4)))
    snap["helix"][sid] = {"title": "TheDragonsDogma", "click_url": "", "image_url_4x": m.image_url(sid),
                          "description": "This badge was earned by watching Dragon's Dogma 2: "
                                         "Dark Arisen for 1 hour"}
    snap["page_availability"][sid] = [m.availability(
        sid, None, None, objectives=[[{"type": "watch", "watch_minutes": 60}]],
        categories=[{"id": "435870350", "name": "Dragon's Dogma II"}])]
    snap["twitch_links"][sid] = {"label": "Dragon's Dogma 2's",
                                 "url": "https://store.steampowered.com/app/2054970/Dragons_Dogma_2/"}
    snap["category_urls"]["Dragon's Dogma II"] = "https://www.twitch.tv/directory/category/dragons-dogma-ii"
    # как в жизни: значок ещё и в событии, с availability без дат и без категорий
    ev = {"_id": "ev-dd2", "title": "Dragon's Dogma 2", "content": "", "start_at_date": "",
          "start_at_time": "", "end_at_date": "", "end_at_time": "",
          "twitch_global_badges": [{"_id": "x", "current": snap["badges"][-1]["current"],
                                    "availability": [m.availability(sid, None, None, categories=())]}]}
    snap["events"].append(ev)
    built = build(snap, RecordsContext(now=T0))
    r = next(x for x in built.records if x["set_id"] == sid)
    assert r["status"] == "upcoming" and r["window"]["dates_unknown"]
    assert r["window"]["category_href"] == "https://www.twitch.tv/directory/category/dragons-dogma-ii"
    cap = channel_caption("appeared_nodates", r, built.category_urls)
    assert "dragons-dogma-ii" in cap and "steampowered" not in cap
    assert channel_buttons(r)[0][0]["text"] == "▶️ Смотреть: Dragon's Dogma II"
