"""Профиль диалога: форма, потолок и порядок вытеснения (§5.5)."""

from __future__ import annotations

import datetime as dt

from siga.core.memory import MAX_TOKENS, Profile, estimate_tokens, trim


def test_a_profile_survives_a_round_trip() -> None:
    profile = Profile(
        facts=["работает в IT"],
        recurring_errors=["винительный после предлогов"],
        recent_topics=["кофе"],
        tone_notes="отвечает коротко",
        last_seen_at=dt.datetime(2026, 9, 7, 19, 12, tzinfo=dt.UTC),
    )

    assert Profile.from_dict(profile.to_dict()) == profile


def test_an_empty_profile_writes_nothing() -> None:
    """Пустые поля в промпте — потраченные токены и лишний шум для модели."""
    assert Profile().to_dict() == {}
    assert Profile().is_empty()


def test_garbage_from_the_model_does_not_break_the_profile() -> None:
    """Профиль пишет модель, и однажды она напишет туда не то."""
    profile = Profile.from_dict(
        {
            "facts": ["живёт в Лимассоле", "", 42, None],
            "recurring_errors": "не список",
            "tone_notes": {"нет": "строки"},
            "last_seen_at": "позавчера",
        }
    )

    assert profile.facts == ["живёт в Лимассоле"]
    assert profile.recurring_errors == []
    assert profile.tone_notes == ""
    assert profile.last_seen_at is None


def _long(prefix: str, index: int) -> str:
    """Запись профиля правдоподобной длины — фраза, а не одно слово."""
    return f"{prefix} {index}: " + "подробность про человека, записанная словами, " * 2


def test_recent_topics_are_evicted_before_facts() -> None:
    """FR-MEM-3: «есть дочь шести лет» переживает тему «кофе»."""
    profile = Profile(
        facts=[_long("факт", index) for index in range(15)],
        recent_topics=[_long("тема", index) for index in range(8)],
    )
    assert estimate_tokens(profile) > MAX_TOKENS, "иначе тест ничего не проверяет"

    trimmed = trim(profile)

    assert estimate_tokens(trimmed) <= MAX_TOKENS
    assert trimmed.facts == profile.facts, "факты держатся дольше всего"
    assert len(trimmed.recent_topics) < len(profile.recent_topics)


def test_the_oldest_goes_first() -> None:
    """Новое дописывается в конец, значит начало списка — самое старое."""
    profile = Profile(recent_topics=[f"тема {index}" * 60 for index in range(30)])

    trimmed = trim(profile)

    assert trimmed.recent_topics[-1] == profile.recent_topics[-1]
    assert profile.recent_topics[0] not in trimmed.recent_topics


def test_a_small_profile_is_left_alone() -> None:
    profile = Profile(facts=["есть дочь 6 лет"], recent_topics=["кофе", "Афины"])

    assert trim(profile) == profile
