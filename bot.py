import asyncio
import base64
import html as html_lib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal, Optional

import discord
import yt_dlp
import aiohttp
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ------------------------------------------------------------------ config

DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", "!")
PREFIX_FILE = os.getenv("PREFIX_FILE", "prefixes.json")
STAY_FILE = os.getenv("STAY_FILE", "247.json")
AFK_FILE = os.getenv("AFK_FILE", "afk.json")
AR_FILE = os.getenv("AUTORESPONDER_FILE", "autoresponders.json")
IDLE_TIMEOUT = 300  # seconds before leaving an idle/empty channel (when 24/7 mode is off)
STATUS_EMOJI = os.getenv("STATUS_EMOJI", "<a:Music:1565878360054173877>")  # shown before the song in the voice channel status
START_TIME = time.time()
NO_COOKIE_CLIENTS = {"android_vr", "android", "ios"}  # these YouTube clients don't accept cookies
YT_PLAYER_CLIENT = os.getenv("YT_PLAYER_CLIENT")  # optional: force one YouTube client (e.g. android_vr)
YT_FALLBACK_CLIENTS = [c.strip() for c in os.getenv("YT_FALLBACK_CLIENTS", "android_vr,tv,mweb,web_safari").split(",") if c.strip()]

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "source_address": "0.0.0.0",
}
if YT_PLAYER_CLIENT:
    YTDL_OPTS["extractor_args"] = {"youtube": {"player_client": [YT_PLAYER_CLIENT]}}


def find_cookies():
    """Use a YouTube cookies file if one exists (helps with 'Sign in to confirm you're not a bot').

    Looked up in this order: the YOUTUBE_COOKIES_B64 / YOUTUBE_COOKIES environment variables
    (handy on Railway), then a cookies.txt file."""
    dst = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")  # writable copy: yt-dlp updates cookies

    text = os.getenv("YOUTUBE_COOKIES")
    b64 = os.getenv("YOUTUBE_COOKIES_B64")
    if b64:
        try:
            text = base64.b64decode(b64.strip()).decode("utf-8")
        except Exception as e:
            print(f"YOUTUBE_COOKIES_B64 could not be decoded: {e!r}")
    if text and text.strip():
        if "\n" not in text:  # some hosts flatten newlines into a literal backslash-n
            text = text.replace("\\n", "\n")
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.strip() + "\n")
        print("Using cookies from the environment variable")
        return dst

    for path in (os.getenv("COOKIES_FILE"), "/etc/secrets/cookies.txt", "cookies.txt"):
        if path and os.path.isfile(path):
            shutil.copyfile(path, dst)
            print(f"Using cookies from {path}")
            return dst
    print("No cookies found (no YOUTUBE_COOKIES_B64 variable and no cookies.txt file).")
    return None


_cookies = find_cookies()
if _cookies:
    YTDL_OPTS["cookiefile"] = _cookies

# Ways to fetch a YouTube song, tried in order if one fails: (client, use cookies?).
# Old or invalid cookies can make YouTube refuse requests that work fine without them,
# so the second attempt is always "default clients, no cookies".
YT_ATTEMPTS = ([(None, True)] if _cookies else []) + [(None, False)] + [(c, False) for c in YT_FALLBACK_CLIENTS]

if shutil.which("ffmpeg") is None:
    print("WARNING: ffmpeg was not found on PATH, so music cannot play. Install it, then close and reopen this window.")

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# ---------------------------------------------------------------- prefixes


def load_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json_file(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_prefixes() -> dict:
    return load_json_file(PREFIX_FILE)


def save_prefixes():
    save_json_file(PREFIX_FILE, prefixes)


def save_stay247():
    save_json_file(STAY_FILE, stay247)


prefixes: dict[str, str] = load_prefixes()
stay247: dict[str, int] = load_json_file(STAY_FILE)  # guild id -> voice channel id
afk_data: dict[str, dict] = load_json_file(AFK_FILE)  # guild id -> user id -> {"reason", "since"}
autoresponders: dict[str, dict] = load_json_file(AR_FILE)  # guild id -> trigger -> {"trigger", "response"}


def save_afk():
    save_json_file(AFK_FILE, afk_data)


def save_autoresponders():
    save_json_file(AR_FILE, autoresponders)


def prefix_for(guild: discord.Guild | None) -> str:
    if guild is None:
        return DEFAULT_PREFIX
    return prefixes.get(str(guild.id), DEFAULT_PREFIX)


async def get_prefix(bot, message: discord.Message):
    # Mentioning the bot always works, so a forgotten prefix can be recovered
    return commands.when_mentioned_or(prefix_for(message.guild))(bot, message)


# ------------------------------------------------------------------- state


@dataclass
class Track:
    title: str
    webpage_url: str
    requester: str
    requester_mention: str = ""
    artist: str = "Unknown"
    duration: int | None = None
    platform: str = "Unknown"
    thumbnail: str | None = None
    video_id: str | None = None
    play_url: str | None = None  # what to actually stream (differs from webpage_url for Spotify entries)
    origin: str | None = None  # e.g. "Spotify" when the song came from a Spotify link
    search_query: str | None = None  # used to find Spotify entries on YouTube
    resolved: bool = True  # False until a Spotify entry has been matched to a video
    attempt: int = 0  # how many fallback YouTube clients have been tried for this song
    likes: set = field(default_factory=set)
    dislikes: set = field(default_factory=set)


class GuildState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop: str = "off"  # "off", "track", or "queue"
        self.volume: float = 0.5  # 0.0 - 1.0
        self.skip_requested: bool = False
        self.idle_since: float | None = None
        self.notify_channel = None  # where to report playback errors and post the player message
        self.player_message = None  # last 'Now Playing' message, replaced when the next song starts
        self.player_view = None
        self.last_status: str | None = None  # last voice channel status we set
        self.status_warned: bool = False  # only complain about missing permission once
        self.history: deque[Track] = deque(maxlen=25)  # for the Previous button
        self.autoplay: bool = False
        self.going_back: bool = False  # set while restarting a song (previous / seek)
        self.start_offset: int = 0  # seconds to skip into the next song (seek)
        self.position_anchor: float = 0.0  # playback position at anchor_time
        self.anchor_time: float | None = None  # monotonic time of anchor, None while paused


states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    return states.setdefault(guild_id, GuildState())


# ------------------------------------------------------------ message style

PLAYER_NAME = "Mannat Player"
PLAYER_EMOJI = "🌸"  # shown next to the bot's name everywhere

COLORS = {  # soft pastel palette, one shade per kind of message
    "pink": 0xFFB3C6, "rose": 0xFFC8DD, "blush": 0xFFB7B2, "peach": 0xFFCBA4, "apricot": 0xFFD6A5,
    "cream": 0xFFE5D9, "butter": 0xFDFFB6, "lemon": 0xFFF1B5, "sage": 0xCAFFBF, "mint": 0xB5EAD7,
    "seafoam": 0xC7F9CC, "aqua": 0x9BF6FF, "powder": 0xCDE7F0, "cloud": 0xD6E6FF, "sky": 0xA0C4FF,
    "periwinkle": 0xBDB2FF, "lavender": 0xCDB4DB, "wisteria": 0xD7C6F3, "mist": 0xE8DFF5,
    "lilac": 0xE0BBE4, "orchid": 0xFFC6FF, "coral": 0xFFADAD,
    "petal": 0xFFD1DC, "honey": 0xFFE29A, "lagoon": 0xB8E0D2, "melon": 0xFFC9B9,
    "pistachio": 0xDCEDC8, "rosewater": 0xF8D7DA, "ice": 0xC4E3F3, "taffy": 0xF7C8E0,
}
PANEL_COLOR = COLORS["pink"]

KINDS = {  # kind -> (emoji, color)
    "success": ("✅", COLORS["mint"]),
    "error": ("❌", COLORS["coral"]),
    "warn": ("⚠️", COLORS["butter"]),
    "info": ("✨", COLORS["lilac"]),
}


def msg_embed(text: str, kind: str = "info", title: str | None = None, color: int | None = None) -> discord.Embed:
    emoji, default = KINDS[kind]
    return discord.Embed(title=title, description=f"{emoji}  {text}", color=discord.Color(color if color is not None else default))


async def reply(ctx: commands.Context, text: str, kind: str = "info", title: str | None = None,
                ephemeral: bool = False, color: int | None = None):
    return await ctx.send(embed=msg_embed(text, kind, title, color), ephemeral=ephemeral)


async def ireply(interaction: discord.Interaction, text: str, kind: str = "error", title: str | None = None):
    """Private (only-you-can-see) reply to a button or menu press."""
    await interaction.response.send_message(embed=msg_embed(text, kind, title), ephemeral=True)


# --------------------------------------------------------------------- bot


async def start_web_server():
    """Tiny health-check page so hosts like Render (and uptime pingers) can reach the bot."""
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="Bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ["PORT"])).start()


class MusicBot(commands.Bot):
    async def setup_hook(self):
        voice_maintenance.start()
        player_refresh.start()
        await self.tree.sync()  # register slash commands


intents = discord.Intents.default()
intents.message_content = True  # required for prefix commands (enable it in the Developer Portal too)
bot = MusicBot(command_prefix=get_prefix, intents=intents, help_command=None)


@bot.check
async def servers_only(ctx: commands.Context):
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return True


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play | /help"))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # Typing only "@Bot" tells you the current prefix
    if message.guild and bot.user and message.content.strip() in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        p = prefix_for(message.guild)
        return await message.channel.send(
            embed=msg_embed(f"My prefix here is `{p}`\nTry `{p}help` or `/help`.", "info", f"{PLAYER_EMOJI} {PLAYER_NAME}", color=COLORS["lilac"])
        )
    if message.guild:
        ctx = await bot.get_context(message)
        try:
            is_afk_command = bool(ctx.valid and ctx.command and ctx.command.qualified_name == "afk")
            await handle_afk(message, is_afk_command)
            if not ctx.valid:  # real commands are never auto-responded to
                await handle_autoresponders(message)
        except Exception as e:
            print(f"Message handler error: {e!r}")
    await bot.process_commands(message)


@tasks.loop(seconds=30)
async def voice_maintenance():
    # 1) Leave idle or empty channels (skipped for servers with 24/7 mode on)
    for vc in list(bot.voice_clients):
        if str(vc.guild.id) in stay247:
            continue
        state = get_state(vc.guild.id)
        alone = not any(uid != bot.user.id for uid in vc.channel.voice_states)
        if vc.is_playing() and not alone:
            state.idle_since = None
        elif state.idle_since is None:
            state.idle_since = time.monotonic()
        elif time.monotonic() - state.idle_since >= IDLE_TIMEOUT:
            state.queue.clear()
            state.current = None
            state.idle_since = None
            await update_voice_status(vc.guild, None)
            await vc.disconnect()

    # 2) 24/7 servers: follow the bot if it was moved, and rejoin if it got disconnected
    changed = False
    for key, channel_id in list(stay247.items()):
        guild = bot.get_guild(int(key))
        if guild is None:
            continue
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != channel_id:
                stay247[key] = vc.channel.id
                changed = True
            continue
        channel = guild.get_channel(channel_id)
        if channel is None:  # channel was deleted
            del stay247[key]
            changed = True
            continue
        try:
            if vc:
                await vc.disconnect(force=True)
            await channel.connect()
            print(f"24/7: rejoined {channel.name} in {guild.name}")
        except Exception as e:
            print(f"24/7: could not rejoin {channel.name} in {guild.name}: {e}")
    if changed:
        save_stay247()


@voice_maintenance.before_loop
async def before_voice_maintenance():
    await bot.wait_until_ready()


@tasks.loop(seconds=10)
async def player_refresh():
    """Keep the progress bar on the player message moving."""
    for guild_id, state in list(states.items()):
        guild = bot.get_guild(guild_id)
        if not (state.player_message and state.current and guild and guild.voice_client):
            continue
        if not guild.voice_client.is_playing():  # paused or between songs
            continue
        try:
            await state.player_message.edit(embed=now_playing_embed(state.current, state))
        except discord.NotFound:
            state.player_message = None
        except discord.HTTPException:
            pass


@player_refresh.before_loop
async def before_player_refresh():
    await bot.wait_until_ready()


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.HybridCommandError):
        error = error.original
    if isinstance(error, commands.CommandInvokeError):
        error = error.original

    if isinstance(error, commands.MissingPermissions):
        perms = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        msg = f"You need the **{perms}** permission to use this command."
    elif isinstance(error, commands.BotMissingPermissions):
        perms = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        msg = f"I need the **{perms}** permission to do that. Ask an admin to add it to my role."
    elif isinstance(error, commands.MemberNotFound):
        msg = "I couldn't find that member. Mention them like `@username`."
    elif isinstance(error, commands.NoPrivateMessage):
        msg = "This command only works in a server."
    elif isinstance(error, commands.MissingRequiredArgument):
        msg = f"Missing `{error.param.name}`. Usage: `{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}`"
    elif isinstance(error, commands.UserInputError):
        msg = f"Invalid input. Usage: `{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}`"
    elif isinstance(error, commands.CheckFailure):
        msg = "You can't use this command."
    else:
        print(f"Unhandled error in {ctx.command}: {error!r}")
        msg = "Something went wrong running that command."
    try:
        await reply(ctx, msg, "error", ephemeral=True)
    except discord.HTTPException:
        pass


# ----------------------------------------------------------------- helpers


def build_query(query: str, source: str = "youtube") -> str:
    """Links are used as-is; plain text becomes a search on the chosen source."""
    if query.startswith(("http://", "https://")):
        return query
    prefix = "ytsearch5:" if source == "youtube" else "scsearch5:"
    return prefix + query


def parse_source(query: str) -> tuple[str, str]:
    """'sc: song' -> soundcloud, 'yt: song' -> youtube, otherwise YouTube."""
    m = re.match(r"^(yt|youtube|sc|soundcloud)\s*:\s*(.+)$", query.strip(), re.IGNORECASE | re.DOTALL)
    if m:
        return ("youtube" if m.group(1).lower() in ("yt", "youtube") else "soundcloud"), m.group(2).strip()
    return "youtube", query.strip()


async def extract(query: str, exclude: set | None = None) -> dict:
    loop = asyncio.get_running_loop()

    def run(base):
        is_search = query.startswith(("scsearch", "ytsearch"))
        opts = {**base, "extract_flat": "in_playlist"} if is_search else base
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)

        if "entries" not in info:
            return info
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise ValueError("No results found")
        if not is_search:
            return entries[0]

        # Search: try the results in order and use the first one that is playable.
        # Some results are DRM-protected, geo-blocked or removed.
        last_error = None
        with yt_dlp.YoutubeDL(base) as ydl:
            for entry in entries:
                if exclude and entry.get("id") in exclude:
                    continue
                url = entry.get("url") or entry.get("webpage_url")
                if not url:
                    continue
                try:
                    return ydl.extract_info(url, download=False)
                except Exception as e:
                    print(f"Skipping unplayable result {url}: {e}")
                    last_error = e
                    if "not a bot" in str(e):  # YouTube is blocking us; trying more results only makes it worse
                        break
        raise last_error or ValueError("No playable results found")

    try:
        return await loop.run_in_executor(None, run, YTDL_OPTS)
    except Exception as e:
        broken_cookies = any(m in str(e).lower() for m in ("page needs to be reloaded", "not a bot", "403"))
        if YTDL_OPTS.get("cookiefile") and broken_cookies:
            print("Request failed with cookies; retrying without them")
            return await loop.run_in_executor(None, run, {k: v for k, v in YTDL_OPTS.items() if k != "cookiefile"})
        raise


def fmt_duration(seconds) -> str:
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def track_from_info(info: dict, user: discord.abc.User) -> Track:
    artist = info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel") or "Unknown"
    artist = re.sub(r"\s*-\s*Topic$", "", artist)
    return Track(
        title=info.get("title", "Unknown title"),
        webpage_url=info.get("webpage_url") or info["url"],
        requester=user.display_name,
        requester_mention=f"{user.mention} ({user.name})",
        artist=artist,
        duration=info.get("duration"),
        platform=(info.get("extractor_key") or "Unknown").upper(),
        thumbnail=info.get("thumbnail"),
        video_id=info.get("id"),
    )


def track_embed(track: Track, title: str) -> discord.Embed:
    """Compact card shown when a song is added to the queue."""
    embed = discord.Embed(
        title=track.title[:250],
        url=track.webpage_url,
        description=f"🎤 **{track.artist[:80]}** · {track.requester_mention or track.requester}",
        color=discord.Color(COLORS["lavender"]),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=title)
    embed.add_field(name="⏱️ Length", value=f"`{fmt_duration(track.duration)}`", inline=True)
    source_label = f"{track.origin} → {track.platform.title()}" if track.origin else track.platform.title()
    embed.add_field(name="📡 Source", value=f"`{source_label}`", inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.set_footer(text=PLAYER_NAME)
    return embed


def fmt_clock(seconds) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def current_position(state: "GuildState", track: Track) -> float:
    pos = state.position_anchor
    if state.anchor_time is not None:
        pos += time.monotonic() - state.anchor_time
    if track.duration:
        pos = min(pos, float(track.duration))
    return max(0.0, pos)


def mark_paused(state: "GuildState"):
    if state.anchor_time is not None:
        state.position_anchor += time.monotonic() - state.anchor_time
        state.anchor_time = None


def mark_resumed(state: "GuildState"):
    if state.anchor_time is None:
        state.anchor_time = time.monotonic()


def progress_bar(pos: float, total, width: int = 13) -> str:
    if not total:
        return "🔘" + "▬" * (width - 1)
    idx = min(int(max(0.0, min(1.0, pos / total)) * width), width - 1)
    return "▬" * idx + "🔘" + "▬" * (width - 1 - idx)


def now_playing_embed(track: Track, state: "GuildState") -> discord.Embed:
    pos = current_position(state, track)
    total = fmt_clock(track.duration) if track.duration else "LIVE"
    who = track.requester_mention or track.requester
    embed = discord.Embed(
        title=track.title[:250],
        url=track.webpage_url,
        description=(
            f"🎤 **{track.artist[:80]}** · {who}\n\n"
            f"`{fmt_clock(pos)}` {progress_bar(pos, track.duration)} `{total}`"
        ),
        color=discord.Color(COLORS["pink"]),
    )
    paused = state.anchor_time is None
    embed.set_author(name=f"{PLAYER_EMOJI} {PLAYER_NAME}" + ("  ·  ⏸️ Paused" if paused else ""))
    n = len(state.queue)
    embed.add_field(name="🔊 Volume", value=f"`{int(state.volume * 100)}%`", inline=True)
    embed.add_field(name="📋 Queue", value=f"`{n} track{'s' if n != 1 else ''}`", inline=True)
    embed.add_field(name="🔁 Loop", value=f"`{'NONE' if state.loop == 'off' else state.loop.upper()}`", inline=True)
    if track.thumbnail:
        embed.set_image(url=track.thumbnail)
    embed.set_footer(
        text=f"{PLAYER_NAME}  •  👍 {len(track.likes)}  👎 {len(track.dislikes)}  •  Autoplay {'ON' if state.autoplay else 'OFF'}"
    )
    return embed


def queue_embed(state: "GuildState") -> discord.Embed:
    embed = discord.Embed(title="📜 Queue", color=discord.Color(COLORS["sky"]))
    embed.set_author(name=f"{PLAYER_EMOJI} {PLAYER_NAME}")
    if not state.current and not state.queue:
        embed.description = "The queue is empty.\nAdd a song with `/play`."
        return embed
    if state.current:
        c = state.current
        embed.add_field(
            name="🎶 Now playing",
            value=f"[{c.title[:100]}]({c.webpage_url})\n`{fmt_duration(c.duration)}` · {c.requester_mention or c.requester}",
            inline=False,
        )
    if state.queue:
        lines = [
            f"`{i}.` **{t.title[:55]}** · {t.artist[:25]} `{fmt_duration(t.duration)}`"
            for i, t in enumerate(list(state.queue)[:10], 1)
        ]
        if len(state.queue) > 10:
            lines.append(f"*...and {len(state.queue) - 10} more*")
        embed.description = "**Up next**\n" + "\n".join(lines)
    else:
        embed.description = "Nothing queued after this song."
    total = sum(t.duration or 0 for t in state.queue)
    n = len(state.queue)
    embed.set_footer(
        text=f"{n} track{'s' if n != 1 else ''} up next  •  {fmt_clock(total)} total  •  "
             f"Loop: {state.loop}  •  Volume: {int(state.volume * 100)}%"
    )
    return embed


def apply_volume(vc, state: "GuildState"):
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume  # applies instantly to the current song


class PlayerView(discord.ui.View):
    """The button panel under the Now Playing message."""

    def __init__(self, state: "GuildState"):
        super().__init__(timeout=None)
        self.state = state
        self.sync_buttons()

    def sync_buttons(self):
        paused = self.state.anchor_time is None
        self.pause_btn.emoji = "▶️" if paused else "⏸️"
        self.loop_btn.style = discord.ButtonStyle.success if self.state.loop != "off" else discord.ButtonStyle.secondary
        self.autoplay_btn.style = discord.ButtonStyle.success if self.state.autoplay else discord.ButtonStyle.secondary

    async def guard(self, interaction: discord.Interaction):
        """Controls only work for people in the bot's voice channel."""
        vc = interaction.guild.voice_client if interaction.guild else None
        user = interaction.user
        if vc is None or not isinstance(user, discord.Member) or not user.voice or user.voice.channel != vc.channel:
            await ireply(interaction, "Join my voice channel to use the controls.")
            return None
        return vc

    async def update(self, interaction: discord.Interaction):
        self.sync_buttons()
        if self.state.current:
            await interaction.response.edit_message(embed=now_playing_embed(self.state.current, self.state), view=self)
        else:
            await interaction.response.defer()

    # ---- row 1: previous, pause/resume, next, shuffle, loop
    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self.guard(interaction)
        if not vc:
            return
        state = self.state
        if not (vc.is_playing() or vc.is_paused()):
            return await ireply(interaction, "Nothing is playing.")
        if not state.history:
            return await ireply(interaction, "There's no previous song.")
        prev = state.history.pop()
        if state.current:
            state.queue.appendleft(state.current)
        state.queue.appendleft(prev)
        state.going_back = True
        await interaction.response.defer()
        vc.stop()

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self.guard(interaction)
        if not vc:
            return
        if vc.is_paused():
            vc.resume()
            mark_resumed(self.state)
        elif vc.is_playing():
            vc.pause()
            mark_paused(self.state)
        else:
            return await ireply(interaction, "Nothing is playing.")
        await self.update(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self.guard(interaction)
        if not vc:
            return
        if not (vc.is_playing() or vc.is_paused()):
            return await ireply(interaction, "Nothing is playing.")
        self.state.skip_requested = True
        await interaction.response.defer()
        vc.stop()

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        if len(self.state.queue) < 2:
            return await ireply(interaction, "Need at least 2 songs in the queue to shuffle.")
        items = list(self.state.queue)
        random.shuffle(items)
        self.state.queue = deque(items)
        await self.update(interaction)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        order = ["off", "track", "queue"]
        self.state.loop = order[(order.index(self.state.loop) + 1) % 3]
        await self.update(interaction)

    # ---- row 2: volume down/up, queue, autoplay, stop
    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self.guard(interaction)
        if not vc:
            return
        self.state.volume = max(0.0, round(self.state.volume - 0.1, 2))
        apply_volume(vc, self.state)
        await self.update(interaction)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self.guard(interaction)
        if not vc:
            return
        self.state.volume = min(1.0, round(self.state.volume + 0.1, 2))
        apply_volume(vc, self.state)
        await self.update(interaction)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=queue_embed(self.state), ephemeral=True)

    @discord.ui.button(emoji="✨", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        self.state.autoplay = not self.state.autoplay
        await self.update(interaction)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = await self.guard(interaction)
        if not vc:
            return
        self.state.queue.clear()
        self.state.current = None  # so loop doesn't restart it
        await interaction.response.defer()
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await close_player(self.state)

    # ---- row 3: rewind, forward, like, dislike
    async def seek(self, interaction: discord.Interaction, delta: int):
        vc = await self.guard(interaction)
        if not vc:
            return
        state, track = self.state, self.state.current
        if not track or not (vc.is_playing() or vc.is_paused()):
            return await ireply(interaction, "Nothing is playing.")
        if not track.duration:
            return await ireply(interaction, "I can't seek in this stream.")
        new_pos = max(0, min(track.duration - 2, current_position(state, track) + delta))
        state.queue.appendleft(track)  # restart the same song from the new position
        state.start_offset = int(new_pos)
        state.going_back = True
        state.position_anchor = float(new_pos)
        state.anchor_time = time.monotonic()
        await self.update(interaction)
        vc.stop()

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, row=2)
    async def rewind_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.seek(interaction, -10)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, row=2)
    async def forward_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.seek(interaction, 10)

    @discord.ui.button(emoji="👍", style=discord.ButtonStyle.secondary, row=2)
    async def like_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        track, uid = self.state.current, interaction.user.id
        if track:
            if uid in track.likes:
                track.likes.discard(uid)
            else:
                track.likes.add(uid)
                track.dislikes.discard(uid)
        await self.update(interaction)

    @discord.ui.button(emoji="👎", style=discord.ButtonStyle.secondary, row=2)
    async def dislike_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        track, uid = self.state.current, interaction.user.id
        if track:
            if uid in track.dislikes:
                track.dislikes.discard(uid)
            else:
                track.dislikes.add(uid)
                track.likes.discard(uid)
        await self.update(interaction)


async def delete_player(state: "GuildState"):
    if state.player_view:
        state.player_view.stop()
        state.player_view = None
    if state.player_message:
        try:
            await state.player_message.delete()
        except discord.HTTPException:
            pass
        state.player_message = None


async def close_player(state: "GuildState"):
    """Remove the buttons when playback is over (the message itself stays)."""
    if state.player_view:
        state.player_view.stop()
        state.player_view = None
    if state.player_message:
        try:
            await state.player_message.edit(view=None)
        except discord.HTTPException:
            pass
        state.player_message = None


async def refresh_player(state: "GuildState"):
    """Re-draw the player message after a slash/prefix command changed something."""
    if not (state.player_message and state.current):
        return
    if state.player_view:
        state.player_view.stop()
    view = PlayerView(state)
    state.player_view = view
    try:
        await state.player_message.edit(embed=now_playing_embed(state.current, state), view=view)
    except discord.NotFound:
        state.player_message = None
    except discord.HTTPException:
        pass


async def post_player(state: "GuildState", track: Track, send):
    """Send a fresh player message with `send` (channel.send or ctx.send), replacing the old one."""
    await delete_player(state)
    view = PlayerView(state)
    msg = await send(embed=now_playing_embed(track, state), view=view)
    state.player_view = view
    # Use a partial message so later edits keep working after interaction tokens expire
    channel = state.notify_channel
    state.player_message = channel.get_partial_message(msg.id) if channel else msg


async def announce_track(state: "GuildState", track: Track):
    if not state.notify_channel:
        return
    try:
        await post_player(state, track, state.notify_channel.send)
    except discord.HTTPException:
        pass


async def autoplay_next(guild: discord.Guild, finished: Track) -> bool:
    """When the queue runs dry and Autoplay is on, queue another song by the same artist."""
    state = get_state(guild.id)
    if not state.autoplay:
        return False
    source = "youtube" if "youtube" in finished.platform.lower() else "soundcloud"
    seen = {t.video_id for t in state.history if t.video_id}
    if finished.video_id:
        seen.add(finished.video_id)
    try:
        info = await extract(build_query(f"{finished.artist} songs", source), exclude=seen)
    except Exception as e:
        print(f"Autoplay found nothing: {e}")
        return False
    track = track_from_info(info, bot.user)
    track.requester = "Autoplay"
    track.requester_mention = "✨ Autoplay"
    state.queue.append(track)
    return True


background_tasks: set = set()


def spawn(coro):
    """Run a coroutine in the background without blocking playback."""
    task = asyncio.get_running_loop().create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def set_channel_status(channel: discord.VoiceChannel, text: str | None):
    try:
        await channel.edit(status=text or None)
    except TypeError:  # older discord.py without the status option: call the endpoint directly
        from discord.http import Route
        await bot.http.request(
            Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel.id),
            json={"status": text or None},
        )


async def update_voice_status(guild: discord.Guild, text: str | None):
    """Set (or clear, with None) the status line shown under the voice channel's name. Never raises."""
    vc = guild.voice_client
    if not vc or not isinstance(vc.channel, discord.VoiceChannel):
        return
    state = get_state(guild.id)
    text = (text or "")[:500]
    if state.last_status == text:
        return
    try:
        try:
            await set_channel_status(vc.channel, text)
        except discord.HTTPException as e:
            if isinstance(e, discord.Forbidden) or STATUS_EMOJI not in text:
                raise
            # Discord didn't accept the custom emoji: retry with a plain one
            text = text.replace(STATUS_EMOJI, "🎵")
            await set_channel_status(vc.channel, text)
        state.last_status = text
    except discord.Forbidden:
        if not state.status_warned:
            state.status_warned = True
            await notify(state, "I need the **Set Voice Channel Status** permission to show the current song in the channel status.")
    except Exception as e:
        print(f"Could not update voice channel status: {e!r}")


async def notify(state: GuildState, text: str):
    if state.notify_channel:
        try:
            await state.notify_channel.send(embed=msg_embed(text[:1900], "error"))
        except discord.HTTPException:
            pass


async def play_next(guild: discord.Guild, announce: bool = False):
    state = get_state(guild.id)

    finished = state.current
    going_back = state.going_back  # set by previous/seek buttons: replay instead of finishing
    state.going_back = False
    if finished and not going_back:
        repeating = state.loop == "track" and not state.skip_requested
        if not repeating:
            state.history.append(finished)
        if repeating:
            state.queue.appendleft(finished)
        elif state.loop == "queue":
            state.queue.append(finished)
    state.skip_requested = False

    vc = guild.voice_client
    if vc and finished and not going_back and not state.queue:
        await autoplay_next(guild, finished)

    if not vc or not state.queue:
        state.current = None
        if vc:
            spawn(update_voice_status(guild, None))
        await close_player(state)
        return

    track = state.queue.popleft()
    state.current = track
    if track is finished:  # same song again (loop or seek): don't post a new player message
        announce = False
    offset = state.start_offset
    state.start_offset = 0

    ffmpeg_err = tempfile.TemporaryFile()  # lets us show ffmpeg's own error message
    ytdlp_err = None
    ytdlp_proc = None
    try:
        if not track.resolved:
            await resolve_track(track)
        if "youtube" in track.platform.lower():
            # YouTube rejects ffmpeg opening the stream URL directly (403), so let yt-dlp
            # download the audio and pipe it into ffmpeg instead.
            ytdlp_err = tempfile.TemporaryFile()
            cmd = [sys.executable, "-m", "yt_dlp", "-f", "bestaudio[ext=webm]/bestaudio/best",
                   "--no-playlist", "-q", "-o", "-"]
            if YT_PLAYER_CLIENT:
                client = YT_PLAYER_CLIENT
                use_cookies = bool(YTDL_OPTS.get("cookiefile")) and client not in NO_COOKIE_CLIENTS
            else:
                client, use_cookies = YT_ATTEMPTS[min(track.attempt, len(YT_ATTEMPTS) - 1)]
            if use_cookies:
                cmd += ["--cookies", YTDL_OPTS["cookiefile"]]
            if client:
                cmd += ["--extractor-args", f"youtube:player_client={client}"]
            print(f"yt-dlp client: {client or 'default'} (cookies: {'yes' if use_cookies else 'no'})")
            cmd.append(track.play_url or track.webpage_url)
            ytdlp_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ytdlp_err)
            options = FFMPEG_OPTS["options"] + (f" -ss {offset}" if offset else "")
            source = discord.FFmpegPCMAudio(ytdlp_proc.stdout, pipe=True, options=options, stderr=ffmpeg_err)
        else:
            # Other sites (e.g. SoundCloud): ffmpeg can read the stream URL directly.
            # Re-resolve right before playing; stream URLs expire.
            info = await extract(track.play_url or track.webpage_url)
            headers = info.get("http_headers") or {}
            header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            before = FFMPEG_OPTS["before_options"]
            if header_str:
                before = f"-headers {shlex.quote(header_str)} " + before
            if offset:
                before = f"-ss {offset} " + before
            source = discord.FFmpegPCMAudio(
                info["url"], before_options=before, options=FFMPEG_OPTS["options"], stderr=ffmpeg_err
            )
        source = discord.PCMVolumeTransformer(source, volume=state.volume)
    except Exception as e:
        print(f"Failed to play {track.title}: {e!r}")
        if ytdlp_proc and ytdlp_proc.poll() is None:
            ytdlp_proc.kill()
        state.current = None  # don't loop a broken track
        await notify(state, f"Couldn't play **{track.title}**: `{e}`")
        return await play_next(guild, announce=announce)

    started = time.monotonic()
    print(f"Playing: {track.title} [{track.platform}] length={fmt_duration(track.duration)} from {offset}s")

    def read_log(f) -> str:
        try:
            f.seek(0)
            text = f.read().decode("utf-8", "replace").strip()[-700:]
            f.close()
            return text
        except Exception:
            return ""

    def after(err):
        elapsed = time.monotonic() - started
        print(f"Playback finished after {elapsed:.1f}s (error: {err})")
        if ytdlp_proc and ytdlp_proc.poll() is None:
            ytdlp_proc.kill()
        details = ""
        bot_check = False
        logs = {}
        for label, f in (("yt-dlp", ytdlp_err), ("ffmpeg", ffmpeg_err)):
            text = read_log(f) if f else ""
            logs[label] = text
            if text:
                print(f"{label} said:\n{text}")
                details += f"\n{label} said:\n```{text[-400:]}```"
                bot_check = bot_check or "not a bot" in text
        ytdlp_text = logs.get("yt-dlp", "").lower()
        forbidden = any(m in ytdlp_text for m in ("403", "requested format is not available", "page needs to be reloaded"))
        still_ours = state.current is track and not state.skip_requested and not state.going_back
        if forbidden and still_ours and not YT_PLAYER_CLIENT and track.attempt < len(YT_ATTEMPTS) - 1:
            # YouTube refused the audio (403): quietly try again with a different YouTube client,
            # picking up roughly where the song left off.
            track.attempt += 1
            resume_at = int(offset + elapsed)
            next_client, next_cookies = YT_ATTEMPTS[track.attempt]
            print(f"YouTube refused the audio; retrying from {resume_at}s with client '{next_client or 'default'}', cookies {'on' if next_cookies else 'off'}")

            async def retry():
                state.queue.appendleft(track)
                state.start_offset = resume_at
                state.going_back = True  # replay the same song instead of finishing it
                await play_next(guild)

            asyncio.run_coroutine_threadsafe(retry(), bot.loop)
            return
        if forbidden and not bot_check:
            details = (
                "\n\n**YouTube wouldn't hand over the audio** (it answered 403 or offered no usable format), even after trying other methods. "
                "Restarting me updates yt-dlp, which often fixes this, and fresh cookies can help too. "
                "Meanwhile, `sc:` searches still work, like `/play sc: song name`."
            )
        if bot_check:
            details = (
                "\n\n**YouTube is asking me to prove I'm not a bot.** This usually clears up after a while. "
                "To fix it for good, put a `cookies.txt` file in my folder. "
                "Meanwhile, `sc:` searches still work, like `/play sc: song name`."
            )
        if err:
            asyncio.run_coroutine_threadsafe(notify(state, f"Playback error: `{err}`"), bot.loop)
        elif (still_ours and track.duration and (forbidden or elapsed < min(15, track.duration * 0.5))):
            asyncio.run_coroutine_threadsafe(
                notify(state, f"**{track.title}** stopped after only {elapsed:.0f}s (it should last "
                              f"{fmt_duration(track.duration)}), so the audio stream was cut off." + details),
                bot.loop,
            )
        asyncio.run_coroutine_threadsafe(play_next(guild, announce=True), bot.loop)

    vc.play(source, after=after)
    state.position_anchor = float(offset)
    state.anchor_time = time.monotonic()
    spawn(update_voice_status(guild, f"{STATUS_EMOJI} Vibing to {track.title}"))
    if announce:
        await announce_track(state, track)


# ------------------------------------------------------- spotify & playlists

MAX_PLAYLIST = 100  # most tracks added from one playlist link
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z\-]+/)?(track|album|playlist)/([A-Za-z0-9]{22})", re.I)
SPOTIFY_URI_RE = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$", re.I)
SPOTIFY_SHORT_RE = re.compile(r"^https?://(?:spotify\.link|spoti\.fi|link\.tospotify\.com)/", re.I)
SPOTIFY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def dig(obj, *path):
    """Safely read obj[a][b][c]..., returning None if anything is missing."""
    for key in path:
        try:
            obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return None
    return obj


async def parse_spotify_link(query: str):
    """Return (kind, id) for a Spotify track/album/playlist link, or None."""
    q = query.strip()
    if SPOTIFY_SHORT_RE.match(q):  # spotify.link short URLs redirect to the real page
        try:
            async with aiohttp.ClientSession(headers=SPOTIFY_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as sess:
                async with sess.get(q, allow_redirects=True) as r:
                    q = str(r.url)
        except Exception:
            return None
    m = SPOTIFY_RE.search(q) or SPOTIFY_URI_RE.match(q)
    return (m.group(1).lower(), m.group(2)) if m else None


def find_spotify_entity(data):
    """Find the part of the page data that describes the track, album or playlist."""
    entity = dig(data, "props", "pageProps", "state", "data", "entity")
    if isinstance(entity, dict):
        return entity
    stack = [data]  # layout changed? search the whole structure
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(cur.get("trackList"), list) or ("artists" in cur and ("name" in cur or "title" in cur)):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def spotify_track_info(t: dict) -> dict:
    title = (t.get("title") or t.get("name") or "").strip()
    artist = t.get("subtitle")
    if not artist and isinstance(t.get("artists"), list):
        artist = ", ".join(a.get("name", "") for a in t["artists"] if isinstance(a, dict))
    artist = (artist or "Unknown").replace("\xa0", " ").strip()
    dur = t.get("duration")
    if isinstance(dur, dict):
        dur = dur.get("totalMilliseconds")
    dur = (int(dur / 1000) if dur > 1000 else int(dur)) if isinstance(dur, (int, float)) else None
    uri = t.get("uri") or ""
    return {"title": title, "artist": artist, "duration": dur, "id": uri.split(":")[-1] if uri.startswith("spotify:track:") else None}


def parse_spotify_embed(page: str, kind: str, sid: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if m:
        try:
            entity = find_spotify_entity(json.loads(m.group(1)))
        except json.JSONDecodeError:
            entity = None
        if entity:
            raw = entity.get("trackList")
            items = raw if isinstance(raw, list) else [entity]
            tracks = [spotify_track_info(t) for t in items if isinstance(t, dict)]
            tracks = [t for t in tracks if t["title"]]
            if tracks:
                return {
                    "kind": kind,
                    "name": entity.get("name") or entity.get("title") or kind.title(),
                    "image": dig(entity, "coverArt", "sources", 0, "url") or dig(entity, "visualIdentity", "image", 0, "url"),
                    "tracks": tracks,
                }
    # Fallback for single tracks: the page's Open Graph tags
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    desc = re.search(r'<meta property="og:description" content="([^"]*)"', page)
    image = re.search(r'<meta property="og:image" content="([^"]*)"', page)
    if kind == "track" and title:
        artist = html_lib.unescape(desc.group(1)).split("·")[0].strip() if desc else "Unknown"
        return {
            "kind": kind,
            "name": html_lib.unescape(title.group(1)),
            "image": image.group(1) if image else None,
            "tracks": [{"title": html_lib.unescape(title.group(1)), "artist": artist or "Unknown", "duration": None, "id": sid}],
        }
    raise ValueError("I couldn't read that Spotify link. It may be private, or Spotify changed its page.")


async def load_spotify(kind: str, sid: str, user: discord.abc.User):
    """Read a Spotify link and turn it into queue entries that are matched to YouTube when played."""
    async with aiohttp.ClientSession(headers=SPOTIFY_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as sess:
        async with sess.get(f"https://open.spotify.com/embed/{kind}/{sid}") as r:
            if r.status != 200:
                raise ValueError(f"Spotify answered with HTTP {r.status}.")
            page = await r.text()
    data = parse_spotify_embed(page, kind, sid)
    url = f"https://open.spotify.com/{kind}/{sid}"
    tracks = [
        Track(
            title=t["title"],
            webpage_url=f"https://open.spotify.com/track/{t['id']}" if t["id"] else url,
            requester=user.display_name,
            requester_mention=f"{user.mention} ({user.name})",
            artist=t["artist"],
            duration=t["duration"],
            platform="YOUTUBE",
            thumbnail=data["image"] if kind == "track" else None,
            origin="Spotify",
            search_query=f"{t['title']} {t['artist']}",
            resolved=False,
        )
        for t in data["tracks"][:MAX_PLAYLIST]
    ]
    meta = {"kind": kind, "name": data["name"], "url": url, "image": data["image"], "origin": "Spotify → YouTube"}
    return meta, tracks


def is_playlist_url(q: str) -> bool:
    u = q.lower()
    return u.startswith(("http://", "https://")) and ("youtube.com/playlist" in u or ("soundcloud.com/" in u and "/sets/" in u))


async def load_playlist(url: str, user: discord.abc.User):
    """Queue every track of a YouTube or SoundCloud playlist link."""
    loop = asyncio.get_running_loop()

    def run():
        opts = {**YTDL_OPTS, "noplaylist": False, "extract_flat": "in_playlist", "playlistend": MAX_PLAYLIST}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await loop.run_in_executor(None, run)
    entries = [e for e in (info.get("entries") or []) if e and e.get("title") not in ("[Private video]", "[Deleted video]")]
    default_platform = (info.get("extractor_key") or "Unknown").upper()
    tracks = []
    for e in entries[:MAX_PLAYLIST]:
        entry_url = e.get("url") or e.get("webpage_url")
        if not entry_url:
            continue
        platform = (e.get("ie_key") or default_platform).upper()
        if "SOUNDCLOUD" in platform:
            platform = "SOUNDCLOUD"
        vid = e.get("id")
        thumb = dig(e, "thumbnails", -1, "url")
        if not thumb and platform == "YOUTUBE" and vid:
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        tracks.append(Track(
            title=e.get("title") or "Unknown title",
            webpage_url=entry_url,
            requester=user.display_name,
            requester_mention=f"{user.mention} ({user.name})",
            artist=e.get("uploader") or e.get("channel") or "Unknown",
            duration=e.get("duration"),
            platform=platform,
            thumbnail=thumb,
            video_id=vid,
            play_url=entry_url,
        ))
    if not tracks:
        raise ValueError("That playlist is empty or private.")
    meta = {
        "kind": "playlist",
        "name": info.get("title") or "Playlist",
        "url": url,
        "image": tracks[0].thumbnail,
        "origin": tracks[0].platform.title(),
    }
    return meta, tracks


async def resolve_track(track: Track):
    """Spotify entries are matched to a YouTube video just before they play."""
    if track.search_query:
        try:
            info = await extract(build_query(track.search_query, "youtube"))
        except Exception as e:
            if "not a bot" not in str(e):
                raise
            print("YouTube blocked this search; falling back to SoundCloud")
            info = await extract(build_query(track.search_query, "soundcloud"))
        track.play_url = info.get("webpage_url") or info["url"]
        track.platform = (info.get("extractor_key") or "YouTube").upper()
        track.duration = info.get("duration") or track.duration
        track.thumbnail = track.thumbnail or info.get("thumbnail")
        track.video_id = info.get("id")
    track.resolved = True


def playlist_embed(meta: dict, tracks: list, position: int | None) -> discord.Embed:
    n = len(tracks)
    who = tracks[0].requester_mention or tracks[0].requester
    preview = "\n".join(f"`{i}.` **{t.title[:50]}** · {t.artist[:25]}" for i, t in enumerate(tracks[:5], 1))
    if n > 5:
        preview += f"\n*...and {n - 5} more*"
    embed = discord.Embed(
        title=meta["name"][:250],
        url=meta["url"],
        description=f"📀 **{n} track{'s' if n != 1 else ''}** added by {who}\n\n{preview}",
        color=discord.Color(COLORS["apricot"]),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=f"✅  Added {meta['kind'].title()} to Queue" + (f"  ·  from position {position}" if position else ""))
    total = sum(t.duration or 0 for t in tracks)
    embed.add_field(name="⏱️ Total length", value=f"`{fmt_clock(total)}`" if total else "`Unknown`", inline=True)
    embed.add_field(name="📡 Source", value=f"`{meta['origin']}`", inline=True)
    if meta.get("image"):
        embed.set_thumbnail(url=meta["image"])
    note = f"  •  Limited to the first {MAX_PLAYLIST} tracks" if n >= MAX_PLAYLIST else ""
    embed.set_footer(text=f"{PLAYER_NAME}{note}")
    return embed


# ---------------------------------------------------------------- playback


@bot.hybrid_command(name="play", aliases=["p"], description="Play a song, Spotify link or playlist (YouTube by default)")
@app_commands.describe(query="Song name, or a YouTube/SoundCloud/Spotify link or playlist. Start with 'sc:' to search SoundCloud")
async def play(ctx: commands.Context, *, query: str):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await reply(ctx, "Join a voice channel first.", "error", ephemeral=True)

    await ctx.defer()
    channel = ctx.author.voice.channel
    guild = ctx.guild

    vc = guild.voice_client
    if vc is None:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    state = get_state(guild.id)
    state.notify_channel = ctx.channel
    query = query.strip()

    meta, tracks = None, []
    try:
        spotify = await parse_spotify_link(query)
        if spotify:
            meta, tracks = await load_spotify(spotify[0], spotify[1], ctx.author)
        elif "open.spotify.com" in query.lower():
            raise ValueError("I can play Spotify tracks, albums and playlists. This link is a different kind of page.")
        elif is_playlist_url(query):
            meta, tracks = await load_playlist(query, ctx.author)
        else:
            source, text = parse_source(query)
            try:
                info = await extract(build_query(text, source))
            except Exception as e:
                if source != "youtube" or "not a bot" not in str(e):
                    raise
                print("YouTube blocked this search; falling back to SoundCloud")
                info = await extract(build_query(text, "soundcloud"))
            tracks = [track_from_info(info, ctx.author)]
    except Exception as e:
        return await reply(ctx, f"Couldn't find that track.\n```{str(e)[:500]}```", "error")

    is_bulk = meta is not None and meta["kind"] != "track"
    was_busy = vc.is_playing() or vc.is_paused()
    first_position = len(state.queue) + 1
    state.queue.extend(tracks)

    if is_bulk:
        await ctx.send(embed=playlist_embed(meta, tracks, first_position if was_busy else None))
    elif was_busy:
        await ctx.send(embed=track_embed(tracks[0], f"✅  Added to Queue  ·  position {first_position}"))

    if not was_busy:
        await play_next(guild)
        if state.current:
            await post_player(state, state.current, ctx.send)
        else:
            await reply(ctx, "I couldn't start that track. See the message above for details.", "error")


@bot.hybrid_command(name="skip", aliases=["s"], description="Skip the current song")
async def skip(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        state = get_state(ctx.guild.id)
        skipped = state.current
        state.skip_requested = True  # skip ignores track-loop
        vc.stop()  # triggers after() -> play_next
        await reply(ctx, f"Skipped **{skipped.title[:100]}**" if skipped else "Skipped.", "success", color=COLORS["peach"])
    else:
        await reply(ctx, "Nothing is playing.", "error", ephemeral=True)


@bot.hybrid_command(name="pause", description="Pause playback")
async def pause(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        mark_paused(get_state(ctx.guild.id))
        await reply(ctx, "Playback paused.", "success", "⏸️  Paused", color=COLORS["lemon"])
        await refresh_player(get_state(ctx.guild.id))
    else:
        await reply(ctx, "Nothing is playing.", "error", ephemeral=True)


@bot.hybrid_command(name="resume", description="Resume playback")
async def resume(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        mark_resumed(get_state(ctx.guild.id))
        await reply(ctx, "Playback resumed.", "success", "▶️  Resumed", color=COLORS["sage"])
        await refresh_player(get_state(ctx.guild.id))
    else:
        await reply(ctx, "Nothing is paused.", "error", ephemeral=True)


@bot.hybrid_command(name="stop", description="Stop playback and clear the queue (stays in the channel)")
async def stop(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    state.queue.clear()
    state.current = None  # so loop doesn't restart it
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await reply(ctx, "Stopped playback and cleared the queue.", "success", "⏹️  Stopped", color=COLORS["blush"])


@bot.hybrid_command(name="leave", aliases=["disconnect"], description="Disconnect the bot from voice")
async def leave(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    state.queue.clear()
    state.current = None
    await close_player(state)
    was_247 = stay247.pop(str(ctx.guild.id), None) is not None
    if was_247:
        save_stay247()
    vc = ctx.voice_client
    if vc:
        await update_voice_status(ctx.guild, None)
        await vc.disconnect()
        await reply(ctx, "Left the voice channel." + ("\n24/7 mode is now **off**." if was_247 else ""), "success", "👋  Disconnected", color=COLORS["cream"])
    else:
        await reply(ctx, "I'm not in a voice channel.", "error", ephemeral=True)


# ------------------------------------------------------------------- queue


@bot.hybrid_command(name="queue", aliases=["q"], description="Show the queue")
async def queue_cmd(ctx: commands.Context):
    await ctx.send(embed=queue_embed(get_state(ctx.guild.id)))


@bot.hybrid_command(name="nowplaying", aliases=["np"], description="Show the current song")
async def nowplaying(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    if state.current:
        await ctx.send(embed=now_playing_embed(state.current, state))
    else:
        await reply(ctx, "Nothing is playing.", "error", ephemeral=True)


@bot.hybrid_command(name="shuffle", description="Shuffle the upcoming songs in the queue")
async def shuffle(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    if len(state.queue) < 2:
        return await reply(ctx, "Need at least 2 songs in the queue to shuffle.", "error", ephemeral=True)
    items = list(state.queue)
    random.shuffle(items)
    state.queue = deque(items)
    await reply(ctx, f"Shuffled **{len(items)}** songs.", "success", "🔀  Shuffled", color=COLORS["orchid"])
    await refresh_player(state)


def remove_track(state: GuildState, track: Track) -> bool:
    for i, t in enumerate(state.queue):
        if t is track:
            del state.queue[i]
            return True
    return False


class RemoveView(discord.ui.View):
    """Dropdown listing the queue so a song can be picked and removed."""

    def __init__(self, author_id: int, state: GuildState):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.state = state
        self.snapshot = list(state.queue)[:25]
        self.message = None
        options = [
            discord.SelectOption(
                label=f"{i}. {t.title}"[:100],
                description=f"{t.artist[:60]} · {fmt_duration(t.duration)}",
                value=str(i - 1),
            )
            for i, t in enumerate(self.snapshot, 1)
        ]
        select = discord.ui.Select(placeholder="Choose a song to remove...", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        track = self.snapshot[int(interaction.data["values"][0])]
        if remove_track(self.state, track):
            embed = msg_embed(f"Removed **{track.title[:100]}** from the queue.", "success", "🗑️  Removed", color=COLORS["wisteria"])
        else:
            embed = msg_embed("That song is no longer in the queue.", "warn")
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()
        await refresh_player(self.state)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await ireply(interaction, "This menu belongs to someone else. Run the remove command yourself.")
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content=None, embed=msg_embed("This menu timed out.", "warn"), view=None)
            except discord.HTTPException:
                pass


@bot.hybrid_command(name="remove", aliases=["rm"], description="Remove a song from the queue")
@app_commands.describe(position="Queue number to remove (leave empty to pick from a list)")
async def remove(ctx: commands.Context, position: Optional[int] = None):
    state = get_state(ctx.guild.id)
    if not state.queue:
        return await reply(ctx, "The queue is empty, so there's nothing to remove.", "error", ephemeral=True)

    if position is None:
        note = "\n*Showing the first 25. Use `/remove <number>` for the rest.*" if len(state.queue) > 25 else ""
        view = RemoveView(ctx.author.id, state)
        view.message = await ctx.send(
            embed=msg_embed("Pick the song you want to remove from the list below." + note, "info", "🗑️  Remove a song", color=COLORS["mist"]),
            view=view,
        )
        return

    if not 1 <= position <= len(state.queue):
        return await reply(ctx, f"Pick a number between 1 and {len(state.queue)}.", "error", ephemeral=True)
    track = state.queue[position - 1]
    del state.queue[position - 1]
    await reply(ctx, f"Removed **{track.title[:100]}** from the queue.", "success", "🗑️  Removed", color=COLORS["wisteria"])
    await refresh_player(state)


@remove.autocomplete("position")
async def remove_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    choices = []
    for i, t in enumerate(get_state(interaction.guild.id).queue, 1):
        label = f"{i}. {t.title}"[:100]
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=i))
        if len(choices) >= 25:
            break
    return choices


# ---------------------------------------------------------------- settings


@bot.hybrid_command(name="loop", description="Set loop mode: off, current track, or whole queue")
@app_commands.describe(mode="What to loop: off, track, or queue")
async def loop_cmd(ctx: commands.Context, mode: Literal["off", "track", "queue"]):
    get_state(ctx.guild.id).loop = mode
    labels = {"off": "Loop is **off**.", "track": "Looping the **current track**.", "queue": "Looping the **whole queue**."}
    await reply(ctx, labels[mode], "success", "🔁  Loop", color=COLORS["aqua"])
    await refresh_player(get_state(ctx.guild.id))


@bot.hybrid_command(name="volume", aliases=["vol"], description="Set the volume (0-100)")
@app_commands.describe(level="Volume from 0 to 100")
async def volume(ctx: commands.Context, level: commands.Range[int, 0, 100]):
    state = get_state(ctx.guild.id)
    state.volume = level / 100
    vc = ctx.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume  # applies instantly to the current song
    await reply(ctx, f"Volume set to **{level}%**.", "success", "🔊  Volume", color=COLORS["seafoam"])
    await refresh_player(state)


# -------------------------------------------------------------------- roles

ROLE_DECOR = "๑•.୨୧ \u200b"  # characters around the keyword, e.g. "๑•.୨ baddiez ୧•."


def role_core(name: str) -> str:
    """'๑•.୨ baddiez ୧•.' -> 'baddiez'"""
    return name.strip(ROLE_DECOR).strip().lower()


def find_roles(guild: discord.Guild, keyword: str) -> list[discord.Role]:
    kw = keyword.strip(ROLE_DECOR).strip().lower()
    if not kw:
        return []
    roles = [r for r in guild.roles if not r.is_default()]
    same = [r for r in roles if role_core(r.name) == kw]
    if same:
        return same
    return [r for r in roles if kw in r.name.lower()]


@bot.hybrid_command(name="role", aliases=["r"], description="Add a role to a member, or remove it if they already have it")
@app_commands.describe(member="Who to give the role to or take it from", role_name="Role name, e.g. baddiez")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def role_cmd(ctx: commands.Context, member: discord.Member, *, role_name: str):
    guild = ctx.guild
    matches = find_roles(guild, role_name)
    if not matches:
        return await reply(ctx, f"I couldn't find a role matching **{role_name[:50]}**.", "error", ephemeral=True)
    if len(matches) > 1:
        names = "\n".join(f"• {r.name}" for r in matches[:5])
        return await reply(ctx, f"Several roles match **{role_name[:50]}**. Be more specific:\n{names}", "error", ephemeral=True)

    role = matches[0]
    if role.managed:
        return await reply(ctx, f"{role.mention} is managed by an integration, so it can't be given manually.", "error", ephemeral=True)
    if role >= guild.me.top_role:
        return await reply(ctx, f"{role.mention} is above my highest role. Move my role higher in Server Settings → Roles.", "error", ephemeral=True)
    if ctx.author.id != guild.owner_id and role >= ctx.author.top_role:
        return await reply(ctx, f"You can't manage {role.mention} because it's at or above your highest role.", "error", ephemeral=True)

    had_role = role in member.roles
    try:
        if had_role:
            await member.remove_roles(role, reason=f"Removed by {ctx.author} via the bot")
        else:
            await member.add_roles(role, reason=f"Added by {ctx.author} via the bot")
    except discord.Forbidden:
        return await reply(ctx, "Discord wouldn't let me change that role. Check my permissions and role position.", "error", ephemeral=True)
    except discord.HTTPException as e:
        return await reply(ctx, f"Something went wrong changing the role: `{e}`", "error", ephemeral=True)

    embed = discord.Embed(
        title="➖  Role removed" if had_role else "➕  Role added",
        description=f"{role.mention} was {'removed from' if had_role else 'added to'} {member.mention}",
        color=role.color if role.color.value else discord.Color(COLORS["peach"] if had_role else COLORS["sage"]),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@role_cmd.autocomplete("role_name")
async def role_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    cur = current.strip().lower()
    top = interaction.guild.me.top_role
    choices = []
    for r in reversed(interaction.guild.roles):  # highest roles first
        core = role_core(r.name)
        if r.is_default() or r.managed or r >= top or not core:
            continue
        if cur in r.name.lower():
            choices.append(app_commands.Choice(name=r.name[:100], value=core[:100]))
        if len(choices) >= 25:
            break
    return choices


# ------------------------------------------------------------------ server


@bot.hybrid_command(name="setprefix", description="Change this server's command prefix (admins only)")
@app_commands.describe(prefix="New prefix: up to 5 characters, no spaces")
@commands.has_permissions(administrator=True)
@app_commands.default_permissions(administrator=True)
async def setprefix(ctx: commands.Context, prefix: str):
    if len(prefix) > 5 or any(c.isspace() for c in prefix) or "`" in prefix:
        return await reply(ctx, "A prefix must be 1-5 characters with no spaces or backticks.", "error", ephemeral=True)
    prefixes[str(ctx.guild.id)] = prefix
    save_prefixes()
    await reply(ctx, f"Prefix for this server is now `{prefix}`\nTry `{prefix}help`.", "success", "⚙️  Prefix updated", color=COLORS["mint"])


@bot.hybrid_command(name="247", description="Toggle 24/7 mode: the bot stays in the voice channel")
@commands.has_permissions(manage_guild=True)
@app_commands.default_permissions(manage_guild=True)
async def stay_247(ctx: commands.Context):
    key = str(ctx.guild.id)
    if key in stay247:
        del stay247[key]
        save_stay247()
        return await reply(ctx, f"I'll leave the voice channel after {IDLE_TIMEOUT // 60} minutes of inactivity.", "success", "🌙  24/7 mode off", color=COLORS["cloud"])

    vc = ctx.voice_client
    if vc is None:
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await reply(ctx, "Join a voice channel first, then run this again.", "error", ephemeral=True)
        await ctx.defer()
        vc = await ctx.author.voice.channel.connect()

    stay247[key] = vc.channel.id
    save_stay247()
    await reply(ctx, f"I'll stay in **{vc.channel.name}** and rejoin if I get disconnected.\nRun the command again to turn it off.", "success", "☀️  24/7 mode on", color=COLORS["butter"])


@bot.hybrid_command(name="prefix", description="Show this server's command prefix")
async def prefix_cmd(ctx: commands.Context):
    p = prefix_for(ctx.guild)
    await reply(ctx, f"The prefix here is `{p}`\nYou can also mention me instead, and slash commands always work.", "info", "⚙️  Prefix", color=COLORS["powder"])


# ------------------------------------------------------------------- about

INVITE_PERMISSIONS = 281475248376832  # view/send/embed/history, connect/speak, manage roles, set voice status


def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


SMALL_CAPS = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ")


def small_caps(text: str) -> str:
    """'created by' -> 'ᴄʀᴇᴀᴛᴇᴅ ʙʏ' (lower-case letters only)."""
    return text.translate(SMALL_CAPS)


def script(text: str) -> str:
    """Bold script lettering: 'Mannat Player' -> '𝓜𝓪𝓷𝓷𝓪𝓽 𝓟𝓵𝓪𝔂𝓮𝓻'."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D4D0 + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D4EA + ord(ch) - ord("a")))
        else:
            out.append(ch)
    return "".join(out)


class AboutView(discord.ui.View):
    def __init__(self, invite_url: str, prefix: str):
        super().__init__(timeout=300)
        self.prefix = prefix
        self.add_item(discord.ui.Button(label="Invite me", emoji="💌", style=discord.ButtonStyle.link, url=invite_url))

    @discord.ui.button(label="Commands", emoji="📖", style=discord.ButtonStyle.secondary)
    async def commands_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=help_embed("home", self.prefix), ephemeral=True)


@bot.hybrid_command(name="about", aliases=["info", "botinfo"], description="Learn about the bot")
async def about(ctx: commands.Context):
    me = bot.user
    p = prefix_for(ctx.guild)

    owner = None
    try:
        owner = (await bot.application_info()).owner
    except discord.HTTPException:
        pass

    embed = discord.Embed(
        description=(
            f"🎶 **{script('Your server’s personal DJ')}**\n"
            "Music from YouTube, SoundCloud & Spotify with one-tap controls."
        ),
        color=discord.Color(COLORS["rose"]),
    )
    embed.set_author(name=f"{PLAYER_EMOJI} {script(PLAYER_NAME)}", icon_url=me.display_avatar.url)
    embed.set_thumbnail(url=me.display_avatar.url)
    embed.add_field(name=f"👤 {small_caps('created by')}", value=owner.mention if owner else "Unknown", inline=True)
    embed.add_field(name=f"📅 {small_caps('created')}", value=discord.utils.format_dt(me.created_at, "D"), inline=True)
    embed.add_field(name=f"🌐 {small_caps('servers')}", value=f"`{len(bot.guilds)}`", inline=True)
    embed.set_footer(text=f"{small_caps('prefix')} {p}  •  {small_caps('up')} {fmt_uptime(time.time() - START_TIME)}")

    invite = discord.utils.oauth_url(
        me.id, permissions=discord.Permissions(INVITE_PERMISSIONS), scopes=("bot", "applications.commands")
    )
    await ctx.send(embed=embed, view=AboutView(invite, p))


# ------------------------------------------------------------------- extras

AR_MAX_PER_SERVER = 50


def fmt_away(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "less than a minute"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = [f"{n}{u}" for n, u in ((days, "d"), (hours, "h"), (minutes, "m")) if n]
    return " ".join(parts)


# ---- avatar

def avatar_links(asset: discord.Asset) -> str:
    formats = ["png", "jpg", "webp"] + (["gif"] if asset.is_animated() else [])
    return " · ".join(f"[{f.upper()}]({asset.replace(size=1024, format=f).url})" for f in formats)


@bot.hybrid_command(name="avatar", aliases=["av", "pfp"], description="Show someone's avatar")
@app_commands.describe(member="Whose avatar to show (default: you)")
async def avatar_cmd(ctx: commands.Context, member: Optional[discord.Member] = None):
    target = member
    ref = ctx.message.reference if ctx.interaction is None else None
    if target is None and ref:  # used as a reply: show the avatar of the person being replied to
        replied = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if replied is None and ref.message_id:
            try:
                replied = await ctx.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                replied = None
        if replied:
            target = replied.author
    target = target or ctx.author

    avatar = target.display_avatar
    embed = discord.Embed(
        title=f"{PLAYER_EMOJI} {target.display_name}’s Avatar",
        description=f"🖼️ {avatar_links(avatar)}",
        color=discord.Color(COLORS["petal"]),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_image(url=avatar.replace(size=1024).url)
    global_avatar = getattr(target, "avatar", None)
    if global_avatar and global_avatar.key != avatar.key:  # they have a server-specific avatar too
        embed.add_field(name="🌍 Global avatar", value=avatar_links(global_avatar), inline=False)
    embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open original", emoji="🔗", style=discord.ButtonStyle.link, url=avatar.replace(size=4096).url))
    await ctx.send(embed=embed, view=view)


# ---- afk

afk_notice_times: dict[tuple[int, int], float] = {}


@bot.hybrid_command(name="afk", aliases=["aft"], description="Mark yourself as AFK")
@app_commands.describe(reason="Why you're away (optional)")
async def afk_cmd(ctx: commands.Context, *, reason: str = "AFK"):
    reason = reason.strip()[:100] or "AFK"
    afk_data.setdefault(str(ctx.guild.id), {})[str(ctx.author.id)] = {"reason": reason, "since": time.time()}
    save_afk()
    await reply(ctx, f"{ctx.author.mention} is now AFK: **{reason}**\nI'll let people know if they mention you.",
                "success", "💤  AFK set", color=COLORS["honey"])


async def handle_afk(message: discord.Message, is_afk_command: bool):
    entries = afk_data.get(str(message.guild.id))
    if not entries:
        return

    uid = str(message.author.id)
    if uid in entries and not is_afk_command:  # they're talking again: welcome them back
        info = entries.pop(uid)
        save_afk()
        await message.reply(
            embed=msg_embed(
                f"Welcome back {message.author.mention}! I removed your AFK.\nYou were away for **{fmt_away(time.time() - info['since'])}**.",
                "success", "👋  Welcome back", color=COLORS["lagoon"],
            ),
            mention_author=False, delete_after=20,
        )

    notes, seen, now = [], set(), time.time()
    for member in message.mentions:  # tell people when someone they pinged is away
        if member.id in seen or member.id == message.author.id or member.bot:
            continue
        seen.add(member.id)
        info = entries.get(str(member.id))
        key = (message.channel.id, member.id)
        if not info or now - afk_notice_times.get(key, 0) < 30:
            continue
        afk_notice_times[key] = now
        notes.append(f"**{member.display_name}** is AFK: {info['reason']} · <t:{int(info['since'])}:R>")
        if len(notes) == 3:
            break
    if notes:
        await message.reply(embed=msg_embed("\n".join(notes), "info", "💤  AFK", color=COLORS["melon"]),
                            mention_author=False, delete_after=30)


# ---- auto-responders

ar_cooldowns: dict[tuple[int, str], float] = {}


async def handle_autoresponders(message: discord.Message):
    entries = autoresponders.get(str(message.guild.id))
    if not entries or not message.content:
        return
    text = message.content.lower()
    for key, entry in entries.items():
        if not re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text):  # the trigger as a whole word or phrase
            continue
        cooldown_key, now = (message.channel.id, key), time.time()
        if now - ar_cooldowns.get(cooldown_key, 0) < 5:
            return
        ar_cooldowns[cooldown_key] = now
        out = (entry["response"]
               .replace("{user}", message.author.mention)
               .replace("{username}", message.author.display_name)
               .replace("{server}", message.guild.name))
        await message.reply(out[:2000], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        return


@bot.hybrid_group(name="autoresponder", aliases=["ar"], description="Manage auto-responders (admins only)", invoke_without_command=True)
@commands.has_permissions(administrator=True)
@app_commands.default_permissions(administrator=True)
async def autoresponder(ctx: commands.Context):
    p = ctx.clean_prefix
    embed = msg_embed(
        f"`{p}autoresponder add <trigger> <response>`\nAdd one. Put multi-word triggers in quotes.\n\n"
        f"`{p}autoresponder remove <trigger>`\nRemove one.\n\n"
        f"`{p}autoresponder list`\nShow every auto-responder in this server.\n\n"
        "Use `{user}`, `{username}` or `{server}` inside a response.",
        "info", "🤖  Auto-responders", color=COLORS["taffy"],
    )
    await ctx.send(embed=embed)


@autoresponder.command(name="add", description="Add an auto-responder")
@app_commands.describe(trigger="Word or phrase to react to", response="What I reply with. {user}, {username} and {server} work")
@commands.has_permissions(administrator=True)
async def ar_add(ctx: commands.Context, trigger: str, *, response: str):
    trigger, response = trigger.strip(), response.strip()
    if not trigger or not response:
        return await reply(ctx, "I need both a trigger and a response.", "error", ephemeral=True)
    if len(trigger) > 50:
        return await reply(ctx, "Triggers can be at most 50 characters.", "error", ephemeral=True)
    if len(response) > 1000:
        return await reply(ctx, "Responses can be at most 1000 characters.", "error", ephemeral=True)

    entries = autoresponders.setdefault(str(ctx.guild.id), {})
    key = trigger.lower()
    existed = key in entries
    if not existed and len(entries) >= AR_MAX_PER_SERVER:
        return await reply(ctx, f"This server already has {AR_MAX_PER_SERVER} auto-responders. Remove one first.", "error", ephemeral=True)
    entries[key] = {"trigger": trigger, "response": response}
    save_autoresponders()

    embed = msg_embed(f"Auto-responder {'updated' if existed else 'added'}.", "success",
                      "🤖  Updated" if existed else "🤖  Added", color=COLORS["pistachio"])
    embed.add_field(name="🎯 Trigger", value=f"`{trigger}`", inline=True)
    embed.add_field(name="💬 Response", value=response[:500], inline=True)
    await ctx.send(embed=embed)


@autoresponder.command(name="remove", aliases=["delete"], description="Remove an auto-responder")
@app_commands.describe(trigger="The trigger to remove")
@commands.has_permissions(administrator=True)
async def ar_remove(ctx: commands.Context, *, trigger: str):
    entries = autoresponders.get(str(ctx.guild.id), {})
    entry = entries.pop(trigger.strip().lower(), None)
    if not entry:
        return await reply(ctx, f"I don't have an auto-responder for **{trigger.strip()[:50]}**. Check `{ctx.clean_prefix}autoresponder list`.",
                           "error", ephemeral=True)
    save_autoresponders()
    await reply(ctx, f"Removed the auto-responder for **{entry['trigger']}**.", "success", "🗑️  Removed", color=COLORS["rosewater"])


@ar_remove.autocomplete("trigger")
async def ar_remove_autocomplete(interaction: discord.Interaction, current: str):
    entries = autoresponders.get(str(interaction.guild.id), {}) if interaction.guild else {}
    cur = current.lower()
    return [app_commands.Choice(name=e["trigger"][:100], value=e["trigger"][:100]) for k, e in entries.items() if cur in k][:25]


@autoresponder.command(name="list", aliases=["all"], description="Show every auto-responder in this server")
@commands.has_permissions(administrator=True)
async def ar_list(ctx: commands.Context):
    entries = autoresponders.get(str(ctx.guild.id), {})
    if not entries:
        return await reply(ctx, f"No auto-responders yet. Add one with `{ctx.clean_prefix}autoresponder add`.", "info",
                           "🤖  Auto-responders", color=COLORS["ice"])
    lines = []
    for i, e in enumerate(entries.values(), 1):
        preview = " ".join(e["response"].split())
        lines.append(f"`{i}.` **{e['trigger'][:50]}** → {preview[:45]}{'…' if len(preview) > 45 else ''}")
    embed = discord.Embed(title="🤖  Auto-responders", description="\n".join(lines), color=discord.Color(COLORS["ice"]))
    embed.set_footer(text=f"{len(entries)} of {AR_MAX_PER_SERVER}  •  {PLAYER_NAME}")
    await ctx.send(embed=embed)


# -------------------------------------------------------------------- help

HELP_PAGES = {
    "home": (
        f"{PLAYER_NAME} Help",
        "Pick a category from the menu below to see its commands.\n\n"
        "**Quick start:** join a voice channel and use `/play` (or `{p}play`) with a song name or link.\n"
        "Every command works as a slash command and with the prefix `{p}`.",
        [
            ("Playback", "play, skip, pause, resume, stop, leave"),
            ("Queue", "queue, nowplaying, shuffle, remove"),
            ("Settings", "loop, volume"),
            ("Server", "prefix, setprefix, 247, role, about"),
            ("Extras", "avatar, afk, autoresponder"),
        ],
    ),
    "playback": (
        "Playback Commands",
        "Control what's playing.",
        [
            ("/play <song or link>", "Play a song or add it to the queue. Accepts names, links, **Spotify tracks/albums/playlists** and YouTube/SoundCloud playlists. Searches YouTube; start with `sc:` for SoundCloud. Alias: `{p}p`"),
            ("/skip", "Skip the current song. Alias: `{p}s`"),
            ("/pause", "Pause playback."),
            ("/resume", "Resume playback."),
            ("/stop", "Stop playing and clear the queue."),
            ("/leave", "Disconnect the bot from voice (also turns 24/7 mode off)."),
        ],
    ),
    "queue": (
        "Queue Commands",
        "See and manage what's coming up.",
        [
            ("/queue", "Show the current song and the next 10. Alias: `{p}q`"),
            ("/nowplaying", "Show the current song. Alias: `{p}np`"),
            ("/shuffle", "Randomize the order of upcoming songs."),
            ("/remove [number]", "Remove a song from the queue. Leave the number out to pick from a list. Alias: `{p}rm`"),
        ],
    ),
    "settings": (
        "Settings Commands",
        "Tweak how the bot plays.",
        [
            ("/loop <off|track|queue>", "Off, loop the current track, or loop the whole queue."),
            ("/volume <0-100>", "Change the volume. Applies right away. Alias: `{p}vol`"),
        ],
    ),
    "server": (
        "Server Commands",
        "Per-server settings.",
        [
            ("/about", "Learn about me: what I do, who made me, and my stats. Aliases: `{p}info`, `{p}botinfo`"),
            ("/prefix", "Show this server's prefix."),
            ("/setprefix <prefix>", "Change the prefix (administrators only). Mentioning the bot always works too."),
            ("/role <member> <name>", "Add a role to a member, or remove it if they already have it. Needs Manage Roles. Alias: `{p}r`"),
            ("/247", "Toggle 24/7 mode: I stay in the voice channel and rejoin if disconnected (needs Manage Server). Without it I leave after 5 idle minutes."),
        ],
    ),
}


HELP_PAGES["extras"] = (
    "Extra Commands",
    "Handy little extras.",
    [
        ("/avatar [member]", "Show someone's avatar. Reply to a message with `{p}av` to see that person's avatar. Aliases: `{p}av`, `{p}pfp`"),
        ("/afk [reason]", "Mark yourself as away. I'll tell people who mention you, and welcome you back when you talk again."),
        ("/autoresponder", "**Admins only.** `add <trigger> <response>`, `remove <trigger>` and `list` manage the replies I send when someone says a trigger. Alias: `{p}ar`"),
    ],
)


def help_embed(key: str, p: str) -> discord.Embed:
    title, desc, fields = HELP_PAGES[key]
    embed = discord.Embed(title=title, description=desc.format(p=p), color=discord.Color(COLORS["periwinkle"]))
    embed.set_author(name=f"{PLAYER_EMOJI} {PLAYER_NAME}")
    for name, value in fields:
        embed.add_field(name=name, value=value.format(p=p), inline=False)
    embed.set_footer(text=f"Prefix: {p}  |  Use the menu to switch categories")
    return embed


class HelpView(discord.ui.View):
    def __init__(self, author_id: int, p: str):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.p = p
        self.message: discord.Message | None = None

    @discord.ui.select(
        placeholder="Choose a category...",
        options=[
            discord.SelectOption(label="Home", value="home", emoji="🏠", description="Overview"),
            discord.SelectOption(label="Playback", value="playback", emoji="▶️", description="Play, skip, pause, stop"),
            discord.SelectOption(label="Queue", value="queue", emoji="📜", description="Queue, now playing, shuffle"),
            discord.SelectOption(label="Settings", value="settings", emoji="⚙️", description="Loop and volume"),
            discord.SelectOption(label="Server", value="server", emoji="🛠️", description="Prefix settings"),
            discord.SelectOption(label="Extras", value="extras", emoji="🎀", description="Avatar, AFK, auto-responders"),
        ],
    )
    async def select_category(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.edit_message(embed=help_embed(select.values[0], self.p), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.message.delete()
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await ireply(interaction, "This menu belongs to someone else. Run the help command to get your own.")
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.hybrid_command(name="help", description="Show all commands")
async def help_cmd(ctx: commands.Context):
    p = prefix_for(ctx.guild)
    view = HelpView(ctx.author.id, p)
    view.message = await ctx.send(embed=help_embed("home", p), view=view)


async def main():
    if os.getenv("PORT"):  # only on hosts that provide a PORT (e.g. Render)
        await start_web_server()  # up before login, so the host sees the service as alive
    async with bot:
        try:
            await bot.start(TOKEN)
        except discord.HTTPException as e:
            if e.status == 429:
                # Discord is rate limiting this IP. Wait instead of crash-looping, which makes it worse.
                print("Discord returned 429 (rate limited). Waiting 15 minutes before exiting/retrying...")
                await asyncio.sleep(900)
            raise


if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in your .env file.")

discord.utils.setup_logging()
asyncio.run(main())
