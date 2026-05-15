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
import re
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
analyzer = LoLAnalyzer(config.anthropic_api_key, config.coach_model)
log.info("Components initialised (region=%s, routing=%s)", config.region, config.regional_routing)

# ---------------------------------------------------------------------------
# Per-user Riot ID storage (persisted in the same SQLite DB)
# ---------------------------------------------------------------------------

cache.ensure_user_profiles_table()


def save_riot_id(discord_id: str, riot_id: str):
    cache.save_user_profile(discord_id, riot_id)


def load_riot_id(discord_id: str) -> str | None:
    return cache.load_user_profile(discord_id)


def resolve_riot_id(discord_id: str, provided: str | None) -> str | None:
    """Return provided Riot ID if given, otherwise fall back to saved one."""
    return provided or load_riot_id(discord_id)


# ---------------------------------------------------------------------------
# Per-user conversation history (in-memory, keyed by channel+user)
# ---------------------------------------------------------------------------

CONVERSATION_TIMEOUT = config.conversation_timeout
MAX_HISTORY_MESSAGES = 20
_histories: dict[tuple[int, int], dict] = {}


def get_history(channel_id: int, user_id: int) -> list:
    entry = _histories.get((channel_id, user_id))
    if entry and time.time() - entry["last_active"] < CONVERSATION_TIMEOUT:
        return list(entry["messages"])
    return []


def save_history(channel_id: int, user_id: int, messages: list):
    now = time.time()
    # Prune stale entries to prevent unbounded memory growth
    stale = [k for k, v in _histories.items() if now - v["last_active"] > CONVERSATION_TIMEOUT * 2]
    for k in stale:
        del _histories[k]
    # Cap stored messages to prevent context overflow on next turn
    _histories[(channel_id, user_id)] = {
        "messages": messages[-MAX_HISTORY_MESSAGES:],
        "last_active": now,
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
            r = await asyncio.get_running_loop().run_in_executor(
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

COACH_SYSTEM = (
    "You are an expert League of Legends coach. "
    "Use the available tools to fetch real player data and give specific, "
    "data-backed coaching advice. Always reference actual numbers."
)


def coach_first_message(riot_id: str | None, query: str) -> str:
    """Prepend the user's saved Riot ID to their first message so it stays out of the system prompt."""
    if riot_id:
        return f"[My Riot ID is {riot_id}]\n\n{query}"
    return query


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
    if not re.match(r"^[A-Za-z0-9 _\.]{1,16}#[A-Za-z0-9]{3,5}$", riot_id):
        await ctx.send("Please use the format `Name#Tag` (e.g. `!setid Faker#KR1`). The tag must be 3-5 alphanumeric characters.")
        return
    save_riot_id(str(ctx.author.id), riot_id)
    log.info("Saved Riot ID for %s: %s", ctx.author, riot_id)
    await ctx.send(f"Got it! I'll remember your Riot ID as **{riot_id}**. You can now use `!coach`, `!stats`, `!match` without specifying it.")


_in_flight: set[tuple[int, int]] = set()


async def run_coach(ctx, query: str):
    """Core agentic loop — shared by !coach and plain follow-up messages."""
    session_key = (ctx.channel.id, ctx.author.id)
    if session_key in _in_flight:
        await ctx.send("Still thinking about your last question — please wait.")
        return
    _in_flight.add(session_key)
    try:
        await _run_coach_inner(ctx, query, session_key)
    except Exception as exc:
        log.error("Coach error for %s: %s", ctx.author, exc, exc_info=VERBOSE)
        await ctx.send("Something went wrong — please try again in a moment.")
    finally:
        _in_flight.discard(session_key)


async def _call_claude(loop, msgs_snapshot: list) -> object:
    """Call Claude with retry logic for transient API errors (overloaded / rate limit)."""
    delays = [5, 15, 30]
    for attempt, delay in enumerate(delays):
        try:
            return await loop.run_in_executor(
                None,
                lambda: analyzer.client.messages.create(
                    model=config.coach_model,
                    max_tokens=4096,
                    system=COACH_SYSTEM,
                    tools=TOOLS,
                    messages=msgs_snapshot,
                ),
            )
        except anthropic.APIStatusError as exc:
            # 529 = overloaded, 529/529 are transient — retry with backoff
            if exc.status_code in (429, 529) and attempt < len(delays) - 1:
                log.warning(
                    "Claude API %s (attempt %d/%d) — retrying in %ds",
                    exc.status_code, attempt + 1, len(delays), delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")


async def _run_coach_inner(ctx, query: str, session_key: tuple[int, int]):
    riot_id = load_riot_id(str(ctx.author.id))
    log.info("Coach query from %s (riot_id=%s): %s", ctx.author, riot_id, query)

    messages = get_history(ctx.channel.id, ctx.author.id)
    # Inject Riot ID into the first user message (not system prompt) to prevent prompt injection
    first_message = coach_first_message(riot_id, query) if not messages else query
    messages.append({"role": "user", "content": first_message})

    loop = asyncio.get_running_loop()
    for iteration in range(10):
        log.debug("Claude iteration %d", iteration + 1)
        msgs_snapshot = list(messages)
        try:
            response = await _call_claude(loop, msgs_snapshot)
        except anthropic.BadRequestError as exc:
            if "context_length_exceeded" in str(exc) or "prompt is too long" in str(exc).lower():
                log.warning("Context overflow for %s — clearing history and retrying", ctx.author)
                messages = [{"role": "user", "content": first_message}]
                response = await _call_claude(loop, list(messages))
            else:
                raise
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
        analysis = await asyncio.get_running_loop().run_in_executor(
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
