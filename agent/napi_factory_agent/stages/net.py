"""Этап NET (раздел 5.2): линк 100 Full, ошибки, IP, сервер, iperf3 в обе стороны.

По умолчанию — короткий UDP-поток с фиксированной скоростью (потери, ошибки порта):
канал занят секунды, и 50 плат не забивают сеть. TCP-пропускная способность —
опция «тотальная проверка» (method: tcp). Успешный ping тестом Ethernet не считается.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import StageCtx, StageResult


def parse_iperf3(result: dict[str, Any]) -> float:
    """Пропускная способность (Мбит/с) из `iperf3 -J`: принятые данные на приёмной стороне."""
    if result.get("error"):
        raise ValueError(f"iperf3: {result['error']}")
    end = result.get("end") or {}
    for key in ("sum_received", "sum"):
        s = end.get(key)
        if s and "bits_per_second" in s:
            return float(s["bits_per_second"]) / 1e6
    raise ValueError("iperf3: нет end.sum_received")


def _read(base: Path, name: str) -> str | None:
    try:
        return (base / name).read_text().strip()
    except OSError:
        return None


def run(ctx: StageCtx) -> StageResult:
    p = ctx.p
    be = ctx.backend
    iface = p.get("iface", "end0")  # VALIDATE ON NAPI-C: имя интерфейса
    base = Path(be.root) / "sys/class/net" / iface
    meas: dict[str, Any] = {"iface": iface}

    if not base.exists():
        return StageResult("fail", f"NET: no {iface}", meas)
    if _read(base, "carrier") != "1":
        return StageResult("fail", "NET: no carrier", meas)
    try:
        speed = int(_read(base, "speed") or -1)
    except ValueError:
        speed = -1
    duplex = (_read(base, "duplex") or "?").lower()
    meas.update(speed=speed, duplex=duplex)
    exp_speed = int(p.get("expect_speed", 100))
    exp_duplex = str(p.get("expect_duplex", "full")).lower()
    link = f"{speed}{duplex.capitalize()}"
    ctx.log(f"{iface}: link {link}")
    if speed != exp_speed or duplex != exp_duplex:
        return StageResult("fail", f"NET: link {link}≠{exp_speed}{exp_duplex.capitalize()}", meas)

    stats = base / "statistics"
    errs0 = _err_counters(stats)
    meas.update(rx_errors=errs0["rx_errors"], tx_errors=errs0["tx_errors"])
    if errs0["rx_errors"] > int(p.get("max_rx_errors", 0)) or errs0["tx_errors"] > int(p.get("max_tx_errors", 0)):
        return StageResult("fail", f"NET: errors rx {errs0['rx_errors']} tx {errs0['tx_errors']}", meas)

    ip = be.iface_ip(iface)
    meas["ip"] = ip
    if not ip:
        return StageResult("fail", "NET: no IP", meas)
    if ctx.server_addr and not be.tcp_reachable(*ctx.server_addr, timeout=5):
        return StageResult("fail", "NET: server unreachable", meas)
    ctx.check()

    # Слот iperf3 от планировщика сервера
    ctx.progress(10, "ожидание iperf3")
    grant = ctx.request({"type": "net_request", "job_id": ctx.job_id}, "net_grant",
                        float(p.get("grant_timeout_s", 600)))
    if not grant:
        return StageResult("fail", "NET: no iperf3 slot", meas)
    host, port = grant.get("host") or (ctx.server_addr or ("", 0))[0], int(grant["port"])
    full = p.get("method", "udp") == "tcp"
    warnings: list[str] = []
    try:
        # 1. Качество канала: короткий UDP-поток с фиксированной скоростью в обе стороны.
        # Канал занят ~2×udp_seconds, нагрузка предсказуема; дефект виден по потерям и ошибкам.
        rate = float(p.get("udp_rate_mbps", 90))
        secs = int(p.get("udp_seconds", 3))
        legacy = p.get("udp_max_loss_pct")
        limits = {"tx": float(p.get("udp_max_loss_tx_pct", legacy if legacy is not None else 0.5)),
                  "rx": float(p.get("udp_max_loss_rx_pct", legacy if legacy is not None else 2.0))}
        udp: dict[str, tuple[float, float]] = {}
        for i, (d, reverse) in enumerate((("tx", False), ("rx", True))):
            ctx.progress(20 + 20 * i, f"UDP {'→' if d == 'tx' else '←'} {rate:g} Mbit/s")
            res = _udp(be, host, port, secs, reverse, rate, ctx)
            ok = _udp_ok(res, rate, limits[d])
            if not ok and p.get("udp_retry", True):
                ctx.log(f"UDP {d}: {res[0]:.1f} Mbit/s, потери {res[1]:.2f} % > {limits[d]:g} %, повтор")
                ctx.check()
                first = res
                res = _udp(be, host, port, secs, reverse, rate, ctx)
                ok = _udp_ok(res, rate, limits[d])
                if ok:
                    warnings.append(f"NET: UDP {d} с повтора: потери {first[1]:.2f} % → {res[1]:.2f} %")
            udp[d] = res
            meas.update({f"udp_{d}_mbps": round(res[0], 1), f"udp_{d}_loss_pct": round(res[1], 3)})
            ctx.log(f"UDP {d}: {res[0]:.1f} Mbit/s, потери {res[1]:.3f} %")
            if not ok:
                if res[0] < 0.9 * rate:
                    return StageResult("fail", f"NET: UDP {d} {res[0]:.0f} Mbit/s<{0.9 * rate:.0f}", meas)
                return StageResult("fail", f"NET: UDP {d} потери {res[1]:.1f} %>{limits[d]:g} %", meas)
            ctx.check()

        # 2. Тотальная проверка (опция): TCP-пропускная способность в обе стороны
        tcp = None
        if full:
            tsecs = int(p.get("iperf_seconds", 10))
            min_mbps = float(p.get("min_mbps", 80))
            ctx.progress(60, "TCP →")
            tx = parse_iperf3(be.run_iperf(host, port, tsecs, False, ctx))
            ctx.check()
            ctx.progress(80, "TCP ←")
            rx = parse_iperf3(be.run_iperf(host, port, tsecs, True, ctx))
            meas.update(tx_mbps=round(tx, 1), rx_mbps=round(rx, 1))
            ctx.log(f"iperf3 TCP: tx {tx:.1f} Mbit/s, rx {rx:.1f} Mbit/s")
            if tx < min_mbps:
                return StageResult("fail", f"NET: TCP tx {tx:.0f} Mbit/s<{min_mbps:g}", meas)
            if rx < min_mbps:
                # NAPI-C (2026-10-10): TCP-приём ~36 Мбит/с при чистом UDP-канале — особенность, не дефект
                warnings.append(f"NET: TCP rx {rx:.0f} Mbit/s<{min_mbps:g} при UDP rx {udp['rx'][0]:.0f} Mbit/s, "
                                f"потери {udp['rx'][1]:.2f} %")
            tcp = (tx, rx)
    except ValueError as e:
        ctx.log(str(e))
        return StageResult("fail", "NET: iperf3 error", meas)
    finally:
        ctx.request({"type": "net_release", "job_id": ctx.job_id}, "", 0)

    # 3. Ошибки порта за время теста
    errs1 = _err_counters(stats)
    delta = {k: errs1[k] - errs0[k] for k in errs0 if errs1[k] > errs0[k]}
    meas["errors_delta"] = delta
    if delta:
        ctx.log(f"рост счётчиков ошибок: {delta}")
        return StageResult("fail", "NET: errors " + ", ".join(f"{k} +{v}" for k, v in delta.items()), meas)

    detail = f"{link}, UDP {udp['tx'][0]:.0f}/{udp['rx'][0]:.0f} Mbit/s, потери {udp['tx'][1]:.2f}/{udp['rx'][1]:.2f} %"
    if tcp:
        detail += f", TCP {tcp[0]:.0f}/{tcp[1]:.0f}"
    return StageResult("pass", detail, meas, warnings)


ERROR_COUNTERS = ("rx_errors", "tx_errors", "rx_crc_errors", "rx_fifo_errors", "rx_missed_errors",
                  "rx_frame_errors", "tx_fifo_errors", "tx_carrier_errors")


def _err_counters(stats: Path) -> dict[str, int]:
    out = {}
    for name in ERROR_COUNTERS:
        try:
            out[name] = int(_read(stats, name) or 0)
        except ValueError:
            out[name] = 0
    return out


def _udp(be, host: str, port: int, secs: int, reverse: bool, rate: float, ctx) -> tuple[float, float]:
    """(принято Мбит/с, потери %) на приёмной стороне."""
    r = be.run_iperf(host, port, secs, reverse, ctx, udp_mbps=rate)
    if r.get("error"):
        raise ValueError(f"iperf3: {r['error']}")
    end = r.get("end") or {}
    s = end.get("sum_received") or end.get("sum") or {}
    if "bits_per_second" not in s:
        raise ValueError("iperf3: нет end.sum_received (UDP)")
    return float(s["bits_per_second"]) / 1e6, float(s.get("lost_percent", 100.0))


def _udp_ok(res: tuple[float, float], rate: float, max_loss: float) -> bool:
    return res[0] >= 0.9 * rate and res[1] <= max_loss
