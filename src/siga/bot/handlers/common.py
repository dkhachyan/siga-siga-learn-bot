"""/help и заглушка на всё остальное."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from siga.bot import texts

router = Router(name="common")


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(texts.HELP, disable_web_page_preview=True)


@router.message()
async def handle_unknown(message: Message) -> None:
    await message.answer(texts.UNKNOWN)
