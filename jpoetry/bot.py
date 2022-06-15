import asyncio
from bisect import bisect_left
from datetime import datetime
import logging
import sys
from io import BytesIO
from pathlib import Path
from this import d

from aiogram import Bot, Dispatcher
from aiogram.types import (
    ContentType,
    InlineQuery,
    InlineQueryResult,
    InlineQueryResultArticle,
    InputFile,
    InputMessageContent,
    Message,
    ParseMode,
    Update,
)
from aiogram.utils.markdown import escape_md
from loguru import logger

from jpoetry.answers import HELP_TEXT, WELCOME_TEXT
from jpoetry.config import BOT_TOKEN, DEFAULT_AUTHOR, TOO_LONG_MESSAGE_FILE
from jpoetry.image import TooLongTextError, draw_text
from jpoetry.poetry import Genre, Poem, detect_poems, iter_poems
from jpoetry.templates import POETRY_IMAGES_INFO
from jpoetry.text import remove_unsupported_chars
from jpoetry.utils import TimeAwareCounter, Timer


class InterceptHandler(logging.Handler):
    def emit(self, record) -> None:
        logger_opt = logger.opt(depth=6, exception=record.exc_info)
        message = record.getMessage()
        while r'\u' in message:
            message = message.encode().decode('unicode-escape')
        logger_opt.log(record.levelno, message)


logging.basicConfig(handlers=[InterceptHandler()], level=0)

logger.add(sys.stderr, level="DEBUG")

# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.MARKDOWN_V2, validate_token=False)
dp = Dispatcher(bot)

# HACK I don't want to do proper monitoring, just want to know how many people are using the bot
# Ugly global shit. I know. And I don't care ✨
one_day = 60 * 60 * 24
PEOPLE = TimeAwareCounter[int](one_day, "people")
GROUPS = TimeAwareCounter[int](one_day, "groups")
MESSAGES_HANDLED_FOR_LAST_24_HOURS = TimeAwareCounter(one_day, "messages")
POEMS_GENERATED_FOR_LAST_24_HOURS = TimeAwareCounter(one_day, "poems")
INLINE_REQUESTS_FOR_LAST_24_HOURS = TimeAwareCounter(one_day, "inline")


def get_author(message: Message, max_len: int = 40) -> str:
    """
    Find most suitable author name
    """
    if (not message.is_forward() and (user := message.from_user)) or (
        user := message.forward_from
    ):
        author = remove_unsupported_chars(user.full_name)

        if user.username and (
            len(author) > max_len or len(author) < len(user.full_name) / 2
        ):
            author = "@" + user.username
    else:  # it's forward, but user info is hidden
        author = remove_unsupported_chars(message.forward_sender_name)

    if not author or len(author) > max_len:
        return DEFAULT_AUTHOR
    return author


@dp.message_handler(
    lambda message: message.from_user.id == message.chat.id, commands=["start"]
)
async def welcome_user(message: Message) -> None:
    await message.reply(WELCOME_TEXT)


@dp.message_handler(commands=["help"])
async def send_cheat_sheet(message: Message) -> None:
    await message.reply(HELP_TEXT)


@dp.message_handler(commands=["info"])
async def print_info(message: Message) -> None:
    if not (message_to_reply := message.reply_to_message):
        message = await message.reply(escape_md("Пошёл нахуй)"))
        await asyncio.sleep(0.5)
        await message.delete()
        return

    poems, words_info, total_syllables = next(
        (detect_poems(message_to_reply.text, strict=False), (None, None, None))
    )
    poem = next(poems, None)
    if poem is not None:
        await message_to_reply.reply(escape_md(repr(poem)))
    elif words_info is None or total_syllables is None:
        await message_to_reply.reply(escape_md("Ну хуй знает..."))
    else:
        await message_to_reply.reply(
            escape_md(" ".join(map(repr, words_info)) + f"\n\nИтого: {total_syllables}")
        )


def get_interval(seconds):
    minutes = seconds // 60
    if not minutes:
        return f"{int(seconds)} seconds"
    hours = minutes // 60
    if not hours:
        return f"{int(minutes)} minutes"
    days = hours // 24
    if not days:
        return f"{int(hours)} hours"
    return f"{int(days)} days"


@dp.message_handler(commands=["stats"])
async def get_stats(message: Message) -> None:
    await message.reply(
        escape_md(
            f"Generated {POEMS_GENERATED_FOR_LAST_24_HOURS.count()} poems from\n"
            f"{MESSAGES_HANDLED_FOR_LAST_24_HOURS.count()} messages in\n"
            f"{GROUPS.count_unique()} groups and\n"
            f"{PEOPLE.count_unique()} personal dialogs and handled\n"
            f"{INLINE_REQUESTS_FOR_LAST_24_HOURS.count()} inline requests\n"
            f"in the last {(MESSAGES_HANDLED_FOR_LAST_24_HOURS.timestamps or 0) and get_interval(datetime.now().timestamp() - MESSAGES_HANDLED_FOR_LAST_24_HOURS.timestamps[0][0])}"
        )
    )


# @dp.channel_post_handler(content_types=ContentType.ANY)
# @dp.edited_channel_post_handler(content_types=ContentType.ANY)
# @dp.edited_message_handler(content_types=ContentType.ANY)
@dp.message_handler(content_types=ContentType.ANY)
async def detect_and_send_poem(message: Message) -> None:
    if message.chat.id == (person_id := message.from_user.id):
        PEOPLE.add(person_id)
    else:
        GROUPS.add(person_id)

    MESSAGES_HANDLED_FOR_LAST_24_HOURS.inc()
    if message.text is None:
        return

    for poem in iter_poems(message.text):
        POEMS_GENERATED_FOR_LAST_24_HOURS.inc()
        author = get_author(message)
        try:
            with Timer("get_poem_image") as timer:
                image_data = await asyncio.get_event_loop().run_in_executor(
                    None, get_poem_image, poem, author
                )
            logger.info(f"{poem.genre.name} image is created in {timer.elapsed:.4} seconds")
        except TooLongTextError:
            image = TOO_LONG_MESSAGE_FILE
            logger.error(f"Too many chars in {poem.genre.name}, sending default image")
        else:
            image = InputFile(image_data, filename=f"{author} — {poem.genre}.png")

        await bot.send_photo(message.chat.id, image, reply_to_message_id=message.message_id)


@dp.inline_handler()
async def answer_inline_query(query: InlineQuery) -> None:
    INLINE_REQUESTS_FOR_LAST_24_HOURS.inc()
    text = query.query
    if not text:
        await query.answer(
            results=[],
            switch_pm_text="Не ссы, пиши давай",
            switch_pm_parameter='ok',
            cache_time=0,
        )

    total_syllables = words_info = best_poem = None
    for poems, words_info, total_syllables in detect_poems(text, strict=False):
        try:
            current_poem = min(poems, key=lambda poem: poem.total_issues)
        except ValueError:
            continue
        if not best_poem or best_poem.total_issues > current_poem.total_issues:
            best_poem = current_poem

    if words_info is None or total_syllables is None:
        await query.answer(
            results=[],
            switch_pm_text="Ну хуй знает...",
            switch_pm_parameter='ok',
            cache_time=0,
        )
        return
    if best_poem is not None:
        info = message_text = repr(best_poem)
    else:
        words = " ".join(map(repr, words_info))
        info = message_text = f"{total_syllables}: " + words
        if len(message_text) > 30:
            info = f"{total_syllables}: " + f'{words[:10]}...{words[-17:]}'

    print(info)
    await query.answer(
        results=[
            InlineQueryResultArticle(
                id=i,
                title=line,
                input_message_content=InputMessageContent(message_text=message_text),
            )
            for i, line in enumerate(info.split('\n'))
            if line.strip()
        ]
    )


def get_poem_image(poem: Poem, author: str) -> BytesIO:
    return draw_text(
        POETRY_IMAGES_INFO[poem.genre],
        phrases=list(map(str, poem.phrases)),
        author=[f"— {author}"],
    )
