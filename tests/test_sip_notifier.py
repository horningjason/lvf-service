"""Tests for SipWireAdapter — the sipmessage wire layer around
i3_fe_core.notify.SipNotifier.

Subscription registry, expiry, the rate-filter/watchdog, event-package
validation, and the SUBSCRIBE accept/reject decision are core's
responsibility and are covered by i3-fe-core's own test suite. These tests
exercise only what the wire adapter itself owns: SIP parsing/serialization,
the To-tag/WireContext bookkeeping, and NOTIFY transmission.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import List
from unittest.mock import patch

import pytest

from sipmessage import Message, Request, Response

from i3_fe_core.config.identity import ElementIdentity
from i3_fe_core.state.store import ElementState, InProcessStateStore, ServiceState
from i3_fe_core.state.element_state import (
    EVENT_PACKAGE_NAME as EVENT_ELEMENT,
    ElementStateNotifier,
)
from i3_fe_core.state.service_state import EVENT_PACKAGE_NAME as EVENT_SERVICE, ServiceStateNotifier

from src.notify.sip_notifier import SipWireAdapter


# ── Fixtures / helpers ──────────────────────────────────────────────────────

def _make_notifiers():
    identity = ElementIdentity(element_id="lvf.example.com", agency_id="example.com", service_name="LVF")
    store = InProcessStateStore()
    element_notifier = ElementStateNotifier(identity, store, min_notify_interval=0.0)
    service_notifier = ServiceStateNotifier(
        service="lvf.example.com",
        name="LVF",
        domain="lvf.example.com",
        service_id="lvf.example.com",
        store=store,
        min_notify_interval=0.0,
        supports_security_posture=False,
    )
    return element_notifier, service_notifier


def _make_adapter() -> SipWireAdapter:
    """Create a SipWireAdapter without binding any sockets."""
    element_notifier, service_notifier = _make_notifiers()
    return SipWireAdapter(element_notifier, service_notifier, host="127.0.0.1", port=15060)


def _make_subscribe(
    event: str = EVENT_ELEMENT,
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


async def _send(adapter: SipWireAdapter, data: bytes, send_fn=lambda _: None) -> None:
    await adapter._handle_message(data, ("192.168.1.1", 5060), send_fn, "UDP")
    await asyncio.sleep(0)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSubscribeHandling:
    def test_subscribe_accepted_returns_200(self):
        adapter = _make_adapter()
        responses: List[bytes] = []

        asyncio.run(_send(adapter, _make_subscribe(), responses.append))

        assert responses, "Expected a response"
        resp = Message.parse(responses[0])
        assert isinstance(resp, Response)
        assert resp.code == 200

    def test_subscribe_immediate_notify_sent(self):
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        asyncio.run(_send(adapter, _make_subscribe()))

        assert notifies, "Expected an immediate NOTIFY"
        notify = Message.parse(notifies[0])
        assert isinstance(notify, Request)
        assert notify.method == "NOTIFY"
        assert notify.headers.get("Event") == EVENT_ELEMENT
        assert notify.headers.get("Subscription-State", "").startswith("active")

    def test_subscribe_notify_body_is_valid_json(self):
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        asyncio.run(_send(adapter, _make_subscribe()))

        assert notifies
        notify = Message.parse(notifies[0])
        payload = json.loads(notify.body)
        assert "elementId" in payload
        assert "state" in payload

    def test_subscribe_to_tag_added_in_200(self):
        adapter = _make_adapter()
        responses: List[bytes] = []

        asyncio.run(_send(adapter, _make_subscribe(), responses.append))

        resp = Message.parse(responses[0])
        assert resp.to_address is not None
        assert resp.to_address.parameters.get("tag") is not None, "To tag must be present in 200 OK"

    def test_subscribe_service_state_event(self):
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        asyncio.run(_send(adapter, _make_subscribe(event=EVENT_SERVICE)))

        assert notifies
        notify = Message.parse(notifies[0])
        assert notify.headers.get("Event") == EVENT_SERVICE
        ct = str(notify.content_type) if notify.content_type else ""
        assert "ServiceState" in ct

    def test_resubscribe_reuses_to_tag(self):
        """Re-SUBSCRIBE with the same Call-ID keeps the previously assigned To tag."""
        adapter = _make_adapter()
        responses: List[bytes] = []

        async def run():
            await _send(adapter, _make_subscribe(call_id="resub@host"), responses.append)
            await _send(adapter, _make_subscribe(expires=7200, call_id="resub@host"), responses.append)

        asyncio.run(run())

        assert len(responses) == 2
        tag1 = Message.parse(responses[0]).to_address.parameters.get("tag")
        tag2 = Message.parse(responses[1]).to_address.parameters.get("tag")
        assert tag1 == tag2

    def test_interval_too_brief_returns_423_with_min_expires(self):
        adapter = _make_adapter()
        responses: List[bytes] = []

        asyncio.run(_send(adapter, _make_subscribe(expires=5), responses.append))

        assert responses
        resp = Message.parse(responses[0])
        assert resp.code == 423
        assert resp.headers.get("Min-Expires") is not None

    def test_bad_event_returns_489(self):
        adapter = _make_adapter()
        responses: List[bytes] = []

        asyncio.run(_send(adapter, _make_subscribe(event="unknown-package"), responses.append))

        assert responses
        resp = Message.parse(responses[0])
        assert resp.code == 489


class TestSubscribeAccessControl:
    def test_unauthorized_subscriber_returns_403(self):
        adapter = _make_adapter()
        responses: List[bytes] = []

        async def run():
            with patch.dict(
                os.environ,
                {"LVF_SIP_ALLOWED_SUBSCRIBERS": "sip:allowed@example.com:5060"},
            ):
                await _send(adapter, _make_subscribe(contact="sip:rejected@192.168.1.1:5060"), responses.append)

        asyncio.run(run())

        assert responses
        resp = Message.parse(responses[0])
        assert isinstance(resp, Response)
        assert resp.code == 403

    def test_authorized_subscriber_returns_200(self):
        adapter = _make_adapter()
        responses: List[bytes] = []

        async def run():
            with patch.dict(
                os.environ,
                {"LVF_SIP_ALLOWED_SUBSCRIBERS": "sip:esrp@192.168.1.1:5060"},
            ):
                await _send(adapter, _make_subscribe(contact="sip:esrp@192.168.1.1:5060"), responses.append)

        asyncio.run(run())

        assert responses
        resp = Message.parse(responses[0])
        assert resp.code == 200

    def test_no_allowed_list_accepts_all(self):
        adapter = _make_adapter()
        responses: List[bytes] = []
        env = {k: v for k, v in os.environ.items() if k != "LVF_SIP_ALLOWED_SUBSCRIBERS"}

        async def run():
            with patch.dict(os.environ, env, clear=True):
                await _send(adapter, _make_subscribe(contact="sip:anyone@anywhere.example"), responses.append)

        asyncio.run(run())

        assert responses
        resp = Message.parse(responses[0])
        assert resp.code == 200


class TestUnsubscribe:
    def test_expires_0_removes_wire_context_and_returns_200(self):
        adapter = _make_adapter()
        unsub_responses: List[bytes] = []

        async def run():
            await _send(adapter, _make_subscribe(call_id="unsub@host"))
            await _send(adapter, _make_subscribe(expires=0, call_id="unsub@host"), unsub_responses.append)

        asyncio.run(run())

        assert "unsub@host" not in adapter._wire, "Wire context must be removed"
        assert unsub_responses
        resp = Message.parse(unsub_responses[0])
        assert resp.code == 200
        assert resp.expires == 0

    def test_expires_0_sends_terminated_notify(self):
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        async def run():
            await _send(adapter, _make_subscribe(call_id="term@host"))
            notifies.clear()  # discard initial NOTIFY
            await _send(adapter, _make_subscribe(expires=0, call_id="term@host"))

        asyncio.run(run())

        terminated = [
            n for n in notifies
            if Message.parse(n).headers.get("Subscription-State", "").startswith("terminated")
        ]
        assert terminated, "Expected a terminated NOTIFY after unsubscribe"


class TestNotifyOnStateChange:
    def test_element_state_change_triggers_notify(self):
        """No min-interval was requested in the SUBSCRIBE (§2.4.1: the watchdog
        filter only applies "if specified"), so this delivers immediately —
        no floor, no wait for a watchdog timer."""
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        async def run():
            await _send(adapter, _make_subscribe(event=EVENT_ELEMENT, call_id="elem@host"))
            notifies.clear()
            adapter._element_notifier.set_state(ElementState.SERVICE_DISRUPTION, "test")
            await asyncio.sleep(0)

        asyncio.run(run())

        assert notifies, "Expected NOTIFY after state change"
        notify = Message.parse(notifies[0])
        payload = json.loads(notify.body)
        assert payload["state"] == "ServiceDisruption"

    def test_service_state_change_triggers_notify(self):
        """See test_element_state_change_triggers_notify: no min-interval
        requested means immediate delivery, same reasoning applies here."""
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        async def run():
            await _send(adapter, _make_subscribe(event=EVENT_SERVICE, call_id="svc@host"))
            notifies.clear()
            adapter._service_notifier.set_state(ServiceState.OVERLOADED, "maintenance")
            await asyncio.sleep(0)

        asyncio.run(run())

        assert notifies
        payload = json.loads(Message.parse(notifies[0]).body)
        assert payload["serviceState"]["state"] == "Overloaded"

    def test_unrequested_min_interval_does_not_force_heartbeat(self):
        """§2.4.1/§2.4.2: 'Filter requests MAY specify a minimum notification
        interval. The element MUST generate a NOTIFY meeting this filter, if
        specified.' No filter was specified here, so nothing should arrive
        without an actual state change — the watchdog is opt-in, not a
        server-imposed default."""
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        async def run():
            await _send(adapter, _make_subscribe(event=EVENT_ELEMENT, call_id="quiet@host"))
            notifies.clear()
            await asyncio.sleep(1.2)  # longer than the old 1.0s floor would have used

        asyncio.run(run())

        assert not notifies, "No NOTIFY should be sent absent a real state change or a requested filter"

    def test_explicit_min_interval_is_still_floored_and_watchdogs(self):
        """A subscriber that DOES request a fast min-interval is still floored
        at 1.0s (flood protection) — but unlike the no-filter case, MUST
        watchdog per §2.4.1/§2.4.2 since the subscriber explicitly asked for
        a filtered interval."""
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        async def run():
            await _send(
                adapter,
                _make_subscribe(event=f"{EVENT_ELEMENT};min-interval=0.1", call_id="fast@host"),
            )
            notifies.clear()
            adapter._element_notifier.set_state(ElementState.SERVICE_DISRUPTION, "test")
            adapter._element_notifier.set_state(ElementState.NORMAL, "recovered")
            await asyncio.sleep(1.2)

        asyncio.run(run())

        # Rapid changes within the 1.0s floor coalesce to a single NOTIFY
        # carrying the latest state, not one per set_state() call.
        assert len(notifies) == 1, f"Expected exactly one coalesced NOTIFY, got {len(notifies)}"
        payload = json.loads(Message.parse(notifies[0]).body)
        assert payload["state"] == "Normal"

    def test_element_change_does_not_notify_service_subscribers(self):
        adapter = _make_adapter()
        notifies: List[bytes] = []

        async def mock_transmit(data, ctx):
            notifies.append(data)

        adapter._transmit = mock_transmit

        async def run():
            await _send(adapter, _make_subscribe(event=EVENT_SERVICE, call_id="svc-only@host"))
            notifies.clear()
            adapter._element_notifier.set_state(ElementState.SERVICE_DISRUPTION, "test")
            await asyncio.sleep(0)

        asyncio.run(run())

        assert not notifies, "Service-state subscriber must not receive element-state NOTIFY"
