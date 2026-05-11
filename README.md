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

### 1. Create a Discord Application

1. Go to **https://discord.com/developers/applications** and click **New Application**
2. Give it a name (e.g. `LoL Coach`) and confirm

### 2. Create a Bot user

1. In your application, go to the **Bot** tab
2. Click **Add Bot** → confirm
3. Under **Privileged Gateway Intents**, enable **Message Content Intent** (required — without this the bot cannot read messages)
4. Click **Reset Token**, copy the token and save it — you'll need it in step 4

### 3. Invite the bot to your server

1. Go to **OAuth2 → URL Generator**
2. Under **Scopes**, check both `bot` and `applications.commands`
3. Under **Bot Permissions**, check: **Send Messages**, **Read Message History**, **Embed Links**
4. Copy the generated URL, open it in your browser, and select your server

### 4. Configure `.env`

```bash
cp .env.example .env
```

Fill in your values:

```env
RIOT_API_KEY=RGAPI-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-...
REGION=eun1        # your region: na1 | euw1 | eun1 | kr | br1 | jp1 ...
DISCORD_TOKEN=your-bot-token-here
VERBOSE=0          # set to 1 for debug logging
```

Get a Riot API key at **https://developer.riotgames.com** (development keys expire every 24 hours; apply for a production key for permanent use).

### 5. Run the bot

```bash
source .venv/bin/activate    # Windows: .venv\Scripts\activate
python discord_bot.py
```

You should see:
```
[INFO] lol-bot: LoL Coach Bot online as LoL Coach#1234 (id=...)
[INFO] lol-bot: Serving 1 guild(s)
```

If it says `Serving 0 guild(s)`, the bot was not invited correctly — repeat step 3 and make sure the `bot` scope is checked.

### Commands

| Command | Description |
|---|---|
| `!setid Name#Tag` | Save your Riot ID once — all other commands use it automatically |
| `!coach <question>` | Start a coaching conversation — Claude fetches your data automatically |
| `!reset` | Clear conversation history and start fresh |
| `!stats [Name#Tag]` | Ranked stats embed (Solo/Duo & Flex) |
| `!match [Name#Tag]` | AI review of your most recent match |
| `!profile [Name#Tag]` | Summoner profile |
| `!help` | Show all commands and example questions |

### Conversation flow

After `!coach`, you can reply naturally without typing `!coach` again — the bot remembers the last 30 minutes of conversation:

```
You:  !coach how was my last game?
Bot:  [analyses and replies]
You:  what should I work on?        ← no prefix needed
Bot:  [continues from context]
You:  !reset                        ← start fresh
```

### Keeping the bot running

To run the bot permanently in the background:

```bash
# macOS / Linux
nohup python discord_bot.py > bot.log 2>&1 &

# Or with verbose logging
VERBOSE=1 nohup python discord_bot.py > bot.log 2>&1 &
```

For a more robust setup, consider running it as a `systemd` service or on a small cloud VM (e.g. a free-tier Oracle Cloud or AWS EC2 instance).

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
