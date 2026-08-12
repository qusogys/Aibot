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

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

DATA_FILE = "mog_data.json"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

dp = Dispatcher()


# ============================================================
# DATA
# ============================================================

WEIGHTS = {
    "avatar": 0.30,
    "username": 0.20,
    "bio": 0.20,
    "coherence": 0.15,
    "vibe": 0.15,
}


CATEGORIES = [
    ("AVATAR", "avatar"),
    ("USERNAME", "username"),
    ("BIO", "bio"),
    ("COHERENCE", "coherence"),
    ("VIBE", "vibe"),
]


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
            encoding="utf-8"
        ) as file:

            return json.load(file)

    except Exception:

        logger.exception(
            "Could not load database"
        )

        return {
            "users": {},
            "battles": [],
        }


def save_data(data):

    temp_file = DATA_FILE + ".tmp"

    with open(
        temp_file,
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
        temp_file,
        DATA_FILE
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
            "score_sum": 0,
        }

    else:

        data["users"][user_id][
            "username"
        ] = username


def register_battle(
    player1,
    player2,
    result,
    scores,
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

    p1["battles"] += 1
    p2["battles"] += 1

    p1["score_sum"] += scores[0]
    p2["score_sum"] += scores[1]

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
        "time":
            datetime.utcnow().isoformat(),

        "player1":
            player1.username,

        "player2":
            player2.username,

        "score1":
            scores[0],

        "score2":
            scores[1],

        "winner":
            result["winner"],

        "status":
            result["status"],
    }


    data["battles"].append(
        battle
    )


    # Keep only last 500 battles.

    data["battles"] = data[
        "battles"
    ][-500:]


    save_data(data)


# ============================================================
# TELEGRAM PROFILE
# ============================================================

async def get_profile(
    bot: Bot,
    user_id: int,
):

    chat = await bot.get_chat(
        user_id
    )

    username = (
        f"@{chat.username}"
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

            file = await bot.get_file(
                photo.file_id
            )

            buffer = io.BytesIO()

            await bot.download_file(
                file.file_path,
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
        user_id,
        username,
        name,
        bio,
        avatar,
    )


# ============================================================
# GEMINI
# ============================================================

async def analyze_with_gemini(
    player1,
    player2,
):

    prompt = """
You are the AI judge of a humorous Telegram profile
comparison game called MOG.

Compare TWO Telegram profiles.

Judge ONLY public profile presentation.

Categories:

avatar
username
bio
coherence
vibe

Avatar:
Judge visual quality, composition, recognizability,
originality and suitability as a profile avatar.

Username:
Judge readability, memorability, uniqueness and style.

Bio:
Judge writing quality, originality, personality and presentation.

Coherence:
Judge how well avatar, username, name and bio fit together.

Vibe:
Judge overall profile style and presentation.

Do NOT judge or infer:

race
ethnicity
religion
politics
sexual orientation
health
disability
body
physical attractiveness
sensitive personal characteristics
exact age

Do not invent missing information.

Use the full 0-10 range.

Do not give everyone 7-9.

Return ONLY valid JSON.

Format:

{
  "players": [
    {
      "avatar": 0.0,
      "username": 0.0,
      "bio": 0.0,
      "coherence": 0.0,
      "vibe": 0.0,
      "reasons": {
        "avatar": "...",
        "username": "...",
        "bio": "...",
        "coherence": "...",
        "vibe": "..."
      }
    },
    {
      "avatar": 0.0,
      "username": 0.0,
      "bio": 0.0,
      "coherence": 0.0,
      "vibe": 0.0,
      "reasons": {
        "avatar": "...",
        "username": "...",
        "bio": "...",
        "coherence": "...",
        "vibe": "..."
      }
    }
  ],
  "verdict": "Короткий русский вердикт максимум 250 символов"
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
                "\nPLAYER 1\n"
                +
                profile_text(
                    player1
                )
        },

        {
            "text":
                "\nPLAYER 2\n"
                +
                profile_text(
                    player2
                )
        },
    ]


    if player1.avatar:

        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
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
                    "This image is PLAYER 1 avatar."
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
                        ).decode(
                            "utf-8"
                        )
                }
            }
        )

        parts.append(
            {
                "text":
                    "This image is PLAYER 2 avatar."
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

        response.raise_for_status()

        data = response.json()


    try:

        text = (
            data["candidates"][0]
            ["content"]["parts"][0]
            ["text"]
        )

    except Exception:

        logger.error(
            "Gemini response: %s",
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

        raise RuntimeError(
            "Gemini JSON parsing failed."
        )


# ============================================================
# SCORING
# ============================================================

def clamp(
    value
):

    try:

        value = float(
            value
        )

    except Exception:

        value = 0


    return max(
        0,
        min(
            10,
            value
        )
    )


def calculate_scores(
    ai_result,
    names
):

    players = []


    for raw in ai_result[
        "players"
    ][:2]:

        scores = {}


        for key in WEIGHTS:

            scores[key] = clamp(
                raw.get(
                    key,
                    0
                )
            )


        overall = sum(
            scores[key] *
            weight

            for key, weight
            in WEIGHTS.items()
        )


        scores["overall"] = round(
            overall,
            2
        )


        players.append(
            scores
        )


    difference = round(
        abs(
            players[0]["overall"]
            -
            players[1]["overall"]
        ),
        2
    )


    if difference < 0.10:

        winner = None
        loser = None
        status = "DRAW"

    elif players[0]["overall"] > players[1]["overall"]:

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
            )[:300],
    }


# ============================================================
# IMAGE
# ============================================================

def get_font(
    size,
    bold=False
):

    paths = [

        (
            "/usr/share/fonts/"
            "truetype/dejavu/"
            "DejaVuSans-Bold.ttf"
            if bold
            else
            "/usr/share/fonts/"
            "truetype/dejavu/"
            "DejaVuSans.ttf"
        ),

        (
            "/usr/share/fonts/"
            "truetype/liberation2/"
            "LiberationSans-Bold.ttf"
            if bold
            else
            "/usr/share/fonts/"
            "truetype/liberation2/"
            "LiberationSans-Regular.ttf"
        )
    ]


    for path in paths:

        if os.path.exists(path):

            return ImageFont.truetype(
                path,
                size
            )


    return ImageFont.load_default()


def make_avatar(
    data,
    size=190
):

    if data:

        try:

            avatar = Image.open(
                io.BytesIO(data)
            ).convert(
                "RGB"
            )

        except Exception:

            avatar = Image.new(
                "RGB",
                (
                    size,
                    size
                ),
                "#33333d"
            )

    else:

        avatar = Image.new(
            "RGB",
            (
                size,
                size
            ),
            "#33333d"
        )


    avatar.thumbnail(
        (
            size,
            size
        )
    )


    canvas = Image.new(
        "RGB",
        (
            size,
            size
        ),
        "#17171f"
    )


    canvas.paste(
        avatar,
        (
            (size - avatar.width) // 2,
            (size - avatar.height) // 2
        )
    )


    mask = Image.new(
        "L",
        (
            size,
            size
        ),
        0
    )


    ImageDraw.Draw(
        mask
    ).ellipse(
        (
            0,
            0,
            size,
            size
        ),
        fill=255
    )


    result = Image.new(
        "RGB",
        (
            size,
            size
        )
    )


    result.paste(
        canvas,
        mask=mask
    )


    return result


def create_card(
    player1,
    player2,
    result
):

    W = 1200
    H = 1280

    BG = "#0b0b10"
    WHITE = "#f5f5f7"
    MUTED = "#8d8d99"
    YELLOW = "#f4c542"
    PURPLE = "#8b5cf6"
    RED = "#ff3030"
    BAR_BG = "#292933"


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


    draw.text(
        (60, 40),
        "MOG BATTLE",
        font=get_font(
            58,
            True
        ),
        fill=WHITE
    )


    draw.text(
        (63, 108),
        "AI PROFILE COMPARISON",
        font=get_font(
            21,
            True
        ),
        fill=MUTED
    )


    players = [
        player1,
        player2
    ]

    xs = [
        100,
        700
    ]


    for i, (
        player,
        x
    ) in enumerate(
        zip(
            players,
            xs
        )
    ):

        image.paste(
            make_avatar(
                player.avatar
            ),
            (
                x,
                165
            )
        )


        draw.text(
            (
                x,
                370
            ),
            player.username[:19],
            font=get_font(
                28,
                True
            ),
            fill=WHITE
        )


        draw.text(
            (
                x,
                410
            ),
            player.name[:24],
            font=get_font(
                19
            ),
            fill=MUTED
        )


        if result["loser"] == i:

            badge = Image.new(
                "RGBA",
                (
                    500,
                    180
                ),
                (
                    0,
                    0,
                    0,
                    0
                )
            )


            badge_draw = ImageDraw.Draw(
                badge
            )


            badge_draw.text(
                (
                    10,
                    20
                ),
                "MOGGED",
                font=get_font(
                    65,
                    True
                ),
                fill=RED
            )


            badge = badge.rotate(
                12,
                expand=True,
                resample=
                    Image.Resampling.BICUBIC
            )


            image.paste(
                badge,
                (
                    x - 55,
                    135
                ),
                badge
            )


    y = 480


    for label, key in CATEGORIES:

        draw.text(
            (
                60,
                y
            ),
            label,
            font=get_font(
                19,
                True
            ),
            fill=MUTED
        )


        for i, x in enumerate(xs):

            score = result[
                "players"
            ][i][key]


            bar_y = y + 35
            bar_width = 400
            bar_height = 19


            draw.rounded_rectangle(
                (
                    x,
                    bar_y,
                    x + bar_width,
                    bar_y + bar_height
                ),
                radius=9,
                fill=BAR_BG
            )


            draw.rounded_rectangle(
                (
                    x,
                    bar_y,
                    x +
                    bar_width *
                    score /
                    10,
                    bar_y + bar_height
                ),
                radius=9,
                fill=YELLOW
            )


            draw.text(
                (
                    x +
                    bar_width +
                    14,
                    bar_y - 5
                ),
                f"{score:.1f}",
                font=get_font(
                    19,
                    True
                ),
                fill=WHITE
            )


        y += 75


    draw.text(
        (
            60,
            885
        ),
        "OVERALL",
        font=get_font(
            24,
            True
        ),
        fill=MUTED
    )


    for i, x in enumerate(xs):

        score = result[
            "players"
        ][i]["overall"]


        bar_y = 930
        bar_width = 400
        bar_height = 31


        draw.rounded_rectangle(
            (
                x,
                bar_y,
                x + bar_width,
                bar_y + bar_height
            ),
            radius=15,
            fill="#2c243b"
        )


        draw.rounded_rectangle(
            (
                x,
                bar_y,
                x +
                bar_width *
                score /
                10,
                bar_y + bar_height
            ),
            radius=15,
            fill=PURPLE
        )


        draw.text(
            (
                x +
                bar_width +
                14,
                bar_y - 8
            ),
            f"{score:.2f}",
            font=get_font(
                27,
                True
            ),
            fill=WHITE
        )


    draw.text(
        (
            60,
            1020
        ),
        result["status"],
        font=get_font(
            24,
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
            60,
            1060
        ),
        winner_text,
        font=get_font(
            43,
            True
        ),
        fill=PURPLE
    )


    verdict = result[
        "verdict"
    ]


    # Prevent extremely long verdict
    # from leaving the card.

    if len(verdict) > 95:

        verdict = (
            verdict[:92]
            + "..."
        )


    draw.text(
        (
            60,
            1130
        ),
        verdict,
        font=get_font(
            20
        ),
        fill=WHITE
    )


    draw.text(
        (
            60,
            1210
        ),
        "MOG AI • POWERED BY GEMINI",
        font=get_font(
            17
        ),
        fill=MUTED
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
# GET TARGET
# ============================================================

async def resolve_target(
    message,
    bot
):

    # Reply mode

    if (
        message.reply_to_message
        and
        message.reply_to_message.from_user
    ):

        return (
            message.reply_to_message
            .from_user.id,
            message.from_user.id
        )


    # Username mode

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

        except Exception:

            return None


    return None


# ============================================================
# MOG CORE
# ============================================================

async def run_mog(
    message,
    bot,
    player1_id,
    player2_id
):

    if player1_id == player2_id:

        await message.answer(
            "😐 Себя с собой сравнивать нельзя."
        )

        return


    status = await message.answer(
        "⚔️ <b>MOG BATTLE</b>\n\n"
        "🔎 Получаю профили...\n"
        "🖼 Анализирую аватары...\n"
        "📝 Анализирую bio...\n"
        "🔤 Анализирую username...\n"
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


        ai_result = (
            await analyze_with_gemini(
                player1,
                player2
            )
        )


        result = calculate_scores(
            ai_result,
            [
                player1.username,
                player2.username
            ]
        )


        scores = [
            result["players"][0]["overall"],
            result["players"][1]["overall"],
        ]


        register_battle(
            player1,
            player2,
            result,
            scores
        )


        card = create_card(
            player1,
            player2,
            result
        )


        caption = (
            "🏆 <b>"
            + result["winner_name"]
            + "</b>\n\n"

            "⚡ <b>"
            + result["status"]
            + "</b>\n\n"

            f"{player1.username}: "
            f"<b>{scores[0]:.2f}/10</b>\n"

            f"{player2.username}: "
            f"<b>{scores[1]:.2f}/10</b>\n\n"

            f"📊 Difference: "
            f"<b>{result['difference']:.2f}</b>\n\n"

            f"💬 {result['verdict']}"
        )


        await message.answer_photo(
            BufferedInputFile(
                card,
                filename="mog.png"
            ),
            caption=caption,
            reply_markup=result_keyboard(
                player1_id,
                player2_id
            )
        )


        await status.delete()


    except Exception as error:

        logger.exception(
            "MOG failed"
        )


        error_text = str(error)

        if len(error_text) > 700:

            error_text = (
                error_text[:700]
                + "..."
            )


        try:

            await status.edit_text(
                "❌ <b>MOG FAILED</b>\n\n"
                "<code>"
                + error_text
                + "</code>"
            )

        except Exception:

            await message.answer(
                "❌ Произошла ошибка:\n\n"
                "<code>"
                + error_text
                + "</code>"
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

            "2️⃣ Или используй username:\n"
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
# /start
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

        "Gemini оценит аватар, username, bio, "
        "coherence и vibe.\n\n"

        "🏆 Побеждает тот, у кого выше Overall."
    )


# ============================================================
# /help
# ============================================================

@dp.message(
    Command("help")
)
async def help_command(
    message: Message
):

    await message.answer(
        "<b>⚔️ MOG AI — команды</b>\n\n"

        "<code>.мог</code>\n"
        "Сравнить себя с человеком, "
        "на сообщение которого ты ответил.\n\n"

        "<code>.мог @username</code>\n"
        "Сравнить себя с указанным username.\n\n"

        "<code>/mog</code>\n"
        "То же самое, что .мог.\n\n"

        "<code>/stats</code>\n"
        "Твоя статистика.\n\n"

        "<code>/top</code>\n"
        "Топ игроков.\n\n"

        "<code>/history</code>\n"
        "Последние MOG-баттлы.\n\n"

        "<code>/help</code>\n"
        "Эта справка."
    )


# ============================================================
# /stats
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


    if battles:

        winrate = (
            wins /
            battles *
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

        f"📈 Winrate: "
        f"<b>{winrate:.1f}%</b>\n"

        f"⭐ Average score: "
        f"<b>{average:.2f}/10</b>"
    )


# ============================================================
# /top
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

        if user["battles"] == 0:

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

            medal = medals[index]

        else:

            medal = (
                f"<b>{index + 1}.</b>"
            )


        text += (
            f"{medal} "
            f"{user['username']} — "
            f"<b>{user['wins']}</b> wins "
            f"• {user['average']:.2f}/10\n"
        )


    await message.answer(
        text
    )


# ============================================================
# /history
# ============================================================

@dp.message(
    Command("history")
)
async def history_command(
    message: Message
):

    data = load_data()

    battles = data[
        "battles"
    ][-10:]


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

            winner = battle[
                "player1"
            ]

        elif battle["winner"] == 1:

            winner = battle[
                "player2"
            ]

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
# CALLBACKS
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

        _, p1, p2 = (
            callback.data
            .split(":")
        )

        p1 = int(p1)
        p2 = int(p2)


        await callback.answer(
            "🔄 Новый MOG!"
        )


        await run_mog(
            callback.message,
            bot,
            p1,
            p2
        )

    except Exception as error:

        logger.exception(
            "Rematch error"
        )

        await callback.answer(
            "❌ Ошибка",
            show_alert=True
        )


@dp.callback_query(
    F.data == "details"
)
async def details_callback(
    callback: CallbackQuery
):

    await callback.answer(
        "Оценки по категориям уже показаны на карточке.",
        show_alert=True
    )


# ============================================================
# START
# ============================================================

async def main():

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )


    logger.info(
        "MOG AI bot starting..."
    )


    await dp.start_polling(
        bot
    )


if __name__ == "__main__":

    asyncio.run(
        main()
            )
