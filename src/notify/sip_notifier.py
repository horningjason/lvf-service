"""SIP wire adapter for ElementState and ServiceState NOTIFY delivery.

Implements only the sipmessage wire layer per NENA-STA-010.3f-2021 §2.4.1 and
§2.4.2: UDP/TCP listeners, SIP message parsing/serialization, and datagram
transmission. Subscription registry, expiry, the rate-filter/watchdog timer,
event-package validation, and the SUBSCRIBE accept/reject decision all live
in i3_fe_core.notify.SipNotifier — this module never duplicates that state.

Configure with:
  LVF_SIP_HOST  — bind address (default 0.0.0.0)
  LVF_SIP_PORT  — bind port (default 5060; set to 0 to disable)
  LVF_SIP_ALLOWED_SUBSCRIBERS — comma-separated permitted Contact URIs (unset = all accepted)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from sipmessage import Address, CSeq, MediaType, Message, Parameters, Request, Response, URI, Via

from i3_fe_core.notify import SipNotifier, SipResponse, SipSubscribeRequest, SipSubscription
from i3_fe_core.state.element_state import (
    EVENT_PACKAGE_NAME as _ELEMENT_EVENT_PACKAGE,
    NOTIFY_MIME_TYPE as _ELEMENT_MIME_TYPE,
    ElementStateNotifier,
)
from i3_fe_core.state.service_state import (
    NOTIFY_MIME_TYPE as _SERVICE_MIME_TYPE,
    ServiceStateNotifier,
)

log = logging.getLogger(__name__)


@dataclass
class WireContext:
    """Per-subscription wire state that core's SipSubscription doesn't carry."""

    from_addr: str          # subscriber's From header string
    to_addr_with_tag: str   # our To header (with assigned tag)
    contact_uri: str        # subscriber's Contact URI string
    transport: str          # 'UDP' or 'TCP'
    remote_host: str
    remote_port: int


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


class SipWireAdapter:
    """Thin SIP transport shell around i3_fe_core.notify.SipNotifier."""

    def __init__(
        self,
        element_notifier: ElementStateNotifier,
        service_notifier: ServiceStateNotifier,
        host: str = "0.0.0.0",
        port: int = 5060,
    ) -> None:
        self._host = host
        self._port = port
        self._server_uri = os.environ.get("LVF_SERVER_URI", "lostserver.example.com")
        self._element_notifier = element_notifier
        self._service_notifier = service_notifier
        self._wire: Dict[str, WireContext] = {}
        self._udp: Optional[_UDPProtocol] = None
        self._tcp_server: Optional[asyncio.AbstractServer] = None

        self._core = SipNotifier(
            element_notifier=element_notifier,
            service_notifier=service_notifier,
            send_notify=self._send_notify,
            authorize_subscriber=self._authorize,
        )

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        try:
            _, protocol = await loop.create_datagram_endpoint(
                lambda: _UDPProtocol(self._handle_datagram),
                local_addr=(self._host, self._port),
            )
            self._udp = protocol
        except OSError as exc:
            log.info(
                "SIP notifier: UDP bind on %s:%d failed (%s) — SIP notifier not started",
                self._host, self._port, exc,
            )
            return

        try:
            self._tcp_server = await asyncio.start_server(
                self._handle_tcp_client,
                host=self._host,
                port=self._port,
            )
        except OSError as exc:
            log.info(
                "SIP notifier: TCP bind on %s:%d failed (%s) — SIP notifier not started",
                self._host, self._port, exc,
            )
            if self._udp is not None and self._udp.transport is not None:
                self._udp.transport.close()
            self._udp = None
            return

        self._core.start()
        log.info("SIP notifier listening on %s:%d (UDP+TCP)", self._host, self._port)

    async def stop(self) -> None:
        """Stop the core subscription manager and close the UDP/TCP listeners.
        Safe to call even if start() never successfully bound (no-op in that
        case) — required for graceful shutdown under gunicorn: without this,
        the bound listeners and core's cleanup loop keep the event loop alive
        past graceful_timeout."""
        await self._core.stop()

        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None

        if self._udp is not None and self._udp.transport is not None:
            self._udp.transport.close()
        self._udp = None

        log.info("SIP notifier stopped")

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
        event_package = event_header.split(";")[0].strip()
        call_id = req.call_id or ""

        min_interval = max(_parse_min_interval(event_header), 1.0)

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

        existing = self._wire.get(call_id)
        if existing is not None:
            to_addr_with_tag = existing.to_addr_with_tag
        else:
            tag = uuid.uuid4().hex[:8]
            to_addr_with_tag = (
                _add_tag(req.to_address, tag)
                if req.to_address
                else f"<sip:{self._server_uri}>;tag={tag}"
            )

        ctx = WireContext(
            from_addr=str(req.from_address) if req.from_address else "",
            to_addr_with_tag=to_addr_with_tag,
            contact_uri=contact_uri,
            transport=transport,
            remote_host=remote_host,
            remote_port=remote_port,
        )
        self._wire[call_id] = ctx

        request = SipSubscribeRequest(
            event_package=event_package,
            subscriber_uri=contact_uri,
            call_id=call_id,
            expires=int(req.expires) if req.expires is not None else None,
            min_notify_interval=min_interval,
            filter_body=req.body.decode("utf-8", "replace") if req.body else None,
        )

        resp: SipResponse = self._core.handle_subscribe(request)

        if resp.status_code == 200:
            send_fn(bytes(_ok_response(req, resp.expires or 0, ctx.to_addr_with_tag)))
            if resp.expires == 0:
                await self._send_terminating_notify(call_id, event_package, ctx)
                del self._wire[call_id]
        elif resp.status_code == 423:
            send_fn(bytes(_error_response(
                req, 423, "Interval Too Brief",
                extra_headers={"Min-Expires": str(resp.min_expires)},
            )))
        elif resp.status_code == 489:
            send_fn(bytes(_error_response(req, 489, "Bad Event")))
        elif resp.status_code == 503:
            send_fn(bytes(_error_response(req, 503, resp.reason)))
        elif resp.status_code == 403:
            send_fn(bytes(_error_response(req, 403, "Forbidden")))
        else:
            send_fn(bytes(_error_response(req, resp.status_code, resp.reason)))

    def _authorize(self, request: SipSubscribeRequest) -> bool:
        """§5.4 accept/reject hook fed to core.SipNotifier."""
        allowed_raw = os.environ.get("LVF_SIP_ALLOWED_SUBSCRIBERS", "").strip()
        if not allowed_raw:
            return True
        allowed = {u.strip() for u in allowed_raw.split(",") if u.strip()}
        return request.subscriber_uri in allowed

    # ── NOTIFY delivery ───────────────────────────────────────────────────────

    def _send_notify(self, sub: SipSubscription, body: dict, mime: str) -> None:
        """Sync callback invoked by core (SipNotifier.send_notify). Builds the
        wire message and schedules the actual transmit; does not block core."""
        ctx = self._wire.get(sub.call_id)
        if ctx is None:
            log.warning("SIP: no wire context for call_id %s, cannot deliver NOTIFY", sub.call_id)
            return

        body_bytes = json.dumps(body).encode()
        target = _parse_contact_uri(ctx.contact_uri, ctx.remote_host, ctx.remote_port)

        notify = Request("NOTIFY", target, body=body_bytes)

        try:
            notify.to_address = Address.parse(ctx.from_addr)
        except (ValueError, AttributeError):
            pass

        try:
            notify.from_address = Address.parse(ctx.to_addr_with_tag)
        except (ValueError, AttributeError):
            notify.from_address = Address(uri=URI(scheme="sip", host=self._server_uri))

        notify.call_id = sub.call_id
        notify.cseq = CSeq(sub.notify_cseq, "NOTIFY")

        branch = f"z9hG4bK{uuid.uuid4().hex[:16]}"
        try:
            notify.via = [Via.parse(f"SIP/2.0/{ctx.transport} {self._server_uri};branch={branch}")]
        except ValueError:
            pass

        notify.max_forwards = 70
        notify.headers.add("Event", sub.event_package)

        remaining = max(0, int(sub.expires_at - time.monotonic()))
        notify.headers.add("Subscription-State", f"active;expires={remaining}")

        try:
            notify.content_type = MediaType.parse(mime)
        except ValueError:
            notify.headers.add("Content-Type", mime)

        notify.content_length = len(body_bytes)

        asyncio.ensure_future(self._transmit(bytes(notify), ctx))

    async def _send_terminating_notify(
        self, call_id: str, event_package: str, ctx: WireContext
    ) -> None:
        """Explicit Expires:0 unsubscribe — core has already dropped the
        subscription, so this is built directly from the WireContext rather
        than through _send_notify (which requires a live SipSubscription)."""
        mime = _ELEMENT_MIME_TYPE if event_package == _ELEMENT_EVENT_PACKAGE else _SERVICE_MIME_TYPE
        body = (
            self._element_notifier.get_notify_body()
            if event_package == _ELEMENT_EVENT_PACKAGE
            else self._service_notifier.get_notify_body()
        )
        body_bytes = json.dumps(body).encode()
        target = _parse_contact_uri(ctx.contact_uri, ctx.remote_host, ctx.remote_port)

        notify = Request("NOTIFY", target, body=body_bytes)

        try:
            notify.to_address = Address.parse(ctx.from_addr)
        except (ValueError, AttributeError):
            pass

        try:
            notify.from_address = Address.parse(ctx.to_addr_with_tag)
        except (ValueError, AttributeError):
            notify.from_address = Address(uri=URI(scheme="sip", host=self._server_uri))

        notify.call_id = call_id
        notify.cseq = CSeq(1, "NOTIFY")

        branch = f"z9hG4bK{uuid.uuid4().hex[:16]}"
        try:
            notify.via = [Via.parse(f"SIP/2.0/{ctx.transport} {self._server_uri};branch={branch}")]
        except ValueError:
            pass

        notify.max_forwards = 70
        notify.headers.add("Event", event_package)
        notify.headers.add("Subscription-State", "terminated;reason=timeout")

        try:
            notify.content_type = MediaType.parse(mime)
        except ValueError:
            notify.headers.add("Content-Type", mime)

        notify.content_length = len(body_bytes)

        await self._transmit(bytes(notify), ctx)

    async def _transmit(self, data: bytes, ctx: WireContext) -> None:
        if ctx.transport == "TCP":
            try:
                reader, writer = await asyncio.open_connection(ctx.remote_host, ctx.remote_port)
                writer.write(data)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception as exc:
                log.warning(
                    "SIP: TCP NOTIFY to %s:%d failed: %s",
                    ctx.remote_host, ctx.remote_port, exc,
                )
        else:
            if self._udp and self._udp.transport:
                self._udp.transport.sendto(data, (ctx.remote_host, ctx.remote_port))
            else:
                log.warning(
                    "SIP: UDP transport unavailable, cannot deliver NOTIFY to %s:%d",
                    ctx.remote_host, ctx.remote_port,
                )


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


def _error_response(
    req: Request, code: int, phrase: str, extra_headers: Optional[Dict[str, str]] = None
) -> Response:
    resp = Response(code, phrase)
    if req.via:
        resp.via = req.via
    resp.from_address = req.from_address
    resp.to_address = req.to_address
    resp.call_id = req.call_id
    resp.cseq = req.cseq
    resp.content_length = 0
    if extra_headers:
        for name, value in extra_headers.items():
            resp.headers.add(name, value)
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
