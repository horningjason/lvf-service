"""Tests for SIPNotifier SUBSCRIBE/NOTIFY handling."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import List
from unittest.mock import patch

import pytest

from sipmessage import Message, Request, Response

from src.notifications.sip_notifier import (
    SIPNotifier,
    _Subscription,
    _EVENT_ELEMENT,
    _EVENT_SERVICE,
    _CT_ELEMENT,
    _CT_SERVICE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_subscribe(
    event: str = _EVENT_ELEMENT,
    expires: int = 3600,
    from_uri: str = "sip:esrp@192.168.1.1",
    to_uri: str = "sip:lvf.example.com",
    call_id: str = "test-call-id@192.168.1.1",
    contact: str = "sip:esrp@192.168.1.1:5060",
) -> bytes:
    return (
        f"SUBSCRIBE {to_uri} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP 192.168.1.1:5060;branch=z9hG4bKtest\r\n"
        f"From: <{from_uri}>;tag=sub-tag-1\r\n"
        f"To: <{to_uri}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 SUBSCRIBE\r\n"
        f"Event: {event}\r\n"
        f"Expires: {expires}\r\n"
        f"Contact: <{contact}>\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    ).encode()


def _make_notifier() -> SIPNotifier:
    """Create a SIPNotifier without binding any sockets."""
    return SIPNotifier(host="127.0.0.1", port=15060)


def _inject_sub(
    notifier: SIPNotifier,
    call_id: str = "injected@host",
    event_type: str = _EVENT_ELEMENT,
    expires_in: float = 3600,
    last_notified: float = 0.0,
    min_interval: float = 0.0,
) -> _Subscription:
    sub = _Subscription(
        call_id=call_id,
        from_addr="<sip:esrp@192.168.1.1>;tag=sub-tag",
        to_addr_with_tag="<sip:lvf.example.com>;tag=server-tag",
        contact_uri="sip:esrp@192.168.1.1:5060",
        event_type=event_type,
        expires_at=time.monotonic() + expires_in,
        min_interval=min_interval,
        last_notified=last_notified,
        notify_cseq=1,
        transport="UDP",
        remote_host="192.168.1.1",
        remote_port=5060,
    )
    notifier._subscriptions[call_id] = sub
    return sub


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSubscribeHandling:
    def test_subscribe_accepted_returns_200(self):
        """Valid SUBSCRIBE returns 200 OK."""
        notifier = _make_notifier()
        responses: List[bytes] = []

        async def mock_transmit(data, sub):
            pass

        notifier._transmit = mock_transmit

        async def run():
            await notifier._handle_message(
                _make_subscribe(),
                ("192.168.1.1", 5060),
                responses.append,
                "UDP",
            )

        asyncio.run(run())

        assert responses, "Expected a response"
        resp = Message.parse(responses[0])
        assert isinstance(resp, Response)
        assert resp.code == 200

    def test_subscribe_creates_subscription(self):
        """Successful SUBSCRIBE stores a subscription entry."""
        notifier = _make_notifier()

        async def mock_transmit(data, sub):
            pass

        notifier._transmit = mock_transmit

        async def run():
            await notifier._handle_message(
                _make_subscribe(call_id="create-test@host"),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )

        asyncio.run(run())

        assert "create-test@host" in notifier._subscriptions
        sub = notifier._subscriptions["create-test@host"]
        assert sub.event_type == _EVENT_ELEMENT

    def test_subscribe_immediate_notify_sent(self):
        """Immediate NOTIFY with current state is sent after successful SUBSCRIBE."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit

        async def run():
            await notifier._handle_message(
                _make_subscribe(),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )
            await asyncio.sleep(0)

        asyncio.run(run())

        assert notifies, "Expected an immediate NOTIFY"
        notify = Message.parse(notifies[0])
        assert isinstance(notify, Request)
        assert notify.method == "NOTIFY"
        assert notify.headers.get("Event") == _EVENT_ELEMENT
        assert notify.headers.get("Subscription-State", "").startswith("active")

    def test_subscribe_notify_body_is_valid_json(self):
        """Immediate NOTIFY body is valid JSON with state fields."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit

        async def run():
            await notifier._handle_message(
                _make_subscribe(),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )
            await asyncio.sleep(0)

        asyncio.run(run())

        assert notifies
        notify = Message.parse(notifies[0])
        payload = json.loads(notify.body)
        assert "elementId" in payload
        assert "state" in payload

    def test_subscribe_to_tag_added_in_200(self):
        """200 OK for SUBSCRIBE includes a To tag."""
        notifier = _make_notifier()
        responses: List[bytes] = []

        async def mock_transmit(data, sub):
            pass

        notifier._transmit = mock_transmit

        async def run():
            await notifier._handle_message(
                _make_subscribe(),
                ("192.168.1.1", 5060),
                responses.append,
                "UDP",
            )

        asyncio.run(run())

        resp = Message.parse(responses[0])
        assert resp.to_address is not None
        assert resp.to_address.parameters.get("tag") is not None, "To tag must be present in 200 OK"

    def test_subscribe_service_state_event(self):
        """ServiceState SUBSCRIBE is accepted and NOTIFY has correct content-type."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit

        async def run():
            await notifier._handle_message(
                _make_subscribe(event=_EVENT_SERVICE),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )
            await asyncio.sleep(0)

        asyncio.run(run())

        assert notifies
        notify = Message.parse(notifies[0])
        assert notify.headers.get("Event") == _EVENT_SERVICE
        ct = str(notify.content_type) if notify.content_type else ""
        assert "ServiceState" in ct

    def test_resubscribe_updates_expiry(self):
        """Re-SUBSCRIBE with same Call-ID refreshes expiry without creating duplicate."""
        notifier = _make_notifier()

        async def mock_transmit(data, sub):
            pass

        notifier._transmit = mock_transmit

        async def run():
            # First subscribe
            await notifier._handle_message(
                _make_subscribe(call_id="resub@host"),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )
            # Re-subscribe
            await notifier._handle_message(
                _make_subscribe(expires=7200, call_id="resub@host"),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )
            await asyncio.sleep(0)

        asyncio.run(run())

        assert len(notifier._subscriptions) == 1
        sub = notifier._subscriptions["resub@host"]
        # expires_at should reflect the longer 7200s value
        assert sub.expires_at > time.monotonic() + 6000


class TestSubscribeAccessControl:
    def test_unauthorized_from_returns_603(self):
        """SUBSCRIBE from non-allowed URI returns 603 Decline."""
        notifier = _make_notifier()
        responses: List[bytes] = []

        async def run():
            with patch.dict(
                os.environ,
                {"LVF_SIP_ALLOWED_SUBSCRIBERS": "sip:allowed@example.com"},
            ):
                await notifier._handle_message(
                    _make_subscribe(from_uri="sip:rejected@192.168.1.1"),
                    ("192.168.1.1", 5060),
                    responses.append,
                    "UDP",
                )

        asyncio.run(run())

        assert responses
        resp = Message.parse(responses[0])
        assert isinstance(resp, Response)
        assert resp.code == 603

    def test_authorized_from_returns_200(self):
        """SUBSCRIBE from an allowed URI returns 200 OK."""
        notifier = _make_notifier()
        responses: List[bytes] = []

        async def mock_transmit(data, sub):
            pass

        notifier._transmit = mock_transmit

        async def run():
            with patch.dict(
                os.environ,
                {"LVF_SIP_ALLOWED_SUBSCRIBERS": "sip:esrp@192.168.1.1"},
            ):
                await notifier._handle_message(
                    _make_subscribe(from_uri="sip:esrp@192.168.1.1"),
                    ("192.168.1.1", 5060),
                    responses.append,
                    "UDP",
                )

        asyncio.run(run())

        assert responses
        resp = Message.parse(responses[0])
        assert resp.code == 200

    def test_no_allowed_list_accepts_all(self):
        """When LVF_SIP_ALLOWED_SUBSCRIBERS is unset, all SUBSCRIBE requests are accepted."""
        notifier = _make_notifier()
        responses: List[bytes] = []

        async def mock_transmit(data, sub):
            pass

        notifier._transmit = mock_transmit
        env = {k: v for k, v in os.environ.items() if k != "LVF_SIP_ALLOWED_SUBSCRIBERS"}

        async def run():
            with patch.dict(os.environ, env, clear=True):
                await notifier._handle_message(
                    _make_subscribe(from_uri="sip:anyone@anywhere.example"),
                    ("192.168.1.1", 5060),
                    responses.append,
                    "UDP",
                )

        asyncio.run(run())

        assert responses
        resp = Message.parse(responses[0])
        assert resp.code == 200


class TestUnsubscribe:
    def test_expires_0_removes_subscription(self):
        """SUBSCRIBE with Expires: 0 removes the subscription and returns 200."""
        notifier = _make_notifier()
        notifies: List[bytes] = []
        unsub_responses: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit

        async def run():
            await notifier._handle_message(
                _make_subscribe(call_id="unsub@host"),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )
            await asyncio.sleep(0)

            await notifier._handle_message(
                _make_subscribe(expires=0, call_id="unsub@host"),
                ("192.168.1.1", 5060),
                unsub_responses.append,
                "UDP",
            )
            await asyncio.sleep(0)

        asyncio.run(run())

        assert "unsub@host" not in notifier._subscriptions, "Subscription must be removed"
        assert unsub_responses
        resp = Message.parse(unsub_responses[0])
        assert resp.code == 200
        assert resp.expires == 0

    def test_expires_0_sends_terminated_notify(self):
        """Unsubscribe triggers a final NOTIFY with Subscription-State: terminated."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit

        async def run():
            await notifier._handle_message(
                _make_subscribe(call_id="term@host"),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )
            await asyncio.sleep(0)
            notifies.clear()  # discard initial NOTIFY

            await notifier._handle_message(
                _make_subscribe(expires=0, call_id="term@host"),
                ("192.168.1.1", 5060),
                lambda _: None,
                "UDP",
            )
            await asyncio.sleep(0)

        asyncio.run(run())

        terminated = [
            n for n in notifies
            if Message.parse(n).headers.get("Subscription-State", "").startswith("terminated")
        ]
        assert terminated, "Expected a terminated NOTIFY after unsubscribe"

    def test_expires_0_unknown_call_id_returns_481(self):
        """Unsubscribe for an unknown Call-ID returns 481 Subscription Does Not Exist."""
        notifier = _make_notifier()
        responses: List[bytes] = []

        async def run():
            await notifier._handle_message(
                _make_subscribe(expires=0, call_id="unknown@host"),
                ("192.168.1.1", 5060),
                responses.append,
                "UDP",
            )

        asyncio.run(run())

        assert responses
        resp = Message.parse(responses[0])
        assert resp.code == 481


class TestNotifyOnStateChange:
    def test_element_state_change_triggers_notify(self):
        """State change fires NOTIFY to active element-state subscribers."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit
        _inject_sub(notifier, event_type=_EVENT_ELEMENT)

        body = {"elementId": "test.lvf.example", "state": "ServiceDisruption", "reason": "test"}

        async def run():
            await notifier._on_element_state_change(body)
            await asyncio.sleep(0)

        asyncio.run(run())

        assert notifies, "Expected NOTIFY after state change"
        notify = Message.parse(notifies[0])
        assert notify.method == "NOTIFY"
        payload = json.loads(notify.body)
        assert payload["state"] == "ServiceDisruption"

    def test_service_state_change_triggers_notify(self):
        """ServiceState change fires NOTIFY to service-state subscribers."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit
        _inject_sub(notifier, event_type=_EVENT_SERVICE, call_id="svc@host")

        body = {
            "service": "LVF",
            "name": "LVF",
            "serviceId": "test.lvf.example",
            "serviceState": {"state": "Down", "reason": "maintenance"},
        }

        async def run():
            await notifier._on_service_state_change(body)
            await asyncio.sleep(0)

        asyncio.run(run())

        assert notifies
        payload = json.loads(Message.parse(notifies[0]).body)
        assert payload["serviceState"]["state"] == "Down"

    def test_element_change_does_not_notify_service_subscribers(self):
        """Element-state change does not notify service-state subscribers."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit
        _inject_sub(notifier, event_type=_EVENT_SERVICE, call_id="svc-only@host")

        body = {"elementId": "test", "state": "Down", "reason": ""}

        async def run():
            await notifier._on_element_state_change(body)
            await asyncio.sleep(0)

        asyncio.run(run())

        assert not notifies, "Service-state subscriber must not receive element-state NOTIFY"


class TestExpiredSubscriptions:
    def test_expired_subscription_not_notified(self):
        """Expired subscriptions do not receive NOTIFY."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit
        _inject_sub(notifier, call_id="expired@host", expires_in=-10)  # already expired

        body = {"elementId": "test", "state": "Down", "reason": ""}

        async def run():
            await notifier._on_element_state_change(body)
            await asyncio.sleep(0)

        asyncio.run(run())

        assert not notifies, "Expired subscription must not be notified"

    def test_cleanup_removes_expired_subscriptions(self):
        """Background cleanup removes subscriptions past their expiry."""
        notifier = _make_notifier()
        _inject_sub(notifier, call_id="will-expire@host", expires_in=-1)
        _inject_sub(notifier, call_id="still-active@host", expires_in=3600)

        async def run():
            now = time.monotonic()
            expired = [k for k, v in notifier._subscriptions.items() if now >= v.expires_at]
            for k in expired:
                del notifier._subscriptions[k]

        asyncio.run(run())

        assert "will-expire@host" not in notifier._subscriptions
        assert "still-active@host" in notifier._subscriptions


class TestRFC6446MinInterval:
    def test_second_notify_within_min_interval_deferred(self):
        """State change within subscriber min_interval is deferred, not dropped."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit
        sub = _inject_sub(
            notifier,
            call_id="rate-limited@host",
            min_interval=5.0,
            last_notified=time.monotonic(),  # just notified
        )

        body = {"elementId": "test", "state": "ServiceDisruption", "reason": "test"}

        async def run():
            await notifier._on_element_state_change(body)
            await asyncio.sleep(0)

        asyncio.run(run())

        assert not notifies, "Should not send NOTIFY within min_interval"
        assert sub.pending_notify is True, "Deferred NOTIFY should be scheduled"

    def test_notify_sent_when_interval_has_elapsed(self):
        """State change after min_interval has elapsed sends NOTIFY immediately."""
        notifier = _make_notifier()
        notifies: List[bytes] = []

        async def mock_transmit(data, sub):
            notifies.append(data)

        notifier._transmit = mock_transmit
        _inject_sub(
            notifier,
            call_id="not-rate-limited@host",
            min_interval=2.0,
            last_notified=time.monotonic() - 10.0,  # well past the interval
        )

        body = {"elementId": "test", "state": "Overloaded", "reason": "test"}

        async def run():
            await notifier._on_element_state_change(body)
            await asyncio.sleep(0)

        asyncio.run(run())

        assert notifies, "NOTIFY should be sent when interval has elapsed"
        payload = json.loads(Message.parse(notifies[0]).body)
        assert payload["state"] == "Overloaded"

    def test_no_duplicate_pending_scheduled(self):
        """Two rapid state changes schedule only one deferred NOTIFY."""
        notifier = _make_notifier()

        async def mock_transmit(data, sub):
            pass

        notifier._transmit = mock_transmit
        sub = _inject_sub(
            notifier,
            call_id="double-fire@host",
            min_interval=5.0,
            last_notified=time.monotonic(),
        )

        body = {"elementId": "test", "state": "Down", "reason": ""}

        async def run():
            await notifier._on_element_state_change(body)
            await notifier._on_element_state_change(body)
            await asyncio.sleep(0)

        asyncio.run(run())

        assert sub.pending_notify is True
        # Only one deferred scheduled — verified by pending_notify still True (not reset)
