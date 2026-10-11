"""Агент ↔ фейковый WS-сервер: hello, задание, обрыв посреди задания, досылка очереди."""

import asyncio
import json
import time

import websockets

from napi_factory_agent.main import run_agent
from napi_factory_agent.testing import job_params

from .conftest import make_agent


class FakeServer:
    def __init__(self):
        self.hellos = []
        self.events = {}            # (stream, seq) -> event
        self.order = []
        self.heartbeats = []
        self.refuse_until = 0.0
        self.dropped = False
        self.job_sent = False
        self.auth = []

    async def handler(self, ws, path=None):
        self.auth.append(ws.request_headers.get("Authorization"))
        if time.monotonic() < self.refuse_until:
            await ws.close(1013, "busy")
            return
        async for raw in ws:
            m = json.loads(raw)
            t = m["type"]
            if t == "hello":
                self.hellos.append(m)
                await ws.send(json.dumps({"type": "hello_ack", "device_id": 1, "cpuid": m["cpuid"], "serial": None}))
                if not self.job_sent:
                    self.job_sent = True
                    await ws.send(json.dumps({"type": "job", "job_id": "jE", "mode": "test_only",
                                              "params": job_params("test_only")}))
                elif m["current_job"]:
                    # повторная выдача того же job_id не перезапускает задание
                    await ws.send(json.dumps({"type": "job", "job_id": "jE", "mode": "test_only",
                                              "params": job_params("test_only")}))
            elif t == "event":
                key = (m["stream"], m["seq"])
                if key not in self.events:
                    self.events[key] = m
                    self.order.append(m["seq"])
                await ws.send(json.dumps({"type": "ack", "stream": m["stream"], "seq": m["seq"]}))
            elif t == "heartbeat":
                self.heartbeats.append(m)
                st = m["stages"].get("ram", {})
                if st.get("state") == "running" and not self.dropped:
                    # обрыв связи посреди этапа RAM: агент продолжает локально
                    self.dropped = True
                    self.refuse_until = time.monotonic() + 1.5
                    await ws.close()
                    return
            elif t == "net_request":
                await ws.send(json.dumps({"type": "net_grant", "job_id": m["job_id"], "host": "127.0.0.1",
                                          "port": 5201}))


def test_agent_e2e_with_drop(tmp_path, images):
    srv = FakeServer()

    async def go():
        server = await websockets.serve(srv.handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        agent = make_agent(tmp_path, link=False)
        agent.backend.speed = 8
        link = await run_agent(agent, f"ws://127.0.0.1:{port}/ws/agent", "tok", heartbeat_s=0.2, mdns=False)
        try:
            for _ in range(400):
                await asyncio.sleep(0.05)
                if any(e["kind"] == "job_finished" for e in srv.events.values()) and len(agent.queue) == 0:
                    break
        finally:
            link.stop()
            await asyncio.wait_for(link.task, 5)
            server.close()
            await server.wait_closed()
        return agent

    agent = asyncio.run(go())
    assert srv.dropped
    assert len(srv.hellos) >= 2
    assert srv.auth[0] == "Bearer tok"
    h1 = srv.hellos[0]
    assert h1["simulated"] is True and h1["inventory"]["ok"] and h1["inventory"]["target"] == "mmcblk0"
    # самопроверка идёт в фоне: первый hello может прийти с running, итог — повторным hello
    assert h1["sd_selfcheck"]["state"] in ("running", "pass")
    assert any(h["sd_selfcheck"]["state"] == "pass" for h in srv.hellos)
    assert h1["test_sd_version"] == "2026.10.1"
    # снимок незавершённого задания при реконнекте
    assert any((h.get("current_job") or {}).get("job_id") == "jE" for h in srv.hellos[1:])
    seqs = sorted(s for (_, s) in srv.events)
    assert seqs == list(range(1, len(seqs) + 1))        # ни одно событие не потеряно
    kinds = [srv.events[(h1["stream"], s)]["kind"] for s in seqs]
    assert kinds[0] == "job_started" and kinds[-1] == "job_finished"
    fin = srv.events[(h1["stream"], seqs[-1])]
    assert fin["data"]["result"] == "pass"
    assert agent.job["result"] == "pass"
    assert len(agent.queue) == 0                          # всё подтверждено
    assert sum(1 for e in srv.events.values() if e["kind"] == "job_started") == 1   # не перезапускалось
