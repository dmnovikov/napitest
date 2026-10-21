"""Карточка платы, история партий, экспорт CSV/JSON и этикетка с QR."""
from __future__ import annotations

import csv
import html
import io
from typing import Any

import segno

from ..config import STAGES
from ..db.database import loads
from .factory import BOARDS as BOARD_NAMES
from .factory import Factory, FactoryError

CSV_FIELDS = ["serial", "cpuid", "board", "board_detected", "uplink", "batch", "date", "mode", "status",
              "test_sd_version", "os", "image_version",
              "image_file", "image_sha256", "image_url", "agent_version", "job_id", "warnings"] + [f"{s}_{k}" for s in STAGES for k in ("state", "detail")]


class Reports:
    def __init__(self, factory: Factory):
        self.f = factory
        self.db = factory.db

    def device_card(self, device_id: int) -> dict:
        dev = self.db.one("SELECT * FROM device WHERE id = ?", (device_id,))
        if dev is None:
            raise FactoryError("неизвестная плата", 404)
        jobs = []
        for job in self.db.all("SELECT * FROM job WHERE device_id = ? ORDER BY created_at DESC, rowid DESC",
                               (device_id,)):
            jobs.append(self._job_detail(job, include_events=False))
        return {
            "device": self.f.device_view(dev),
            "inventory": loads(dev["inventory_json"]),
            "sd_selfcheck": loads(dev["selfcheck_json"]),
            "tested_pass": bool(dev["tested_pass"]),
            "first_seen": dev["first_seen"],
            "jobs": jobs,
        }

    def _job_detail(self, job, include_events: bool) -> dict:
        stages_now = self.f.job_stages(job)
        rows = {r["stage"]: r for r in self.db.all("SELECT * FROM stage_result WHERE job_id = ?", (job["id"],))}
        stages = []
        for s in STAGES:
            row = rows.get(s)
            cur = stages_now.get(s, {})
            stages.append({
                "stage": s,
                "state": cur.get("state", row["state"] if row else "pending"),
                "t": cur.get("t", row["t"] if row else None),
                "detail": cur.get("detail", row["detail"] if row else None),
                "measurements": loads(row["measurements_json"], {}) if row else {},
                "warnings": loads(row["warnings_json"], []) if row else [],
                "log": row["log"] if row else None,
            })
        batch = self.db.one("SELECT name FROM batch WHERE id = ?", (job["batch_id"],))
        out = {
            "job_id": job["id"], "batch_id": job["batch_id"], "batch": batch["name"] if batch else None,
            "mode": job["mode"], "status": job["status"], "result": job["result"], "detail": job["detail"],
            "created_at": job["created_at"], "started_at": job["started_at"], "finished_at": job["finished_at"],
            "image_version": job["image_version"], "image_sha256": job["image_sha256"],
            "os_name": job["os_name"], "image_file": job["image_file"], "image_url": job["image_url"],
            "test_sd_version": job["test_sd_version"], "agent_version": job["agent_version"],
            "board_type": job["board_type"], "board_detected": job["board_detected"], "uplink_iface": job["uplink_iface"],
            "stages": stages,
        }
        if include_events:
            out["events"] = [
                {"seq": e["seq"], "ts": e["ts"], "kind": e["kind"], "stage": e["stage"], "data": loads(e["data_json"])}
                for e in self.db.all("SELECT * FROM event WHERE job_id = ? ORDER BY stream, seq", (job["id"],))
            ]
        return out

    def job(self, job_id: str) -> dict:
        job = self.db.one("SELECT * FROM job WHERE id = ?", (job_id,))
        if job is None:
            raise FactoryError("неизвестное задание", 404)
        return self._job_detail(job, include_events=True)

    def _batch_view(self, b) -> dict:
        jobs = self.db.all("SELECT status FROM job WHERE batch_id = ?", (b["id"],))
        versions = [r["v"] for r in self.db.all(
            "SELECT DISTINCT test_sd_version AS v FROM job WHERE batch_id = ? AND test_sd_version IS NOT NULL",
            (b["id"],))]
        return {
            "batch_id": b["id"], "name": b["name"], "mode": b["mode"], "kind": b["kind"],
            "operator": b["username"], "started_at": b["started_at"], "finished_at": b["finished_at"],
            "image_version": b["image_version"], "test_sd_versions": versions,
            "os": next((r["os_name"] for r in self.db.all(
                "SELECT DISTINCT os_name FROM job WHERE batch_id = ? AND os_name IS NOT NULL", (b["id"],))), None),
            "total": len(jobs),
            "pass": sum(1 for j in jobs if j["status"] == "pass"),
            "warn": sum(1 for j in jobs if j["status"] == "warn"),
            "fail": sum(1 for j in jobs if j["status"] in ("fail", "interrupted", "offline")),
            "golden": b["golden_result"],
        }

    def batches(self, limit: int = 200) -> list[dict]:
        rows = self.db.all(
            "SELECT b.*, o.username FROM batch b LEFT JOIN operator o ON o.id = b.operator_id"
            " ORDER BY b.id DESC LIMIT ?", (limit,))
        return [self._batch_view(b) for b in rows]

    def batch(self, batch_id: int) -> dict:
        b = self.db.one("SELECT b.*, o.username FROM batch b LEFT JOIN operator o ON o.id = b.operator_id"
                        " WHERE b.id = ?", (batch_id,))
        if b is None:
            raise FactoryError("неизвестная партия", 404)
        jobs = []
        for job in self.db.all(
            "SELECT j.*, d.serial, d.cpuid FROM job j JOIN device d ON d.id = j.device_id WHERE j.batch_id = ?"
            " ORDER BY d.display_no, d.id", (batch_id,)):
            stages = self.f.job_stages(job)
            jobs.append({
                "job_id": job["id"], "device_id": job["device_id"], "serial": job["serial"], "cpuid": job["cpuid"],
                "board_type": job["board_type"], "status": job["status"], "result": job["result"], "detail": job["detail"],
                "stages": {s: {"state": v.get("state"), "detail": v.get("detail"), "warnings": v.get("warnings") or []}
                           for s, v in stages.items()},
            })
        return {"batch": self._batch_view(b), "jobs": jobs}

    def export_rows(self, batch_id: int) -> list[dict[str, Any]]:
        b = self.db.one("SELECT * FROM batch WHERE id = ?", (batch_id,))
        if b is None:
            raise FactoryError("неизвестная партия", 404)
        rows = []
        for job in self.db.all(
            "SELECT j.*, d.serial, d.cpuid FROM job j JOIN device d ON d.id = j.device_id WHERE j.batch_id = ?"
            " ORDER BY d.display_no, d.id", (batch_id,)):
            stages = self.f.job_stages(job)
            row = {
                "serial": job["serial"] or "", "cpuid": job["cpuid"], "board": BOARD_NAMES.get(job["board_type"], ""),
                "board_detected": BOARD_NAMES.get(job["board_detected"], ""), "uplink": job["uplink_iface"] or "",
                "batch": b["name"],
                "date": (job["finished_at"] or job["created_at"])[:10], "mode": job["mode"], "status": job["status"],
                "test_sd_version": job["test_sd_version"] or "", "os": job["os_name"] or "",
                "image_version": job["image_version"] or "", "image_file": job["image_file"] or "",
                "image_sha256": job["image_sha256"] or "", "image_url": job["image_url"] or "",
                "agent_version": job["agent_version"] or "", "job_id": job["id"],
            }
            row["warnings"] = "; ".join(w for s in STAGES for w in stages[s].get("warnings") or [])
            for s in STAGES:
                row[f"{s}_state"] = stages[s].get("state", "")
                row[f"{s}_detail"] = stages[s].get("detail") or ""
            rows.append(row)
        return rows

    def export_csv(self, batch_id: int) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(self.export_rows(batch_id))
        return "﻿" + buf.getvalue()  # BOM: корректная кириллица в Excel

    def export_json(self, batch_id: int) -> dict:
        return {"batch": self.batch(batch_id)["batch"], "rows": self.export_rows(batch_id)}

    def audit(self, limit: int = 500) -> list[dict]:
        return [
            {"ts": r["ts"], "operator": r["operator"], "action": r["action"], "detail": loads(r["detail_json"])}
            for r in self.db.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        ]

    def label_html(self, device_id: int) -> str:
        dev = self.db.one("SELECT * FROM device WHERE id = ?", (device_id,))
        if dev is None:
            raise FactoryError("неизвестная плата", 404)
        job = self.db.one(
            "SELECT j.*, b.name AS batch_name FROM job j JOIN batch b ON b.id = j.batch_id"
            " WHERE j.device_id = ? AND j.status IN ('pass', 'warn') ORDER BY j.finished_at DESC LIMIT 1", (device_id,))
        if not dev["serial"] or job is None:
            raise FactoryError("этикетка печатается только для платы с PASS или WARN и серийником", 409)
        date = (job["finished_at"] or "")[:10]
        payload = f"SN:{dev['serial']};B:{job['batch_name']};D:{date}"
        qr = segno.make(payload, error="m").svg_inline(scale=3, border=1)
        e = html.escape
        return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Этикетка {e(dev['serial'])}</title>
<style>
@page {{ size: 50mm 30mm; margin: 0; }}
body {{ margin: 0; font-family: sans-serif; }}
.label {{ width: 50mm; height: 30mm; display: flex; align-items: center; gap: 2mm; padding: 1.5mm; box-sizing: border-box; }}
.label svg {{ width: 26mm; height: 26mm; }}
.t {{ font-size: 8pt; line-height: 1.25; }}
.sn {{ font-weight: bold; font-size: 10pt; }}
@media screen {{ body {{ padding: 20px; background: #eee; }} .label {{ background: #fff; border: 1px dashed #999; }} }}
</style></head>
<body onload="window.print()"><div class="label">{qr}<div class="t">
<div class="sn">{e(dev['serial'])}</div><div>{e(job['batch_name'])}</div><div>{e(date)}</div>
<div>{e(job['mode'])} PASS</div></div></div></body></html>"""
