import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("RIOT_API_KEY", "test-riot-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("CACHE_DB_PATH", ":memory:")
