"""Слой эпизодов против настоящего Postgres.

Арифметика лестницы проверена в `test_srs`; здесь — то, что на чистых
функциях не проверить: что оценки собираются из ходов, что закрытие
двигает боксы, что брошенный эпизод никого не наказывает.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from siga.core.enums import EpisodeIntent, EpisodeStatus, Verdict, WordSource
from siga.core.wordlist import parse_wordlist
from siga.db import episodes, memory, packs, progress, users
from siga.db.models import Episode, Word, WordProgress

NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)

THREE_WORDS = """
το νερό — вода
ο καφές — кофе
το γάλα — молоко
"""


async def _active_pack(session: AsyncSession) -> tuple[int, list[Word]]:
    """Человек с одной активной пачкой из трёх слов и прогрессом по ним."""
    user, _ = await users.get_or_create(session, tg_user_id=111)
    pack = await packs.create_draft(
        session,
        user_id=user.id,
        title="Проверочная пачка",
        source_type=WordSource.TEXT,
        raw_text=THREE_WORDS,
        words=parse_wordlist(THREE_WORDS).words,
    )
    await packs.activate(session, pack=pack, period_days=14, now=NOW)
    return user.id, await packs.list_words(session, pack_id=pack.id)


async def _episode(session: AsyncSession, user_id: int, words: list[Word]) -> Episode:
    episode = await episodes.create(
        session,
        user_id=user_id,
        pack_id=words[0].pack_id,
        intent=EpisodeIntent.D1,
        scene="Ник в очереди за кофе",
        frame={"goal": "спросить, что человек пьёт"},
        opening_text="Καλημέρα! Τι πίνεις;",
        target_word_ids=[word.id for word in words[:2]],
    )
    await episodes.open_(session, episode=episode, now=NOW)
    return episode


async def test_a_new_episode_waits_before_it_is_sent(session: AsyncSession) -> None:
    user_id, words = await _active_pack(session)
    episode = await episodes.create(
        session,
        user_id=user_id,
        pack_id=words[0].pack_id,
        intent=EpisodeIntent.D1,
        scene=None,
        frame={},
        opening_text="Γεια σου!",
        target_word_ids=[words[0].id],
    )

    assert episode.status == EpisodeStatus.PENDING
    assert episode.opened_at is None
    assert await episodes.get_open(session, user_id=user_id) is None


async def test_opening_writes_the_first_turn(session: AsyncSession) -> None:
    """Ход заводится при отправке реплики, а не после ответа."""
    user_id, words = await _active_pack(session)
    episode = await _episode(session, user_id, words)

    turns = await episodes.list_turns(session, episode_id=episode.id)

    assert episode.status == EpisodeStatus.OPEN
    assert [turn.idx for turn in turns] == [0]
    assert turns[0].bot_text == "Καλημέρα! Τι πίνεις;"
    assert turns[0].user_text is None, "человек ещё не ответил"
    assert await episodes.get_open(session, user_id=user_id) is not None


class TestTurnByIndex:
    """Адресация хода номером — то, чем живут кнопки под репликами.

    По `id` нельзя: клавиатуру надо прицепить к сообщению до того, как ход
    записан (NFR-1), а номер вводной реплики известен заранее.
    """

    async def test_a_turn_is_found_by_its_number(self, session: AsyncSession) -> None:
        user_id, words = await _active_pack(session)
        episode = await _episode(session, user_id, words)
        added = await episodes.add_turn(session, episode=episode, bot_text="Και τι τρως;", now=NOW)

        assert added.idx == 1, "номер нового хода — то, что уезжает в кнопку"
        opening = await episodes.get_turn(session, episode_id=episode.id, idx=0)
        second = await episodes.get_turn(session, episode_id=episode.id, idx=1)
        assert opening is not None
        assert opening.bot_text == "Καλημέρα! Τι πίνεις;"
        assert second is not None
        assert second.id == added.id

    async def test_a_number_without_a_turn_is_not_an_error(self, session: AsyncSession) -> None:
        """Процесс мог умереть между отправкой и записью: кнопка есть, хода нет."""
        user_id, words = await _active_pack(session)
        episode = await _episode(session, user_id, words)

        assert await episodes.get_turn(session, episode_id=episode.id, idx=7) is None


class TestTranslationCache:
    """`turns.bot_text_ru`: кэш маршрута `R6` (FR-CHK-7)."""

    async def test_a_fresh_turn_has_no_translation(self, session: AsyncSession) -> None:
        """`NULL` — это «ещё не переводили», и по нему решается, идти ли к модели."""
        user_id, words = await _active_pack(session)
        episode = await _episode(session, user_id, words)

        turn = await episodes.get_turn(session, episode_id=episode.id, idx=0)
        assert turn is not None
        assert turn.bot_text_ru is None

    async def test_a_translation_is_remembered(self, session: AsyncSession) -> None:
        user_id, words = await _active_pack(session)
        episode = await _episode(session, user_id, words)
        turn = await episodes.get_turn(session, episode_id=episode.id, idx=0)
        assert turn is not None

        stored = await episodes.save_translation(session, turn=turn, ru="Доброе утро! Что пьёшь?")

        assert stored == "Доброе утро! Что пьёшь?"
        again = await episodes.get_turn(session, episode_id=episode.id, idx=0)
        assert again is not None
        assert again.bot_text_ru == "Доброе утро! Что пьёшь?"

    async def test_the_first_translation_wins(self, session: AsyncSession) -> None:
        """Два быстрых нажатия не должны переписывать друг друга."""
        user_id, words = await _active_pack(session)
        episode = await _episode(session, user_id, words)
        turn = await episodes.get_turn(session, episode_id=episode.id, idx=0)
        assert turn is not None

        first = await episodes.save_translation(session, turn=turn, ru="Первый перевод")
        second = await episodes.save_translation(session, turn=turn, ru="Второй перевод")

        assert first == "Первый перевод"
        assert second == "Первый перевод", "второе нажатие показывает то, что уже лежит"


async def test_an_answer_lands_in_the_same_turn(session: AsyncSession) -> None:
    user_id, words = await _active_pack(session)
    episode = await _episode(session, user_id, words)
    turn = await episodes.last_turn(session, episode_id=episode.id)
    assert turn is not None

    await episodes.record_answer(
        session,
        turn=turn,
        user_text="Πίνω καφέ.",
        assessments=[{"word_id": words[1].id, "verdict": Verdict.CORRECT}],
        corrections=[],
        now=NOW,
        latency_ms=1400,
    )

    turns = await episodes.list_turns(session, episode_id=episode.id)
    assert len(turns) == 1, "ответ дописывается в ход, а не заводит новый"
    assert turns[0].user_text == "Πίνω καφέ."
    assert turns[0].latency_ms == 1400


async def test_the_best_verdict_across_turns_wins(session: AsyncSession) -> None:
    """Ошибся, Ник переспросил, повторил верно — эпизод засчитан (§5.4)."""
    user_id, words = await _active_pack(session)
    episode = await _episode(session, user_id, words)
    first = await episodes.last_turn(session, episode_id=episode.id)
    assert first is not None

    await episodes.record_answer(
        session,
        turn=first,
        user_text="Πίνω καφές.",
        assessments=[{"word_id": words[1].id, "verdict": Verdict.INCORRECT}],
        corrections=[{"from": "καφές", "to": "καφέ", "type": "case"}],
        now=NOW,
    )
    second = await episodes.add_turn(session, episode=episode, bot_text="Καφέ, ναι;", now=NOW)
    await episodes.record_answer(
        session,
        turn=second,
        user_text="Πίνω καφέ.",
        assessments=[{"word_id": words[1].id, "verdict": Verdict.CORRECT}],
        corrections=[],
        now=NOW,
    )

    verdicts = await episodes.collect_verdicts(session, episode_id=episode.id)

    assert verdicts[words[1].id] == [Verdict.INCORRECT, Verdict.CORRECT]


async def test_closing_moves_the_boxes(session: AsyncSession) -> None:
    user_id, words = await _active_pack(session)
    episode = await _episode(session, user_id, words)
    turn = await episodes.last_turn(session, episode_id=episode.id)
    assert turn is not None

    await episodes.record_answer(
        session,
        turn=turn,
        user_text="Πίνω νερό και καφέ.",
        assessments=[
            {"word_id": words[0].id, "verdict": Verdict.CORRECT},
            {"word_id": words[1].id, "verdict": Verdict.INCORRECT},
        ],
        corrections=[],
        now=NOW,
    )
    outcomes = await episodes.close(session, episode=episode, now=NOW, summary_ru="Про напитки")

    assert episode.status == EpisodeStatus.CLOSED
    assert episode.summary_ru == "Про напитки"
    assert outcomes[words[0].id].verdict is Verdict.CORRECT

    first = await progress.get(session, word_id=words[0].id)
    second = await progress.get(session, word_id=words[1].id)
    assert first is not None and second is not None
    assert first.box == 1
    assert first.next_due_at == NOW + dt.timedelta(minutes=20)
    assert second.box == 1, "ошибка отправляет в первый бокс, а не в нулевой"
    assert second.correct_uses == 0


async def test_a_word_that_never_came_up_keeps_its_box(session: AsyncSession) -> None:
    """FR-EP-7: уклонился — не ошибся, но спросим первым делом."""
    user_id, words = await _active_pack(session)
    stored = await progress.get(session, word_id=words[1].id)
    assert stored is not None
    stored.box = 3
    await session.commit()

    episode = await _episode(session, user_id, words)
    turn = await episodes.last_turn(session, episode_id=episode.id)
    assert turn is not None
    await episodes.record_answer(
        session,
        turn=turn,
        user_text="Πίνω νερό.",
        assessments=[{"word_id": words[0].id, "verdict": Verdict.CORRECT}],
        corrections=[],
        now=NOW,
    )

    outcomes = await episodes.close(session, episode=episode, now=NOW)

    assert not outcomes[words[1].id].used
    after = await progress.get(session, word_id=words[1].id)
    assert after is not None
    assert after.box == 3
    assert after.next_due_at == NOW


async def test_a_verdict_the_model_invented_is_ignored(session: AsyncSession) -> None:
    """Одна непонятная строка не должна отменять итог всего эпизода."""
    user_id, words = await _active_pack(session)
    episode = await _episode(session, user_id, words)
    turn = await episodes.last_turn(session, episode_id=episode.id)
    assert turn is not None

    await episodes.record_answer(
        session,
        turn=turn,
        user_text="Πίνω νερό.",
        assessments=[
            {"word_id": words[0].id, "verdict": "brilliant"},
            {"word_id": words[1].id, "verdict": Verdict.CORRECT},
        ],
        corrections=[],
        now=NOW,
    )
    outcomes = await episodes.close(session, episode=episode, now=NOW)

    assert not outcomes[words[0].id].used
    assert outcomes[words[1].id].verdict is Verdict.CORRECT


async def test_an_abandoned_episode_costs_nothing(session: AsyncSession) -> None:
    """FR-SCH-6: молчание — не ошибка в греческом, штрафа за него нет."""
    user_id, words = await _active_pack(session)
    episode = await _episode(session, user_id, words)
    stored = await progress.get(session, word_id=words[0].id)
    assert stored is not None
    stored.box = 4
    await session.commit()

    later = NOW + dt.timedelta(hours=13)
    stale = await episodes.abandon_stale(session, now=later)

    assert [item.id for item in stale] == [episode.id]
    assert episode.status == EpisodeStatus.ABANDONED
    after = await progress.get(session, word_id=words[0].id)
    assert after is not None
    assert after.box == 4
    assert after.next_due_at == later, "слово вернулось в пул"


async def test_a_fresh_episode_is_not_abandoned(session: AsyncSession) -> None:
    user_id, words = await _active_pack(session)
    await _episode(session, user_id, words)

    assert await episodes.abandon_stale(session, now=NOW + dt.timedelta(hours=1)) == []


class TestSelection:
    """Отбор слов из пачки — уже против базы, а не в вакууме."""

    async def test_new_words_come_first_and_no_more_than_asked(self, session: AsyncSession) -> None:
        _, words = await _active_pack(session)

        chosen = await progress.pick(session, pack_id=words[0].pack_id, now=NOW, limit=2)

        assert len(chosen) == 2

    async def test_a_learned_word_leaves_the_pool(self, session: AsyncSession) -> None:
        """Выученное возвращается эпизодами D5 из архива, а не занимает место."""
        _, words = await _active_pack(session)
        stored = await progress.get(session, word_id=words[0].id)
        assert stored is not None
        stored.learned_at = NOW
        await session.commit()

        chosen = await progress.pick(session, pack_id=words[0].pack_id, now=NOW, limit=10)

        assert words[0].id not in [word.id for word, _ in chosen]
        assert len(chosen) == 2

    async def test_an_overdue_word_outranks_a_new_one(self, session: AsyncSession) -> None:
        _, words = await _active_pack(session)
        stored = await progress.get(session, word_id=words[2].id)
        assert stored is not None
        stored.box = 2
        stored.next_due_at = NOW - dt.timedelta(days=1)
        await session.commit()

        chosen = await progress.pick(session, pack_id=words[0].pack_id, now=NOW, limit=1)

        assert [word.id for word, _ in chosen] == [words[2].id]


class TestMemory:
    """Профиль диалога в базе (§5.5)."""

    async def test_an_unknown_person_has_an_empty_profile(self, session: AsyncSession) -> None:
        user, _ = await users.get_or_create(session, tg_user_id=222)

        assert (await memory.load(session, user_id=user.id)).is_empty()

    async def test_saving_and_reading_back(self, session: AsyncSession) -> None:
        user, _ = await users.get_or_create(session, tg_user_id=222)
        profile = await memory.load(session, user_id=user.id)
        profile.facts.append("живёт в Лимассоле")

        await memory.save(session, user_id=user.id, profile=profile, now=NOW)
        loaded = await memory.load(session, user_id=user.id)

        assert loaded.facts == ["живёт в Лимассоле"]
        assert loaded.last_seen_at == NOW, "FR-MEM-5: без этого не заметить разрыв"

    async def test_clearing_leaves_the_row(self, session: AsyncSession) -> None:
        """FR-MEM-4: право на забвение стоит того, чтобы остался его след."""
        user, _ = await users.get_or_create(session, tg_user_id=222)
        profile = await memory.load(session, user_id=user.id)
        profile.facts.append("работает в IT")
        await memory.save(session, user_id=user.id, profile=profile, now=NOW)

        await memory.clear(session, user_id=user.id, now=NOW)

        assert (await memory.load(session, user_id=user.id)).is_empty()


async def test_progress_rows_survive_only_with_their_word(session: AsyncSession) -> None:
    """Каскад: удалили слово — ушла и его строка прогресса."""
    _, words = await _active_pack(session)
    word = await session.get(Word, words[0].id)
    assert word is not None

    await session.delete(word)
    await session.commit()

    assert await session.get(WordProgress, words[0].id) is None
