"""Сценарий эпизода: начать разговор, провести ход, закрыть.

Здесь сходятся три слоя: правила из `core`, база из `db` и модель из `llm`.
Хендлеру остаётся Telegram — прочитать сообщение, показать «печатает», вывести
текст. Тем же сценарием пользуются воркеры (§9): расписание открывает эпизоды
без всякого чата, и логика открытия у них та же самая.

Про aiogram этот модуль не знает ничего.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from siga.core import episodes as rules
from siga.core import memory as core_memory
from siga.core import schedule as clock
from siga.core import srs
from siga.core.enums import EpisodeIntent, EpisodeStatus, EventStatus, EventType, Level
from siga.db import episodes as episodes_db
from siga.db import memory as memory_db
from siga.db import packs as packs_db
from siga.db import progress as progress_db
from siga.db import schedule as schedule_db
from siga.db.models import Episode, ScheduledEvent, User, Word
from siga.llm.base import LlmBadOutput, LlmClient
from siga.llm.dialogue import WordBrief
from siga.llm.frames import EpisodeRequest, make_frames
from siga.llm.turn import Exchange, TurnContext, TurnResponse, make_turn
from siga.persona import Persona
from siga.persona import load as load_persona

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Started:
    """Открытый эпизод и первая реплика Ника."""

    episode: Episode
    opening: str


@dataclass(frozen=True, slots=True)
class Replied:
    """Итог одного хода."""

    reply_text: str
    analysis_ru: str
    closed: bool = False
    outcomes: dict[int, srs.Outcome] = field(default_factory=dict)
    """Итоги по словам — только у закрывающего хода."""
    turn_idx: int | None = None
    """Номер хода, в котором лежит эта реплика, — адрес кнопки `🇷🇺 Перевод`.

    `None` у закрывающего хода: прощание строкой в `turns` не становится, и
    переводить его пока нечем. Ноль по умолчанию был бы хуже, чем `None`:
    кнопка молча указала бы на вводную реплику."""


class NoWordsToPractise(Exception):
    """Разговор начинать не о чем: нет активной пачки или пул пуст.

    Отдельное исключение, а не `None`: у отказа две разные причины, и человеку
    надо сказать разное — «загрузи пачку» или «на сегодня всё».
    """

    def __init__(self, *, has_pack: bool) -> None:
        super().__init__("нет слов для эпизода")
        self.has_pack = has_pack


def _brief(word: Word, box: int) -> WordBrief:
    return WordBrief(
        word_id=word.id,
        lemma=word.lemma_accented,
        translation_ru=word.translation_ru,
        box=box,
    )


async def start(
    session: AsyncSession,
    llm: LlmClient,
    *,
    user: User,
    now: dt.datetime,
    persona: Persona | None = None,
) -> Started:
    """Отобрать слова, придумать рамку и открыть эпизод.

    Рамка спрашивается здесь и сейчас, хотя по FR-EP-4 её готовит ночной `R3`
    (см. оговорку к этапу 2 в §12). Когда появится расписание, изменится
    только место вызова: `create` и `open_` разнесены как раз затем, чтобы
    рамка могла полежать в базе до своего часа.
    """
    pack = await packs_db.get_active(session, user_id=user.id)
    if pack is None:
        raise NoWordsToPractise(has_pack=False)

    picked = await progress_db.pick(session, pack_id=pack.id, now=now, limit=rules.MAX_TARGET_WORDS)
    if not picked:
        raise NoWordsToPractise(has_pack=True)

    words = [_brief(word, progress.box) for word, progress in picked]
    intent = rules.intent_for(word.box for word in words)
    profile = await memory_db.load(session, user_id=user.id)

    result = await make_frames(
        llm,
        persona=persona or load_persona(),
        level=Level(user.level),
        profile=profile,
        requests=[EpisodeRequest(intent=intent, words=words)],
    )
    frame = result.frames[0]
    if not frame.opening.strip():
        # Эпизод без первой реплики отправить нечем. Это отказ модели, и
        # честнее извиниться, чем открыть разговор пустым сообщением.
        raise LlmBadOutput("R3 не вернул первую реплику")

    episode = await episodes_db.create(
        session,
        user_id=user.id,
        pack_id=pack.id,
        intent=intent,
        scene=frame.scene or None,
        frame=frame.as_frame_json(),
        opening_text=frame.opening.strip(),
        target_word_ids=[word.word_id for word in words],
    )
    await episodes_db.open_(session, episode=episode, now=now)
    log.info("эпизод %s открыт: %s, слов %s", episode.id, intent.value, len(words))
    return Started(episode=episode, opening=episode.opening_text or "")


@dataclass(frozen=True, slots=True)
class DayPlan:
    """Что получилось расписать человеку на день."""

    episodes: list[Episode] = field(default_factory=list)
    events: list[ScheduledEvent] = field(default_factory=list)


async def plan_day(
    session: AsyncSession,
    llm: LlmClient,
    *,
    user: User,
    day: dt.date,
    now: dt.datetime,
    persona: Persona | None = None,
    rng: random.Random | None = None,
) -> DayPlan:
    """Собрать день целиком: слова, рамки и моменты отправки (FR-SCH-2).

    Один вызов `R3` на весь день, а не по вызову на эпизод: рамки — это самая
    дорогая часть суток, а модель всё равно получает один и тот же статический
    блок и один и тот же профиль. Четыре запроса вместо одного отличались бы
    только тем, что за общий контекст мы заплатили бы четырежды.

    Порядок шагов не случаен: сначала слова, потом моменты. Иначе пришлось бы
    выкидывать уже расставленные слоты, когда слов на все не хватило, — а
    выкидывать удобнее последние, отчего разговоры сползли бы в утро. Так же
    поступаем с рамкой без первой реплики: слот просто остаётся пустым, потому
    что молчание в 15:20 человек не заметит, а пустое сообщение — заметит.

    Ничего не делает, если человек на паузе или пачки нет: расписание — это
    про активную пачку, а пауза по FR-SCH-7 останавливает именно расписание.
    """
    if user.paused_at is not None:
        return DayPlan()

    pack = await packs_db.get_active(session, user_id=user.id)
    if pack is None:
        return DayPlan()

    tz = clock.zone(user.tz)
    _, closes = clock.window_bounds(day, tz=tz, start=user.window_start, end=user.window_end)
    groups = await progress_db.pick_groups(
        session,
        pack_id=pack.id,
        now=now,
        until=closes,
        groups=clock.clamp_episodes_per_day(user.episodes_per_day),
        size=rules.MAX_TARGET_WORDS,
    )
    if not groups:
        log.info("пользователю %s на %s нечего повторять", user.id, day)
        return DayPlan()

    slots = clock.plan_slots(
        day,
        tz=tz,
        start=user.window_start,
        end=user.window_end,
        count=len(groups),
        gap=clock.gap_of(user.min_gap_minutes),
        not_before=now,
        rng=rng,
    )
    if not slots:
        return DayPlan()

    briefs = [[_brief(word, progress.box) for word, progress in group] for group in groups]
    profile = await memory_db.load(session, user_id=user.id)
    result = await make_frames(
        llm,
        persona=persona or load_persona(),
        level=Level(user.level),
        profile=profile,
        requests=[
            EpisodeRequest(intent=rules.intent_for(word.box for word in group), words=group)
            for group in briefs
        ],
    )

    episodes: list[Episode] = []
    planned: list[schedule_db.Planned] = []
    for slot, group, frame in zip(slots, briefs, result.frames, strict=False):
        if not frame.opening.strip():
            log.warning("R3 не дал первую реплику, слот %s пропущен", slot.isoformat())
            continue
        episode = await episodes_db.create(
            session,
            user_id=user.id,
            pack_id=pack.id,
            intent=rules.intent_for(word.box for word in group),
            scene=frame.scene or None,
            frame=frame.as_frame_json(),
            opening_text=frame.opening.strip(),
            target_word_ids=[word.word_id for word in group],
        )
        episodes.append(episode)
        planned.append(
            schedule_db.Planned(
                fire_at=slot, type=EventType.EPISODE, payload={"episode_id": episode.id}
            )
        )

    events = await schedule_db.plan(session, user_id=user.id, events=planned)
    log.info("пользователю %s на %s расписано %s эпизодов", user.id, day, len(events))
    return DayPlan(episodes=episodes, events=events)


@dataclass(frozen=True, slots=True)
class Delivery:
    """Решение по одному событию расписания: что отправлять и чем это кончится.

    Отправка разнесена на «решить» и «записать» нарочно. Между ними стоит
    Telegram, который может ответить ошибкой или не ответить вовсе, — и всё,
    что мы пишем в базу, должно происходить уже после него, одной транзакцией
    (NFR-1).
    """

    status: EventStatus
    text: str = ""
    """Что отправить. Пусто у всего, кроме `SENT`."""
    episode: Episode | None = None
    """Эпизод, судьба которого решается вместе с событием."""
    nudge_at: dt.datetime | None = None


async def prepare(
    session: AsyncSession,
    *,
    event: ScheduledEvent,
    user: User,
    now: dt.datetime,
    persona: Persona | None = None,
    rng: random.Random | None = None,
) -> Delivery:
    """Решить, что делать с созревшим событием. В базу ничего не пишет.

    Проверки повторяются здесь, хотя раскладка их уже делала: между 03:00 и
    слотом человек мог сузить окно, встать на паузу или сам начать разговор
    через `/next`. Расписание — это намерение, а не обещание.
    """
    if user.paused_at is not None:
        return Delivery(EventStatus.SKIPPED, episode=await _episode_of(session, event))

    if clock.is_overdue(event.fire_at, now):
        # FR-SCH-4: полтора часа опоздания — и разговор уже не про этот день.
        return Delivery(EventStatus.EXPIRED, episode=await _episode_of(session, event))

    tz = clock.zone(user.tz)
    if not clock.is_within_window(now, tz=tz, start=user.window_start, end=user.window_end):
        return Delivery(EventStatus.SKIPPED, episode=await _episode_of(session, event))

    if event.type == EventType.NUDGE:
        return await _prepare_nudge(session, event=event, now=now, persona=persona, rng=rng)
    return await _prepare_episode(session, event=event, user=user, now=now)


async def _prepare_episode(
    session: AsyncSession, *, event: ScheduledEvent, user: User, now: dt.datetime
) -> Delivery:
    episode = await _episode_of(session, event)
    if episode is None or episode.status != EpisodeStatus.PENDING:
        # Эпизод уже открыли, закрыли или не завели вовсе. Второй раз не шлём.
        return Delivery(EventStatus.SKIPPED)

    if await episodes_db.get_open(session, user_id=user.id) is not None:
        # FR-SCH-5: разговор уже идёт. Начинать поверх него второй — это не
        # «больше практики», а два собеседника в одном окне.
        return Delivery(EventStatus.SKIPPED, episode=episode)

    text = (episode.opening_text or "").strip()
    if not text:
        return Delivery(EventStatus.SKIPPED, episode=episode)

    return Delivery(
        EventStatus.SENT,
        text=text,
        episode=episode,
        nudge_at=now + clock.NUDGE_AFTER,
    )


async def _prepare_nudge(
    session: AsyncSession,
    *,
    event: ScheduledEvent,
    now: dt.datetime,
    persona: Persona | None,
    rng: random.Random | None,
) -> Delivery:
    """Пинок при молчании (FR-SCH-6). Модель для одной фразы не зовём.

    Считаем не от начала эпизода, а от последней реплики Ника: человек мог
    ответить, получить продолжение и замолчать уже на нём. Если он ответил
    только что — напоминать не о чем, разговор идёт.
    """
    episode = await _episode_of(session, event)
    if episode is None or episode.status != EpisodeStatus.OPEN:
        return Delivery(EventStatus.SKIPPED)

    turn = await episodes_db.last_turn(session, episode_id=episode.id)
    if turn is None or turn.user_text is not None:
        return Delivery(EventStatus.SKIPPED)
    if not clock.needs_nudge(turn.sent_at, now):
        return Delivery(EventStatus.SKIPPED)

    # В ходы пинок не пишется: это не реплика разговора, а стук в дверь. Иначе
    # он съел бы один из четырёх ходов (§5.5) и уехал бы в историю промпта.
    return Delivery(EventStatus.SENT, text=(persona or load_persona()).nudge(rng))


async def settle(
    session: AsyncSession, *, event: ScheduledEvent, delivery: Delivery, now: dt.datetime
) -> None:
    """Записать исход события — одной транзакцией с открытием эпизода (NFR-1).

    Порядок в отправщике такой: отправить в Telegram, потом сюда. Дубль в
    худшем случае — одно повторное сообщение, если процесс умрёт ровно между
    этими двумя шагами; обратный порядок терял бы слот целиком и молча.
    """
    schedule_db.mark(event, status=delivery.status, now=now)

    episode = delivery.episode
    if episode is not None and delivery.status == EventStatus.SENT:
        await episodes_db.open_(session, episode=episode, now=now, commit=False)
        if delivery.nudge_at is not None:
            await schedule_db.plan(
                session,
                user_id=event.user_id,
                events=[
                    schedule_db.Planned(
                        fire_at=delivery.nudge_at,
                        type=EventType.NUDGE,
                        payload={"episode_id": episode.id},
                    )
                ],
                commit=False,
            )
    elif episode is not None and episode.status == EpisodeStatus.PENDING:
        # Отправка не состоялась — эпизод так и не начался. Слова возвращаем в
        # пул без штрафа: человек тут ни при чём (FR-SCH-4).
        episode.status = EpisodeStatus.ABANDONED
        episode.closed_at = now
        await progress_db.release(session, word_ids=episode.target_word_ids, now=now)

    await session.commit()


async def clear_day(session: AsyncSession, *, user_id: int, now: dt.datetime) -> int:
    """Снять остаток расписания: пауза и смена настроек (FR-SCH-7).

    Событие не удаляется, а помечается пропущенным: по истории потом видно,
    что день был расписан и почему не состоялся, — иначе на жалобу «он мне
    сегодня не написал» ответить нечем.

    Заодно распускаются эпизоды, заведённые под эти события: слова в них уже
    отмечены занятыми, и без этого шага они провисели бы бронью до ночи, хотя
    разговора не будет.
    """
    events = await schedule_db.pending_at(session, user_id=user_id, since=now)
    for event in events:
        schedule_db.mark(event, status=EventStatus.SKIPPED, now=now)
        episode = await _episode_of(session, event)
        if episode is not None and episode.status == EpisodeStatus.PENDING:
            episode.status = EpisodeStatus.ABANDONED
            episode.closed_at = now
            await progress_db.release(session, word_ids=episode.target_word_ids, now=now)

    if events:
        await session.commit()
    return len(events)


async def _episode_of(session: AsyncSession, event: ScheduledEvent) -> Episode | None:
    """Эпизод, на который ссылается событие, если он у события есть."""
    episode_id = schedule_db.episode_id_of(event)
    if episode_id is None:
        return None
    return await session.get(Episode, episode_id)


async def _words_of(session: AsyncSession, episode: Episode) -> list[WordBrief]:
    """Целевые слова эпизода с их текущим боксом."""
    briefs: list[WordBrief] = []
    for word_id in episode.target_word_ids:
        word = await session.get(Word, word_id)
        if word is None:
            # Слово удалили вместе с пачкой посреди разговора. Редко, но
            # уронить из-за этого ответ человека было бы совсем нелепо.
            continue
        row = await progress_db.get(session, word_id=word_id)
        briefs.append(_brief(word, row.box if row else 0))
    return briefs


async def words_to_practise(session: AsyncSession, episode: Episode) -> list[WordBrief]:
    """Целевые слова, которые в этом разговоре ещё не прозвучали (FR-EP-7).

    Нужны подсказке: предлагать сказать то, что уже сказано, значит гонять
    человека по кругу и тратить ход впустую.

    Прозвучало всё — отдаём все целевые, а не пустой список: эпизод в этот
    момент уже закрывается, но кнопку могли нажать и раньше, а подсказка без
    слов бессмысленна.
    """
    assessed = await episodes_db.collect_verdicts(session, episode_id=episode.id)
    pending = set(rules.unused_words(episode.target_word_ids, assessed))
    words = await _words_of(session, episode)
    return [word for word in words if word.word_id in pending] or words


async def answer(
    session: AsyncSession,
    llm: LlmClient,
    *,
    episode: Episode,
    user: User,
    text: str,
    now: dt.datetime,
    persona: Persona | None = None,
    latency_ms: int | None = None,
) -> Replied:
    """Провести один ход: ответ человека → реплика Ника и оценки.

    Ход записывается в базу до того, как решается судьба эпизода: даже если
    закрытие потом упадёт, ответ человека и его оценка останутся.

    `latency_ms` можно не передавать: время вызова модели меряется здесь. Оно
    и есть та пауза, которую человек видит как «печатает», — всё остальное в
    ходе укладывается в миллисекунды.
    """
    turns = await episodes_db.list_turns(session, episode_id=episode.id)
    current = turns[-1] if turns else None
    if current is None:
        raise LlmBadOutput(f"у эпизода {episode.id} нет ни одного хода")

    profile = await memory_db.load(session, user_id=user.id)
    words = await _words_of(session, episode)
    turns_left = rules.MAX_TURNS - len(turns)

    started_at = time.monotonic()
    result = await make_turn(
        llm,
        persona=persona or load_persona(),
        context=TurnContext(
            level=Level(user.level),
            intent=EpisodeIntent(episode.intent),
            frame=episode.frame,
            words=words,
            history=[Exchange(turn.bot_text, turn.user_text) for turn in turns],
            profile=profile,
            user_text=text,
            turns_left=max(turns_left, 1),
            now=now,
        ),
    )
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    reply = result.answer

    await episodes_db.record_answer(
        session,
        turn=current,
        user_text=text,
        assessments=[item.as_json() for item in reply.word_assessments],
        corrections=reply.corrections(),
        now=now,
        analysis_ru=reply.analysis_ru,
        latency_ms=latency_ms if latency_ms is not None else elapsed_ms,
    )
    await _remember(session, user_id=user.id, reply=reply, now=now)

    verdicts = await episodes_db.collect_verdicts(session, episode_id=episode.id)
    closing = rules.should_close(
        turns_done=len(turns),
        target_word_ids=episode.target_word_ids,
        assessed=verdicts,
        model_wants_close=reply.wants_close,
    )

    if not closing:
        turn = await episodes_db.add_turn(
            session, episode=episode, bot_text=reply.reply_text, now=now
        )
        # Номер берём у самой строки, а не у `episode.turns_count`: совпадают
        # они только пока сессия не сбрасывает объекты на коммите, а кнопка
        # перевода промахнётся молча — покажет перевод чужой реплики.
        return Replied(
            reply_text=reply.reply_text, analysis_ru=reply.analysis_ru, turn_idx=turn.idx
        )

    # `summary_ru` не заполняем разбором последнего хода: блок «Как прошло»
    # (FR-EP-8) собирается из `outcomes`, а объяснения по ходам лежат каждое
    # в своём ходе.
    outcomes = await episodes_db.close(session, episode=episode, now=now)
    return Replied(
        reply_text=reply.reply_text,
        analysis_ru=reply.analysis_ru,
        closed=True,
        outcomes=dict(outcomes),
    )


async def close_by_request(
    session: AsyncSession, *, episode: Episode, user_id: int, now: dt.datetime
) -> dict[int, srs.Outcome]:
    """Человек сам сказал «хватит» (FR-EP-6). Модель для этого не нужна."""
    outcomes = await episodes_db.close(session, episode=episode, now=now)
    await memory_db.touch(session, user_id=user_id, now=now)
    return dict(outcomes)


async def _remember(
    session: AsyncSession, *, user_id: int, reply: TurnResponse, now: dt.datetime
) -> None:
    """Дописать в профиль то, что модель узнала на этом ходу (FR-MEM-1).

    Спецификация говорит обновлять память при закрытии эпизода — имея в виду,
    что отдельного вызова к модели на это не будет. Его и нет: заметки
    приезжают вместе с репликой. А пишем их сразу, потому что эпизод может
    оборваться по TTL, и терять из-за этого сказанное человеком незачем.
    """
    updates = reply.memory_updates
    profile = await memory_db.load(session, user_id=user_id)
    if not updates.is_empty():
        profile = core_memory.merge(
            profile,
            facts=updates.facts,
            recurring_errors=updates.recurring_errors,
            recent_topics=updates.recent_topics,
            tone_notes=updates.tone_notes,
        )
    await memory_db.save(session, user_id=user_id, profile=profile, now=now)


__all__ = [
    "DayPlan",
    "Delivery",
    "NoWordsToPractise",
    "Replied",
    "Started",
    "answer",
    "clear_day",
    "close_by_request",
    "plan_day",
    "prepare",
    "settle",
    "start",
    "words_to_practise",
]
