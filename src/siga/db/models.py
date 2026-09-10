"""Модели SQLAlchemy.

Таблицы заводятся не все разом, а по мере появления кода, который их
использует. Этап 0 — `users`, этап 1 — `imports`, `packs`, `words`,
`word_progress`. Остальное из §7 спецификации (`episodes`, `turns`,
`scheduled_events`, …) приедет на своих этапах.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from siga.core.enums import (
    EpisodeIntent,
    EpisodeStatus,
    EventStatus,
    EventType,
    Gender,
    Level,
    PackStatus,
    PartOfSpeech,
    UserState,
    WordSource,
)
from siga.core.episodes import MAX_TURNS
from siga.core.srs import MAX_BOX
from siga.db.base import Base

DEFAULT_TZ = "Asia/Nicosia"
DEFAULT_WINDOW_START = dt.time(10, 0)
DEFAULT_WINDOW_END = dt.time(20, 0)
DEFAULT_EPISODES_PER_DAY = 4
DEFAULT_MIN_GAP_MINUTES = 45
DEFAULT_PERIOD_DAYS = 14

#: Границы периода пачки, FR-SCH-1.
MIN_PERIOD_DAYS = 3
MAX_PERIOD_DAYS = 60


def _values(enum: type[StrEnum]) -> str:
    """SQL-список значений перечисления для CHECK: `'a', 'b', 'c'`.

    Пишем ограничение из самого перечисления, чтобы новое значение нельзя
    было добавить в код, забыв про базу: `alembic check` сразу увидит расхождение.
    """
    return ", ".join(f"'{member.value}'" for member in enum)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "episodes_per_day BETWEEN 1 AND 12",
            name="episodes_per_day_range",
        ),
        CheckConstraint(
            "min_gap_minutes BETWEEN 15 AND 480",
            name="min_gap_minutes_range",
        ),
        CheckConstraint(f"state IN ({_values(UserState)})", name="state_known"),
        CheckConstraint(f"level IN ({_values(Level)})", name="level_known"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(32))

    tz: Mapped[str] = mapped_column(String(64), default=DEFAULT_TZ, server_default=DEFAULT_TZ)
    level: Mapped[str] = mapped_column(String(2), default=Level.A1, server_default=Level.A1)

    window_start: Mapped[dt.time] = mapped_column(
        Time, default=DEFAULT_WINDOW_START, server_default="10:00"
    )
    window_end: Mapped[dt.time] = mapped_column(
        Time, default=DEFAULT_WINDOW_END, server_default="20:00"
    )
    episodes_per_day: Mapped[int] = mapped_column(
        SmallInteger, default=DEFAULT_EPISODES_PER_DAY, server_default="4"
    )
    min_gap_minutes: Mapped[int] = mapped_column(
        SmallInteger, default=DEFAULT_MIN_GAP_MINUTES, server_default="45"
    )
    """Сколько минут Ник выдерживает между разговорами (FR-SCH-8).

    В минутах, а не `Interval`: рядом лежит `episodes_per_day` числом, экран
    настроек оперирует минутами, и целое проще и в CHECK, и в миграции."""

    state: Mapped[str] = mapped_column(
        String(16), default=UserState.NEW, server_default=UserState.NEW
    )

    invited_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    paused_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """С какого момента расписание стоит (FR-SCH-7). Хранится у человека, а не
    у пачки: на паузу ставят себя, а не список слов. Период продлевается при
    снятии паузы — по этому моменту и считается, на сколько дней."""

    def __repr__(self) -> str:
        return f"<User id={self.id} tg={self.tg_user_id} state={self.state}>"


class Import(Base):
    """Один акт загрузки списка: что человек прислал и чем это кончилось.

    Храним сырой текст, а не фотографию: §10.2 требует удалять снимок с диска
    сразу после распознавания. Запись живёт и после подтверждения — по ней
    разбираются жалобы «бот неправильно прочитал моё слово».
    """

    __tablename__ = "imports"
    __table_args__ = (
        CheckConstraint(f"source_type IN ({_values(WordSource)})", name="source_type_known"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    source_type: Mapped[str] = mapped_column(String(16))
    raw_text: Mapped[str] = mapped_column(Text)
    """То, что разбирали: набранный текст или результат распознавания."""

    word_count: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    """Сколько слов вышло после разбора и дедупликации."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """None — человек до экрана подтверждения дошёл, но «Всё верно» не нажал."""

    def __repr__(self) -> str:
        return f"<Import id={self.id} user={self.user_id} words={self.word_count}>"


class Pack(Base):
    """Пачка слов: то, что загрузили за раз и изучают один период."""

    __tablename__ = "packs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(PackStatus)})", name="status_known"),
        CheckConstraint(
            f"period_days BETWEEN {MIN_PERIOD_DAYS} AND {MAX_PERIOD_DAYS}",
            name="period_days_range",
        ),
        CheckConstraint("paused_days >= 0", name="paused_days_non_negative"),
        Index("ix_packs_user_id_status", "user_id", "status"),
        # Активная пачка у человека одна (§5.8): период, отчёт и перенос слов
        # везде написаны в единственном числе. Проверка в хендлере есть, но
        # она защищает только от одного пути; частичный уникальный индекс
        # закрывает и остальные — включая будущий планировщик.
        Index(
            "uq_packs_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text(f"status = '{PackStatus.ACTIVE.value}'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    import_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("imports.id", ondelete="SET NULL")
    )
    """Откуда пачка приехала. Пустой у пачек, собранных вручную."""

    title: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default=PackStatus.DRAFT, server_default=PackStatus.DRAFT
    )

    period_days: Mapped[int] = mapped_column(
        SmallInteger, default=DEFAULT_PERIOD_DAYS, server_default="14"
    )
    paused_days: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    """Сколько дней пачка простояла на паузе — на столько сдвигается `ends_at` (FR-SCH-7)."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Момент активации. У черновика пустой — период ещё не тикает."""
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Pack id={self.id} user={self.user_id} status={self.status}>"


class Word(Base):
    """Слово в пачке — вместе с грамматикой, добытой обогащением.

    Два поля под лемму намеренно: `lemma` — ключ сравнения (без артикля,
    без ударений, см. `siga.core.greek.normalize_lemma`), `lemma_accented` —
    то, что показываем человеку. Показывать слово без ударения нельзя: без
    него оно написано неправильно, а иногда ударение различает значения.
    """

    __tablename__ = "words"
    __table_args__ = (
        CheckConstraint(f"pos IS NULL OR pos IN ({_values(PartOfSpeech)})", name="pos_known"),
        CheckConstraint(f"gender IS NULL OR gender IN ({_values(Gender)})", name="gender_known"),
        CheckConstraint(f"source IN ({_values(WordSource)})", name="source_known"),
        # Дедупликация против архива (FR-IMP-5): ищем по владельцу и ключу леммы.
        Index("ix_words_user_id_lemma", "user_id", "lemma"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pack_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("packs.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    """Дубль владельца пачки — чтобы искать по архиву без join'а на packs."""

    lemma: Mapped[str] = mapped_column(String(64))
    """Ключ сравнения: нижний регистр, без артикля и диакритики."""
    lemma_accented: Mapped[str] = mapped_column(String(64))
    """Как слово выглядит для человека: с ударением, у существительных с артиклем."""

    translation_ru: Mapped[str | None] = mapped_column(String(160))
    """Перевод пользователя, если он его дал (FR-IMP-4), иначе наш."""
    translation_model: Mapped[str | None] = mapped_column(String(160))
    """Перевод модели. Держим рядом, чтобы показать расхождение, а не затирать чужое."""

    pos: Mapped[str | None] = mapped_column(String(16))
    article: Mapped[str | None] = mapped_column(String(8))
    gender: Mapped[str | None] = mapped_column(String(1))
    verb_form: Mapped[str | None] = mapped_column(String(64))
    """1 л. ед. ч. наст. вр. для глаголов (FR-IMP-7)."""

    examples: Mapped[list[dict[str, str]]] = mapped_column(JSONB, default=list, server_default="[]")
    """Список `{"el": ..., "ru": ...}` — 1–2 коротких примера под уровень человека."""

    source: Mapped[str] = mapped_column(String(16))
    enriched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """None — грамматики ещё нет: обогащение не дошло или упало."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Word id={self.id} {self.lemma_accented!r} pack={self.pack_id}>"


class WordProgress(Base):
    """Усвоение одного слова: бокс лестницы интервалов и статистика (§5.7).

    Строка на слово, поэтому первичный ключ — сам `word_id`. `user_id`
    продублирован из `words` ради индекса `(user_id, next_due_at)`: отбор
    созревших слов — горячий путь ночного пересчёта, join там лишний.
    """

    __tablename__ = "word_progress"
    __table_args__ = (
        CheckConstraint(f"box BETWEEN 0 AND {MAX_BOX}", name="box_range"),
        CheckConstraint("correct_uses <= uses", name="correct_uses_within_uses"),
        CheckConstraint("correct_streak >= 0", name="correct_streak_non_negative"),
        Index("ix_word_progress_user_id_next_due_at", "user_id", "next_due_at"),
    )

    word_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("words.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))

    box: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    correct_streak: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    uses: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    correct_uses: Mapped[int] = mapped_column(SmallInteger, server_default="0")

    last_shown_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Когда слово снова созреет. У нового слова пусто — оно и так в пуле."""
    learned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Заполняется при боксе 5 и двух верных употреблениях подряд (FR-SRS-3)."""

    def __repr__(self) -> str:
        return f"<WordProgress word={self.word_id} box={self.box}>"


class Episode(Base):
    """Один короткий разговор вокруг двух-трёх слов (§5.3).

    Рамка (`frame`) и первая реплика (`opening_text`) приезжают из `R3` заранее
    и лежат готовыми: когда наступает слот, отправить нужно сразу, а не ждать
    модель. Поэтому `pending` — полноценное состояние, а не техническая пауза.
    """

    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint(f"intent IN ({_values(EpisodeIntent)})", name="intent_known"),
        CheckConstraint(f"status IN ({_values(EpisodeStatus)})", name="episode_status_known"),
        CheckConstraint(
            f"turns_count BETWEEN 0 AND {MAX_TURNS}",
            name="turns_count_range",
        ),
        CheckConstraint(
            "array_length(target_word_ids, 1) BETWEEN 1 AND 3",
            name="target_word_ids_size",
        ),
        # Одновременно открыт не более одного эпизода (FR-SCH-5), и проверка
        # «есть ли открытый» делается на каждом входящем сообщении — §7.
        Index("ix_episodes_user_id_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    pack_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("packs.id", ondelete="SET NULL")
    )
    """Пачка, из которой брались слова. Пустеет, когда пачку удалили, — сам
    эпизод при этом остаётся: он часть истории, а не часть пачки."""

    intent: Mapped[str] = mapped_column(String(2))
    scene: Mapped[str | None] = mapped_column(String(160))
    """Место и обстановка одной строкой — «Ник в очереди за кофе»."""
    frame: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, server_default="{}")
    """Сцена, цель и план ходов от `R3`. Формат описан в §8.3."""

    opening_text: Mapped[str | None] = mapped_column(Text)
    target_word_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))

    status: Mapped[str] = mapped_column(
        String(16), default=EpisodeStatus.PENDING, server_default=EpisodeStatus.PENDING
    )
    turns_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    opened_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Когда ушла первая реплика. По нему считается TTL (FR-SCH-6)."""
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    summary_ru: Mapped[str | None] = mapped_column(Text)
    """Что было в эпизоде — для `/history` и для отчёта в конце периода."""
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )
    """Сумма по всем вызовам эпизода. Цена ответа — то, что придётся держать
    в узде, и считать её задним числом по логам сложнее, чем копить сразу."""

    def __repr__(self) -> str:
        return f"<Episode id={self.id} user={self.user_id} {self.intent} {self.status}>"


class Turn(Base):
    """Один ход: реплика Ника и ответ человека на неё.

    Строка заводится в момент отправки реплики, а `user_text` дописывается,
    когда человек ответил. Ход без ответа — это не мусор, а сам факт: по нему
    видно, где разговор оборвался.
    """

    __tablename__ = "turns"
    __table_args__ = (
        CheckConstraint("idx >= 0", name="turn_idx_non_negative"),
        UniqueConstraint("episode_id", "idx", name="uq_turns_episode_id_idx"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(SmallInteger)
    """Номер хода внутри эпизода, с нуля. По нему собирается история для `R4`."""

    bot_text: Mapped[str] = mapped_column(Text)
    user_text: Mapped[str | None] = mapped_column(Text)

    assessments: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    """Оценка каждого целевого слова в этом ответе — формат в §8.3."""
    corrections: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    """Что поправил рекаст: было — стало — тип ошибки. Отсюда растёт `🔍 Разбор`."""
    analysis_ru: Mapped[str | None] = mapped_column(Text)
    """Объяснение по-русски к этому ходу. В чат не уходит, пока не нажат
    `🔍 Разбор` (FR-CHK-2): в реплике Ника грамматике места нет."""
    bot_text_ru: Mapped[str | None] = mapped_column(Text)
    """Перевод `bot_text` на русский — кэш маршрута `R6` (FR-CHK-7).

    Заполняется только по нажатию `🇷🇺 Перевод`: платить за перевод каждой
    реплики незачем, читают его редко. `NULL` значит «ещё не переводили», и
    пустую строку сюда писать нельзя — иначе кнопка навсегда замолчит."""
    hint_ru: Mapped[str | None] = mapped_column(Text)
    """Варианты ответа по-русски — кэш маршрута `R7` (FR-CHK-8).

    Готовым текстом сообщения, а не JSON: подсказка собирается один раз и
    показывается как есть, а разбирать её обратно на варианты незачем.

    Кэш честен, потому что подсказка зависит от того, каких слов ученик ещё не
    сказал, а в пределах одного хода этот набор не меняется: оценки пишутся
    вместе с ответом, а ответ открывает уже следующий ход. Как и у соседа,
    `NULL` значит «не спрашивали», и пустую строку сюда писать нельзя."""

    sent_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    answered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    """Сколько думала модель. Ответ дольше нескольких секунд ломает переписку —
    без замера этого не увидеть до жалоб."""
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )

    def __repr__(self) -> str:
        return f"<Turn episode={self.episode_id} idx={self.idx}>"


class ScheduledEvent(Base):
    """Одно намерение написать человеку в назначенный момент (§5.6).

    Очередь живёт в базе, а не в памяти процесса: расписание должно переживать
    деплой и расходиться на несколько отправщиков. `FOR UPDATE SKIP LOCKED` по
    индексу `(status, fire_at)` даёт и то, и другое без брокера — см. §9.

    Ссылка на эпизод лежит в `payload`, а не отдельной колонкой: типов событий
    будет больше (конец периода, отчёт), и не у каждого есть эпизод.
    """

    __tablename__ = "scheduled_events"
    __table_args__ = (
        CheckConstraint(f"type IN ({_values(EventType)})", name="event_type_known"),
        CheckConstraint(f"status IN ({_values(EventStatus)})", name="event_status_known"),
        CheckConstraint("attempts_count >= 0", name="attempts_count_non_negative"),
        # Горячий путь отправщика: «что уже пора» раз в 30 секунд — §7.
        Index("ix_scheduled_events_status_fire_at", "status", "fire_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    fire_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    """Когда отправлять. В UTC — местное время человека уже учтено раскладкой."""

    type: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, server_default="{}")
    """`{"episode_id": …}` — что именно отправлять."""

    status: Mapped[str] = mapped_column(
        String(16), default=EventStatus.PENDING, server_default=EventStatus.PENDING
    )
    attempts_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    """Сколько раз отправщик за это брался. Растёт и при неудаче — по нему
    видно события, на которых он крутится вхолостую."""

    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    """Когда событие взяли в работу. Нужно не для блокировки — её держит сама
    транзакция, — а чтобы потом понять, где отправщик застрял."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<ScheduledEvent id={self.id} {self.type} at={self.fire_at} {self.status}>"


class DialogMemory(Base):
    """Профиль диалога: то, что Ник о человеке помнит (§5.5).

    Одна строка на человека, целиком в JSON. Раскладывать факты по таблицам
    незачем: профиль всегда читается и пишется целиком, а его потолок — не
    строки, а токены, и держать этот потолок удобнее над одним документом.
    """

    __tablename__ = "dialog_memory"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    profile: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, server_default="{}")
    """`facts`, `recurring_errors`, `recent_topics`, `tone_notes`, `last_seen_at`."""

    tokens_estimate: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    """Оценка размера профиля: по ней срабатывает вытеснение (FR-MEM-3)."""

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<DialogMemory user={self.user_id} ~{self.tokens_estimate}t>"
