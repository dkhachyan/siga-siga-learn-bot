"""Разговор целиком: /next → ответ → закрытие → разбор.

Апдейты идут через настоящий Dispatcher, база настоящая, подменена только
модель. Проверяется то, что человек видит в переписке, и то, что после
разговора осталось в базе, — потому что именно из второго потом растёт
расписание повторений.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from siga.bot import keyboards
from siga.core.enums import EpisodeStatus
from siga.db.models import DialogMemory, Episode, Turn, WordProgress
from siga.llm.base import LlmClient, LlmUnavailable, Route
from tests.fake_llm import ScriptedClient
from tests.fake_telegram import TG_USER_ID, FakeTelegram
from tests.test_import_flow import ENRICHED_THREE, THREE_WORDS

type UseLlm = Callable[[LlmClient], None]
type Sessions = async_sessionmaker[AsyncSession]

#: Слова пачки получают номера по порядку: схема пересоздаётся на каждый тест.
NERO, KAFES, GALA = 1, 2, 3

FRAMES = json.dumps(
    {
        "episodes": [
            {
                "index": 0,
                "scene": "утро, кухня",
                "goal": "спросить, что человек пьёт по утрам",
                "plan": ["спросить про завтрак", "подхватить ответ"],
                "opening": "Καλημέρα! Τι πίνεις το πρωί;",
            }
        ]
    },
    ensure_ascii=False,
)


def turn(
    *,
    reply: str = "Ωραία! Και τι τρως;",
    assessments: Sequence[dict[str, Any]] = (),
    unused: Sequence[int] = (),
    action: str = "continue",
    analysis: str = "",
    memory: dict[str, Any] | None = None,
) -> str:
    """Ответ `R4` по сценарию."""
    return json.dumps(
        {
            "reply_text": reply,
            "reply_contains_recast": False,
            "word_assessments": list(assessments),
            "unused_target_words": list(unused),
            "episode_action": action,
            "analysis_ru": analysis,
            "memory_updates": memory
            or {"facts": [], "recurring_errors": [], "recent_topics": [], "tone_notes": ""},
        },
        ensure_ascii=False,
    )


def correct(word_id: int) -> dict[str, Any]:
    return {"word_id": word_id, "used": True, "verdict": "correct"}


def almost(word_id: int) -> dict[str, Any]:
    return {
        "word_id": word_id,
        "used": True,
        "verdict": "almost",
        "error_type": "case",
        "user_form": "νερώ",
        "correct_form": "νερό",
    }


@pytest.fixture
def db(engine: AsyncEngine) -> Sessions:
    """Сессия, чтобы заглянуть в базу мимо бота."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def with_pack(
    telegram: FakeTelegram, use_llm: UseLlm, *answers: str | Exception
) -> ScriptedClient:
    """Активная пачка из трёх слов и заготовленные ответы для разговора."""
    client = ScriptedClient(ENRICHED_THREE, *answers)
    use_llm(client)
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)
    await telegram.press("✅")
    return client


# --- начало -------------------------------------------------------------------


async def test_next_without_a_pack_asks_for_words(telegram: FakeTelegram) -> None:
    answers = await telegram.send("/next")
    assert "/add" in answers[-1]


async def test_next_opens_an_episode(telegram: FakeTelegram, use_llm: UseLlm, db: Sessions) -> None:
    await with_pack(telegram, use_llm, FRAMES)

    answers = await telegram.send("/next")

    assert "Καλημέρα! Τι πίνεις το πρωί;" in answers[-1]
    assert telegram.find_button("🔍") is not None, "разбор доступен во время эпизода (FR-CHK-2)"
    assert telegram.find_button("💡") is not None, "подсказка под вопросом (FR-CHK-8)"
    assert telegram.find_button("🇷🇺") is not None

    async with db() as session:
        episode = await session.scalar(select(Episode))
        assert episode is not None
        assert episode.status == EpisodeStatus.OPEN
        assert episode.target_word_ids == [NERO, KAFES, GALA]
        assert episode.frame["scene"] == "утро, кухня"
        assert "opening" not in episode.frame, "реплика хранится отдельно от рамки"


async def test_second_next_does_not_start_a_parallel_talk(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    await with_pack(telegram, use_llm, FRAMES)
    await telegram.send("/next")

    answers = await telegram.send("/next")
    assert "/end" in answers[-1]


async def test_a_silent_model_does_not_lose_the_pack(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    await with_pack(telegram, use_llm, LlmUnavailable("таймаут"))

    answers = await telegram.send("/next")
    assert "/next" in answers[-1], "предлагаем попробовать снова, а не разводим руками"

    screen = await telegram.send("/pack")
    assert "το νερό" in screen[-1]


# --- ход ----------------------------------------------------------------------


async def test_free_text_without_an_episode_falls_through(telegram: FakeTelegram) -> None:
    """Хендлер эпизода пропускает ход дальше — иначе заглушка перестала бы работать."""
    answers = await telegram.send("Καλημέρα")
    assert "/help" in answers[-1]


async def test_an_answer_gets_a_reply(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    answer = turn(assessments=[correct(NERO)], unused=[KAFES, GALA])
    await with_pack(telegram, use_llm, FRAMES, answer)
    await telegram.send("/next")

    answers = await telegram.send("Πίνω νερό.")

    assert answers[-1] == "Ωραία! Και τι τρως;"
    assert "Как прошло" not in answers[-1], "разговор ещё идёт"

    async with db() as session:
        turns = list(await session.scalars(select(Turn).order_by(Turn.idx)))
    assert turns[0].user_text == "Πίνω νερό."
    assert turns[0].assessments[0]["verdict"] == "correct"
    assert turns[0].latency_ms is not None, "время ответа модели пишется само"
    assert len(turns) == 2, "следующий ход уже заведён"


async def test_a_meaningless_answer_never_reaches_the_model(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    """FR-CHK-4: на «...» модель не зовём — сценарий кончился бы ошибкой."""
    client = await with_pack(telegram, use_llm, FRAMES)
    await telegram.send("/next")
    before = client.calls

    answers = await telegram.send("...")

    assert client.calls == before
    assert "пару слов" in answers[-1]


async def test_an_old_dunno_button_still_works(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    """`🤷 Не знаю` больше не рисуется — её место заняла подсказка (FR-CHK-8).

    Но кнопки живут в переписке вечно, и нажатая в прошлогоднем разговоре она
    обязана остаться обычным ходом, а не молчащим кружком: убрать вместе с
    кнопкой ещё и обработчик — ровно та поломка, что была у `/settings`.
    """
    await with_pack(telegram, use_llm, FRAMES, turn(reply="Πίνω καφέ κι εγώ."))
    await telegram.send("/next")

    assert telegram.find_button("🤷") is None, "новая клавиатура её не предлагает"
    answers = await telegram.click(keyboards.EpisodeAction(action="dunno").pack())

    assert answers[-1] == "Πίνω καφέ κι εγώ."
    async with db() as session:
        turns = list(await session.scalars(select(Turn).order_by(Turn.idx)))
    assert turns[0].user_text == "Δεν ξέρω."


# --- закрытие -----------------------------------------------------------------


async def test_all_words_assessed_closes_the_episode(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    """Правило наше, а не модели: она просила продолжать, но оценивать больше нечего."""
    await with_pack(
        telegram,
        use_llm,
        FRAMES,
        turn(assessments=[correct(NERO), almost(KAFES), correct(GALA)], action="continue"),
    )
    await telegram.send("/next")

    answers = await telegram.send("Πίνω νερό, καφέ και γάλα.")

    summary = answers[-1]
    assert "Как прошло" in summary
    assert "✅ <b>το νερό</b>" in summary
    assert "≈ <b>ο καφές</b>" in summary
    assert telegram.find_button("🔍") is not None, "FR-EP-8: разбор под итогом"

    async with db() as session:
        episode = await session.scalar(select(Episode))
        progress = {row.word_id: row for row in await session.scalars(select(WordProgress))}
    assert episode is not None and episode.status == EpisodeStatus.CLOSED
    assert progress[NERO].box == 1, "верно — на ступеньку вверх"
    assert progress[KAFES].box == 0, "почти — бокс на месте (§5.7)"


async def test_an_unused_word_is_not_punished(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    """FR-EP-7: молчание — не ошибка, слово просто возвращается в пул."""
    answer = turn(assessments=[correct(NERO)], unused=[KAFES, GALA], action="close")
    await with_pack(telegram, use_llm, FRAMES, answer)
    await telegram.send("/next")

    answers = await telegram.send("Πίνω νερό.")

    assert "не прозвучало" in answers[-1]
    async with db() as session:
        progress = {row.word_id: row for row in await session.scalars(select(WordProgress))}
    assert progress[GALA].box == 0
    assert progress[GALA].next_due_at is not None, "вернётся в ближайший отбор"


async def test_end_closes_the_talk_without_the_model(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    client = await with_pack(telegram, use_llm, FRAMES)
    await telegram.send("/next")
    before = client.calls

    answers = await telegram.send("/end")

    assert client.calls == before, "«хватит» модели не касается"
    assert "Как прошло" in answers[-1]
    async with db() as session:
        episode = await session.scalar(select(Episode))
    assert episode is not None and episode.status == EpisodeStatus.CLOSED


async def test_end_without_a_talk_says_so(telegram: FakeTelegram) -> None:
    answers = await telegram.send("/end")
    assert "/next" in answers[-1]


# --- разбор и память ----------------------------------------------------------


async def test_analysis_shows_the_correction(telegram: FakeTelegram, use_llm: UseLlm) -> None:
    await with_pack(
        telegram,
        use_llm,
        FRAMES,
        turn(
            assessments=[almost(NERO)],
            analysis="После πίνω нужен винительный: νερό.",
            action="close",
        ),
    )
    await telegram.send("/next")
    await telegram.send("Πίνω νερώ.")

    answers = await telegram.press("🔍")

    screen = answers[-1]
    assert "νερώ → νερό (падеж)" in screen
    assert "После πίνω нужен винительный" in screen
    assert "<i>Πίνω νερώ.</i>" in screen, "человек должен узнать свою фразу"


async def test_the_reply_itself_stays_free_of_grammar(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    """FR-CHK-2: разбор ждёт кнопки, в реплике Ника его нет."""
    await with_pack(
        telegram, use_llm, FRAMES, turn(assessments=[almost(NERO)], analysis="Нужен винительный.")
    )
    await telegram.send("/next")

    answers = await telegram.send("Πίνω νερώ.")

    assert "винительный" not in " ".join(answers)


async def test_what_the_person_told_about_himself_is_remembered(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    await with_pack(
        telegram,
        use_llm,
        FRAMES,
        turn(
            assessments=[correct(NERO)],
            memory={
                "facts": ["пьёт воду по утрам"],
                "recurring_errors": [],
                "recent_topics": ["завтрак"],
                "tone_notes": "",
            },
        ),
    )
    await telegram.send("/next")
    await telegram.send("Πίνω νερό κάθε πρωί.")

    async with db() as session:
        row = await session.scalar(select(DialogMemory))
    assert row is not None
    assert row.profile["facts"] == ["пьёт воду по утрам"]
    assert row.tokens_estimate > 0


# --- перевод реплики ----------------------------------------------------------


def translation(ru: str) -> str:
    """Ответ `R6` по сценарию."""
    return json.dumps({"ru": ru}, ensure_ascii=False)


async def test_translate_answers_with_a_popup(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    """FR-CHK-7: перевод виден на месте, а переписка остаётся греческой."""
    client = await with_pack(
        telegram, use_llm, FRAMES, translation("Доброе утро! Что ты пьёшь утром?")
    )
    await telegram.send("/next")
    before = client.calls

    answers = await telegram.press("🇷🇺")

    alert = telegram.last_alert
    assert alert is not None
    assert alert.text == "Доброе утро! Что ты пьёшь утром?"
    assert alert.show_alert, "исчезающую подсказку человек прочитать не успеет"
    assert answers == [], "в переписку перевод не сыплется"
    assert client.calls == before + 1
    assert client.routes[-1] is Route.TRANSLATE

    async with db() as session:
        stored = await session.scalar(select(Turn.bot_text_ru).order_by(Turn.idx))
    assert stored == "Доброе утро! Что ты пьёшь утром?"


async def test_the_second_press_costs_nothing(telegram: FakeTelegram, use_llm: UseLlm) -> None:
    """Перевод кэшируется: кнопка живёт в переписке вечно, платить дважды незачем."""
    client = await with_pack(telegram, use_llm, FRAMES, translation("Что ты пьёшь утром?"))
    await telegram.send("/next")
    await telegram.press("🇷🇺")
    after_first = client.calls

    await telegram.press("🇷🇺")

    assert client.calls == after_first, "второе нажатие идёт в базу, а не в модель"
    alert = telegram.last_alert
    assert alert is not None
    assert alert.text == "Что ты пьёшь утром?"


async def test_translate_takes_the_reply_it_stands_under(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    """Кнопка адресует свой ход, а не вводную реплику."""
    client = await with_pack(
        telegram,
        use_llm,
        FRAMES,
        turn(assessments=[correct(NERO)], reply="Ωραία! Και τι τρως;"),
        translation("Здорово! А что ты ешь?"),
    )
    await telegram.send("/next")
    await telegram.send("Πίνω νερό.")

    await telegram.press("🇷🇺")

    asked = client.asked[-1][-1].text
    assert "Ωραία! Και τι τρως;" in asked
    assert "Καλημέρα! Τι πίνεις το πρωί;" not in asked, "переводится нажатая реплика"
    assert "Πίνω νερό." in asked, "предыдущий ответ идёт контекстом — без него рекаст непонятен"
    alert = telegram.last_alert
    assert alert is not None
    assert alert.text == "Здорово! А что ты ешь?"


async def test_a_long_translation_goes_as_a_message(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    """В окно Telegram влезает 200 символов; остальное — сообщением, с экранированием."""
    long_ru = "Слово <тут> и дальше по кругу. " * 10
    await with_pack(telegram, use_llm, FRAMES, translation(long_ru))
    await telegram.send("/next")

    answers = await telegram.press("🇷🇺")

    assert answers, "перевод не потерялся"
    assert "&lt;тут&gt;" in answers[-1], "сообщения уходят при ParseMode.HTML"
    alert = telegram.last_alert
    assert alert is not None
    assert not alert.text, "окно пустое: текст ушёл сообщением"


async def test_a_silent_model_leaves_the_button_working(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    await with_pack(telegram, use_llm, FRAMES, LlmUnavailable("провайдер молчит"))
    await telegram.send("/next")

    await telegram.press("🇷🇺")

    alert = telegram.last_alert
    assert alert is not None
    assert "ещё раз" in (alert.text or "")
    async with db() as session:
        stored = await session.scalar(select(Turn.bot_text_ru).order_by(Turn.idx))
    assert stored is None, "пустое в кэш не пишем — иначе кнопка замолчит навсегда"


async def test_someone_elses_episode_is_not_translated(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    """Данные кнопки видны в клиенте и подставляются руками."""
    client = await with_pack(telegram, use_llm, FRAMES)
    await telegram.send("/next")
    async with db() as session:
        episode_id = await session.scalar(select(Episode.id))
    assert episode_id is not None
    before = client.calls

    await telegram.click(
        keyboards.TranslateAction(episode_id=episode_id, turn_idx=0).pack(),
        from_user_id=TG_USER_ID + 1,
    )

    assert client.calls == before, "к модели за чужой репликой не идём"
    alert = telegram.last_alert
    assert alert is not None
    assert "разговора" in (alert.text or "")


async def test_an_unclear_answer_keeps_the_buttons_of_the_question(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    """«Не разобрал ответа» — момент, когда подсказка нужнее всего.

    Раньше кнопки тут шли без адреса, и под этим сообщением оставался один
    разбор. Адресуем текущим ходом: человек, набравший «...», как раз и не
    знает, что сказать.
    """
    await with_pack(telegram, use_llm, FRAMES)
    await telegram.send("/next")

    await telegram.send("...")

    markup = telegram.last_keyboard
    assert markup is not None
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["🔍 Разбор", "💡 Что ответить", "🇷🇺 Перевод"]


async def test_the_farewell_has_no_translate_button(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    """Известное ограничение: у закрывающего хода нет строки в `turns`."""
    await with_pack(
        telegram,
        use_llm,
        FRAMES,
        turn(
            reply="Καλή όρεξη! Τα λέμε.",
            assessments=[correct(NERO), correct(KAFES), correct(GALA)],
            action="close",
        ),
    )
    await telegram.send("/next")
    assert telegram.find_button("🇷🇺") is not None, "под вводной репликой кнопка есть"
    calls_before = len(telegram.session.calls)

    await telegram.send("Πίνω νερό, καφέ και γάλα.")

    fresh = telegram.session.calls[calls_before:]
    markups = [
        call.reply_markup
        for call in fresh
        if isinstance(getattr(call, "reply_markup", None), InlineKeyboardMarkup)
    ]
    labels = [button.text for markup in markups for row in markup.inline_keyboard for button in row]
    assert labels == ["🔍 Разбор"], "под прощанием и итогом переводить нечего"


# --- подсказка «что ответить» -------------------------------------------------


def hint(*options: tuple[str, list[int]]) -> str:
    """Ответ `R7` по сценарию."""
    return json.dumps(
        {"options": [{"ru": ru, "word_ids": ids} for ru, ids in options]}, ensure_ascii=False
    )


HINT_TWO = hint(
    ("Скажи, что пьёшь кофе каждое утро", [KAFES]),
    ("Ответь, что предпочитаешь воду", [NERO]),
)


async def test_the_hint_arrives_as_a_message_with_its_words(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    """FR-CHK-8: варианты читают, пока набирают ответ, — окно тут не годится."""
    client = await with_pack(telegram, use_llm, FRAMES, HINT_TWO)
    await telegram.send("/next")
    before = client.calls

    answers = await telegram.press("💡")

    assert client.calls == before + 1
    assert client.routes[-1] is Route.HINT
    # Нажатие закрываем пустым ответом — спиннер всё равно ничего не покажет,
    # а текст подсказки в окно не влезет и исчезнет от касания клавиатуры.
    alert = telegram.last_alert
    assert alert is not None and not alert.text, "подсказка идёт сообщением, а не окном"

    text = answers[-1]
    assert "Скажи, что пьёшь кофе каждое утро" in text
    assert "Ответь, что предпочитаешь воду" in text
    assert "<b>ο καφές</b>" in text, "слово рисуем из базы, а не со слов модели"
    assert "<b>το νερό</b>" in text
    assert "собери её по-гречески сам" in text, "иначе ответят по-русски"

    async with db() as session:
        stored = await session.scalar(select(Turn.hint_ru).order_by(Turn.idx))
    assert stored == text


async def test_a_hint_is_not_a_turn(telegram: FakeTelegram, use_llm: UseLlm, db: Sessions) -> None:
    """Главное обещание: подсказка не тратит ход и не портит зачёт.

    Стань она ходом — съела бы один из четырёх (FR-EP-6), сдвинула бы
    `turns_left` в промпте `R4` и попала бы в историю эпизода репликой,
    которой Ник не говорил.
    """
    await with_pack(telegram, use_llm, FRAMES, HINT_TWO)
    await telegram.send("/next")

    await telegram.press("💡")

    async with db() as session:
        episode = await session.scalar(select(Episode))
        turns = list(await session.scalars(select(Turn).order_by(Turn.idx)))
    assert episode is not None
    assert episode.turns_count == 0, "подсказка ход не открывает"
    assert len(turns) == 1
    assert turns[0].user_text is None
    assert turns[0].assessments == []


async def test_the_second_hint_costs_nothing(telegram: FakeTelegram, use_llm: UseLlm) -> None:
    """Нажимают её как раз от нетерпения — и дважды подряд."""
    client = await with_pack(telegram, use_llm, FRAMES, HINT_TWO)
    await telegram.send("/next")
    first = await telegram.press("💡")
    after_first = client.calls

    again = await telegram.press("💡")

    assert client.calls == after_first, "второе нажатие идёт в базу, а не в модель"
    assert again[-1] == first[-1]


async def test_a_hint_only_offers_words_that_have_not_sounded_yet(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    """Предлагать сказанное значит гонять человека по кругу (FR-EP-7)."""
    client = await with_pack(
        telegram,
        use_llm,
        FRAMES,
        turn(reply="Α, νερό! Και τι άλλο;", assessments=[correct(NERO)]),
        HINT_TWO,
    )
    await telegram.send("/next")
    await telegram.send("Πίνω νερό.")

    await telegram.press("💡")

    asked = client.asked[-1][1].text
    assert "το νερό" not in asked, "это слово уже зачтено"
    assert "ο καφές" in asked


async def test_a_silent_model_leaves_the_hint_button_working(
    telegram: FakeTelegram, use_llm: UseLlm, db: Sessions
) -> None:
    """Пустое в `hint_ru` — это «не спрашивали», и кнопка обязана остаться живой."""
    await with_pack(telegram, use_llm, FRAMES, LlmUnavailable("модель молчит"))
    await telegram.send("/next")

    answers = await telegram.press("💡")

    assert "не пришла" in answers[-1]
    async with db() as session:
        stored = await session.scalar(select(Turn.hint_ru).order_by(Turn.idx))
    assert stored is None


async def test_someone_elses_episode_gets_no_hint(telegram: FakeTelegram, use_llm: UseLlm) -> None:
    """Данные кнопки видны в клиенте и подставляются руками."""
    client = await with_pack(telegram, use_llm, FRAMES, HINT_TWO)
    await telegram.send("/next")
    before = client.calls

    await telegram.click(
        keyboards.HintAction(episode_id=1, turn_idx=0).pack(), from_user_id=TG_USER_ID + 1
    )

    assert client.calls == before, "к модели за чужим разговором не идём"
    alert = telegram.last_alert
    assert alert is not None
    assert "разговора" in (alert.text or "")


async def test_an_answered_question_does_not_buy_a_hint(
    telegram: FakeTelegram, use_llm: UseLlm
) -> None:
    """Кнопка из истории: разговор ушёл вперёд, платить за подсказку в прошлое незачем."""
    client = await with_pack(
        telegram,
        use_llm,
        FRAMES,
        turn(reply="Α, νερό! Και τι άλλο;", assessments=[correct(NERO)]),
    )
    await telegram.send("/next")
    await telegram.send("Πίνω νερό.")
    before = client.calls

    answers = await telegram.click(keyboards.HintAction(episode_id=1, turn_idx=0).pack())

    assert client.calls == before
    assert "уже ответил" in answers[-1]
