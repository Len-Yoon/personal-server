#!/usr/bin/env python3
"""Closed-loop, loopback-only measurements of the real search aggregation function."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import platform
import resource
import secrets
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ("news", "youtube", "books")
SCENARIOS = ("normal", "delayed", "timeout", "http_failure")
PAYLOAD = {"results": [{"title": "fixed lab result", "url": "#"}]}
SAMPLE_HASH = hashlib.sha256(json.dumps(PAYLOAD, sort_keys=True).encode()).hexdigest()
VERSION = 1


def percentile(samples, quantile):
    if not samples or not 0 < quantile <= 1:
        raise ValueError("invalid percentile input")
    values = sorted(samples)
    return values[math.ceil(len(values) * quantile) - 1]


def summarize(samples, elapsed, planned, started):
    completed = len(samples)
    latencies = [row["latency_seconds"] for row in samples]
    return {
        "planned": planned, "started": started, "completed": completed,
        "cancelled": started - completed, "not_started": planned - started,
        "elapsed_seconds": elapsed,
        "throughput_per_second": completed / elapsed if elapsed > 0 else None,
        "all_ok_ratio": sum(row["valid"] and row["statuses"] == ["ok"] * 3 for row in samples) / completed if completed else None,
        "partial_failure_ratio": sum("unavailable" in row["statuses"] for row in samples) / completed if completed else None,
        "contract_violations": sum(not row["valid"] for row in samples),
        "latency_seconds": {name: percentile(latencies, q) if latencies else None
                            for name, q in (("p50", .5), ("p95", .95), ("p99", .99))}
                           | {"min": min(latencies) if latencies else None, "max": max(latencies) if latencies else None},
    }


def clean_environment():
    # Do not copy credentials, proxy settings, demo flags or external service URLs.
    return {"PATH": os.defpath, "DEMO_MODE": "0", "NO_PROXY": "127.0.0.1,localhost"}


def usage():
    value = resource.getrusage(resource.RUSAGE_SELF)
    return {"user_cpu_seconds": value.ru_utime, "system_cpu_seconds": value.ru_stime,
            "high_water_rss_bytes": int(value.ru_maxrss if sys.platform == "darwin" else value.ru_maxrss * 1024),
            "scope": "cell_process_lifetime_including_warmup"}


def server_process(connection, sockets, nonce, scenario):
    os.environ.clear()
    os.environ.update(clean_environment())

    async def serve():
        handlers = set()

        async def handle(reader, writer, source):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                request = raw.split(b"\r\n", 1)[0].decode("ascii")
                expected = f"GET /{nonce}/{source}?"
                if not request.startswith(expected):
                    status, payload = 404, b"{}"
                else:
                    if source == "books" and scenario in {"delayed", "timeout"}:
                        await asyncio.sleep(.5 if scenario == "delayed" else 2)
                    status = 503 if source == "books" and scenario == "http_failure" else 200
                    payload = json.dumps(PAYLOAD).encode() if status == 200 else b"{}"
                writer.write(f"HTTP/1.1 {status} Lab\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
                await writer.drain()
            except (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                handlers.discard(task)

        servers = []
        try:
            for source, sock in zip(SOURCES, sockets):
                async def callback(reader, writer, source=source):
                    await handle(reader, writer, source)
                servers.append(await asyncio.start_server(callback, sock=sock, limit=8192))
            connection.send({"type": "ready", "nonce": nonce, "sample_hash": SAMPLE_HASH})
            while not connection.poll():
                await asyncio.sleep(.02)
            connection.recv()
        finally:
            for server in servers:
                server.close()
            for server in servers:
                await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*list(handlers), return_exceptions=True)

    try:
        asyncio.run(serve())
        connection.send({"type": "resources", "value": usage()})
    except Exception:
        # Never serialize exception text or ambient configuration.
        pass
    finally:
        connection.close()


def load_process(connection, ports, nonce, scenario, concurrency, count):
    os.environ.clear()
    os.environ.update(clean_environment())
    for source, port in zip(SOURCES, ports):
        os.environ[f"{source.upper()}_SEARCH_URL"] = f"http://127.0.0.1:{port}/{nonce}/{source}"
    path = ROOT / "portal-web/app/services/global_search.py"
    spec = importlib.util.spec_from_file_location("lab_real_global_search", path)
    module = importlib.util.module_from_spec(spec)

    async def run():
        async def one(phase):
            start = time.monotonic()
            connection.send({"type": "started", "phase": phase, "at": start})
            statuses = ["error"] * 3
            valid, reason = False, "exception"
            try:
                result = await asyncio.wait_for(module.search_all("lab-fixed-query"), timeout=5)
                statuses = [result[source]["status"] for source in SOURCES]
                valid = all(status in {"ok", "unavailable"} and
                            result[source]["items"] == (PAYLOAD["results"] if status == "ok" else [])
                            for source, status in zip(SOURCES, statuses))
                if scenario in {"timeout", "http_failure"}:
                    valid = valid and statuses == ["ok", "ok", "unavailable"]
                elif scenario == "normal" and concurrency == 1:
                    valid = valid and statuses == ["ok"] * 3
                reason = "ok" if valid else "contract"
            except asyncio.TimeoutError:
                reason = "safety_deadline"
            except Exception:
                pass
            end = time.monotonic()
            connection.send({"type": "sample", "phase": phase, "at": end,
                             "row": {"latency_seconds": end - start, "statuses": statuses,
                                     "valid": valid, "reason": reason}})

        for _ in range(5):
            await one("warmup")
        work = iter(range(count))

        async def worker():
            for _ in work:
                await one("measurement")
        await asyncio.gather(*(worker() for _ in range(concurrency)))

    try:
        spec.loader.exec_module(module)
        asyncio.run(run())
        connection.send({"type": "done", "resources": usage()})
    except Exception:
        connection.send({"type": "error"})
    finally:
        connection.close()


def stop_processes(processes, server_connection):
    """Graceful stop, then terminate/kill only Process objects created here."""
    try:
        server_connection.send("stop")
    except (BrokenPipeError, EOFError, OSError):
        pass
    end = time.monotonic() + 5
    for process in processes:
        process.join(max(0, end - time.monotonic()))
    for process in processes:
        if process.is_alive():
            process.terminate()
    # Termination and final reap share the second five-second budget.
    end = time.monotonic() + 5
    for process in processes:
        process.join(min(.5, max(0, end - time.monotonic())))
    for process in processes:
        if process.is_alive():
            process.kill()
    for process in processes:
        process.join(max(0, end - time.monotonic()))
    return all(not process.is_alive() for process in processes)


def run_cell(scenario, concurrency, count, deadline, interrupted=lambda: False):
    ctx = mp.get_context("spawn")
    server_parent, server_child = ctx.Pipe()
    load_parent, load_child = ctx.Pipe()
    sockets, processes = [], []
    rows, warmup = [], []
    starts = {"warmup": [], "measurement": []}
    ends = {"warmup": [], "measurement": []}
    result = {"scenario": scenario, "concurrency": concurrency, "samples": rows,
              "resources": {}, "valid": False, "reason": "initialization", "cleanup_ok": False}
    ports = []
    done = False

    def record(event, cutoff=float("inf")):
        if event.get("type") not in {"started", "sample"} or event["at"] > cutoff:
            return
        phase = event["phase"]
        if event["type"] == "started":
            starts[phase].append(event["at"])
        else:
            (rows if phase == "measurement" else warmup).append(event["row"])
            ends[phase].append(event["at"])

    try:
        for _ in SOURCES:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        nonce = secrets.token_hex(16)
        server = ctx.Process(target=server_process, args=(server_child, sockets, nonce, scenario))
        server.start()
        processes.append(server)
        server_child.close()
        for sock in sockets:
            sock.close()
        ready_limit = min(deadline, time.monotonic() + 10)
        while not server_parent.poll(.02):
            if interrupted():
                result["reason"] = "interrupted"
                return result
            if time.monotonic() >= ready_limit or not server.is_alive():
                result["reason"] = "deadline" if time.monotonic() >= deadline else "server_not_ready"
                return result
        ready = server_parent.recv()
        if ready != {"type": "ready", "nonce": nonce, "sample_hash": SAMPLE_HASH}:
            result["reason"] = "server_not_ready"
            return result
        if interrupted() or time.monotonic() >= deadline:
            result["reason"] = "interrupted" if interrupted() else "deadline"
            return result
        load = ctx.Process(target=load_process, args=(load_child, ports, nonce, scenario, concurrency, count))
        load.start()
        processes.insert(0, load)
        load_child.close()
        while True:
            if interrupted() or time.monotonic() >= deadline:
                result["reason"] = "interrupted" if interrupted() else "deadline"
                break
            if load_parent.poll(.02):
                try:
                    event = load_parent.recv()
                except EOFError:
                    result["reason"] = "load_failed"
                    break
                kind = event["type"]
                if kind in {"started", "sample"}:
                    record(event)
                elif kind == "done":
                    result["resources"]["load"] = event["resources"]
                    done = True
                    result["reason"] = "ok"
                    break
                else:
                    result["reason"] = "load_failed"
                    break
            elif not load.is_alive() or not server.is_alive():
                result["reason"] = "child_failed"
                break
    except (OSError, EOFError):
        result["reason"] = "process_failure"
    finally:
        stopped_at = time.monotonic()
        result["cleanup_ok"] = stop_processes(processes, server_parent)
        # Preserve events emitted before cutoff, even if the parent had not read
        # them yet. Requests completed during cleanup do not change the sample.
        try:
            while time.monotonic() < stopped_at + 10 and load_parent.poll():
                record(load_parent.recv(), cutoff=stopped_at)
        except (OSError, EOFError):
            pass
        # Drain the server's final resource message after its clean shutdown.
        try:
            if server_parent.poll():
                event = server_parent.recv()
                if event.get("type") == "resources":
                    result["resources"]["server"] = event["value"]
        except (OSError, EOFError):
            pass
        for connection in (server_parent, server_child, load_parent, load_child):
            connection.close()
        for sock in sockets:
            sock.close()
        for phase, samples, planned in (("measurement", rows, count), ("warmup", warmup, 5)):
            start = min(starts[phase]) if starts[phase] else None
            end = max(ends[phase]) if len(samples) == planned else stopped_at
            summary = summarize(samples, end - start if start is not None else 0, planned, len(starts[phase]))
            result["summary" if phase == "measurement" else "warmup"] = summary
        result["resource_missing"] = [name for name in ("load", "server") if name not in result["resources"]]
        result["children_exited_cleanly"] = all(p.exitcode == 0 for p in processes)
        result["valid"] = (done and len(rows) == count and len(warmup) == 5
                           and all(row["valid"] for row in rows + warmup) and result["cleanup_ok"]
                           and result["children_exited_cleanly"])
        if done and not result["valid"]:
            result["reason"] = ("cleanup_failed" if not result["cleanup_ok"] else
                                "child_failed" if not result["children_exited_cleanly"] else "contract")
    return result


def metadata():
    def git(*args):
        try:
            return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL,
                                           text=True, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            return None
    return {"git_sha": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "python": platform.python_version(), "httpx": importlib.metadata.version("httpx"),
            "os": platform.system(), "architecture": platform.machine(), "sample_sha256": SAMPLE_HASH}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--concurrency", type=int, action="append")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--deadline", type=float, default=600)
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 30 or not 1 <= args.repeats <= 3 or not 0 < args.deadline <= 600:
        parser.error("count=1..30, repeats=1..3, deadline=0..600 (exclusive zero) required")
    args.concurrency = args.concurrency or [1, 5, 10]
    args.scenario = args.scenario or list(SCENARIOS)
    if any(not 1 <= n <= 10 for n in args.concurrency):
        parser.error("concurrency must be 1..10")
    if len(set(args.concurrency)) != len(args.concurrency) or len(set(args.scenario)) != len(args.scenario):
        parser.error("duplicate matrix entries")
    if len(args.concurrency) > 3:
        parser.error("at most three concurrency levels")
    if args.smoke:
        args.scenario, args.concurrency, args.count, args.repeats = ["normal"], [1], 5, 1
    return args


def main(argv=None):
    args = arguments(argv)
    interrupted = False

    def on_signal(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    report = {"schema_version": VERSION, "valid": False, "reason": "initialization", "cells": [],
              "started_at": datetime.now(timezone.utc).isoformat(),
              "measurement_scope": "closed_loop_search_function_with_loopback_fixtures",
              "tail_warning": "30 samples do not establish reliable p99", "remaining_cells": 0}
    deadline = time.monotonic() + args.deadline
    try:
        # Exclusive creation also refuses existing files and symlinks before any socket starts.
        with args.output.open("x", encoding="utf-8") as output:
            def save():
                output.seek(0)
                json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.truncate()
                output.flush()

            save()
            try:
                report["environment"] = metadata()
                matrix = [(scenario, n, repeat) for scenario in args.scenario
                          for n in args.concurrency for repeat in range(1, args.repeats + 1)]
                report["configuration"] = {"scenarios": args.scenario, "concurrency": args.concurrency,
                                           "count": args.count, "repeats": args.repeats,
                                           "deadline_seconds": args.deadline, "warmup_count": 5}
                report["remaining_cells"] = len(matrix)
                report["reason"] = "ok"
                for scenario, n, repeat in matrix:
                    if interrupted or time.monotonic() >= deadline:
                        report["reason"] = "interrupted" if interrupted else "deadline"
                        break
                    cell = run_cell(scenario, n, args.count, deadline, lambda: interrupted)
                    cell["repeat"] = repeat
                    report["cells"].append(cell)
                    report["remaining_cells"] -= 1
                    save()
                    if not cell["valid"]:
                        report["reason"] = cell["reason"]
                        break
                report["valid"] = (report["reason"] == "ok" and report["remaining_cells"] == 0
                                   and bool(report["cells"]) and all(c["valid"] for c in report["cells"]))
                if interrupted:
                    report["valid"], report["reason"] = False, "interrupted"
            except Exception:
                report["reason"] = "runner_failure"
            finally:
                report["finished_at"] = datetime.now(timezone.utc).isoformat()
                save()
    except (OSError, ValueError):
        print("search_lab=FAIL reason=output_unavailable", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"search_lab={'PASS' if report['valid'] else 'FAIL'} cells={len(report['cells'])} reason={report['reason']}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
