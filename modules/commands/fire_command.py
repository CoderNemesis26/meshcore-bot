#!/usr/bin/env python3
"""List active Watch Duty fires or show one fire's details."""

from __future__ import annotations

import asyncio
from typing import Any

from .. import watchduty_poll
from ..models import MeshMessage
from .base_command import BaseCommand


class FireCommand(BaseCommand):
    """Interactive Watch Duty fire list / detail command."""

    name = "fire"
    keywords = ["fire"]
    description = "List active fires or show details (usage: fire [list #|Watch Duty id|name])"
    category = "info"
    requires_internet = True
    cooldown_seconds = 5

    short_description = "List active fires or show one fire's details"
    usage = "fire [list #|Watch Duty id|name]"
    examples = ["fire", "fire 1", "fire 93683", "fire Woods Fire"]
    parameters = [
        {
            "name": "query",
            "description": "Optional list number, Watch Duty id, or fire name",
        }
    ]

    settings_schema = [
        {
            "key": "enabled",
            "label": "Enabled",
            "type": "bool",
            "default": False,
            "help": "Enable the fire command (opt-in).",
        },
        {
            "key": "include_prescribed",
            "label": "Include prescribed burns",
            "type": "bool",
            "default": False,
            "help": "When true, include prescribed burns in fire listings.",
        },
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._enabled = self.get_config_value(
            "Fire_Command", "enabled", fallback=False, value_type="bool"
        )
        # Prefer command-local override; fall back to WatchDuty_Service shared setting.
        include = self.get_config_value(
            "Fire_Command", "include_prescribed", fallback=None, value_type="bool"
        )
        if include is None:
            include = self.get_config_value(
                "WatchDuty_Service",
                "include_prescribed",
                fallback=False,
                value_type="bool",
            )
        self._include_prescribed = bool(include)

    def can_execute(self, message: MeshMessage) -> bool:
        if not self._enabled:
            return False
        return super().can_execute(message)

    def _args_tail(self, message: MeshMessage) -> str:
        # execute() sees content already prefix/mention-cleaned by keyword matching.
        parts = message.content.strip().split(None, 1)
        if not parts:
            return ""
        kws = {x.lower() for x in self.keywords}
        if parts[0].lower() not in kws:
            return ""
        return parts[1].strip() if len(parts) > 1 else ""

    async def execute(self, message: MeshMessage) -> bool:
        tail = self._args_tail(message)
        try:
            events = await asyncio.to_thread(
                watchduty_poll.fetch_active_geo_events_for_user_query,
                self.bot.config,
                include_prescribed=self._include_prescribed,
            )
        except Exception as e:
            self.logger.error("fire command: fetch failed: %s", e)
            return await self.send_response(
                message, "Could not load fires (Watch Duty)."
            )

        if not tail:
            if not events:
                return await self.send_response(
                    message, "No active fires match the current filter."
                )
            max_len = self.get_max_message_length(message)
            lines = [f"Active fires ({len(events)}):"]
            for i, event in enumerate(events, start=1):
                name = (event.get("name") or f"Event {event.get('id')}").strip()
                loc = watchduty_poll.format_location_short(event)
                eid = event.get("id")
                id_part = f" · {eid}" if eid is not None else ""
                lines.append(f"{i}. {name} ({loc}){id_part}")
            chunks = watchduty_poll.mesh_pack_lines(lines, max_len)
            if len(chunks) == 1:
                return await self.send_response(message, chunks[0])
            return await self.send_response_chunked(message, chunks)

        event, err = await asyncio.to_thread(
            watchduty_poll.resolve_active_event_by_query,
            events,
            tail,
            config=self.bot.config,
            include_prescribed=self._include_prescribed,
        )
        if err:
            if err == "usage":
                return await self.send_response(
                    message,
                    "Usage: fire [list #|Watch Duty id|name] — "
                    "ids match app.watchduty.org/i/<id>.",
                )
            return await self.send_response(message, err)

        assert event is not None
        eid = event.get("id")
        if eid is None:
            return await self.send_response(message, "Invalid event (missing id).")

        detail = await asyncio.to_thread(watchduty_poll.fetch_event_detail, int(eid))
        if not detail:
            detail = event
        name = (detail.get("name") or f"Event {eid}").strip()
        acres = watchduty_poll.get_event_acres(detail)
        acres_s = f"{acres:g} ac" if acres is not None else "acres: unknown"
        containment = watchduty_poll.format_containment_display(detail)
        location = watchduty_poll.format_location(detail)
        evac_count = watchduty_poll.evacuation_display_count(detail)

        lines = [
            name,
            f"{acres_s} | {containment} contained",
            f"{location}",
            f"evacs: {evac_count}",
        ]
        max_len = self.get_max_message_length(message)
        chunks = watchduty_poll.mesh_pack_lines(lines, max_len)
        if len(chunks) == 1:
            return await self.send_response(message, chunks[0])
        return await self.send_response_chunked(message, chunks)
