import json
import anthropic

SYSTEM_PROMPT = """You are an expert League of Legends coach with deep knowledge of:
- Champion mechanics, itemisation, and matchups
- Wave management, CS patterns, and laning fundamentals
- Vision control and map awareness
- Team fighting, objective trading, and macro play
- Ranked climb psychology and habit formation

When analyzing data, always reference specific numbers and give concrete, actionable advice
rather than generic platitudes. Prioritize the highest-impact improvements first."""


class LoLAnalyzer:
    def __init__(self, api_key: str, model: str = "claude-opus-4-7"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _chat(self, user_content: str, max_tokens: int = 2048) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text

    def analyze_performance(
        self,
        riot_id: str,
        matches_data: dict,
        ranked_stats: dict,
        focus_area: str = "overall",
    ) -> str:
        matches = matches_data.get("matches", [])
        prompt = f"""Analyze this League of Legends player's recent performance.

**Player:** {riot_id}
**Focus area:** {focus_area}

**Ranked Stats:**
{json.dumps(ranked_stats.get("ranked", {}), indent=2)}

**Last {len(matches)} matches:**
{json.dumps(matches, indent=2)}

Please provide:
1. **Performance Summary** — key trends across these games (wins, KDA, CS, vision)
2. **Strengths** — what this player consistently does well (with numbers)
3. **Weaknesses** — the 2-3 biggest problems dragging down performance (with numbers)
4. **Actionable Drills** — specific things to practice in the next 5 games
5. **Priority Focus** — single most impactful habit to change right now

Be direct, reference actual stats, and tailor advice to the player's current rank."""
        return self._chat(prompt, max_tokens=2048)

    def analyze_match(self, riot_id: str, match_data: dict) -> str:
        prompt = f"""Review this specific League of Legends match for player {riot_id}.

**Match data:**
{json.dumps(match_data, indent=2)}

Provide:
1. **Match Overview** — result, champion, role, duration
2. **What went well** — good decisions or stats (reference numbers)
3. **Key mistakes** — specific errors that hurt the result
4. **Improvement points** — 3 concrete things to do differently next game
5. **One Takeaway** — the single most important lesson from this match"""
        return self._chat(prompt, max_tokens=1024)

    def analyze_champion(self, riot_id: str, champion_stats: dict) -> str:
        prompt = f"""Analyze champion pool performance for {riot_id}.

**Champion stats (from recent matches):**
{json.dumps(champion_stats, indent=2)}

Provide:
1. **Best champions** — which to keep playing and why
2. **Underperforming picks** — champions with poor win rates or stats
3. **Pool recommendations** — should they focus, expand, or swap?
4. **Skill gaps** — what champion-specific mechanics to work on
5. **Actionable advice** — which champion to main for fastest rank improvement"""
        return self._chat(prompt, max_tokens=1024)

    def get_pre_game_tips(self, riot_id: str, champion: str, opponent: str, role: str) -> str:
        prompt = f"""Give pre-game coaching tips for {riot_id}.

**Matchup:** {champion} vs {opponent} in {role}

Provide:
1. **Laning phase plan** — trade patterns, level spikes, when to all-in
2. **Itemisation** — first 3 items and why for this matchup
3. **Early game priority** — CS focus vs aggressive play
4. **Key threats** — what to watch out for from {opponent}
5. **Win condition** — how {champion} beats {opponent} and wins the game

Be specific to this exact matchup."""
        return self._chat(prompt, max_tokens=800)
