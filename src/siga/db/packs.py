"""Работа с пачками слов: черновик, правки, активация.

Черновик живёт в базе, а не в состоянии диалога. Так экран подтверждения
переживает рестарт бота: человек прислал сорок слов, бот перезапустился —
список на месте, а не «пришлите ещё раз». Плата за это — статус `draft`
у пачки и уборка брошенных черновиков.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from siga.core.enums import PackStatus, WordSource
from siga.core.greek import normalize_lemma
from siga.core.wordlist import ParsedWord
from siga.db.models import Import, Pack, Word, WordProgress
from siga.llm.enrich import EnrichedWord


async def find_known_lemmas(
    session: AsyncSession,
    *,
    user_id: int,
    lemmas: Sequence[str],
    exclude_pack_id: int | None = None,
) -> set[str]:
    """Какие из этих ключей у пользователя уже есть — в любой пачке (FR-IMP-5).

    Возвращает ключи нормализованных лемм, а не сами слова: вызывающему нужно
    лишь понять, про какие строки пометить как «уже изучали».

    `exclude_pack_id` — пачка, которую не считаем «архивом»: слова черновика
    лежат в той же таблице, и без этого он нашёл бы сам себя.
    """
    if not lemmas:
        return set()
    query = select(Word.lemma).where(Word.user_id == user_id, Word.lemma.in_(set(lemmas)))
    if exclude_pack_id is not None:
        query = query.where(Word.pack_id != exclude_pack_id)
    rows = await session.scalars(query)
    return set(rows)


async def get_draft(session: AsyncSession, *, user_id: int) -> Pack | None:
    """Незавершённый импорт пользователя, если он есть."""
    pack: Pack | None = await session.scalar(
        select(Pack)
        .where(Pack.user_id == user_id, Pack.status == PackStatus.DRAFT)
        .order_by(Pack.created_at.desc())
    )
    return pack


async def get_active(session: AsyncSession, *, user_id: int) -> Pack | None:
    """Пачка, по которой сейчас идёт работа. Она у человека одна (§5.8).

    Порядок в запросе оставлен, хотя после миграции 0003 уникальный индекс и
    так не даёт завести вторую: сортировка — это то, что делало выборку
    однозначной до индекса, и повторяется в самой миграции при уборке данных.
    """
    pack: Pack | None = await session.scalar(
        select(Pack)
        .where(Pack.user_id == user_id, Pack.status == PackStatus.ACTIVE)
        .order_by(Pack.started_at.desc().nullslast(), Pack.id.desc())
    )
    return pack


async def archive(session: AsyncSession, *, pack: Pack) -> Pack:
    """Убрать пачку из работы. Слова и прогресс остаются — это и есть архив.

    Удалять нечего: по словам архива работает `/review` (§5.9), а история
    употреблений — половина статистики. Меняется только статус.
    """
    pack.status = PackStatus.ARCHIVED
    await session.commit()
    return pack


async def list_words(session: AsyncSession, *, pack_id: int) -> list[Word]:
    """Слова пачки в том порядке, в котором их прислали."""
    rows = await session.scalars(select(Word).where(Word.pack_id == pack_id).order_by(Word.id))
    return list(rows)


async def lemmas(session: AsyncSession, *, word_ids: Sequence[int]) -> dict[int, str]:
    """Слова по номерам — для итога эпизода и разбора.

    Пропавшее слово (пачку удалили посреди разговора) просто не попадает в
    словарь: показать его нечем, а падать из-за этого не за что.
    """
    if not word_ids:
        return {}
    rows = await session.scalars(select(Word).where(Word.id.in_(set(word_ids))))
    return {word.id: word.lemma_accented for word in rows}


async def create_draft(
    session: AsyncSession,
    *,
    user_id: int,
    title: str,
    source_type: WordSource,
    raw_text: str,
    words: Sequence[ParsedWord],
) -> Pack:
    """Завести черновик пачки со словами и записью об импорте.

    Прошлый черновик того же пользователя удаляется: черновик — это «список,
    который сейчас на экране», и держать их несколько значит спрашивать
    человека, какой из них он имел в виду. Подтверждённые пачки не трогаются.
    """
    await session.execute(
        delete(Pack).where(Pack.user_id == user_id, Pack.status == PackStatus.DRAFT)
    )

    record = Import(
        user_id=user_id,
        source_type=source_type,
        raw_text=raw_text,
        word_count=len(words),
    )
    session.add(record)
    await session.flush()  # нужен import_id

    pack = Pack(user_id=user_id, import_id=record.id, title=title, status=PackStatus.DRAFT)
    session.add(pack)
    await session.flush()  # нужен pack_id

    session.add_all(
        Word(
            pack_id=pack.id,
            user_id=user_id,
            lemma=word.key,
            lemma_accented=word.lemma,
            translation_ru=word.translation_ru,
            source=source_type,
        )
        for word in words
    )
    await session.commit()
    return pack


async def add_word(
    session: AsyncSession,
    *,
    pack: Pack,
    lemma: str,
    translation_ru: str | None,
    source: WordSource = WordSource.MANUAL,
) -> Word | None:
    """Добавить слово в черновик. None, если такое в пачке уже есть."""
    key = normalize_lemma(lemma)
    existing = await session.scalar(
        select(Word.id).where(Word.pack_id == pack.id, Word.lemma == key)
    )
    if existing is not None:
        return None

    word = Word(
        pack_id=pack.id,
        user_id=pack.user_id,
        lemma=key,
        lemma_accented=lemma.strip(),
        translation_ru=translation_ru,
        source=source,
    )
    session.add(word)
    await session.commit()
    return word


async def remove_word(session: AsyncSession, *, pack_id: int, word_id: int) -> bool:
    """Убрать слово из пачки — черновика или активной. False, если его там не было.

    Прогресс слова умирает вместе с ним (`ondelete=CASCADE` у `word_progress`)
    — намеренно: раз слово убрали, отслеживать его больше нечего, а вернуть
    пачке взятое с нуля честнее, чем прогресс, которого человек не помнит.
    История эпизодов при этом цела: там слово лежит номером, и пропавшее
    просто не попадает в словарь (`packs.lemmas`).
    """
    word = await session.scalar(select(Word).where(Word.id == word_id, Word.pack_id == pack_id))
    if word is None:
        return False
    await session.delete(word)
    await session.commit()
    return True


async def set_translation(
    session: AsyncSession, *, pack_id: int, word_id: int, translation_ru: str
) -> Word | None:
    """Заменить перевод. None, если слово не из этой пачки."""
    word = await session.scalar(select(Word).where(Word.id == word_id, Word.pack_id == pack_id))
    if word is None:
        return None
    word.translation_ru = translation_ru
    await session.commit()
    return word


async def apply_enrichment(
    session: AsyncSession,
    *,
    pack_id: int,
    enriched: Mapping[str, EnrichedWord],
    now: dt.datetime,
) -> int:
    """Разложить грамматику по словам пачки (FR-IMP-7). Сколько слов обновили.

    Слова, про которые модель промолчала, остаются с пустым `enriched_at` —
    по нему их потом можно догнать повторным вызовом, не трогая остальные.
    """
    updated = 0
    for word in await list_words(session, pack_id=pack_id):
        found = enriched.get(word.lemma)
        if found is None:
            continue

        # Перевод человека не затираем (FR-IMP-4): он для него правильный,
        # даже если словарь считает иначе. Модельный кладём рядом.
        word.translation_model = found.translation_ru
        if not word.translation_ru:
            word.translation_ru = found.translation_ru

        # Ударение и артикль ставим только если это то же самое слово: иначе
        # модель, ответившая не тем, молча подменит человеку слово в карточке.
        if found.lemma_accented and normalize_lemma(found.lemma_accented) == word.lemma:
            word.lemma_accented = found.lemma_accented

        word.pos = found.pos
        word.article = found.article
        word.gender = found.gender
        word.verb_form = found.verb_form
        word.examples = [example.model_dump() for example in found.examples]
        word.enriched_at = now
        updated += 1

    await session.commit()
    return updated


async def activate(
    session: AsyncSession, *, pack: Pack, period_days: int, now: dt.datetime
) -> Pack:
    """Подтвердить пачку: черновик → активная, слова получают прогресс.

    `now` передаётся аргументом, а не берётся из `utcnow()`: иначе тест на
    границы периода пришлось бы писать через патч времени.

    Прежняя активная пачка уходит в архив здесь же. Спрашивает об этом `/add`,
    но активной пачка становится только тут — и если инвариант «одна активная»
    держать в хендлере, следующий вход (перенос слов в конце периода, §5.8)
    придётся не забыть про него снова. Уникальный индекс иначе просто упадёт.
    """
    await session.execute(
        update(Pack)
        .where(
            Pack.user_id == pack.user_id,
            Pack.status == PackStatus.ACTIVE,
            Pack.id != pack.id,
        )
        .values(status=PackStatus.ARCHIVED)
    )

    pack.status = PackStatus.ACTIVE
    pack.period_days = period_days
    pack.started_at = now
    pack.ends_at = now + dt.timedelta(days=period_days)

    if pack.import_id is not None:
        record = await session.get(Import, pack.import_id)
        if record is not None:
            record.confirmed_at = now

    words = await list_words(session, pack_id=pack.id)
    session.add_all(WordProgress(word_id=word.id, user_id=pack.user_id) for word in words)

    await session.commit()
    return pack
