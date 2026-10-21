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


def test_discovery_direct_network_beats_public_url(tmp_path):
    from app.discovery import _Protocol
    # 192.168.55.0/24 подключена напрямую, 10.0.0.0/8 — через шлюз
    routes = tmp_path / "route"
    routes.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "enp2s0\t0037A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
        "enp1s0\t0000000A\t0110A8C0\t0003\t0\t0\t0\t000000FF\n")
    p = _Protocol(8080, "http://192.168.16.100:8080", route_path=str(routes))
    import app.discovery as d
    real = d.local_ip_for
    d.local_ip_for = lambda peer: "192.168.55.100" if peer.startswith("192.168.55.") else "192.168.16.100"
    try:
        near = p.reply_for(b'{"type":"napi_discover"}', "192.168.55.80")
        far = p.reply_for(b'{"type":"napi_discover"}', "10.1.2.3")
    finally:
        d.local_ip_for = real
    assert near["ws"] == "ws://192.168.55.100:8080/ws/agent"
    assert far["ws"] == "ws://192.168.16.100:8080/ws/agent"
