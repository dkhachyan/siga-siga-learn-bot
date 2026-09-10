"""Эпизоды и ходы: завести, открыть, дописать ответ, закрыть.

Ход пишется в базу в момент отправки реплики, а не после ответа. Иначе
эпизод, на который человек не ответил, не оставил бы следа — а именно такие
эпизоды и интересно потом смотреть.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from siga.core import srs
from siga.core.enums import EpisodeIntent, EpisodeStatus, Verdict
from siga.core.episodes import EPISODE_TTL
from siga.db import progress as progress_db
from siga.db.models import Episode, Turn


async def get_open(session: AsyncSession, *, user_id: int) -> Episode | None:
    """Эпизод, в котором человек сейчас разговаривает.

    Проверяется на каждом входящем сообщении: свободный текст — это ответ в
    открытом эпизоде (§6), и без этой проверки не отличить его от болтовни.
    """
    episode: Episode | None = await session.scalar(
        select(Episode)
        .where(Episode.user_id == user_id, Episode.status == EpisodeStatus.OPEN)
        .order_by(Episode.opened_at.desc().nullslast(), Episode.id.desc())
    )
    return episode


async def get_last_closed(session: AsyncSession, *, user_id: int) -> Episode | None:
    """Последний законченный разговор — для `🔍 Разбор` после закрытия."""
    episode: Episode | None = await session.scalar(
        select(Episode)
        .where(Episode.user_id == user_id, Episode.status == EpisodeStatus.CLOSED)
        .order_by(Episode.closed_at.desc().nullslast(), Episode.id.desc())
    )
    return episode


async def create(
    session: AsyncSession,
    *,
    user_id: int,
    pack_id: int | None,
    intent: EpisodeIntent,
    scene: str | None,
    frame: dict[str, Any],
    opening_text: str,
    target_word_ids: Sequence[int],
) -> Episode:
    """Завести эпизод с готовой рамкой. Реплика ещё не отправлена."""
    episode = Episode(
        user_id=user_id,
        pack_id=pack_id,
        intent=intent,
        scene=scene,
        frame=frame,
        opening_text=opening_text,
        target_word_ids=list(target_word_ids),
        status=EpisodeStatus.PENDING,
    )
    session.add(episode)
    await session.commit()
    return episode


async def open_(
    session: AsyncSession,
    *,
    episode: Episode,
    now: dt.datetime,
    cost_usd: Decimal | None = None,
    commit: bool = True,
) -> Turn:
    """Отметить, что первая реплика ушла, и записать нулевой ход.

    `commit=False` нужен отправщику: он открывает эпизод и закрывает событие
    расписания одной транзакцией (NFR-1), иначе после падения между двумя
    коммитами эпизод оказался бы открыт, а событие — снова готово к отправке.
    """
    episode.status = EpisodeStatus.OPEN
    episode.opened_at = now
    if cost_usd is not None:
        episode.cost_usd += cost_usd

    turn = Turn(episode_id=episode.id, idx=0, bot_text=episode.opening_text or "", sent_at=now)
    session.add(turn)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return turn


async def list_turns(session: AsyncSession, *, episode_id: int) -> list[Turn]:
    """Ходы эпизода по порядку — история для промпта `R4` и для разбора."""
    rows = await session.scalars(
        select(Turn).where(Turn.episode_id == episode_id).order_by(Turn.idx)
    )
    return list(rows)


async def get_turn(session: AsyncSession, *, episode_id: int, idx: int) -> Turn | None:
    """Ход по номеру внутри эпизода — так адресуются кнопки под репликами.

    Не по `id`, потому что кнопку надо прицепить к сообщению до того, как ход
    записан: отправщик сначала отправляет, и только потом пишет (NFR-1). Номер
    вводной реплики известен заранее — он всегда нулевой, — а `id` нет.
    Пара уникальна (`uq_turns_episode_id_idx`), так что это одна строка.
    """
    turn: Turn | None = await session.scalar(
        select(Turn).where(Turn.episode_id == episode_id, Turn.idx == idx)
    )
    return turn


async def save_translation(session: AsyncSession, *, turn: Turn, ru: str) -> str:
    """Запомнить перевод реплики, если его ещё не запомнил кто-то другой.

    Два быстрых нажатия приходят разными апдейтами и в разных сессиях, и оба
    успевают увидеть пустое поле. Условие в `UPDATE` делает победителем
    первого: перевод у них всё равно одинаковый по смыслу, а вот платить за
    него дважды и переписывать друг друга незачем. Возвращается то, что в
    итоге лежит в базе, — его и показываем.
    """
    result = await session.execute(
        update(Turn)
        .where(Turn.id == turn.id, Turn.bot_text_ru.is_(None))
        .values(bot_text_ru=ru)
        .returning(Turn.bot_text_ru)
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        stored = await session.scalar(select(Turn.bot_text_ru).where(Turn.id == turn.id)) or ru
    await session.commit()
    turn.bot_text_ru = stored
    return stored


async def save_hint(session: AsyncSession, *, turn: Turn, text: str) -> str:
    """Запомнить подсказку к ходу — тем же условным `UPDATE`, что и перевод.

    Довод тот же (см. `save_translation`), но здесь он весомее: подсказку
    нажимают, когда не знают, что сказать, а в этот момент как раз и жмут
    дважды от нетерпения.
    """
    result = await session.execute(
        update(Turn)
        .where(Turn.id == turn.id, Turn.hint_ru.is_(None))
        .values(hint_ru=text)
        .returning(Turn.hint_ru)
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        stored = await session.scalar(select(Turn.hint_ru).where(Turn.id == turn.id)) or text
    await session.commit()
    turn.hint_ru = stored
    return stored


async def last_turn(session: AsyncSession, *, episode_id: int) -> Turn | None:
    turn: Turn | None = await session.scalar(
        select(Turn).where(Turn.episode_id == episode_id).order_by(Turn.idx.desc())
    )
    return turn


async def record_answer(
    session: AsyncSession,
    *,
    turn: Turn,
    user_text: str,
    assessments: Sequence[dict[str, Any]],
    corrections: Sequence[dict[str, Any]],
    now: dt.datetime,
    analysis_ru: str | None = None,
    latency_ms: int | None = None,
    cost_usd: Decimal | None = None,
) -> Turn:
    """Дописать в ход ответ человека и то, как его оценили."""
    turn.user_text = user_text
    turn.assessments = list(assessments)
    turn.corrections = list(corrections)
    turn.analysis_ru = analysis_ru or None
    turn.answered_at = now
    turn.latency_ms = latency_ms
    if cost_usd is not None:
        turn.cost_usd += cost_usd
    await session.commit()
    return turn


async def add_turn(
    session: AsyncSession, *, episode: Episode, bot_text: str, now: dt.datetime
) -> Turn:
    """Следующая реплика Ника — новый ход."""
    episode.turns_count += 1
    turn = Turn(episode_id=episode.id, idx=episode.turns_count, bot_text=bot_text, sent_at=now)
    session.add(turn)
    await session.commit()
    return turn


async def collect_verdicts(session: AsyncSession, *, episode_id: int) -> dict[int, list[Verdict]]:
    """Все оценки эпизода, сгруппированные по словам.

    Считаются из ходов, а не копятся отдельным счётчиком: ходы — то, что
    действительно произошло, и расходиться с ними агрегату незачем.
    """
    verdicts: dict[int, list[Verdict]] = {}
    for turn in await list_turns(session, episode_id=episode_id):
        for item in turn.assessments:
            word_id = item.get("word_id")
            verdict = item.get("verdict")
            if not isinstance(word_id, int) or not isinstance(verdict, str):
                continue
            try:
                verdicts.setdefault(word_id, []).append(Verdict(verdict))
            except ValueError:
                # Модель прислала оценку, которой нет в §5.4. Пропускаем: одна
                # непонятная строка не должна отменять весь итог эпизода.
                continue
    return verdicts


async def close(
    session: AsyncSession,
    *,
    episode: Episode,
    now: dt.datetime,
    summary_ru: str | None = None,
    cost_usd: Decimal | None = None,
) -> Mapping[int, srs.Outcome]:
    """Закрыть эпизод и продвинуть по лестнице каждое целевое слово.

    Возвращает итог по словам — из него собирается сообщение о закрытии
    (FR-EP-8) и разбор. Не прозвучавшее слово получает `used=False`: оно
    вернётся в ближайший отбор, но бокс не потеряет (FR-EP-7).
    """
    verdicts = await collect_verdicts(session, episode_id=episode.id)
    outcomes = {
        word_id: srs.aggregate(verdicts.get(word_id, [])) for word_id in episode.target_word_ids
    }

    await progress_db.apply(session, outcomes=outcomes, now=now)

    episode.status = EpisodeStatus.CLOSED
    episode.closed_at = now
    if summary_ru is not None:
        episode.summary_ru = summary_ru
    if cost_usd is not None:
        episode.cost_usd += cost_usd

    await session.commit()
    return outcomes


async def abandon_stale(
    session: AsyncSession, *, now: dt.datetime, user_id: int | None = None
) -> list[Episode]:
    """Развести по домам эпизоды, до которых человек так и не дошёл (FR-SCH-6).

    Слова возвращаются в пул без штрафа: молчание — это не ошибка в греческом.
    Считаем от `opened_at`, а не от последнего хода: TTL — про то, сколько
    эпизод вообще живёт, иначе вялый разговор мог бы тянуться сутками.

    `user_id` сужает уборку до одного человека — так делает бот, когда тот
    пишет сам. Отправщик подметает всех подряд, и одно другому не мешает:
    операция идемпотентна, а строки берутся разные.
    """
    where = [
        Episode.status == EpisodeStatus.OPEN,
        Episode.opened_at.is_not(None),
        Episode.opened_at < now - EPISODE_TTL,
    ]
    if user_id is not None:
        where.append(Episode.user_id == user_id)

    rows = await session.scalars(select(Episode).where(*where))

    stale = list(rows)
    for episode in stale:
        episode.status = EpisodeStatus.ABANDONED
        episode.closed_at = now
        await progress_db.release(session, word_ids=episode.target_word_ids, now=now)

    if stale:
        await session.commit()
    return stale
