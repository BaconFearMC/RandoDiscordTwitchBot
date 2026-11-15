import asyncio
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks

# ---------- Configuration ----------

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

SCAN_INTERVAL = max(60, int(os.getenv("SCAN_INTERVAL", "60")))
GAME_NAME = "Super Mario Odyssey"

# Substring matching is intentional: "Randomizer" contains "Random".
KEYWORD_PATTERN = re.compile(r"Randomizers|Random|Rando", re.IGNORECASE)

STATE_DIR = Path(os.getenv("STATE_DIR", "/data"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = STATE_DIR / "stream_state.sqlite3"

TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("smo-twitch-alerts")


# ---------- Persistent state ----------

def initialize_database():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS streams (
                stream_id TEXT PRIMARY KEY,
                was_matching INTEGER NOT NULL,
                last_title TEXT NOT NULL,
                last_seen REAL NOT NULL
            )
        """)
        db.commit()


def get_previous_stream(stream_id):
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT was_matching FROM streams WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
    return bool(row[0]) if row else False


def save_stream(stream_id, matches, title):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            INSERT INTO streams (stream_id, was_matching, last_title, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(stream_id) DO UPDATE SET
                was_matching = excluded.was_matching,
                last_title = excluded.last_title,
                last_seen = excluded.last_seen
        """, (stream_id, int(matches), title, time.time()))
        db.commit()


def remove_streams_not_seen(stream_ids):
    with sqlite3.connect(DB_PATH) as db:
        if not stream_ids:
            db.execute("DELETE FROM streams")
        else:
            placeholders = ",".join("?" for _ in stream_ids)
            db.execute(
                f"DELETE FROM streams WHERE stream_id NOT IN ({placeholders})",
                tuple(stream_ids),
            )
        db.commit()


# ---------- Twitch API ----------

class TwitchClient:
    def __init__(self):
        self.session = None
        self.access_token = None
        self.token_expires_at = 0
        self.game_id = None

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)
        await self.authenticate()
        await self.find_game()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def authenticate(self):
        if self.access_token and time.time() < self.token_expires_at - 60:
            return

        async with self.session.post(
            TWITCH_TOKEN_URL,
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        ) as response:
            response.raise_for_status()
            payload = await response.json()

        self.access_token = payload["access_token"]
        self.token_expires_at = time.time() + payload["expires_in"]
        log.info("Authenticated with Twitch.")

    async def request(self, endpoint, params=None):
        await self.authenticate()
        headers = {
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {self.access_token}",
        }

        async with self.session.get(
            f"{TWITCH_API}/{endpoint}",
            headers=headers,
            params=params,
        ) as response:
            if response.status == 401:
                self.access_token = None
                await self.authenticate()
                headers["Authorization"] = f"Bearer {self.access_token}"
                async with self.session.get(
                    f"{TWITCH_API}/{endpoint}",
                    headers=headers,
                    params=params,
                ) as retry:
                    retry.raise_for_status()
                    return await retry.json()

            response.raise_for_status()
            return await response.json()

    async def find_game(self):
        payload = await self.request("games", {"name": GAME_NAME})
        for game in payload.get("data", []):
            if game["name"].casefold() == GAME_NAME.casefold():
                self.game_id = game["id"]
                log.info("Monitoring Twitch category %s (ID %s).",
                         game["name"], self.game_id)
                return
        raise RuntimeError(f"Twitch category not found: {GAME_NAME}")

    async def get_live_streams(self):
        results = []
        cursor = None

        while True:
            params = {"game_id": self.game_id, "first": 100}
            if cursor:
                params["after"] = cursor

            payload = await self.request("streams", params)
            results.extend(payload.get("data", []))
            cursor = payload.get("pagination", {}).get("cursor")
            if not cursor:
                break

        return results


# ---------- Discord bot ----------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
twitch = TwitchClient()
scan_lock = asyncio.Lock()


async def send_stream_alert(channel, stream):
    title = stream["title"]
    match = KEYWORD_PATTERN.search(title)
    keyword = match.group(0) if match else "Rando"

    username = discord.utils.escape_markdown(stream["user_name"])
    login = stream["user_login"]

    embed = discord.Embed(
        title="Super Mario Odyssey Rando stream is LIVE!",
        description=(
            f"**[{username} is live on Twitch](https://twitch.tv/{login})**\n\n"
            f"**Title:** {discord.utils.escape_markdown(title)}"
        ),
        color=discord.Color.purple(),
        url=f"https://twitch.tv/{login}",
    )
    embed.add_field(name="Matched keyword", value=f"`{keyword}`", inline=True)
    embed.add_field(
        name="Viewers", value=str(stream.get("viewer_count", 0)), inline=True
    )
    embed.add_field(name="Category", value=GAME_NAME, inline=True)

    thumbnail = stream.get("thumbnail_url", "")
    if thumbnail:
        thumbnail = thumbnail.replace("{width}", "640").replace("{height}", "360")
        embed.set_image(url=thumbnail)

    embed.set_footer(text="SMO Twitch Rando Monitor")
    await channel.send(
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def scan_once():
    async with scan_lock:
        channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)

        streams = await twitch.get_live_streams()
        seen_ids = []
        pending_alerts = []

        # Determine alerts first. Persist each stream only after its alert
        # succeeds, so a Discord send failure can be retried next scan.
        for stream in streams:
            stream_id = stream["id"]
            title = stream["title"]
            matches = bool(KEYWORD_PATTERN.search(title))
            previously_matching = get_previous_stream(stream_id)

            seen_ids.append(stream_id)

            if matches and not previously_matching:
                pending_alerts.append(stream)

            # Non-matching titles are saved immediately. A later matching
            # title will then trigger an alert.
            if not matches:
                save_stream(stream_id, False, title)

        for stream in pending_alerts:
            await send_stream_alert(channel, stream)
            save_stream(stream["id"], True, stream["title"])
            log.info("Alert sent for %s: %s",
                     stream["user_name"], stream["title"])

        # Mark matching streams that were already announced as still matching.
        for stream in streams:
            if KEYWORD_PATTERN.search(stream["title"]):
                save_stream(stream["id"], True, stream["title"])

        remove_streams_not_seen(seen_ids)
        log.info(
            "Scan complete: %d live SMO streams; %d new alerts.",
            len(streams), len(pending_alerts),
        )


@tasks.loop(seconds=SCAN_INTERVAL)
async def scan_twitch():
    try:
        await scan_once()
    except Exception:
        log.exception("Twitch scan failed; will retry on the next interval.")


@scan_twitch.before_loop
async def before_scan():
    await bot.wait_until_ready()


class SMOBot(commands.Bot):
    async def setup_hook(self):
        initialize_database()
        await twitch.start()
        scan_twitch.start()

    async def close(self):
        if scan_twitch.is_running():
            scan_twitch.cancel()
        await twitch.close()
        await super().close()


bot = SMOBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info("Discord bot connected as %s.", bot.user)
    log.info("Scanning %s every %s seconds for: Rando, Random, Randomizers.",
             GAME_NAME, SCAN_INTERVAL)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
