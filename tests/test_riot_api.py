import pytest
from unittest.mock import MagicMock

from riot_api import RiotAPIClient


@pytest.fixture
def client():
    cache = MagicMock()
    cache.get.return_value = None
    config = MagicMock()
    config.region = "na1"
    config.regional_routing = "americas"
    return RiotAPIClient("fake-key", cache, config)


# Minimal realistic match payload with two participants on the same team.
MATCH_DATA = {
    "metadata": {"matchId": "NA1_123"},
    "info": {
        "gameMode": "CLASSIC",
        "gameDuration": 1800,  # 30 minutes
        "gameEndedInSurrender": False,
        "participants": [
            {
                "puuid": "faker-puuid",
                "riotIdGameName": "Faker",
                "teamId": 100,
                "championName": "Zed",
                "teamPosition": "MIDDLE",
                "kills": 10, "deaths": 2, "assists": 5,
                "totalMinionsKilled": 200, "neutralMinionsKilled": 10,
                "totalDamageDealtToChampions": 30000,
                "totalDamageTaken": 15000,
                "visionScore": 20, "wardsPlaced": 5, "visionWardsBoughtInGame": 2,
                "goldEarned": 15000,
                "doubleKills": 1, "tripleKills": 0, "quadraKills": 0, "pentaKills": 0,
                "dragonKills": 0, "baronKills": 0, "turretKills": 1,
                **{f"item{i}": i for i in range(7)},
            },
            {
                "puuid": "support-puuid",
                "riotIdGameName": "Support",
                "teamId": 100,
                "championName": "Lulu",
                "teamPosition": "UTILITY",
                "kills": 3, "deaths": 4, "assists": 12,
                "totalMinionsKilled": 30, "neutralMinionsKilled": 0,
                "totalDamageDealtToChampions": 8000,
                "totalDamageTaken": 12000,
                "visionScore": 40, "wardsPlaced": 15, "visionWardsBoughtInGame": 5,
                "goldEarned": 9000,
                "doubleKills": 0, "tripleKills": 0, "quadraKills": 0, "pentaKills": 0,
                "dragonKills": 0, "baronKills": 0, "turretKills": 0,
                **{f"item{i}": 0 for i in range(7)},
            },
        ],
        "teams": [
            {"teamId": 100, "win": True},
            {"teamId": 200, "win": False},
        ],
    },
}


class TestSummariseForPlayer:
    def test_finds_player_by_puuid(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="faker-puuid")
        assert result["champion"] == "Zed"
        assert result["kills"] == 10

    def test_puuid_takes_priority_over_name(self, client):
        # puuid points to Support, even though riot_id says Faker
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="support-puuid")
        assert result["champion"] == "Lulu"

    def test_falls_back_to_game_name_when_no_puuid(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid=None)
        assert result["champion"] == "Zed"

    def test_falls_back_to_game_name_when_puuid_not_found(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="unknown-puuid")
        assert result["champion"] == "Zed"

    def test_player_not_found_returns_error(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Ghost#000", puuid="bad-puuid")
        assert "error" in result
        assert result["match_id"] == "NA1_123"

    def test_win_is_true_when_team_won(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="faker-puuid")
        assert result["win"] is True

    def test_win_is_false_for_losing_team(self, client):
        losing_data = {
            **MATCH_DATA,
            "info": {
                **MATCH_DATA["info"],
                "teams": [
                    {"teamId": 100, "win": False},
                    {"teamId": 200, "win": True},
                ],
            },
        }
        result = client._summarise_for_player(losing_data, "Faker#KR1", puuid="faker-puuid")
        assert result["win"] is False

    def test_cs_per_min_calculated_correctly(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="faker-puuid")
        # 200 lane + 10 jungle = 210 cs over 30 min = 7.0
        assert result["cs"] == 210
        assert result["cs_per_min"] == 7.0

    def test_kda_calculated_correctly(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="faker-puuid")
        assert result["kda"] == round((10 + 5) / max(2, 1), 2)

    def test_kda_zero_deaths_uses_1_as_denominator(self, client):
        no_death_data = {
            **MATCH_DATA,
            "info": {
                **MATCH_DATA["info"],
                "participants": [
                    {**MATCH_DATA["info"]["participants"][0], "deaths": 0},
                    MATCH_DATA["info"]["participants"][1],
                ],
            },
        }
        result = client._summarise_for_player(no_death_data, "Faker#KR1", puuid="faker-puuid")
        assert result["kda"] == round((10 + 5) / 1, 2)

    def test_match_id_included_in_result(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="faker-puuid")
        assert result["match_id"] == "NA1_123"

    def test_game_duration_minutes(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="faker-puuid")
        assert result["game_duration_minutes"] == 30.0

    def test_role_extracted(self, client):
        result = client._summarise_for_player(MATCH_DATA, "Faker#KR1", puuid="faker-puuid")
        assert result["role"] == "MIDDLE"


class TestGetAccountByRiotId:
    def test_invalid_format_raises_value_error(self, client):
        with pytest.raises(ValueError, match="Name#Tag"):
            import asyncio
            asyncio.run(client.get_account_by_riot_id("NoHashTag"))
