import asyncio
import time

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import discord_bot


@pytest.fixture(autouse=True)
def reset_state():
    discord_bot._histories.clear()
    discord_bot._in_flight.clear()
    yield
    discord_bot._histories.clear()
    discord_bot._in_flight.clear()


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------

class TestHistory:
    def test_get_history_empty_for_new_session(self):
        assert discord_bot.get_history(1, 1) == []

    def test_save_and_get_roundtrip(self):
        msgs = [{"role": "user", "content": "hello"}]
        discord_bot.save_history(1, 1, msgs)
        assert discord_bot.get_history(1, 1) == msgs

    def test_get_history_returns_expired_as_empty(self):
        discord_bot._histories[(1, 1)] = {
            "messages": [{"role": "user", "content": "old"}],
            "last_active": time.time() - discord_bot.CONVERSATION_TIMEOUT - 1,
        }
        assert discord_bot.get_history(1, 1) == []

    def test_save_history_caps_at_max_messages(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(30)]
        discord_bot.save_history(1, 1, msgs)
        stored = discord_bot.get_history(1, 1)
        assert len(stored) == discord_bot.MAX_HISTORY_MESSAGES
        # Should keep the tail (most recent)
        assert stored[0]["content"] == "10"
        assert stored[-1]["content"] == "29"

    def test_save_history_prunes_stale_entries(self):
        stale_time = time.time() - discord_bot.CONVERSATION_TIMEOUT * 3
        discord_bot._histories[(99, 99)] = {"messages": [], "last_active": stale_time}
        discord_bot.save_history(1, 1, [{"role": "user", "content": "new"}])
        assert (99, 99) not in discord_bot._histories

    def test_different_sessions_are_isolated(self):
        discord_bot.save_history(1, 1, [{"role": "user", "content": "user-a"}])
        discord_bot.save_history(1, 2, [{"role": "user", "content": "user-b"}])
        assert discord_bot.get_history(1, 1)[0]["content"] == "user-a"
        assert discord_bot.get_history(1, 2)[0]["content"] == "user-b"

    def test_clear_history_removes_session(self):
        discord_bot.save_history(1, 1, [{"role": "user", "content": "hi"}])
        discord_bot.clear_history(1, 1)
        assert discord_bot.get_history(1, 1) == []

    def test_has_active_conversation_true(self):
        discord_bot.save_history(1, 1, [{"role": "user", "content": "hi"}])
        assert discord_bot.has_active_conversation(1, 1) is True

    def test_has_active_conversation_false_when_no_history(self):
        assert discord_bot.has_active_conversation(1, 1) is False

    def test_has_active_conversation_false_when_expired(self):
        discord_bot._histories[(1, 1)] = {
            "messages": [{"role": "user", "content": "old"}],
            "last_active": time.time() - discord_bot.CONVERSATION_TIMEOUT - 1,
        }
        assert discord_bot.has_active_conversation(1, 1) is False


# ---------------------------------------------------------------------------
# coach_first_message
# ---------------------------------------------------------------------------

class TestCoachFirstMessage:
    def test_prepends_riot_id_when_present(self):
        result = discord_bot.coach_first_message("Faker#KR1", "how am I doing?")
        assert "Faker#KR1" in result
        assert "how am I doing?" in result

    def test_returns_query_unchanged_when_no_riot_id(self):
        assert discord_bot.coach_first_message(None, "hello") == "hello"

    def test_riot_id_appears_before_query(self):
        result = discord_bot.coach_first_message("Player#TAG", "my question")
        assert result.index("Player#TAG") < result.index("my question")


# ---------------------------------------------------------------------------
# _send_long
# ---------------------------------------------------------------------------

class TestSendLong:
    @pytest.mark.asyncio
    async def test_short_message_sent_as_single_chunk(self):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        await discord_bot._send_long(ctx, "Hello, world!")
        ctx.send.assert_called_once_with("Hello, world!")

    @pytest.mark.asyncio
    async def test_long_message_split_into_chunks(self):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        long_text = "x" * 4000
        await discord_bot._send_long(ctx, long_text)
        # 4000 chars / 1900 per chunk = 3 chunks
        assert ctx.send.call_count == 3

    @pytest.mark.asyncio
    async def test_chunk_size_does_not_exceed_limit(self):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        await discord_bot._send_long(ctx, "y" * 5700)
        for call in ctx.send.call_args_list:
            assert len(call.args[0]) <= 1900

    @pytest.mark.asyncio
    async def test_chunks_reconstruct_original_text(self):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        original = "ab" * 2000
        await discord_bot._send_long(ctx, original)
        reconstructed = "".join(call.args[0] for call in ctx.send.call_args_list)
        assert reconstructed == original


# ---------------------------------------------------------------------------
# _call_claude retry logic
# ---------------------------------------------------------------------------

def _make_api_status_error(status_code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return anthropic.APIStatusError("error", response=response, body=None)


class TestCallClaudeRetry:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self):
        loop = asyncio.get_running_loop()
        mock_response = MagicMock(stop_reason="end_turn", content=[])

        with patch.object(discord_bot.analyzer.client.messages, "create", return_value=mock_response):
            result = await discord_bot._call_claude(loop, [{"role": "user", "content": "hi"}])

        assert result is mock_response

    @pytest.mark.asyncio
    async def test_retries_on_529_overloaded(self):
        loop = asyncio.get_running_loop()
        mock_response = MagicMock(stop_reason="end_turn", content=[])
        call_count = 0

        def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise _make_api_status_error(529)
            return mock_response

        with patch.object(discord_bot.analyzer.client.messages, "create", side_effect=fake_create):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await discord_bot._call_claude(loop, [])

        assert call_count == 2
        assert result is mock_response

    @pytest.mark.asyncio
    async def test_retries_on_429_rate_limit(self):
        loop = asyncio.get_running_loop()
        mock_response = MagicMock(stop_reason="end_turn", content=[])
        call_count = 0

        def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise _make_api_status_error(429)
            return mock_response

        with patch.object(discord_bot.analyzer.client.messages, "create", side_effect=fake_create):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await discord_bot._call_claude(loop, [])

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries_exhausted(self):
        loop = asyncio.get_running_loop()

        with patch.object(
            discord_bot.analyzer.client.messages,
            "create",
            side_effect=_make_api_status_error(529),
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(anthropic.APIStatusError):
                    await discord_bot._call_claude(loop, [])

    @pytest.mark.asyncio
    async def test_does_not_retry_on_400_bad_request(self):
        loop = asyncio.get_running_loop()
        call_count = 0

        def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            raise _make_api_status_error(400)

        with patch.object(discord_bot.analyzer.client.messages, "create", side_effect=fake_create):
            with pytest.raises(anthropic.APIStatusError):
                await discord_bot._call_claude(loop, [])

        assert call_count == 1  # no retries for non-transient errors

    @pytest.mark.asyncio
    async def test_retry_uses_backoff_delays(self):
        loop = asyncio.get_running_loop()
        slept = []

        async def fake_sleep(delay):
            slept.append(delay)

        mock_response = MagicMock(stop_reason="end_turn", content=[])
        call_count = 0

        def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _make_api_status_error(529)
            return mock_response

        with patch.object(discord_bot.analyzer.client.messages, "create", side_effect=fake_create):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await discord_bot._call_claude(loop, [])

        assert slept == [5, 15]  # first two delays from [5, 15, 30]


# ---------------------------------------------------------------------------
# run_coach concurrency guard
# ---------------------------------------------------------------------------

class TestRunCoachConcurrencyGuard:
    @pytest.mark.asyncio
    async def test_blocks_duplicate_requests(self):
        ctx = MagicMock()
        ctx.channel.id = 1
        ctx.author.id = 1
        ctx.author.__str__ = lambda self: "TestUser"
        ctx.send = AsyncMock()

        session_key = (1, 1)
        discord_bot._in_flight.add(session_key)

        await discord_bot.run_coach(ctx, "duplicate question")

        ctx.send.assert_called_once()
        assert "wait" in ctx.send.call_args.args[0].lower()
