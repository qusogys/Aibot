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
from aiogram.types import (
    Message,
    BufferedInputFile,
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ЕДИНСТВЕННЫЙ человек, который может менять Gemini settings
SETTINGS_ADMIN_ID = 8904429775

SETTINGS_FILE = "mog_settings.json"
DATA_FILE = "mog_data.json"

DEFAULT_MODEL = "gemini-2.5-flash"

GEMINI_TIMEOUT = 120
GEMINI_MAX_RETRIES_PER_KEY = 2

GEMINI_SEMAPHORE = asyncio.Semaphore(2)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("mog_ai")

dp = Dispatcher()


# ============================================================
# SETTINGS
# ============================================================

settings_lock = asyncio.Lock()


def default_settings():
    return {
        "gemini_keys": [],
        "gemini_model": DEFAULT_MODEL,
        "active_key": 0,
    }


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return default_settings()

    try:
        with open(
            SETTINGS_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return default_settings()

        data.setdefault("gemini_keys", [])
        data.setdefault("gemini_model", DEFAULT_MODEL)
        data.setdefault("active_key", 0)

        if not isinstance(data["gemini_keys"], list):
            data["gemini_keys"] = []

        # Удаляем пустые ключи
        data["gemini_keys"] = [
            str(key).strip()
            for key in data["gemini_keys"]
            if str(key).strip()
        ]

        if not data["gemini_model"]:
            data["gemini_model"] = DEFAULT_MODEL

        if not isinstance(data["active_key"], int):
            data["active_key"] = 0

        if data["gemini_keys"]:
            data["active_key"] %= len(data["gemini_keys"])
        else:
            data["active_key"] = 0

        return data

    except Exception:
        logger.exception("Failed to load settings")
        return default_settings()


def save_settings(data):
    temporary_file = SETTINGS_FILE + ".tmp"

    with open(
        temporary_file,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temporary_file,
        SETTINGS_FILE,
    )


def is_settings_admin(message: Message):
    return (
        message.from_user
        and
        message.from_user.id == SETTINGS_ADMIN_ID
    )


def mask_key(key):
    key = str(key)

    if len(key) <= 10:
        return "••••••••"

    return (
        key[:6]
        +
        "..."
        +
        key[-4:]
    )


def get_gemini_config():
    data = load_settings()

    keys = data.get("gemini_keys", [])
    model = str(
        data.get(
            "gemini_model",
            DEFAULT_MODEL,
        )
    ).strip()

    if not model:
        model = DEFAULT_MODEL

    return keys, model


# ============================================================
# SETTINGS KEYBOARD
# ============================================================

def settings_keyboard():
    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔑 Добавить API key",
        callback_data="settings:add_key",
    )

    builder.button(
        text="📋 API keys",
        callback_data="settings:list_keys",
    )

    builder.button(
        text="🗑 Удалить API key",
        callback_data="settings:delete_key",
    )

    builder.button(
        text="🤖 Изменить модель",
        callback_data="settings:model",
    )

    builder.button(
        text="🔄 Проверить ключи",
        callback_data="settings:test_keys",
    )

    builder.button(
        text="❌ Закрыть",
        callback_data="settings:close",
    )

    builder.adjust(
        1,
        1,
        1,
        1,
        1,
        1,
    )

    return builder.as_markup()


# ============================================================
# SETTINGS STATE
# ============================================================

settings_waiting = {}


# ============================================================
# DATABASE
# ============================================================

def load_data():
    if not os.path.exists(DATA_FILE):
        return {
            "users": {},
            "battles": [],
        }

    try:
        with open(
            DATA_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {
                "users": {},
                "battles": [],
            }

        data.setdefault("users", {})
        data.setdefault("battles", [])

        return data

    except Exception:
        logger.exception("Failed to load database")

        return {
            "users": {},
            "battles": [],
        }


def save_data(data):
    temporary_file = DATA_FILE + ".tmp"

    try:
        with open(
            temporary_file,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(
            temporary_file,
            DATA_FILE,
        )

    except Exception:
        logger.exception("Failed to save database")


def ensure_user(
    data,
    user_id,
    username,
):
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


def register_battle(
    player1,
    player2,
    result,
):
    data = load_data()

    ensure_user(
        data,
        player1.user_id,
        player1.username,
    )

    ensure_user(
        data,
        player2.user_id,
        player2.username,
    )

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

    battle = {
        "time": datetime.now(
            timezone.utc
        ).isoformat(),
        "player1": player1.username,
        "player2": player2.username,
        "score1": score1,
        "score2": score2,
        "winner": result["winner"],
        "status": result["status"],
    }

    data["battles"].append(battle)
    data["battles"] = data["battles"][-500:]

    save_data(data)


# ============================================================
# PROFILE
# ============================================================

class Profile:

    def __init__(
        self,
        user_id,
        username,
        name,
        bio,
        avatar,
    ):
        self.user_id = user_id
        self.username = username
        self.name = name
        self.bio = bio
        self.avatar = avatar


async def get_profile(
    bot: Bot,
    user_id: int,
):
    chat = await bot.get_chat(user_id)

    username = (
        "@"
        + chat.username
        if chat.username
        else "no_username"
    )

    name = (
        chat.full_name
        or "Unknown"
    )

    bio = (
        getattr(
            chat,
            "bio",
            "",
        )
        or ""
    )

    avatar = None

    try:
        photos = await bot.get_user_profile_photos(
            user_id=user_id,
            limit=1,
        )

        if photos.total_count:
            photo = photos.photos[0][-1]

            telegram_file = await bot.get_file(
                photo.file_id,
            )

            buffer = io.BytesIO()

            await bot.download_file(
                telegram_file.file_path,
                buffer,
            )

            avatar = buffer.getvalue()

    except Exception as error:
        logger.warning(
            "Avatar error for %s: %s",
            user_id,
            error,
        )

    return Profile(
        user_id=user_id,
        username=username,
        name=name,
        bio=bio,
        avatar=avatar,
    )


# ============================================================
# GEMINI SCHEMA
# ============================================================

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
                    "name": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "username": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "bio": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "coherence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "vibe": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                    },
                },
                "required": [
                    "name",
                    "username",
                    "bio",
                    "coherence",
                    "vibe",
                ],
            },
        },
        "verdict": {
            "type": "string",
        },
    },
    "required": [
        "players",
        "verdict",
    ],
}


# ============================================================
# GEMINI JSON EXTRACTION
# ============================================================

def extract_json_object(text):
    text = str(text or "").strip()

    if not text:
        raise RuntimeError(
            "Gemini returned empty text."
        )

    # Сначала пробуем чистый JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Убираем markdown fences
    cleaned = re.sub(
        r"```(?:json)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    cleaned = cleaned.rstrip("`").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Ищем объект по балансировке скобок
    start = cleaned.find("{")

    if start == -1:
        raise RuntimeError(
            "Gemini JSON parsing failed: no JSON object."
        )

    depth = 0
    in_string = False
    escaped = False

    for index in range(
        start,
        len(cleaned),
    ):
        char = cleaned[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True

        elif char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                candidate = cleaned[
                    start:index + 1
                ]

                try:
                    return json.loads(
                        candidate
                    )
                except json.JSONDecodeError:
                    break

    raise RuntimeError(
        "Gemini JSON parsing failed: no valid JSON object."
    )


# ============================================================
# GEMINI SINGLE REQUEST
# ============================================================

async def gemini_request(
    api_key,
    model,
    player1,
    player2,
):
    prompt = """
Ты — AI-судья юмористической игры MOG BATTLE.

Сравни два Telegram-профиля.

Оценивай исключительно оформление профиля,
а не личность человека.

Нужно РОВНО ПЯТЬ оценок:

1. NAME
2. USERNAME
3. BIO
4. COHERENCE
5. VIBE

Каждая оценка от 0.0 до 10.0.

NAME:
качество отображаемого имени, читаемость,
стиль, оригинальность и запоминаемость.

USERNAME:
читаемость, запоминаемость, оригинальность,
простота и визуальный стиль.

BIO:
качество текста, краткость, оригинальность,
характер и оформление.

Если bio отсутствует — не выдумывай его.

COHERENCE:
сочетание NAME + USERNAME + BIO + AVATAR.

VIBE:
общая атмосфера, стиль, цельность,
характер и запоминаемость профиля.

Аватар учитывай только в COHERENCE и VIBE.

Не оценивай и не делай выводы о:
расе, этничности, религии, политике,
сексуальной ориентации, здоровье,
инвалидности, теле, физической привлекательности,
точном возрасте или других чувствительных
персональных характеристиках.

Не выдумывай информацию.

Используй диапазон 0-10.

Сделай короткий смешной русский вердикт,
максимум 180 символов.

Верни ТОЛЬКО JSON по схеме.
"""

    def profile_text(profile):
        return (
            f"\nUSERNAME: {profile.username}"
            f"\nNAME: {profile.name}"
            f"\nBIO: {profile.bio or '(нет bio)'}"
        )

    parts = [
        {
            "text": prompt,
        },
        {
            "text": (
                "\n\nPLAYER 1"
                +
                profile_text(player1)
            ),
        },
        {
            "text": (
                "\n\nPLAYER 2"
                +
                profile_text(player2)
            ),
        },
    ]

    if player1.avatar:
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(
                        player1.avatar
                    ).decode("utf-8"),
                },
            }
        )

        parts.append(
            {
                "text": (
                    "Это изображение — "
                    "AVATAR PLAYER 1."
                ),
            }
        )
    else:
        parts.append(
            {
                "text": (
                    "PLAYER 1 не имеет аватара."
                ),
            }
        )

    if player2.avatar:
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(
                        player2.avatar
                    ).decode("utf-8"),
                },
            }
        )

        parts.append(
            {
                "text": (
                    "Это изображение — "
                    "AVATAR PLAYER 2."
                ),
            }
        )
    else:
        parts.append(
            {
                "text": (
                    "PLAYER 2 не имеет аватара."
                ),
            }
        )

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": parts,
            },
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": GEMINI_RESPONSE_SCHEMA,
            "maxOutputTokens": 1000,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    async with httpx.AsyncClient(
        timeout=GEMINI_TIMEOUT,
    ) as client:

        response = await client.post(
            url,
            headers=headers,
            json=payload,
        )

    if response.status_code != 200:
        error_body = response.text[:2000]

        raise RuntimeError(
            f"Gemini API error {response.status_code}: "
            f"{error_body}"
        )

    try:
        data = response.json()
    except Exception as error:
        raise RuntimeError(
            "Gemini returned invalid HTTP JSON."
        ) from error

    candidates = data.get(
        "candidates",
        [],
    )

    if not candidates:
        raise RuntimeError(
            "Gemini returned no candidates."
        )

    content = candidates[0].get(
        "content",
        {},
    )

    response_parts = content.get(
        "parts",
        [],
    )

    text_parts = []

    for part in response_parts:
        if "text" in part:
            text_parts.append(
                str(part["text"])
            )

    text = "".join(
        text_parts
    ).strip()

    if not text:
        raise RuntimeError(
            "Gemini returned empty response."
        )

    logger.info(
        "Gemini raw response: %s",
        text[:1500],
    )

    result = extract_json_object(text)

    if not isinstance(result, dict):
        raise RuntimeError(
            "Gemini result is not an object."
        )

    return result


# ============================================================
# GEMINI WITH FALLBACK KEYS
# ============================================================

async def analyze_with_gemini(
    player1,
    player2,
):
    keys, model = get_gemini_config()

    if not keys:
        raise RuntimeError(
            "Нет Gemini API keys. "
            "Администратор должен добавить ключ "
            "через /settings."
        )

    errors = []

    async with GEMINI_SEMAPHORE:

        settings = load_settings()

        start_index = settings.get(
            "active_key",
            0,
        )

        if start_index >= len(keys):
            start_index = 0

        ordered_keys = (
            keys[start_index:]
            +
            keys[:start_index]
        )

        for key_index, api_key in enumerate(
            ordered_keys
        ):
            for attempt in range(
                GEMINI_MAX_RETRIES_PER_KEY
            ):
                try:
                    logger.info(
                        "Gemini request using key %s/%s, model=%s",
                        key_index + 1,
                        len(ordered_keys),
                        model,
                    )

                    result = await gemini_request(
                        api_key,
                        model,
                        player1,
                        player2,
                    )

                    # Успешный ключ становится активным
                    original_index = (
                        start_index
                        +
                        key_index
                    ) % len(keys)

                    settings["active_key"] = (
                        original_index
                    )

                    save_settings(settings)

                    return result

                except Exception as error:
                    error_text = str(error)

                    logger.warning(
                        "Gemini key %s attempt %s failed: %s",
                        key_index + 1,
                        attempt + 1,
                        error_text,
                    )

                    errors.append(
                        f"key {key_index + 1}: "
                        f"{error_text[:300]}"
                    )

                    # Если это обычная ошибка — пробуем ещё раз
                    if attempt < (
                        GEMINI_MAX_RETRIES_PER_KEY - 1
                    ):
                        await asyncio.sleep(
                            2 ** attempt
                        )

            # Переходим к следующему ключу
            continue

    raise RuntimeError(
        "Все Gemini API keys не сработали.\n"
        +
        "\n".join(errors[-10:])
    )


# ============================================================
# SCORE
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


def calculate_scores(
    ai_result,
    names,
):
    raw_players = ai_result.get(
        "players",
        [],
    )

    if not isinstance(
        raw_players,
        list,
    ) or len(raw_players) != 2:
        raise RuntimeError(
            "Gemini returned invalid players data."
        )

    players = []

    for raw in raw_players:
        if not isinstance(raw, dict):
            raise RuntimeError(
                "Invalid player score data."
            )

        scores = {}

        for key in WEIGHTS:
            try:
                value = float(
                    raw.get(
                        key,
                        0,
                    )
                )
            except Exception:
                value = 0.0

            scores[key] = max(
                0.0,
                min(
                    10.0,
                    value,
                ),
            )

        overall = sum(
            scores[key] * WEIGHTS[key]
            for key in WEIGHTS
        )

        scores["overall"] = round(
            overall,
            2,
        )

        players.append(scores)

    score1 = players[0]["overall"]
    score2 = players[1]["overall"]

    difference = round(
        abs(score1 - score2),
        2,
    )

    if difference < 0.10:
        winner = None
        loser = None
        status = "DRAW"

    elif score1 > score2:
        winner = 0
        loser = 1

        if difference >= 2:
            status = "ABSOLUTE MOG"
        elif difference >= 1:
            status = "DOMINATED"
        else:
            status = "MOGGED"

    else:
        winner = 1
        loser = 0

        if difference >= 2:
            status = "ABSOLUTE MOG"
        elif difference >= 1:
            status = "DOMINATED"
        else:
            status = "MOGGED"

    winner_name = (
        "DRAW"
        if winner is None
        else names[winner]
    )

    return {
        "players": players,
        "winner": winner,
        "loser": loser,
        "winner_name": winner_name,
        "difference": difference,
        "status": status,
        "verdict": str(
            ai_result.get(
                "verdict",
                "Нет вердикта.",
            )
        )[:300],
    }


# ============================================================
# FONTS
# ============================================================

FONT_SIZES = {
    "title": 58,
    "subtitle": 22,
    "profile_label": 23,
    "profile_name": 42,
    "profile_username": 27,
    "overall_label": 22,
    "overall_score": 36,
    "category": 24,
    "category_score": 24,
    "vs": 70,
    "stamp": 56,
    "status": 35,
    "winner": 34,
    "difference_label": 23,
    "difference_score": 31,
    "verdict": 25,
    "footer": 17,
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


def get_font(
    key,
    bold=False,
):
    cache_key = (
        key,
        bold,
    )

    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]

    size = FONT_SIZES[key]

    if bold:
        paths = [
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
    else:
        paths = [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]

    for path in paths:
        if os.path.exists(path):
            font = ImageFont.truetype(
                path,
                int(size),
            )

            _FONT_CACHE[cache_key] = font
            return font

    font = ImageFont.load_default()

    _FONT_CACHE[cache_key] = font

    return font


def f(
    key,
    bold=False,
):
    return get_font(
        key,
        bold,
    )


# ============================================================
# DRAW HELPERS
# ============================================================

def truncate_text(
    text,
    max_chars,
):
    text = str(text or "")

    if len(text) <= max_chars:
        return text

    return (
        text[:max_chars - 1]
        +
        "…"
    )


def draw_center(
    draw,
    text,
    x,
    y,
    font_obj,
    fill,
):
    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font_obj,
    )

    width = bbox[2] - bbox[0]

    draw.text(
        (
            x - width / 2,
            y,
        ),
        text,
        font=font_obj,
        fill=fill,
    )


def wrap_text(
    draw,
    text,
    font_obj,
    max_width,
):
    words = str(text or "").split()

    if not words:
        return [""]

    lines = []
    current = ""

    for word in words:
        candidate = (
            word
            if not current
            else current + " " + word
        )

        bbox = draw.textbbox(
            (0, 0),
            candidate,
            font=font_obj,
        )

        width = bbox[2] - bbox[0]

        if width <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)

            current = word

    if current:
        lines.append(current)

    return lines


# ============================================================
# AVATAR
# ============================================================

def make_avatar(
    data,
    size=260,
):
    result = Image.new(
        "RGBA",
        (size, size),
        (0, 0, 0, 0),
    )

    if data:
        try:
            avatar = Image.open(
                io.BytesIO(data)
            ).convert("RGB")

            width, height = avatar.size
            side = min(
                width,
                height,
            )

            left = (
                width - side
            ) // 2

            top = (
                height - side
            ) // 2

            avatar = avatar.crop(
                (
                    left,
                    top,
                    left + side,
                    top + side,
                )
            )

            avatar = avatar.resize(
                (size, size),
                Image.Resampling.LANCZOS,
            )

        except Exception:
            avatar = Image.new(
                "RGB",
                (size, size),
                "#26262f",
            )
    else:
        avatar = Image.new(
            "RGB",
            (size, size),
            "#26262f",
        )

    mask = Image.new(
        "L",
        (size, size),
        0,
    )

    mask_draw = ImageDraw.Draw(mask)

    mask_draw.ellipse(
        (
            0,
            0,
            size - 1,
            size - 1,
        ),
        fill=255,
    )

    result.paste(
        avatar,
        (0, 0),
        mask,
    )

    border = ImageDraw.Draw(result)

    border.ellipse(
        (
            3,
            3,
            size - 4,
            size - 4,
        ),
        outline=YELLOW,
        width=6,
    )

    return result


def draw_crown(
    draw,
    center_x,
    y,
):
    points = [
        (center_x - 80, y + 50),
        (center_x - 65, y - 15),
        (center_x - 22, y + 22),
        (center_x, y - 30),
        (center_x + 22, y + 22),
        (center_x + 65, y - 15),
        (center_x + 80, y + 50),
    ]

    draw.polygon(
        points,
        fill=YELLOW,
    )

    draw.rounded_rectangle(
        (
            center_x - 80,
            y + 40,
            center_x + 80,
            y + 67,
        ),
        radius=7,
        fill=YELLOW,
    )


# ============================================================
# CARD
# ============================================================

def draw_profile_header(
    image,
    draw,
    player,
    result,
    player_index,
    y,
):
    W = image.width
    center_x = W // 2

    panel_left = 70
    panel_right = W - 70
    panel_h = 490

    draw.rounded_rectangle(
        (
            panel_left,
            y,
            panel_right,
            y + panel_h,
        ),
        radius=32,
        fill=PANEL,
        outline=BORDER,
        width=3,
    )

    if result["winner"] == player_index:
        draw_crown(
            draw,
            center_x,
            y + 45,
        )

    avatar_size = 260

    avatar = make_avatar(
        player.avatar,
        avatar_size,
    )

    avatar_x = (
        center_x
        -
        avatar_size // 2
    )

    avatar_y = y + 50

    image.paste(
        avatar,
        (
            avatar_x,
            avatar_y,
        ),
        avatar,
    )

    label = (
        "HOST PROFILE"
        if player_index == 0
        else "GUEST PROFILE"
    )

    label_font = f(
        "profile_label",
        True,
    )

    label_box_w = 245
    label_box_h = 48

    label_x = (
        center_x
        -
        label_box_w // 2
    )

    label_y = y + 330

    label_fill = (
        YELLOW
        if result["winner"] == player_index
        else "#292932"
    )

    label_color = (
        "#17171b"
        if result["winner"] == player_index
        else MUTED
    )

    draw.rounded_rectangle(
        (
            label_x,
            label_y,
            label_x + label_box_w,
            label_y + label_box_h,
        ),
        radius=24,
        fill=label_fill,
    )

    draw_center(
        draw,
        label,
        center_x,
        label_y + 8,
        label_font,
        label_color,
    )

    draw_center(
        draw,
        truncate_text(
            player.name,
            23,
        ),
        center_x,
        y + 395,
        f("profile_name", True),
        WHITE,
    )

    draw_center(
        draw,
        truncate_text(
            player.username,
            30,
        ),
        center_x,
        y + 445,
        f("profile_username", True),
        YELLOW,
    )


def draw_score_panel(
    draw,
    result,
    player_index,
    y,
):
    W = draw._image.width

    panel_left = 70
    panel_right = W - 70
    panel_h = 585

    draw.rounded_rectangle(
        (
            panel_left,
            y,
            panel_right,
            y + panel_h,
        ),
        radius=32,
        fill=PANEL,
        outline=BORDER,
        width=3,
    )

    draw.text(
        (
            panel_left + 38,
            y + 28,
        ),
        "OVERALL SCORE",
        font=f(
            "overall_label",
            True,
        ),
        fill=MUTED,
    )

    overall = result[
        "players"
    ][player_index]["overall"]

    draw.text(
        (
            panel_left + 38,
            y + 60,
        ),
        f"{overall:.2f}",
        font=f(
            "overall_score",
            True,
        ),
        fill=WHITE,
    )

    if result["winner"] == player_index:
        badge_w = 155
        badge_h = 48

        badge_x = (
            panel_right
            -
            badge_w
            -
            38
        )

        badge_y = y + 27

        draw.rounded_rectangle(
            (
                badge_x,
                badge_y,
                badge_x + badge_w,
                badge_y + badge_h,
            ),
            radius=24,
            fill="#403310",
        )

        draw_center(
            draw,
            "WINNER",
            badge_x + badge_w / 2,
            badge_y + 7,
            f(
                "profile_label",
                True,
            ),
            YELLOW,
        )

    label_x = panel_left + 38
    bar_x = 300
    bar_width = 550
    score_x = 885

    first_y = y + 145
    row_gap = 82

    for index, (
        label,
        key,
    ) in enumerate(CATEGORIES):

        row_y = (
            first_y
            +
            index * row_gap
        )

        score = result[
            "players"
        ][player_index][key]

        draw.text(
            (
                label_x,
                row_y,
            ),
            label,
            font=f(
                "category",
                True,
            ),
            fill=MUTED,
        )

        bar_y = row_y + 4
        bar_h = 26

        draw.rounded_rectangle(
            (
                bar_x,
                bar_y,
                bar_x + bar_width,
                bar_y + bar_h,
            ),
            radius=13,
            fill=BAR_BG,
        )

        fill_w = (
            bar_width
            *
            score
            /
            10
        )

        if fill_w > 0:
            draw.rounded_rectangle(
                (
                    bar_x,
                    bar_y,
                    bar_x + fill_w,
                    bar_y + bar_h,
                ),
                radius=13,
                fill=YELLOW,
            )

        draw.text(
            (
                score_x,
                row_y - 2,
            ),
            f"{score:.2f}",
            font=f(
                "category_score",
                True,
            ),
            fill=WHITE,
        )


def draw_vs(
    draw,
    y,
):
    W = draw._image.width
    center_x = W // 2

    line_y = y + 58

    draw.line(
        (
            70,
            line_y,
            center_x - 115,
            line_y,
        ),
        fill=BORDER,
        width=3,
    )

    draw.line(
        (
            center_x + 115,
            line_y,
            W - 70,
            line_y,
        ),
        fill=BORDER,
        width=3,
    )

    draw.rounded_rectangle(
        (
            center_x - 115,
            y,
            center_x + 115,
            y + 115,
        ),
        radius=36,
        fill=BG,
    )

    draw_center(
        draw,
        "VS",
        center_x,
        y + 8,
        f(
            "vs",
            True,
        ),
        WHITE,
    )


def create_mogged_stamp():
    width = 520
    height = 115

    stamp = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(stamp)

    draw.rounded_rectangle(
        (
            4,
            4,
            width - 4,
            height - 4,
        ),
        radius=18,
        fill=RED,
        outline="white",
        width=5,
    )

    draw_center(
        draw,
        "MOGGED",
        width // 2,
        22,
        f(
            "stamp",
            True,
        ),
        "white",
    )

    return stamp.rotate(
        9,
        expand=True,
        resample=Image.Resampling.BICUBIC,
    )


def create_card(
    player1,
    player2,
    result,
):
    W = 1100
    H = 2500

    image = Image.new(
        "RGB",
        (W, H),
        BG,
    )

    draw = ImageDraw.Draw(image)

    center_x = W // 2

    draw_center(
        draw,
        "MOG BATTLE",
        center_x,
        30,
        f(
            "title",
            True,
        ),
        YELLOW,
    )

    draw_center(
        draw,
        "AI PROFILE COMPARISON",
        center_x,
        105,
        f(
            "subtitle",
            True,
        ),
        MUTED,
    )

    draw_profile_header(
        image,
        draw,
        player1,
        result,
        0,
        150,
    )

    draw_score_panel(
        draw,
        result,
        0,
        670,
    )

    draw_vs(
        draw,
        1280,
    )

    draw_profile_header(
        image,
        draw,
        player2,
        result,
        1,
        1415,
    )

    draw_score_panel(
        draw,
        result,
        1,
        1935,
    )

    if result["loser"] is not None:
        stamp = create_mogged_stamp()

        stamp_x = (
            center_x
            -
            stamp.width // 2
        )

        image.paste(
            stamp,
            (
                stamp_x,
                2145,
            ),
            stamp,
        )

    result_y = 2400

    draw.line(
        (
            70,
            result_y,
            W - 70,
            result_y,
        ),
        fill=BORDER,
        width=3,
    )

    draw.text(
        (
            70,
            result_y + 20,
        ),
        result["status"],
        font=f(
            "status",
            True,
        ),
        fill=RED,
    )

    winner_text = (
        "DRAW"
        if result["winner"] is None
        else (
            "WINNER  "
            +
            result["winner_name"]
        )
    )

    draw_center(
        draw,
        winner_text,
        center_x,
        result_y + 65,
        f(
            "winner",
            True,
        ),
        YELLOW,
    )

    draw.text(
        (
            70,
            result_y + 120,
        ),
        "Разрыв",
        font=f(
            "difference_label",
            True,
        ),
        fill=MUTED,
    )

    draw.text(
        (
            920,
            result_y + 116,
        ),
        f"{result['difference']:.2f}",
        font=f(
            "difference_score",
            True,
        ),
        fill=YELLOW,
    )

    verdict = truncate_text(
        result.get(
            "verdict",
            "",
        ).strip(),
        150,
    )

    verdict_lines = wrap_text(
        draw,
        verdict,
        f(
            "verdict",
            True,
        ),
        W - 130,
    )

    verdict_y = result_y + 175

    for line in verdict_lines[:2]:
        draw_center(
            draw,
            line,
            center_x,
            verdict_y,
            f(
                "verdict",
                True,
            ),
            WHITE,
        )

        verdict_y += 34

    draw_center(
        draw,
        "MOG AI  •  POWERED BY GEMINI",
        center_x,
        H - 45,
        f(
            "footer",
            True,
        ),
        MUTED,
    )

    output = io.BytesIO()

    image.save(
        output,
        format="PNG",
        optimize=True,
    )

    return output.getvalue()


# ============================================================
# RESULT KEYBOARD
# ============================================================

def result_keyboard(
    player1_id,
    player2_id,
):
    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔄 Rematch",
        callback_data=(
            f"rematch:"
            f"{player1_id}:"
            f"{player2_id}"
        ),
    )

    builder.button(
        text="📊 Details",
        callback_data="details",
    )

    builder.adjust(
        1,
        1,
    )

    return builder.as_markup()


# ============================================================
# RESOLVE TARGET
# ============================================================

async def resolve_target(
    message: Message,
    bot: Bot,
):
    # ========================================================
    # REPLY
    # ========================================================

    if (
        message.reply_to_message
        and
        message.reply_to_message.from_user
    ):
        return (
            message.from_user.id,
            message.reply_to_message.from_user.id,
        )

    # ========================================================
    # USERNAME
    # ========================================================

    text = message.text or ""

    match = re.match(
        r"^(\.мог|/mog)"
        r"(?:\s+@([A-Za-z0-9_]{5,32}))?$",
        text,
        re.IGNORECASE,
    )

    if (
        match
        and
        match.group(2)
    ):
        username = match.group(2)

        try:
            target = await bot.get_chat(
                "@" + username,
            )

            return (
                message.from_user.id,
                target.id,
            )

        except Exception as error:
            logger.warning(
                "Username lookup failed: %s",
                error,
            )

            return None

    return None


# ============================================================
# RUN MOG
# ============================================================

async def run_mog(
    message: Message,
    bot: Bot,
    player1_id: int,
    player2_id: int,
):
    if player1_id == player2_id:
        await message.answer(
            "😐 Себя с собой сравнивать нельзя."
        )
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
        "🧠 Gemini считает..."
    )

    try:
        player1 = await get_profile(
            bot,
            player1_id,
        )

        player2 = await get_profile(
            bot,
            player2_id,
        )

        ai_result = await analyze_with_gemini(
            player1,
            player2,
        )

        result = calculate_scores(
            ai_result,
            [
                player1.username,
                player2.username,
            ],
        )

        register_battle(
            player1,
            player2,
            result,
        )

        card = create_card(
            player1,
            player2,
            result,
        )

        score1 = result[
            "players"
        ][0]["overall"]

        score2 = result[
            "players"
        ][1]["overall"]

        if result["winner"] is None:
            winner_text = "🤝 <b>DRAW</b>"
        else:
            winner_text = (
                "👑 <b>"
                +
                html.escape(
                    result["winner_name"]
                )
                +
                "</b>"
            )

        caption = (
            "⚔️ <b>MOG BATTLE</b>\n\n"
            f"{html.escape(player1.username)} "
            f"<b>{score1:.2f}/10</b>\n"
            f"{html.escape(player2.username)} "
            f"<b>{score2:.2f}/10</b>\n\n"
            f"{winner_text}\n"
            f"📊 Difference: "
            f"<b>{result['difference']:.2f}</b>\n\n"
            f"💬 "
            f"{html.escape(result['verdict'])}"
        )

        await message.answer_photo(
            BufferedInputFile(
                card,
                filename="mog_battle.png",
            ),
            caption=caption,
            reply_markup=result_keyboard(
                player1_id,
                player2_id,
            ),
        )

        try:
            await status_message.delete()
        except Exception:
            pass

    except Exception as error:
        logger.exception("MOG failed")

        error_text = html.escape(
            str(error)
        )

        if len(error_text) > 3000:
            error_text = (
                error_text[:3000]
                +
                "..."
            )

        try:
            await status_message.edit_text(
                "❌ <b>MOG FAILED</b>\n\n"
                "<code>"
                +
                error_text
                +
                "</code>"
            )
        except Exception:
            await message.answer(
                "❌ <b>MOG FAILED</b>\n\n"
                "<code>"
                +
                error_text
                +
                "</code>"
            )


# ============================================================
# MOG COMMAND
# ============================================================

@dp.message(
    F.text.regexp(
        r"^(\.мог|/mog)"
        r"(?:\s+@[A-Za-z0-9_]{5,32})?$",
        flags=re.IGNORECASE,
    )
)
async def mog_command(
    message: Message,
    bot: Bot,
):
    target = await resolve_target(
        message,
        bot,
    )

    if not target:
        await message.answer(
            "<b>⚔️ MOG AI</b>\n\n"
            "Использование:\n\n"
            "1️⃣ Ответь на сообщение:\n"
            "<code>.мог</code>\n\n"
            "2️⃣ Или укажи username:\n"
            "<code>.мог @username</code>"
        )
        return

    player1_id, player2_id = target

    await run_mog(
        message,
        bot,
        player1_id,
        player2_id,
    )


# ============================================================
# SETTINGS COMMAND
# ============================================================

@dp.message(
    F.text.regexp(
        r"^(\.настройки|/settings)$",
        flags=re.IGNORECASE,
    )
)
async def settings_command(
    message: Message,
):
    if not is_settings_admin(message):
        await message.answer(
            "⛔ У тебя нет доступа к настройкам."
        )
        return

    settings = load_settings()

    keys = settings["gemini_keys"]
    model = settings["gemini_model"]

    await message.answer(
        "⚙️ <b>MOG AI SETTINGS</b>\n\n"
        f"🤖 Model: <code>{html.escape(model)}</code>\n"
        f"🔑 API keys: <b>{len(keys)}</b>\n\n"
        "Выбери действие:",
        reply_markup=settings_keyboard(),
    )


# ============================================================
# SETTINGS CALLBACKS
# ============================================================

@dp.callback_query(
    F.data.startswith("settings:")
)
async def settings_callback(
    callback: CallbackQuery,
):
    if (
        not callback.from_user
        or
        callback.from_user.id != SETTINGS_ADMIN_ID
    ):
        await callback.answer(
            "⛔ Нет доступа.",
            show_alert=True,
        )
        return

    action = callback.data.split(
        ":",
        1,
    )[1]

    if action == "close":
        await callback.answer("Закрыто.")

        try:
            await callback.message.delete()
        except Exception:
            pass

        return

    if action == "add_key":
        settings_waiting[
            callback.from_user.id
        ] = "add_key"

        await callback.answer()

        await callback.message.answer(
            "🔑 <b>Добавление API key</b>\n\n"
            "Отправь следующим сообщением "
            "ТОЛЬКО Gemini API key.\n\n"
            "Например:\n"
            "<code>AIza...</code>\n\n"
            "После добавления ключ будет сохранён "
            "в запасные ключи."
        )

        return

    if action == "list_keys":
        settings = load_settings()
        keys = settings["gemini_keys"]

        if not keys:
            text = (
                "🔑 <b>API KEYS</b>\n\n"
                "Ключей пока нет."
            )
        else:
            lines = []

            for index, key in enumerate(
                keys
            ):
                active = (
                    " 🟢"
                    if index == settings["active_key"]
                    else ""
                )

                lines.append(
                    f"{index + 1}. "
                    f"<code>{mask_key(key)}</code>"
                    f"{active}"
                )

            text = (
                "🔑 <b>API KEYS</b>\n\n"
                +
                "\n".join(lines)
                +
                "\n\n🟢 — последний рабочий ключ"
            )

        await callback.answer()

        await callback.message.answer(
            text,
            reply_markup=settings_keyboard(),
        )

        return

    if action == "delete_key":
        settings = load_settings()
        keys = settings["gemini_keys"]

        if not keys:
            await callback.answer(
                "Ключей нет.",
                show_alert=True,
            )
            return

        settings_waiting[
            callback.from_user.id
        ] = "delete_key"

        await callback.answer()

        await callback.message.answer(
            "🗑 <b>Удаление API key</b>\n\n"
            "Отправь номер ключа.\n\n"
            "Например:\n"
            "<code>1</code>"
        )

        return

    if action == "model":
        settings_waiting[
            callback.from_user.id
        ] = "model"

        await callback.answer()

        await callback.message.answer(
            "🤖 <b>Изменение модели</b>\n\n"
            "Отправь название модели, например:\n"
            "<code>gemini-2.5-flash</code>\n\n"
            "Можно также использовать другую "
            "модель, доступную твоим API keys."
        )

        return

    if action == "test_keys":
        await callback.answer(
            "🔄 Проверяю ключи...",
        )

        settings = load_settings()
        keys = settings["gemini_keys"]
        model = settings["gemini_model"]

        if not keys:
            await callback.message.answer(
                "❌ Нет API keys."
            )
            return

        results = []

        async with httpx.AsyncClient(
            timeout=30
        ) as client:

            for index, key in enumerate(keys):
                url = (
                    "https://generativelanguage.googleapis.com/"
                    f"v1beta/models/{model}"
                )

                try:
                    response = await client.get(
                        url,
                        headers={
                            "x-goog-api-key": key,
                        },
                    )

                    if response.status_code == 200:
                        results.append(
                            f"🟢 <b>#{index + 1}</b> "
                            f"{mask_key(key)} — OK"
                        )
                    else:
                        results.append(
                            f"🔴 <b>#{index + 1}</b> "
                            f"{mask_key(key)} — "
                            f"HTTP {response.status_code}"
                        )

                except Exception as error:
                    results.append(
                        f"🔴 <b>#{index + 1}</b> "
                        f"{mask_key(key)} — "
                        f"{html.escape(str(error)[:100])}"
                    )

        await callback.message.answer(
            "🔍 <b>Результат проверки</b>\n\n"
            +
            "\n".join(results),
            reply_markup=settings_keyboard(),
        )

        return


# ============================================================
# SETTINGS INPUT
# ============================================================

@dp.message()
async def settings_input_handler(
    message: Message,
):
    if not is_settings_admin(message):
        return

    user_id = message.from_user.id

    action = settings_waiting.get(
        user_id
    )

    if not action:
        return

    text = (
        message.text or ""
    ).strip()

    if not text:
        await message.answer(
            "❌ Нужно отправить текст."
        )
        return

    # ========================================================
    # ADD KEY
    # ========================================================

    if action == "add_key":

        # Убираем случайные пробелы
        key = text.strip()

        # Gemini keys обычно начинаются с AIza,
        # но не будем жёстко ограничивать формат.
        if len(key) < 20:
            await message.answer(
                "❌ Похоже, это слишком короткий API key.\n"
                "Отправь настоящий Gemini API key."
            )
            return

        async with settings_lock:
            settings = load_settings()

            if key in settings["gemini_keys"]:
                settings_waiting.pop(
                    user_id,
                    None,
                )

                await message.answer(
                    "⚠️ Этот API key уже добавлен.",
                    reply_markup=settings_keyboard(),
                )

                return

            settings["gemini_keys"].append(key)

            # Если это первый ключ
            if len(settings["gemini_keys"]) == 1:
                settings["active_key"] = 0

            save_settings(settings)

        settings_waiting.pop(
            user_id,
            None,
        )

        await message.answer(
            "✅ <b>API key добавлен.</b>\n\n"
            f"🔑 Ключ: "
            f"<code>{mask_key(key)}</code>\n"
            f"📦 Всего ключей: "
            f"<b>{len(settings['gemini_keys'])}</b>\n\n"
            "Если текущий ключ перестанет работать, "
            "бот автоматически попробует следующий.",
            reply_markup=settings_keyboard(),
        )

        return

    # ========================================================
    # DELETE KEY
    # ========================================================

    if action == "delete_key":

        try:
            index = int(text) - 1
        except ValueError:
            await message.answer(
                "❌ Отправь номер ключа, например <code>1</code>."
            )
            return

        async with settings_lock:
            settings = load_settings()
            keys = settings["gemini_keys"]

            if (
                index < 0
                or
                index >= len(keys)
            ):
                await message.answer(
                    "❌ Такого ключа нет."
                )
                return

            deleted = keys.pop(index)

            if keys:
                settings["active_key"] %= len(keys)
            else:
                settings["active_key"] = 0

            save_settings(settings)

        settings_waiting.pop(
            user_id,
            None,
        )

        await message.answer(
            "🗑 <b>API key удалён.</b>\n\n"
            f"<code>{mask_key(deleted)}</code>\n\n"
            f"Осталось ключей: "
            f"<b>{len(keys)}</b>",
            reply_markup=settings_keyboard(),
        )

        return

    # ========================================================
    # MODEL
    # ========================================================

    if action == "model":

        model = text

        # Небольшая защита от мусора
        if len(model) > 100:
            await message.answer(
                "❌ Слишком длинное название модели."
            )
            return

        async with settings_lock:
            settings = load_settings()
            settings["gemini_model"] = model
            save_settings(settings)

        settings_waiting.pop(
            user_id,
            None,
        )

        await message.answer(
            "✅ <b>Модель изменена.</b>\n\n"
            f"🤖 <code>{html.escape(model)}</code>\n\n"
            "Теперь MOG будет использовать её.",
            reply_markup=settings_keyboard(),
        )

        return


# ============================================================
# START
# ============================================================

@dp.message(
    Command("start")
)
async def start_command(
    message: Message,
):
    await message.answer(
        "<b>⚔️ MOG AI</b>\n\n"
        "AI-баттлы Telegram-профилей.\n\n"
        "🥊 Ответь на сообщение человека:\n"
        "<code>.мог</code>\n\n"
        "Или:\n"
        "<code>.мог @username</code>\n\n"
        "Gemini оценивает:\n"
        "👤 NAME\n"
        "🔤 USERNAME\n"
        "📝 BIO\n"
        "🔗 COHERENCE\n"
        "✨ VIBE\n\n"
        "🏆 Побеждает тот, у кого выше Overall."
    )


# ============================================================
# HELP
# ============================================================

@dp.message(
    Command("help")
)
async def help_command(
    message: Message,
):
    await message.answer(
        "<b>⚔️ MOG AI — COMMANDS</b>\n\n"
        "<code>.мог</code>\n"
        "Сравнить себя с человеком, "
        "на сообщение которого ты ответил.\n\n"
        "<code>.мог @username</code>\n"
        "Сравнить себя с username.\n\n"
        "<code>/mog</code>\n"
        "То же самое.\n\n"
        "<code>.настройки</code>\n"
        "Настройки Gemini для администратора.\n\n"
        "<code>/stats</code>\n"
        "Твоя статистика.\n\n"
        "<code>/top</code>\n"
        "Топ игроков.\n\n"
        "<code>/history</code>\n"
        "Последние баттлы.\n\n"
        "<code>/help</code>\n"
        "Помощь."
    )


# ============================================================
# STATS
# ============================================================

@dp.message(
    Command("stats")
)
async def stats_command(
    message: Message,
):
    data = load_data()

    user_id = str(
        message.from_user.id
    )

    user = data["users"].get(
        user_id
    )

    if not user:
        await message.answer(
            "📊 У тебя пока нет MOG-баттлов."
        )
        return

    battles = user["battles"]
    wins = user["wins"]
    losses = user["losses"]
    draws = user["draws"]

    if battles:
        winrate = (
            wins
            /
            battles
            *
            100
        )

        average = (
            user["score_sum"]
            /
            battles
        )
    else:
        winrate = 0
        average = 0

    await message.answer(
        "<b>📊 YOUR MOG STATS</b>\n\n"
        f"⚔️ Battles: <b>{battles}</b>\n"
        f"🏆 Wins: <b>{wins}</b>\n"
        f"💀 Losses: <b>{losses}</b>\n"
        f"🤝 Draws: <b>{draws}</b>\n\n"
        f"📈 Winrate: <b>{winrate:.1f}%</b>\n"
        f"⭐ Average score: <b>{average:.2f}/10</b>"
    )


# ============================================================
# TOP
# ============================================================

@dp.message(
    Command("top")
)
async def top_command(
    message: Message,
):
    data = load_data()

    users = []

    for user in data["users"].values():
        if user["battles"] <= 0:
            continue

        average = (
            user["score_sum"]
            /
            user["battles"]
        )

        users.append(
            {
                "username": user["username"],
                "wins": user["wins"],
                "battles": user["battles"],
                "average": average,
            }
        )

    users.sort(
        key=lambda x: (
            x["wins"],
            x["average"],
        ),
        reverse=True,
    )

    users = users[:10]

    if not users:
        await message.answer(
            "🏆 Пока нет статистики."
        )
        return

    medals = [
        "🥇",
        "🥈",
        "🥉",
    ]

    text = "<b>🏆 MOG TOP 10</b>\n\n"

    for index, user in enumerate(users):
        if index < 3:
            position = medals[index]
        else:
            position = f"<b>{index + 1}.</b>"

        username = html.escape(
            str(user["username"])
        )

        text += (
            f"{position} "
            f"{username} — "
            f"<b>{user['wins']}</b> wins "
            f"• {user['average']:.2f}/10\n"
        )

    await message.answer(text)


# ============================================================
# HISTORY
# ============================================================

@dp.message(
    Command("history")
)
async def history_command(
    message: Message,
):
    data = load_data()

    battles = data["battles"][-10:]

    if not battles:
        await message.answer(
            "📜 История пока пустая."
        )
        return

    battles.reverse()

    text = "<b>📜 LAST MOG BATTLES</b>\n\n"

    for battle in battles:

        if battle["winner"] == 0:
            winner = battle["player1"]
        elif battle["winner"] == 1:
            winner = battle["player2"]
        else:
            winner = "DRAW"

        p1 = html.escape(
            str(battle["player1"])
        )

        p2 = html.escape(
            str(battle["player2"])
        )

        winner = html.escape(
            str(winner)
        )

        text += (
            f"⚔️ {p1} "
            f"<b>{battle['score1']:.2f}</b>"
            f" × "
            f"<b>{battle['score2']:.2f}</b> "
            f"{p2}\n"
            f"🏆 {winner}\n\n"
        )

    await message.answer(text)


# ============================================================
# REMATCH
# ============================================================

@dp.callback_query(
    F.data.startswith("rematch:")
)
async def rematch_callback(
    callback: CallbackQuery,
    bot: Bot,
):
    try:
        parts = callback.data.split(":")

        if len(parts) != 3:
            raise ValueError(
                "Invalid callback data"
            )

        player1_id = int(parts[1])
        player2_id = int(parts[2])

        await callback.answer(
            "🔄 Новый MOG!"
        )

        await run_mog(
            callback.message,
            bot,
            player1_id,
            player2_id,
        )

    except Exception:
        logger.exception(
            "Rematch failed"
        )

        await callback.answer(
            "❌ Ошибка",
            show_alert=True,
        )


# ============================================================
# DETAILS
# ============================================================

@dp.callback_query(
    F.data == "details"
)
async def details_callback(
    callback: CallbackQuery,
):
    await callback.answer(
        "Карточка содержит все пять оценок.",
        show_alert=True,
    )


# ============================================================
# UNKNOWN COMMAND HANDLER
# ============================================================

@dp.message(
    F.text.startswith("/")
)
async def unknown_command(
    message: Message,
):
    await message.answer(
        "❓ Неизвестная команда.\n\n"
        "Напиши <code>/help</code>."
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    logger.info(
        "Starting MOG AI bot..."
    )

    settings = load_settings()

    logger.info(
        "Gemini model: %s",
        settings["gemini_model"],
    )

    logger.info(
        "Gemini keys configured: %s",
        len(settings["gemini_keys"]),
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    try:
        me = await bot.get_me()

        logger.info(
            "Logged in as @%s",
            me.username,
        )

        await dp.start_polling(bot)

    finally:
        await bot.session.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
