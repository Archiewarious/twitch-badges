"""Типы шагов условия: старые имена и с 11.10.2026 — с префиксом twitch_."""
import pytest

from twitch_badges.domain.conditions import cost_from_steps, describe_steps_ru
from twitch_badges.sources.format import Problems, check_steps

OLD = [[{"type": "subscription", "subscription_amount": 1, "subscription_tier": 1},
        {"type": "subscription_gift", "subscription_gift_amount": 2, "subscription_gift_tier": 1}],
       [{"type": "watch", "watch_minutes": 60, "watch_days": 3}]]
NEW = [[{"type": "twitch_subscription", "amount": 1, "tier": 1},
        {"type": "twitch_gift_subscription", "amount": 2, "tier": 1}],
       [{"type": "twitch_watch", "minutes": 60, "days": 3}]]


def test_new_names_read_like_old():
    text = describe_steps_ru(NEW)
    assert text == describe_steps_ru(OLD)
    assert "подписку" in text and "60" in text or "1 час" in text
    assert "3 разных дня" in text


@pytest.mark.parametrize("step, needle", [
    ({"type": "twitch_clip"}, "клип"),
    ({"type": "twitch_twitchcon_ticket", "days": 2}, "TwitchCon (2 дня)"),
    ({"type": "twitch_watch", "minutes": 30}, "30 минут"),
])
def test_single_new_steps(step, needle):
    assert needle in describe_steps_ru([[step]])


def test_cost_uses_normalized_types():
    assert cost_from_steps(NEW, None) == "paid"
    assert cost_from_steps([[{"type": "twitch_watch", "minutes": 20}]], None) == "free"
    assert cost_from_steps([[{"type": "twitch_clip"}]], None) == "free"


def _problems(steps):
    ev = [{"twitch_global_badges": [{"availability": [{"objectives": steps}]}]}]
    p = Problems()
    check_steps(p, ev, {})
    return list(p)


def test_format_check_accepts_new_names_and_flags_unknown():
    assert _problems(NEW) == []
    assert _problems([[{"type": "twitch_clip"}, {"type": "twitch_hologram"}]])


def test_unlock_step_is_prerequisite_stage():
    """14.10.2026 SD: эволюции покемонов — «получи значок», затем «смотри 20 мин × 3 дня»."""
    steps = [[{"type": "twitch_unlock",
               "twitch_global_chat_badges": [{"set_id": "gengar", "version": {"id": "1"}}]}],
             [{"type": "twitch_watch", "minutes": 20, "days": 3}]]
    text = describe_steps_ru(steps)
    assert text.startswith("Иметь предыдущий значок эволюции, затем смотреть эфир")
    assert "3 разных дня" in text and "gengar" not in text
    assert cost_from_steps(steps, None) == "free"
    assert _problems(steps) == []


def test_streamdatabase_link_is_not_a_watch_target():
    from twitch_badges.publisher.captions import watch_target
    from twitch_badges.sources.sd_parse import extract_link_from_text
    text = "This badge is the evolution of the [Pichu](https://www.streamdatabase.com/twitch/global-badges/pichu/1) badge."
    assert extract_link_from_text(text) is None
    r = {"window": {"twitch_link": {"label": "Pichu",
                                    "url": "https://www.streamdatabase.com/twitch/global-badges/pichu/1"}}}
    assert watch_target(r) == (None, None, None)


# ── сроки внутри шагов (стартовые покемоны, 20.10.2026) ──

from datetime import datetime, timezone  # noqa: E402

from twitch_badges.domain.conditions import entry_deadline  # noqa: E402

BULBASAUR = [[{"type": "twitch_subscription", "tier": 1, "amount": 1},
              {"type": "twitch_gift_subscription", "tier": 1, "amount": 1}],
             [{"type": "twitch_watch", "minutes": 20, "days": None,
               "end_at_date": "2026-10-01", "end_at_time": "07:00"}],
             [{"type": "twitch_watch", "minutes": 20, "days": 2}]]
UTC = timezone.utc


def test_entry_deadline_is_earliest_closed_stage():
    assert entry_deadline(BULBASAUR) == datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
    assert entry_deadline(NEW) is None
    # срок лишь у одной из альтернатив — этап не закрыт: можно пройти другой
    alt = [[{"type": "twitch_watch", "minutes": 20, "end_at_date": "2026-10-01"},
            {"type": "twitch_subscription", "tier": 1, "amount": 1}]]
    assert entry_deadline(alt) is None


def test_closed_entry_is_hidden_with_reason():
    from twitch_badges import monitors
    from twitch_badges.publisher.captions import is_shown
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    r = {"set_id": "bulbasaur", "status": "active", "group": None,
         "window": {"start": datetime(2026, 8, 24, tzinfo=UTC),
                    "end": datetime(2026, 11, 1, 6, tzinfo=UTC),
                    "entry_until": entry_deadline(BULBASAUR)}}
    assert not is_shown(r, now)
    assert "вход закрыт" in monitors.hidden_reason(r)
    assert is_shown(r, datetime(2026, 9, 20, tzinfo=UTC))      # до срока — показываем


def test_old_badge_with_live_catalog_window_is_scanned():
    from twitch_badges.sources.sd_parse import pages_to_scan
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    old = {"current": {"set_id": "pichu"}, "added": True, "added_at": "2026-08-17T16:05:41.845Z",
           "start_at_date": "2026-08-24", "start_at_time": "17:00",
           "end_at_date": "2026-11-01", "end_at_time": "07:00"}
    ended = dict(old, current={"set_id": "gone"}, end_at_date="2026-10-01")
    scan = pages_to_scan([], [old, ended], now)
    assert "pichu" in scan and "gone" not in scan


def test_streamer_steps():
    """Clipped That (23.10.2026): twitch_dj_program → twitch_stream → twitch_clip(share)."""
    steps = [[{"type": "twitch_dj_program"}], [{"minutes": None, "type": "twitch_stream"}],
             [{"download": None, "export": None, "self": True, "share": True, "type": "twitch_clip"}]]
    assert describe_steps_ru(steps) == ("Состоять в DJ-программе Twitch, затем провести стрим, "
                                        "затем создать клип и поделиться им")
    assert describe_steps_ru([[{"type": "twitch_stream", "minutes": 60}]]) == "Провести стрим 1 час"
    assert cost_from_steps(steps, None) == "free"
    assert _problems(steps) == []
