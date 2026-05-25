"""Unit tests for src/ntp.py."""

import datetime
import os
from unittest.mock import patch

import ntplib
import pytest

from src.ntp import NTPClient

_WITH_SERVER = patch.dict(os.environ, {"LVF_NTP_SERVER": "test.ntp.example"})


def test_fallback_on_ntp_exception():
    """get_current_time() falls back to system clock when ntplib raises NTPException."""
    with _WITH_SERVER, patch("ntplib.NTPClient.request", side_effect=ntplib.NTPException("no response")):
        client = NTPClient()
        result = client.get_current_time()

    assert isinstance(result, datetime.datetime)
    assert result.tzinfo is not None
    assert not client.is_synchronized
    assert client.last_sync_time is None


def test_fallback_on_generic_exception():
    """get_current_time() falls back to system clock on any exception (e.g. socket timeout)."""
    with _WITH_SERVER, patch("ntplib.NTPClient.request", side_effect=OSError("network unreachable")):
        client = NTPClient()
        result = client.get_current_time()

    assert isinstance(result, datetime.datetime)
    assert not client.is_synchronized


def test_no_ntp_attempt_when_server_unset():
    """When LVF_NTP_SERVER is not set, get_current_time() returns system clock without querying."""
    env = {k: v for k, v in os.environ.items() if k != "LVF_NTP_SERVER"}
    with patch.dict(os.environ, env, clear=True):
        client = NTPClient()
        assert client.server is None
        with patch("ntplib.NTPClient.request") as mock_req:
            result = client.get_current_time()
            mock_req.assert_not_called()

    assert isinstance(result, datetime.datetime)
    assert not client.is_synchronized


def test_synchronized_on_success():
    """get_current_time() returns NTP time and sets is_synchronized=True on success."""
    fake_time = 1_700_000_000.0

    class FakeResponse:
        tx_time = fake_time

    with _WITH_SERVER, patch("ntplib.NTPClient.request", return_value=FakeResponse()):
        client = NTPClient()
        result = client.get_current_time()

    expected = datetime.datetime.fromtimestamp(fake_time, tz=datetime.timezone.utc)
    assert result == expected
    assert client.is_synchronized
    assert client.last_sync_time == expected


def test_degraded_transition_calls_notify():
    """Transitioning from synchronized to failed calls notify_degraded("ntp")."""
    fake_time = 1_700_000_000.0

    class FakeResponse:
        tx_time = fake_time

    with _WITH_SERVER:
        client = NTPClient()

        # First call succeeds — client is synchronized
        with patch("ntplib.NTPClient.request", return_value=FakeResponse()):
            client.get_current_time()
        assert client.is_synchronized

        # Second call fails — should trigger notify_degraded
        with patch("ntplib.NTPClient.request", side_effect=ntplib.NTPException("timeout")):
            with patch("src.notifications.element_state.notify_degraded") as mock_notify:
                client.get_current_time()
                mock_notify.assert_called_once_with("ntp")

    assert not client.is_synchronized
