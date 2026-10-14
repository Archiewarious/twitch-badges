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
