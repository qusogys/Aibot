
import asyncio
import base64
import html
import io
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ID человека, которому разрешены глобальные настройки.
SETTINGS_ADMIN_ID = 8904429775

CONFIG_FILE = "mog_config.json"
DATA_FILE = "mog_data.json"

DEFAULT_MODEL = "gemini-3.5-flash"
GEMINI_TIMEOUT = 120
GEMINI_MAX_RETRIES = 3
GEMINI_SEMAPHORE = asyncio.Semaphore(2)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("mog_ai")

dp = Dispatcher()

# Для ввода настроек одним сообщением.
SETTINGS_INPUT_MODE = {}


# ============================================================
# GLOBAL GEMINI CONFIG
# ============================================================

def default_config():
    return {
        "model": DEFAULT_MODEL,
        "api_keys": [],
        "active_key": 0,
    }


def load_config():
    if not os.path.exists(CONFIG_FILE):
        cfg = default_config()
        save_config(cfg)
        return cfg

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        if not isinstance(cfg, dict):
            cfg = default_config()

        cfg.setdefault("model", DEFAULT_MODEL)
        cfg.setdefault("api_keys", [])
        cfg.setdefault("active_key", 0)

        if not isinstance(cfg["api_keys"], list):
            cfg["api_keys"] = []

        # Удаляем случайные дубликаты, сохраняя порядок.
        unique = []
        for key in cfg["api_keys"]:
            key = str(key).strip()
            if key and key not in unique:
                unique.append(key)
        cfg["api_keys"] = unique

        if not isinstance(cfg["active_key"], int):
            cfg["active_key"] = 0

        if cfg["api_keys"]:
            cfg["active_key"] %= len(cfg["api_keys"])
        else:
            cfg["active_key"] = 0

        return cfg

    except Exception:
        logger.exception("Failed to load Gemini config")
        return default_config()


def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def mask_key(key):
    key = str(key)
    if len(key) <= 4:
        return "••••"
    return "••••" + key[-4:]


def add_api_key(key):
    key = key.strip()
    if not key:
        return False, "Пустой ключ."

    cfg = load_config()

    if key in cfg["api_keys"]:
        return False, "Такой API-ключ уже добавлен."

    cfg["api_keys"].append(key)
    if len(cfg["api_keys"]) == 1:
        cfg["active_key"] = 0

    save_config(cfg)
    return True, mask_key(key)


def delete_api_key(index):
    cfg = load_config()

    if index < 0 or index >= len(cfg["api_keys"]):
        return False, "Ключ не найден."

    removed = cfg["api_keys"].pop(index)

    if cfg["api_keys"]:
        cfg["active_key"] %= len(cfg["api_keys"])
    else:
        cfg["active_key"] = 0

    save_config(cfg)
    return True, mask_key(removed)


def set_model(model):
    model = model.strip()
    model = model.replace("models/", "", 1)

    # Разрешаем только нормальный model id.
    if not re.fullmatch(r"[A-Za-z0-9._:-]{2,100}", model):
        return False

    cfg = load_config()
    cfg["model"] = model
    save_config(cfg)
    return True


# ============================================================
# SETTINGS UI
# ============================================================

def is_settings_admin(user_id):
    return int(user_id) == SETTINGS_ADMIN_ID


def settings_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🤖 Изменить модель", callback_data="set_model")
    b.button(text="➕ Добавить API ключ", callback_data="add_key")
    b.button(text="🗑 Удалить API ключ", callback_data="delete_key")
    b.button(text="🔄 Проверить ключи", callback_data="check_keys")
    b.button(text="🔎 Доступные модели", callback_data="list_models")
    b.button(text="🔁 Сбросить выбор ключа", callback_data="reset_key")
    b.adjust(1)
    return b.as_markup()


def settings_text():
    cfg = load_config()
    keys = cfg["api_keys"]

    if keys:
        lines = []
        for i, key in enumerate(keys):
            marker = "🟢" if i == cfg["active_key"] else "⚪"
            lines.append(f"{marker} {i + 1}. {mask_key(key)}")
        key_text = "\n".join(lines)
    else:
        key_text = "❌ API-ключей пока нет"

    return (
        "<b>⚙️ MOG AI SETTINGS</b>\n\n"
        f"🤖 <b>Модель:</b> <code>{html.escape(cfg['model'])}</code>\n\n"
        f"🔑 <b>API ключи ({len(keys)}):</b>\n"
        f"{key_text}\n\n"
        "🟢 — последний успешно использованный ключ.\n"
        "Если ключ не работает, MOG автоматически попробует следующий."
    )


@dp.message(Command("settings"))
@dp.message(Command("настройки"))
async def settings_command(message: Message):
    if not is_settings_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к глобальным настройкам.")
        return

    await message.answer(settings_text(), reply_markup=settings_keyboard())


@dp.callback_query(F.data == "set_model")
async def settings_set_model(callback: CallbackQuery):
    if not is_settings_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    SETTINGS_INPUT_MODE[callback.from_user.id] = "model"
    await callback.answer()
    await callback.message.answer(
        "🤖 <b>Изменение модели</b>\n\n"
        "Отправь model ID одним сообщением.\n\n"
        "Например:\n"
        "<code>gemini-3.5-flash</code>\n"
        "или\n"
        "<code>gemini-3.5-flash-lite</code>"
    )


@dp.callback_query(F.data == "add_key")
async def settings_add_key(callback: CallbackQuery):
    if not is_settings_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    SETTINGS_INPUT_MODE[callback.from_user.id] = "api_key"
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Добавление Gemini API ключа</b>\n\n"
        "Отправь API-ключ одним сообщением.\n"
        "После сохранения он будет скрыт в меню настроек."
    )


@dp.callback_query(F.data == "delete_key")
async def settings_delete_key(callback: CallbackQuery):
    if not is_settings_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    cfg = load_config()

    if not cfg["api_keys"]:
        await callback.answer("Ключей нет.", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for i, key in enumerate(cfg["api_keys"]):
        b.button(
            text=f"🗑 {i + 1}. {mask_key(key)}",
            callback_data=f"delkey:{i}",
        )
    b.button(text="⬅️ Назад", callback_data="settings_back")
    b.adjust(1)

    await callback.answer()
    await callback.message.answer(
        "Выбери ключ, который удалить:",
        reply_markup=b.as_markup(),
    )


@dp.callback_query(F.data.startswith("delkey:"))
async def settings_delete_key_confirm(callback: CallbackQuery):
    if not is_settings_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Ошибка.", show_alert=True)
        return

    ok, info = delete_api_key(index)
    await callback.answer("Удалено" if ok else info, show_alert=True)

    if ok:
        await callback.message.edit_text(
            settings_text(),
            reply_markup=settings_keyboard(),
        )


@dp.callback_query(F.data == "reset_key")
async def settings_reset_key(callback: CallbackQuery):
    if not is_settings_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    cfg = load_config()
    cfg["active_key"] = 0
    save_config(cfg)
    await callback.answer("Выбор ключа сброшен.")
    await callback.message.edit_text(
        settings_text(),
        reply_markup=settings_keyboard(),
    )


@dp.callback_query(F.data == "settings_back")
async def settings_back(callback: CallbackQuery):
    if not is_settings_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(
        settings_text(),
        reply_markup=settings_keyboard(),
    )


@dp.message(F.text)
async def settings_input_handler(message: Message):
    user_id = message.from_user.id

    if not is_settings_admin(user_id):
        return

    mode = SETTINGS_INPUT_MODE.get(user_id)
    if not mode:
        return

    value = (message.text or "").strip()
    SETTINGS_INPUT_MODE.pop(user_id, None)

    if mode == "api_key":
        ok, info = add_api_key(value)
        if ok:
            await message.answer(
                "✅ <b>API-ключ добавлен.</b>\n\n"
                f"Ключ: <code>{info}</code>\n"
                f"Всего ключей: <b>{len(load_config()['api_keys'])}</b>",
                reply_markup=settings_keyboard(),
            )
        else:
            await message.answer(
                f"❌ {html.escape(info)}",
                reply_markup=settings_keyboard(),
            )
        return

    if mode == "model":
        if set_model(value):
            await message.answer(
                "✅ <b>Модель изменена.</b>\n\n"
                f"Теперь: <code>{html.escape(load_config()['model'])}</code>",
                reply_markup=settings_keyboard(),
            )
        else:
            await message.answer(
                "❌ Неверный model ID.",
                reply_markup=settings_keyboard(),
            )


# ============================================================
# GEMINI MODEL CHECK
# ============================================================

async def list_available_models():
    cfg = load_config()
    if not cfg["api_keys"]:
        raise RuntimeError("Нет Gemini API ключей.")

    errors = []

    for i, key in enumerate(cfg["api_keys"]):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": key},
                    params={"pageSize": 1000},
                )

            if r.status_code == 200:
                data = r.json()
                models = []
                for m in data.get("models", []):
                    name = str(m.get("name", ""))
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods:
                        models.append(name.replace("models/", "", 1))

                if models:
                    cfg["active_key"] = i
                    save_config(cfg)
                    return sorted(models)

            errors.append(f"key #{i + 1}: HTTP {r.status_code}")

        except Exception as e:
            errors.append(f"key #{i + 1}: {type(e).__name__}")

    raise RuntimeError("; ".join(errors))


@dp.callback_query(F.data == "list_models")
async def settings_list_models(callback: CallbackQuery):
    if not is_settings_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.answer("Проверяю...")
    try:
        models = await list_available_models()
        # Сначала самые полезные Flash-модели.
        preferred = [
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash",
        ]
        ordered = [m for m in preferred if m in models]
        ordered += [m for m in models if m not in ordered]

        text = "<b>🔎 Доступные generateContent модели</b>\n\n"
        text += "\n".join(f"• <code>{html.escape(m)}</code>" for m in ordered[:40])
        if len(ordered) > 40:
            text += f"\n\n… и ещё {len(ordered) - 40}"

        await callback.message.answer(text)
    except Exception as e:
        await callback.message.answer(
            "❌ Не удалось получить список моделей:\n"
            f"<code>{html.escape(str(e)[:1500])}</code>"
        )


@dp.callback_query(F.data == "check_keys")
async def settings_check_keys(callback: CallbackQuery):
    if not is_settings_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    cfg = load_config()

    if not cfg["api_keys"]:
        await callback.answer("API ключей нет.", show_alert=True)
        return

    await callback.answer("Проверяю ключи...")

    results = []
    active = None

    async with httpx.AsyncClient(timeout=30) as client:
        for i, key in enumerate(cfg["api_keys"]):
            try:
                r = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": key},
                    params={"pageSize": 1},
                )
                if r.status_code == 200:
                    results.append(f"🟢 KEY #{i + 1} {mask_key(key)} — OK")
                    if active is None:
                        active = i
                elif r.status_code == 429:
                    results.append(f"🟡 KEY #{i + 1} {mask_key(key)} — RATE LIMIT")
                elif r.status_code in (401, 403):
                    results.append(f"🔴 KEY #{i + 1} {mask_key(key)} — AUTH ERROR")
                else:
                    results.append(
                        f"🟠 KEY #{i + 1} {mask_key(key)} — HTTP {r.status_code}"
                    )
            except Exception as e:
                results.append(
                    f"🔴 KEY #{i + 1} {mask_key(key)} — {type(e).__name__}"
                )

    if active is not None:
        cfg["active_key"] = active
        save_config(cfg)

    await callback.message.answer(
        "<b>🔄 Проверка API ключей</b>\n\n" + "\n".join(results)
    )


# ============================================================
# DATABASE
# ============================================================

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "battles": []}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError()
        data.setdefault("users", {})
        data.setdefault("battles", [])
        return data
    except Exception:
        logger.exception("Failed to load database")
        return {"users": {}, "battles": []}


def save_data(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def ensure_user(data, user_id, username):
    user_id = str(user_id)
    if user_id not in data["users"]:
        data["users"][user_id] = {
            "username": username,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "battles": 0,
            "score_sum": 0.0,
        }
    else:
        data["users"][user_id]["username"] = username


def register_battle(player1, player2, result):
    data = load_data()
    ensure_user(data, player1.user_id, player1.username)
    ensure_user(data, player2.user_id, player2.username)

    p1 = data["users"][str(player1.user_id)]
    p2 = data["users"][str(player2.user_id)]

    score1 = result["players"][0]["overall"]
    score2 = result["players"][1]["overall"]

    p1["battles"] += 1
    p2["battles"] += 1
    p1["score_sum"] += score1
    p2["score_sum"] += score2

    if result["winner"] == 0:
        p1["wins"] += 1
        p2["losses"] += 1
    elif result["winner"] == 1:
        p2["wins"] += 1
        p1["losses"] += 1
    else:
        p1["draws"] += 1
        p2["draws"] += 1

    data["battles"].append({
        "time": datetime.now(timezone.utc).isoformat(),
        "player1": player1.username,
        "player2": player2.username,
        "score1": score1,
        "score2": score2,
        "winner": result["winner"],
        "status": result["status"],
    })
    data["battles"] = data["battles"][-500:]
    save_data(data)


# ============================================================
# PROFILE
# ============================================================

class Profile:
    def __init__(self, user_id, username, name, bio, avatar):
        self.user_id = user_id
        self.username = username
        self.name = name
        self.bio = bio
        self.avatar = avatar


async def get_profile(bot: Bot, user_id: int):
    chat = await bot.get_chat(user_id)

    username = "@" + chat.username if chat.username else "no_username"
    name = chat.full_name or "Unknown"
    bio = getattr(chat, "bio", "") or ""
    avatar = None

    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if photos.total_count:
            photo = photos.photos[0][-1]
            telegram_file = await bot.get_file(photo.file_id)
            buffer = io.BytesIO()
            await bot.download_file(telegram_file.file_path, buffer)
            avatar = buffer.getvalue()
    except Exception as e:
        logger.warning("Avatar error for %s: %s", user_id, e)

    return Profile(user_id, username, name, bio, avatar)


# ============================================================
# SCHEMA / SCORING
# ============================================================

WEIGHTS = {
    "name": 0.20,
    "username": 0.20,
    "bio": 0.20,
    "coherence": 0.20,
    "vibe": 0.20,
}

CATEGORIES = [
    ("NAME", "name"),
    ("USERNAME", "username"),
    ("BIO", "bio"),
    ("COHERENCE", "coherence"),
    ("VIBE", "vibe"),
]

GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "players": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "number", "minimum": 0, "maximum": 10},
                    "username": {"type": "number", "minimum": 0, "maximum": 10},
                    "bio": {"type": "number", "minimum": 0, "maximum": 10},
                    "coherence": {"type": "number", "minimum": 0, "maximum": 10},
                    "vibe": {"type": "number", "minimum": 0, "maximum": 10},
                },
                "required": ["name", "username", "bio", "coherence", "vibe"],
            },
        },
        "verdict": {"type": "string"},
    },
    "required": ["players", "verdict"],
}


# ============================================================
# GEMINI
# ============================================================

PROMPT = """
Ты — AI-судья юмористической игры MOG BATTLE.

Сравни два Telegram-профиля.

Оценивай только качество оформления профиля, а не личность человека.

РОВНО ПЯТЬ оценок от 0.0 до 10.0:
1. NAME
2. USERNAME
3. BIO
4. COHERENCE
5. VIBE

NAME: читаемость, стиль, запоминаемость, оригинальность display name.
USERNAME: читаемость, запоминаемость, оригинальность, простота, визуальный стиль.
BIO: качество текста, краткость, оригинальность, характер, оформление.
Если bio нет — не выдумывай его.
COHERENCE: сочетание NAME + USERNAME + BIO + AVATAR.
VIBE: общий стиль, атмосфера, цельность, характер и запоминаемость.

Аватар учитывай только в COHERENCE и VIBE.
Отдельной оценки AVATAR нет.

Не делай выводов о расе, этничности, религии, политических взглядах,
сексуальной ориентации, здоровье, инвалидности, теле, физической
привлекательности, точном возрасте и других чувствительных характеристиках.

Не выдумывай информацию.

Используй весь диапазон 0-10.
Не ставь одинаковые оценки без причины.

Сделай короткий смешной русский вердикт, максимум 180 символов.

Верни ТОЛЬКО JSON по заданной схеме.
"""


def profile_text(profile):
    return (
        f"\nUSERNAME: {profile.username}"
        f"\nNAME: {profile.name}"
        f"\nBIO: {profile.bio or '(нет bio)'}"
    )


def extract_json(text):
    """
    Более надёжный парсер:
    1) обычный JSON;
    2) markdown code fence;
    3) ищем сбалансированный JSON-объект.
    Не используем жадный r'\\{.*\\}'.
    """
    text = (text or "").strip()
    if not text:
        raise RuntimeError("Gemini JSON parsing failed: empty response.")

    candidates = [text]

    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    if cleaned != text:
        candidates.append(cleaned)

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Ищем первый сбалансированный {...}.
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False

        for i in range(start, len(text)):
            ch = text[i]

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    fragment = text[start:i + 1]
                    try:
                        obj = json.loads(fragment)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break

        start = text.find("{", start + 1)

    raise RuntimeError(
        "Gemini JSON parsing failed: no JSON object.\n"
        f"RAW: {text[:2000]}"
    )


def extract_model_text(data):
    candidates = data.get("candidates", [])
    if not candidates:
        block = data.get("promptFeedback") or data.get("error")
        raise RuntimeError(
            "Gemini returned no candidates. "
            + json.dumps(block, ensure_ascii=False)[:1200]
        )

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(
        str(p.get("text", ""))
        for p in parts
        if isinstance(p, dict) and "text" in p
    ).strip()

    if not text:
        raise RuntimeError(
            "Gemini returned empty text. "
            + json.dumps(data, ensure_ascii=False)[:1500]
        )

    return text


async def analyze_with_gemini(player1, player2):
    cfg = load_config()

    if not cfg["api_keys"]:
        raise RuntimeError(
            "Gemini API keys are not configured. "
            "Открой /настройки и добавь хотя бы один ключ."
        )

    model = cfg["model"].replace("models/", "", 1)
    keys = cfg["api_keys"]

    parts = [
        {"text": PROMPT},
        {"text": "\n\nPLAYER 1" + profile_text(player1)},
        {"text": "\n\nPLAYER 2" + profile_text(player2)},
    ]

    if player1.avatar:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(player1.avatar).decode("utf-8"),
            }
        })
        parts.append({"text": "Следующее изображение — аватар PLAYER 1."})
    else:
        parts.append({"text": "PLAYER 1 не имеет аватара."})

    if player2.avatar:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(player2.avatar).decode("utf-8"),
            }
        })
        parts.append({"text": "Следующее изображение — аватар PLAYER 2."})
    else:
        parts.append({"text": "PLAYER 2 не имеет аватара."})

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": GEMINI_RESPONSE_SCHEMA,
            "maxOutputTokens": 1000,
        },
    }

    errors = []

    async with GEMINI_SEMAPHORE:
        async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT) as client:
            # Начинаем с последнего рабочего ключа.
            start = cfg["active_key"] if keys else 0
            order = [(start + i) % len(keys) for i in range(len(keys))]

            for key_index in order:
                key = keys[key_index]

                for attempt in range(GEMINI_MAX_RETRIES):
                    try:
                        response = await client.post(
                            url,
                            headers={
                                "Content-Type": "application/json",
                                "x-goog-api-key": key,
                            },
                            json=payload,
                        )
                    except (
                        httpx.TimeoutException,
                        httpx.ConnectError,
                        httpx.RemoteProtocolError,
                    ) as e:
                        if attempt == GEMINI_MAX_RETRIES - 1:
                            errors.append(
                                f"KEY #{key_index + 1}: network {type(e).__name__}"
                            )
                            break
                        await asyncio.sleep(2 ** attempt)
                        continue

                    if response.status_code == 200:
                        cfg["active_key"] = key_index
                        save_config(cfg)

                        try:
                            data = response.json()
                        except Exception:
                            errors.append(
                                f"KEY #{key_index + 1}: invalid HTTP JSON"
                            )
                            break

                        try:
                            text = extract_model_text(data)
                            result = extract_json(text)
                            return validate_gemini_result(result)
                        except Exception as e:
                            # JSON/model-format error не имеет смысла повторять
                            # тем же ключом много раз: пробуем следующий ключ.
                            errors.append(
                                f"KEY #{key_index + 1}: {str(e)[:500]}"
                            )
                            break

                    # Ключ недействителен или нет доступа.
                    if response.status_code in (400, 401, 403):
                        try:
                            body = response.json()
                            detail = body.get("error", {}).get("message", "")
                        except Exception:
                            detail = response.text[:500]

                        errors.append(
                            f"KEY #{key_index + 1}: HTTP {response.status_code} "
                            f"{detail}"
                        )
                        break

                    # Лимит — пробуем повторить, затем следующий ключ.
                    if response.status_code == 429:
                        if attempt < GEMINI_MAX_RETRIES - 1:
                            retry_after = response.headers.get("Retry-After")
                            try:
                                delay = float(retry_after)
                            except (TypeError, ValueError):
                                delay = 2 ** attempt
                            await asyncio.sleep(min(delay, 15))
                            continue

                        errors.append(
                            f"KEY #{key_index + 1}: HTTP 429 RATE LIMIT"
                        )
                        break

                    # Серверная ошибка.
                    if response.status_code in (500, 502, 503, 504):
                        if attempt < GEMINI_MAX_RETRIES - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue

                        errors.append(
                            f"KEY #{key_index + 1}: HTTP {response.status_code}"
                        )
                        break

                    try:
                        detail = response.json().get("error", {}).get("message", "")
                    except Exception:
                        detail = response.text[:500]

                    errors.append(
                        f"KEY #{key_index + 1}: HTTP {response.status_code} "
                        f"{detail}"
                    )
                    break

    raise RuntimeError(
        "Gemini API error: all configured API keys failed.\n"
        + "\n".join(errors[-len(keys):])
    )


def validate_gemini_result(result):
    if not isinstance(result, dict):
        raise RuntimeError("Gemini result is not an object.")

    players = result.get("players")
    if not isinstance(players, list) or len(players) != 2:
        raise RuntimeError("Gemini returned invalid players data.")

    for player in players:
        if not isinstance(player, dict):
            raise RuntimeError("Gemini returned invalid player data.")

        for key in WEIGHTS:
            try:
                value = float(player.get(key, 0))
            except Exception:
                value = 0.0
            player[key] = max(0.0, min(10.0, value))

    result["verdict"] = str(
        result.get("verdict", "Нет вердикта.")
    )[:300]

    return result


def calculate_scores(ai_result, names):
    players = []

    for raw in ai_result["players"]:
        scores = {
            key: float(max(0, min(10, raw[key])))
            for key in WEIGHTS
        }
        scores["overall"] = round(
            sum(scores[k] * WEIGHTS[k] for k in WEIGHTS), 2
        )
        players.append(scores)

    score1 = players[0]["overall"]
    score2 = players[1]["overall"]
    difference = round(abs(score1 - score2), 2)

    if difference < 0.10:
        winner = loser = None
        status = "DRAW"
    elif score1 > score2:
        winner, loser = 0, 1
        status = (
            "ABSOLUTE MOG" if difference >= 2
            else "DOMINATED" if difference >= 1
            else "MOGGED"
        )
    else:
        winner, loser = 1, 0
        status = (
            "ABSOLUTE MOG" if difference >= 2
            else "DOMINATED" if difference >= 1
            else "MOGGED"
        )

    return {
        "players": players,
        "winner": winner,
        "loser": loser,
        "winner_name": "DRAW" if winner is None else names[winner],
        "difference": difference,
        "status": status,
        "verdict": str(ai_result.get("verdict", "Нет вердикта."))[:300],
    }


# ============================================================
# CARD
# ============================================================

FONT_SIZES = {
    "title": 58, "subtitle": 22, "profile_label": 23,
    "profile_name": 42, "profile_username": 27,
    "overall_label": 22, "overall_score": 36,
    "category": 24, "category_score": 24, "vs": 70,
    "stamp": 56, "status": 35, "winner": 34,
    "difference_label": 23, "difference_score": 31,
    "verdict": 25, "footer": 17,
}

BG = "#09090d"
PANEL = "#111118"
BORDER = "#24242d"
WHITE = "#f4f4f7"
MUTED = "#858591"
YELLOW = "#f4c542"
RED = "#ef3030"
BAR_BG = "#292932"

_FONT_CACHE = {}


def font(key, bold=False):
    ck = (key, bold)
    if ck in _FONT_CACHE:
        return _FONT_CACHE[ck]

    paths = (
        [
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold else
        [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )

    for path in paths:
        if os.path.exists(path):
            obj = ImageFont.truetype(path, FONT_SIZES[key])
            _FONT_CACHE[ck] = obj
            return obj

    obj = ImageFont.load_default()
    _FONT_CACHE[ck] = obj
    return obj


def draw_center(draw, text, x, y, fnt, fill):
    box = draw.textbbox((0, 0), text, font=fnt)
    draw.text((x - (box[2] - box[0]) / 2, y), text, font=fnt, fill=fill)


def truncate(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "…"


def wrap_text(draw, text, fnt, max_width):
    words = str(text or "").split()
    lines, cur = [], ""
    for word in words:
        test = word if not cur else cur + " " + word
        box = draw.textbbox((0, 0), test, font=fnt)
        if box[2] - box[0] <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def make_avatar(data, size=260):
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    if data:
        try:
            av = Image.open(io.BytesIO(data)).convert("RGB")
            w, h = av.size
            side = min(w, h)
            av = av.crop(((w-side)//2, (h-side)//2,
                          (w+side)//2, (h+side)//2))
            av = av.resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            av = Image.new("RGB", (size, size), "#26262f")
    else:
        av = Image.new("RGB", (size, size), "#26262f")

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size-1, size-1), fill=255)
    out.paste(av, (0, 0), mask)
    ImageDraw.Draw(out).ellipse(
        (3, 3, size-4, size-4),
        outline=YELLOW,
        width=6,
    )
    return out


def draw_profile_header(image, draw, player, result, idx, y):
    W = image.width
    cx = W // 2

    draw.rounded_rectangle(
        (70, y, W-70, y+490),
        radius=32,
        fill=PANEL,
        outline=BORDER,
        width=3,
    )

    avatar = make_avatar(player.avatar, 260)
    image.paste(avatar, (cx-130, y+50), avatar)

    label = "HOST PROFILE" if idx == 0 else "GUEST PROFILE"
    lx, ly = cx-122, y+330
    fill = YELLOW if result["winner"] == idx else "#292932"
    color = "#17171b" if result["winner"] == idx else MUTED

    draw.rounded_rectangle((lx, ly, lx+244, ly+48), radius=24, fill=fill)
    draw_center(draw, label, cx, ly+8, font("profile_label", True), color)

    draw_center(
        draw, truncate(player.name, 23), cx, y+395,
        font("profile_name", True), WHITE
    )
    draw_center(
        draw, truncate(player.username, 30), cx, y+445,
        font("profile_username", True), YELLOW
    )


def draw_score_panel(draw, result, idx, y):
    W = draw._image.width
    left, right = 70, W-70

    draw.rounded_rectangle(
        (left, y, right, y+585),
        radius=32,
        fill=PANEL,
        outline=BORDER,
        width=3,
    )

    draw.text(
        (left+38, y+28), "OVERALL SCORE",
        font=font("overall_label", True), fill=MUTED
    )
    draw.text(
        (left+38, y+60),
        f"{result['players'][idx]['overall']:.2f}",
        font=font("overall_score", True), fill=WHITE
    )

    if result["winner"] == idx:
        bx = right-193
        draw.rounded_rectangle(
            (bx, y+27, bx+155, y+75),
            radius=24,
            fill="#403310",
        )
        draw_center(
            draw, "WINNER", bx+77, y+34,
            font("profile_label", True), YELLOW
        )

    first_y = y+145
    for n, (label, key) in enumerate(CATEGORIES):
        ry = first_y+n*82
        score = result["players"][idx][key]

        draw.text(
            (left+38, ry), label,
            font=font("category", True), fill=MUTED
        )

        bar_x, bar_w = 300, 550
        draw.rounded_rectangle(
            (bar_x, ry+4, bar_x+bar_w, ry+30),
            radius=13, fill=BAR_BG
        )

        fill_w = bar_w*score/10
        if fill_w:
            draw.rounded_rectangle(
                (bar_x, ry+4, bar_x+fill_w, ry+30),
                radius=13, fill=YELLOW
            )

        draw.text(
            (885, ry-2), f"{score:.2f}",
            font=font("category_score", True), fill=WHITE
        )


def create_mogged_stamp():
    w, h = 520, 115
    stamp = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(stamp)
    d.rounded_rectangle(
        (4, 4, w-4, h-4),
        radius=18, fill=RED, outline="white", width=5
    )
    draw_center(
        d, "MOGGED", w//2, 22,
        font("stamp", True), "white"
    )
    return stamp.rotate(9, expand=True, resample=Image.Resampling.BICUBIC)


def draw_vs(draw, y):
    W = draw._image.width
    cx = W//2
    line_y = y+58

    draw.line((70, line_y, cx-115, line_y), fill=BORDER, width=3)
    draw.line((cx+115, line_y, W-70, line_y), fill=BORDER, width=3)
    draw.rounded_rectangle(
        (cx-115, y, cx+115, y+115),
        radius=36, fill=BG
    )
    draw_center(draw, "VS", cx, y+8, font("vs", True), WHITE)


def create_card(player1, player2, result):
    W, H = 1100, 2500
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    cx = W//2

    draw_center(draw, "MOG BATTLE", cx, 30, font("title", True), YELLOW)
    draw_center(draw, "AI PROFILE COMPARISON", cx, 105,
                font("subtitle", True), MUTED)

    draw_profile_header(image, draw, player1, result, 0, 150)
    draw_score_panel(draw, result, 0, 670)

    draw_vs(draw, 1280)

    draw_profile_header(image, draw, player2, result, 1, 1415)
    draw_score_panel(draw, result, 1, 1935)

    if result["loser"] is not None:
        stamp = create_mogged_stamp()
        image.paste(
            stamp,
            (cx-stamp.width//2, 2145),
            stamp,
        )

    result_y = 2400
    draw.line((70, result_y, W-70, result_y), fill=BORDER, width=3)

    draw.text(
        (70, result_y+20), result["status"],
        font=font("status", True), fill=RED
    )

    winner_text = (
        "DRAW"
        if result["winner"] is None
        else "WINNER  " + result["winner_name"]
    )
    draw_center(
        draw, winner_text, cx, result_y+65,
        font("winner", True), YELLOW
    )

    draw.text(
        (70, result_y+120), "Разрыв",
        font=font("difference_label", True), fill=MUTED
    )
    draw.text(
        (920, result_y+116),
        f"{result['difference']:.2f}",
        font=font("difference_score", True), fill=YELLOW
    )

    verdict = truncate(result.get("verdict", ""), 150)
    vy = result_y+175
    for line in wrap_text(
        draw, verdict, font("verdict", True), W-130
    )[:2]:
        draw_center(
            draw, line, cx, vy,
            font("verdict", True), WHITE
        )
        vy += 34

    draw_center(
        draw, "MOG AI  •  POWERED BY GEMINI",
        cx, H-45, font("footer", True), MUTED
    )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


# ============================================================
# COMMANDS
# ============================================================

def result_keyboard(p1, p2):
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Rematch",
             callback_data=f"rematch:{p1}:{p2}")
    b.button(text="📊 Details", callback_data="details")
    b.adjust(1)
    return b.as_markup()


async def resolve_target(message: Message, bot: Bot):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.from_user.id, message.reply_to_message.from_user.id

    text = message.text or ""
    m = re.match(
        r"^(\.мог|/mog)(?:\s+@([A-Za-z0-9_]{5,32}))?$",
        text,
        re.I,
    )
    if m and m.group(2):
        try:
            target = await bot.get_chat("@" + m.group(2))
            return message.from_user.id, target.id
        except Exception as e:
            logger.warning("Username lookup failed: %s", e)

    return None


async def run_mog(message, bot, player1_id, player2_id):
    if player1_id == player2_id:
        await message.answer("😐 Себя с собой сравнивать нельзя.")
        return

    status_message = await message.answer(
        "⚔️ <b>MOG BATTLE</b>\n\n"
        "🔎 Получаю профили...\n"
        "🖼 Загружаю аватары...\n"
        "👤 NAME...\n"
        "🔤 USERNAME...\n"
        "📝 BIO...\n"
        "🔗 COHERENCE...\n"
        "✨ VIBE...\n"
        "🧠 Gemini считает оценки..."
    )

    try:
        player1 = await get_profile(bot, player1_id)
        player2 = await get_profile(bot, player2_id)

        ai_result = await analyze_with_gemini(player1, player2)

        result = calculate_scores(
            ai_result,
            [player1.username, player2.username],
        )

        register_battle(player1, player2, result)

        card = create_card(player1, player2, result)

        score1 = result["players"][0]["overall"]
        score2 = result["players"][1]["overall"]

        winner_text = (
            "🤝 <b>DRAW</b>"
            if result["winner"] is None
            else "👑 <b>" + html.escape(result["winner_name"]) + "</b>"
        )

        caption = (
            "⚔️ <b>MOG BATTLE</b>\n\n"
            f"{html.escape(player1.username)} <b>{score1:.2f}/10</b>\n"
            f"{html.escape(player2.username)} <b>{score2:.2f}/10</b>\n\n"
            f"{winner_text}\n"
            f"📊 Difference: <b>{result['difference']:.2f}</b>\n\n"
            f"💬 {html.escape(result['verdict'])}"
        )

        await message.answer_photo(
            BufferedInputFile(card, filename="mog_battle.png"),
            caption=caption,
            reply_markup=result_keyboard(player1_id, player2_id),
        )

        try:
            await status_message.delete()
        except Exception:
            pass

    except Exception as error:
        logger.exception("MOG failed")
        error_text = html.escape(str(error)[:2500])

        try:
            await status_message.edit_text(
                "❌ <b>MOG FAILED</b>\n\n"
                f"<code>{error_text}</code>"
            )
        except Exception:
            await message.answer(
                "❌ <b>MOG FAILED</b>\n\n"
                f"<code>{error_text}</code>"
            )


@dp.message(
    F.text.regexp(
        r"^(\.мог|/mog)(?:\s+@[A-Za-z0-9_]{5,32})?$"
    )
)
async def mog_command(message: Message, bot: Bot):
    target = await resolve_target(message, bot)

    if not target:
        await message.answer(
            "<b>⚔️ MOG AI</b>\n\n"
            "1️⃣ Ответь на сообщение:\n"
            "<code>.мог</code>\n\n"
            "2️⃣ Или:\n"
            "<code>.мог @username</code>"
        )
        return

    await run_mog(message, bot, *target)


@dp.message(Command("start"))
async def start_command(message: Message):
    await message.answer(
        "<b>⚔️ MOG AI</b>\n\n"
        "AI-баттлы Telegram-профилей.\n\n"
        "Ответь на сообщение: <code>.мог</code>\n"
        "Или: <code>.мог @username</code>\n\n"
        "Администратор: <code>/настройки</code>"
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "<b>⚔️ MOG AI — COMMANDS</b>\n\n"
        "<code>.мог</code> — сравнение с человеком из reply.\n"
        "<code>.мог @username</code> — сравнение с username.\n"
        "<code>/stats</code> — статистика.\n"
        "<code>/top</code> — топ.\n"
        "<code>/history</code> — история.\n"
        "<code>/настройки</code> — глобальные настройки (только админ)."
    )


@dp.message(Command("stats"))
async def stats_command(message: Message):
    data = load_data()
    user = data["users"].get(str(message.from_user.id))

    if not user:
        await message.answer("📊 У тебя пока нет MOG-баттлов.")
        return

    battles = user["battles"]
    winrate = user["wins"] / battles * 100 if battles else 0
    average = user["score_sum"] / battles if battles else 0

    await message.answer(
        "<b>📊 YOUR MOG STATS</b>\n\n"
        f"⚔️ Battles: <b>{battles}</b>\n"
        f"🏆 Wins: <b>{user['wins']}</b>\n"
        f"💀 Losses: <b>{user['losses']}</b>\n"
        f"🤝 Draws: <b>{user['draws']}</b>\n\n"
        f"📈 Winrate: <b>{winrate:.1f}%</b>\n"
        f"⭐ Average score: <b>{average:.2f}/10</b>"
    )


@dp.message(Command("top"))
async def top_command(message: Message):
    data = load_data()
    users = []

    for user in data["users"].values():
        if user["battles"] <= 0:
            continue
        users.append({
            "username": user["username"],
            "wins": user["wins"],
            "average": user["score_sum"] / user["battles"],
        })

    users.sort(key=lambda x: (x["wins"], x["average"]), reverse=True)
    users = users[:10]

    if not users:
        await message.answer("🏆 Пока нет статистики.")
        return

    medals = ["🥇", "🥈", "🥉"]
    text = "<b>🏆 MOG TOP 10</b>\n\n"

    for i, user in enumerate(users):
        pos = medals[i] if i < 3 else f"<b>{i+1}.</b>"
        text += (
            f"{pos} {html.escape(str(user['username']))} — "
            f"<b>{user['wins']}</b> wins • {user['average']:.2f}/10\n"
        )

    await message.answer(text)


@dp.message(Command("history"))
async def history_command(message: Message):
    data = load_data()
    battles = list(reversed(data["battles"][-10:]))

    if not battles:
        await message.answer("📜 История пока пустая.")
        return

    text = "<b>📜 LAST MOG BATTLES</b>\n\n"

    for battle in battles:
        if battle["winner"] == 0:
            winner = battle["player1"]
        elif battle["winner"] == 1:
            winner = battle["player2"]
        else:
            winner = "DRAW"

        text += (
            f"⚔️ {html.escape(str(battle['player1']))} "
            f"<b>{battle['score1']:.2f}</b> × "
            f"<b>{battle['score2']:.2f}</b> "
            f"{html.escape(str(battle['player2']))}\n"
            f"🏆 {html.escape(str(winner))}\n\n"
        )

    await message.answer(text)


@dp.callback_query(F.data.startswith("rematch:"))
async def rematch_callback(callback: CallbackQuery, bot: Bot):
    try:
        parts = callback.data.split(":")
        if len(parts) != 3:
            raise ValueError("Invalid callback data")

        p1, p2 = int(parts[1]), int(parts[2])
        await callback.answer("🔄 Новый MOG!")
        await run_mog(callback.message, bot, p1, p2)
    except Exception:
        logger.exception("Rematch failed")
        await callback.answer("❌ Ошибка", show_alert=True)


@dp.callback_query(F.data == "details")
async def details_callback(callback: CallbackQuery):
    await callback.answer(
        "Карточка содержит NAME, USERNAME, BIO, COHERENCE и VIBE.",
        show_alert=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    cfg = load_config()

    logger.info("Starting MOG AI bot...")
    logger.info("Gemini model: %s", cfg["model"])
    logger.info("Gemini API keys configured: %s", len(cfg["api_keys"]))

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        me = await bot.get_me()
        logger.info("Logged in as @%s", me.username)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
