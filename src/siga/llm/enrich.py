"""Маршрут R2: обогащение слов грамматикой и примерами (FR-IMP-7).

Здесь схема ответа, промпт и сопоставление ответа со списком, который мы
отправляли. Записью в базу занимается `db.packs` — этот модуль ничего не
знает ни про SQLAlchemy, ни про то, откуда слова пришли.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from siga.core.enums import Gender, Level, PartOfSpeech
from siga.core.greek import normalize_lemma
from siga.llm.base import LlmClient, LlmError, Message, Route, Usage, complete_json

log = logging.getLogger(__name__)

#: Слов за один вызов. Сотня слов сразу — это ~8k токенов выхода, то есть
#: обрезанный ответ и потерянная пачка. Двадцать помещаются с запасом, а
#: пять вызовов вместо одного всё равно идут параллельно ночному расписанию.
ENRICH_BATCH = 20

#: Сколько примеров просим. Больше двух человек не читает (FR-IMP-7).
MAX_EXAMPLES = 2

SYSTEM_PROMPT = """\
Ты — редактор учебных карточек греческого языка. Тебе дают список греческих \
слов, ты возвращаешь для каждого грамматику и примеры в формате JSON.

Правила:
- `lemma_accented` — слово с правильным ударением. Для существительных \
добавь артикль: «το νερό», «ο καφές», «η θάλασσα».
- `pos` — часть речи: noun, verb, adjective, adverb, phrase, other.
- Для существительных заполни `article` (ο, η, το) и `gender` (m, f, n). \
Для остальных частей речи оставь их null.
- Для глаголов `verb_form` — первое лицо единственного числа настоящего \
времени: «πίνω», «τρώω». Для остальных null.
- `translation_ru` — короткий русский перевод, одно-три слова, без пояснений \
в скобках и без вариантов через запятую, если можно обойтись одним.
- `examples` — одна-две коротких фразы, которые человек мог бы сказать в \
жизни. Не определения из словаря. Каждый пример с русским переводом.
- `lemma` возвращай ровно в том виде, в каком слово пришло на вход: по нему \
мы сопоставляем ответ со списком.

Отвечай только JSON-объектом, без markdown и без пояснений. Формат:

{"words": [{"lemma": "νερό", "lemma_accented": "το νερό", "pos": "noun", \
"article": "το", "gender": "n", "verb_form": null, "translation_ru": "вода", \
"examples": [{"el": "Θέλω ένα ποτήρι νερό.", "ru": "Хочу стакан воды."}]}]}"""


class Example(BaseModel):
    el: str
    ru: str


class EnrichedWord(BaseModel):
    lemma: str
    """Как слово пришло на вход — ключ сопоставления, а не результат."""
    lemma_accented: str
    translation_ru: str
    pos: PartOfSpeech = PartOfSpeech.OTHER
    article: str | None = None
    gender: Gender | None = None
    verb_form: str | None = None
    examples: list[Example] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return normalize_lemma(self.lemma)


class EnrichResponse(BaseModel):
    words: list[EnrichedWord] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EnrichResult:
    words: dict[str, EnrichedWord]
    """Обогащённые слова по ключу леммы (`normalize_lemma`)."""
    missing: tuple[str, ...]
    """Ключи, про которые модель не ответила: их оставляем как были."""
    usage: Usage

    @property
    def enriched_count(self) -> int:
        return len(self.words)


def _batches(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def build_messages(lemmas: Sequence[str], level: Level) -> list[Message]:
    """Промпт одного вызова.

    Статическая часть (§8.4) — целиком в `system`, чтобы её кэшировал
    провайдер. Всё изменчивое — уровень и сам список — только в `user`.
    """
    listed = "\n".join(f"- {lemma}" for lemma in lemmas)
    return [
        Message("system", SYSTEM_PROMPT),
        Message(
            "user",
            f"Уровень ученика: {level.value}. "
            f"Примеры подбирай под него: не сложнее, но и не из одного слова.\n"
            f"Не более {MAX_EXAMPLES} примеров на слово.\n\n"
            f"Слова ({len(lemmas)}):\n{listed}",
        ),
    ]


def _clean(word: EnrichedWord) -> EnrichedWord:
    """Убрать грамматику, которой у этой части речи не бывает.

    Модель иногда приписывает род прилагательному. Чинить это отказом от
    всей пачки — плохая сделка: остальные девятнадцать слов не виноваты.
    """
    fixes: dict[str, object] = {}
    if word.pos is not PartOfSpeech.NOUN and (word.article or word.gender):
        fixes.update(article=None, gender=None)
    if word.pos is not PartOfSpeech.VERB and word.verb_form:
        fixes.update(verb_form=None)
    if len(word.examples) > MAX_EXAMPLES:
        fixes["examples"] = word.examples[:MAX_EXAMPLES]

    if not fixes:
        return word
    log.info("R2: подчистил лишние поля у «%s» (%s)", word.lemma, ", ".join(fixes))
    return word.model_copy(update=fixes)


async def enrich(
    client: LlmClient,
    *,
    lemmas: Sequence[str],
    level: Level,
) -> EnrichResult:
    """Обогатить список лемм. Ошибка одной партии не роняет остальные.

    Возвращаем то, что получилось, и отдельно — про что модель промолчала.
    Частично обогащённая пачка лучше, чем пачка целиком без грамматики: у
    слова без обогащения `enriched_at` останется пустым, и его можно будет
    догнать повторным вызовом.
    """
    found: dict[str, EnrichedWord] = {}
    usage = Usage()
    failure: LlmError | None = None

    for batch in _batches(lemmas, ENRICH_BATCH):
        try:
            answer, spent = await complete_json(
                client,
                route=Route.ENRICH,
                messages=build_messages(batch, level),
                schema=EnrichResponse,
            )
        except LlmError as error:
            # Партия могла не пройти по своей причине: модель споткнулась на
            # одном слове или провайдер моргнул. Остальные пробуем всё равно.
            log.warning("R2: партия из %s слов не обогатилась (%s)", len(batch), error)
            failure = failure or error
            continue
        usage += spent

        wanted = {normalize_lemma(lemma) for lemma in batch}
        for word in answer.words:
            if word.key not in wanted:
                # Модель придумала слово, которого мы не просили, — не берём:
                # иначе в пачке появится то, чего человек не загружал.
                log.warning("R2: в ответе лишнее слово «%s», пропускаю", word.lemma)
                continue
            found[word.key] = _clean(word)

    if failure is not None and not found:
        # Не прошло вообще ничего — это авария (нет ключа, лежит провайдер), и
        # вызывающий должен сказать человеку правду, а не «обогатил 0 слов».
        raise failure

    missing = tuple(lemma for lemma in lemmas if normalize_lemma(lemma) not in found)
    if missing:
        log.warning("R2: без обогащения осталось %s слов: %s", len(missing), ", ".join(missing))

    return EnrichResult(words=found, missing=missing, usage=usage)


__all__ = [
    "ENRICH_BATCH",
    "MAX_EXAMPLES",
    "SYSTEM_PROMPT",
    "EnrichResponse",
    "EnrichResult",
    "EnrichedWord",
    "Example",
    "build_messages",
    "enrich",
]
