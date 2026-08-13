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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Надёжный fallback.
# Можно переопределить через ENV:
# GEMINI_MODEL=gemini-3.5-flash
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash"
).strip()

# Если основной model не сработал — пробуем этот.
GEMINI_FALLBACK_MODEL = os.getenv(
    "GEMINI_FALLBACK_MODEL",
    "gemini-2.5-flash"
).strip()

DATA_FILE = "mog_data.json"

GEMINI_MAX_RETRIES = 4
GEMINI_TIMEOUT = 120

GEMINI_SEMAPHORE = asyncio.Semaphore(2)


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")


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
# FONT SETTINGS
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

        if not isinstance(data, dict):

            return {
                "users": {},
                "battles": []
            }

        data.setdefault("users", {})
        data.setdefault("battles", [])

        return data

    except Exception:

        logger.exception(
            "Failed to load database"
        )

        return {
            "users": {},
            "battles": []
        }


def save_data(data):

    temporary_file = DATA_FILE + ".tmp"

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

    user_id = str(user_id)

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

    data["battles"].append(battle)

    data["battles"] = data["battles"][-500:]

    save_data(data)


# ============================================================
# TELEGRAM PROFILE
# ============================================================

async def get_profile(
    bot: Bot,
    user_id: int
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
            ""
        )
        or ""
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
# GEMINI JSON SCHEMA
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
                        "maximum": 10
                    },

                    "username": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10
                    },

                    "bio": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10
                    },

                    "coherence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10
                    },

                    "vibe": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10
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
            "type": "string"
        }
    },

    "required": [
        "players",
        "verdict"
    ]
}


# ============================================================
# JSON EXTRACTION
# ============================================================

def extract_json_object(text: str):

    if not text:

        raise ValueError(
            "no JSON object: empty Gemini text"
        )

    text = str(text).strip()

    logger.info(
        "Gemini raw text length: %s",
        len(text)
    )

    logger.debug(
        "Gemini raw text: %s",
        text[:10000]
    )

    # --------------------------------------------------------
    # 1. Direct JSON
    # --------------------------------------------------------

    try:

        parsed = json.loads(text)

        if isinstance(parsed, dict):

            return parsed

    except json.JSONDecodeError:

        pass

    # --------------------------------------------------------
    # 2. Remove markdown fences
    # --------------------------------------------------------

    cleaned = re.sub(
        r"^\s*```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    cleaned = re.sub(
        r"\s*```\s*$",
        "",
        cleaned
    ).strip()

    try:

        parsed = json.loads(cleaned)

        if isinstance(parsed, dict):

            return parsed

    except json.JSONDecodeError:

        pass

    # --------------------------------------------------------
    # 3. Find balanced JSON object
    #
    # Не используем:
    # re.search(r"\{.*\}")
    #
    # потому что это ломается на вложенных объектах.
    # --------------------------------------------------------

    start = cleaned.find("{")

    while start != -1:

        depth = 0
        in_string = False
        escaped = False

        for index in range(
            start,
            len(cleaned)
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

                        parsed = json.loads(
                            candidate
                        )

                        if isinstance(
                            parsed,
                            dict
                        ):

                            return parsed

                    except json.JSONDecodeError:

                        pass

                    break

        start = cleaned.find(
            "{",
            start + 1
        )

    raise ValueError(
        "no JSON object"
    )


# ============================================================
# GEMINI RESPONSE TEXT
# ============================================================

def extract_gemini_text(data):

    candidates = data.get(
        "candidates",
        []
    )

    if not candidates:

        prompt_feedback = data.get(
            "promptFeedback"
        )

        logger.error(
            "Gemini returned no candidates. "
            "promptFeedback=%s",
            json.dumps(
                prompt_feedback,
                ensure_ascii=False
            )
            if prompt_feedback
            else "none"
        )

        return ""

    candidate = candidates[0]

    finish_reason = candidate.get(
        "finishReason"
    )

    logger.info(
        "Gemini finishReason=%s",
        finish_reason
    )

    content = candidate.get(
        "content"
    ) or {}

    parts = content.get(
        "parts"
    ) or []

    text_parts = []

    for part in parts:

        if not isinstance(
            part,
            dict
        ):

            continue

        text_value = part.get(
            "text"
        )

        if isinstance(
            text_value,
            str
        ):

            text_parts.append(
                text_value
            )

    text = "".join(
        text_parts
    ).strip()

    return text


# ============================================================
# GEMINI REQUEST
# ============================================================

async def gemini_request(
    client,
    model,
    payload,
    headers
):

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    response = None

    for attempt in range(
        GEMINI_MAX_RETRIES
    ):

        try:

            response = await client.post(
                url,
                headers=headers,
                json=payload
            )

        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError
        ) as error:

            logger.warning(
                "Gemini network error "
                "(model=%s attempt=%s/%s): %s",
                model,
                attempt + 1,
                GEMINI_MAX_RETRIES,
                error
            )

            if attempt >= GEMINI_MAX_RETRIES - 1:

                raise RuntimeError(
                    "Gemini network error."
                ) from error

            delay = 3 * (2 ** attempt)

            await asyncio.sleep(
                delay
            )

            continue

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        if response.status_code == 200:

            try:

                data = response.json()

            except Exception as error:

                logger.error(
                    "Gemini HTTP response is not JSON: %s",
                    response.text[:5000]
                )

                raise RuntimeError(
                    "Gemini returned invalid HTTP JSON."
                ) from error

            return data

        # ----------------------------------------------------
        # RATE LIMIT
        # ----------------------------------------------------

        if response.status_code == 429:

            if attempt >= GEMINI_MAX_RETRIES - 1:

                raise RuntimeError(
                    "Gemini API rate limit exceeded. "
                    "Попробуй немного позже."
                )

            retry_after = response.headers.get(
                "Retry-After"
            )

            if retry_after:

                try:

                    delay = float(
                        retry_after
                    )

                except ValueError:

                    delay = 3 * (2 ** attempt)

            else:

                delay = 3 * (2 ** attempt)

            logger.warning(
                "Gemini 429. "
                "Retry %s/%s after %.1fs",
                attempt + 1,
                GEMINI_MAX_RETRIES,
                delay
            )

            await asyncio.sleep(
                delay
            )

            continue

        # ----------------------------------------------------
        # SERVER ERRORS
        # ----------------------------------------------------

        if response.status_code in (
            500,
            502,
            503,
            504
        ):

            if attempt >= GEMINI_MAX_RETRIES - 1:

                logger.error(
                    "Gemini server error: %s",
                    response.text[:3000]
                )

                response.raise_for_status()

            delay = 3 * (2 ** attempt)

            logger.warning(
                "Gemini server error %s. "
                "Retry in %.1fs",
                response.status_code,
                delay
            )

            await asyncio.sleep(
                delay
            )

            continue

        # ----------------------------------------------------
        # OTHER ERROR
        # ----------------------------------------------------

        logger.error(
            "Gemini HTTP %s: %s",
            response.status_code,
            response.text[:5000]
        )

        try:

            error_data = response.json()

        except Exception:

            error_data = None

        if error_data:

            error_message = (
                error_data
                .get("error", {})
                .get("message")
            )

            if error_message:

                raise RuntimeError(
                    f"Gemini API error: {error_message}"
                )

        response.raise_for_status()

    raise RuntimeError(
        "Gemini request failed."
    )


# ============================================================
# ANALYZE WITH GEMINI
# ============================================================

async def analyze_with_gemini(
    player1,
    player2
):

    prompt = """
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
Оцени отображаемое имя профиля:
читаемость, стиль, запоминаемость,
оригинальность и то, насколько хорошо оно выглядит
как Telegram display name.

USERNAME:
Оцени @username:
читаемость, запоминаемость, оригинальность,
простоту и визуальный стиль.

BIO:
Оцени bio:
качество текста, краткость, оригинальность,
характер и оформление.

Если bio отсутствует, не выдумывай текст.

COHERENCE:
Оцени сочетание:
NAME + USERNAME + BIO + AVATAR.

Аватар используется ТОЛЬКО здесь и в VIBE.
Отдельной оценки AVATAR НЕТ.

VIBE:
Оцени общий стиль профиля:
визуальное впечатление, атмосферу,
цельность, характер и запоминаемость.

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
- других чувствительных персональных характеристиках.

Не выдумывай информацию.

Используй весь диапазон 0-10.
Не ставь одинаковые оценки без причины.

Сделай короткий смешной русский вердикт,
максимум 180 символов.

ВАЖНО:
Ответ должен быть ОДНИМ валидным JSON-объектом.
Никакого Markdown.
Никаких ```json.
Никакого текста до JSON.
Никакого текста после JSON.

Формат:

{
  "players": [
    {
      "name": 0.0,
      "username": 0.0,
      "bio": 0.0,
      "coherence": 0.0,
      "vibe": 0.0
    },
    {
      "name": 0.0,
      "username": 0.0,
      "bio": 0.0,
      "coherence": 0.0,
      "vibe": 0.0
    }
  ],
  "verdict": "короткий смешной вердикт"
}
"""

    def profile_text(profile):

        return (
            f"\nUSERNAME: {profile.username}"
            f"\nNAME: {profile.name}"
            f"\nBIO: {profile.bio or '(нет bio)'}"
        )

    parts = [

        {
            "text": prompt
        },

        {
            "text":
                "\n\nPLAYER 1"
                +
                profile_text(player1)
        },

        {
            "text":
                "\n\nPLAYER 2"
                +
                profile_text(player2)
        }
    ]

    # ========================================================
    # AVATAR 1
    # ========================================================

    if player1.avatar:

        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(
                        player1.avatar
                    ).decode("utf-8")
                }
            }
        )

        parts.append(
            {
                "text":
                    "Следующее изображение — аватар PLAYER 1."
            }
        )

    else:

        parts.append(
            {
                "text":
                    "PLAYER 1 не имеет аватара."
            }
        )

    # ========================================================
    # AVATAR 2
    # ========================================================

    if player2.avatar:

        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(
                        player2.avatar
                    ).decode("utf-8")
                }
            }
        )

        parts.append(
            {
                "text":
                    "Следующее изображение — аватар PLAYER 2."
            }
        )

    else:

        parts.append(
            {
                "text":
                    "PLAYER 2 не имеет аватара."
            }
        )

    # ========================================================
    # PAYLOAD
    # ========================================================

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": parts
            }
        ],

        "generationConfig": {
            "responseMimeType": "application/json",

            "responseJsonSchema":
                GEMINI_RESPONSE_SCHEMA,

            "maxOutputTokens": 1000,

            "temperature": 0.7
        }
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    # ========================================================
    # MODELS
    # ========================================================

    models = []

    if GEMINI_MODEL:

        models.append(
            GEMINI_MODEL
        )

    if (
        GEMINI_FALLBACK_MODEL
        and
        GEMINI_FALLBACK_MODEL
        not in models
    ):

        models.append(
            GEMINI_FALLBACK_MODEL
        )

    # ========================================================
    # REQUEST
    # ========================================================

    async with GEMINI_SEMAPHORE:

        async with httpx.AsyncClient(
            timeout=GEMINI_TIMEOUT
        ) as client:

            last_error = None

            for model in models:

                # ------------------------------------------------
                # Try the model.
                # If HTTP succeeds but JSON is broken,
                # retry the same model before fallback.
                # ------------------------------------------------

                for parse_attempt in range(2):

                    try:

                        logger.info(
                            "Calling Gemini model=%s "
                            "parse_attempt=%s",
                            model,
                            parse_attempt + 1
                        )

                        data = await gemini_request(
                            client,
                            model,
                            payload,
                            headers
                        )

                        # ----------------------------------------
                        # Log safety/block reason if present
                        # ----------------------------------------

                        if data.get(
                            "promptFeedback"
                        ):

                            logger.info(
                                "Gemini promptFeedback: %s",
                                json.dumps(
                                    data["promptFeedback"],
                                    ensure_ascii=False
                                )[:3000]
                            )

                        # ----------------------------------------
                        # Extract text
                        # ----------------------------------------

                        text = extract_gemini_text(
                            data
                        )

                        if not text:

                            logger.error(
                                "Gemini returned no text. "
                                "Full response: %s",
                                json.dumps(
                                    data,
                                    ensure_ascii=False
                                )[:10000]
                            )

                            raise ValueError(
                                "no JSON object: "
                                "Gemini returned no text"
                            )

                        logger.info(
                            "Gemini response text: %s",
                            text[:5000]
                        )

                        # ----------------------------------------
                        # Parse
                        # ----------------------------------------

                        result = extract_json_object(
                            text
                        )

                        # ----------------------------------------
                        # Validate
                        # ----------------------------------------

                        validate_gemini_result(
                            result
                        )

                        logger.info(
                            "Gemini JSON parsed successfully "
                            "with model=%s",
                            model
                        )

                        return result

                    except Exception as error:

                        last_error = error

                        logger.warning(
                            "Gemini parse/request attempt failed "
                            "(model=%s attempt=%s): %s",
                            model,
                            parse_attempt + 1,
                            error
                        )

                        if parse_attempt == 0:

                            await asyncio.sleep(1)

                            continue

                        break

            # ====================================================
            # ALL FAILED
            # ====================================================

            logger.error(
                "All Gemini attempts failed. Last error: %s",
                last_error
            )

            raise RuntimeError(
                f"Gemini JSON parsing failed: "
                f"{last_error or 'unknown error'}"
            )


# ============================================================
# VALIDATE GEMINI RESULT
# ============================================================

def validate_gemini_result(result):

    if not isinstance(
        result,
        dict
    ):

        raise ValueError(
            "Gemini result is not an object"
        )

    players = result.get(
        "players"
    )

    if not isinstance(
        players,
        list
    ):

        raise ValueError(
            "Gemini players is not a list"
        )

    if len(players) != 2:

        raise ValueError(
            f"Gemini returned {len(players)} players"
        )

    for index, player in enumerate(players):

        if not isinstance(
            player,
            dict
        ):

            raise ValueError(
                f"Player {index + 1} is not an object"
            )

        for key in WEIGHTS:

            if key not in player:

                raise ValueError(
                    f"Player {index + 1} "
                    f"missing field: {key}"
                )

            try:

                value = float(
                    player[key]
                )

            except Exception:

                raise ValueError(
                    f"Player {index + 1} "
                    f"field {key} is not numeric"
                )

            if not 0 <= value <= 10:

                raise ValueError(
                    f"Player {index + 1} "
                    f"field {key} outside 0-10"
                )

    verdict = result.get(
        "verdict"
    )

    if verdict is None:

        result["verdict"] = "MOG суд вынес вердикт."

    else:

        result["verdict"] = str(
            verdict
        )[:300]


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

    if len(raw_players) != 2:

        raise RuntimeError(
            "Gemini returned invalid number of players."
        )

    players = []

    for raw in raw_players:

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

            scores[key] = max(
                0.0,
                min(
                    10.0,
                    value
                )
            )

        overall = sum(
            scores[key] * WEIGHTS[key]
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
        abs(score1 - score2),
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
        "players": players,
        "winner": winner,
        "loser": loser,
        "winner_name": winner_name,
        "difference": difference,
        "status": status,
        "verdict": str(
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
        text[:max_chars - 1]
        + "…"
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

    words = str(
        text or ""
    ).split()

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
            ).convert("RGB")

            width, height = avatar.size

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
        center_x - avatar_size // 2
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

    label = (
        "HOST PROFILE"
        if player_index == 0
        else "GUEST PROFILE"
    )

    label_font = f(
        "profile_label",
        True
    )

    label_box_w = 245
    label_box_h = 48

    label_x = (
        center_x - label_box_w // 2
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
            panel_right - badge_w - 38
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

    player1_profile_y = 150

    draw_profile_header(
        image,
        draw,
        player1,
        result,
        0,
        player1_profile_y
    )

    player1_score_y = 670

    draw_score_panel(
        draw,
        result,
        0,
        player1_score_y
    )

    vs_y = 1280

    draw_vs(
        draw,
        vs_y
    )

    player2_profile_y = 1415

    draw_profile_header(
        image,
        draw,
        player2,
        result,
        1,
        player2_profile_y
    )

    player2_score_y = 1935

    draw_score_panel(
        draw,
        result,
        1,
        player2_score_y
    )

    if result["loser"] is not None:

        stamp = create_mogged_stamp()

        stamp_x = (
            center_x - stamp.width // 2
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

    verdict = str(
        result.get(
            "verdict",
            ""
        )
    ).strip()

    verdict = truncate_text(
        verdict,
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

    verdict_y = result_y + 175

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
# KEYBOARD
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
# RESOLVE TARGET
# ============================================================

async def resolve_target(
    message: Message,
    bot: Bot
):

    if (
        message.reply_to_message
        and
        message.reply_to_message.from_user
    ):

        return (
            message.from_user.id,
            message.reply_to_message.from_user.id
        )

    text = message.text or ""

    match = re.match(
        r"^(\.мог|/mog)"
        r"(?:\s+@([A-Za-z0-9_]{5,32}))?$",
        text,
        re.IGNORECASE
    )

    if (
        match
        and
        match.group(2)
    ):

        username = match.group(2)

        try:

            target = await bot.get_chat(
                "@" + username
            )

            return (
                message.from_user.id,
                target.id
            )

        except Exception as error:

            logger.warning(
                "Username lookup failed: %s",
                error
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
    player2_id: int
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
        "🧠 Gemini считает оценки..."
    )

    try:

        player1 = await get_profile(
            bot,
            player1_id
        )

        player2 = await get_profile(
            bot,
            player2_id
        )

        ai_result = await analyze_with_gemini(
            player1,
            player2
        )

        result = calculate_scores(
            ai_result,
            [
                player1.username,
                player2.username
            ]
        )

        register_battle(
            player1,
            player2,
            result
        )

        card = create_card(
            player1,
            player2,
            result
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
                filename="mog_battle.png"
            ),
            caption=caption,
            reply_markup=result_keyboard(
                player1_id,
                player2_id
            )
        )

        try:

            await status_message.delete()

        except Exception:

            pass

    except Exception as error:

        logger.exception(
            "MOG failed"
        )

        error_text = str(
            error
        )

        if len(error_text) > 1200:

            error_text = (
                error_text[:1200]
                +
                "..."
            )

        error_text = html.escape(
            error_text
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
        r"(?:\s+@[A-Za-z0-9_]{5,32})?$"
    )
)
async def mog_command(
    message: Message,
    bot: Bot
):

    target = await resolve_target(
        message,
        bot
    )

    if not target:

        await message.answer(
            "<b>⚔️ MOG AI</b>\n\n"

            "Использование:\n\n"

            "1️⃣ Ответь на сообщение человека:\n"
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
        player2_id
    )


# ============================================================
# START
# ============================================================

@dp.message(
    Command("start")
)
async def start_command(
    message: Message
):

    await message.answer(
        "<b>⚔️ MOG AI</b>\n\n"

        "AI-баттлы Telegram-профилей.\n\n"

        "🥊 Ответь на сообщение:\n"
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
    message: Message
):

    await message.answer(
        "<b>⚔️ MOG AI — COMMANDS</b>\n\n"

        "<code>.мог</code>\n"
        "Сравнить себя с человеком, "
        "на сообщение которого ты ответил.\n\n"

        "<code>.мог @username</code>\n"
        "Сравнить себя с username.\n\n"

        "<code>/mog</code>\n"
        "То же самое, что .мог.\n\n"

        "<code>/stats</code>\n"
        "Твоя статистика.\n\n"

        "<code>/top</code>\n"
        "Топ игроков.\n\n"

        "<code>/history</code>\n"
        "Последние баттлы.\n\n"

        "<code>/help</code>\n"
        "Список команд."
    )


# ============================================================
# STATS
# ============================================================

@dp.message(
    Command("stats")
)
async def stats_command(
    message: Message
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

        f"⚔️ Battles: "
        f"<b>{battles}</b>\n"

        f"🏆 Wins: "
        f"<b>{wins}</b>\n"

        f"💀 Losses: "
        f"<b>{losses}</b>\n"

        f"🤝 Draws: "
        f"<b>{draws}</b>\n\n"

        f"📈 Winrate: "
        f"<b>{winrate:.1f}%</b>\n"

        f"⭐ Average score: "
        f"<b>{average:.2f}/10</b>"
    )


# ============================================================
# TOP
# ============================================================

@dp.message(
    Command("top")
)
async def top_command(
    message: Message
):

    data = load_data()

    users = []

    for user_id, user in data["users"].items():

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
                "average": average
            }
        )

    users.sort(
        key=lambda x: (
            x["wins"],
            x["average"]
        ),
        reverse=True
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
        "🥉"
    ]

    text = (
        "<b>🏆 MOG TOP 10</b>\n\n"
    )

    for index, user in enumerate(users):

        if index < 3:

            position = medals[index]

        else:

            position = f"<b>{index + 1}.</b>"

        username = html.escape(
            str(
                user["username"]
            )
        )

        text += (
            f"{position} "
            f"{username} — "
            f"<b>{user['wins']}</b> wins "
            f"• {user['average']:.2f}/10\n"
        )

    await message.answer(
        text
    )


# ============================================================
# HISTORY
# ============================================================

@dp.message(
    Command("history")
)
async def history_command(
    message: Message
):

    data = load_data()

    battles = data["battles"][-10:]

    if not battles:

        await message.answer(
            "📜 История пока пустая."
        )

        return

    battles.reverse()

    text = (
        "<b>📜 LAST MOG BATTLES</b>\n\n"
    )

    for battle in battles:

        if battle["winner"] == 0:

            winner = battle["player1"]

        elif battle["winner"] == 1:

            winner = battle["player2"]

        else:

            winner = "DRAW"

        p1 = html.escape(
            str(
                battle["player1"]
            )
        )

        p2 = html.escape(
            str(
                battle["player2"]
            )
        )

        winner = html.escape(
            str(winner)
        )

        text += (
            f"⚔️ "
            f"{p1} "
            f"<b>{battle['score1']:.2f}</b>"
            f" × "
            f"<b>{battle['score2']:.2f}</b> "
            f"{p2}\n"

            f"🏆 {winner}\n\n"
        )

    await message.answer(
        text
    )


# ============================================================
# REMATCH
# ============================================================

@dp.callback_query(
    F.data.startswith(
        "rematch:"
    )
)
async def rematch_callback(
    callback: CallbackQuery,
    bot: Bot
):

    try:

        parts = callback.data.split(":")

        if len(parts) != 3:

            raise ValueError(
                "Invalid callback data"
            )

        player1_id = int(
            parts[1]
        )

        player2_id = int(
            parts[2]
        )

        await callback.answer(
            "🔄 Новый MOG!"
        )

        await run_mog(
            callback.message,
            bot,
            player1_id,
            player2_id
        )

    except Exception:

        logger.exception(
            "Rematch failed"
        )

        await callback.answer(
            "❌ Ошибка",
            show_alert=True
        )


# ============================================================
# DETAILS
# ============================================================

@dp.callback_query(
    F.data == "details"
)
async def details_callback(
    callback: CallbackQuery
):

    await callback.answer(
        "Карточка содержит все пять оценок.",
        show_alert=True
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "Starting MOG AI bot..."
    )

    logger.info(
        "Gemini primary model: %s",
        GEMINI_MODEL
    )

    logger.info(
        "Gemini fallback model: %s",
        GEMINI_FALLBACK_MODEL
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    try:

        me = await bot.get_me()

        logger.info(
            "Logged in as @%s",
            me.username
        )

        await dp.start_polling(
            bot
        )

    finally:

        await bot.session.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )
