# lol-mcp — League of Legends MCP Coach

An MCP (Model Context Protocol) server that connects Claude to the Riot Games API so you can ask Claude to coach you, review your matches, and analyse your stats — all in natural language.

Also ships an **optional Discord bot** that wraps the same tools.

---

## Features

| Tool | What it does |
|---|---|
| `get_summoner` | Look up any player profile by Riot ID |
| `get_ranked_stats` | Tier, LP, wins, losses, win rate for Solo/Duo and Flex |
| `get_match_history` | Recent match IDs (filterable by queue) |
| `get_match_details` | Full per-player stats for a specific match |
| `get_champion_stats` | Aggregated champion pool stats from recent matches |
| `analyze_performance` | AI coaching report on recent performance |
| `analyze_match` | AI post-match review for one game |
| `analyze_champion_pool` | AI champion pool audit |
| `get_pre_game_tips` | Matchup-specific advice before a game |
| `cache_stats` | Inspect / purge the local SQLite cache |

Responses are cached in SQLite to minimise Riot API calls:
- Match data: 1 hour
- Summoner/account: 5 minutes
- Ranked entries: 2 minutes

---

## Quick start

### 1. Clone & install

```bash
cd lol-mcp
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and fill in RIOT_API_KEY, ANTHROPIC_API_KEY, REGION
```

Get a Riot API key at <https://developer.riotgames.com>.  
Get an Anthropic API key at <https://console.anthropic.com>.

### 3. Register the MCP server with Claude Desktop

Add this to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "lol-coach": {
      "command": "/absolute/path/to/lol-mcp/.venv/bin/python3",
      "args": ["/absolute/path/to/lol-mcp/server.py"],
      "env": {
        "RIOT_API_KEY": "RGAPI-...",
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "REGION": "na1"
      }
    }
  }
}
```

Or use a `.env` file and omit the `env` block.

### 4. Ask Claude

Once registered, open Claude Desktop and try:

- *"Coach me — my Riot ID is YourName#TAG"*
- *"How is my CS in my last 10 ranked games?"*
- *"I play Jinx and my recent stats are bad, what should I improve?"*
- *"Analyze my last match: YourName#TAG"*
- *"Give me tips for playing Zed into Yasuo mid"*

---

## Discord bot (optional)

```bash
# Add DISCORD_TOKEN to your .env, then:
source .venv/bin/activate
python discord_bot.py
```

### Commands

| Command | Description |
|---|---|
| `!coach <question>` | Agentic coaching (Claude fetches data automatically) |
| `!stats Name#Tag` | Ranked stats embed |
| `!match Name#Tag` | AI review of most recent match |
| `!profile Name#Tag` | Summoner profile |

---

## Project structure

```
lol-mcp/
├── server.py        # MCP server entry point
├── riot_api.py      # Riot API client (async, cached)
├── analyzer.py      # Claude AI analysis & coaching
├── cache.py         # SQLite cache
├── config.py        # Config from env vars / .env
├── discord_bot.py   # Discord bot (optional)
├── requirements.txt
└── .env.example
```

---

## Extending

**Add a new Riot API endpoint** — add a method to `RiotAPIClient` in `riot_api.py`.

**Add a new MCP tool** — add an entry to `TOOLS` in `server.py` and a branch in `_dispatch()`.

**Add a new coaching prompt** — add a method to `LoLAnalyzer` in `analyzer.py`.

**Connect another interface** (Slack, web, CLI) — import `RiotAPIClient` and `LoLAnalyzer` and call them directly; the cache is shared automatically.
