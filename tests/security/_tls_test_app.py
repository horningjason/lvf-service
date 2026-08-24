"""Minimal ASGI app for tests/security's live-handshake tests.

Deliberately not src.server:app: the handshake tests exercise the TLS
context-construction and injection mechanism (main.py), not LVF business
logic, which needs GIS data and .env configuration that would only add
unrelated failure modes to a security test.
"""
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route


async def _ok(request):
    return PlainTextResponse("ok")


app = Starlette(routes=[Route("/", _ok)])
