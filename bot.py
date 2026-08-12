import asyncio
import base64
import io
import json
import logging
import os
import re

import httpx
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message, BufferedInputFile


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

router = Router()


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
# TELEGRAM PROFILE
# ============================================================

async def get_profile(
    bot: Bot,
    user_id: int
):

    chat = await bot.get_chat(user_id)

    username = (
        f"@{chat.username}"
        if chat.username
        else "no_username"
    )

    name = chat.full_name or "Unknown"

    bio = getattr(
        chat,
        "bio",
        ""
    ) or ""

    avatar = None

    try:

        photos = await bot.get_user_profile_photos(
            user_id=user_id,
            limit=1
        )

        if photos.total_count > 0:

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

    except Exception as e:

        logging.warning(
            "Could not get avatar for %s: %s",
            user_id,
            e
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
    player1: Profile,
    player2: Profile
):

    prompt = """
You are the AI judge of a humorous Telegram profile
comparison game called MOG.

You are comparing TWO Telegram profiles.

IMPORTANT:

Judge ONLY the profile presentation.

You may judge:

- avatar
- username
- bio
- profile coherence
- visual/style vibe
- originality
- first impression of the PROFILE

Do NOT judge or infer:

- race
- ethnicity
- religion
- politics
- sexual orientation
- health
- disability
- exact age
- body
- physical attractiveness
- sensitive personal characteristics

Do not identify people.

Do not invent information that is not provided.

Use the entire 0-10 scale.

Avoid giving everybody similar scores.
A truly exceptional profile can receive 9-10.
A weak profile can receive 0-4.

Score the following:

avatar
username
bio
coherence
vibe

For each category provide:

1. score from 0 to 10
2. short explanation

Then provide a short Russian verdict.

RETURN ONLY VALID JSON.

The exact format must be:

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
  "verdict": "короткий русский вердикт"
}

Maximum verdict length: 250 characters.
"""


    def profile_text(profile):

        return (
            f"USERNAME: {profile.username}\n"
            f"DISPLAY NAME: {profile.name}\n"
            f"BIO: {profile.bio or '(no bio)'}"
        )


    parts = [
        {
            "text": prompt
        },

        {
            "text":
                "\nPLAYER 1\n"
                + profile_text(player1)
        },

        {
            "text":
                "\nPLAYER 2\n"
                + profile_text(player2)
        }
    ]


    # PLAYER 1 AVATAR

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
                    "The image above is PLAYER 1 avatar."
            }
        )


    # PLAYER 2 AVATAR

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
                    "The image above is PLAYER 2 avatar."
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
            "responseMimeType": "application/json"
        }
    }


    async with httpx.AsyncClient(
        timeout=120
    ) as client:

        response = await client.post(
            url,
            params={
                "key": GEMINI_API_KEY
            },
            json=payload
        )

        response.raise_for_status()

        data = response.json()


    try:

        text = (
            data["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

    except Exception:

        raise RuntimeError(
            "Gemini returned an unexpected response."
        )


    # Remove accidental markdown fences

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


    # Extract JSON

    match = re.search(
        r"\{.*\}",
        text,
        re.DOTALL
    )


    if not match:

        raise RuntimeError(
            "Gemini did not return valid JSON."
        )


    try:

        return json.loads(
            match.group(0)
        )

    except json.JSONDecodeError:

        raise RuntimeError(
            "Could not parse Gemini JSON."
        )


# ============================================================
# SCORING
# ============================================================

WEIGHTS = {

    "avatar":
        0.30,

    "username":
        0.20,

    "bio":
        0.20,

    "coherence":
        0.15,

    "vibe":
        0.15
}


def clamp(
    value
):

    try:

        value = float(value)

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


    for raw in ai_result["players"][:2]:

        scores = {}

        for key in WEIGHTS:

            scores[key] = clamp(
                raw.get(
                    key,
                    0
                )
            )


        # IMPORTANT:
        # Overall is calculated by the bot,
        # not Gemini.

        overall = 0

        for key, weight in WEIGHTS.items():

            overall += (
                scores[key] *
                weight
            )


        scores["overall"] = round(
            overall,
            2
        )


        players.append(
            scores
        )


    if (
        players[0]["overall"]
        >=
        players[1]["overall"]
    ):

        winner = 0
        loser = 1

    else:

        winner = 1
        loser = 0


    difference = round(
        abs(
            players[0]["overall"]
            -
            players[1]["overall"]
        ),
        2
    )


    if difference >= 2:

        status = "ABSOLUTE MOG"

    elif difference >= 1:

        status = "DOMINATED"

    elif difference >= 0.25:

        status = "MOGGED"

    else:

        status = "CLOSE DUEL"


    verdict = ai_result.get(
        "verdict",
        ""
    )


    if not verdict:

        verdict = (
            f"{names[winner]} wins."
        )


    return {
        "players":
            players,

        "winner":
            winner,

        "loser":
            loser,

        "winner_name":
            names[winner],

        "status":
            status,

        "difference":
            difference,

        "verdict":
            str(verdict)[:300]
    }


# ============================================================
# CARD DESIGN
# ============================================================

def get_font(
    size,
    bold=False
):

    possible_fonts = [

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


    for path in possible_fonts:

        if os.path.exists(path):

            return ImageFont.truetype(
                path,
                size
            )


    return ImageFont.load_default()


def make_circle_avatar(
    avatar_data,
    size=190
):

    if avatar_data:

        try:

            avatar = Image.open(
                io.BytesIO(
                    avatar_data
                )
            ).convert("RGB")

        except Exception:

            avatar = Image.new(
                "RGB",
                (size, size),
                "#33333d"
            )

    else:

        avatar = Image.new(
            "RGB",
            (size, size),
            "#33333d"
        )


    avatar.thumbnail(
        (size, size)
    )


    canvas = Image.new(
        "RGB",
        (size, size),
        "#17171f"
    )


    x = (
        size -
        avatar.width
    ) // 2

    y = (
        size -
        avatar.height
    ) // 2


    canvas.paste(
        avatar,
        (x, y)
    )


    mask = Image.new(
        "L",
        (size, size),
        0
    )


    mask_draw = ImageDraw.Draw(
        mask
    )


    mask_draw.ellipse(
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
        (size, size)
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

    WIDTH = 1200
    HEIGHT = 1280


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
            WIDTH,
            HEIGHT
        ),
        BG
    )


    draw = ImageDraw.Draw(
        image
    )


    # HEADER

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


    positions = [
        100,
        700
    ]


    # AVATARS

    for index, (
        player,
        x
    ) in enumerate(
        zip(
            players,
            positions
        )
    ):

        avatar = make_circle_avatar(
            player.avatar,
            190
        )


        image.paste(
            avatar,
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


        # MOGGED BADGE

        if index == result["loser"]:

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


    # CATEGORY BARS

    categories = [

        (
            "AVATAR",
            "avatar"
        ),

        (
            "USERNAME",
            "username"
        ),

        (
            "BIO",
            "bio"
        ),

        (
            "COHERENCE",
            "coherence"
        ),

        (
            "VIBE",
            "vibe"
        )
    ]


    y = 480


    for label, key in categories:

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


        for index, x in enumerate(
            positions
        ):

            score = result[
                "players"
            ][index][key]


            bar_x = x
            bar_y = y + 35

            bar_width = 400
            bar_height = 19


            # background

            draw.rounded_rectangle(
                (
                    bar_x,
                    bar_y,
                    bar_x + bar_width,
                    bar_y + bar_height
                ),
                radius=9,
                fill=BAR_BG
            )


            # score

            draw.rounded_rectangle(
                (
                    bar_x,
                    bar_y,
                    bar_x +
                    (
                        bar_width *
                        score /
                        10
                    ),
                    bar_y +
                    bar_height
                ),
                radius=9,
                fill=YELLOW
            )


            draw.text(
                (
                    bar_x +
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


    # OVERALL

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


    for index, x in enumerate(
        positions
    ):

        score = result[
            "players"
        ][index]["overall"]


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
                (
                    bar_width *
                    score /
                    10
                ),
                bar_y +
                bar_height
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


    # WINNER

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


    draw.text(
        (
            60,
            1060
        ),
        "WINNER  " +
        result["winner_name"],
        font=get_font(
            43,
            True
        ),
        fill=PURPLE
    )


    # VERDICT

    draw.text(
        (
            60,
            1130
        ),
        result["verdict"],
        font=get_font(
            20
        ),
        fill=WHITE
    )


    # FOOTER

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
# MOG COMMAND
# ============================================================

@router.message(
    re.compile(
        r"^(\.мог|/mog)"
        r"(?:\s+@([A-Za-z0-9_]{5,32}))?$",
        re.IGNORECASE
    )
)
async def mog_command(
    message: Message,
    bot: Bot
):

    match = message.regexp_match

    target_username = (
        match.group(2)
        if match
        else None
    )


    # --------------------------------------------------------
    # MODE 1
    # .мог as reply
    # --------------------------------------------------------

    if (
        message.reply_to_message
        and
        message.reply_to_message.from_user
    ):

        player1_id = (
            message
            .reply_to_message
            .from_user
            .id
        )

        player2_id = (
            message
            .from_user
            .id
        )


    # --------------------------------------------------------
    # MODE 2
    # .мог @username
    # --------------------------------------------------------

    elif target_username:

        try:

            target = await bot.get_chat(
                "@" + target_username
            )


            player1_id = (
                message
                .from_user
                .id
            )

            player2_id = target.id


        except Exception:

            await message.answer(
                "❌ Не удалось найти пользователя.\n\n"
                "Самый надёжный вариант — "
                "ответить <code>.мог</code> "
                "на сообщение пользователя."
            )

            return


    # --------------------------------------------------------
    # NO TARGET
    # --------------------------------------------------------

    else:

        await message.answer(
            "<b>⚔️ MOG AI</b>\n\n"
            "Ответь командой <code>.мог</code> "
            "на сообщение пользователя.\n\n"
            "Или используй:\n"
            "<code>.мог @username</code>"
        )

        return


    # --------------------------------------------------------
    # SELF MOG
    # --------------------------------------------------------

    if player1_id == player2_id:

        await message.answer(
            "😐 Себя с собой сравнивать нельзя."
        )

        return


    # --------------------------------------------------------
    # STATUS MESSAGE
    # --------------------------------------------------------

    status_message = await message.answer(
        "⚔️ <b>MOG BATTLE STARTED</b>\n\n"
        "🔎 Получаю профили...\n"
        "🖼 Проверяю аватары...\n"
        "📝 Анализирую bio...\n"
        "🔤 Анализирую username...\n"
        "🧠 Gemini готовит verdict..."
    )


    try:

        # ----------------------------------------------------
        # GET PROFILES
        # ----------------------------------------------------

        player1 = await get_profile(
            bot,
            player1_id
        )


        player2 = await get_profile(
            bot,
            player2_id
        )


        # ----------------------------------------------------
        # GEMINI
        # ----------------------------------------------------

        ai_result = await analyze_with_gemini(
            player1,
            player2
        )


        # ----------------------------------------------------
        # CALCULATE
        # ----------------------------------------------------

        result = calculate_scores(
            ai_result,
            [
                player1.username,
                player2.username
            ]
        )


        # ----------------------------------------------------
        # CREATE CARD
        # ----------------------------------------------------

        card = create_card(
            player1,
            player2,
            result
        )


        # ----------------------------------------------------
        # CAPTION
        # ----------------------------------------------------

        caption = (
            "🏆 <b>"
            + result["winner_name"]
            + "</b>\n"
            "\n"
            "⚡ <b>"
            + result["status"]
            + "</b>\n"
            "\n"
            f"{player1.username}: "
            f"<b>"
            f"{result['players'][0]['overall']:.2f}"
            f"/10"
            f"</b>\n"
            f"{player2.username}: "
            f"<b>"
            f"{result['players'][1]['overall']:.2f}"
            f"/10"
            f"</b>\n"
            "\n"
            f"📊 Difference: "
            f"<b>"
            f"{result['difference']:.2f}"
            f"</b>\n"
            "\n"
            f"💬 {result['verdict']}"
        )


        # ----------------------------------------------------
        # SEND CARD
        # ----------------------------------------------------

        await message.answer_photo(
            BufferedInputFile(
                card,
                filename="mog.png"
            ),
            caption=caption
        )


        await status_message.delete()


    except Exception as error:

        logging.exception(
            "MOG ERROR"
        )


        error_text = str(error)


        if len(error_text) > 700:

            error_text = (
                error_text[:700]
                + "..."
            )


        try:

            await status_message.edit_text(
                "❌ <b>MOG FAILED</b>\n\n"
                "<code>"
                + error_text
                + "</code>"
            )

        except Exception:

            await message.answer(
                "❌ MOG не удалось запустить.\n\n"
                "<code>"
                + error_text
                + "</code>"
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


    dp = Dispatcher()

    dp.include_router(
        router
    )


    logging.info(
        "MOG AI bot started"
    )


    await dp.start_polling(
        bot
    )


if __name__ == "__main__":

    asyncio.run(
        main()
)
