"""Telegram bot for managing referrers: python -m app.bot (settings: BOT_TOKEN, BOT_ADMIN_IDS)."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from ..config import get_settings
from ..db import init_db
from . import service as svc
from .handlers import notify_loop, setup

log = logging.getLogger("kago.bot")


async def main() -> int:
    s = get_settings()
    if not s.bot_token:
        log.error("BOT_TOKEN не задан")
        return 1
    if not s.admin_ids:
        log.warning("BOT_ADMIN_IDS пуст: управлять реферерами будет некому")
    if not s.vpn_bot_username:
        log.warning("VPN_BOT_USERNAME не задан: приглашённым некуда будет перейти")
    init_db()

    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    setup(dp, s.admin_ids)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Моя статистика и ссылка"),
            BotCommand(command="link", description="Моя реферальная ссылка"),
            BotCommand(command="id", description="Мой Telegram ID"),
        ],
        scope=BotCommandScopeDefault(),
    )
    for admin_id in s.admin_ids:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="menu", description="Управление реферерами"),
                    BotCommand(command="cancel", description="Отменить ввод"),
                    BotCommand(command="id", description="Мой Telegram ID"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as exc:  # the admin has not started the bot yet
            log.info("commands for %s: %s", admin_id, exc)

    me = await bot.get_me()
    await asyncio.to_thread(svc.set_bot_username, me.username)  # referrer links point to this bot
    log.warning("bot @%s started, admins: %s", me.username, sorted(s.admin_ids))
    interval = max(60, s.bot_notify_seconds) if s.bot_notify_seconds > 0 else 0
    tasks = [asyncio.create_task(notify_loop(bot, interval, s.admin_ids))]
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        await bot.session.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("kago").setLevel(logging.INFO)
    sys.exit(asyncio.run(main()))
