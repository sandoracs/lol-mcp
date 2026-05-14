import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    def __init__(self):
        self.riot_api_key = os.getenv("RIOT_API_KEY", "")
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.region = os.getenv("REGION", "na1")
        self.regional_routing = self._get_regional_routing(self.region)
        self.cache_db_path = os.getenv("CACHE_DB_PATH", "lol_cache.db")
        self.cache_ttl_matches = int(os.getenv("CACHE_TTL_MATCHES", 3600))    # 1 hour
        self.cache_ttl_summoner = int(os.getenv("CACHE_TTL_SUMMONER", 300))  # 5 min
        self.cache_ttl_ranked = int(os.getenv("CACHE_TTL_RANKED", 120))      # 2 min
        self.cache_ttl_match_ids = int(os.getenv("CACHE_TTL_MATCH_IDS", 300))  # 5 min
        self.coach_model = os.getenv("COACH_MODEL", "claude-opus-4-7")
        self.conversation_timeout = int(os.getenv("CONVERSATION_TIMEOUT", 1800))  # 30 min
        self.discord_token = os.getenv("DISCORD_TOKEN", "")

        if not self.riot_api_key:
            raise ValueError("RIOT_API_KEY environment variable is required")
        if not self.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required")

    def _get_regional_routing(self, region: str) -> str:
        americas = {"na1", "br1", "la1", "la2"}
        europe = {"euw1", "eun1", "tr1", "ru"}
        asia = {"kr", "jp1"}
        sea = {"oc1", "ph2", "sg2", "th2", "tw2", "vn2"}

        if region in americas:
            return "americas"
        elif region in europe:
            return "europe"
        elif region in asia:
            return "asia"
        elif region in sea:
            return "sea"
        return "americas"
