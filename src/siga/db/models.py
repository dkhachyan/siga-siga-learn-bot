"""Модели SQLAlchemy.

Таблицы заводятся не все разом, а по мере появления кода, который их
использует. Этап 0 — `users`, этап 1 — `imports`, `packs`, `words`,
`word_progress`. Остальное из §7 спецификации (`episodes`, `turns`,
`scheduled_events`, …) приедет на своих этапах.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from siga.core.enums import (
    Gender,
    Level,
    PackStatus,
    PartOfSpeech,
    UserState,
    WordSource,
)
from siga.db.base import Base

DEFAULT_TZ = "Asia/Nicosia"
DEFAULT_WINDOW_START = dt.time(10, 0)
DEFAULT_WINDOW_END = dt.time(20, 0)
DEFAULT_EPISODES_PER_DAY = 4
DEFAULT_PERIOD_DAYS = 14

#: Границы периода пачки, FR-SCH-1.
MIN_PERIOD_DAYS = 3
MAX_PERIOD_DAYS = 60

#: Верхний бокс лестницы интервалов, §5.7.
MAX_BOX = 5


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
