#!/usr/bin/env python3
"""Regression: disabled built-in must not block another command's alias.

Reproduces the report where [Test_Command] aliases = path, p with
[Path_Command] enabled = false still claimed !path and sent no reply.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.path_command import PathCommand
from modules.commands.test_command import TestCommand as MeshTestCommand
from tests.conftest import mock_message
from tests.test_command_manager import make_manager
from tests.unit.test_command_path_byte_gating import _base_bot


@pytest.mark.unit
def test_test_command_aliases_match_path_and_p():
    bot = _base_bot()
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.set("Test_Command", "aliases", "path, p")

    cmd = MeshTestCommand(bot)
    assert "path" in cmd.keywords
    assert "p" in cmd.keywords
    assert cmd.matches_keyword(mock_message(content="!path", is_dm=True)) is True
    assert cmd.matches_keyword(mock_message(content="path", is_dm=True)) is True
    assert cmd.matches_keyword(mock_message(content="p", is_dm=True)) is True
    assert cmd.matches_keyword(mock_message(content="test", is_dm=True)) is True
    assert cmd.matches_keyword(mock_message(content="ping", is_dm=True)) is False


@pytest.mark.unit
def test_check_keywords_prefers_test_alias_when_path_disabled():
    bot = _base_bot()
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "enabled", "false")
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.set("Test_Command", "aliases", "path, p")
    bot.config.set("Test_Command", "response_format", "ack-from-test")

    path_cmd = PathCommand(bot)
    test_cmd = MeshTestCommand(bot)
    # path before test — same claim order as the user report
    manager = make_manager(bot, commands={"path": path_cmd, "test": test_cmd})

    matches = manager.check_keywords(mock_message(content="!path", is_dm=True))
    assert matches == [("test", "ack-from-test")]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_execute_commands_skips_disabled_and_runs_next():
    bot = _base_bot()
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "enabled", "false")

    path_cmd = PathCommand(bot)
    path_cmd.should_execute = Mock(return_value=True)
    path_cmd.get_response_format = Mock(return_value=None)
    path_cmd.execute = AsyncMock(return_value=True)

    other = MagicMock()
    other.is_channel_allowed = Mock(return_value=True)
    other.should_execute = Mock(return_value=True)
    other.get_response_format = Mock(return_value=None)
    other.can_execute_now = Mock(return_value=True)
    other.requires_internet = False
    other.cooldown_seconds = 0
    other.execute = AsyncMock(return_value=True)
    other._record_execution = Mock()
    other.last_response = None

    manager = make_manager(bot, commands={"path": path_cmd, "other": other})
    manager.send_response = AsyncMock(return_value=True)

    await manager.execute_commands(mock_message(content="!path", is_dm=True))

    path_cmd.execute.assert_not_called()
    other.execute.assert_awaited_once()
