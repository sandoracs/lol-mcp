#!/usr/bin/env python3
"""League of Legends MCP Server — exposes Riot API data and AI coaching as MCP tools."""

import asyncio
import json

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from config import Config
from cache import CacheManager
from riot_api import RiotAPIClient
from analyzer import LoLAnalyzer

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

config = Config()
cache = CacheManager(config.cache_db_path)
riot = RiotAPIClient(config.riot_api_key, cache, config)
analyzer = LoLAnalyzer(config.anthropic_api_key, config.coach_model)

app = Server("lol-mcp")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        name="get_summoner",
        description="Look up a summoner profile by Riot ID (Name#Tag). Returns level, PUUID, and summoner ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "riot_id": {
                    "type": "string",
                    "description": "Riot ID in format Name#Tag, e.g. Faker#KR1",
                },
            },
            "required": ["riot_id"],
        },
    ),
    types.Tool(
        name="get_ranked_stats",
        description="Get ranked Solo/Duo and Flex stats for a summoner: tier, LP, wins, losses, win rate.",
        inputSchema={
            "type": "object",
            "properties": {
                "riot_id": {"type": "string", "description": "Riot ID in format Name#Tag"},
            },
            "required": ["riot_id"],
        },
    ),
    types.Tool(
        name="get_match_history",
        description="Fetch recent match IDs for a summoner. Use get_match_details to drill into individual matches.",
        inputSchema={
            "type": "object",
            "properties": {
                "riot_id": {"type": "string", "description": "Riot ID in format Name#Tag"},
                "count": {
                    "type": "integer",
                    "description": "Number of matches to fetch (1-20, default 10)",
                    "default": 10,
                },
                "queue": {
                    "type": "integer",
                    "description": "Optional queue filter: 420=Solo/Duo, 440=Flex, 450=ARAM",
                },
            },
            "required": ["riot_id"],
        },
    ),
    types.Tool(
        name="get_match_details",
        description="Get detailed stats for a specific match, optionally focused on one player.",
        inputSchema={
            "type": "object",
            "properties": {
                "match_id": {
                    "type": "string",
                    "description": "Match ID, e.g. NA1_1234567890",
                },
                "riot_id": {
                    "type": "string",
                    "description": "Optional Riot ID to extract player-specific stats",
                },
            },
            "required": ["match_id"],
        },
    ),
    types.Tool(
        name="get_champion_stats",
        description="Aggregate champion statistics from a player's recent matches: win rate, avg KDA, CS, damage per champion.",
        inputSchema={
            "type": "object",
            "properties": {
                "riot_id": {"type": "string", "description": "Riot ID in format Name#Tag"},
                "champion_name": {
                    "type": "string",
                    "description": "Filter to a single champion (optional)",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of recent matches to scan (default 20)",
                    "default": 20,
                },
            },
            "required": ["riot_id"],
        },
    ),
    types.Tool(
        name="analyze_performance",
        description="AI coaching analysis of a player's recent performance. Returns strengths, weaknesses, and actionable drills.",
        inputSchema={
            "type": "object",
            "properties": {
                "riot_id": {"type": "string", "description": "Riot ID in format Name#Tag"},
                "count": {
                    "type": "integer",
                    "description": "Number of recent matches to analyze (default 10)",
                    "default": 10,
                },
                "focus_area": {
                    "type": "string",
                    "description": "Specific area to focus on: 'csing', 'vision', 'deaths', 'teamfighting', 'overall'",
                    "default": "overall",
                },
            },
            "required": ["riot_id"],
        },
    ),
    types.Tool(
        name="analyze_match",
        description="AI analysis of a specific match with improvement tips for a given player.",
        inputSchema={
            "type": "object",
            "properties": {
                "match_id": {"type": "string", "description": "Match ID to analyze"},
                "riot_id": {"type": "string", "description": "Riot ID to focus analysis on"},
            },
            "required": ["match_id", "riot_id"],
        },
    ),
    types.Tool(
        name="analyze_champion_pool",
        description="AI analysis of a player's champion pool: which champs to keep, drop, or improve.",
        inputSchema={
            "type": "object",
            "properties": {
                "riot_id": {"type": "string", "description": "Riot ID in format Name#Tag"},
                "count": {
                    "type": "integer",
                    "description": "Matches to scan for champion data (default 30)",
                    "default": 30,
                },
            },
            "required": ["riot_id"],
        },
    ),
    types.Tool(
        name="get_pre_game_tips",
        description="Get coaching tips for a specific matchup before a game.",
        inputSchema={
            "type": "object",
            "properties": {
                "riot_id": {"type": "string", "description": "Riot ID in format Name#Tag"},
                "champion": {"type": "string", "description": "Champion you are playing"},
                "opponent": {"type": "string", "description": "Champion you are facing"},
                "role": {
                    "type": "string",
                    "description": "Your role: TOP, JUNGLE, MID, BOTTOM, SUPPORT",
                },
            },
            "required": ["riot_id", "champion", "opponent", "role"],
        },
    ),
    types.Tool(
        name="cache_stats",
        description="Show cache statistics and optionally clean up expired entries.",
        inputSchema={
            "type": "object",
            "properties": {
                "cleanup": {
                    "type": "boolean",
                    "description": "If true, remove expired entries from cache",
                    "default": False,
                },
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, indent=2, ensure_ascii=False)
        return [types.TextContent(type="text", text=text)]
    except Exception as exc:
        return [types.TextContent(type="text", text=f"Error: {exc}")]


async def _dispatch(name: str, args: dict):
    if name == "get_summoner":
        return await riot.get_summoner(args["riot_id"])

    if name == "get_ranked_stats":
        return await riot.get_ranked_stats(args["riot_id"])

    if name == "get_match_history":
        return await riot.get_match_history(
            args["riot_id"],
            count=min(args.get("count", 10), 20),
            queue=args.get("queue"),
        )

    if name == "get_match_details":
        riot_id = args.get("riot_id")
        puuid = None
        if riot_id:
            account = await riot.get_account_by_riot_id(riot_id)
            puuid = account["puuid"]
        return await riot.get_match_details(args["match_id"], riot_id, puuid)

    if name == "get_champion_stats":
        return await riot.get_champion_stats(
            args["riot_id"],
            champion_name=args.get("champion_name"),
            count=args.get("count", 20),
        )

    if name == "analyze_performance":
        count = args.get("count", 10)
        matches = await riot.get_match_history(args["riot_id"], count, include_details=True)
        ranked = await riot.get_ranked_stats(args["riot_id"])
        return await asyncio.get_running_loop().run_in_executor(
            None,
            analyzer.analyze_performance,
            args["riot_id"],
            matches,
            ranked,
            args.get("focus_area", "overall"),
        )

    if name == "analyze_match":
        match_data = await riot.get_match_details(args["match_id"], args["riot_id"])
        return await asyncio.get_running_loop().run_in_executor(
            None,
            analyzer.analyze_match,
            args["riot_id"],
            match_data,
        )

    if name == "analyze_champion_pool":
        champion_stats = await riot.get_champion_stats(
            args["riot_id"], count=args.get("count", 30)
        )
        return await asyncio.get_running_loop().run_in_executor(
            None,
            analyzer.analyze_champion,
            args["riot_id"],
            champion_stats,
        )

    if name == "get_pre_game_tips":
        return await asyncio.get_running_loop().run_in_executor(
            None,
            analyzer.get_pre_game_tips,
            args["riot_id"],
            args["champion"],
            args["opponent"],
            args["role"],
        )

    if name == "cache_stats":
        stats = cache.stats()
        if args.get("cleanup"):
            deleted = cache.cleanup()
            stats["cleaned_up"] = deleted
        return stats

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
