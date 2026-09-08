"""/start — первое касание и регистрация."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from siga.bot import texts
from siga.db import users

log = logging.getLogger(__name__)

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:  # апдейты из каналов — не наш случай
        return

    user, created = await users.get_or_create(
        session,
        tg_user_id=message.from_user.id,
        tg_username=message.from_user.username,
    )

    if created:
        log.info("новый пользователь id=%s tg=%s", user.id, user.tg_user_id)

    await message.answer(texts.START_NEW if created else texts.START_BACK)
