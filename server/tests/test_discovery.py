import asyncio
import json
import socket

from app.discovery import DiscoveryResponder


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ask(port, payload):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(1)
        s.sendto(payload, ("127.0.0.1", port))
        try:
            return json.loads(s.recv(2048))
        except socket.timeout:
            return None


def test_discovery_reply_and_ignores_garbage():
    port = free_udp_port()

    async def go():
        r = DiscoveryResponder(port, 8080)
        await r.start()
        try:
            ok = await asyncio.to_thread(ask, port, b'{"type":"napi_discover","v":1}')
            bad = await asyncio.to_thread(ask, port, b"hello")
        finally:
            await r.stop()
        return ok, bad
    ok, bad = asyncio.run(go())
    assert ok == {"type": "napi_server", "v": 1, "http": "http://127.0.0.1:8080",
                  "ws": "ws://127.0.0.1:8080/ws/agent", "version": ok["version"]}
    assert bad is None


def test_discovery_public_url():
    port = free_udp_port()

    async def go():
        r = DiscoveryResponder(port, 8080, "https://factory.example:8443/")
        await r.start()
        try:
            return await asyncio.to_thread(ask, port, b'{"type":"napi_discover"}')
        finally:
            await r.stop()
    rep = asyncio.run(go())
    assert rep["http"] == "https://factory.example:8443" and rep["ws"] == "wss://factory.example:8443/ws/agent"
