#!/usr/bin/env python3
"""Unit tests for Watch Duty poll helpers."""

from __future__ import annotations

import configparser

import pytest

from modules import watchduty_poll


def test_strip_html_and_first_sentence():
    plain = watchduty_poll.strip_html("<p>Hello <b>world</b>.</p> More text!")
    assert "Hello" in plain and "world" in plain
    assert watchduty_poll.first_sentence("Hello world. More text!") == "Hello world."


def test_indicates_forward_progress_stopped():
    assert watchduty_poll.indicates_forward_progress_stopped(
        "Forward progress has stopped on the north flank."
    )
    assert not watchduty_poll.indicates_forward_progress_stopped(
        "Crews continue to make forward progress."
    )


def test_event_meets_min_acres():
    event = {"data": {"acres": 5}}
    assert watchduty_poll.event_meets_min_acres(event, min_acres=1.0)
    assert not watchduty_poll.event_meets_min_acres(event, min_acres=10.0)
    assert not watchduty_poll.event_meets_min_acres({"data": {}}, min_acres=1.0)


def test_feed_state_changed():
    assert watchduty_poll.feed_state_changed(None, 10.0, "50")
    assert watchduty_poll.feed_state_changed((10.0, "50"), 11.0, "50")
    assert watchduty_poll.feed_state_changed((10.0, "50"), 10.0, "60")
    assert not watchduty_poll.feed_state_changed((10.0, "50"), 10.0, "50")


def test_format_feed_summary_keeps_url():
    msg = watchduty_poll.format_feed_summary_message(
        "Woods Fire",
        1200.0,
        "20%",
        "Near Somewhere Very Long Place Name That Should Truncate",
        93683,
        max_len=136,
    )
    assert "https://app.watchduty.org/i/93683" in msg
    assert len(msg) <= 136


def test_format_report_message_keeps_url():
    msg = watchduty_poll.format_report_message(
        "Woods Fire",
        "The Red Cross is staffing a care and reception center at the community hall.",
        event_id=93683,
        max_len=136,
    )
    assert msg.endswith("https://app.watchduty.org/i/93683") or "watchduty.org/i/93683" in msg
    assert len(msg) <= 136


def test_mesh_pack_lines():
    chunks = watchduty_poll.mesh_pack_lines(["a", "b", "c" * 50], max_len=20)
    assert all(len(c) <= 20 for c in chunks)
    assert chunks[0] == "a\nb" or chunks[0].startswith("a")


def test_bbox_and_min_acres_from_config():
    cfg = configparser.ConfigParser()
    cfg.add_section("WatchDuty_Service")
    cfg.set("WatchDuty_Service", "bbox", "32.0,-120.5,35.5,-114.0")
    cfg.set("WatchDuty_Service", "min_acres", "2.5")
    assert watchduty_poll.watchduty_bbox_from_config(cfg) == (
        32.0,
        -120.5,
        35.5,
        -114.0,
    )
    assert watchduty_poll.watchduty_min_acres_from_config(cfg) == 2.5


def test_resolve_active_event_by_list_index():
    events = [
        {"id": 10, "name": "Alpha Fire"},
        {"id": 20, "name": "Beta Fire"},
    ]
    event, err = watchduty_poll.resolve_active_event_by_query(events, "2")
    assert err is None
    assert event is not None
    assert event["id"] == 20


def test_resolve_active_event_by_name():
    events = [
        {"id": 10, "name": "Alpha Fire"},
        {"id": 20, "name": "Beta Fire"},
    ]
    event, err = watchduty_poll.resolve_active_event_by_query(events, "beta")
    assert err is None
    assert event is not None
    assert event["id"] == 20


def test_evac_display_lines():
    geo = {
        "data": {
            "evacuation_orders": ["Leave zone A now."],
            "evacuation_warnings": ["Prepare zone B."],
        }
    }
    lines = watchduty_poll.evacuation_display_lines(geo)
    assert lines == ["[Order] Leave zone A now.", "[Warn] Prepare zone B."]
    assert watchduty_poll.incident_has_evac_info(geo)


@pytest.mark.asyncio
async def test_watchduty_service_skips_start_without_modes():
    from modules.service_plugins.watchduty_service import WatchDutyService

    cfg = configparser.ConfigParser()
    cfg.add_section("WatchDuty_Service")
    cfg.set("WatchDuty_Service", "enabled", "true")
    cfg.set("WatchDuty_Service", "channel", "")
    cfg.set("WatchDuty_Service", "poll_api", "false")

    class FakeBot:
        config = cfg
        logger = type(
            "L",
            (),
            {
                "info": lambda *a, **k: None,
                "warning": lambda *a, **k: None,
                "error": lambda *a, **k: None,
                "debug": lambda *a, **k: None,
            },
        )()

    svc = WatchDutyService(FakeBot())
    svc.enabled = True
    await svc.start()
    assert svc._poll_task is None
    assert not svc._running


@pytest.mark.asyncio
async def test_watchduty_service_poll_sends_feed_and_first_report(tmp_path):
    import asyncio
    import contextlib
    import sqlite3

    from modules.db_migrations import MigrationRunner
    from modules.service_plugins.watchduty_service import WatchDutyService

    class NullLogger:
        def info(self, *a, **k):
            return None

        def warning(self, *a, **k):
            return None

        def error(self, *a, **k):
            return None

        def debug(self, *a, **k):
            return None

    db_path = tmp_path / "wd.db"
    conn = sqlite3.connect(db_path)
    MigrationRunner(conn, logger=NullLogger()).run()
    conn.close()

    cfg = configparser.ConfigParser()
    cfg.add_section("WatchDuty_Service")
    cfg.set("WatchDuty_Service", "enabled", "true")
    cfg.set("WatchDuty_Service", "channel", "#fires")
    cfg.set("WatchDuty_Service", "poll_api", "true")
    cfg.set("WatchDuty_Service", "min_acres", "1")
    cfg.set("WatchDuty_Service", "stop_alerts_when_forward_progress_stops", "false")

    sent: list[tuple[str, str]] = []

    class FakeCM:
        async def send_channel_message(self, channel, text, **kwargs):
            sent.append((channel, text))
            return True

    class MiniDB:
        def __init__(self, path):
            self.db_path = str(path)

        @contextlib.contextmanager
        def connection(self):
            c = sqlite3.connect(self.db_path, timeout=30.0)
            try:
                yield c
            finally:
                c.close()

    class FakeBot:
        config = cfg
        connected = True
        command_manager = FakeCM()
        db_manager = MiniDB(db_path)
        bot_tx_rate_limiter = type("R", (), {"seconds": 0})()
        logger = type(
            "L",
            (),
            {
                "info": lambda *a, **k: None,
                "warning": lambda *a, **k: None,
                "error": lambda *a, **k: None,
                "debug": lambda *a, **k: None,
            },
        )()

    events = [
        {
            "id": 42,
            "name": "Test Fire",
            "is_active": True,
            "address": "Somewhere, CA",
            "lat": 34.0,
            "lng": -118.0,
            "data": {"acres": 100, "containment": 10, "is_prescribed": False},
        }
    ]
    reports = [
        {
            "id": 1,
            "message": "<p>First report.</p>",
            "date_created": "2026-01-01T00:00:00Z",
        },
        {
            "id": 2,
            "message": "<p>Second report.</p>",
            "date_created": "2026-01-02T00:00:00Z",
        },
    ]

    svc = WatchDutyService(FakeBot())

    async def fake_fetch_events():
        return events

    async def fake_fetch_reports(_eid):
        return reports

    async def nop_pause():
        return None

    svc._fetch_events = fake_fetch_events  # type: ignore[method-assign]
    svc._fetch_reports = fake_fetch_reports  # type: ignore[method-assign]
    svc._tx_pause = nop_pause  # type: ignore[method-assign]

    await svc._poll_api_and_send()

    assert any("Test Fire" in text and "100" in text for _, text in sent)
    assert any("Second report" in text for _, text in sent)
    with MiniDB(db_path).connection() as c:
        rows = c.execute(
            "SELECT report_id FROM watchduty_sent_reports WHERE event_id = 42"
        ).fetchall()
        assert {r[0] for r in rows} == {1, 2}


def _make_watchduty_service(tmp_path, *, max_events=20, max_duration=90):
    import contextlib
    import sqlite3

    from modules.db_migrations import MigrationRunner
    from modules.service_plugins.watchduty_service import WatchDutyService

    class NullLogger:
        def info(self, *a, **k):
            return None

        def warning(self, *a, **k):
            return None

        def error(self, *a, **k):
            return None

        def debug(self, *a, **k):
            return None

    db_path = tmp_path / "wd_budget.db"
    conn = sqlite3.connect(db_path)
    MigrationRunner(conn, logger=NullLogger()).run()
    conn.close()

    cfg = configparser.ConfigParser()
    cfg.add_section("WatchDuty_Service")
    cfg.set("WatchDuty_Service", "enabled", "true")
    cfg.set("WatchDuty_Service", "channel", "#fires")
    cfg.set("WatchDuty_Service", "poll_api", "true")
    cfg.set("WatchDuty_Service", "min_acres", "1")
    cfg.set("WatchDuty_Service", "stop_alerts_when_forward_progress_stops", "false")
    cfg.set("WatchDuty_Service", "max_events_per_poll", str(max_events))
    cfg.set("WatchDuty_Service", "max_poll_duration_seconds", str(max_duration))

    sent: list[tuple[str, str]] = []

    class FakeCM:
        async def send_channel_message(self, channel, text, **kwargs):
            sent.append((channel, text))
            return True

    class MiniDB:
        def __init__(self, path):
            self.db_path = str(path)

        @contextlib.contextmanager
        def connection(self):
            c = sqlite3.connect(self.db_path, timeout=30.0)
            try:
                yield c
            finally:
                c.close()

    class FakeBot:
        config = cfg
        connected = True
        command_manager = FakeCM()
        db_manager = MiniDB(db_path)
        bot_tx_rate_limiter = type("R", (), {"seconds": 0})()
        logger = NullLogger()

    svc = WatchDutyService(FakeBot())

    async def nop_pause():
        return None

    svc._tx_pause = nop_pause  # type: ignore[method-assign]
    return svc, sent, MiniDB(db_path)


@pytest.mark.asyncio
async def test_watchduty_poll_respects_max_events_and_rotates(tmp_path):
    from modules.service_plugins.watchduty_service import WatchDutyService

    svc, _sent, _db = _make_watchduty_service(tmp_path, max_events=2)

    events = [
        {
            "id": i,
            "name": f"Fire {i}",
            "is_active": True,
            "address": "CA",
            "data": {"acres": 10, "containment": 0},
        }
        for i in (1, 2, 3)
    ]

    async def fake_fetch_events():
        return list(events)

    async def fake_fetch_reports(_eid):
        return [{"id": 1, "message": "Update.", "date_created": "2026-01-01T00:00:00Z"}]

    processed: list[int] = []

    async def track_process(event):
        processed.append(int(event["id"]))
        await WatchDutyService._process_event(svc, event)

    svc._fetch_events = fake_fetch_events  # type: ignore[method-assign]
    svc._fetch_reports = fake_fetch_reports  # type: ignore[method-assign]
    svc._process_event = track_process  # type: ignore[method-assign]

    await svc._poll_api_and_send()
    assert processed == [1, 2]
    assert svc._event_rotate_offset == 2

    processed.clear()
    await svc._poll_api_and_send()
    assert processed == [3, 1]
    assert svc._event_rotate_offset == 1


@pytest.mark.asyncio
async def test_watchduty_first_sync_marks_empty_reports(tmp_path):
    svc, sent, db = _make_watchduty_service(tmp_path)

    event = {
        "id": 7,
        "name": "Empty Fire",
        "is_active": True,
        "address": "CA",
        "data": {"acres": 10, "containment": 0},
    }

    async def fake_fetch_events():
        return [event]

    async def fake_fetch_reports(_eid):
        return [
            {"id": 1, "message": "<p>   </p>", "date_created": "2026-01-01T00:00:00Z"},
            {"id": 2, "message": "", "date_created": "2026-01-02T00:00:00Z"},
        ]

    svc._fetch_events = fake_fetch_events  # type: ignore[method-assign]
    svc._fetch_reports = fake_fetch_reports  # type: ignore[method-assign]

    await svc._poll_api_and_send()

    assert any("Empty Fire" in text for _, text in sent)
    assert not any("Empty Fire:" in text for _, text in sent)
    with db.connection() as c:
        rows = c.execute(
            "SELECT report_id FROM watchduty_sent_reports WHERE event_id = 7"
        ).fetchall()
        assert {r[0] for r in rows} == {1, 2}


@pytest.mark.asyncio
async def test_watchduty_incremental_marks_empty_report_without_send(tmp_path):
    svc, sent, db = _make_watchduty_service(tmp_path)

    with db.connection() as c:
        c.execute(
            "INSERT INTO watchduty_sent_reports (event_id, report_id, sent_at) "
            "VALUES (9, 1, '2026-01-01T00:00:00+00:00')"
        )
        c.commit()

    event = {
        "id": 9,
        "name": "Synced Fire",
        "is_active": True,
        "address": "CA",
        "data": {"acres": 10, "containment": 0},
    }

    async def fake_fetch_events():
        return [event]

    async def fake_fetch_reports(_eid):
        return [
            {"id": 1, "message": "Old.", "date_created": "2026-01-01T00:00:00Z"},
            {"id": 2, "message": "   ", "date_created": "2026-01-02T00:00:00Z"},
            {
                "id": 3,
                "message": "Real update.",
                "date_created": "2026-01-03T00:00:00Z",
            },
        ]

    svc._fetch_events = fake_fetch_events  # type: ignore[method-assign]
    svc._fetch_reports = fake_fetch_reports  # type: ignore[method-assign]

    await svc._poll_api_and_send()

    assert any("Real update" in text for _, text in sent)
    with db.connection() as c:
        rows = c.execute(
            "SELECT report_id FROM watchduty_sent_reports WHERE event_id = 9"
        ).fetchall()
        assert {r[0] for r in rows} == {1, 2, 3}
