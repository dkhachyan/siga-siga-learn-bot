"""Раскладка эпизодов по дню: когда именно писать человеку (§5.6).

Чистая арифметика над временем — ни базы, ни Telegram. Вход: окно, число
эпизодов и часовой пояс; выход: список моментов. Всё, что делает планировщик
интересным, живёт здесь, а воркер только ходит с этим в базу.

Главная мысль раскладки: время суток должно быть похоже на человеческое, а не
на будильник. Ровно в 10:00, 13:20, 16:40 пишет система рассылки; живой
знакомый пишет «когда-то днём». Отсюда сдвиг и минимальный зазор.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

#: Во сколько по времени человека планируется его день, FR-SCH-2.
PLANNING_HOUR = 3

#: Ближе этого два эпизода не ставятся, пока человек не сказал иного, FR-SCH-8.
#: Эпизод — это не одно задание, а разговор на несколько минут; два подряд
#: читаются как назойливость.
DEFAULT_MIN_GAP = dt.timedelta(minutes=45)

#: Границы промежутка, FR-SCH-8. Ближе четверти часа два разговора наезжают друг
#: на друга — предыдущий ещё идёт. Дальше восьми часов настройка вырождается в
#: «раз в день», а это уже частота, и настраивается она отдельно.
MIN_GAP_MINUTES = 15
MAX_GAP_MINUTES = 480

#: Насколько опоздавшее событие ещё имеет смысл отправлять, FR-SCH-4.
MAX_DELAY = dt.timedelta(minutes=90)

#: Через сколько молчания Ник напоминает о себе — один раз, FR-SCH-6.
NUDGE_AFTER = dt.timedelta(minutes=40)

#: Границы частоты, FR-SCH-1. Верхняя совпадает с CHECK-ограничением в `users`
#: намеренно: разъехавшись, они дали бы отказ базы вместо внятного ответа.
MIN_EPISODES_PER_DAY = 1
MAX_EPISODES_PER_DAY = 12


def zone(name: str) -> ZoneInfo:
    """Часовой пояс по имени. Незнакомое имя — не повод сорвать день.

    В базе лежит строка, а базы данных о часовых поясах у разных систем
    разъезжаются: зона, которую записали при регистрации, может исчезнуть при
    обновлении образа. Отправить в UTC хуже, чем вовремя, но лучше, чем никак.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("неизвестный часовой пояс %r, считаем по UTC", name)
        return ZoneInfo("UTC")


def window_bounds(
    day: dt.date, *, tz: ZoneInfo, start: dt.time, end: dt.time
) -> tuple[dt.datetime, dt.datetime]:
    """Начало и конец окна этого дня в UTC.

    Считаем через локальное время и переводим: у часового пояса бывает переход
    на летнее время, и «10:00 у человека» — это не фиксированный сдвиг от UTC.
    """
    opens = dt.datetime.combine(day, start, tzinfo=tz)
    closes = dt.datetime.combine(day, end, tzinfo=tz)
    return opens.astimezone(dt.UTC), closes.astimezone(dt.UTC)


def window_minutes(start: dt.time, end: dt.time) -> float:
    """Длина окна в минутах. Вывернутое наизнанку окно даёт отрицательное.

    Считается в пределах суток, без часового пояса: перевод стрелок сдвигает
    границы окна вместе, а не растягивает его.
    """
    span = dt.datetime.combine(dt.date.min, end) - dt.datetime.combine(dt.date.min, start)
    return span.total_seconds() / 60


def fits_in_window(minutes: float, count: int, *, gap: dt.timedelta) -> int:
    """Сколько эпизодов реально влезает в окно такой длины.

    Промежуток — обещание, которое сильнее настройки частоты: человек,
    выбравший шесть эпизодов в двухчасовом окне, просит невозможного, и
    честнее дать ему меньше эпизодов, чем очередь сообщений подряд.

    `gap` без значения по умолчанию нарочно: молчаливый дефолт однажды оставил
    бы вызов со старыми сорока пятью минутами, и нашлось бы это не здесь, а в
    переписке человека, который просил другое.
    """
    room = int(minutes // (gap.total_seconds() / 60))
    return max(1, min(count, room))


def plan_slots(
    day: dt.date,
    *,
    tz: ZoneInfo,
    start: dt.time,
    end: dt.time,
    count: int,
    gap: dt.timedelta,
    not_before: dt.datetime | None = None,
    rng: random.Random | None = None,
) -> list[dt.datetime]:
    """Моменты эпизодов на день, в UTC и по возрастанию (FR-SCH-2).

    Окно делится на равные доли, эпизод садится в середину своей доли и
    сдвигается на случайные ±(доля/4). Середина, а не начало доли, — чтобы
    первый эпизод не приходил ровно в тот момент, когда окно открылось: это
    самое «системное» время суток из возможных.

    Сдвиг ужимается, если доля узкая: обещанный промежуток важнее размаха
    случайности. Чего сдвиг не делает — не выносит эпизод за пределы своей
    доли, поэтому порядок слотов не перемешивается и окно не нарушается.

    `not_before` отбрасывает слоты, которые уже прошли: планировщик мог
    проснуться позже своего часа — после рестарта, например, — и вываливать
    человеку разом всё, что он проспал, нельзя.
    """
    opens, closes = window_bounds(day, tz=tz, start=start, end=end)
    minutes = (closes - opens).total_seconds() / 60
    if minutes <= 0 or count <= 0:
        # Окно вывернуто наизнанку (конец раньше начала) или эпизодов не просят.
        # Разбираться, что человек имел в виду, — дело экрана настроек.
        return []

    slots_count = fits_in_window(minutes, count, gap=gap)
    share = minutes / slots_count
    spread = max(0.0, min(share / 4, (share - gap.total_seconds() / 60) / 2))
    dice = rng or random.Random()

    slots = []
    for index in range(slots_count):
        offset = share * (index + 0.5) + dice.uniform(-spread, spread)
        slots.append((opens + dt.timedelta(minutes=round(offset))).replace(second=0, microsecond=0))

    if not_before is not None:
        slots = [slot for slot in slots if slot >= not_before]
    return slots


def is_within_window(moment: dt.datetime, *, tz: ZoneInfo, start: dt.time, end: dt.time) -> bool:
    """Попадает ли момент в окно тишины наоборот — в разрешённые часы (US-4).

    Проверяется не при раскладке, а при отправке: между раскладкой и слотом
    человек мог сузить окно, и старое расписание не должно это переживать.
    """
    local = moment.astimezone(tz).timetz().replace(tzinfo=None)
    return start <= local <= end


def is_overdue(fire_at: dt.datetime, now: dt.datetime) -> bool:
    """Опоздали настолько, что отправлять уже неприлично (FR-SCH-4).

    Полтора часа — не техническая величина: разговор, начатый глубоко не в
    своё время, ломает и день человека, и остаток расписания.
    """
    return now - fire_at > MAX_DELAY


def needs_nudge(last_sent_at: dt.datetime, now: dt.datetime) -> bool:
    """Пора ли мягко напомнить о себе (FR-SCH-6)."""
    return now - last_sent_at >= NUDGE_AFTER


#: «10:00-20:00», «9.30 — 21.00» и прочее в этом духе. Точка наравне с
#: двоеточием и любое тире — потому что человек печатает с телефона, а не
#: заполняет форму.
_WINDOW_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[-–—до\s]+\s*(\d{1,2})[:.](\d{2})\s*$")


def parse_window(
    text: str, *, min_span: dt.timedelta = DEFAULT_MIN_GAP
) -> tuple[dt.time, dt.time] | None:
    """Разобрать окно, написанное руками. `None` — не разобралось.

    Окно короче промежутка отвергаем: в него не поместится и один разговор, а
    молча превратить его в «один эпизод в 10:15» значит сделать не то, что
    человек просил, и не сказать об этом. Меркой служит промежуток этого
    человека, а не общая константа: выбрав два часа между разговорами, он тем
    самым объявил часовое окно бессмысленным.
    """
    match = _WINDOW_RE.match(text)
    if match is None:
        return None

    try:
        start = dt.time(int(match[1]), int(match[2]))
        end = dt.time(int(match[3]), int(match[4]))
    except ValueError:
        return None

    if window_minutes(start, end) < min_span.total_seconds() / 60:
        return None
    return start, end


#: «90», «90 минут», «2 часа», «1ч». Единица необязательна — по умолчанию
#: минуты: человек печатает это с телефона, а не заполняет форму.
_GAP_RE = re.compile(r"^\s*(\d{1,4})\s*(ч\w*|h\w*|м\w*|m\w*)?\s*$", re.IGNORECASE)


def parse_gap(text: str) -> int | None:
    """Промежуток в минутах, написанный руками. `None` — не разобралось.

    Выход за границы — тоже `None`, а не подгонка: `clamp_min_gap` существует
    для значений, уже лежащих в базе, а набранному руками числу честнее
    ответить «столько не могу», чем тихо записать другое.
    """
    match = _GAP_RE.match(text)
    if match is None:
        return None

    unit = (match[2] or "").lower()
    minutes = int(match[1]) * (60 if unit.startswith(("ч", "h")) else 1)
    if not MIN_GAP_MINUTES <= minutes <= MAX_GAP_MINUTES:
        return None
    return minutes


def parse_episodes_per_day(text: str) -> int | None:
    """Число разговоров в день, написанное руками. `None` — не подходит."""
    stripped = text.strip()
    if not stripped.isdecimal():
        return None
    count = int(stripped)
    if not MIN_EPISODES_PER_DAY <= count <= MAX_EPISODES_PER_DAY:
        return None
    return count


def clamp_episodes_per_day(count: int) -> int:
    """Частота в допустимых границах, FR-SCH-1."""
    return max(MIN_EPISODES_PER_DAY, min(MAX_EPISODES_PER_DAY, count))


def clamp_min_gap(minutes: int) -> int:
    """Промежуток в допустимых границах, FR-SCH-8.

    Нужен для значения из базы: границы кода и базы могут разъехаться при
    откате миграции, а планировщик из-за этого падать не должен.
    """
    return max(MIN_GAP_MINUTES, min(MAX_GAP_MINUTES, minutes))


def gap_of(minutes: int) -> dt.timedelta:
    """Промежуток пользователя как `timedelta` — с проверкой границ."""
    return dt.timedelta(minutes=clamp_min_gap(minutes))


def local_day(now: dt.datetime, *, tz: ZoneInfo) -> dt.date:
    """Какое сегодня число у человека. Не то же, что дата в UTC."""
    return now.astimezone(tz).date()


def is_planning_time(now: dt.datetime, *, tz: ZoneInfo) -> bool:
    """Настал ли у человека час ночного пересчёта (FR-SCH-2).

    Час, а не минута: воркер просыпается не каждую секунду, и попасть точно в
    03:00 он не обязан. Второй раз за тот же день план не соберётся — за этим
    следит проверка «на сегодня уже есть», а не точность попадания.
    """
    return now.astimezone(tz).hour == PLANNING_HOUR


def paused_days(paused_at: dt.datetime, now: dt.datetime) -> int:
    """Сколько дней простоя вернуть периоду при снятии паузы (FR-SCH-7).

    Округляем вверх: пауза на полтора дня — это два дня, в которые человек не
    занимался, и отдавать ему один было бы мелочной арифметикой не в его
    пользу. Пауза короче суток стоит одного дня, а не нуля, по той же причине.
    """
    elapsed = now - paused_at
    if elapsed <= dt.timedelta(0):
        return 0
    return -(-elapsed // dt.timedelta(days=1))


__all__ = [
    "DEFAULT_MIN_GAP",
    "MAX_DELAY",
    "MAX_EPISODES_PER_DAY",
    "MAX_GAP_MINUTES",
    "MIN_EPISODES_PER_DAY",
    "MIN_GAP_MINUTES",
    "NUDGE_AFTER",
    "PLANNING_HOUR",
    "clamp_episodes_per_day",
    "clamp_min_gap",
    "fits_in_window",
    "gap_of",
    "is_overdue",
    "is_planning_time",
    "is_within_window",
    "local_day",
    "needs_nudge",
    "parse_episodes_per_day",
    "parse_gap",
    "parse_window",
    "paused_days",
    "plan_slots",
    "window_bounds",
    "window_minutes",
    "zone",
]
