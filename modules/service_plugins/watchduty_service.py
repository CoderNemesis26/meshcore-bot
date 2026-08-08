#!/usr/bin/env python3
"""
Watch Duty wildfire alert service for MeshCore Bot.

Polls the public Watch Duty API (and optionally a local roundup file) and posts
feed/report updates to configured mesh channels.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import watchduty_poll
from ..utils import resolve_path
from .base_service import BaseServicePlugin

_MESH_CHUNK_LEN = 136


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WatchDutyService(BaseServicePlugin):
    """Background Watch Duty file roundup + API poller."""

    config_section = "WatchDuty_Service"
    description = "Watch Duty wildfire feed and report alerts"

    settings_schema = [
        {
            "key": "channel",
            "label": "Default channel",
            "type": "str",
            "default": "general",
            "help": "Fallback channel for file roundup and API feed/report posts.",
            "required": True,
        },
        {
            "key": "feed_channel",
            "label": "Feed channel",
            "type": "str",
            "default": "",
            "help": "Optional channel for acreage/containment summaries. Falls back to channel.",
        },
        {
            "key": "report_channel",
            "label": "Report channel",
            "type": "str",
            "default": "",
            "help": "Optional channel for approved Watch Duty reports. Falls back to channel.",
        },
        {
            "key": "poll_api",
            "label": "Poll Watch Duty API",
            "type": "bool",
            "default": False,
            "help": "When true, poll the public Watch Duty API for active incidents.",
        },
        {
            "key": "check_interval_seconds",
            "label": "Poll interval",
            "type": "int",
            "min": 30,
            "default": 300,
            "unit": "s",
            "help": "How often to check the roundup file and/or API.",
        },
        {
            "key": "max_events_per_poll",
            "label": "Max events per poll",
            "type": "int",
            "min": 1,
            "default": 20,
            "help": "Cap incidents processed each API poll cycle (rotation continues next cycle).",
        },
        {
            "key": "max_poll_duration_seconds",
            "label": "Max poll duration",
            "type": "int",
            "min": 10,
            "default": 90,
            "unit": "s",
            "help": "Stop processing more incidents once this wall-clock budget is spent.",
        },
        {
            "key": "min_acres",
            "label": "Minimum acres",
            "type": "float",
            "min": 0,
            "default": 1.0,
            "help": "Only include active incidents with known acreage at or above this value.",
        },
        {
            "key": "bbox",
            "label": "Bounding box",
            "type": "str",
            "default": "",
            "help": "Optional lat_min,lng_min,lat_max,lng_max filter. Empty = all events.",
        },
        {
            "key": "output_file",
            "label": "Roundup file",
            "type": "str",
            "default": "",
            "help": "Optional path to a local watchduty roundup text file to relay when updated.",
        },
        {
            "key": "feed_initial_only",
            "label": "Feed initial only",
            "type": "bool",
            "default": False,
            "help": "When true, send only the first feed summary per incident.",
        },
        {
            "key": "stop_alerts_when_forward_progress_stops",
            "label": "Stop on no forward progress",
            "type": "bool",
            "default": True,
            "help": "Suppress further alerts for an incident once reports say forward progress stopped.",
        },
        {
            "key": "include_prescribed",
            "label": "Include prescribed burns",
            "type": "bool",
            "default": False,
            "help": "When true, include prescribed burns in API polling.",
        },
        {
            "key": "silence_mesh_output",
            "label": "Silence mesh output",
            "type": "bool",
            "default": False,
            "help": "When true, skip mesh channel posts (external webhooks still send if configured).",
        },
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        section = self.config_section

        self.channel = self.bot.config.get(section, "channel", fallback="general").strip()
        self.feed_channel = (
            self.bot.config.get(section, "feed_channel", fallback="") or ""
        ).strip() or self.channel
        self.report_channel = (
            self.bot.config.get(section, "report_channel", fallback="") or ""
        ).strip() or self.channel
        self.output_file = (
            self.bot.config.get(section, "output_file", fallback="") or ""
        ).strip()
        self.poll_api = self.bot.config.getboolean(section, "poll_api", fallback=False)
        self.check_interval_seconds = max(
            30,
            self.bot.config.getint(section, "check_interval_seconds", fallback=300),
        )
        self.max_events_per_poll = max(
            1,
            self.bot.config.getint(section, "max_events_per_poll", fallback=20),
        )
        # Keep the budget below the poll interval so cycles cannot starve the loop.
        default_budget = min(90, max(10, self.check_interval_seconds - 10))
        self.max_poll_duration_seconds = max(
            10,
            self.bot.config.getint(
                section, "max_poll_duration_seconds", fallback=default_budget
            ),
        )
        try:
            self.min_acres = self.bot.config.getfloat(section, "min_acres", fallback=1.0)
        except ValueError:
            self.min_acres = 1.0
        self.bbox = watchduty_poll.watchduty_bbox_from_config(self.bot.config)
        self.feed_initial_only = self.bot.config.getboolean(
            section, "feed_initial_only", fallback=False
        )
        self.stop_on_no_forward_progress = self.bot.config.getboolean(
            section, "stop_alerts_when_forward_progress_stops", fallback=True
        )
        self.include_prescribed = self.bot.config.getboolean(
            section, "include_prescribed", fallback=False
        )
        self.silence_mesh_output = self.bot.config.getboolean(
            section, "silence_mesh_output", fallback=False
        )

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        # Rotate through the filtered event list across polls so a large set is
        # eventually covered even when per-cycle budgets truncate work.
        self._event_rotate_offset = 0

        self.logger.info(
            "WatchDuty service initialized: channel=%s feed=%s report=%s poll_api=%s "
            "interval=%ss max_events=%s max_duration=%ss",
            self.channel,
            self.feed_channel,
            self.report_channel,
            self.poll_api,
            self.check_interval_seconds,
            self.max_events_per_poll,
            self.max_poll_duration_seconds,
        )

    def _file_mode_ready(self) -> bool:
        return bool(self.output_file and self.channel)

    def _api_mode_ready(self) -> bool:
        return bool(self.poll_api and (self.feed_channel or self.report_channel))

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("WatchDuty service is disabled, not starting")
            return
        if not self._file_mode_ready() and not self._api_mode_ready():
            self.logger.warning(
                "WatchDuty service enabled but neither output_file+channel nor "
                "poll_api with a channel is configured; not starting"
            )
            return
        self._running = True
        self.logger.info("Starting WatchDuty service")
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.logger.info("WatchDuty service started")

    async def stop(self) -> None:
        self._running = False
        self.logger.info("Stopping WatchDuty service")
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        self.logger.info("WatchDuty service stopped")

    async def _poll_loop(self) -> None:
        self.logger.info(
            "WatchDuty poll loop started (interval=%ss)",
            self.check_interval_seconds,
        )
        while self._running:
            try:
                if self._file_mode_ready():
                    await self._send_roundup_if_updated()
                if self._api_mode_ready():
                    await self._poll_api_and_send()
                await asyncio.sleep(self.check_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in WatchDuty poll loop: %s", e)
                await asyncio.sleep(60)

    async def _tx_pause(self) -> None:
        limiter = getattr(self.bot, "bot_tx_rate_limiter", None)
        seconds = getattr(limiter, "seconds", None)
        await asyncio.sleep(max(1.0, float(seconds) if seconds is not None else 1.0))

    async def _send_mesh(self, channel: str, text: str) -> bool:
        if not channel or not text:
            return False
        if self.silence_mesh_output:
            return True
        if not getattr(self.bot, "connected", False):
            return False
        cm = getattr(self.bot, "command_manager", None)
        if cm is None:
            return False
        return await cm.send_channel_message(
            channel,
            text,
            skip_user_rate_limit=True,
            scope=self.get_mesh_flood_scope(),
        )

    async def _notify(self, text: str) -> None:
        if not text:
            return
        try:
            await self.send_external_notifications(text)
        except Exception as e:
            self.logger.warning("WatchDuty external notify failed: %s", e)

    async def _send_roundup_if_updated(self) -> None:
        """Relay a local Watch Duty roundup file when its mtime advances."""
        base = getattr(self.bot, "bot_root", os.getcwd())
        path = Path(resolve_path(self.output_file, base))
        sent_marker = path.with_suffix(path.suffix + ".last_sent")
        try:
            if not path.exists():
                self.logger.debug("WatchDuty output file not found: %s", path)
                return
            mtime = path.stat().st_mtime
            last_sent_mtime = 0.0
            if sent_marker.exists():
                try:
                    last_sent_mtime = float(sent_marker.read_text(encoding="utf-8").strip())
                except (ValueError, OSError):
                    pass
            if mtime <= last_sent_mtime:
                return
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                return

            # Pack/truncate to mesh-safe chunks (handles overlong single lines).
            chunks = [
                c
                for c in watchduty_poll.mesh_pack_lines(
                    content.split("\n"), _MESH_CHUNK_LEN
                )
                if c.strip()
            ]
            if not chunks:
                return

            for i, chunk in enumerate(chunks):
                ok = await self._send_mesh(self.channel, chunk)
                if not ok and not self.silence_mesh_output:
                    self.logger.warning(
                        "WatchDuty: failed to send roundup chunk %s to %s",
                        i + 1,
                        self.channel,
                    )
                    return
                await self._notify(chunk)
                await self._tx_pause()

            sent_marker.write_text(str(mtime), encoding="utf-8")
            self.logger.info(
                "WatchDuty: sent %s roundup chunk(s) to %s",
                len(chunks),
                self.channel,
            )
        except Exception as e:
            self.logger.error("WatchDuty roundup error: %s", e)

    def _db(self) -> Any:
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            raise RuntimeError("db_manager unavailable")
        return db

    async def _db_call(self, fn: Any, *args: Any) -> Any:
        """Run a sync DB helper off the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args))

    def _is_suppressed(self, event_id: int) -> bool:
        with self._db().connection() as conn:
            row = conn.execute(
                "SELECT suppressed FROM watchduty_alert_suppression WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return bool(row and int(row[0]) == 1)

    def _set_suppressed(self, event_id: int, reason: str) -> None:
        with self._db().connection() as conn:
            conn.execute(
                """
                INSERT INTO watchduty_alert_suppression
                    (event_id, suppressed, reason, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    suppressed = 1,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (event_id, reason, _utc_now_iso()),
            )
            conn.commit()

    def _feed_row(self, event_id: int) -> Optional[tuple[Any, Any]]:
        with self._db().connection() as conn:
            row = conn.execute(
                "SELECT last_acres, last_containment FROM watchduty_feed_state WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            return (row[0], row[1])

    def _save_feed_state(self, event_id: int, acres: float, containment_sig: str) -> None:
        with self._db().connection() as conn:
            conn.execute(
                """
                INSERT INTO watchduty_feed_state
                    (event_id, last_acres, last_containment, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    last_acres = excluded.last_acres,
                    last_containment = excluded.last_containment,
                    updated_at = excluded.updated_at
                """,
                (event_id, acres, containment_sig, _utc_now_iso()),
            )
            conn.commit()

    def _sent_report_ids(self, event_id: int) -> set[int]:
        with self._db().connection() as conn:
            rows = conn.execute(
                "SELECT report_id FROM watchduty_sent_reports WHERE event_id = ?",
                (event_id,),
            ).fetchall()
            return {int(r[0]) for r in rows if r[0] is not None}

    def _mark_reports_sent(self, event_id: int, report_ids: list[int]) -> None:
        if not report_ids:
            return
        stamp = _utc_now_iso()
        with self._db().connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO watchduty_sent_reports
                    (event_id, report_id, sent_at)
                VALUES (?, ?, ?)
                """,
                [(event_id, rid, stamp) for rid in report_ids],
            )
            conn.commit()

    async def _fetch_events(self) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        events = await loop.run_in_executor(
            None,
            lambda: watchduty_poll.fetch_geo_events(bbox=self.bbox),
        )
        if not self.include_prescribed:
            events = [
                e for e in events if not (e.get("data") or {}).get("is_prescribed")
            ]
        events = [e for e in events if watchduty_poll.geo_event_is_active(e)]
        events = [
            e
            for e in events
            if watchduty_poll.event_meets_min_acres(e, min_acres=self.min_acres)
        ]
        return events

    async def _fetch_reports(self, event_id: int) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                lambda eid=event_id: watchduty_poll.fetch_reports(eid),
            )
        except Exception as e:
            self.logger.warning(
                "WatchDuty poll: fetch reports for event %s failed: %s",
                event_id,
                e,
            )
            return []

    async def _poll_api_and_send(self) -> None:
        try:
            events = await self._fetch_events()
        except Exception as e:
            self.logger.error("WatchDuty poll: fetch geo_events failed: %s", e)
            return

        if not events:
            self.logger.debug("WatchDuty poll cycle completed (no events)")
            return

        # Stable order so rotation is deterministic across polls.
        events.sort(key=lambda e: (str(e.get("name") or "").lower(), e.get("id") or 0))
        n = len(events)
        start = self._event_rotate_offset % n
        rotated = events[start:] + events[:start]

        deadline = asyncio.get_running_loop().time() + float(
            self.max_poll_duration_seconds
        )
        processed = 0
        stopped_early = False

        for event in rotated:
            if processed >= self.max_events_per_poll:
                stopped_early = True
                break
            if asyncio.get_running_loop().time() >= deadline:
                stopped_early = True
                break
            try:
                await self._process_event(event)
            except Exception as e:
                self.logger.error(
                    "WatchDuty poll: error processing event %s: %s",
                    event.get("id"),
                    e,
                )
            processed += 1

        # Advance rotation past what we attempted so the next cycle continues
        # from the first unprocessed event (or wraps when we finished the set).
        self._event_rotate_offset = (start + processed) % n

        if stopped_early:
            self.logger.info(
                "WatchDuty poll budget reached after %s/%s events "
                "(max_events=%s, max_duration=%ss); remaining deferred to next cycle",
                processed,
                n,
                self.max_events_per_poll,
                self.max_poll_duration_seconds,
            )
        else:
            self.logger.debug(
                "WatchDuty poll cycle completed (%s event(s))", processed
            )

    @staticmethod
    def _report_ids(reports: list[dict[str, Any]]) -> list[int]:
        ids: list[int] = []
        for report in reports:
            rid = report.get("id")
            if rid is None:
                continue
            try:
                ids.append(int(rid))
            except (TypeError, ValueError):
                continue
        return ids

    @staticmethod
    def _newest_report_with_text(
        reports: list[dict[str, Any]],
    ) -> tuple[Optional[dict[str, Any]], str]:
        """Return newest report that yields non-empty plain text (reports are oldest→newest)."""
        for report in reversed(reports):
            plain = watchduty_poll.first_sentence(
                watchduty_poll.strip_html(report.get("message") or "")
            )
            if plain:
                return report, plain
        return None, ""

    async def _process_event(self, event: dict[str, Any]) -> None:
        event_id = event.get("id")
        if event_id is None:
            return
        try:
            event_id_int = int(event_id)
        except (TypeError, ValueError):
            return

        name = (event.get("name") or "").strip() or f"Event {event_id_int}"
        acres = watchduty_poll.get_event_acres(event)
        if acres is None:
            return
        containment_sig = watchduty_poll.containment_key(event)
        containment_disp = watchduty_poll.format_containment_display(event)
        location = watchduty_poll.format_location(event)

        reports: list[dict[str, Any]] = []
        reports_loaded = False

        if self.stop_on_no_forward_progress:
            is_suppressed = await self._db_call(self._is_suppressed, event_id_int)
            if self.report_channel:
                reports = await self._fetch_reports(event_id_int)
                reports_loaded = True
                if reports:
                    latest_plain = watchduty_poll.strip_html(
                        reports[-1].get("message") or ""
                    )
                    if watchduty_poll.indicates_forward_progress_stopped(latest_plain):
                        if not is_suppressed:
                            await self._db_call(
                                self._set_suppressed,
                                event_id_int,
                                "forward_progress_stopped",
                            )
                            self.logger.info(
                                "WatchDuty poll: suppressing alerts for %s "
                                "(forward progress stopped)",
                                name,
                            )
                        return
            if is_suppressed:
                return

        if self.feed_channel:
            feed_row = await self._db_call(self._feed_row, event_id_int)
            should_send_feed = watchduty_poll.feed_state_changed(
                feed_row, acres, containment_sig
            )
            if self.feed_initial_only and feed_row is not None:
                should_send_feed = False
            if should_send_feed:
                msg_feed = watchduty_poll.format_feed_summary_message(
                    name, acres, containment_disp, location, event_id_int
                )
                ok = await self._send_mesh(self.feed_channel, msg_feed)
                if ok:
                    await self._db_call(
                        self._save_feed_state, event_id_int, acres, containment_sig
                    )
                    await self._notify(msg_feed)
                else:
                    self.logger.warning(
                        "WatchDuty poll: failed to send feed line for %s to %s",
                        name,
                        self.feed_channel,
                    )
                await self._tx_pause()

        if not self.report_channel:
            return

        if not reports_loaded:
            reports = await self._fetch_reports(event_id_int)
        if not reports:
            return

        sent_report_ids = await self._db_call(self._sent_report_ids, event_id_int)
        synced_reports = any(rid > 0 for rid in sent_report_ids)
        all_ids = self._report_ids(reports)

        if not synced_reports:
            _report, plain = self._newest_report_with_text(reports)
            if not plain:
                # No usable text in any report — advance state so we do not retry forever.
                if all_ids:
                    await self._db_call(self._mark_reports_sent, event_id_int, all_ids)
                    self.logger.debug(
                        "WatchDuty poll: marked %s empty report(s) sent for %s",
                        len(all_ids),
                        name,
                    )
                return
            msg_r = watchduty_poll.format_report_message(
                name, plain, event_id=event_id_int
            )
            ok = await self._send_mesh(self.report_channel, msg_r)
            if ok:
                await self._db_call(self._mark_reports_sent, event_id_int, all_ids)
                await self._notify(msg_r)
            else:
                self.logger.warning(
                    "WatchDuty poll: failed to send first report for %s to %s",
                    name,
                    self.report_channel,
                )
            await self._tx_pause()
            return

        for report in reports:
            report_id = report.get("id")
            if report_id is None:
                continue
            try:
                report_id_int = int(report_id)
            except (TypeError, ValueError):
                continue
            if report_id_int in sent_report_ids:
                continue
            plain = watchduty_poll.first_sentence(
                watchduty_poll.strip_html(report.get("message") or "")
            )
            if not plain:
                # Advance past empty reports so they do not block forever.
                await self._db_call(
                    self._mark_reports_sent, event_id_int, [report_id_int]
                )
                sent_report_ids.add(report_id_int)
                continue
            msg = watchduty_poll.format_report_message(
                name, plain, event_id=event_id_int
            )
            ok = await self._send_mesh(self.report_channel, msg)
            if ok:
                await self._db_call(
                    self._mark_reports_sent, event_id_int, [report_id_int]
                )
                sent_report_ids.add(report_id_int)
                await self._notify(msg)
            await self._tx_pause()
