"""
Discord AI Bot: a Discord bot powered by Google Gemini with swappable personas.

- Replies only when someone @mentions it.
- Keeps a short per-channel memory.
- The character lives in a text file in the personas/ folder (see PERSONA_FILE).
"""

import asyncio
import os
from collections import defaultdict, deque
from pathlib import Path

import discord
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# One or more models, comma-separated. If the first is busy, the next one is tried.
# Model names change often: the startup log prints the ones your key can use.
MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_MODELS", "gemini-3.8-flash").split(",")
    if m.strip()
]

# Which persona file to load (relative to this folder). See personas/.
PERSONA_FILE = os.getenv("PERSONA_FILE", "personas/shorekeeper.md")

# Optional: the name the bot should call the server owner.
OWNER_NAME = os.getenv("OWNER_NAME", "").strip()

# Only these Discord user IDs can talk to the bot. Comma-separated.
# Leave empty to let everyone who mentions the bot use it.
ALLOWED_USER_IDS = {
    int(x)
    for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x.isdigit()
}

# How many recent messages (user + bot) to remember per channel.
MEMORY_SIZE = 12

# Total tries per question. Tries rotate through MODELS.
MAX_TRIES = 6


def load_persona() -> str:
    path = (BASE_DIR / PERSONA_FILE).resolve()
    text = path.read_text(encoding="utf-8").strip()
    if OWNER_NAME:
        text += (
            f"\n\nThe person who runs this server is called {OWNER_NAME}. Their Discord "
            f"name may look different, but always call them {OWNER_NAME}. Be especially "
            "warm and familiar with them. Everyone else is a valued member: be kind to "
            "all of them equally."
        )
    return text


PERSONA = load_persona()

intents = discord.Intents.default()
intents.message_content = True  # also enable "Message Content Intent" in the dev portal

bot = discord.Client(intents=intents)
gemini = genai.Client(api_key=GEMINI_API_KEY)

# channel_id -> recent messages as Gemini Content objects
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MEMORY_SIZE))


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Discord messages max out at 2000 characters, so split long replies."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


async def ask_gemini(contents: list) -> str | None:
    """Ask Gemini, rotating through MODELS and retrying if Google is busy."""
    for attempt in range(1, MAX_TRIES + 1):
        model = MODELS[(attempt - 1) % len(MODELS)]
        try:
            response = await gemini.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=PERSONA,
                    max_output_tokens=600,
                    temperature=0.9,
                ),
            )
            return (response.text or "").strip()
        except Exception as exc:  # busy servers (503), rate limits (429), bad model (404)
            print(f"Gemini error (try {attempt}/{MAX_TRIES}, model {model}): {exc}")
            if attempt < MAX_TRIES:
                await asyncio.sleep(min(2 * attempt, 8))
    return None


def list_usable_models() -> list[str]:
    names = []
    for m in gemini.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if "generateContent" in actions:
            names.append((m.name or "").removeprefix("models/"))
    return names


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (models in order: {', '.join(MODELS)})")
    print(f"Persona: {PERSONA_FILE}")
    if ALLOWED_USER_IDS:
        print(f"Only answering {len(ALLOWED_USER_IDS)} allowed user(s).")
    else:
        print("ALLOWED_USER_IDS is empty: answering everyone who mentions me.")
    try:
        names = await asyncio.to_thread(list_usable_models)
        print("Models your key can use:", ", ".join(names) or "(none found)")
    except Exception as exc:
        print(f"Could not list models: {exc}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if bot.user is None or bot.user not in message.mentions:
        return
    if ALLOWED_USER_IDS and message.author.id not in ALLOWED_USER_IDS:
        return  # not on the allow-list: stay silent

    # Remove the @mention so the model just sees the question.
    text = message.content
    for mention in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        text = text.replace(mention, "")
    text = text.strip() or "(they only mentioned you)"

    channel_id = message.channel.id
    history[channel_id].append(
        types.Content(
            role="user",
            parts=[types.Part(text=f"{message.author.display_name}: {text}")],
        )
    )

    async with message.channel.typing():
        reply = await ask_gemini(list(history[channel_id]))

    if reply is None:
        # Gemini failed every try: forget this question so it doesn't pile up.
        history[channel_id].pop()
        await message.reply(
            "Something went wrong on my end... please try again in a moment.",
            mention_author=False,
        )
        return

    if not reply:
        reply = "...I could not find the words for that. Could you ask in another way?"

    history[channel_id].append(
        types.Content(role="model", parts=[types.Part(text=reply)])
    )

    chunks = split_message(reply)
    await message.reply(chunks[0], mention_author=False)
    for chunk in chunks[1:]:
        await message.channel.send(chunk)


bot.run(DISCORD_TOKEN)
