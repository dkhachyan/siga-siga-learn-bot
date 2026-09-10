"""Раскладка дня: во сколько Ник напишет и что будет, если он опоздал.

Случайность здесь настоящая, поэтому проверяются не конкретные минуты, а
обещания: слоты внутри окна, по возрастанию, не ближе обещанного промежутка
друг к другу, и от запуска к запуску разные. `Random` с зерном — только там,
где нужен повторяемый пример.
"""

from __future__ import annotations

import datetime as dt
import random
from itertools import pairwise
from zoneinfo import ZoneInfo

from siga.core import schedule

NICOSIA = ZoneInfo("Asia/Nicosia")
DAY = dt.date(2026, 9, 10)
MORNING = dt.time(10, 0)
EVENING = dt.time(20, 0)


def slots(count: int = 4, *, seed: int = 1, **kwargs: object) -> list[dt.datetime]:
    params: dict[str, object] = {
        "tz": NICOSIA,
        "start": MORNING,
        "end": EVENING,
        "count": count,
        "gap": schedule.DEFAULT_MIN_GAP,
        "rng": random.Random(seed),
    }
    params.update(kwargs)
    return schedule.plan_slots(DAY, **params)  # type: ignore[arg-type]


def gaps(moments: list[dt.datetime]) -> list[dt.timedelta]:
    return [later - earlier for earlier, later in pairwise(moments)]


def test_the_day_gets_as_many_episodes_as_asked() -> None:
    assert len(slots(4)) == 4


def test_slots_stay_inside_the_window() -> None:
    opens, closes = schedule.window_bounds(DAY, tz=NICOSIA, start=MORNING, end=EVENING)

    for moment in slots(6):
        assert opens <= moment <= closes


def test_slots_go_in_order_and_keep_their_distance() -> None:
    """FR-SCH-8: два разговора подряд читаются как назойливость."""
    for gap in gaps(slots(6)):
        assert gap >= schedule.DEFAULT_MIN_GAP


def test_the_same_day_looks_different_every_time() -> None:
    """Без сдвига расписание превращается в будильник."""
    first = slots(4, seed=1)
    second = slots(4, seed=2)

    assert first != second


def test_a_narrow_window_gets_fewer_episodes_not_a_burst() -> None:
    """Полтора часа и четыре эпизода — просьба невыполнимая, и зазор важнее."""
    tight = slots(4, start=dt.time(18, 0), end=dt.time(19, 30))

    assert len(tight) == 2
    assert gaps(tight)[0] >= schedule.DEFAULT_MIN_GAP


def test_an_hour_long_window_still_gets_one_episode() -> None:
    assert len(slots(3, start=dt.time(18, 0), end=dt.time(18, 40))) == 1


def test_a_chosen_gap_outranks_the_chosen_frequency() -> None:
    """FR-SCH-8: обещание молчать сильнее просьбы приходить чаще."""
    two_hours = dt.timedelta(hours=2)
    rare = slots(6, gap=two_hours)

    assert len(rare) < len(slots(6))
    for gap in gaps(rare):
        assert gap >= two_hours


def test_an_upside_down_window_plans_nothing() -> None:
    assert slots(4, start=EVENING, end=MORNING) == []


def test_a_late_planner_does_not_dump_the_missed_slots() -> None:
    """Воркер проснулся в полдень — утренние эпизоды пропали, и это правильно."""
    noon = dt.datetime.combine(DAY, dt.time(12, 0), tzinfo=NICOSIA).astimezone(dt.UTC)

    planned = slots(6, not_before=noon)

    assert planned, "остаток дня всё же расписан"
    assert all(moment >= noon for moment in planned)
    assert len(planned) < 6


def test_summer_time_is_counted_in_local_hours() -> None:
    """10:00 у человека — это разный UTC зимой и летом."""
    summer, _ = schedule.window_bounds(dt.date(2026, 7, 1), tz=NICOSIA, start=MORNING, end=EVENING)
    winter, _ = schedule.window_bounds(dt.date(2026, 1, 1), tz=NICOSIA, start=MORNING, end=EVENING)

    assert summer.hour == 7
    assert winter.hour == 8


def test_the_window_is_checked_again_at_sending_time() -> None:
    """Человек мог сузить окно после того, как день уже расписали."""
    evening = dt.datetime(2026, 9, 10, 21, 0, tzinfo=NICOSIA)

    assert not schedule.is_within_window(evening, tz=NICOSIA, start=MORNING, end=EVENING)
    assert schedule.is_within_window(
        evening.replace(hour=15), tz=NICOSIA, start=MORNING, end=EVENING
    )


def test_an_hour_late_is_still_worth_sending() -> None:
    fire_at = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)

    assert not schedule.is_overdue(fire_at, fire_at + dt.timedelta(minutes=60))
    assert schedule.is_overdue(fire_at, fire_at + dt.timedelta(minutes=91))


def test_silence_is_noticed_after_forty_minutes() -> None:
    sent = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)

    assert not schedule.needs_nudge(sent, sent + dt.timedelta(minutes=39))
    assert schedule.needs_nudge(sent, sent + dt.timedelta(minutes=40))


def test_planning_happens_at_three_in_the_morning_local() -> None:
    three_local = dt.datetime(2026, 9, 10, 3, 30, tzinfo=NICOSIA)

    assert schedule.is_planning_time(three_local, tz=NICOSIA)
    assert not schedule.is_planning_time(three_local, tz=ZoneInfo("Europe/Lisbon"))


def test_the_local_day_is_not_the_utc_day() -> None:
    late = dt.datetime(2026, 9, 10, 22, 30, tzinfo=dt.UTC)

    assert schedule.local_day(late, tz=NICOSIA) == dt.date(2026, 9, 11)


def test_a_pause_shorter_than_a_day_still_costs_a_day() -> None:
    """FR-SCH-7: считаем в пользу человека — период сдвинется чуть щедрее."""
    start = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)

    assert schedule.paused_days(start, start) == 0
    assert schedule.paused_days(start, start + dt.timedelta(hours=3)) == 1
    assert schedule.paused_days(start, start + dt.timedelta(days=1, hours=12)) == 2


def test_the_frequency_stays_within_bounds() -> None:
    assert schedule.clamp_episodes_per_day(0) == schedule.MIN_EPISODES_PER_DAY
    assert schedule.clamp_episodes_per_day(99) == schedule.MAX_EPISODES_PER_DAY
    assert schedule.clamp_episodes_per_day(3) == 3


def test_the_gap_from_the_database_stays_within_bounds() -> None:
    """Откат миграции может вернуть значение, которого код уже не ждёт."""
    assert schedule.clamp_min_gap(0) == schedule.MIN_GAP_MINUTES
    assert schedule.clamp_min_gap(9999) == schedule.MAX_GAP_MINUTES
    assert schedule.clamp_min_gap(90) == 90


def test_a_typed_frequency_is_refused_instead_of_trimmed() -> None:
    """Набранному руками числу честнее ответить «столько не могу»."""
    assert schedule.parse_episodes_per_day(" 10 ") == 10
    assert schedule.parse_episodes_per_day(str(schedule.MAX_EPISODES_PER_DAY + 1)) is None
    assert schedule.parse_episodes_per_day("0") is None
    assert schedule.parse_episodes_per_day("-3") is None
    assert schedule.parse_episodes_per_day("много") is None


def test_a_typed_gap_reads_hours_and_minutes() -> None:
    """С телефона пишут как придётся — единица необязательна."""
    assert schedule.parse_gap("90") == 90
    assert schedule.parse_gap("90 минут") == 90
    assert schedule.parse_gap("2 часа") == 120
    assert schedule.parse_gap("1ч") == 60


def test_a_typed_gap_outside_the_bounds_is_refused() -> None:
    assert schedule.parse_gap("5") is None
    assert schedule.parse_gap("9 часов") is None
    assert schedule.parse_gap("полчаса") is None
    assert schedule.parse_gap("") is None


def test_a_window_is_measured_by_the_gap_it_has_to_hold() -> None:
    """Выбрав два часа между разговорами, человек объявил часовое окно пустым."""
    assert schedule.parse_window("09:00-10:30") is not None
    assert schedule.parse_window("09:00-10:30", min_span=dt.timedelta(hours=2)) is None
