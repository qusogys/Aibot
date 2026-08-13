import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import datetime

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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Актуальная стабильная модель
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash"
).strip()

DATA_FILE = "mog_data.json"

# Сколько раз повторять запрос при 429 / временных ошибках
GEMINI_RETRIES = 4

# Базовая задержка перед retry
GEMINI_RETRY_DELAY = 3


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("mog_ai")

dp = Dispatcher()


# ============================================================
# SCORING
# ============================================================

# 5 категорий:
#
# NAME       — имя / ник
# USERNAME   — @username
# BIO        — описание
# COHERENCE  — сочетание элементов профиля
# VIBE       — общий вайб
#
# Сумма = 1.00

WEIGHTS = {
    "name": 0.15,
    "username": 0.20,
    "bio": 0.20,
    "coherence": 0.20,
    "vibe": 0.25,
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
#
# ВСЕ РАЗМЕРЫ ШРИФТОВ ЗДЕСЬ.
#
# Если хочешь увеличить/уменьшить текст —
# меняй только числа ниже.
#
# Например:
#
# "title": 56
#
# ============================================================

FONT_SIZES = {
    # Header
    "title": 58,
    "subtitle": 22,

    # Profile
    "profile_label": 23,
    "profile_name": 43,
    "profile_username": 27,

    # Categories
    "category": 25,
    "score": 25,

    # Overall
    "overall_label": 24,
    "overall_score": 43,

    # Result
    "status": 37,
    "winner": 31,
    "difference": 29,
    "verdict": 25,

    # VS
    "vs": 72,

    # Stamp
    "stamp": 54,

    # Footer
    "footer": 17,
}


# ============================================================
# PROFILE OBJECT
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
        "time": datetime.utcnow().isoformat(),

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
# GEMINI
# ============================================================

async def analyze_with_gemini(
    player1,
    player2
):

    prompt = """
You are the AI judge of a humorous Telegram profile
comparison game called MOG.

Compare TWO Telegram profiles.

Judge ONLY profile presentation.
Do not judge the person themselves.

There are EXACTLY FIVE categories:

1. name
2. username
3. bio
4. coherence
5. vibe

IMPORTANT:
Do NOT score avatar separately.

The avatar can be considered only when judging:
- coherence
- vibe

CATEGORY DEFINITIONS:

NAME:
Judge the displayed Telegram name.
Consider readability, memorability, style,
originality and how well it works as a profile name.

USERNAME:
Judge the @username.
Consider readability, memorability, uniqueness,
length, style and how well it looks.

BIO:
Judge the bio.
Consider writing quality, originality,
brevity, personality and presentation.

COHERENCE:
Judge how well NAME + USERNAME + BIO + AVATAR
work together as one profile.

VIBE:
Judge the overall profile presentation,
style, atmosphere and impression.

Score every category from 0.0 to 10.0.

Use the complete 0-10 scale.
Do not give everybody similar scores.

Do not infer or judge:

- race
- ethnicity
- religion
- politics
- political affiliation
- sexual orientation
- health
- disability
- body
- physical attractiveness
- sensitive personal characteristics
- exact age

Do not invent information.

If bio is missing:
score the absence of bio naturally.
Do not invent a bio.

Return ONLY valid JSON.

Required JSON:

{
  "players": [
    {
      "name": 0.0,
      "username": 0.0,
      "bio": 0.0,
      "coherence": 0.0,
      "vibe": 0.0,
      "reasons": {
        "name": "short reason",
        "username": "short reason",
        "bio": "short reason",
        "coherence": "short reason",
        "vibe": "short reason"
      }
    },
    {
      "name": 0.0,
      "username": 0.0,
      "bio": 0.0,
      "coherence": 0.0,
      "vibe": 0.0,
      "reasons": {
        "name": "short reason",
        "username": "short reason",
        "bio": "short reason",
        "coherence": "short reason",
        "vibe": "short reason"
      }
    }
  ],
  "verdict": "Короткий смешной русский вердикт максимум 180 символов"
}
"""

    def profile_text(profile):

        return (
            f"USERNAME: {profile.username}\n"
            f"NAME: {profile.name}\n"
            f"BIO: {profile.bio or '(нет био)'}"
        )

    parts = [

        {
            "text": prompt
        },

        {
            "text":
                "\n\nPLAYER 1\n"
                +
                profile_text(player1)
        },

        {
            "text":
                "\n\nPLAYER 2\n"
                +
                profile_text(player2)
        }
    ]

    if player1.avatar:

        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data":
                        base64.b64encode(
                            player1.avatar
                        ).decode("utf-8")
                }
            }
        )

        parts.append(
            {
                "text":
                    "The previous image is PLAYER 1 avatar."
            }
        )

    if player2.avatar:

        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data":
                        base64.b64encode(
                            player2.avatar
                        ).decode("utf-8")
                }
            }
        )

        parts.append(
            {
                "text":
                    "The previous image is PLAYER 2 avatar."
            }
        )

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{GEMINI_MODEL}:generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": parts
            }
        ],

        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 1500
        }
    }

    last_error = None

    for attempt in range(
        GEMINI_RETRIES
    ):

        try:

            async with httpx.AsyncClient(
                timeout=120
            ) as client:

                response = await client.post(
                    url,
                    headers={
                        "Content-Type":
                            "application/json"
                    },
                    params={
                        "key":
                            GEMINI_API_KEY
                    },
                    json=payload
                )

            # =================================================
            # RATE LIMIT
            # =================================================

            if response.status_code == 429:

                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after:

                    try:

                        delay = float(
                            retry_after
                        )

                    except ValueError:

                        delay = (
                            GEMINI_RETRY_DELAY
                            *
                            (attempt + 1)
                        )

                else:

                    delay = (
                        GEMINI_RETRY_DELAY
                        *
                        (attempt + 1)
                    )

                logger.warning(
                    "Gemini 429. Retry %s/%s in %.1fs",
                    attempt + 1,
                    GEMINI_RETRIES,
                    delay
                )

                await asyncio.sleep(
                    delay
                )

                continue

            # =================================================
            # TEMPORARY SERVER ERRORS
            # =================================================

            if response.status_code in (
                500,
                502,
                503,
                504
            ):

                delay = (
                    GEMINI_RETRY_DELAY
                    *
                    (attempt + 1)
                )

                logger.warning(
                    "Gemini HTTP %s. Retry %s/%s in %.1fs",
                    response.status_code,
                    attempt + 1,
                    GEMINI_RETRIES,
                    delay
                )

                await asyncio.sleep(
                    delay
                )

                continue

            if response.status_code != 200:

                logger.error(
                    "Gemini HTTP %s: %s",
                    response.status_code,
                    response.text[:1500]
                )

            response.raise_for_status()

            data = response.json()

            break

        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError
        ) as error:

            last_error = error

            delay = (
                GEMINI_RETRY_DELAY
                *
                (attempt + 1)
            )

            logger.warning(
                "Gemini network error. "
                "Retry %s/%s in %.1fs: %s",
                attempt + 1,
                GEMINI_RETRIES,
                delay,
                error
            )

            await asyncio.sleep(
                delay
            )

    else:

        raise RuntimeError(
            "Gemini не отвечает после нескольких попыток."
        ) from last_error

    # =========================================================
    # EXTRACT RESPONSE
    # =========================================================

    try:

        text = (
            data[
                "candidates"
            ][0][
                "content"
            ][
                "parts"
            ][0][
                "text"
            ]
        )

    except Exception:

        logger.error(
            "Invalid Gemini response: %s",
            data
        )

        raise RuntimeError(
            "Gemini returned an invalid response."
        )

    text = re.sub(
        r"```json\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"```\s*",
        "",
        text
    )

    text = text.strip()

    # Иногда модель всё-таки добавляет текст вокруг JSON
    match = re.search(
        r"\{.*\}",
        text,
        re.DOTALL
    )

    if not match:

        logger.error(
            "Gemini did not return JSON: %s",
            text
        )

        raise RuntimeError(
            "Gemini did not return JSON."
        )

    try:

        return json.loads(
            match.group(0)
        )

    except json.JSONDecodeError:

        logger.error(
            "Gemini invalid JSON: %s",
            text
        )

        raise RuntimeError(
            "Gemini JSON parsing failed."
        )


# ============================================================
# SCORE CALCULATION
# ============================================================

def clamp(value):

    try:

        value = float(value)

    except Exception:

        value = 0.0

    return max(
        0.0,
        min(
            10.0,
            value
        )
    )


def calculate_scores(
    ai_result,
    names
):

    raw_players = ai_result.get(
        "players",
        []
    )

    if len(raw_players) < 2:

        raise RuntimeError(
            "Gemini returned fewer than two players."
        )

    players = []

    for raw in raw_players[:2]:

        scores = {}

        for key in WEIGHTS:

            scores[key] = clamp(
                raw.get(
                    key,
                    0
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
        abs(
            score1 - score2
        ),
        2
    )

    # ========================================================
    # DRAW
    # ========================================================

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

    if winner is None:

        winner_name = "DRAW"

    else:

        winner_name = names[winner]

    return {
        "players": players,

        "winner": winner,
        "loser": loser,

        "winner_name": winner_name,

        "difference": difference,

        "status": status,

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


def load_font(
    size,
    bold=False
):

    cache_key = (
        size,
        bold
    )

    if cache_key in _FONT_CACHE:

        return _FONT_CACHE[
            cache_key
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

            font = ImageFont.truetype(
                path,
                size
            )

            _FONT_CACHE[
                cache_key
            ] = font

            return font

    font = ImageFont.load_default()

    _FONT_CACHE[
        cache_key
    ] = font

    return font


def font(
    name,
    bold=False
):

    return load_font(
        FONT_SIZES[name],
        bold
    )


# ============================================================
# TEXT HELPERS
# ============================================================

def truncate(
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
        +
        "…"
    )


def draw_centered(
    draw,
    text,
    center_x,
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
        bbox[2]
        -
        bbox[0]
    )

    draw.text(
        (
            center_x - width / 2,
            y
        ),
        text,
        font=font_obj,
        fill=fill
    )


def draw_wrapped_centered(
    draw,
    text,
    center_x,
    y,
    max_width,
    font_obj,
    fill,
    line_spacing=8
):

    words = str(
        text or ""
    ).split()

    if not words:

        return y

    lines = []
    current = ""

    for word in words:

        test = (
            word
            if not current
            else current + " " + word
        )

        bbox = draw.textbbox(
            (0, 0),
            test,
            font=font_obj
        )

        width = (
            bbox[2]
            -
            bbox[0]
        )

        if width <= max_width:

            current = test

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

    line_height = (
        font_obj.size
        +
        line_spacing
    )

    for index, line in enumerate(
        lines
    ):

        draw_centered(
            draw,
            line,
            center_x,
            y + index * line_height,
            font_obj,
            fill
        )

    return (
        y
        +
        len(lines) * line_height
    )


# ============================================================
# AVATAR
# ============================================================

def make_avatar(
    data,
    size=270
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
                "#24242c"
            )

    else:

        avatar = Image.new(
            "RGB",
            (
                size,
                size
            ),
            "#24242c"
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

    draw = ImageDraw.Draw(
        result
    )

    draw.ellipse(
        (
            3,
            3,
            size - 4,
            size - 4
        ),
        outline="#f4c542",
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

    yellow = "#f4c542"

    points = [
        (
            center_x - 75,
            y + 55
        ),
        (
            center_x - 62,
            y - 15
        ),
        (
            center_x - 20,
            y + 25
        ),
        (
            center_x,
            y - 32
        ),
        (
            center_x + 22,
            y + 25
        ),
        (
            center_x + 65,
            y - 15
        ),
        (
            center_x + 78,
            y + 55
        )
    ]

    draw.polygon(
        points,
        fill=yellow
    )

    draw.rounded_rectangle(
        (
            center_x - 78,
            y + 45,
            center_x + 78,
            y + 70
        ),
        radius=7,
        fill=yellow
    )


# ============================================================
# MOGGED STAMP
# ============================================================

def create_mogged_stamp():

    stamp_w = 500
    stamp_h = 110

    stamp = Image.new(
        "RGBA",
        (
            stamp_w,
            stamp_h
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
            stamp_w - 4,
            stamp_h - 4
        ),
        radius=18,
        fill="#ef3030",
        outline="white",
        width=5
    )

    draw_centered(
        draw,
        "MOGGED",
        stamp_w // 2,
        23,
        font("stamp", True),
        "white"
    )

    return stamp.rotate(
        10,
        expand=True,
        resample=Image.Resampling.BICUBIC
    )


# ============================================================
# PROFILE SECTION
# ============================================================

def draw_profile_section(
    image,
    draw,
    player,
    result,
    player_index,
    y
):

    W, H = image.size

    BG = "#09090d"
    PANEL = "#101017"
    BORDER = "#20202a"

    WHITE = "#f4f4f7"
    MUTED = "#858591"
    YELLOW = "#f4c542"

    center_x = W // 2

    panel_left = 70
    panel_right = W - 70

    panel_top = y
    panel_bottom = y + 590

    # ========================================================
    # PANEL
    # ========================================================

    draw.rounded_rectangle(
        (
            panel_left,
            panel_top,
            panel_right,
            panel_bottom
        ),
        radius=30,
        fill=PANEL,
        outline=BORDER,
        width=3
    )

    # ========================================================
    # CROWN
    # ========================================================

    if result["winner"] == player_index:

        draw_crown(
            draw,
            center_x,
            panel_top + 48
        )

    # ========================================================
    # AVATAR
    # ========================================================

    avatar_size = 270

    avatar = make_avatar(
        player.avatar,
        avatar_size
    )

    avatar_x = (
        center_x
        -
        avatar_size // 2
    )

    avatar_y = (
        panel_top
        +
        75
    )

    image.paste(
        avatar,
        (
            avatar_x,
            avatar_y
        ),
        avatar
    )

    # ========================================================
    # PROFILE LABEL
    # ========================================================

    if player_index == result["winner"]:

        label = "WINNER PROFILE"

    elif player_index == result["loser"]:

        label = "GUEST PROFILE"

    else:

        label = "PROFILE"

    label_font = font(
        "profile_label",
        True
    )

    bbox = draw.textbbox(
        (0, 0),
        label,
        font=label_font
    )

    label_width = (
        bbox[2]
        -
        bbox[0]
        +
        50
    )

    label_x1 = (
        center_x
        -
        label_width // 2
    )

    label_x2 = (
        center_x
        +
        label_width // 2
    )

    label_y = (
        panel_top
        +
        355
    )

    label_fill = (
        YELLOW
        if player_index == result["winner"]
        else "#292932"
    )

    label_text = (
        "#18181d"
        if player_index == result["winner"]
        else MUTED
    )

    draw.rounded_rectangle(
        (
            label_x1,
            label_y,
            label_x2,
            label_y + 48
        ),
        radius=24,
        fill=label_fill
    )

    draw_centered(
        draw,
        label,
        center_x,
        label_y + 8,
        label_font,
        label_text
    )

    # ========================================================
    # NAME
    # ========================================================

    name_y = (
        panel_top
        +
        420
    )

    name = truncate(
        player.name,
        26
    )

    draw_centered(
        draw,
        name,
        center_x,
        name_y,
        font("profile_name", True),
        WHITE
    )

    # ========================================================
    # USERNAME
    # ========================================================

    username_y = (
        name_y
        +
        55
    )

    username = truncate(
        player.username,
        28
    )

    draw_centered(
        draw,
        username,
        center_x,
        username_y,
        font("profile_username", True),
        YELLOW
    )

    # ========================================================
    # MOGGED STAMP
    # ========================================================

    if result["loser"] == player_index:

        stamp = create_mogged_stamp()

        stamp_x = (
            center_x
            -
            stamp.width // 2
        )

        stamp_y = (
            panel_top
            +
            455
        )

        image.paste(
            stamp,
            (
                stamp_x,
                stamp_y
            ),
            stamp
        )


# ============================================================
# SCORE SECTION
# ============================================================

def draw_score_section(
    draw,
    player,
    result,
    player_index,
    y
):

    W = draw._image.width

    WHITE = "#f4f4f7"
    MUTED = "#858591"

    YELLOW = "#f4c542"
    PURPLE = "#8b5cf6"

    panel_left = 70
    panel_right = W - 70

    panel_top = y
    panel_bottom = y + 600

    # ========================================================
    # PANEL
    # ========================================================

    draw.rounded_rectangle(
        (
            panel_left,
            panel_top,
            panel_right,
            panel_bottom
        ),
        radius=30,
        fill="#101017",
        outline="#20202a",
        width=3
    )

    # ========================================================
    # OVERALL
    # ========================================================

    draw.text(
        (
            105,
            panel_top + 30
        ),
        "OVERALL SCORE",
        font=font(
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
            105,
            panel_top + 65
        ),
        f"{overall:.2f}",
        font=font(
            "overall_score",
            True
        ),
        fill=WHITE
    )

    if result["winner"] == player_index:

        draw.rounded_rectangle(
            (
                W - 280,
                panel_top + 32,
                W - 105,
                panel_top + 82
            ),
            radius=25,
            fill="#2c2410"
        )

        draw_centered(
            draw,
            "WINNER",
            W - 192,
            panel_top + 40,
            font(
                "profile_label",
                True
            ),
            YELLOW
        )

    # ========================================================
    # BARS
    # ========================================================

    label_x = 105

    bar_x = 330

    bar_width = 550

    score_x = 905

    start_y = (
        panel_top
        +
        145
    )

    row_height = 85

    for index, (
        label,
        key
    ) in enumerate(
        CATEGORIES
    ):

        row_y = (
            start_y
            +
            index * row_height
        )

        score = result[
            "players"
        ][player_index][key]

        # Label

        draw.text(
            (
                label_x,
                row_y
            ),
            label,
            font=font(
                "category",
                True
            ),
            fill=MUTED
        )

        # Background

        draw.rounded_rectangle(
            (
                bar_x,
                row_y + 5,
                bar_x + bar_width,
                row_y + 31
            ),
            radius=14,
            fill="#292932"
        )

        # Fill

        fill_width = (
            bar_width
            *
            score
            /
            10
        )

        if fill_width > 0:

            bar_color = (
                PURPLE
                if key == "vibe"
                else YELLOW
            )

            draw.rounded_rectangle(
                (
                    bar_x,
                    row_y + 5,
                    bar_x + fill_width,
                    row_y + 31
                ),
                radius=14,
                fill=bar_color
            )

        # Score

        draw.text(
            (
                score_x,
                row_y - 1
            ),
            f"{score:.2f}",
            font=font(
                "score",
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

    line_y = y + 50

    draw.line(
        (
            70,
            line_y,
            W // 2 - 95,
            line_y
        ),
        fill="#292932",
        width=3
    )

    draw.line(
        (
            W // 2 + 95,
            line_y,
            W - 70,
            line_y
        ),
        fill="#292932",
        width=3
    )

    draw.rounded_rectangle(
        (
            W // 2 - 100,
            y,
            W // 2 + 100,
            y + 105
        ),
        radius=35,
        fill="#0b0b10"
    )

    draw_centered(
        draw,
        "VS",
        W // 2,
        y + 10,
        font("vs", True),
        "#f4f4f7"
    )


# ============================================================
# CREATE CARD
# ============================================================

def create_card(
    player1,
    player2,
    result
):

    # Большая вертикальная карточка
    W = 1100
    H = 2400

    BG = "#09090d"
    WHITE = "#f4f4f7"
    MUTED = "#858591"
    YELLOW = "#f4c542"
    RED = "#ff3030"

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

    # ========================================================
    # HEADER
    # ========================================================

    draw_centered(
        draw,
        "MOG BATTLE",
        W // 2,
        35,
        font("title", True),
        YELLOW
    )

    draw_centered(
        draw,
        "AI PROFILE COMPARISON",
        W // 2,
        110,
        font("subtitle", True),
        MUTED
    )

    # ========================================================
    # PLAYER 1 PROFILE
    # ========================================================

    draw_profile_section(
        image,
        draw,
        player1,
        result,
        0,
        155
    )

    # ========================================================
    # PLAYER 1 SCORES
    # ========================================================

    draw_score_section(
        draw,
        player1,
        result,
        0,
        765
    )

    # ========================================================
    # VS
    # ========================================================

    draw_vs(
        draw,
        1395
    )

    # ========================================================
    # PLAYER 2 PROFILE
    # ========================================================

    draw_profile_section(
        image,
        draw,
        player2,
        result,
        1,
        1535
    )

    # ========================================================
    # PLAYER 2 SCORES
    # ========================================================

    draw_score_section(
        draw,
        player2,
        result,
        1,
        2145
    )

    # ========================================================
    # FINAL RESULT
    # ========================================================
    #
    # NOTE:
    # Score panel ends at 2745, but current card is 2400.
    #
    # Therefore we intentionally don't put a huge second
    # score panel after the second profile.
    #
    # Instead, create final result in a compact area
    # inside the lower panel.
    #
    # ========================================================

    # На самом деле обрежем нижнюю часть корректно:
    # пересоздадим размер карточки, если нужно.
    #
    # Чтобы оба score-блока полностью помещались,
    # карточку делаем ещё выше.

    return create_card_final(
        player1,
        player2,
        result
    )


# ============================================================
# FINAL CARD VERSION
# ============================================================

def create_card_final(
    player1,
    player2,
    result
):

    W = 1100
    H = 2850

    BG = "#09090d"
    WHITE = "#f4f4f7"
    MUTED = "#858591"
    YELLOW = "#f4c542"
    RED = "#ff3030"
    PURPLE = "#8b5cf6"

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

    # ========================================================
    # HEADER
    # ========================================================

    draw_centered(
        draw,
        "MOG BATTLE",
        W // 2,
        35,
        font("title", True),
        YELLOW
    )

    draw_centered(
        draw,
        "AI PROFILE COMPARISON",
        W // 2,
        110,
        font("subtitle", True),
        MUTED
    )

    # ========================================================
    # PLAYER 1
    # ========================================================

    draw_profile_section(
        image,
        draw,
        player1,
        result,
        0,
        155
    )

    draw_score_section(
        draw,
        player1,
        result,
        0,
        765
    )

    # ========================================================
    # VS
    # ========================================================

    draw_vs(
        draw,
        1395
    )

    # ========================================================
    # PLAYER 2
    # ========================================================

    draw_profile_section(
        image,
        draw,
        player2,
        result,
        1,
        1535
    )

    draw_score_section(
        draw,
        player2,
        result,
        1,
        2145
    )

    # ========================================================
    # FINAL RESULT
    # ========================================================

    result_y = 2780

    # Difference
    draw.text(
        (
            80,
            result_y
        ),
        "DIFFERENCE",
        font=font(
            "difference",
            True
        ),
        fill=MUTED
    )

    draw.text(
        (
            880,
            result_y
        ),
        f"{result['difference']:.2f}",
        font=font(
            "difference",
            True
        ),
        fill=YELLOW
    )

    # ========================================================
    # VERDICT
    # ========================================================

    verdict_y = result_y + 50

    verdict = str(
        result.get(
            "verdict",
            ""
        )
    )

    verdict = truncate(
        verdict,
        100
    )

    draw_wrapped_centered(
        draw,
        verdict,
        W // 2,
        verdict_y,
        W - 160,
        font("verdict", True),
        WHITE
    )

    # ========================================================
    # FOOTER
    # ========================================================

    draw.text(
        (
            80,
            H - 45
        ),
        "MOG AI  •  POWERED BY GEMINI",
        font=font(
            "footer",
            True
        ),
        fill=MUTED
    )

    # ========================================================
    # EXPORT
    # ========================================================

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
            message.reply_to_message.from_user.id
        )

    # ========================================================
    # USERNAME
    # ========================================================

    text = message.text or ""

    match = re.match(
        r"^(\.мог|/mog)"
        r"(?:\s+@([A-Za-z0-9_]{5,32}))?$",
        text,
        re.IGNORECASE
    )

    if match and match.group(2):

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
        "📝 Анализирую NAME...\n"
        "🔤 Анализирую USERNAME...\n"
        "📖 Анализирую BIO...\n"
        "🔗 Проверяю COHERENCE...\n"
        "✨ Оцениваю VIBE..."
    )

    try:

        # ====================================================
        # GET PROFILES
        # ====================================================

        player1 = await get_profile(
            bot,
            player1_id
        )

        player2 = await get_profile(
            bot,
            player2_id
        )

        # ====================================================
        # GEMINI
        # ====================================================

        ai_result = await analyze_with_gemini(
            player1,
            player2
        )

        # ====================================================
        # SCORES
        # ====================================================

        result = calculate_scores(
            ai_result,
            [
                player1.username,
                player2.username
            ]
        )

        # ====================================================
        # SAVE
        # ====================================================

        register_battle(
            player1,
            player2,
            result
        )

        # ====================================================
        # CARD
        # ====================================================

        card = create_card_final(
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
                result["winner_name"]
                +
                "</b>"
            )

        caption = (
            "⚔️ <b>MOG BATTLE</b>\n\n"

            f"{player1.username} "
            f"<b>{score1:.2f}/10</b>\n"

            f"{player2.username} "
            f"<b>{score2:.2f}/10</b>\n\n"

            f"{winner_text}\n"

            f"📊 Difference: "
            f"<b>{result['difference']:.2f}</b>\n\n"

            f"💬 {result['verdict']}"
        )

        # ====================================================
        # SEND
        # ====================================================

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

        error_text = str(error)

        if len(error_text) > 900:

            error_text = (
                error_text[:900]
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
# .МОГ / /MOG
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
        "Последние баттлы."
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

    if battles > 0:

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
                "username":
                    user["username"],

                "wins":
                    user["wins"],

                "battles":
                    user["battles"],

                "average":
                    average
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

    text = "<b>🏆 MOG TOP 10</b>\n\n"

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for index, user in enumerate(users):

        if index < 3:

            position = medals[index]

        else:

            position = f"<b>{index + 1}.</b>"

        text += (
            f"{position} "
            f"{user['username']} — "
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

        text += (
            f"⚔️ "
            f"{battle['player1']} "
            f"<b>{battle['score1']:.2f}</b>"
            f" × "
            f"<b>{battle['score2']:.2f}</b> "
            f"{battle['player2']}\n"

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
        "Подробные оценки находятся на карточке.",
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
        "Gemini model: %s",
        GEMINI_MODEL
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
