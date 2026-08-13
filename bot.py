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

OWNER_ID = 8904429775

DATA_FILE = "mog_data.json"
SETTINGS_FILE = "mog_settings.json"

GEMINI_MAX_RETRIES = 3
GEMINI_TIMEOUT = 120

GEMINI_SEMAPHORE = asyncio.Semaphore(2)

DEFAULT_MODEL = "gemini-2.5-flash"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("mog_ai")

dp = Dispatcher()


# ============================================================
# GLOBAL USER STATE
# ============================================================

# Здесь хранится состояние владельца:
#
# {
#   8904429775: "waiting_api_key",
#   8904429775: "waiting_model"
# }
#
# Это решает проблему:
# "Отправь API key" -> следующее сообщение ничего не делает.

USER_STATES = {}


# ============================================================
# SCORING
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


# ============================================================
# CARD FONT SETTINGS
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


# ============================================================
# COLORS
# ============================================================

BG = "#09090d"
PANEL = "#111118"
PANEL_2 = "#17171f"
BORDER = "#24242d"

WHITE = "#f4f4f7"
MUTED = "#858591"

YELLOW = "#f4c542"
PURPLE = "#8b5cf6"
PURPLE_DARK = "#241a3d"

RED = "#ef3030"
RED_DARK = "#8b1010"

BAR_BG = "#292932"


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
        avatar
    ):
        self.user_id = user_id
        self.username = username
        self.name = name
        self.bio = bio
        self.avatar = avatar


# ============================================================
# SETTINGS DATABASE
# ============================================================

def default_settings():

    return {
        "api_keys": [],
        "model": DEFAULT_MODEL
    }


def load_settings():

    if not os.path.exists(SETTINGS_FILE):

        return default_settings()

    try:

        with open(
            SETTINGS_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if not isinstance(data, dict):

            return default_settings()

        data.setdefault(
            "api_keys",
            []
        )

        data.setdefault(
            "model",
            DEFAULT_MODEL
        )

        if not isinstance(
            data["api_keys"],
            list
        ):

            data["api_keys"] = []

        # Удаляем мусор
        clean_keys = []

        for key in data["api_keys"]:

            if isinstance(
                key,
                str
            ):

                key = key.strip()

                if key and key not in clean_keys:

                    clean_keys.append(key)

        data["api_keys"] = clean_keys

        return data

    except Exception:

        logger.exception(
            "Failed to load settings"
        )

        return default_settings()


def save_settings(
    settings
):

    temporary_file = (
        SETTINGS_FILE
        +
        ".tmp"
    )

    try:

        with open(
            temporary_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                settings,
                file,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temporary_file,
            SETTINGS_FILE
        )

    except Exception:

        logger.exception(
            "Failed to save settings"
        )


# ============================================================
# OWNER CHECK
# ============================================================

def is_owner(
    user_id
):

    return int(user_id) == OWNER_ID


# ============================================================
# MASK API KEY
# ============================================================

def mask_api_key(
    key
):

    key = str(key)

    if len(key) <= 12:

        return "*" * len(key)

    return (
        key[:6]
        +
        "..."
        +
        key[-6:]
    )


# ============================================================
# DATABASE
# ============================================================

def load_data():

    if not os.path.exists(DATA_FILE):

        return {
            "users": {},
            "battles": []
        }

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if not isinstance(
            data,
            dict
        ):

            return {
                "users": {},
                "battles": []
            }

        data.setdefault(
            "users",
            {}
        )

        data.setdefault(
            "battles",
            []
        )

        return data

    except Exception:

        logger.exception(
            "Failed to load database"
        )

        return {
            "users": {},
            "battles": []
        }


def save_data(
    data
):

    temporary_file = (
        DATA_FILE
        +
        ".tmp"
    )

    try:

        with open(
            temporary_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temporary_file,
            DATA_FILE
        )

    except Exception:

        logger.exception(
            "Failed to save database"
        )


def ensure_user(
    data,
    user_id,
    username
):

    user_id = str(
        user_id
    )

    if user_id not in data["users"]:

        data["users"][user_id] = {
            "username": username,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "battles": 0,
            "score_sum": 0.0
        }

    else:

        data["users"][user_id]["username"] = username


def register_battle(
    player1,
    player2,
    result
):

    data = load_data()

    ensure_user(
        data,
        player1.user_id,
        player1.username
    )

    ensure_user(
        data,
        player2.user_id,
        player2.username
    )

    p1 = data["users"][
        str(player1.user_id)
    ]

    p2 = data["users"][
        str(player2.user_id)
    ]

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
        "status": result["status"]
    }

    data["battles"].append(
        battle
    )

    data["battles"] = (
        data["battles"][-500:]
    )

    save_data(
        data
    )


# ============================================================
# TELEGRAM PROFILE
# ============================================================

async def get_profile(
    bot: Bot,
    user_id: int
):

    chat = await bot.get_chat(
        user_id
    )

    username = (
        "@"
        +
        chat.username
        if chat.username
        else "no_username"
    )

    name = (
        chat.full_name
        or
        "Unknown"
    )

    bio = (
        getattr(
            chat,
            "bio",
            ""
        )
        or
        ""
    )

    avatar = None

    try:

        photos = await bot.get_user_profile_photos(
            user_id=user_id,
            limit=1
        )

        if photos.total_count:

            photo = photos.photos[0][-1]

            telegram_file = await bot.get_file(
                photo.file_id
            )

            buffer = io.BytesIO()

            await bot.download_file(
                telegram_file.file_path,
                buffer
            )

            avatar = buffer.getvalue()

    except Exception as error:

        logger.warning(
            "Avatar error for %s: %s",
            user_id,
            error
        )

    return Profile(
        user_id=user_id,
        username=username,
        name=name,
        bio=bio,
        avatar=avatar
    )


# ============================================================
# GEMINI SCHEMA
# ============================================================

GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {

        "players": {
            "type": "ARRAY",
            "minItems": 2,
            "maxItems": 2,

            "items": {
                "type": "OBJECT",

                "properties": {

                    "name": {
                        "type": "NUMBER"
                    },

                    "username": {
                        "type": "NUMBER"
                    },

                    "bio": {
                        "type": "NUMBER"
                    },

                    "coherence": {
                        "type": "NUMBER"
                    },

                    "vibe": {
                        "type": "NUMBER"
                    }
                },

                "required": [
                    "name",
                    "username",
                    "bio",
                    "coherence",
                    "vibe"
                ]
            }
        },

        "verdict": {
            "type": "STRING"
        }
    },

    "required": [
        "players",
        "verdict"
    ]
}


# ============================================================
# GEMINI PROMPT
# ============================================================

GEMINI_PROMPT = """
Ты — AI-судья юмористической игры MOG BATTLE.

Сравни два Telegram-профиля.

Оценивай исключительно качество оформления профиля,
а не личность человека.

Нужно выставить РОВНО ПЯТЬ оценок:

1. NAME
2. USERNAME
3. BIO
4. COHERENCE
5. VIBE

Каждая оценка от 0.0 до 10.0.

NAME:
Оцени отображаемое имя:
читаемость, стиль, запоминаемость,
оригинальность и визуальное качество.

USERNAME:
Оцени @username:
читаемость, запоминаемость, оригинальность,
простоту и визуальный стиль.

BIO:
Оцени bio:
качество текста, краткость, оригинальность,
характер и оформление.

Если bio отсутствует,
не выдумывай текст.

COHERENCE:
Оцени сочетание:
NAME + USERNAME + BIO + AVATAR.

VIBE:
Оцени общий стиль профиля:
визуальное впечатление, атмосферу,
цельность, характер и запоминаемость.

Аватар используется только для COHERENCE и VIBE.

Не оценивай и не делай выводы о:

- расе;
- этничности;
- религии;
- политических взглядах;
- сексуальной ориентации;
- здоровье;
- инвалидности;
- теле;
- физической привлекательности;
- точном возрасте;
- других чувствительных характеристиках.

Не выдумывай информацию.

Используй весь диапазон 0-10.
Не ставь одинаковые оценки без причины.

Сделай короткий смешной русский вердикт,
максимум 180 символов.

Верни ТОЛЬКО JSON.
"""


# ============================================================
# GEMINI API ERROR
# ============================================================

class GeminiAPIError(
    RuntimeError
):
    pass


# ============================================================
# ONE GEMINI REQUEST
# ============================================================

async def gemini_request(
    client,
    api_key,
    model,
    parts
):

    model = model.strip()

    if model.startswith(
        "models/"
    ):

        model = model[
            len("models/"):
        ]

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": parts
            }
        ],

        "generationConfig": {
            "responseMimeType":
                "application/json",

            "responseJsonSchema":
                GEMINI_RESPONSE_SCHEMA,

            "temperature": 0.7,

            "maxOutputTokens":
                1000
        }
    }

    headers = {
        "Content-Type":
            "application/json",

        "x-goog-api-key":
            api_key
    }

    response = await client.post(
        url,
        headers=headers,
        json=payload
    )

    if response.status_code != 200:

        body = response.text[:2000]

        raise GeminiAPIError(
            f"Gemini API error {response.status_code}: "
            f"{body}"
        )

    try:

        data = response.json()

    except Exception as error:

        raise GeminiAPIError(
            "Gemini returned invalid HTTP JSON."
        ) from error

    candidates = data.get(
        "candidates",
        []
    )

    if not candidates:

        raise GeminiAPIError(
            "Gemini API returned no candidates."
        )

    candidate = candidates[0]

    content = candidate.get(
        "content",
        {}
    )

    response_parts = content.get(
        "parts",
        []
    )

    text_parts = []

    for part in response_parts:

        text_value = part.get(
            "text"
        )

        if text_value:

            text_parts.append(
                text_value
            )

    text = "".join(
        text_parts
    ).strip()

    if not text:

        finish_reason = candidate.get(
            "finishReason",
            "UNKNOWN"
        )

        raise GeminiAPIError(
            "Gemini returned empty response. "
            f"finishReason={finish_reason}"
        )

    return text


# ============================================================
# PARSE GEMINI JSON
# ============================================================

def parse_gemini_json(
    text
):

    text = str(
        text or ""
    ).strip()

    # --------------------------------------------------------
    # DIRECT
    # --------------------------------------------------------

    try:

        return json.loads(
            text
        )

    except json.JSONDecodeError:
        pass

    # --------------------------------------------------------
    # CODE BLOCK
    # --------------------------------------------------------

    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    cleaned = re.sub(
        r"\s*```$",
        "",
        cleaned
    ).strip()

    try:

        return json.loads(
            cleaned
        )

    except json.JSONDecodeError:
        pass

    # --------------------------------------------------------
    # FIND OBJECT SAFELY
    # --------------------------------------------------------

    start = cleaned.find(
        "{"
    )

    if start == -1:

        raise RuntimeError(
            "Gemini JSON parsing failed: no JSON object."
        )

    depth = 0
    in_string = False
    escape = False

    for index in range(
        start,
        len(cleaned)
    ):

        char = cleaned[index]

        if escape:

            escape = False
            continue

        if char == "\\" and in_string:

            escape = True
            continue

        if char == '"':

            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":

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

    logger.error(
        "Gemini invalid JSON: %s",
        text[:5000]
    )

    raise RuntimeError(
        "Gemini JSON parsing failed."
    )


# ============================================================
# ANALYZE WITH GEMINI
# ============================================================

async def analyze_with_gemini(
    player1,
    player2
):

    settings = load_settings()

    api_keys = settings.get(
        "api_keys",
        []
    )

    model = settings.get(
        "model",
        DEFAULT_MODEL
    )

    if not api_keys:

        raise RuntimeError(
            "Нет Gemini API keys. "
            "Владелец должен открыть /настройки "
            "и добавить хотя бы один ключ."
        )

    def profile_text(
        profile
    ):

        return (
            f"\nUSERNAME: {profile.username}"
            f"\nNAME: {profile.name}"
            f"\nBIO: {profile.bio or '(нет bio)'}"
        )

    parts = [

        {
            "text":
                GEMINI_PROMPT
        },

        {
            "text":
                "\n\nPLAYER 1"
                +
                profile_text(
                    player1
                )
        },

        {
            "text":
                "\n\nPLAYER 2"
                +
                profile_text(
                    player2
                )
        }
    ]

    # --------------------------------------------------------
    # AVATAR 1
    # --------------------------------------------------------

    if player1.avatar:

        parts.append(
            {
                "inline_data": {
                    "mime_type":
                        "image/jpeg",

                    "data":
                        base64.b64encode(
                            player1.avatar
                        ).decode(
                            "utf-8"
                        )
                }
            }
        )

        parts.append(
            {
                "text":
                    "Это аватар PLAYER 1."
            }
        )

    else:

        parts.append(
            {
                "text":
                    "PLAYER 1 не имеет аватара."
            }
        )

    # --------------------------------------------------------
    # AVATAR 2
    # --------------------------------------------------------

    if player2.avatar:

        parts.append(
            {
                "inline_data": {
                    "mime_type":
                        "image/jpeg",

                    "data":
                        base64.b64encode(
                            player2.avatar
                        ).decode(
                            "utf-8"
                        )
                }
            }
        )

        parts.append(
            {
                "text":
                    "Это аватар PLAYER 2."
            }
        )

    else:

        parts.append(
            {
                "text":
                    "PLAYER 2 не имеет аватара."
            }
        )

    # --------------------------------------------------------
    # TRY KEYS
    # --------------------------------------------------------

    last_error = None

    async with GEMINI_SEMAPHORE:

        async with httpx.AsyncClient(
            timeout=GEMINI_TIMEOUT
        ) as client:

            for key_index, api_key in enumerate(
                api_keys
            ):

                for attempt in range(
                    GEMINI_MAX_RETRIES
                ):

                    try:

                        logger.info(
                            "Gemini request: key %s/%s, model=%s, attempt=%s",
                            key_index + 1,
                            len(api_keys),
                            model,
                            attempt + 1
                        )

                        text = await gemini_request(
                            client,
                            api_key,
                            model,
                            parts
                        )

                        result = parse_gemini_json(
                            text
                        )

                        return result

                    except GeminiAPIError as error:

                        last_error = error

                        error_text = str(
                            error
                        )

                        logger.warning(
                            "Gemini key %s failed: %s",
                            key_index + 1,
                            error_text[:1000]
                        )

                        # ------------------------------------------------
                        # INVALID MODEL / INVALID KEY / PERMISSION /
                        # QUOTA / RATE LIMIT:
                        # сразу пробуем следующий ключ.
                        # ------------------------------------------------

                        if (
                            "400" in error_text
                            or
                            "401" in error_text
                            or
                            "403" in error_text
                            or
                            "404" in error_text
                            or
                            "429" in error_text
                        ):

                            break

                        if attempt < GEMINI_MAX_RETRIES - 1:

                            await asyncio.sleep(
                                2 ** attempt
                            )

                    except Exception as error:

                        last_error = error

                        logger.warning(
                            "Gemini parsing/request error: %s",
                            error
                        )

                        # Ошибка JSON может быть временной.
                        if attempt < GEMINI_MAX_RETRIES - 1:

                            await asyncio.sleep(
                                2 ** attempt
                            )

                        else:

                            break

    if last_error:

        raise RuntimeError(
            f"Gemini API error: {last_error}"
        )

    raise RuntimeError(
        "Gemini request failed."
    )


# ============================================================
# SCORE CALCULATION
# ============================================================

def calculate_scores(
    ai_result,
    names
):

    raw_players = ai_result.get(
        "players",
        []
    )

    if (
        not isinstance(
            raw_players,
            list
        )
        or
        len(raw_players) != 2
    ):

        raise RuntimeError(
            "Gemini returned invalid players data."
        )

    players = []

    for raw in raw_players:

        if not isinstance(
            raw,
            dict
        ):

            raise RuntimeError(
                "Gemini returned invalid player data."
            )

        scores = {}

        for key in WEIGHTS:

            try:

                value = float(
                    raw.get(
                        key,
                        0
                    )
                )

            except Exception:

                value = 0.0

            value = max(
                0.0,
                min(
                    10.0,
                    value
                )
            )

            scores[key] = value

        overall = sum(
            scores[key]
            *
            WEIGHTS[key]

            for key in WEIGHTS
        )

        scores["overall"] = round(
            overall,
            2
        )

        players.append(
            scores
        )

    score1 = players[0]["overall"]
    score2 = players[1]["overall"]

    difference = round(
        abs(
            score1 - score2
        ),
        2
    )

    if difference < 0.10:

        winner = None
        loser = None
        status = "DRAW"

    elif score1 > score2:

        winner = 0
        loser = 1

        if difference >= 2.0:

            status = "ABSOLUTE MOG"

        elif difference >= 1.0:

            status = "DOMINATED"

        else:

            status = "MOGGED"

    else:

        winner = 1
        loser = 0

        if difference >= 2.0:

            status = "ABSOLUTE MOG"

        elif difference >= 1.0:

            status = "DOMINATED"

        else:

            status = "MOGGED"

    winner_name = (
        "DRAW"
        if winner is None
        else names[winner]
    )

    return {
        "players":
            players,

        "winner":
            winner,

        "loser":
            loser,

        "winner_name":
            winner_name,

        "difference":
            difference,

        "status":
            status,

        "verdict":
            str(
                ai_result.get(
                    "verdict",
                    "Нет вердикта."
                )
            )[:300]
    }


# ============================================================
# FONTS
# ============================================================

_FONT_CACHE = {}


def get_font(
    key,
    bold=False
):

    cache_key = (
        key,
        bold
    )

    if cache_key in _FONT_CACHE:

        return _FONT_CACHE[
            cache_key
        ]

    size = FONT_SIZES[
        key
    ]

    if bold:

        paths = [
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        ]

    else:

        paths = [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ]

    for path in paths:

        if os.path.exists(path):

            font_obj = ImageFont.truetype(
                path,
                int(size)
            )

            _FONT_CACHE[
                cache_key
            ] = font_obj

            return font_obj

    font_obj = ImageFont.load_default()

    _FONT_CACHE[
        cache_key
    ] = font_obj

    return font_obj


def f(
    key,
    bold=False
):

    return get_font(
        key,
        bold
    )


# ============================================================
# TEXT HELPERS
# ============================================================

def truncate_text(
    text,
    max_chars
):

    text = str(
        text or ""
    )

    if len(text) <= max_chars:

        return text

    return (
        text[
            :max_chars - 1
        ]
        +
        "…"
    )


def draw_center(
    draw,
    text,
    x,
    y,
    font_obj,
    fill
):

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font_obj
    )

    width = (
        bbox[2] - bbox[0]
    )

    draw.text(
        (
            x - width / 2,
            y
        ),
        text,
        font=font_obj,
        fill=fill
    )


def wrap_text(
    draw,
    text,
    font_obj,
    max_width
):

    words = (
        str(text or "")
        .split()
    )

    if not words:

        return [""]

    lines = []
    current = ""

    for word in words:

        candidate = (
            word
            if not current
            else
            current
            +
            " "
            +
            word
        )

        bbox = draw.textbbox(
            (0, 0),
            candidate,
            font=font_obj
        )

        width = (
            bbox[2] - bbox[0]
        )

        if width <= max_width:

            current = candidate

        else:

            if current:

                lines.append(
                    current
                )

            current = word

    if current:

        lines.append(
            current
        )

    return lines


# ============================================================
# AVATAR
# ============================================================

def make_avatar(
    data,
    size=260
):

    result = Image.new(
        "RGBA",
        (
            size,
            size
        ),
        (
            0,
            0,
            0,
            0
        )
    )

    if data:

        try:

            avatar = Image.open(
                io.BytesIO(data)
            ).convert(
                "RGB"
            )

            width, height = (
                avatar.size
            )

            side = min(
                width,
                height
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
                    top + side
                )
            )

            avatar = avatar.resize(
                (
                    size,
                    size
                ),
                Image.Resampling.LANCZOS
            )

        except Exception:

            avatar = Image.new(
                "RGB",
                (
                    size,
                    size
                ),
                "#26262f"
            )

    else:

        avatar = Image.new(
            "RGB",
            (
                size,
                size
            ),
            "#26262f"
        )

    mask = Image.new(
        "L",
        (
            size,
            size
        ),
        0
    )

    mask_draw = ImageDraw.Draw(
        mask
    )

    mask_draw.ellipse(
        (
            0,
            0,
            size - 1,
            size - 1
        ),
        fill=255
    )

    result.paste(
        avatar,
        (
            0,
            0
        ),
        mask
    )

    border = ImageDraw.Draw(
        result
    )

    border.ellipse(
        (
            3,
            3,
            size - 4,
            size - 4
        ),
        outline=YELLOW,
        width=6
    )

    return result


# ============================================================
# CROWN
# ============================================================

def draw_crown(
    draw,
    center_x,
    y
):

    points = [
        (
            center_x - 80,
            y + 50
        ),
        (
            center_x - 65,
            y - 15
        ),
        (
            center_x - 22,
            y + 22
        ),
        (
            center_x,
            y - 30
        ),
        (
            center_x + 22,
            y + 22
        ),
        (
            center_x + 65,
            y - 15
        ),
        (
            center_x + 80,
            y + 50
        )
    ]

    draw.polygon(
        points,
        fill=YELLOW
    )

    draw.rounded_rectangle(
        (
            center_x - 80,
            y + 40,
            center_x + 80,
            y + 67
        ),
        radius=7,
        fill=YELLOW
    )


# ============================================================
# PROFILE HEADER
# ============================================================

def draw_profile_header(
    image,
    draw,
    player,
    result,
    player_index,
    y
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
            y + panel_h
        ),
        radius=32,
        fill=PANEL,
        outline=BORDER,
        width=3
    )

    if result["winner"] == player_index:

        draw_crown(
            draw,
            center_x,
            y + 45
        )

    avatar_size = 260

    avatar = make_avatar(
        player.avatar,
        avatar_size
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
            avatar_y
        ),
        avatar
    )

    if player_index == 0:

        label = "HOST PROFILE"

    else:

        label = "GUEST PROFILE"

    label_font = f(
        "profile_label",
        True
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
            label_y + label_box_h
        ),
        radius=24,
        fill=label_fill
    )

    draw_center(
        draw,
        label,
        center_x,
        label_y + 8,
        label_font,
        label_color
    )

    name = truncate_text(
        player.name,
        23
    )

    draw_center(
        draw,
        name,
        center_x,
        y + 395,
        f(
            "profile_name",
            True
        ),
        WHITE
    )

    username = truncate_text(
        player.username,
        30
    )

    draw_center(
        draw,
        username,
        center_x,
        y + 445,
        f(
            "profile_username",
            True
        ),
        YELLOW
    )


# ============================================================
# SCORE PANEL
# ============================================================

def draw_score_panel(
    draw,
    result,
    player_index,
    y
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
            y + panel_h
        ),
        radius=32,
        fill=PANEL,
        outline=BORDER,
        width=3
    )

    draw.text(
        (
            panel_left + 38,
            y + 28
        ),
        "OVERALL SCORE",
        font=f(
            "overall_label",
            True
        ),
        fill=MUTED
    )

    overall = result[
        "players"
    ][player_index]["overall"]

    draw.text(
        (
            panel_left + 38,
            y + 60
        ),
        f"{overall:.2f}",
        font=f(
            "overall_score",
            True
        ),
        fill=WHITE
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
                badge_y + badge_h
            ),
            radius=24,
            fill="#403310"
        )

        draw_center(
            draw,
            "WINNER",
            badge_x + badge_w / 2,
            badge_y + 7,
            f(
                "profile_label",
                True
            ),
            YELLOW
        )

    label_x = panel_left + 38

    bar_x = 300
    bar_width = 550
    score_x = 885

    first_y = y + 145
    row_gap = 82

    for index, (
        label,
        key
    ) in enumerate(
        CATEGORIES
    ):

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
                row_y
            ),
            label,
            font=f(
                "category",
                True
            ),
            fill=MUTED
        )

        bar_y = row_y + 4
        bar_h = 26

        draw.rounded_rectangle(
            (
                bar_x,
                bar_y,
                bar_x + bar_width,
                bar_y + bar_h
            ),
            radius=13,
            fill=BAR_BG
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
                    bar_y + bar_h
                ),
                radius=13,
                fill=YELLOW
            )

        draw.text(
            (
                score_x,
                row_y - 2
            ),
            f"{score:.2f}",
            font=f(
                "category_score",
                True
            ),
            fill=WHITE
        )


# ============================================================
# VS
# ============================================================

def draw_vs(
    draw,
    y
):

    W = draw._image.width
    center_x = W // 2

    line_y = y + 58

    draw.line(
        (
            70,
            line_y,
            center_x - 115,
            line_y
        ),
        fill=BORDER,
        width=3
    )

    draw.line(
        (
            center_x + 115,
            line_y,
            W - 70,
            line_y
        ),
        fill=BORDER,
        width=3
    )

    draw.rounded_rectangle(
        (
            center_x - 115,
            y,
            center_x + 115,
            y + 115
        ),
        radius=36,
        fill=BG
    )

    draw_center(
        draw,
        "VS",
        center_x,
        y + 8,
        f(
            "vs",
            True
        ),
        WHITE
    )


# ============================================================
# MOGGED STAMP
# ============================================================

def create_mogged_stamp():

    width = 520
    height = 115

    stamp = Image.new(
        "RGBA",
        (
            width,
            height
        ),
        (
            0,
            0,
            0,
            0
        )
    )

    draw = ImageDraw.Draw(
        stamp
    )

    draw.rounded_rectangle(
        (
            4,
            4,
            width - 4,
            height - 4
        ),
        radius=18,
        fill=RED,
        outline="white",
        width=5
    )

    draw_center(
        draw,
        "MOGGED",
        width // 2,
        22,
        f(
            "stamp",
            True
        ),
        "white"
    )

    return stamp.rotate(
        9,
        expand=True,
        resample=Image.Resampling.BICUBIC
    )


# ============================================================
# CARD
# ============================================================

def create_card(
    player1,
    player2,
    result
):

    W = 1100
    H = 2500

    image = Image.new(
        "RGB",
        (
            W,
            H
        ),
        BG
    )

    draw = ImageDraw.Draw(
        image
    )

    center_x = W // 2

    draw_center(
        draw,
        "MOG BATTLE",
        center_x,
        30,
        f(
            "title",
            True
        ),
        YELLOW
    )

    draw_center(
        draw,
        "AI PROFILE COMPARISON",
        center_x,
        105,
        f(
            "subtitle",
            True
        ),
        MUTED
    )

    draw_profile_header(
        image,
        draw,
        player1,
        result,
        0,
        150
    )

    draw_score_panel(
        draw,
        result,
        0,
        670
    )

    draw_vs(
        draw,
        1280
    )

    draw_profile_header(
        image,
        draw,
        player2,
        result,
        1,
        1415
    )

    draw_score_panel(
        draw,
        result,
        1,
        1935
    )

    if result["loser"] is not None:

        stamp = create_mogged_stamp()

        stamp_x = (
            center_x
            -
            stamp.width // 2
        )

        stamp_y = 2145

        image.paste(
            stamp,
            (
                stamp_x,
                stamp_y
            ),
            stamp
        )

    result_y = 2400

    draw.line(
        (
            70,
            result_y,
            W - 70,
            result_y
        ),
        fill=BORDER,
        width=3
    )

    draw.text(
        (
            70,
            result_y + 20
        ),
        result["status"],
        font=f(
            "status",
            True
        ),
        fill=RED
    )

    if result["winner"] is None:

        winner_text = "DRAW"

    else:

        winner_text = (
            "WINNER  "
            +
            result["winner_name"]
        )

    draw_center(
        draw,
        winner_text,
        center_x,
        result_y + 65,
        f(
            "winner",
            True
        ),
        YELLOW
    )

    draw.text(
        (
            70,
            result_y + 120
        ),
        "Разрыв",
        font=f(
            "difference_label",
            True
        ),
        fill=MUTED
    )

    draw.text(
        (
            920,
            result_y + 116
        ),
        f"{result['difference']:.2f}",
        font=f(
            "difference_score",
            True
        ),
        fill=YELLOW
    )

    verdict = truncate_text(
        str(
            result.get(
                "verdict",
                ""
            )
        ).strip(),
        150
    )

    verdict_lines = wrap_text(
        draw,
        verdict,
        f(
            "verdict",
            True
        ),
        W - 130
    )

    verdict_y = (
        result_y
        +
        175
    )

    for line in verdict_lines[:2]:

        draw_center(
            draw,
            line,
            center_x,
            verdict_y,
            f(
                "verdict",
                True
            ),
            WHITE
        )

        verdict_y += 34

    draw_center(
        draw,
        "MOG AI  •  POWERED BY GEMINI",
        center_x,
        H - 45,
        f(
            "footer",
            True
        ),
        MUTED
    )

    output = io.BytesIO()

    image.save(
        output,
        format="PNG",
        optimize=True
    )

    return output.getvalue()


# ============================================================
# RESULT KEYBOARD
# ============================================================

def result_keyboard(
    player1_id,
    player2_id
):

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔄 Rematch",
        callback_data=(
            f"rematch:"
            f"{player1_id}:"
            f"{player2_id}"
        )
    )

    builder.button(
        text="📊 Details",
        callback_data="details"
    )

    builder.adjust(
        1,
        1
    )

    return builder.as_markup()


# ============================================================
# SETTINGS KEYBOARD
# ============================================================

def settings_keyboard():

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔑 Добавить API key",
        callback_data="settings:add_key"
    )

    builder.button(
        text="📋 Список API keys",
        callback_data="settings:list_keys"
    )

    builder.button(
        text="🗑 Удалить API key",
        callback_data="settings:delete_key"
    )

    builder.button(
        text="🤖 Изменить модель",
        callback_data="settings:model"
    )

    builder.button(
        text="🧪 Проверить ключи",
        callback_data="settings:test"
    )

    builder.button(
        text="❌ Закрыть",
        callback_data="settings:close"
    )

    builder.adjust(
        1,
        1,
        1,
        1,
        1,
        1
    )

    return builder.as_markup()


# ============================================================
# SETTINGS TEXT
# ============================================================

def settings_text():

    settings = load_settings()

    keys = settings.get(
        "api_keys",
        []
    )

    model = settings.get(
        "model",
        DEFAULT_MODEL
    )

    if keys:

        key_text = (
            f"🔑 API keys: <b>{len(keys)}</b>"
        )

    else:

        key_text = (
            "🔑 API keys: <b>0</b>"
        )

    return (
        "<b>⚙️ MOG AI — НАСТРОЙКИ</b>\n\n"

        f"{key_text}\n"

        f"🤖 Модель: "
        f"<code>{html.escape(model)}</code>\n\n"

        "Здесь можно настроить Gemini "
        "глобально для всего бота.\n\n"

        "Если один API key перестанет работать, "
        "бот автоматически попробует следующий."
    )


# ============================================================
# SETTINGS COMMAND
# ============================================================

@dp.message(
    F.text.regexp(
        r"^(?:\.настройки|/настройки|/settings)$",
        flags=re.IGNORECASE
    )
)
async def settings_command(
    message: Message
):

    if not is_owner(
        message.from_user.id
    ):

        await message.answer(
            "⛔ У тебя нет доступа к настройкам."
        )

        return

    USER_STATES.pop(
        message.from_user.id,
        None
    )

    await message.answer(
        settings_text(),
        reply_markup=settings_keyboard()
    )


# ============================================================
# SETTINGS CALLBACK
# ============================================================

@dp.callback_query(
    F.data.startswith(
        "settings:"
    )
)
async def settings_callback(
    callback: CallbackQuery
):

    if not is_owner(
        callback.from_user.id
    ):

        await callback.answer(
            "⛔ Нет доступа.",
            show_alert=True
        )

        return

    action = callback.data.split(
        ":",
        1
    )[1]

    user_id = callback.from_user.id

    # --------------------------------------------------------
    # ADD KEY
    # --------------------------------------------------------

    if action == "add_key":

        USER_STATES[user_id] = (
            "waiting_api_key"
        )

        await callback.answer()

        await callback.message.answer(
            "🔑 <b>Добавление API key</b>\n\n"

            "Отправь следующим сообщением "
            "<b>только Gemini API key</b>.\n\n"

            "Например:\n"
            "<code>AIza...</code>\n\n"

            "❗ После отправки ключ будет сохранён "
            "глобально для всего бота.\n\n"

            "Для отмены отправь:\n"
            "<code>отмена</code>"
        )

        return

    # --------------------------------------------------------
    # LIST KEYS
    # --------------------------------------------------------

    if action == "list_keys":

        settings = load_settings()

        keys = settings.get(
            "api_keys",
            []
        )

        if not keys:

            text = (
                "📋 <b>API KEYS</b>\n\n"
                "Ключей пока нет."
            )

        else:

            lines = []

            for index, key in enumerate(
                keys,
                1
            ):

                lines.append(
                    f"{index}. "
                    f"<code>{html.escape(mask_api_key(key))}</code>"
                )

            text = (
                "📋 <b>API KEYS</b>\n\n"
                +
                "\n".join(lines)
                +
                "\n\n"
                +
                f"🤖 Model: "
                f"<code>{html.escape(settings.get('model', DEFAULT_MODEL))}</code>"
            )

        await callback.answer()

        await callback.message.answer(
            text,
            reply_markup=settings_keyboard()
        )

        return

    # --------------------------------------------------------
    # DELETE KEY
    # --------------------------------------------------------

    if action == "delete_key":

        settings = load_settings()

        keys = settings.get(
            "api_keys",
            []
        )

        if not keys:

            await callback.answer(
                "Ключей нет.",
                show_alert=True
            )

            return

        builder = InlineKeyboardBuilder()

        for index, key in enumerate(
            keys
        ):

            builder.button(
                text=(
                    f"🗑 {index + 1}. "
                    f"{mask_api_key(key)}"
                ),
                callback_data=(
                    f"settings:delete:{index}"
                )
            )

        builder.button(
            text="⬅️ Назад",
            callback_data="settings:back"
        )

        builder.adjust(
            1
        )

        await callback.answer()

        await callback.message.answer(
            "<b>🗑 Удаление API key</b>\n\n"
            "Выбери ключ:",
            reply_markup=builder.as_markup()
        )

        return

    # --------------------------------------------------------
    # MODEL
    # --------------------------------------------------------

    if action == "model":

        USER_STATES[user_id] = (
            "waiting_model"
        )

        settings = load_settings()

        current_model = settings.get(
            "model",
            DEFAULT_MODEL
        )

        await callback.answer()

        await callback.message.answer(
            "🤖 <b>Изменение модели</b>\n\n"

            "Текущая модель:\n"
            f"<code>{html.escape(current_model)}</code>\n\n"

            "Отправь название новой модели.\n\n"

            "Например:\n"
            "<code>gemini-2.5-flash</code>\n\n"

            "Также можно отправить:\n"
            "<code>gemini-3.6-flash</code>\n\n"

            "Для отмены:\n"
            "<code>отмена</code>"
        )

        return

    # --------------------------------------------------------
    # TEST
    # --------------------------------------------------------

    if action == "test":

        await callback.answer(
            "🧪 Проверяю ключи..."
        )

        settings = load_settings()

        keys = settings.get(
            "api_keys",
            []
        )

        model = settings.get(
            "model",
            DEFAULT_MODEL
        )

        if not keys:

            await callback.message.answer(
                "❌ Нет API keys."
            )

            return

        results = []

        async with httpx.AsyncClient(
            timeout=30
        ) as client:

            for index, key in enumerate(
                keys,
                1
            ):

                try:

                    url = (
                        "https://generativelanguage.googleapis.com/"
                        f"v1beta/models/{model}:generateContent"
                    )

                    payload = {
                        "contents": [
                            {
                                "parts": [
                                    {
                                        "text":
                                            "Reply only with OK."
                                    }
                                ]
                            }
                        ],

                        "generationConfig": {
                            "maxOutputTokens": 10
                        }
                    }

                    response = await client.post(
                        url,
                        headers={
                            "Content-Type":
                                "application/json",

                            "x-goog-api-key":
                                key
                        },
                        json=payload
                    )

                    if response.status_code == 200:

                        results.append(
                            f"✅ Key {index}: работает"
                        )

                    else:

                        results.append(
                            f"❌ Key {index}: "
                            f"HTTP {response.status_code}"
                        )

                except Exception as error:

                    results.append(
                        f"❌ Key {index}: "
                        f"{str(error)[:100]}"
                    )

        await callback.message.answer(
            "<b>🧪 ПРОВЕРКА GEMINI</b>\n\n"
            +
            "\n".join(results)
            +
            "\n\n"
            f"🤖 Model: "
            f"<code>{html.escape(model)}</code>"
        )

        return

    # --------------------------------------------------------
    # DELETE ONE KEY
    # --------------------------------------------------------

    if action.startswith(
        "delete"
    ):

        parts = action.split(
            ":"
        )

        if len(parts) != 2:

            await callback.answer(
                "Ошибка.",
                show_alert=True
            )

            return

        try:

            index = int(
                parts[1]
            )

        except ValueError:

            await callback.answer(
                "Ошибка.",
                show_alert=True
            )

            return

        settings = load_settings()

        keys = settings.get(
            "api_keys",
            []
        )

        if (
            index < 0
            or
            index >= len(keys)
        ):

            await callback.answer(
                "Ключ уже отсутствует.",
                show_alert=True
            )

            return

        deleted = keys.pop(
            index
        )

        settings["api_keys"] = keys

        save_settings(
            settings
        )

        USER_STATES.pop(
            user_id,
            None
        )

        await callback.answer(
            "Ключ удалён."
        )

        await callback.message.answer(
            "🗑 API key удалён:\n"
            f"<code>{html.escape(mask_api_key(deleted))}</code>\n\n"
            +
            settings_text(),
            reply_markup=settings_keyboard()
        )

        return

    # --------------------------------------------------------
    # BACK
    # --------------------------------------------------------

    if action == "back":

        USER_STATES.pop(
            user_id,
            None
        )

        await callback.answer()

        await callback.message.answer(
            settings_text(),
            reply_markup=settings_keyboard()
        )

        return

    # --------------------------------------------------------
    # CLOSE
    # --------------------------------------------------------

    if action == "close":

        USER_STATES.pop(
            user_id,
            None
        )

        await callback.answer(
            "Настройки закрыты."
        )

        try:

            await callback.message.delete()

        except Exception:

            pass

        return


# ============================================================
# SETTINGS INPUT HANDLER
#
# КЛЮЧЕВОЙ FIX
# ============================================================

@dp.message(
    F.text
)
async def settings_input_handler(
    message: Message
):

    user_id = message.from_user.id

    # --------------------------------------------------------
    # Только OWNER
    # --------------------------------------------------------

    if not is_owner(
        user_id
    ):

        return

    state = USER_STATES.get(
        user_id
    )

    # --------------------------------------------------------
    # Ничего не ждём
    # --------------------------------------------------------

    if not state:

        return

    text = (
        message.text
        or
        ""
    ).strip()

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    if text.lower() in (
        "отмена",
        "cancel",
        "/cancel"
    ):

        USER_STATES.pop(
            user_id,
            None
        )

        await message.answer(
            "❌ Действие отменено.\n\n"
            +
            settings_text(),
            reply_markup=settings_keyboard()
        )

        return

    # ========================================================
    # WAITING API KEY
    # ========================================================

    if state == "waiting_api_key":

        # Telegram API key обычно начинается с AIza,
        # но не будем жёстко требовать конкретный формат,
        # чтобы не ломать новые форматы ключей.

        if (
            len(text) < 20
            or
            len(text) > 500
            or
            any(
                char.isspace()
                for char in text
            )
        ):

            await message.answer(
                "❌ Похоже, это не API key.\n\n"
                "Отправь ключ одним сообщением "
                "без пробелов.\n\n"
                "Для отмены: <code>отмена</code>"
            )

            return

        settings = load_settings()

        keys = settings.get(
            "api_keys",
            []
        )

        if text in keys:

            USER_STATES.pop(
                user_id,
                None
            )

            await message.answer(
                "⚠️ Этот API key уже есть в списке.\n\n"
                +
                settings_text(),
                reply_markup=settings_keyboard()
            )

            return

        keys.append(
            text
        )

        settings["api_keys"] = keys

        save_settings(
            settings
        )

        USER_STATES.pop(
            user_id,
            None
        )

        await message.answer(
            "✅ <b>API key добавлен!</b>\n\n"

            f"🔑 Key: "
            f"<code>{html.escape(mask_api_key(text))}</code>\n"

            f"📊 Всего ключей: "
            f"<b>{len(keys)}</b>\n\n"

            "Если этот ключ перестанет работать, "
            "бот автоматически перейдёт к следующему.\n\n"

            +
            settings_text(),
            reply_markup=settings_keyboard()
        )

        return

    # ========================================================
    # WAITING MODEL
    # ========================================================

    if state == "waiting_model":

        model = text

        model = model.replace(
            "models/",
            ""
        ).strip()

        if (
            len(model) < 3
            or
            len(model) > 150
            or
            " " in model
        ):

            await message.answer(
                "❌ Некорректное название модели.\n\n"
                "Например:\n"
                "<code>gemini-2.5-flash</code>"
            )

            return

        settings = load_settings()

        settings["model"] = model

        save_settings(
            settings
        )

        USER_STATES.pop(
            user_id,
            None
        )

        await message.answer(
            "✅ <b>Модель изменена!</b>\n\n"

            f"🤖 Теперь используется:\n"
            f"<code>{html.escape(model)}</code>\n\n"

            "Глобально для всего бота.",
            reply_markup=settings_keyboard()
        )

        return


# ============================================================
# RESOLVE TARGET
# ============================================================

async def resolve_target(
    message: Message,
    bot: Bot
):

    if (
        message.reply_to_message
        and
        message.reply_to
