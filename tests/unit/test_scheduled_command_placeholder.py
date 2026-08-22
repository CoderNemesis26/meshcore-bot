#!/usr/bin/env python3
"""
Unit tests for {cmd:...} placeholders in scheduled messages.

The contract that matters most: rendering a command for its text must never put
anything on the air. Everything else (unknown command, disabled command, admin
command, timeout, failure) degrades to an empty substitution rather than leaking
raw placeholder text into a broadcast.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.models import MeshMessage


class _Recorder:
    """Stands in for the pieces of CommandManager the render path touches."""

    def __init__(self):
        self.sent_dms = []
        self.sent_channel = []


def _make_manager(bot, commands):
    from modules.command_manager import CommandManager

    mgr = object.__new__(CommandManager)
    mgr.bot = bot
    mgr.logger = MagicMock()
    mgr.commands = commands
    mgr._last_response = None
    return mgr


def _make_command(*, name, keywords, reply, enabled=True, admin=False, delay=0.0):
    cmd = MagicMock()
    cmd.name = name
    cmd.keywords = keywords
    cmd._derive_config_section_name.return_value = f"{name.title()}_Command"
    cmd.get_config_value.return_value = enabled
    cmd.requires_admin_access.return_value = admin

    async def execute(message):
        if delay:
            await asyncio.sleep(delay)
        if reply is not None:
            message.capture_sink.append(reply)
        return True

    cmd.execute = execute
    return cmd


@pytest.mark.unit
class TestRenderCommandOutput:
    @pytest.mark.asyncio
    async def test_returns_reply_text(self, mock_bot):
        wx = _make_command(name="wx", keywords=["wx", "weather"], reply="Seattle: 12C rain")
        mgr = _make_manager(mock_bot, {"wx": wx})
        assert await mgr.render_command_output("wx Seattle") == "Seattle: 12C rain"

    @pytest.mark.asyncio
    async def test_resolves_by_alias_keyword(self, mock_bot):
        wx = _make_command(name="wx", keywords=["wx", "weather"], reply="ok")
        mgr = _make_manager(mock_bot, {"wx": wx})
        assert await mgr.render_command_output("weather Tacoma") == "ok"

    @pytest.mark.asyncio
    async def test_passes_full_spec_as_message_content(self, mock_bot):
        seen = {}

        async def execute(message):
            seen['content'] = message.content
            seen['capture'] = message.capture_sink is not None
            message.capture_sink.append("x")
            return True

        wx = _make_command(name="wx", keywords=["wx"], reply="x")
        wx.execute = execute
        mgr = _make_manager(mock_bot, {"wx": wx})
        await mgr.render_command_output("wx Seattle 98101")
        assert seen['content'] == "wx Seattle 98101"
        assert seen['capture'] is True

    @pytest.mark.asyncio
    async def test_unknown_command_returns_none(self, mock_bot):
        mgr = _make_manager(mock_bot, {})
        assert await mgr.render_command_output("nope arg") is None

    @pytest.mark.asyncio
    async def test_disabled_command_returns_none(self, mock_bot):
        wx = _make_command(name="wx", keywords=["wx"], reply="ok", enabled=False)
        mgr = _make_manager(mock_bot, {"wx": wx})
        assert await mgr.render_command_output("wx") is None

    @pytest.mark.asyncio
    async def test_admin_command_is_refused(self, mock_bot):
        adm = _make_command(name="admin", keywords=["admin"], reply="secret", admin=True)
        mgr = _make_manager(mock_bot, {"admin": adm})
        assert await mgr.render_command_output("admin reboot") is None

    @pytest.mark.asyncio
    async def test_non_renderable_command_is_refused(self, mock_bot):
        """announcements transmits directly, so rendering it would broadcast for real."""
        ann = _make_command(name="announcements", keywords=["announce"], reply="hi")
        mgr = _make_manager(mock_bot, {"announcements": ann})
        assert await mgr.render_command_output("announce hi") is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, mock_bot):
        slow = _make_command(name="slow", keywords=["slow"], reply="late", delay=0.5)
        mgr = _make_manager(mock_bot, {"slow": slow})
        assert await mgr.render_command_output("slow", timeout=0.05) is None

    @pytest.mark.asyncio
    async def test_raising_command_returns_none(self, mock_bot):
        boom = _make_command(name="boom", keywords=["boom"], reply=None)

        async def explode(message):
            raise RuntimeError("kaboom")

        boom.execute = explode
        mgr = _make_manager(mock_bot, {"boom": boom})
        assert await mgr.render_command_output("boom") is None

    @pytest.mark.asyncio
    async def test_silent_command_returns_none(self, mock_bot):
        quiet = _make_command(name="quiet", keywords=["quiet"], reply=None)
        mgr = _make_manager(mock_bot, {"quiet": quiet})
        assert await mgr.render_command_output("quiet") is None

    @pytest.mark.asyncio
    async def test_multiple_sends_are_joined(self, mock_bot):
        multi = _make_command(name="multi", keywords=["multi"], reply=None)

        async def two_parts(message):
            message.capture_sink.append("part one")
            message.capture_sink.append("part two")
            return True

        multi.execute = two_parts
        mgr = _make_manager(mock_bot, {"multi": multi})
        assert await mgr.render_command_output("multi") == "part one\npart two"


@pytest.mark.unit
class TestCaptureSuppressesTransmission:
    @pytest.mark.asyncio
    async def test_send_response_captures_instead_of_sending(self, mock_bot):
        """The whole point: a rendered command must not spend airtime."""
        from modules.command_manager import CommandManager

        mgr = object.__new__(CommandManager)
        mgr.bot = mock_bot
        mgr.logger = MagicMock()
        mgr._last_response = "previous real response"
        mgr.send_dm = AsyncMock()
        mgr.send_channel_message = AsyncMock()

        sink = []
        msg = MeshMessage(content="wx", channel="#general", is_dm=False, capture_sink=sink)

        assert await CommandManager.send_response(mgr, msg, "rendered text") is True
        assert sink == ["rendered text"]
        mgr.send_dm.assert_not_called()
        mgr.send_channel_message.assert_not_called()
        # A background render must not clobber the response captured for a real command.
        assert mgr._last_response == "previous real response"


def _make_scheduler(bot, render_result):
    from modules.scheduler import MessageScheduler

    sched = object.__new__(MessageScheduler)
    sched.bot = bot
    sched.logger = MagicMock()
    bot.command_manager = MagicMock()
    if isinstance(render_result, Exception):
        bot.command_manager.render_command_output = AsyncMock(side_effect=render_result)
    elif callable(render_result):
        bot.command_manager.render_command_output = AsyncMock(side_effect=render_result)
    else:
        bot.command_manager.render_command_output = AsyncMock(return_value=render_result)
    return sched


@pytest.mark.unit
class TestExpandCommandPlaceholders:
    def test_detects_placeholder(self, mock_bot):
        sched = _make_scheduler(mock_bot, "x")
        assert sched._has_command_placeholders("Forecast: {cmd:wx Seattle}") is True
        assert sched._has_command_placeholders("no placeholders here") is False

    @pytest.mark.asyncio
    async def test_substitutes_output_in_place(self, mock_bot):
        sched = _make_scheduler(mock_bot, "12C rain")
        out = await sched._expand_command_placeholders("Today: {cmd:wx Seattle}", "#general")
        assert out == "Today: 12C rain"

    @pytest.mark.asyncio
    async def test_passes_channel_and_spec(self, mock_bot):
        sched = _make_scheduler(mock_bot, "ok")
        await sched._expand_command_placeholders("{cmd:wx Seattle 98101}", "#weather")
        kwargs = mock_bot.command_manager.render_command_output.call_args
        assert kwargs[0][0] == "wx Seattle 98101"
        assert kwargs[1]["channel"] == "#weather"

    @pytest.mark.asyncio
    async def test_expands_several_placeholders(self, mock_bot):
        replies = iter(["sunny", "3 contacts"])
        sched = _make_scheduler(mock_bot, lambda *a, **k: next(replies))
        out = await sched._expand_command_placeholders(
            "wx={cmd:wx} net={cmd:stats}", "#general"
        )
        assert out == "wx=sunny net=3 contacts"

    @pytest.mark.asyncio
    async def test_failed_render_leaves_no_raw_placeholder(self, mock_bot):
        sched = _make_scheduler(mock_bot, None)
        out = await sched._expand_command_placeholders("Today: {cmd:bogus}", "#general")
        assert "{cmd:" not in out
        assert out == "Today: "

    @pytest.mark.asyncio
    async def test_raising_render_is_contained(self, mock_bot):
        sched = _make_scheduler(mock_bot, RuntimeError("boom"))
        out = await sched._expand_command_placeholders("A {cmd:wx} B", "#general")
        assert out == "A  B"

    @pytest.mark.asyncio
    async def test_command_output_is_not_rescanned(self, mock_bot):
        """A reply containing {cmd:...} must not trigger another render."""
        sched = _make_scheduler(mock_bot, "look: {cmd:wx}")
        out = await sched._expand_command_placeholders("{cmd:wx}", "#general")
        assert out == "look: {cmd:wx}"
        assert mock_bot.command_manager.render_command_output.await_count == 1


@pytest.mark.unit
class TestRenderWithRealCommand:
    """End-to-end through a real command's plumbing, not a mocked execute()."""

    @pytest.mark.asyncio
    async def test_real_ping_command_renders_without_transmitting(self, command_mock_bot):
        from modules.command_manager import CommandManager
        from modules.commands.ping_command import PingCommand

        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")

        ping = PingCommand(command_mock_bot)

        mgr = object.__new__(CommandManager)
        mgr.bot = command_mock_bot
        mgr.logger = MagicMock()
        mgr.commands = {"ping": ping}
        mgr._last_response = None
        mgr.send_dm = AsyncMock()
        mgr.send_channel_message = AsyncMock()
        # The real command calls self.bot.command_manager.send_response(...)
        command_mock_bot.command_manager.send_response = (
            lambda message, content, **kw: CommandManager.send_response(mgr, message, content, **kw)
        )

        rendered = await mgr.render_command_output("ping", channel="#general")

        assert rendered, "a real ping should produce text"
        mgr.send_dm.assert_not_called()
        mgr.send_channel_message.assert_not_called()
