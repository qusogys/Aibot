#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот «Барыга» на aiogram 3.x и Google GenAI (Gemini).
Сохранить как main.py и запустить в окружении с TELEGRAM_BOT_TOKEN и GEMINI_API_KEY.
"""

import asyncio
import logging
import os
import html
from typing import Optional

from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, ChatActions
from aiogram.filters import Command

# Попытка импортировать официальный новый клиент google-genai (genai.Client).
# Если его нет, пытаемся использовать старую библиотеку google.generativeai в качестве fallback.
GENAI_CLIENT = None
USE_GENAI_CLIENT = None
try:
    import genai  # type: ignore
    # Ожидаемый интерфейс: genai.Client(api_key=...)
    USE_GENAI_CLIENT = "genai"
except Exception:
    try:
        import google.generativeai as genai_fallback  # type: ignore
        USE_GENAI_CLIENT = "google.generativeai"
    except Exception:
        USE_GENAI_CLIENT = None

# Конфигурация логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Чтение переменных окружения
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN не найден в окружении. Экспортируйте переменную и перезапустите.")
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")

if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY не найден в окружении. Экспортируйте переменную и перезапустите.")
    raise SystemExit("Missing GEMINI_API_KEY")

# Инициализация Telegram бота
bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# Инициализация GenAI клиента в runtime (в async main() получим final client при необходимости)
genai_client = None

# Системная инструкция (роль бота). Передаётся в запрос к Gemini как system prompt.
SYSTEM_PROMPT = (
    "You are a role-play character named \"Барыга\" in a group chat. "
    "Style: street-wise, cheeky, uses slang, but NO profanity or insults. "
    "Always offer some kind of \"выгода\" (benefit), \"сделка\" (deal), tease or answer a question with a question. "
    "Keep answers short (1-3 sentences), natural and lively. "
    "Speak in Russian. Harmless role-play only."
)

async def init_genai_client():
    """
    Инициализация клиента для Google GenAI (Gemini).
    Поддерживаются два варианта импорта (новый genai.Client или старый google.generativeai).
    Возвращает объект-клиент и строку-тип.
    """
    global genai_client, USE_GENAI_CLIENT
    if USE_GENAI_CLIENT == "genai":
        try:
            # Ожидаемый интерфейс: genai.Client(api_key=...)
            genai_client = genai.Client(api_key=GEMINI_API_KEY)  # type: ignore
            logger.info("Используется genai.Client")
            return genai_client
        except Exception as ex:
            logger.exception("Не удалось инициализировать genai.Client: %s", ex)
            # fallthrough to try fallback
    if USE_GENAI_CLIENT == "google.generativeai":
        try:
            import google.generativeai as genai_fallback  # type: ignore
            genai_fallback.configure(api_key=GEMINI_API_KEY)
            genai_client = genai_fallback
            logger.info("Используется google.generativeai (fallback)")
            return genai_client
        except Exception as ex:
            logger.exception("Не удалось инициализировать google.generativeai: %s", ex)
    # Если ничего не получилось, пробуем последний-resort: попытаться импортировать genai динамически
    try:
        import genai as genai_try  # type: ignore
        genai_client = genai_try.Client(api_key=GEMINI_API_KEY)  # type: ignore
        USE_GENAI_CLIENT = "genai"
        logger.info("Динамически инициализирован genai.Client")
        return genai_client
    except Exception as ex:
        logger.exception("Не удалось инициализировать GenAI клиент: %s", ex)
        return None


def extract_text_from_response(resp) -> Optional[str]:
    """
    Универсально извлекает текст из возможных ответов SDK.
    Попытки извлечь в порядке частоты встречаемости полей.
    """
    try:
        # Если это dict-like
        if isinstance(resp, dict):
            # Попробуем разные пути
            if "text" in resp and isinstance(resp["text"], str):
                return resp["text"]
            # Новый формат: output -> [{'content': [{'type': 'output_text', 'text': '...'}], ...}]
            output = resp.get("output") or resp.get("outputs")
            if isinstance(output, list) and output:
                first = output[0]
                content = first.get("content") or first.get("contents")
                if isinstance(content, list) and content:
                    item = content[0]
                    text = item.get("text") or item.get("content") or item.get("payload")
                    if isinstance(text, str):
                        return text
            # Старый google.generativeai может вернуть {'candidates': [{'content': '...'}]}
            candidates = resp.get("candidates")
            if isinstance(candidates, list) and candidates:
                cand = candidates[0]
                if isinstance(cand, dict) and "content" in cand:
                    return cand["content"]
        else:
            # Объект с атрибутами
            if hasattr(resp, "text"):
                return getattr(resp, "text")
            # genai.Client может возвращать объект с .output или .candidates
            if hasattr(resp, "output"):
                out = getattr(resp, "output")
                # если out - list-like
                try:
                    if isinstance(out, (list, tuple)) and out:
                        first = out[0]
                        # попытка найти text внутри
                        if isinstance(first, dict):
                            content = first.get("content")
                            if isinstance(content, list) and content:
                                cont0 = content[0]
                                if isinstance(cont0, dict) and "text" in cont0:
                                    return cont0["text"]
                        if hasattr(first, "content"):
                            c = getattr(first, "content")
                            if isinstance(c, (list, tuple)) and c and hasattr(c[0], "text"):
                                return c[0].text
                except Exception:
                    pass
            if hasattr(resp, "candidates"):
                cand = getattr(resp, "candidates")
                if isinstance(cand, (list, tuple)) and cand:
                    first = cand[0]
                    if isinstance(first, dict) and "content" in first:
                        return first["content"]
                    if hasattr(first, "content"):
                        return first.content
    except Exception:
        logger.exception("Ошибка при извлечении текста из ответа GenAI")
    # В крайнем случае попытаемся привести к строке
    try:
        return str(resp)
    except Exception:
        return None


async def generate_reply_with_gemini(user_text: str) -> str:
    """
    Отправляет запрос в Gemini и возвращает краткий ответ.
    Внутри есть try/except, чтобы бот не падал при ошибках.
    """
    global genai_client
    if genai_client is None:
        await init_genai_client()
    if genai_client is None:
        logger.error("GenAI клиент не инициализирован")
        return "Не могу сейчас подумать — позже дам знать."

    # Сформируем подсказку (prompt). Даём явную инструкцию отвечать 1-3 предложения и в стиле Барыги.
    prompt = (
        SYSTEM_PROMPT + "\n\n"
        "User: «" + user_text + "»\n"
        "Assistant: Ответь коротко (1-3 предложения) по-русски в роли Барыги, предложи выгоду/сделку или подколи, "
        "без мата и оскорблений."
    )

    model_name = "gemini-2.5-flash"
    try:
        # Вариант для нового genai.Client
        if USE_GENAI_CLIENT == "genai":
            # Ожидаемый интерфейс (примерный) — client.generate или client.generate_text
            try:
                # Современные обвязки часто имеют метод generate_text или generate
                if hasattr(genai_client, "generate_text"):
                    resp = genai_client.generate_text(model=model_name, prompt=prompt, max_output_tokens=256, temperature=0.7)
                elif hasattr(genai_client, "generate"):
                    resp = genai_client.generate(model=model_name, prompt=prompt, max_output_tokens=256, temperature=0.7)
                else:
                    # Пробуем более общий метод create (chat-like)
                    if hasattr(genai_client, "chat"):
                        resp = genai_client.chat.create(model=model_name, messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}], max_output_tokens=256, temperature=0.7)
                    else:
                        raise RuntimeError("Не найден подходящий метод в genai.Client")
                text = extract_text_from_response(resp)
                if text:
                    return text.strip()
            except Exception:
                logger.exception("Ошибка вызова genai.Client")
                # fallthrough to fallback below

        # Вариант для google.generativeai
        if USE_GENAI_CLIENT == "google.generativeai" or getattr(genai_client, "__name__", "") == "google.generativeai":
            # genai_client — модуль google.generativeai
            try:
                model = genai_client.GenerativeModel(model_name)
                resp = model.generate_content(prompt)
                text = None
                # объект ответа может иметь .text или .candidates etc.
                if hasattr(resp, "text"):
                    text = resp.text
                else:
                    # попробовать преобразовать в dict-like
                    text = extract_text_from_response(resp)
                if text:
                    return text.strip()
            except Exception:
                logger.exception("Ошибка вызова google.generativeai")
    except Exception:
        logger.exception("Неизвестная ошибка при запросе в Gemini")

    # Если ничего не получилось — вернуть гуманную заглушку
    return "Чёрт, не получилось придумать прямо сейчас — заходи позже за выгодой."

@dp.message.register()
async def handle_all_messages(message: Message):
    """
    Главный обработчик сообщений в групповых чатах.
    Применяет фильтры и, при необходимости, обращается к Gemini и отвечает.
    """
    # Игнорируем свои собственные сообщения
    if message.from_user and message.from_user.is_bot:
        return

    # Получаем username и bot_id при первом использовании
    bot_user = await bot.get_me()
    bot_username = (bot_user.username or "").lower()
    bot_id = bot_user.id

    text = (message.text or message.caption or "")  # caption для медиа
    text_lower = text.lower()

    # Условие реакции: хотя бы одно выполнено:
    # - Сообщение является reply на предыдущее сообщение бота.
    is_reply_to_bot = False
    if message.reply_to_message:
        try:
            reply_from = message.reply_to_message.from_user
            if reply_from and reply_from.is_bot and reply_from.id == bot_id:
                is_reply_to_bot = True
        except Exception:
            is_reply_to_bot = False

    # - Содержит упоминание юзернейма бота (например @baryga_bot)
    mentions_bot = False
    if bot_username and ("@" + bot_username) in text_lower:
        mentions_bot = True
    else:
        # Также проверим entities на тип mention / text_mention
        if message.entities:
            for ent in message.entities:
                if ent.type == "mention":
                    ent_text = text[ent.offset: ent.offset + ent.length].lower()
                    if ent_text == f"@{bot_username}":
                        mentions_bot = True
                        break

    # - Содержит ключевое слово "барыга" (в любом регистре)
    contains_keyword = "барыга" in text_lower

    # Если ни одного условия нет — молчим
    if not (is_reply_to_bot or mentions_bot or contains_keyword):
        return

    # Отправляем действие "печатает..."
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatActions.TYPING)
    except Exception:
        # Не критично, продолжаем
        logger.debug("Не удалось отправить chat action typing")

    # Подготовка prompt: можно передать весь текст сообщения
    user_text = text.strip() or "..."  # если пустой текст — placeholder

    # Получаем ответ от Gemini (в try/except внутри)
    try:
        reply_text = await generate_reply_with_gemini(user_text)
    except Exception:
        logger.exception("Ошибка при генерации ответа")
        reply_text = "Не могу сейчас ответить — попробуй позже."

    # Отправка реплая на сообщение пользователя
    try:
        # Экранируем HTML, но можно оставить как есть. Используем reply=True
        await message.reply(html.escape(reply_text), reply=True)
    except Exception:
        logger.exception("Не удалось отправить ответ в чат")

@dp.message.register(Command(commands=["start"]))
async def cmd_start(message: Message):
    await message.reply("Я — Барыга. Пиши мне @{} или реплай на моё сообщение, и я предложу тебе сделку.".format((await bot.get_me()).username))

async def main():
    # Инициализируем genai клиент заранее
    await init_genai_client()
    # Получим bot info (кешируется в handlers при обращении, но на всякий случай)
    await bot.get_me()
    logger.info("Бот запущен. Начинаю polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Выключение бота")
