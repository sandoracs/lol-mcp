import httpx
from typing import Optional
from cache import CacheManager
from config import Config


class RiotAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Riot API {status_code}: {message}")


class RiotAPIClient:
    def __init__(self, api_key: str, cache: CacheManager, config: Config):
        self.api_key = api_key
        self.cache = cache
        self.config = config
        self.headers = {"X-Riot-Token": api_key}

    def _platform_url(self, path: str) -> str:
        return f"https://{self.config.region}.api.riotgames.com{path}"

    def _regional_url(self, path: str) -> str:
        return f"https://{self.config.regional_routing}.api.riotgames.com{path}"

    async def _get(self, url: str) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=self.headers)
            if resp.status_code == 404:
                raise RiotAPIError(404, "Not found")
            if resp.status_code == 429:
                raise RiotAPIError(429, "Rate limit exceeded — please wait and retry")
            if resp.status_code == 403:
                raise RiotAPIError(403, "Invalid or expired API key")
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------ #
    # Account & Summoner
    # ------------------------------------------------------------------ #

    async def get_account_by_riot_id(self, riot_id: str) -> dict:
        if "#" not in riot_id:
            raise ValueError("Riot ID must be in format Name#Tag (e.g. Faker#KR1)")
        game_name, tag_line = riot_id.split("#", 1)
        cache_key = f"account:{riot_id.lower()}"
        if cached := self.cache.get(cache_key):
            return cached
        url = self._regional_url(
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        )
        data = await self._get(url)
        self.cache.set(cache_key, data, self.config.cache_ttl_summoner)
        return data

    async def get_summoner_by_puuid(self, puuid: str) -> dict:
        cache_key = f"summoner:puuid:{puuid}"
        if cached := self.cache.get(cache_key):
            return cached
        url = self._platform_url(f"/lol/summoner/v4/summoners/by-puuid/{puuid}")
        data = await self._get(url)
        self.cache.set(cache_key, data, self.config.cache_ttl_summoner)
        return data

    async def get_summoner(self, riot_id: str) -> dict:
        account = await self.get_account_by_riot_id(riot_id)
        summoner = await self.get_summoner_by_puuid(account["puuid"])
        return {
            "riot_id": riot_id,
            "game_name": account["gameName"],
            "tag_line": account["tagLine"],
            "puuid": account["puuid"],
            "summoner_id": summoner["id"],
            "summoner_level": summoner["summonerLevel"],
            "profile_icon_id": summoner["profileIconId"],
        }

    # ------------------------------------------------------------------ #
    # Ranked stats
    # ------------------------------------------------------------------ #

    async def get_ranked_stats(self, riot_id: str) -> dict:
        summoner = await self.get_summoner(riot_id)
        summoner_id = summoner["summoner_id"]

        cache_key = f"ranked:{summoner_id}"
        if cached := self.cache.get(cache_key):
            return cached

        url = self._platform_url(f"/lol/league/v4/entries/by-summoner/{summoner_id}")
        entries = await self._get(url)

        ranked: dict = {}
        for entry in entries:
            queue = entry.get("queueType", "")
            wins = entry.get("wins", 0)
            losses = entry.get("losses", 0)
            ranked[queue] = {
                "tier": entry.get("tier", "UNRANKED"),
                "rank": entry.get("rank", ""),
                "lp": entry.get("leaguePoints", 0),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / max(wins + losses, 1) * 100, 1),
                "hot_streak": entry.get("hotStreak", False),
                "veteran": entry.get("veteran", False),
                "fresh_blood": entry.get("freshBlood", False),
            }

        result = {"summoner": summoner, "ranked": ranked}
        self.cache.set(cache_key, result, self.config.cache_ttl_ranked)
        return result

    # ------------------------------------------------------------------ #
    # Match history
    # ------------------------------------------------------------------ #

    async def get_match_ids(
        self,
        puuid: str,
        count: int = 10,
        queue: Optional[int] = None,
    ) -> list[str]:
        cache_key = f"match_ids:{puuid}:{count}:{queue}"
        if cached := self.cache.get(cache_key):
            return cached

        params = f"count={count}"
        if queue:
            params += f"&queue={queue}"
        url = self._regional_url(
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids?{params}"
        )
        data = await self._get(url)
        self.cache.set(cache_key, data, 300)  # 5 min — list changes as new games finish
        return data

    async def get_match_details(
        self, match_id: str, riot_id: Optional[str] = None
    ) -> dict:
        cache_key = f"match:{match_id}"
        if cached := self.cache.get(cache_key):
            return self._summarise_for_player(cached, riot_id) if riot_id else cached

        url = self._regional_url(f"/lol/match/v5/matches/{match_id}")
        data = await self._get(url)
        self.cache.set(cache_key, data, self.config.cache_ttl_matches)

        return self._summarise_for_player(data, riot_id) if riot_id else data

    def _summarise_for_player(self, match_data: dict, riot_id: str) -> dict:
        game_name = riot_id.split("#")[0].lower() if "#" in riot_id else riot_id.lower()
        info = match_data.get("info", {})
        participants = info.get("participants", [])

        target = next(
            (
                p for p in participants
                if p.get("riotIdGameName", "").lower() == game_name
            ),
            None,
        )
        if not target:
            target = next(
                (
                    p for p in participants
                    if game_name in p.get("riotIdGameName", "").lower()
                ),
                None,
            )
        if not target:
            return {"error": f"Player '{riot_id}' not found in match", "match_id": match_data.get("metadata", {}).get("matchId", "")}

        team_id = target.get("teamId")
        team_won = any(
            t.get("win")
            for t in info.get("teams", [])
            if t.get("teamId") == team_id
        )

        duration_s = info.get("gameDuration", 0)
        minutes = duration_s / 60

        cs = target.get("totalMinionsKilled", 0) + target.get("neutralMinionsKilled", 0)
        cs_per_min = round(cs / max(minutes, 1), 1)

        team_kills = sum(
            p.get("kills", 0) for p in participants if p.get("teamId") == team_id
        )
        kp = round(
            (target.get("kills", 0) + target.get("assists", 0))
            / max(team_kills, 1)
            * 100,
            1,
        )

        return {
            "match_id": match_data.get("metadata", {}).get("matchId", ""),
            "game_mode": info.get("gameMode", ""),
            "game_duration_minutes": round(minutes, 1),
            "win": team_won,
            "champion": target.get("championName", ""),
            "role": target.get("teamPosition") or target.get("individualPosition", ""),
            "kills": target.get("kills", 0),
            "deaths": target.get("deaths", 0),
            "assists": target.get("assists", 0),
            "kda": round(
                (target.get("kills", 0) + target.get("assists", 0))
                / max(target.get("deaths", 1), 1),
                2,
            ),
            "kill_participation_pct": kp,
            "cs": cs,
            "cs_per_min": cs_per_min,
            "damage_to_champions": target.get("totalDamageDealtToChampions", 0),
            "damage_taken": target.get("totalDamageTaken", 0),
            "vision_score": target.get("visionScore", 0),
            "wards_placed": target.get("wardsPlaced", 0),
            "control_wards_bought": target.get("visionWardsBoughtInGame", 0),
            "gold_earned": target.get("goldEarned", 0),
            "items": [target.get(f"item{i}", 0) for i in range(7)],
            "multikills": {
                "double": target.get("doubleKills", 0),
                "triple": target.get("tripleKills", 0),
                "quadra": target.get("quadraKills", 0),
                "penta": target.get("pentaKills", 0),
            },
            "objectives": {
                "dragon_kills": target.get("dragonKills", 0),
                "baron_kills": target.get("baronKills", 0),
                "turret_kills": target.get("turretKills", 0),
            },
            "game_ended_in_surrender": info.get("gameEndedInSurrender", False),
        }

    async def get_match_history(
        self,
        riot_id: str,
        count: int = 10,
        include_details: bool = False,
        queue: Optional[int] = None,
    ) -> dict:
        account = await self.get_account_by_riot_id(riot_id)
        puuid = account["puuid"]
        match_ids = await self.get_match_ids(puuid, count, queue)

        if not include_details:
            return {"match_ids": match_ids, "count": len(match_ids)}

        matches = []
        for mid in match_ids:
            try:
                matches.append(await self.get_match_details(mid, riot_id))
            except Exception:
                continue

        return {"matches": matches, "count": len(matches)}

    # ------------------------------------------------------------------ #
    # Champion stats aggregated from recent matches
    # ------------------------------------------------------------------ #

    async def get_champion_stats(
        self,
        riot_id: str,
        champion_name: Optional[str] = None,
        count: int = 20,
    ) -> dict:
        account = await self.get_account_by_riot_id(riot_id)
        puuid = account["puuid"]

        cache_key = f"champ_stats:{puuid}:{champion_name}:{count}"
        if cached := self.cache.get(cache_key):
            return cached

        match_ids = await self.get_match_ids(puuid, count)
        totals: dict = {}

        for mid in match_ids:
            try:
                m = await self.get_match_details(mid, riot_id)
                champ = m.get("champion", "Unknown")
                if champion_name and champ.lower() != champion_name.lower():
                    continue
                if champ not in totals:
                    totals[champ] = {
                        "games": 0, "wins": 0, "kills": 0, "deaths": 0,
                        "assists": 0, "cs": 0, "vision_score": 0,
                        "damage": 0, "gold": 0,
                    }
                s = totals[champ]
                s["games"] += 1
                s["wins"] += int(m.get("win", False))
                s["kills"] += m.get("kills", 0)
                s["deaths"] += m.get("deaths", 0)
                s["assists"] += m.get("assists", 0)
                s["cs"] += m.get("cs", 0)
                s["vision_score"] += m.get("vision_score", 0)
                s["damage"] += m.get("damage_to_champions", 0)
                s["gold"] += m.get("gold_earned", 0)
            except Exception:
                continue

        result = {}
        for champ, s in sorted(totals.items(), key=lambda x: -x[1]["games"]):
            g = s["games"]
            result[champ] = {
                "games": g,
                "wins": s["wins"],
                "losses": g - s["wins"],
                "win_rate": round(s["wins"] / g * 100, 1),
                "avg_kills": round(s["kills"] / g, 1),
                "avg_deaths": round(s["deaths"] / g, 1),
                "avg_assists": round(s["assists"] / g, 1),
                "avg_kda": round((s["kills"] + s["assists"]) / max(s["deaths"], 1), 2),
                "avg_cs": round(s["cs"] / g, 1),
                "avg_vision_score": round(s["vision_score"] / g, 1),
                "avg_damage": round(s["damage"] / g),
                "avg_gold": round(s["gold"] / g),
            }

        self.cache.set(cache_key, result, 1800)
        return result
