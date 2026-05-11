#!/usr/bin/env python3
"""Discord bot interface for the LoL MCP coach.

Commands:
  !setid <Name#Tag>       — Save your Riot ID (do this once)
  !coach <question>       — Start or continue a coaching conversation
  !reset                  — Clear conversation history and start fresh
  !stats [Name#Tag]       — Quick ranked stats embed
  !match [Name#Tag]       — Analyse most recent match
  !profile [Name#Tag]     — Summoner profile
  !help                   — Show this help
"""

import asyncio
import json
import logging
import os
import sys
import time

import anthropic
import discord
from discord.ext import commands

from analyzer import LoLAnalyzer
from cache import CacheManager
from config import Config
from riot_api import RiotAPIClient

# ---------------------------------------------------------------------------
# Logging — set VERBOSE=1 in .env or environment for debug output
# ---------------------------------------------------------------------------

VERBOSE = os.getenv("VERBOSE", "0") == "1"

logging.basicConfig(
    level=logging.DEBUG if VERBOSE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("lol-bot")

# Suppress noisy discord.py internals unless in verbose mode
if not VERBOSE:
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Bootstrap shared components
# ---------------------------------------------------------------------------

log.info("Starting LoL Coach Bot...")
config = Config()
cache = CacheManager(config.cache_db_path)
riot = RiotAPIClient(config.riot_api_key, cache, config)
analyzer = LoLAnalyzer(config.anthropic_api_key)
anthropic_client = anthropic.Anthropic(api_key=config.anthropic_api_key)
log.info("Components initialised (region=%s, routing=%s)", config.region, config.regional_routing)

# ---------------------------------------------------------------------------
# Per-user Riot ID storage (persisted in the same SQLite DB)
# ---------------------------------------------------------------------------

cache._conn.execute("""
    CREATE TABLE IF NOT EXISTS user_profiles (
        discord_id TEXT PRIMARY KEY,
        riot_id TEXT NOT NULL
    )
""")
cache._conn.commit()


def save_riot_id(discord_id: str, riot_id: str):
    cache._conn.execute(
        "INSERT OR REPLACE INTO user_profiles (discord_id, riot_id) VALUES (?, ?)",
        (discord_id, riot_id),
    )
    cache._conn.commit()


def load_riot_id(discord_id: str) -> str | None:
    row = cache._conn.execute(
        "SELECT riot_id FROM user_profiles WHERE discord_id = ?", (discord_id,)
    ).fetchone()
    return row[0] if row else None


def resolve_riot_id(discord_id: str, provided: str | None) -> str | None:
    """Return provided Riot ID if given, otherwise fall back to saved one."""
    return provided or load_riot_id(discord_id)


# ---------------------------------------------------------------------------
# Per-user conversation history (in-memory, keyed by channel+user)
# ---------------------------------------------------------------------------

CONVERSATION_TIMEOUT = 30 * 60  # 30 minutes of inactivity clears history
_histories: dict[tuple[int, int], dict] = {}


def get_history(channel_id: int, user_id: int) -> list:
    entry = _histories.get((channel_id, user_id))
    if entry and time.time() - entry["last_active"] < CONVERSATION_TIMEOUT:
        return list(entry["messages"])
    return []


def save_history(channel_id: int, user_id: int, messages: list):
    _histories[(channel_id, user_id)] = {
        "messages": messages,
        "last_active": time.time(),
    }


def clear_history(channel_id: int, user_id: int):
    _histories.pop((channel_id, user_id), None)


def has_active_conversation(channel_id: int, user_id: int) -> bool:
    entry = _histories.get((channel_id, user_id))
    return bool(entry and time.time() - entry["last_active"] < CONVERSATION_TIMEOUT)

# ---------------------------------------------------------------------------
# Tool specs mirroring the MCP server (used by the agentic !coach command)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_summoner",
        "description": "Get summoner profile by Riot ID (Name#Tag)",
        "input_schema": {
            "type": "object",
            "properties": {"riot_id": {"type": "string"}},
            "required": ["riot_id"],
        },
    },
    {
        "name": "get_ranked_stats",
        "description": "Get ranked stats for a summoner",
        "input_schema": {
            "type": "object",
            "properties": {"riot_id": {"type": "string"}},
            "required": ["riot_id"],
        },
    },
    {
        "name": "get_match_history",
        "description": "Get recent match IDs for a summoner",
        "input_schema": {
            "type": "object",
            "properties": {
                "riot_id": {"type": "string"},
                "count": {"type": "integer", "default": 5},
            },
            "required": ["riot_id"],
        },
    },
    {
        "name": "analyze_performance",
        "description": "AI coaching analysis of a player's recent matches",
        "input_schema": {
            "type": "object",
            "properties": {
                "riot_id": {"type": "string"},
                "count": {"type": "integer", "default": 10},
                "focus_area": {"type": "string", "default": "overall"},
            },
            "required": ["riot_id"],
        },
    },
    {
        "name": "get_champion_stats",
        "description": "Aggregate champion stats from recent matches",
        "input_schema": {
            "type": "object",
            "properties": {
                "riot_id": {"type": "string"},
                "count": {"type": "integer", "default": 20},
            },
            "required": ["riot_id"],
        },
    },
]


async def execute_tool(name: str, args: dict) -> str:
    log.debug("Tool call: %s(%s)", name, args)
    try:
        if name == "get_summoner":
            r = await riot.get_summoner(args["riot_id"])
        elif name == "get_ranked_stats":
            r = await riot.get_ranked_stats(args["riot_id"])
        elif name == "get_match_history":
            r = await riot.get_match_history(args["riot_id"], args.get("count", 5))
        elif name == "analyze_performance":
            matches = await riot.get_match_history(
                args["riot_id"], args.get("count", 10), include_details=True
            )
            ranked = await riot.get_ranked_stats(args["riot_id"])
            r = await asyncio.get_event_loop().run_in_executor(
                None,
                analyzer.analyze_performance,
                args["riot_id"],
                matches,
                ranked,
                args.get("focus_area", "overall"),
            )
            log.debug("Tool %s completed", name)
            return r if isinstance(r, str) else json.dumps(r, indent=2)
        elif name == "get_champion_stats":
            r = await riot.get_champion_stats(args["riot_id"], count=args.get("count", 20))
        else:
            log.warning("Unknown tool requested: %s", name)
            return f"Unknown tool: {name}"
        log.debug("Tool %s completed", name)
        return json.dumps(r, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.error("Tool %s failed: %s", name, exc, exc_info=VERBOSE)
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def coach_system(riot_id: str | None) -> str:
    base = (
        "You are an expert League of Legends coach. "
        "Use the available tools to fetch real player data and give specific, "
        "data-backed coaching advice. Always reference actual numbers."
    )
    if riot_id:
        base += f" The user's Riot ID is {riot_id} — use it automatically when fetching data unless they ask about someone else."
    return base


async def _send_long(ctx, text: str):
    """Send a message, splitting at 1900 chars to stay within Discord limits."""
    for i in range(0, len(text), 1900):
        await ctx.send(text[i : i + 1900])


@bot.event
async def on_ready():
    log.info("LoL Coach Bot online as %s (id=%s)", bot.user, bot.user.id)
    log.info("Serving %d guild(s)", len(bot.guilds))
    for guild in bot.guilds:
        log.info("  - %s (id=%s)", guild.name, guild.id)
    log.info("Message Content Intent enabled: %s", bot.intents.message_content)
    log.info("Command prefix: %s", bot.command_prefix)


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    log.debug(
        "Message in #%s from %s: %s",
        message.channel,
        message.author,
        message.content[:100],
    )

    # If it's a command, handle normally
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    # If the user has an active coach conversation, treat the message as a follow-up
    if has_active_conversation(message.channel.id, message.author.id):
        ctx = await bot.get_context(message)
        async with message.channel.typing():
            await run_coach(ctx, message.content)


@bot.event
async def on_command(ctx):
    log.info(
        "Command received: !%s from %s#%s in #%s",
        ctx.command,
        ctx.author.name,
        ctx.author.discriminator,
        ctx.channel,
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        log.debug("Unknown command: %s", ctx.message.content)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Check `!help` for usage.")
        log.warning("Missing argument in command from %s: %s", ctx.author, error)
        return
    log.error("Unhandled command error: %s", error, exc_info=VERBOSE)
    await ctx.send(f"Error: {error}")


@bot.command(name="help", aliases=["h"])
async def help_cmd(ctx):
    """Show all commands and example coach questions."""
    embed = discord.Embed(
        title="LoL Agentic Coach — Help",
        color=0xC89B3C,
    )

    embed.add_field(
        name="Setup",
        value=(
            "`!setid Name#Tag` — Save your Riot ID once so you never type it again"
        ),
        inline=False,
    )

    embed.add_field(
        name="Quick Commands",
        value=(
            "`!stats [Name#Tag]` — Ranked stats (Solo/Duo & Flex)\n"
            "`!match [Name#Tag]` — AI review of your most recent game\n"
            "`!profile [Name#Tag]` — Summoner level & info"
        ),
        inline=False,
    )

    embed.add_field(
        name="Coach — example questions",
        value=(
            "`!coach how was my last game?`\n"
            "`!coach what should I improve?`\n"
            "`!coach analyze my last 10 games`\n"
            "`!coach what are my best champions?`\n"
            "`!coach my CS is bad, help me improve`\n"
            "`!coach I keep dying too much, why?`\n"
            "`!coach give me tips for Zed into Yasuo mid`\n"
            "`!coach how is Faker#KR1 doing?`"
        ),
        inline=False,
    )

    embed.add_field(
        name="Conversation",
        value=(
            "After `!coach`, just reply naturally — no need to type `!coach` again.\n"
            "The bot remembers the last 30 minutes of chat.\n"
            "`!reset` — clear history and start fresh."
        ),
        inline=False,
    )

    embed.set_footer(text="Riot ID is optional in [ ] commands if you've used !setid")
    await ctx.send(embed=embed)


@bot.command(name="setid")
async def setid_cmd(ctx, riot_id: str):
    """Save your Riot ID so you don't have to type it every time."""
    if "#" not in riot_id:
        await ctx.send("Please use the format `Name#Tag` (e.g. `!setid Faker#KR1`)")
        return
    save_riot_id(str(ctx.author.id), riot_id)
    log.info("Saved Riot ID for %s: %s", ctx.author, riot_id)
    await ctx.send(f"Got it! I'll remember your Riot ID as **{riot_id}**. You can now use `!coach`, `!stats`, `!match` without specifying it.")


async def run_coach(ctx, query: str):
    """Core agentic loop — shared by !coach and plain follow-up messages."""
    riot_id = load_riot_id(str(ctx.author.id))
    log.info("Coach query from %s (riot_id=%s): %s", ctx.author, riot_id, query)

    messages = get_history(ctx.channel.id, ctx.author.id)
    messages.append({"role": "user", "content": query})

    for iteration in range(10):
        log.debug("Claude iteration %d", iteration + 1)
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: anthropic_client.messages.create(
                model="claude-opus-4-7",
                max_tokens=4096,
                system=coach_system(riot_id),
                tools=TOOLS,
                messages=messages,
            ),
        )
        log.debug("Claude stop_reason=%s", response.stop_reason)

        if response.stop_reason == "end_turn":
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), "No response."
            )
            messages.append({"role": "assistant", "content": text})
            save_history(ctx.channel.id, ctx.author.id, messages)
            log.info("Coach response sent (%d chars, history=%d msgs)", len(text), len(messages))
            await _send_long(ctx, text)
            return

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    log.info("Claude calling tool: %s(%s)", block.name, block.input)
                    result = await execute_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
        else:
            log.warning("Unexpected stop_reason: %s", response.stop_reason)
            await ctx.send("Unexpected stop reason — please try again.")
            return

    log.warning("Coach hit iteration limit for %s", ctx.author)
    await ctx.send("Reached iteration limit. Please try a more specific query.")


@bot.command(name="coach")
async def coach_cmd(ctx, *, query: str):
    """Start or continue a coaching conversation."""
    await ctx.send("*Thinking...*")
    await run_coach(ctx, query)


@bot.command(name="reset")
async def reset_cmd(ctx):
    """Clear your conversation history and start fresh."""
    clear_history(ctx.channel.id, ctx.author.id)
    log.info("Conversation cleared for %s", ctx.author)
    await ctx.send("Conversation cleared. Start fresh with `!coach <question>`.")


@bot.command(name="stats")
async def stats_cmd(ctx, riot_id: str = None):
    """Show ranked stats as a Discord embed."""
    riot_id = resolve_riot_id(str(ctx.author.id), riot_id)
    if not riot_id:
        await ctx.send("I don't know your Riot ID yet. Use `!setid Name#Tag` to save it, or pass it directly: `!stats Name#Tag`")
        return
    log.info("!stats from %s for %s", ctx.author, riot_id)
    try:
        data = await riot.get_ranked_stats(riot_id)
        embed = discord.Embed(
            title=f"Ranked Stats — {riot_id}",
            color=0x1A78C2,
        )
        summoner = data.get("summoner", {})
        embed.set_footer(text=f"Level {summoner.get('summoner_level', '?')}")

        for queue, s in data.get("ranked", {}).items():
            label = "Solo/Duo" if "SOLO" in queue else "Flex"
            tier = s.get("tier", "UNRANKED")
            rank = s.get("rank", "")
            lp = s.get("lp", 0)
            wr = s.get("win_rate", 0)
            w = s.get("wins", 0)
            l = s.get("losses", 0)
            streak = " 🔥" if s.get("hot_streak") else ""
            embed.add_field(
                name=f"{label}{streak}",
                value=f"**{tier} {rank}** {lp} LP\n{w}W / {l}L  ({wr}% WR)",
                inline=True,
            )

        if not data.get("ranked"):
            embed.description = "Unranked this season."

        await ctx.send(embed=embed)
        log.info("!stats response sent for %s", riot_id)
    except Exception as exc:
        log.error("!stats failed for %s: %s", riot_id, exc, exc_info=VERBOSE)
        await ctx.send(f"Error: {exc}")


@bot.command(name="match")
async def match_cmd(ctx, riot_id: str = None):
    """Analyse the player's most recent match."""
    riot_id = resolve_riot_id(str(ctx.author.id), riot_id)
    if not riot_id:
        await ctx.send("I don't know your Riot ID yet. Use `!setid Name#Tag` to save it, or pass it directly: `!match Name#Tag`")
        return
    log.info("!match from %s for %s", ctx.author, riot_id)
    try:
        await ctx.send("*Fetching last match...*")
        history = await riot.get_match_history(riot_id, count=1)
        match_ids = history.get("match_ids", [])
        if not match_ids:
            log.warning("No matches found for %s", riot_id)
            await ctx.send("No recent matches found.")
            return
        log.debug("Fetching match details for %s", match_ids[0])
        match_data = await riot.get_match_details(match_ids[0], riot_id)
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, analyzer.analyze_match, riot_id, match_data
        )
        await _send_long(ctx, f"**Match analysis for {riot_id}**\n\n{analysis}")
        log.info("!match response sent for %s", riot_id)
    except Exception as exc:
        log.error("!match failed for %s: %s", riot_id, exc, exc_info=VERBOSE)
        await ctx.send(f"Error: {exc}")


@bot.command(name="profile")
async def profile_cmd(ctx, riot_id: str = None):
    """Show summoner profile."""
    riot_id = resolve_riot_id(str(ctx.author.id), riot_id)
    if not riot_id:
        await ctx.send("I don't know your Riot ID yet. Use `!setid Name#Tag` to save it, or pass it directly: `!profile Name#Tag`")
        return
    log.info("!profile from %s for %s", ctx.author, riot_id)
    try:
        p = await riot.get_summoner(riot_id)
        embed = discord.Embed(title=riot_id, color=0xC89B3C)
        embed.add_field(name="Level", value=str(p.get("summoner_level", "?")))
        embed.add_field(name="Tag", value=p.get("tag_line", "?"))
        await ctx.send(embed=embed)
        log.info("!profile response sent for %s", riot_id)
    except Exception as exc:
        log.error("!profile failed for %s: %s", riot_id, exc, exc_info=VERBOSE)
        await ctx.send(f"Error: {exc}")


if __name__ == "__main__":
    token = config.discord_token
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set in .env")
    log.info("Verbose mode: %s", VERBOSE)
    bot.run(token, log_handler=None)  # logging already configured above
