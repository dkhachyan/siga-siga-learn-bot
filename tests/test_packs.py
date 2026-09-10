"""Слой пачек против настоящего Postgres.

Здесь проверяется то, что на моках не проверить: чужой ключ, каскады,
уникальность и то, что транзакция кладёт связанные строки целиком.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from siga.core.enums import Gender, PackStatus, PartOfSpeech, WordSource
from siga.core.wordlist import parse_wordlist
from siga.db import packs, users
from siga.db.models import Import, Pack, Word, WordProgress
from siga.llm.enrich import EnrichedWord, Example

TEN_WORDS = """
το νερό — вода
ο καφές — кофе
το γάλα — молоко
το ψωμί — хлеб
η ζάχαρη — сахар
ο φούρνος — пекарня
πίνω — пить
τρώω — есть
αγοράζω — покупать
ζεστός — горячий
"""


async def _user(session: AsyncSession, tg_user_id: int = 111) -> int:
    user, _ = await users.get_or_create(session, tg_user_id=tg_user_id)
    return user.id


async def _draft(session: AsyncSession, user_id: int, text: str = TEN_WORDS) -> Pack:
    return await packs.create_draft(
        session,
        user_id=user_id,
        title="Проверочная пачка",
        source_type=WordSource.TEXT,
        raw_text=text,
        words=parse_wordlist(text).words,
    )


async def test_create_draft_writes_import_pack_and_words(session: AsyncSession) -> None:
    user_id = await _user(session)
    pack = await _draft(session, user_id)

    assert pack.status == PackStatus.DRAFT
    assert pack.started_at is None, "период не должен тикать до подтверждения"

    words = await packs.list_words(session, pack_id=pack.id)
    assert len(words) == 10
    assert words[0].lemma_accented == "το νερό"
    assert words[0].lemma == "νερο", "ключ хранится нормализованным"
    assert words[0].translation_ru == "вода"

    record = await session.get(Import, pack.import_id)
    assert record is not None
    assert record.word_count == 10
    assert record.confirmed_at is None
    assert record.raw_text == TEN_WORDS, "сырой текст храним для разбора жалоб"


async def test_second_draft_replaces_the_first(session: AsyncSession) -> None:
    user_id = await _user(session)
    first = await _draft(session, user_id)
    second = await _draft(session, user_id, "ο ήλιος — солнце\nη θάλασσα — море")

    assert second.id != first.id
    current = await packs.get_draft(session, user_id=user_id)
    assert current is not None
    assert current.id == second.id

    orphans = await session.scalar(
        select(func.count()).select_from(Word).where(Word.pack_id == first.id)
    )
    assert orphans == 0, "слова прошлого черновика должны уйти каскадом"


async def test_confirmed_pack_survives_a_new_draft(session: AsyncSession) -> None:
    """Удаление черновиков не должно задевать подтверждённые пачки."""
    user_id = await _user(session)
    pack = await _draft(session, user_id)
    await packs.activate(session, pack=pack, period_days=14, now=dt.datetime.now(dt.UTC))

    await _draft(session, user_id, "ο ήλιος — солнце")

    still_there = await packs.list_words(session, pack_id=pack.id)
    assert len(still_there) == 10


async def test_activate_starts_the_period_and_creates_progress(session: AsyncSession) -> None:
    user_id = await _user(session)
    pack = await _draft(session, user_id)
    now = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)

    await packs.activate(session, pack=pack, period_days=14, now=now)

    assert pack.status == PackStatus.ACTIVE
    assert pack.started_at == now
    assert pack.ends_at == now + dt.timedelta(days=14)

    record = await session.get(Import, pack.import_id)
    assert record is not None
    assert record.confirmed_at == now

    progress = await session.scalars(select(WordProgress))
    rows = list(progress)
    assert len(rows) == 10
    assert all(row.box == 0 and row.user_id == user_id for row in rows)
    assert all(row.next_due_at is None for row in rows), "новое слово и так в пуле"


async def test_known_lemmas_look_past_the_current_draft(session: AsyncSession) -> None:
    """FR-IMP-5: дубль против архива, но черновик не должен находить сам себя."""
    user_id = await _user(session)
    old = await _draft(session, user_id, "το νερό — вода\nο καφές — кофе")
    await packs.activate(session, pack=old, period_days=14, now=dt.datetime.now(dt.UTC))

    fresh = await _draft(session, user_id, "ΝΕΡΟ — водичка\nο ήλιος — солнце")
    words = await packs.list_words(session, pack_id=fresh.id)

    known = await packs.find_known_lemmas(
        session,
        user_id=user_id,
        lemmas=[word.lemma for word in words],
        exclude_pack_id=fresh.id,
    )
    assert known == {"νερο"}


async def test_known_lemmas_do_not_leak_between_users(session: AsyncSession) -> None:
    mine = await _user(session, tg_user_id=111)
    theirs = await _user(session, tg_user_id=222)
    pack = await _draft(session, theirs, "το νερό — вода")
    await packs.activate(session, pack=pack, period_days=14, now=dt.datetime.now(dt.UTC))

    known = await packs.find_known_lemmas(session, user_id=mine, lemmas=["νερο"])
    assert known == set()


async def test_add_word_rejects_a_duplicate_by_key(session: AsyncSession) -> None:
    user_id = await _user(session)
    pack = await _draft(session, user_id)

    added = await packs.add_word(session, pack=pack, lemma="ο ήλιος", translation_ru="солнце")
    assert added is not None
    assert added.source == WordSource.MANUAL

    # То же слово другим начертанием — уже есть в пачке
    assert await packs.add_word(session, pack=pack, lemma="ΝΕΡΟ", translation_ru="вода") is None
    assert len(await packs.list_words(session, pack_id=pack.id)) == 11


async def test_remove_word_only_from_its_own_pack(session: AsyncSession) -> None:
    user_id = await _user(session)
    pack = await _draft(session, user_id)
    other = await _draft(session, await _user(session, tg_user_id=333))
    stranger = (await packs.list_words(session, pack_id=other.id))[0]

    assert await packs.remove_word(session, pack_id=pack.id, word_id=stranger.id) is False
    assert await packs.remove_word(session, pack_id=pack.id, word_id=-1) is False

    mine = (await packs.list_words(session, pack_id=pack.id))[0]
    assert await packs.remove_word(session, pack_id=pack.id, word_id=mine.id) is True
    assert len(await packs.list_words(session, pack_id=pack.id)) == 9


async def test_removing_a_word_from_the_active_pack_takes_its_progress(
    session: AsyncSession,
) -> None:
    """FR-IMP-11: убранное слово не тащит за собой сироту-прогресс."""
    user_id = await _user(session)
    pack = await _draft(session, user_id, "το νερό — вода\nο καφές — кофе")
    await packs.activate(session, pack=pack, period_days=14, now=dt.datetime.now(dt.UTC))
    assert len(list(await session.scalars(select(WordProgress)))) == 2

    word = (await packs.list_words(session, pack_id=pack.id))[0]
    assert await packs.remove_word(session, pack_id=pack.id, word_id=word.id) is True

    assert await session.get(Word, word.id) is None
    assert await session.get(WordProgress, word.id) is None, "прогресс не переживает слово"
    assert len(await packs.list_words(session, pack_id=pack.id)) == 1


async def test_set_translation_replaces_only_the_translation(session: AsyncSession) -> None:
    user_id = await _user(session)
    pack = await _draft(session, user_id)
    word = (await packs.list_words(session, pack_id=pack.id))[0]

    updated = await packs.set_translation(
        session, pack_id=pack.id, word_id=word.id, translation_ru="питьевая вода"
    )
    assert updated is not None
    assert updated.translation_ru == "питьевая вода"
    assert updated.lemma_accented == "το νερό"

    assert (
        await packs.set_translation(session, pack_id=pack.id, word_id=-1, translation_ru="что-то")
        is None
    )


# --- обогащение --------------------------------------------------------------


def _enriched(
    lemma: str = "νερο",
    *,
    accented: str = "το νερό",
    translation: str = "вода",
    pos: str = "noun",
    gender: str | None = "n",
) -> EnrichedWord:
    return EnrichedWord(
        lemma=lemma,
        lemma_accented=accented,
        translation_ru=translation,
        pos=PartOfSpeech(pos),
        article="το" if pos == "noun" else None,
        gender=Gender(gender) if gender else None,
        examples=[Example(el="Θέλω νερό.", ru="Хочу воды.")],
    )


async def test_apply_enrichment_fills_grammar(session: AsyncSession) -> None:
    user_id = await _user(session)
    pack = await _draft(session, user_id)
    now = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)

    updated = await packs.apply_enrichment(
        session, pack_id=pack.id, enriched={"νερο": _enriched()}, now=now
    )

    assert updated == 1, "остальные девять слов модель не вернула"
    word = (await packs.list_words(session, pack_id=pack.id))[0]
    assert word.pos == PartOfSpeech.NOUN
    assert word.gender == Gender.NEUTER
    assert word.article == "το"
    assert word.examples == [{"el": "Θέλω νερό.", "ru": "Хочу воды."}]
    assert word.enriched_at == now


async def test_apply_enrichment_leaves_untouched_words_unenriched(session: AsyncSession) -> None:
    """Пустой `enriched_at` — метка «сюда ещё вернуться», а не поломка."""
    user_id = await _user(session)
    pack = await _draft(session, user_id)

    await packs.apply_enrichment(
        session,
        pack_id=pack.id,
        enriched={"νερο": _enriched()},
        now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
    )

    without = [w for w in await packs.list_words(session, pack_id=pack.id) if w.enriched_at is None]
    assert len(without) == 9


async def test_apply_enrichment_keeps_the_translation_a_human_gave(session: AsyncSession) -> None:
    """FR-IMP-4: перевод человека — его дело. Модельный кладём рядом."""
    user_id = await _user(session)
    pack = await _draft(session, user_id)

    await packs.apply_enrichment(
        session,
        pack_id=pack.id,
        enriched={"νερο": _enriched(translation="водичка")},
        now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
    )

    word = (await packs.list_words(session, pack_id=pack.id))[0]
    assert word.translation_ru == "вода", "как написал человек"
    assert word.translation_model == "водичка"


async def test_apply_enrichment_fills_a_translation_nobody_gave(session: AsyncSession) -> None:
    user_id = await _user(session)
    pack = await _draft(session, user_id, text="το νερό\nο καφές\nτο γάλα")

    await packs.apply_enrichment(
        session,
        pack_id=pack.id,
        enriched={"νερο": _enriched()},
        now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
    )

    word = (await packs.list_words(session, pack_id=pack.id))[0]
    assert word.translation_ru == "вода"


async def test_apply_enrichment_refuses_to_swap_the_word_itself(session: AsyncSession) -> None:
    """Модель ответила про другое слово — показывать его человеку нельзя."""
    user_id = await _user(session)
    pack = await _draft(session, user_id)

    await packs.apply_enrichment(
        session,
        pack_id=pack.id,
        enriched={"νερο": _enriched(accented="το κρασί")},
        now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
    )

    word = (await packs.list_words(session, pack_id=pack.id))[0]
    assert word.lemma_accented == "το νερό"
