import asyncio
import base64
import html
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

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

# Current model requested for this bot.
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash"
).strip()

DATA_FILE = "mog_data.json"

# Gemini retry settings
GEMINI_MAX_RETRIES = 4
GEMINI_TIMEOUT = 120

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
# FONT SETTINGS
#
# ВСЕ размеры шрифтов меняются ТОЛЬКО ЗДЕСЬ.
# ============================================================

FONT_SIZES = {
    "title": 64,
    "subtitle": 25,

    "profile_label": 22,

    "name": 42,
    "username": 27,

    "category": 27,
    "score": 27,

    "overall_label": 24,
    "overall_score": 39,

    "status": 34,
    "winner": 42,

    "difference_label": 23,
    "difference_score": 29,

    "verdict": 25,
    "footer": 17,

    "vs": 62,
    "mogged": 52,

    "crown": 40,
}


# ============================================================
# SCORING
# ============================================================

# Только 5 категорий.
#
# Avatar не является отдельной категорией.
# Он используется Gemini при оценке coherence/vibe.

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

        data["users"][user_id][
            "username"
        ] = username


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

    score1 = result[
        "players"
    ][0]["overall"]

    score2 = result[
        "players"
    ][1]["overall"]

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

        "player1":
            player1.username,

        "player2":
            player2.username,

        "score1":
            score1,

        "score2":
            score2,

        "winner":
            result["winner"],

        "status":
            result["status"]
    }

    data["battles"].append(
        battle
    )

    # Keep latest 500.
    data["battles"] = (
        data["battles"][-500:]
    )

    save_data(data)


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

        photos = (
            await bot
            .get_user_profile_photos(
                user_id=user_id,
                limit=1
            )
        )

        if photos.total_count:

            photo = (
                photos.photos[0][-1]
            )

            telegram_file = (
                await bot.get_file(
                    photo.file_id
                )
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

You are comparing TWO Telegram profiles.

The goal is to judge profile presentation and style,
NOT the person behind the profile.

There are EXACTLY FIVE scoring categories.

Score every category from 0.0 to 10.0.

1. NAME
2. USERNAME
3. BIO
4. COHERENCE
5. VIBE


NAME:
Judge the displayed Telegram name itself.

Consider:
- readability
- memorability
- style
- originality
- presentation
- how well the name works as a profile name

Do NOT infer age, ethnicity, religion, politics,
sexual orientation, health, disability or other
sensitive characteristics from the name.


USERNAME:
Judge the username itself.

Consider:
- readability
- memorability
- originality
- simplicity
- visual style
- how strong it looks as a Telegram username


BIO:
Judge the bio text.

Consider:
- writing quality
- originality
- personality expressed through the text
- clarity
- style
- presentation

If there is no bio, score based on the absence.
Do NOT invent a bio.


COHERENCE:
Judge how well the whole profile works together.

Consider:
- name
- username
- bio
- avatar
- visual and textual consistency
- whether the elements feel like one coherent profile

The avatar is used here as part of the overall profile,
but DO NOT create a separate avatar score.


VIBE:
Judge the overall presentation and style of the profile.

The avatar may be considered here.

Consider:
- overall visual impression
- style
- consistency
- memorability
- profile energy
- presentation quality

Do NOT judge physical attractiveness.


IMPORTANT SAFETY RULES:

Do NOT judge or infer:

- race
- ethnicity
- nationality
- religion
- political affiliation
- sexual orientation
- gender identity
- health
- disability
- body
- physical attractiveness
- exact age
- socioeconomic status
- other sensitive personal characteristics

Do not invent information.

Do not treat absence of information as negative
unless that category specifically requires information.

Use the full 0-10 scale.

Do not give everyone similar scores.

Be critical but humorous.

Return ONLY valid JSON.

Required format:

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
            f"BIO: "
            f"{profile.bio or '(нет био)'}"
        )

    parts = [

        {
            "text": prompt
        },

        {
            "text":
                "\n\nPLAYER 1\n"
                +
                profile_text(
                    player1
                )
        },

        {
            "text":
                "\n\nPLAYER 2\n"
                +
                profile_text(
                    player2
                )
        }
    ]

    # ========================================================
    # PLAYER 1 AVATAR
    # ========================================================

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
                    "The previous image is PLAYER 1 avatar."
            }
        )

    else:

        parts.append(
            {
                "text":
                    "PLAYER 1 has no avatar."
            }
        )

    # ========================================================
    # PLAYER 2 AVATAR
    # ========================================================

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
                    "The previous image is PLAYER 2 avatar."
            }
        )

    else:

        parts.append(
            {
                "text":
                    "PLAYER 2 has no avatar."
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
            "temperature": 0.25,

            "responseMimeType":
                "application/json"
        }
    }

    # ========================================================
    # REQUEST WITH RETRIES
    # ========================================================

    async with httpx.AsyncClient(
        timeout=GEMINI_TIMEOUT
    ) as client:

        response = None

        for attempt in range(
            GEMINI_MAX_RETRIES
        ):

            try:

                response = await client.post(
                    url,
                    params={
                        "key":
                            GEMINI_API_KEY
                    },
                    json=payload
                )

            except (
                httpx.TimeoutException,
                httpx.NetworkError
            ) as error:

                if (
                    attempt
                    >=
                    GEMINI_MAX_RETRIES - 1
                ):

                    raise RuntimeError(
                        "Gemini network/timeout error."
                    ) from error

                wait_time = (
                    3
                    *
                    (
                        2 ** attempt
                    )
                )

                logger.warning(
                    "Gemini network error. "
                    "Retry %s/%s in %s sec: %s",
                    attempt + 1,
                    GEMINI_MAX_RETRIES,
                    wait_time,
                    error
                )

                await asyncio.sleep(
                    wait_time
                )

                continue

            # =================================================
            # SUCCESS
            # =================================================

            if response.status_code == 200:

                break

            # =================================================
            # RATE LIMIT
            # =================================================

            if response.status_code == 429:

                if attempt >= (
                    GEMINI_MAX_RETRIES - 1
                ):

                    logger.error(
                        "Gemini 429 after all retries: %s",
                        response.text[:1000]
                    )

                    raise RuntimeError(
                        "Gemini API rate limit exceeded. "
                        "Попробуй ещё раз немного позже."
                    )

                retry_after = (
                    response.headers.get(
                        "Retry-After"
                    )
                )

                if retry_after:

                    try:

                        wait_time = float(
                            retry_after
                        )

                    except ValueError:

                        wait_time = (
                            4
                            *
                            (
                                2 ** attempt
                            )
                        )

                else:

                    wait_time = (
                        4
                        *
                        (
                            2 ** attempt
                        )
                    )

                logger.warning(
                    "Gemini HTTP 429. "
                    "Retry %s/%s in %.1f sec",
                    attempt + 1,
                    GEMINI_MAX_RETRIES,
                    wait_time
                )

                await asyncio.sleep(
                    wait_time
                )

                continue

            # =================================================
            # OTHER HTTP ERROR
            # =================================================

            logger.error(
                "Gemini HTTP %s: %s",
                response.status_code,
                response.text[:1000]
            )

            response.raise_for_status()

        else:

            raise RuntimeError(
                "Gemini request failed."
            )

    # ========================================================
    # EXTRACT RESPONSE
    # ========================================================

    try:

        text = (
            response.json()
            [
                "candidates"
            ][0]
            [
                "content"
            ]
            [
                "parts"
            ][0]
            [
                "text"
            ]
        )

    except Exception:

        logger.error(
            "Invalid Gemini response: %s",
            response.text[:3000]
        )

        raise RuntimeError(
            "Gemini returned an invalid response."
        )

    # ========================================================
    # CLEAN JSON
    # ========================================================

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

        value = float(
            value
        )

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
            scores[key]
            *
            WEIGHTS[key]

            for key in WEIGHTS
        )

        scores["overall"] = round(
            overall,
            2
        )

        # Keep AI reasons for Details.
        reasons = raw.get(
            "reasons",
            {}
        )

        if not isinstance(
            reasons,
            dict
        ):

            reasons = {}

        scores["reasons"] = {
            key: str(
                reasons.get(
                    key,
                    ""
                )
            )[:300]

            for key in WEIGHTS
        }

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

    # Close result = draw.
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

        winner_name = names[
            winner
        ]

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


# ============================================================
# IMAGE HELPERS
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
                "#25252d"
            )

    else:

        avatar = Image.new(
            "RGB",
            (
                size,
                size
            ),
            "#25252d"
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

    # Outer border
    draw.ellipse(
        (
            2,
            2,
            size - 3,
            size - 3
        ),
        outline="#f4c542",
        width=5
    )

    return result


def rounded_panel(
    draw,
    box,
    radius=28,
    fill="#111117",
    outline="#1f1f28",
    width=2
):

    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=outline,
        width=width
    )


def text_width(
    draw,
    text,
    font
):

    bbox = draw.textbbox(
        (
            0,
            0
        ),
        text,
        font=font
    )

    return (
        bbox[2]
        -
        bbox[0]
    )


def text_center(
    draw,
    text,
    center_x,
    y,
    font,
    fill
):

    width = text_width(
        draw,
        text,
        font
    )

    draw.text(
        (
            center_x - width / 2,
            y
        ),
        text,
        font=font,
        fill=fill
    )


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
        + "…"
    )


def wrap_text(
    draw,
    text,
    font,
    max_width
):

    text = str(
        text or ""
    ).strip()

    if not text:

        return [""]

    words = text.split()

    lines = []
    current = ""

    for word in words:

        test = (
            word
            if not current
            else current + " " + word
        )

        if (
            text_width(
                draw,
                test,
                font
            )
            <= max_width
        ):

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

    return lines


# ============================================================
# DRAW CROWN
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
            y + 45
        ),
        (
            center_x - 58,
            y - 8
        ),
        (
            center_x - 18,
            y + 28
        ),
        (
            center_x,
            y - 20
        ),
        (
            center_x + 18,
            y + 28
        ),
        (
            center_x + 58,
            y - 8
        ),
        (
            center_x + 75,
            y + 45
        )
    ]

    draw.polygon(
        points,
        fill=yellow
    )

    draw.rounded_rectangle(
        (
            center_x - 75,
            y + 35,
            center_x + 75,
            y + 62
        ),
        radius=7,
        fill=yellow
    )


# ============================================================
# DRAW PROFILE HEADER
# ============================================================

def draw_profile_header(
    image,
    draw,
    player,
    center_x,
    top_y,
    role,
    is_winner
):

    WHITE = "#f4f4f7"
    MUTED = "#858591"
    YELLOW = "#f4c542"
    PANEL = "#101017"
    BORDER = "#1d1d25"

    panel_w = 620

    rounded_panel(
        draw,
        (
            center_x - panel_w // 2,
            top_y,
            center_x + panel_w // 2,
            top_y + 450
        ),
        radius=30,
        fill=PANEL,
        outline=BORDER,
        width=2
    )

    # ========================================================
    # CROWN
    # ========================================================

    if is_winner:

        draw_crown(
            draw,
            center_x,
            top_y + 15
        )

    # ========================================================
    # AVATAR
    # ========================================================

    avatar_size = 250

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
        top_y
        + 55
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
    # ROLE
    # ========================================================

    role_font = get_font(
        "profile_label",
        True
    )

    role_width = text_width(
        draw,
        role,
        role_font
    ) + 60

    role_y = (
        top_y
        + 315
    )

    draw.rounded_rectangle(
        (
            center_x - role_width / 2,
            role_y,
            center_x + role_width / 2,
            role_y + 42
        ),
        radius=21,
        fill="#25252d"
    )

    text_center(
        draw,
        role,
        center_x,
        role_y + 7,
        role_font,
        YELLOW if is_winner else MUTED
    )

    # ========================================================
    # NAME
    # ========================================================

    name = truncate_text(
        player.name,
        24
    )

    text_center(
        draw,
        name,
        center_x,
        top_y + 365,
        get_font(
            "name",
            True
        ),
        WHITE
    )

    # ========================================================
    # USERNAME
    # ========================================================

    username = truncate_text(
        player.username,
        28
    )

    text_center(
        draw,
        username,
        center_x,
        top_y + 410,
        get_font(
            "username",
            True
        ),
        YELLOW if is_winner else MUTED
    )


# ============================================================
# DRAW SCORE ROW
# ============================================================

def draw_score_row(
    draw,
    x,
    y,
    label,
    score,
    bar_width=500
):

    WHITE = "#f4f4f7"
    MUTED = "#858591"
    BAR_BG = "#292932"
    YELLOW = "#f4c542"

    # Label
    draw.text(
        (
            x,
            y
        ),
        label,
        font=get_font(
            "category",
            True
        ),
        fill=MUTED
    )

    # Bar
    bar_x = x + 235

    bar_y = y + 5

    bar_h = 24

    draw.rounded_rectangle(
        (
            bar_x,
            bar_y,
            bar_x + bar_width,
            bar_y + bar_h
        ),
        radius=12,
        fill=BAR_BG
    )

    actual_width = (
        bar_width
        *
        score
        /
        10
    )

    if actual_width > 0:

        draw.rounded_rectangle(
            (
                bar_x,
                bar_y,
                bar_x + actual_width,
                bar_y + bar_h
            ),
            radius=12,
            fill=YELLOW
        )

    # Score
    draw.text(
        (
            bar_x + bar_width + 20,
            y - 3
        ),
        f"{score:.2f}",
        font=get_font(
            "score",
            True
        ),
        fill=WHITE
    )


# ============================================================
# DRAW SCORE SECTION
# ============================================================

def draw_score_section(
    draw,
    player,
    result_player,
    center_x,
    top_y,
    is_winner
):

    PANEL = "#101017"
    BORDER = "#1d1d25"
    WHITE = "#f4f4f7"
    MUTED = "#858591"
    PURPLE = "#8b5cf6"
    PURPLE_DARK = "#251c3a"

    panel_w = 1300

    section_h = 470

    rounded_panel(
        draw,
        (
            center_x - panel_w // 2,
            top_y,
            center_x + panel_w // 2,
            top_y + section_h
        ),
        radius=28,
        fill=PANEL,
        outline=BORDER,
        width=2
    )

    # ========================================================
    # OVERALL
    # ========================================================

    overall_y = top_y + 28

    draw.text(
        (
            center_x - 600,
            overall_y
        ),
        "OVERALL SCORE",
        font=get_font(
            "overall_label",
            True
        ),
        fill=MUTED
    )

    overall = result_player[
        "overall"
    ]

    draw.text(
        (
            center_x - 600,
            overall_y + 31
        ),
        f"{overall:.2f}",
        font=get_font(
            "overall_score",
            True
        ),
        fill=WHITE
    )

    # Small overall bar
    bar_x = center_x - 350
    bar_y = overall_y + 45
    bar_w = 900
    bar_h = 28

    draw.rounded_rectangle(
        (
            bar_x,
            bar_y,
            bar_x + bar_w,
            bar_y + bar_h
        ),
        radius=14,
        fill=PURPLE_DARK
    )

    fill_w = (
        bar_w
        *
        overall
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
            radius=14,
            fill=PURPLE
        )

    # ========================================================
    # CATEGORY ROWS
    # ========================================================

    rows_y = top_y + 105

    for index, (
        label,
        key
    ) in enumerate(
        CATEGORIES
    ):

        draw_score_row(
            draw,
            center_x - 600,
            rows_y + index * 66,
            label,
            result_player[key],
            bar_width=700
        )

    # ========================================================
    # WINNER INDICATOR
    # ========================================================

    if is_winner:

        winner_text = "WINNER"

        winner_font = get_font(
            "profile_label",
            True
        )

        winner_width = (
            text_width(
                draw,
                winner_text,
                winner_font
            )
            + 50
        )

        draw.rounded_rectangle(
            (
                center_x + 430,
                top_y + 25,
                center_x + 430 + winner_width,
                top_y + 65
            ),
            radius=20,
            fill="#3b2d08"
        )

        text_center(
            draw,
            winner_text,
            center_x + 430 + winner_width / 2,
            top_y + 31,
            winner_font,
            "#f4c542"
        )


# ============================================================
# CREATE CARD
# ============================================================

def create_card(
    player1,
    player2,
    result
):

    # ========================================================
    # CANVAS
    # ========================================================

    W = 1440
    H = 2150

    BG = "#09090d"
    WHITE = "#f4f4f7"
    MUTED = "#858591"
    YELLOW = "#f4c542"
    PURPLE = "#8b5cf6"
    RED = "#ff3030"
    BORDER = "#1d1d25"

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

    # ========================================================
    # HEADER
    # ========================================================

    text_center(
        draw,
        "MOG BATTLE",
        center_x,
        35,
        get_font(
            "title",
            True
        ),
        YELLOW
    )

    text_center(
        draw,
        "AI PROFILE COMPARISON",
        center_x,
        105,
        get_font(
            "subtitle",
            True
        ),
        MUTED
    )

    # ========================================================
    # WINNER
    # ========================================================

    winner = result[
        "winner"
    ]

    # ========================================================
    # PLAYER 1 HEADER
    # ========================================================

    draw_profile_header(
        image,
        draw,
        player1,
        center_x,
        150,
        "HOST PROFILE",
        winner == 0
    )

    # ========================================================
    # PLAYER 1 SCORES
    # ========================================================

    draw_score_section(
        draw,
        player1,
        result["players"][0],
        center_x,
        620,
        winner == 0
    )

    # ========================================================
    # VS
    # ========================================================

    vs_y = 1110

    # separator
    draw.line(
        (
            70,
            vs_y + 52,
            W - 70,
            vs_y + 52
        ),
        fill=BORDER,
        width=2
    )

    # black rounded VS box
    vs_w = 160
    vs_h = 105

    draw.rounded_rectangle(
        (
            center_x - vs_w // 2,
            vs_y,
            center_x + vs_w // 2,
            vs_y + vs_h
        ),
        radius=35,
        fill="#09090d"
    )

    text_center(
        draw,
        "VS",
        center_x,
        vs_y + 10,
        get_font(
            "vs",
            True
        ),
        WHITE
    )

    # ========================================================
    # PLAYER 2 HEADER
    # ========================================================

    draw_profile_header(
        image,
        draw,
        player2,
        center_x,
        1240,
        "GUEST PROFILE",
        winner == 1
    )

    # ========================================================
    # PLAYER 2 SCORES
    # ========================================================

    draw_score_section(
        draw,
        player2,
        result["players"][1],
        center_x,
        1710,
        winner == 1
    )

    # ========================================================
    # MOGGED STAMP
    # ========================================================

    if result["loser"] is not None:

        loser_center = center_x

        stamp_w = 560
        stamp_h = 120

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

        stamp_draw = ImageDraw.Draw(
            stamp
        )

        stamp_draw.rounded_rectangle(
            (
                5,
                5,
                stamp_w - 5,
                stamp_h - 5
            ),
            radius=18,
            fill=(
                255,
                48,
                48,
                235
            ),
            outline=(
                255,
                255,
                255,
                220
            ),
            width=4
        )

        stamp_draw.text(
            (
                stamp_w // 2,
                stamp_h // 2
            ),
            "MOGGED",
            font=get_font(
                "mogged",
                True
            ),
            fill="white",
            anchor="mm",
            stroke_width=3,
            stroke_fill="#7d0000"
        )

        stamp = stamp.rotate(
            -10,
            expand=True,
            resample=Image.Resampling.BICUBIC
        )

        stamp_x = (
            loser_center
            -
            stamp.width // 2
        )

        # Put stamp over lower profile area.
        stamp_y = 1590

        image.paste(
            stamp,
            (
                stamp_x,
                stamp_y
            ),
            stamp
        )

    # ========================================================
    # FINAL RESULT
    # ========================================================

    final_y = 2200

    # Canvas may need room.
    # Because H is 2150, put result in lower area
    # inside the second score panel instead.
    final_y = 2020

    draw.line(
        (
            70,
            final_y,
            W - 70,
            final_y
        ),
        fill=BORDER,
        width=2
    )

    # STATUS

    status = result[
        "status"
    ]

    draw.text(
        (
            70,
            final_y + 25
        ),
        status,
        font=get_font(
            "status",
            True
        ),
        fill=RED
    )

    # WINNER

    if winner is None:

        winner_text = "DRAW"

    else:

        winner_text = (
            "WINNER  "
            +
            (
                player1.username
                if winner == 0
                else
                player2.username
            )
        )

    draw.text(
        (
            70,
            final_y + 70
        ),
        winner_text,
        font=get_font(
            "winner",
            True
        ),
        fill=PURPLE
    )

    # Difference

    draw.text(
        (
            1050,
            final_y + 35
        ),
        "DIFFERENCE",
        font=get_font(
            "difference_label",
            True
        ),
        fill=MUTED
    )

    draw.text(
        (
            1050,
            final_y + 68
        ),
        f"{result['difference']:.2f}",
        font=get_font(
            "difference_score",
            True
        ),
        fill=YELLOW
    )

    # ========================================================
    # VERDICT
    # ========================================================

    verdict_y = 2090

    verdict = str(
        result.get(
            "verdict",
            ""
        )
    ).strip()

    if verdict:

        verdict = truncate_text(
            verdict,
            150
        )

        lines = wrap_text(
            draw,
            verdict,
            get_font(
                "verdict"
            ),
            W - 140
        )

        lines = lines[:2]

        for index, line in enumerate(
            lines
        ):

            text_center(
                draw,
                line,
                center_x,
                verdict_y + index * 32,
                get_font(
                    "verdict",
                    True
                ),
                WHITE
            )

    # ========================================================
    # FOOTER
    # ========================================================

    # Footer deliberately placed at bottom.
    footer_y = H - 38

    draw.text(
        (
            70,
            footer_y
        ),
        "MOG AI  •  POWERED BY GEMINI",
        font=get_font(
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

    builder = (
        InlineKeyboardBuilder()
    )

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
    # REPLY MODE
    # ========================================================

    if (
        message.reply_to_message
        and
        message.reply_to_message.from_user
    ):

        target_user_id = (
            message
            .reply_to_message
            .from_user
            .id
        )

        return (
            message.from_user.id,
            target_user_id
        )

    # ========================================================
    # USERNAME MODE
    # ========================================================

    text = (
        message.text
        or ""
    )

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

        username = (
            match.group(2)
        )

        try:

            target = (
                await bot.get_chat(
                    "@"
                    +
                    username
                )
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
        "👤 Анализирую NAME...\n"
        "🔤 Анализирую USERNAME...\n"
        "📝 Анализирую BIO...\n"
        "🔗 Анализирую COHERENCE...\n"
        "✨ Анализирую VIBE...\n"
        "🧠 Gemini считает оценки..."
    )

    try:

        # ====================================================
        # PROFILES
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

        ai_result = (
            await analyze_with_gemini(
                player1,
                player2
            )
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

            winner_text = (
                "🤝 <b>DRAW</b>"
            )

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

        error_text = str(
            error
        )

        if len(error_text) > 900:

            error_text = (
                error_text[:900]
                +
                "..."
            )

        safe_error = html.escape(
            error_text
        )

        try:

            await status_message.edit_text(
                "❌ <b>MOG FAILED</b>\n\n"
                "<code>"
                +
                safe_error
                +
                "</code>"
            )

        except Exception:

            await message.answer(
                "❌ <b>MOG FAILED</b>\n\n"
                "<code>"
                +
                safe_error
                +
                "</code>"
            )


# ============================================================
# .мог / /mog
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
# /START
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
# /HELP
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
# /STATS
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

    user = data[
        "users"
    ].get(
        user_id
    )

    if not user:

        await message.answer(
            "📊 У тебя пока нет MOG-баттлов."
        )

        return

    battles = user[
        "battles"
    ]

    wins = user[
        "wins"
    ]

    losses = user[
        "losses"
    ]

    draws = user[
        "draws"
    ]

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
# /TOP
# ============================================================

@dp.message(
    Command("top")
)
async def top_command(
    message: Message
):

    data = load_data()

    users = []

    for user_id, user in (
        data["users"].items()
    ):

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

    text = (
        "<b>🏆 MOG TOP 10</b>\n\n"
    )

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for index, user in enumerate(
        users
    ):

        if index < 3:

            position = medals[
                index
            ]

        else:

            position = (
                f"<b>{index + 1}.</b>"
            )

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
# /HISTORY
# ============================================================

@dp.message(
    Command("history")
)
async def history_command(
    message: Message
):

    data = load_data()

    battles = (
        data["battles"][-10:]
    )

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

            winner = (
                battle["player1"]
            )

        elif battle["winner"] == 1:

            winner = (
                battle["player2"]
            )

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
# DETAILS
# ============================================================

@dp.callback_query(
    F.data == "details"
)
async def details_callback(
    callback: CallbackQuery
):

    await callback.answer(
        "Оценки NAME / USERNAME / BIO / "
        "COHERENCE / VIBE находятся на карточке.",
        show_alert=True
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

        parts = (
            callback.data
            .split(":")
        )

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

    except Exception as error:

        logger.exception(
            "Rematch failed"
        )

        await callback.answer(
            "❌ Ошибка",
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
