"""Fixed-request, causal replay with per-worker receipts for a 4P/2D test.

Generate fixtures once, then run them inside the cluster. No cluster mutation,
cache flush, policy change, or automatic retry is performed by this program.
"""
import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import gzip
import json
import math
from pathlib import Path
import random
import re
import time

import httpx
import orbench

GAUGES = ("vllm:num_requests_running", "vllm:num_requests_waiting")
COUNTERS = ("vllm:request_success_total", "vllm:prefix_cache_queries_total",
            "vllm:prefix_cache_hits_total")


def fixture(seed, salt, scenario, sessions, turns, rate, model):
    rng = random.Random(seed)
    arrivals = random.Random(seed ^ 0x51A7) if scenario == "input-heavy" else rng
    requests = []
    histories = {}
    clock = 0.0
    for turn in range(1, turns + 1):
        order = list(range(sessions))
        if scenario == "input-heavy":
            rng.shuffle(order)
        for sid in order:
            clock += arrivals.expovariate(rate)
            if sid not in histories:
                # Salt precedes the long context to isolate cache state across
                # arms. Word count is not a token count; record actual usage.
                target = 9000
                if scenario == "input-heavy":
                    target = int(rng.triangular(8000, 32000, 16000) if rng.random() < .7
                                 else rng.triangular(32000, 64000, 48000))
                context = "" if scenario == "short" else orbench.filler(rng, target)
                instruction = ("Analyze the supplied synthetic dataset. Explain your method, "
                               "state limitations, and give a detailed structured answer."
                               if scenario == "input-heavy" else "Reply with exactly OK.")
                histories[sid] = [
                    {"role": "system", "content":
                     f"Fixture {salt}, session {sid:04d}. {instruction}\n{context}"}]
            elif scenario in ("long", "input-heavy"):
                # Recorded history, intentionally independent of generated
                # output. This is a prefix-locality test, not a real agent trace.
                recorded_reply = ("The dataset contains synthetic identifiers. I will count occurrences, "
                                  "check for duplicates, and avoid inferring meaning from the identifiers."
                                  if scenario == "input-heavy" else "OK")
                histories[sid].append({"role": "assistant", "content": recorded_reply})
            else:
                raise ValueError("short fixtures use one turn per independent session")
            question = (f"Turn {turn}: describe a reproducible frequency-analysis procedure for "
                        "these records, with pseudocode, validation checks and complexity analysis. "
                        "Explain how you would handle duplicates and incremental updates."
                        if scenario == "input-heavy" else f"Acknowledge turn {turn}.")
            histories[sid].append({"role": "user", "content": question})
            output_budget = rng.choice([128, 256, 256, 256, 512]) if scenario == "input-heavy" else 32
            requests.append({"session": sid, "turn": turn, "at_s": clock,
                             "slice": scenario, "body": {
                                 "model": model, "messages": list(histories[sid]),
                                 "max_tokens": output_budget, "temperature": 0}})
    # Keep repeated prefixes adjacent for gzip; at_s still defines dispatch
    # timing, independently of serialization order.
    requests.sort(key=lambda r: (r["session"], r["turn"]))
    return {"schema": 1, "seed": seed, "salt": salt, "scenario": scenario,
            "offered_rate": rate, "requests": requests}


def validate_fixture(data):
    if data.get("schema") != 1 or not data.get("requests"):
        raise ValueError("expected schema 1 with a nonempty request list")
    seen = {}
    for req in data["requests"]:
        at = req["at_s"]
        if not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
            raise ValueError("at_s must be a finite nonnegative number")
        sid = req["session"]
        last_turn, last_at = seen.get(sid, (0, -1))
        if req["turn"] != last_turn + 1 or at < last_at:
            raise ValueError("session turns must be consecutive with monotonic times")
        if not req["body"].get("messages") or not req["body"].get("model"):
            raise ValueError("each request needs a model and messages")
        seen[sid] = (req["turn"], at)


def parse_metrics(raw):
    result = defaultdict(float)
    for line in raw.splitlines():
        match = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{.*\})?\s+([^\s]+)', line)
        if match:
            result[match[1]] += float(match[2])
    return dict(result)


async def get_workers(client, base):
    r = await client.get(base + "/v1/workers", timeout=15)
    r.raise_for_status()
    workers = [w for w in r.json()["workers"] if str(w.get("status", "")).lower() == "active"]
    roles = Counter()
    for w in workers:
        mode = str(w.get("disagg_mode", "")).upper()
        role = "prefill" if "PREFILL" in mode else "decode" if "DECODE" in mode else "other"
        w["role"] = role
        roles[role] += 1
    if roles != {"prefill": 4, "decode": 2}:
        raise RuntimeError(f"expected exactly 4 prefill and 2 decode workers, got {dict(roles)}")
    if len({w['worker_id'] for w in workers}) != 6 or len({w['url'] for w in workers}) != 6:
        raise RuntimeError("worker IDs and URLs must be unique")
    return sorted(workers, key=lambda w: w["worker_id"])


async def snapshot(client, workers, dest, label):
    async def read(index, worker):
        r = await client.get(worker["url"].rstrip("/") + "/metrics", timeout=15)
        r.raise_for_status()
        (dest / f"{label}-worker-{index}.metrics").write_text(r.text)
        return worker["worker_id"], parse_metrics(r.text)
    return dict(await asyncio.gather(*(read(i, w) for i, w in enumerate(workers))))


async def wait_idle(client, workers, dest, label, timeout):
    deadline = time.monotonic() + timeout
    consecutive = 0
    while True:
        values = await snapshot(client, workers, dest, label)
        for wid, metrics in values.items():
            if any(g not in metrics or not math.isfinite(metrics[g]) for g in GAUGES):
                raise RuntimeError(f"{wid}: missing/invalid engine running/waiting gauges")
        idle = all(m[g] == 0 for m in values.values() for g in GAUGES)
        consecutive = consecutive + 1 if idle else 0
        if consecutive >= 2:
            return values
        if time.monotonic() >= deadline:
            raise RuntimeError("engine queues did not become idle; no further arm may start")
        await asyncio.sleep(2)


def counter_deltas(before, after, workers):
    result = []
    for worker in workers:
        wid = worker["worker_id"]
        a, b = before[wid], after[wid]
        # Process start plus negative deltas detects common reset cases. Raw
        # receipts remain necessary to review the deployed fork's semantics.
        if a.get("process_start_time_seconds") != b.get("process_start_time_seconds"):
            raise RuntimeError(f"{wid}: process restarted during the arm")
        delta = {}
        for counter in COUNTERS:
            if counter not in a or counter not in b:
                raise RuntimeError(f"{wid}: missing {counter}; inspect metric HELP in raw receipts")
            diff = b[counter] - a[counter]
            if not math.isfinite(diff) or diff < 0:
                raise RuntimeError(f"{wid}: counter reset or invalid value for {counter}")
            delta[counter] = diff
        queries = delta[COUNTERS[1]]
        result.append({"worker_id": wid, "role": worker["role"], "url": worker["url"],
                       "deltas": delta, "prefix_hit_ratio": delta[COUNTERS[2]] / queries if queries else None})
    return result


async def replay(client, data, bench, drain):
    grouped = defaultdict(list)
    for req in data["requests"]:
        grouped[req["session"]].append(req)
    start = time.monotonic()
    delays = []

    async def session_stream(turns):
        for req in turns:
            await asyncio.sleep(max(0, start + req["at_s"] - time.monotonic()))
            delays.append({"session": req["session"], "turn": req["turn"],
                           "delay_s": max(0, time.monotonic() - start - req["at_s"])})
            ok = await orbench.one_request(client, bench, 1.0, None, None, request=req)
            if not ok:
                break  # Do not fabricate a successor turn after a failed one.

    tasks = [asyncio.create_task(session_stream(turns)) for turns in grouped.values()]
    deadline = max(r["at_s"] for r in data["requests"]) + drain
    try:
        _, pending = await asyncio.wait(tasks, timeout=deadline)
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            raise outcome
    return {"elapsed_s": time.monotonic() - start, "drain_timed_out": bool(pending),
            "dispatch_delays": delays}


async def run(args):
    raw = args.fixture.read_bytes()
    data = json.loads(gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw)
    validate_fixture(data)
    args.out.mkdir(parents=True, exist_ok=False)
    fixture_name = "fixture.json.gz" if raw.startswith(b'\x1f\x8b') else "fixture.json"
    (args.out / fixture_name).write_bytes(raw)
    manifest = args.manifest.read_bytes()
    (args.out / "deployment.yaml").write_bytes(manifest)
    receipt = {"fixture_sha256": hashlib.sha256(raw).hexdigest(),
               "deployment_sha256": hashlib.sha256(manifest).hexdigest(),
               "arm": args.arm, "cache_state": args.cache_state,
               "started_utc": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               "planned": len(data["requests"]), "valid": False}
    bench = orbench.Bench(args.out / "requests.csv")
    orbench.BASE = args.base.rstrip("/") + "/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=900, limits=httpx.Limits(max_connections=2000)) as client:
            workers = await get_workers(client, args.base.rstrip("/"))
            (args.out / "workers.json").write_text(json.dumps(workers, indent=2))
            before = await wait_idle(client, workers, args.out, "before", args.idle_timeout)
            receipt.update(await replay(client, data, bench, args.drain))
            after = await wait_idle(client, workers, args.out, "after", args.idle_timeout)
            final_workers = await get_workers(client, args.base.rstrip("/"))
            if workers != final_workers:
                raise RuntimeError("worker registration changed during the arm")
            receipt["workers"] = counter_deltas(before, after, workers)
            receipt["valid"] = not receipt["drain_timed_out"]
    except Exception as exc:
        receipt["error"] = str(exc)
    finally:
        bench.f.close()
        receipt["dispatched"] = len(bench.rows)
        receipt["completed"] = sum(r["completed"] for r in bench.rows)
        receipt["failed"] = len(bench.rows) - receipt["completed"]
        receipt["not_dispatched"] = receipt["planned"] - receipt["dispatched"]
        receipt["valid"] = receipt["valid"] and receipt["failed"] == receipt["not_dispatched"] == 0
        ttft = [r["ttft_s"] for r in bench.rows if r["completed"] and r["ttft_s"] is not None]
        receipt["ttft_s"] = {str(p): orbench.pct(ttft, p) for p in (.5, .9, .99)}
        receipt["completion_tokens"] = sum(r["completion_tokens"] or 0 for r in bench.rows if r["completed"])
        receipt["output_tokens_per_s"] = receipt["completion_tokens"] / max(receipt.get("elapsed_s", 0), 1e-6)
        receipt["prompt_tokens"] = sum(r["prompt_tokens"] or 0 for r in bench.rows if r["completed"])
        receipt["input_tokens_per_s"] = receipt["prompt_tokens"] / max(receipt.get("elapsed_s", 0), 1e-6)
        receipt["usage_complete"] = all(r["prompt_tokens"] is not None and r["completion_tokens"] is not None
                                        for r in bench.rows if r["completed"])
        receipt["offered_rate"] = data.get("offered_rate")
        receipt["offered_window_s"] = max(r["at_s"] for r in data["requests"])
        receipt["peak_client_inflight"] = bench.peak_inflight
        receipt["dispatch_delay_p99_s"] = orbench.pct(
            [r["delay_s"] for r in receipt.get("dispatch_delays", [])], .99)
        (args.out / "summary.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({k: v for k, v in receipt.items() if k != "dispatch_delays"}, indent=2), flush=True)
    return 0 if receipt["valid"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--out", type=Path, required=True)
    generate.add_argument("--salt", required=True, help="distinct early prefix per cold arm; repeat for a warm run")
    generate.add_argument("--scenario", choices=["short", "long", "input-heavy"], required=True)
    generate.add_argument("--sessions", type=int, default=32)
    generate.add_argument("--turns", type=int, default=4)
    generate.add_argument("--rate", type=float, default=.5)
    generate.add_argument("--seed", type=int, default=20260910)
    generate.add_argument("--model", default="moonshotai/Kimi-K3")
    execute = sub.add_parser("run")
    execute.add_argument("--fixture", type=Path, required=True)
    execute.add_argument("--manifest", type=Path, required=True, help="saved deployed manifest without credentials")
    execute.add_argument("--out", type=Path, required=True)
    execute.add_argument("--base", default="http://infera:8000")
    execute.add_argument("--arm", required=True, help="record the actual policy/weights and repeat number")
    execute.add_argument("--cache-state", choices=["fresh-prefix", "warm"], required=True)
    execute.add_argument("--drain", type=float, default=180)
    execute.add_argument("--idle-timeout", type=float, default=180)
    args = parser.parse_args()
    if args.command == "generate":
        if args.sessions <= 0 or args.turns <= 0 or not math.isfinite(args.rate) or args.rate <= 0:
            parser.error("sessions, turns and rate must be positive and finite")
        if args.scenario == "short": args.turns = 1
        data = fixture(args.seed, args.salt, args.scenario, args.sessions, args.turns, args.rate, args.model)
        validate_fixture(data)
        payload = json.dumps(data).encode()
        if args.out.suffix == '.gz':
            payload = gzip.compress(payload, mtime=0)
        with args.out.open("xb") as f: f.write(payload)
        print(f"wrote {len(data['requests'])} requests to {args.out}")
        return 0
    if args.drain <= 0 or args.idle_timeout <= 0:
        parser.error("drain and idle-timeout must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
