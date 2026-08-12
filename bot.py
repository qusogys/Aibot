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

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
).strip()

DATA_FILE = "mog_data.json"

# ============================================================
# FONT SCALE
# ============================================================
# Меняй ТОЛЬКО ЭТО ЧИСЛО.
#
# 1.0  = обычный размер
# 1.25 = +25%
# 1.5  = +50%
# 2.0  = в 2 раза больше
#
FONT_SCALE = 1.5


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set"
    )

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is not set"
    )


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
    "avatar": 0.25,
    "username": 0.17,
    "name": 0.13,
    "bio": 0.17,
    "coherence": 0.14,
    "vibe": 0.14,
}


CATEGORIES = [
    ("AVATAR", "avatar"),
    ("USERNAME", "username"),
    ("NAME", "name"),
    ("BIO", "bio"),
    ("COHERENCE", "coherence"),
    ("VIBE", "vibe"),
]


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

    temporary_file = (
        DATA_FILE
        + ".tmp"
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
        "time": datetime.utcnow().isoformat(),

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

Compare TWO Telegram profiles.

The goal is to judge profile presentation, not the person.

Score these categories from 0.0 to 10.0:

1. avatar
2. username
3. name
4. bio
5. coherence
6. vibe

AVATAR:
Judge visual quality, composition, recognizability,
originality and suitability as a profile avatar.

USERNAME:
Judge readability, memorability, uniqueness and style.

NAME:
Judge the displayed Telegram profile name itself.
Judge readability, memorability, typography/style,
originality and how well it works as a displayed profile name.

BIO:
Judge writing quality, originality, personality and presentation.

COHERENCE:
Judge how well avatar, username, name and bio fit together.

VIBE:
Judge the overall profile presentation and style.

IMPORTANT:

Do not judge or infer:

- race
- ethnicity
- religion
- political affiliation
- sexual orientation
- health
- disability
- body
- physical attractiveness
- sensitive personal characteristics
- exact age

Do not invent information.

If a bio is missing, score the bio based on the absence
and do not invent content.

Use the complete 0-10 scale.

Do not give everyone similar scores.

Return ONLY valid JSON.

Required format:

{
  "players": [
    {
      "avatar": 0.0,
      "username": 0.0,
      "name": 0.0,
      "bio": 0.0,
      "coherence": 0.0,
      "vibe": 0.0,
      "reasons": {
        "avatar": "short reason",
        "username": "short reason",
        "name": "short reason",
        "bio": "short reason",
        "coherence": "short reason",
        "vibe": "short reason"
      }
    },
    {
      "avatar": 0.0,
      "username": 0.0,
      "name": 0.0,
      "bio": 0.0,
      "coherence": 0.0,
      "vibe": 0.0,
      "reasons": {
        "avatar": "short reason",
        "username": "short reason",
        "name": "short reason",
        "bio": "short reason",
        "coherence": "short reason",
        "vibe": "short reason"
      }
    }
  ],
  "verdict": "Короткий смешной русский вердикт максимум 180 символов"
}
"""

    def profile_text(
        profile
    ):

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

    async with httpx.AsyncClient(
        timeout=120
    ) as client:

        response = await client.post(
            url,
            params={
                "key":
                    GEMINI_API_KEY
            },
            json=payload
        )

        if response.status_code != 200:

            logger.error(
                "Gemini HTTP %s: %s",
                response.status_code,
                response.text[:1000]
            )

        response.raise_for_status()

        data = response.json()

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

    match = re.search(
        r"\{.*\}",
        text,
        re.DOTALL
    )

    if not match:

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

def clamp(
    value
):

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

def get_font(
    size,
    bold=False
):

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

            return ImageFont.truetype(
                path,
                size
            )

    return ImageFont.load_default()


# ============================================================
# CARD FONT
# ============================================================
# НИЖЕ БОЛЬШЕ НИЧЕГО В create_card() МЕНЯТЬ НЕ НУЖНО.
#
# Все размеры автоматически умножаются на FONT_SCALE.
# ============================================================

def card_font(
    size,
    bold=False
):

    scaled_size = max(
        1,
        int(
            size * FONT_SCALE
        )
    )

    return get_font(
        scaled_size,
        bold
    )


# ============================================================
# AVATAR
# ============================================================

def make_avatar(
    data,
    size=250
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

    draw.ellipse(
        (
            2,
            2,
            size - 3,
            size - 3
        ),
        outline="#44444f",
        width=4
    )

    return result


# ============================================================
# CARD
# ============================================================

def create_card(
    player1,
    player2,
    result
):

    # ========================================================
    # CARD SIZE
    # ========================================================

    W = 1440
    H = 1750

    # ========================================================
    # COLORS
    # ========================================================

    BG = "#09090d"
    PANEL = "#101017"
    WHITE = "#f4f4f7"
    MUTED = "#858591"
    YELLOW = "#f4c542"
    PURPLE = "#8b5cf6"
    PURPLE_DARK = "#251c3a"
    RED = "#ff3030"
    BAR_BG = "#292932"
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

    # ========================================================
    # HELPERS
    # ========================================================

    def text_center(
        text,
        center_x,
        y,
        font,
        fill
    ):

        bbox = draw.textbbox(
            (
                0,
                0
            ),
            text,
            font=font
        )

        width = (
            bbox[2]
            -
            bbox[0]
        )

        draw.text(
            (
                center_x
                -
                width / 2,
                y
            ),
            text,
            font=font,
            fill=fill
        )

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
            text[
                :max_chars - 1
            ]
            + "…"
        )

    # ========================================================
    # HEADER
    # ========================================================

    text_center(
        "MOG BATTLE",
        W // 2,
        35,
        card_font(
            64,
            True
        ),
        WHITE
    )

    text_center(
        "AI PROFILE COMPARISON",
        W // 2,
        120,
        card_font(
            25,
            True
        ),
        MUTED
    )

    # ========================================================
    # PLAYERS
    # ========================================================

    players = [
        player1,
        player2
    ]

    centers = [
        360,
        1080
    ]

    avatar_size = 250
    avatar_y = 220

    for i, (
        player,
        center_x
    ) in enumerate(
        zip(
            players,
            centers
        )
    ):

        # ====================================================
        # PLAYER PANEL
        # ====================================================

        draw.rounded_rectangle(
            (
                center_x - 310,
                195,
                center_x + 310,
                585
            ),
            radius=28,
            fill=PANEL,
            outline=BORDER,
            width=2
        )

        # ====================================================
        # CROWN
        # ====================================================

        if result["winner"] == i:

            crown_y = 145

            draw.rounded_rectangle(
                (
                    center_x - 82,
                    crown_y + 45,
                    center_x + 82,
                    crown_y + 68
                ),
                radius=7,
                fill=YELLOW
            )

            crown_points = [
                (
                    center_x - 80,
                    crown_y + 48
                ),
                (
                    center_x - 64,
                    crown_y - 3
                ),
                (
                    center_x - 20,
                    crown_y + 29
                ),
                (
                    center_x,
                    crown_y - 18
                ),
                (
                    center_x + 20,
                    crown_y + 29
                ),
                (
                    center_x + 64,
                    crown_y - 3
                ),
                (
                    center_x + 80,
                    crown_y + 48
                )
            ]

            draw.polygon(
                crown_points,
                fill=YELLOW
            )

            for jewel_x in [
                center_x - 64,
                center_x,
                center_x + 64
            ]:

                draw.ellipse(
                    (
                        jewel_x - 6,
                        crown_y + 28,
                        jewel_x + 6,
                        crown_y + 40
                    ),
                    fill=PURPLE
                )

        # ====================================================
        # AVATAR
        # ====================================================

        avatar = make_avatar(
            player.avatar,
            avatar_size
        )

        avatar_x = (
            center_x
            -
            avatar_size // 2
        )

        image.paste(
            avatar,
            (
                avatar_x,
                avatar_y
            ),
            avatar
        )

        # ====================================================
        # MOGGED STAMP
        # ====================================================

        if result["loser"] == i:

            stamp_w = 350
            stamp_h = 90

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
                    2,
                    2,
                    stamp_w - 3,
                    stamp_h - 3
                ),
                radius=16,
                fill=(
                    255,
                    48,
                    48,
                    225
                ),
                outline=(
                    255,
                    255,
                    255,
                    180
                ),
                width=3
            )

            stamp_draw.text(
                (
                    stamp_w // 2,
                    10
                ),
                "MOGGED",
                font=card_font(
                    50,
                    True
                ),
                fill="white",
                anchor="ma"
            )

            stamp = stamp.rotate(
                12,
                expand=True,
                resample=
                    Image.Resampling.BICUBIC
            )

            stamp_x = (
                center_x
                -
                stamp.width // 2
            )

            stamp_y = 375

            image.paste(
                stamp,
                (
                    stamp_x,
                    stamp_y
                ),
                stamp
            )

        # ====================================================
        # USERNAME
        # ====================================================

        username = truncate(
            player.username,
            20
        )

        text_center(
            username,
            center_x,
            480,
            card_font(
                32,
                True
            ),
            WHITE
        )

        # ====================================================
        # NAME
        # ====================================================

        name = truncate(
            player.name,
            25
        )

        text_center(
            name,
            center_x,
            530,
            card_font(
                21
            ),
            MUTED
        )

    # ========================================================
    # SCORE BARS
    # ========================================================

    bar_left = 70
    bar_right = 650

    bar_left_2 = 790
    bar_right_2 = 1370

    bar_width = (
        bar_right
        -
        bar_left
    )

    bar_height = 25

    # 6 categories need more vertical space.
    label_y = 630

    for category_index, (
        label,
        key
    ) in enumerate(
        CATEGORIES
    ):

        y = (
            label_y
            +
            category_index * 105
        )

        # ====================================================
        # LEFT CATEGORY LABEL
        # ====================================================

        draw.text(
            (
                bar_left,
                y
            ),
            label,
            font=card_font(
                24,
                True
            ),
            fill=MUTED
        )

        # ====================================================
        # RIGHT CATEGORY LABEL
        # ====================================================

        draw.text(
            (
                bar_left_2,
                y
            ),
            label,
            font=card_font(
                24,
                True
            ),
            fill=MUTED
        )

        for i, x in enumerate(
            [
                bar_left,
                bar_left_2
            ]
        ):

            score = result[
                "players"
            ][i][key]

            bar_y = y + 48

            actual_width = (
                bar_width
                *
                score
                /
                10
            )

            # =================================================
            # BACKGROUND BAR
            # =================================================

            draw.rounded_rectangle(
                (
                    x,
                    bar_y,
                    x + bar_width,
                    bar_y + bar_height
                ),
                radius=12,
                fill=BAR_BG
            )

            # =================================================
            # SCORE BAR
            # =================================================

            if actual_width > 0:

                draw.rounded_rectangle(
                    (
                        x,
                        bar_y,
                        x + actual_width,
                        bar_y + bar_height
                    ),
                    radius=12,
                    fill=YELLOW
                )

            # =================================================
            # SCORE NUMBER
            # =================================================

            draw.text(
                (
                    x + bar_width + 15,
                    bar_y - 7
                ),
                f"{score:.1f}",
                font=card_font(
                    24,
                    True
                ),
                fill=WHITE
            )

    # ========================================================
    # OVERALL
    # ========================================================

    overall_y = 1290

    draw.text(
        (
            70,
            overall_y
        ),
        "OVERALL",
        font=card_font(
            29,
            True
        ),
        fill=MUTED
    )

    for i, x in enumerate(
        [
            bar_left,
            bar_left_2
        ]
    ):

        score = result[
            "players"
        ][i]["overall"]

        bar_y = (
            overall_y
            + 65
        )

        overall_bar_height = 45

        actual_width = (
            bar_width
            *
            score
            /
            10
        )

        # Background

        draw.rounded_rectangle(
            (
                x,
                bar_y,
                x + bar_width,
                bar_y + overall_bar_height
            ),
            radius=22,
            fill=PURPLE_DARK
        )

        # Purple score

        if actual_width > 0:

            draw.rounded_rectangle(
                (
                    x,
                    bar_y,
                    x + actual_width,
                    bar_y + overall_bar_height
                ),
                radius=22,
                fill=PURPLE
            )

        draw.text(
            (
                x + bar_width + 15,
                bar_y - 9
            ),
            f"{score:.2f}",
            font=card_font(
                31,
                True
            ),
            fill=WHITE
        )

    # ========================================================
    # RESULT
    # ========================================================

    result_y = 1440

    draw.text(
        (
            70,
            result_y
        ),
        result["status"],
        font=card_font(
            31,
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

    draw.text(
        (
            70,
            result_y + 65
        ),
        winner_text,
        font=card_font(
            44,
            True
        ),
        fill=PURPLE
    )

    # ========================================================
    # VERDICT
    # ========================================================

    verdict = str(
        result.get(
            "verdict",
            ""
        )
    )

    if len(verdict) > 110:

        verdict = (
            verdict[:107]
            +
            "..."
        )

    draw.text(
        (
            70,
            result_y + 145
        ),
        verdict,
        font=card_font(
            23
        ),
        fill=WHITE
    )

    # ========================================================
    # FOOTER
    # ========================================================

    draw.text(
        (
            70,
            H - 65
        ),
        "MOG AI  •  POWERED BY GEMINI",
        font=card_font(
            18,
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
        "🖼 Анализирую аватары...\n"
        "📝 Анализирую bio...\n"
        "🔤 Анализирую username...\n"
        "👤 Анализирую name...\n"
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
        # SAVE STATS
        # ====================================================

        register_battle(
            player1,
            player2,
            result
        )

        # ====================================================
        # CREATE CARD
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
        # SEND CARD
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

        "Добро пожаловать в AI-баттлы профилей.\n\n"

        "🥊 Ответь на сообщение человека:\n"
        "<code>.мог</code>\n\n"

        "Или:\n"
        "<code>.мог @username</code>\n\n"

        "Gemini оценит:\n"
        "🖼 Avatar\n"
        "🔤 Username\n"
        "👤 Name\n"
        "📝 Bio\n"
        "🔗 Coherence\n"
        "✨ Vibe\n\n"

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

        text += (
            f"{position} "
            f"{user['username']} — "
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

        text += (
            f"⚔️ "
            f"{battle['player1']} "
            f"<b>{battle['score1']:.2f}</b>"
            f" × "
            f"<b>{battle['score2']:.2f}</b> "
            f"{battle['player2']}\n"

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

    logger.info(
        "Card font scale: %sx",
        FONT_SCALE
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
