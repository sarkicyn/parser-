from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from vacancy_bot.matcher import classify
from vacancy_bot.profile import Profile


ROOT = Path(__file__).resolve().parent


def demo():
    profile = Profile.load(ROOT / "profile.json")
    posts = json.loads((ROOT / "examples" / "vacancies.json").read_text(encoding="utf-8"))
    results = []
    for post in posts:
        match = classify(post["text"], profile)
        print(f"{post['title']}: {match.verdict} — {match.score}/100")
        for reason in (match.reasons if not match.eligible else match.unknowns):
            print("  " + reason)
        results.append({**post, "analysis": match.to_dict()})
    path = ROOT / "demo-results.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\nПодробные результаты: demo-results.json")


async def telegram_main(args):
    try:
        from telethon import TelegramClient
        from telethon.errors import RPCError
        from vacancy_bot.bot_api import BotAPI, BotError
        from vacancy_bot.config import Config
        from vacancy_bot.folders import folder_title, get_folders
        from vacancy_bot.service import Service
        from vacancy_bot.storage import Storage
    except ImportError:
        raise ValueError("Установите зависимости: python -m pip install -r requirements.txt") from None
    config = Config.load(account_only=args.login or args.folders)
    (ROOT / "data").mkdir(exist_ok=True)
    client = TelegramClient(str(ROOT / "data" / "account"), config.api_id, config.api_hash, flood_sleep_threshold=0)
    bot, db, service = None, None, None
    try:
        if args.login:
            await client.start()
            me = await client.get_me()
            if getattr(me, "bot", False):
                raise ValueError("Войдите в личный Telegram-аккаунт, а не в аккаунт бота")
            print(f"Вход выполнен. Впишите в .env: OWNER_TELEGRAM_ID={me.id}")
            return
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Сначала выполните: python app.py --login")
        if args.folders:
            for folder in await get_folders(client):
                print(f"{folder.id} — {folder_title(folder)}")
            return
        me = await client.get_me()
        if me.id != config.owner or getattr(me, "bot", False):
            raise ValueError("OWNER_TELEGRAM_ID должен совпадать с личным аккаунтом, подключённым через --login")
        bot = BotAPI(config.token)
        db = Storage(ROOT / "data" / "vacancies.sqlite3")
        service = Service(client, bot, db, config)
        await service.run()
    except RPCError as exc:
        raise ValueError("Telegram: " + type(exc).__name__) from None
    except BotError as exc:
        raise ValueError(str(exc) + ". Проверьте BOT_TOKEN и нажмите Start в своём боте.") from None
    finally:
        await client.disconnect()
        if bot:
            await bot.close()
        if db:
            db.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Вакансии из Telegram-папки с оценкой соответствия")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true", help="первый вход в Telegram-аккаунт")
    mode.add_argument("--folders", action="store_true", help="показать папки аккаунта")
    mode.add_argument("--demo", action="store_true", help="проверить фильтр без Telegram и ключей")
    mode.add_argument("--check", action="store_true", help="проверить настройки без подключения к Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        if args.demo:
            demo()
        elif args.check:
            from vacancy_bot.config import Config
            config = Config.load()
            Profile.load(ROOT / "profile.json").validate()
            print("Настройки заполнены. Проверка соединения и ключей произойдёт при запуске.")
            print("Сессия Telegram: " + ("найдена" if (ROOT / "data" / "account.session").exists() else "нужен --login"))
        else:
            asyncio.run(telegram_main(args))
    except (ValueError, OSError, ImportError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПарсер остановлен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
