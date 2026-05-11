#!/usr/bin/env python3
"""Discord bot interface for the LoL MCP coach.

Commands:
  !coach <question>       — Ask the AI coach anything (agentic, uses Riot tools)
  !stats <Name#Tag>       — Quick ranked stats embed
  !match <Name#Tag>       — Analyse most recent match
  !profile <Name#Tag>     — Summoner profile
"""

import asyncio
import json
import os

import anthropic
import discord
from discord.ext import commands

from analyzer import LoLAnalyzer
from cache import CacheManager
from config import Config
from riot_api import RiotAPIClient

# ---------------------------------------------------------------------------
# Bootstrap shared components
# ---------------------------------------------------------------------------

config = Config()
cache = CacheManager(config.cache_db_path)
riot = RiotAPIClient(config.riot_api_key, cache, config)
analyzer = LoLAnalyzer(config.anthropic_api_key)
anthropic_client = anthropic.Anthropic(api_key=config.anthropic_api_key)

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
            return r if isinstance(r, str) else json.dumps(r, indent=2)
        elif name == "get_champion_stats":
            r = await riot.get_champion_stats(args["riot_id"], count=args.get("count", 20))
        else:
            return f"Unknown tool: {name}"
        return json.dumps(r, indent=2, ensure_ascii=False)
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

COACH_SYSTEM = (
    "You are an expert League of Legends coach. "
    "Use the available tools to fetch real player data and give specific, "
    "data-backed coaching advice. Always reference actual numbers."
)


async def _send_long(ctx, text: str):
    """Send a message, splitting at 1900 chars to stay within Discord limits."""
    for i in range(0, len(text), 1900):
        await ctx.send(text[i : i + 1900])


@bot.event
async def on_ready():
    print(f"LoL Coach Bot online as {bot.user} (id={bot.user.id})")


@bot.command(name="coach")
async def coach_cmd(ctx, *, query: str):
    """Agentic coaching: Claude picks which tools to call."""
    await ctx.send("*Fetching data and analysing...*")

    messages = [{"role": "user", "content": query}]

    for _ in range(10):  # safety cap on agentic iterations
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: anthropic_client.messages.create(
                model="claude-opus-4-7",
                max_tokens=4096,
                system=COACH_SYSTEM,
                tools=TOOLS,
                messages=messages,
            ),
        )

        if response.stop_reason == "end_turn":
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), "No response."
            )
            await _send_long(ctx, text)
            return

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
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
            await ctx.send("Unexpected stop reason — please try again.")
            return

    await ctx.send("Reached iteration limit. Please try a more specific query.")


@bot.command(name="stats")
async def stats_cmd(ctx, riot_id: str):
    """Show ranked stats as a Discord embed."""
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
    except Exception as exc:
        await ctx.send(f"Error: {exc}")


@bot.command(name="match")
async def match_cmd(ctx, riot_id: str):
    """Analyse the player's most recent match."""
    try:
        await ctx.send("*Fetching last match...*")
        history = await riot.get_match_history(riot_id, count=1)
        match_ids = history.get("match_ids", [])
        if not match_ids:
            await ctx.send("No recent matches found.")
            return
        match_data = await riot.get_match_details(match_ids[0], riot_id)
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, analyzer.analyze_match, riot_id, match_data
        )
        await _send_long(ctx, f"**Match analysis for {riot_id}**\n\n{analysis}")
    except Exception as exc:
        await ctx.send(f"Error: {exc}")


@bot.command(name="profile")
async def profile_cmd(ctx, riot_id: str):
    """Show summoner profile."""
    try:
        p = await riot.get_summoner(riot_id)
        embed = discord.Embed(title=riot_id, color=0xC89B3C)
        embed.add_field(name="Level", value=str(p.get("summoner_level", "?")))
        embed.add_field(name="Tag", value=p.get("tag_line", "?"))
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"Error: {exc}")


if __name__ == "__main__":
    token = config.discord_token
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set in .env")
    bot.run(token)
