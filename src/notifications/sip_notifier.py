"""SIP SUBSCRIBE/NOTIFY notifier for ElementState and ServiceState.

Implements the notifier-side interface per NENA-STA-010.3.1 §2.4.1 and §2.4.2.
Listens for inbound SIP SUBSCRIBE on UDP and TCP, maintains subscriptions, and
delivers SIP NOTIFY to all active subscribers whenever state changes.

Configure with:
  LVF_SIP_HOST  — bind address (default 0.0.0.0)
  LVF_SIP_PORT  — bind port (default 5060; set to 0 to disable)
  LVF_SIP_ALLOWED_SUBSCRIBERS — comma-separated permitted From URIs (unset = all accepted)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from sipmessage import Address, CSeq, MediaType, Message, Parameters, Request, Response, URI, Via

from src.notifications import element_state as _element_state
from src.notifications import service_state as _service_state

log = logging.getLogger(__name__)

_EVENT_ELEMENT = "emergency-ElementState"
_EVENT_SERVICE = "emergency-ServiceState"
_CT_ELEMENT = "Application/EmergencyCallData.ElementState+json"
_CT_SERVICE = "Application/EmergencyCallData.ServiceState+json"

_MIN_EXPIRES = 60
_MAX_EXPIRES = 86400
_DEFAULT_EXPIRES = 3600
_CLEANUP_INTERVAL = 30.0
_LVF_MIN_NOTIFY_INTERVAL = 1.0  # mirrors ElementStateNotifier._MIN_INTERVAL


@dataclass
class _Subscription:
    call_id: str
    from_addr: str           # subscriber's From header string
    to_addr_with_tag: str    # our To header (with assigned tag)
    contact_uri: str         # subscriber's Contact URI string
    event_type: str          # _EVENT_ELEMENT or _EVENT_SERVICE
    expires_at: float        # monotonic clock expiry
    min_interval: float      # subscriber-requested minimum NOTIFY interval
    last_notified: float     # monotonic clock of last NOTIFY sent
    notify_cseq: int         # incrementing CSeq for our NOTIFY requests
    transport: str           # 'UDP' or 'TCP'
    remote_host: str
    remote_port: int
    pending_notify: bool = field(default=False)


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler: Callable) -> None:
        self._handler = handler
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        asyncio.ensure_future(self._handler(data, addr, self.transport))

    def error_received(self, exc: Exception) -> None:
        log.warning("SIP UDP error: %s", exc)


class SIPNotifier:
    """Notifier-side SIP SUBSCRIBE/NOTIFY server (UDP + TCP)."""

    def __init__(self, host: str = "0.0.0.0", port: int = 5060) -> None:
        self._host = host
        self._port = port
        self._server_uri = os.environ.get("LVF_SERVER_URI", "lostserver.example.com")
        self._subscriptions: Dict[str, _Subscription] = {}
        self._udp: Optional[_UDPProtocol] = None
        self._tcp_server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        _, protocol = await loop.create_datagram_endpoint(
            lambda: _UDPProtocol(self._handle_datagram),
            local_addr=(self._host, self._port),
        )
        self._udp = protocol

        self._tcp_server = await asyncio.start_server(
            self._handle_tcp_client,
            host=self._host,
            port=self._port,
        )

        _element_state.subscribe(self._on_element_state_change)
        _service_state.subscribe(self._on_service_state_change)

        asyncio.ensure_future(self._cleanup_loop())
        log.info("SIP notifier listening on %s:%d (UDP+TCP)", self._host, self._port)

    # ── Transport entry points ────────────────────────────────────────────────

    async def _handle_datagram(
        self,
        data: bytes,
        addr: Tuple[str, int],
        transport: asyncio.DatagramTransport,
    ) -> None:
        def send_fn(resp: bytes) -> None:
            transport.sendto(resp, addr)

        await self._handle_message(data, addr, send_fn, "UDP")

    async def _handle_tcp_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername", ("0.0.0.0", 0))
        try:
            data = await _read_sip_tcp(reader)
            if not data:
                return

            def send_fn(resp: bytes) -> None:
                writer.write(resp)
                asyncio.ensure_future(writer.drain())

            await self._handle_message(data, peer, send_fn, "TCP")
        except Exception as exc:
            log.debug("SIP TCP error from %s: %s", peer, exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ── Message dispatch ──────────────────────────────────────────────────────

    async def _handle_message(
        self,
        data: bytes,
        addr: Tuple[str, int],
        send_fn: Callable[[bytes], None],
        transport: str,
    ) -> None:
        try:
            msg = Message.parse(data)
        except ValueError as exc:
            log.debug("SIP parse error from %s: %s", addr, exc)
            return
        if not isinstance(msg, Request):
            return
        if msg.method == "SUBSCRIBE":
            await self._handle_subscribe(msg, addr, send_fn, transport)
        else:
            log.debug("SIP: ignored %s from %s", msg.method, addr)

    # ── SUBSCRIBE handling ────────────────────────────────────────────────────

    async def _handle_subscribe(
        self,
        req: Request,
        addr: Tuple[str, int],
        send_fn: Callable[[bytes], None],
        transport: str,
    ) -> None:
        event_header = (req.headers.get("Event") or "").strip()
        event_type = event_header.split(";")[0].strip()

        if event_type not in (_EVENT_ELEMENT, _EVENT_SERVICE):
            log.debug("SIP: unsupported Event '%s'", event_type)
            send_fn(bytes(_error_response(req, 489, "Bad Event")))
            return

        # Access control
        allowed_raw = os.environ.get("LVF_SIP_ALLOWED_SUBSCRIBERS", "").strip()
        if allowed_raw:
            allowed = {u.strip() for u in allowed_raw.split(",") if u.strip()}
            from_uri = str(req.from_address.uri) if req.from_address else ""
            if from_uri not in allowed:
                log.info("SIP: rejected SUBSCRIBE from '%s' (not in allowed list)", from_uri)
                send_fn(bytes(_error_response(req, 603, "Decline")))
                return

        raw_expires = req.expires if req.expires is not None else _DEFAULT_EXPIRES
        expires_int = int(raw_expires)
        call_id = req.call_id or ""

        # Unsubscribe (Expires: 0)
        if expires_int == 0:
            sub = self._subscriptions.get(call_id)
            if sub is None:
                send_fn(bytes(_error_response(req, 481, "Subscription Does Not Exist")))
                return
            del self._subscriptions[call_id]
            send_fn(bytes(_ok_response(req, 0, sub.to_addr_with_tag)))
            asyncio.ensure_future(self._send_notify(sub, terminated=True))
            log.debug("SIP: unsubscribed %s", call_id)
            return

        expires_clamped = max(_MIN_EXPIRES, min(_MAX_EXPIRES, expires_int))

        # Resolve subscriber address from Contact
        remote_host, remote_port = addr[0], addr[1]
        contact_uri = ""
        if req.contact:
            c = req.contact[0]
            contact_uri = str(c.uri)
            if c.uri.host:
                remote_host = c.uri.host
            if c.uri.port:
                remote_port = c.uri.port
        else:
            remote_port = 5060

        min_interval = _parse_min_interval(event_header)

        if call_id in self._subscriptions:
            # Re-subscribe: refresh expiry
            sub = self._subscriptions[call_id]
            sub.expires_at = time.monotonic() + expires_clamped
            sub.min_interval = max(sub.min_interval, min_interval)
            log.debug("SIP: re-subscribe %s expires=%d", call_id, expires_clamped)
        else:
            # New subscription
            tag = uuid.uuid4().hex[:8]
            to_with_tag = (
                _add_tag(req.to_address, tag)
                if req.to_address
                else f"<sip:{self._server_uri}>;tag={tag}"
            )
            sub = _Subscription(
                call_id=call_id,
                from_addr=str(req.from_address) if req.from_address else "",
                to_addr_with_tag=to_with_tag,
                contact_uri=contact_uri,
                event_type=event_type,
                expires_at=time.monotonic() + expires_clamped,
                min_interval=min_interval,
                last_notified=0.0,
                notify_cseq=1,
                transport=transport,
                remote_host=remote_host,
                remote_port=remote_port,
            )
            self._subscriptions[call_id] = sub
            log.info(
                "SIP: new %s subscription %s from %s",
                event_type, call_id, sub.from_addr,
            )

        send_fn(bytes(_ok_response(req, expires_clamped, sub.to_addr_with_tag)))
        asyncio.ensure_future(self._send_initial_notify(sub))

    # ── NOTIFY delivery ───────────────────────────────────────────────────────

    async def _send_initial_notify(self, sub: _Subscription) -> None:
        body = (
            _element_state.get_notify_body()
            if sub.event_type == _EVENT_ELEMENT
            else _service_state.get_notify_body()
        )
        await self._send_notify(sub, body_dict=body)

    async def _send_notify(
        self,
        sub: _Subscription,
        body_dict: Optional[dict] = None,
        terminated: bool = False,
    ) -> None:
        if body_dict is None:
            body_dict = (
                _element_state.get_notify_body()
                if sub.event_type == _EVENT_ELEMENT
                else _service_state.get_notify_body()
            )

        body_bytes = json.dumps(body_dict).encode()
        ct = _CT_ELEMENT if sub.event_type == _EVENT_ELEMENT else _CT_SERVICE
        target = _parse_contact_uri(sub.contact_uri, sub.remote_host, sub.remote_port)

        notify = Request("NOTIFY", target, body=body_bytes)

        try:
            notify.to_address = Address.parse(sub.from_addr)
        except (ValueError, AttributeError):
            pass

        try:
            notify.from_address = Address.parse(sub.to_addr_with_tag)
        except (ValueError, AttributeError):
            notify.from_address = Address(uri=URI(scheme="sip", host=self._server_uri))

        notify.call_id = sub.call_id
        notify.cseq = CSeq(sub.notify_cseq, "NOTIFY")
        sub.notify_cseq += 1

        branch = f"z9hG4bK{uuid.uuid4().hex[:16]}"
        try:
            notify.via = [Via.parse(f"SIP/2.0/{sub.transport} {self._server_uri};branch={branch}")]
        except ValueError:
            pass

        notify.max_forwards = 70
        notify.headers.add("Event", sub.event_type)

        if terminated:
            sub_state = "terminated;reason=timeout"
        else:
            remaining = max(0, int(sub.expires_at - time.monotonic()))
            sub_state = f"active;expires={remaining}"
        notify.headers.add("Subscription-State", sub_state)

        try:
            notify.content_type = MediaType.parse(ct)
        except ValueError:
            notify.headers.add("Content-Type", ct)

        notify.content_length = len(body_bytes)
        sub.last_notified = time.monotonic()

        await self._transmit(bytes(notify), sub)

    async def _transmit(self, data: bytes, sub: _Subscription) -> None:
        if sub.transport == "TCP":
            try:
                reader, writer = await asyncio.open_connection(sub.remote_host, sub.remote_port)
                writer.write(data)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception as exc:
                log.warning(
                    "SIP: TCP NOTIFY to %s:%d failed: %s",
                    sub.remote_host, sub.remote_port, exc,
                )
        else:
            if self._udp and self._udp.transport:
                self._udp.transport.sendto(data, (sub.remote_host, sub.remote_port))
            else:
                log.warning("SIP: UDP transport unavailable, cannot deliver NOTIFY %s", sub.call_id)

    # ── State-change callbacks (registered with the notifiers) ────────────────

    async def _on_element_state_change(self, body: dict) -> None:
        now = time.monotonic()
        for sub in list(self._subscriptions.values()):
            if sub.event_type != _EVENT_ELEMENT or now >= sub.expires_at:
                continue
            effective_min = max(sub.min_interval, _LVF_MIN_NOTIFY_INTERVAL)
            if now - sub.last_notified >= effective_min:
                asyncio.ensure_future(self._send_notify(sub, body_dict=body))
            elif not sub.pending_notify:
                sub.pending_notify = True
                delay = effective_min - (now - sub.last_notified)
                asyncio.get_running_loop().call_later(delay, self._fire_pending_notify, sub.call_id)

    async def _on_service_state_change(self, body: dict) -> None:
        now = time.monotonic()
        for sub in list(self._subscriptions.values()):
            if sub.event_type != _EVENT_SERVICE or now >= sub.expires_at:
                continue
            effective_min = max(sub.min_interval, _LVF_MIN_NOTIFY_INTERVAL)
            if now - sub.last_notified >= effective_min:
                asyncio.ensure_future(self._send_notify(sub, body_dict=body))
            elif not sub.pending_notify:
                sub.pending_notify = True
                delay = effective_min - (now - sub.last_notified)
                asyncio.get_running_loop().call_later(delay, self._fire_pending_notify, sub.call_id)

    def _fire_pending_notify(self, call_id: str) -> None:
        sub = self._subscriptions.get(call_id)
        if sub is None or time.monotonic() >= sub.expires_at:
            return
        sub.pending_notify = False
        asyncio.ensure_future(self._send_notify(sub))

    # ── Background expiry cleanup ─────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL)
            now = time.monotonic()
            expired = [k for k, v in self._subscriptions.items() if now >= v.expires_at]
            for k in expired:
                log.debug("SIP: subscription %s expired, removing", k)
                del self._subscriptions[k]


# ── Module-level helpers ──────────────────────────────────────────────────────

def _add_tag(addr: Address, tag: str) -> str:
    params = Parameters.parse(f";tag={tag}")
    return str(Address(uri=addr.uri, name=addr.name, parameters=params))


def _parse_min_interval(event_header: str) -> float:
    """Extract min-interval parameter from the Event header value, if present."""
    for part in event_header.split(";")[1:]:
        part = part.strip()
        if part.lower().startswith("min-interval="):
            try:
                return float(part.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                pass
    return 0.0


def _parse_contact_uri(contact_uri: str, fallback_host: str, fallback_port: int) -> URI:
    if contact_uri:
        try:
            return URI.parse(contact_uri)
        except ValueError:
            pass
    return URI(scheme="sip", host=fallback_host, port=fallback_port if fallback_port else 5060)


def _error_response(req: Request, code: int, phrase: str) -> Response:
    resp = Response(code, phrase)
    if req.via:
        resp.via = req.via
    resp.from_address = req.from_address
    resp.to_address = req.to_address
    resp.call_id = req.call_id
    resp.cseq = req.cseq
    resp.content_length = 0
    return resp


def _ok_response(req: Request, expires: int, to_addr_with_tag: str) -> Response:
    resp = Response(200, "OK")
    if req.via:
        resp.via = req.via
    resp.from_address = req.from_address
    try:
        resp.to_address = Address.parse(to_addr_with_tag)
    except (ValueError, AttributeError):
        resp.to_address = req.to_address
    resp.call_id = req.call_id
    resp.cseq = req.cseq
    resp.expires = expires
    event_val = req.headers.get("Event")
    if event_val:
        resp.headers.add("Event", event_val)
    resp.content_length = 0
    return resp


async def _read_sip_tcp(reader: asyncio.StreamReader) -> bytes:
    """Read a complete SIP message from a TCP stream (header + declared body)."""
    data = b""
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data:
                header_part, body_part = data.split(b"\r\n\r\n", 1)
                cl = 0
                for line in header_part.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        try:
                            cl = int(line.split(b":", 1)[1].strip())
                        except (ValueError, IndexError):
                            pass
                        break
                if len(body_part) >= cl:
                    return header_part + b"\r\n\r\n" + body_part[:cl]
    except asyncio.TimeoutError:
        pass
    return data
