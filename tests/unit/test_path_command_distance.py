#!/usr/bin/env python3
"""
Unit tests for the PathCommand {path_distance} placeholder.

Distance is summed sender -> each resolved hop -> bot, and is deliberately blank
whenever any node in that chain lacks usable coordinates, so a partial sum never
gets reported as the real distance travelled.
"""


import pytest

from modules.commands.path_command import PathCommand


@pytest.mark.unit
class TestPathCommandDistance:
    """_calculate_path_distance_km and its {path_distance} rendering."""

    @pytest.fixture
    def path_command(self, mock_bot):
        cmd = PathCommand(mock_bot)
        # Bot sits at the origin; sender and hops are placed east of it.
        cmd.bot_latitude = 47.0
        cmd.bot_longitude = -122.0
        cmd._get_sender_location = lambda: (47.0, -122.5)
        return cmd

    @staticmethod
    def _info(lat, lon, **over):
        base = {'found': True, 'collision': False, 'latitude': lat, 'longitude': lon}
        base.update(over)
        return base

    def test_sums_sender_through_hops_to_bot(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': self._info(47.0, -122.2)}
        km = path_command._calculate_path_distance_km(['AA', 'BB'], info)
        assert km is not None
        # Three legs spanning 0.5 deg of longitude at 47N (~75.9 km/deg) => ~38 km.
        assert 37.0 < km < 39.0

    def test_renders_with_km_suffix(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': self._info(47.0, -122.2)}
        path_command._last_path_distance_km = path_command._calculate_path_distance_km(
            ['AA', 'BB'], info
        )
        rendered = path_command._format_path_distance()
        assert rendered.endswith("km")
        assert rendered[0].isdigit()

    def test_blank_when_a_hop_has_no_coordinates(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': self._info(None, None)}
        assert path_command._calculate_path_distance_km(['AA', 'BB'], info) is None

    def test_blank_when_a_hop_is_a_prefix_collision(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': self._info(47.0, -122.2, collision=True)}
        assert path_command._calculate_path_distance_km(['AA', 'BB'], info) is None

    def test_blank_when_a_hop_is_unresolved(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': {'found': False}}
        assert path_command._calculate_path_distance_km(['AA', 'BB'], info) is None

    def test_blank_when_hop_coordinates_are_null_island(self, path_command):
        """0,0 in the DB means 'unset', not a real position in the Gulf of Guinea."""
        info = {'AA': self._info(0, 0)}
        assert path_command._calculate_path_distance_km(['AA'], info) is None

    def test_blank_when_sender_location_unknown(self, path_command):
        path_command._get_sender_location = lambda: None
        info = {'AA': self._info(47.0, -122.4)}
        assert path_command._calculate_path_distance_km(['AA'], info) is None

    def test_blank_when_bot_has_no_configured_position(self, path_command):
        path_command.bot_latitude = None
        info = {'AA': self._info(47.0, -122.4)}
        assert path_command._calculate_path_distance_km(['AA'], info) is None

    def test_placeholder_is_empty_string_when_unmeasurable(self, path_command):
        path_command._last_path_distance_km = None
        assert path_command._format_path_distance() == ""
