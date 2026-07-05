#!/usr/bin/env python3
"""SIP SUBSCRIBE/NOTIFY live smoke test (§2.4.1).

Exercises the three things unit tests can't prove on the wire:
  1. SUBSCRIBE (Expires>=60)  -> 200 OK + an initial NOTIFY
  2. SUBSCRIBE (Expires=10)   -> 423 Interval Too Brief WITH Min-Expires: 60
  3. SUBSCRIBE (Expires=0)    -> 200 OK + a terminating NOTIFY

Run against a LIVE LVF with the SIP listener up (LVF_ENABLE_SIP=true, leader):
    python tests/smoke/sip_smoke.py                 # defaults to 127.0.0.1:5060
    python tests/smoke/sip_smoke.py <host> <port>

Uses raw UDP so there are no extra deps. Binds a fixed local port and advertises
it as Contact, so both the response and the NOTIFY land back on our socket.
"""
import socket
import sys
import uuid

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5060
LOCAL_PORT = 55160  # our Contact port; NOTIFYs come here

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", LOCAL_PORT))
sock.settimeout(3.0)
local_ip = socket.gethostbyname(socket.gethostname())


def subscribe(call_id: str, expires: int, cseq: int = 1) -> str:
    tag = uuid.uuid4().hex[:8]
    branch = f"z9hG4bK{uuid.uuid4().hex[:16]}"
    msg = (
        f"SUBSCRIBE sip:lvf@{HOST}:{PORT} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{LOCAL_PORT};branch={branch}\r\n"
        f"From: <sip:smoke@{local_ip}>;tag={tag}\r\n"
        f"To: <sip:lvf@{HOST}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} SUBSCRIBE\r\n"
        f"Contact: <sip:smoke@{local_ip}:{LOCAL_PORT}>\r\n"
        f"Event: emergency-ElementState\r\n"
        f"Max-Forwards: 70\r\n"
        f"Expires: {expires}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    sock.sendto(msg.encode(), (HOST, PORT))
    return msg


def drain(label: str, want_status: str = "", want_header: str = "",
          want_notify: bool = False) -> bool:
    """Read datagrams for a moment; report what arrived."""
    got_status = got_header = got_notify = False
    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            break
        text = data.decode(errors="replace")
        first = text.split("\r\n", 1)[0]
        if text.startswith("SIP/2.0"):
            print(f"    ← response: {first}")
            if want_status and want_status in first:
                got_status = True
            if want_header and any(
                l.lower().startswith(want_header.lower()) for l in text.split("\r\n")
            ):
                got_header = True
                hv = next(l for l in text.split("\r\n")
                          if l.lower().startswith(want_header.lower()))
                print(f"      · {hv}")
        elif first.startswith("NOTIFY"):
            substate = next((l for l in text.split("\r\n")
                             if l.lower().startswith("subscription-state")), "")
            print(f"    ← NOTIFY   ({substate})")
            got_notify = True
    ok = True
    if want_status:
        ok &= got_status
    if want_header:
        ok &= got_header
    if want_notify:
        ok &= got_notify
    print(f"  {'✓' if ok else '✗'} {label}")
    return ok


print(f"Target LVF SIP: {HOST}:{PORT}  (our Contact: {local_ip}:{LOCAL_PORT})\n")
results = []

cid = f"smoke-{uuid.uuid4().hex[:12]}"
print("[1] SUBSCRIBE Expires=120  → expect 200 OK + initial NOTIFY")
subscribe(cid, 120)
results.append(drain("200 OK + initial NOTIFY", want_status="200", want_notify=True))

print("\n[2] SUBSCRIBE Expires=10   → expect 423 + Min-Expires: 60")
subscribe(f"smoke-{uuid.uuid4().hex[:12]}", 10)
results.append(drain("423 Interval Too Brief + Min-Expires",
                     want_status="423", want_header="Min-Expires"))

print("\n[3] SUBSCRIBE Expires=0    → expect 200 OK + terminated NOTIFY")
subscribe(cid, 0, cseq=2)  # unsubscribe the dialog from step 1
results.append(drain("200 OK + terminated NOTIFY", want_status="200", want_notify=True))

sock.close()
print(f"\n=== {sum(results)}/{len(results)} checks passed ===")
sys.exit(0 if all(results) else 1)
