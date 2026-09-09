"""OpenRouter-representative load test: OPEN-LOOP, mixed traffic, scored on
what a router actually displays and routes on (median throughput + latency).

WHY OPEN-LOOP
A closed-loop test uses a fixed number of workers that each wait for a response
before sending again. That self-throttles: it can never overload the system and
so can never find the rate at which the endpoint starts failing. A router does
not send an endpoint a concurrency, it sends a RATE. This harness drives Poisson
arrivals at a target rate regardless of what is already in flight, and sweeps the
rate to find the knee.

WHAT THIS MEASURES AND WHY
 - Token lengths are SAMPLED from a heavy-tailed mix (short chat / agentic /
   long-context), not fixed. Fixed sizes make runs non-comparable (matched
   prompts but wildly different completion lengths change the answer).
 - Agentic traffic runs MULTI-TURN SESSIONS whose prefix grows each turn, so
   prefix reuse is organic and partial rather than an artificial 0% or 100%.
 - 429s are NOT retried. In open loop a 429 is a request the user did not get
   served, which is exactly how a router would see it.
 - Scored on the ROUTER VIEW: median per-request throughput (output tokens /
   total time incl TTFT — the displayed number), TTFT percentiles (the latency
   stat), completion rate (uptime proxy), and 429 count. There is NO pass/fail
   goodput gate: a fixed threshold condemns short-output tool traffic that is
   being served in ~1s, and a router has no such gate either — it displays
   medians and routes on them. Tokens/sec is reported but is deliberately not
   the headline, because it rewards long generations and punishes fast short
   ones.

OUTPUT: one row per request to CSV, a per-rate summary, and a rate-vs-displayed-
stats curve to read the advertisable rate off.

CONFIG (all via env, with generic defaults):
  ORBENCH_BASE   chat-completions URL (default a local placeholder)
  API_KEY        bearer token; empty = no auth (many engines require none)
  ORBENCH_MODEL  model id (default moonshotai/kimi-k3)
"""
import argparse, asyncio, csv, json, os, random, statistics as st, time
import httpx

# Overridable so the bench survives endpoint moves and can aim at an in-cluster
# target (e.g. as a Job). API_KEY falls back to empty: engines that do no auth
# accept an empty bearer. Note the model id can differ by target — a gateway
# may serve a public id while the engine is started with a --served-model-name.
BASE = os.environ.get("ORBENCH_BASE", "http://localhost:8000/v1/chat/completions")
KEY  = os.environ.get("API_KEY", "")
MODEL = os.environ.get("ORBENCH_MODEL", "moonshotai/kimi-k3")

# TTFT and per-request throughput are the two things a router displays, so those
# are what we report. SLA is scored the way a router computes throughput: output
# tokens divided by TOTAL wall time INCLUDING TTFT. That single view is the right
# one, because it already penalises slow prefill without pretending a 100k-token
# prompt can deliver a first token in 2s. An absolute TTFT gate would score
# physics as failure (long prompts legitimately have high TTFT). TTFT is still
# reported in full, it just does not gate the score.

WORDS = [f"w{i:05d}" for i in range(80000)]
TOK_PER_WORD = 3.0          # measured: synthetic 6-char tokens cost ~3 tokens each

# Traffic mix. Shares sum to 1. Ranges are lognormal-ish via triangular sampling
# so the tail is heavier than uniform without being absurd.
SLICES = [
    # name        share  prompt_lo  prompt_mode  prompt_hi   out_lo out_hi  session?
    ("chat",      0.60,      200,       800,        2000,      100,   800,  False),
    ("agentic",   0.30,     4000,      8000,       32000,      200,  2000,  True),
    ("longctx",   0.10,    32000,     60000,      200000,      200,  2000,  False),
]

def pick_slice(rng):
    x = rng.random(); acc = 0.0
    for s in SLICES:
        acc += s[1]
        if x <= acc: return s
    return SLICES[-1]

def words_for(tokens): return max(4, int(tokens / TOK_PER_WORD))

def filler(rng, tokens):
    return " ".join(rng.choice(WORDS) for _ in range(words_for(tokens)))


class Sessions:
    """Multi-turn conversations whose prefix GROWS, producing organic reuse.

    Turn 1 sends a system context plus a question. Turn 2 sends the same context,
    the assistant's reply, and a new question, so the cached prefix lengthens each
    turn exactly as a coding agent's does. Sessions retire after a few turns so
    the working set keeps churning instead of going permanently hot.
    """
    def __init__(self, rng):
        self.rng = rng
        self.pool = {}
        self.next_id = 0

    def take(self, prompt_tokens):
        live = [k for k, v in self.pool.items() if len(v["msgs"]) < 2 * v["max_turns"]]
        if live and self.rng.random() < 0.75:
            k = self.rng.choice(live)
            s = self.pool[k]
            s["msgs"].append({"role": "user",
                              "content": f"Turn {len(s['msgs'])//2+1}: continue, one short paragraph."})
            return k, list(s["msgs"])
        k = self.next_id; self.next_id += 1
        base = [{"role": "system", "content": "PROJECT CONTEXT:\n" + filler(self.rng, prompt_tokens)},
                {"role": "user", "content": "Turn 1: summarise the context in one short paragraph."}]
        self.pool[k] = {"msgs": list(base), "max_turns": self.rng.randint(2, 6)}
        if len(self.pool) > 60:
            for old in list(self.pool)[:20]: self.pool.pop(old, None)
        return k, list(base)

    def record_reply(self, k, text):
        if k in self.pool and text:
            self.pool[k]["msgs"].append({"role": "assistant", "content": text[:2000]})


class Bench:
    def __init__(self, csv_path):
        self.rows = []
        self.inflight = 0
        self.peak_inflight = 0
        self.f = open(csv_path, "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(["ts","rate","slice","session","turn","status","ttft_s","total_s",
                         "prompt_tokens","completion_tokens","tok_per_s","completed","err"])
        self.f.flush()

    def add(self, **kw):
        if self.f.closed:
            # a request that outlived the last drain finishes after main()
            # closed the CSV: nothing to record, never a traceback
            return
        self.rows.append(kw)
        self.w.writerow([f"{kw['ts']:.3f}", kw["rate"], kw["slice"], kw["session"], kw["turn"],
                         kw["status"], "" if kw["ttft_s"] is None else f"{kw['ttft_s']:.3f}",
                         f"{kw['total_s']:.3f}", kw["prompt_tokens"] or "", kw["completion_tokens"] or "",
                         "" if kw["tok_per_s"] is None else f"{kw['tok_per_s']:.2f}",
                         int(kw["completed"]), kw["err"] or ""])
        self.f.flush()


async def one_request(client, bench, rate, rng, sessions):
    name, _, plo, pmode, phi, olo, ohi, use_session = pick_slice(rng)
    ptok = int(rng.triangular(plo, phi, pmode))
    otok = rng.randint(olo, ohi)
    sess, turn = -1, 0
    if use_session:
        sess, msgs = sessions.take(ptok)
        turn = len(msgs) // 2
    else:
        msgs = [{"role": "user", "content": filler(rng, ptok)}]
    body = {"model": MODEL, "messages": msgs, "max_tokens": otok,
            "stream": True, "stream_options": {"include_usage": True}}

    bench.inflight += 1
    bench.peak_inflight = max(bench.peak_inflight, bench.inflight)
    t0 = time.time(); first = None; usage = None; err = None; code = 0; text = []
    headers = {"Authorization": f"Bearer {KEY}"} if KEY else {}
    try:
        async with client.stream("POST", BASE, json=body, headers=headers) as r:
            code = r.status_code
            if code != 200:
                err = f"http{code}"; await r.aread()
            else:
                async for line in r.aiter_lines():
                    if not line.startswith("data:"): continue
                    p = line[5:].strip()
                    if p == "[DONE]": break
                    if first is None: first = time.time() - t0
                    try:
                        d = json.loads(p)
                        if d.get("usage"): usage = d["usage"]
                        ch = d.get("choices") or []
                        if ch and ch[0].get("delta", {}).get("content"):
                            text.append(ch[0]["delta"]["content"])
                    except Exception: pass
    except Exception as e:
        err = type(e).__name__
    finally:
        bench.inflight -= 1

    total = time.time() - t0
    ct = (usage or {}).get("completion_tokens")
    # Router's definition: output tokens / TOTAL time, TTFT included.
    tps = (ct / max(total, 1e-6)) if ct else None
    ok = bool(code == 200 and first is not None)   # completed (no SLA gate)
    if use_session and code == 200:
        sessions.record_reply(sess, "".join(text))
    bench.add(ts=time.time(), rate=rate, slice=name, session=sess, turn=turn, status=code,
              ttft_s=first, total_s=total, prompt_tokens=(usage or {}).get("prompt_tokens"),
              completion_tokens=ct, tok_per_s=tps, completed=ok, err=err)


async def run_rate(client, bench, rate, secs, rng, sessions, drain_s, arrival=None):
    """Poisson arrivals at `rate` req/s for `secs`, then drain."""
    tasks = []
    t0 = time.time(); nxt_log = t0 + 30
    while time.time() - t0 < secs:
        await asyncio.sleep(rng.expovariate(arrival if arrival is not None else rate))
        tasks.append(asyncio.create_task(one_request(client, bench, rate, rng, sessions)))
        tasks = [t for t in tasks if not t.done()]
        if time.time() >= nxt_log:
            # Single pass over the rows for the progress line (avoids a quadratic
            # multi-scan over a long run).
            n = g = f429 = 0
            tp = []
            for r in bench.rows:
                if r["rate"] != rate:
                    continue
                n += 1
                if r["completed"]:
                    g += 1
                if r["status"] == 429:
                    f429 += 1
                if r["tok_per_s"]:
                    tp.append(r["tok_per_s"])
            med = st.median(tp) if tp else 0.0
            print(f"    rate={rate:<4.1f} +{(time.time()-t0)/60:4.1f}m  offered={n:5d} "
                  f"completed={100*g/max(n,1):5.1f}%  med tok/s={med:5.1f}  429={f429:4d}  inflight={bench.inflight:4d}",
                  flush=True)
            nxt_log += 30
    if tasks:
        await asyncio.wait(tasks, timeout=drain_s)
    return t0


def pct(xs, p):
    if not xs: return 0.0
    xs = sorted(xs); return xs[min(int(len(xs) * p), len(xs) - 1)]


def summarise(bench, rate, t0):
    g = [r for r in bench.rows if r["rate"] == rate]
    if not g: return None
    done = [r for r in g if r["status"] == 200 and r["ttft_s"] is not None]
    r429 = [r for r in g if r["status"] == 429]
    othererr = [r for r in g if r["status"] not in (200, 429) or (r["status"] == 200 and r["ttft_s"] is None)]
    wall = max(r["ts"] for r in g) - t0
    outtok = sum((r["completion_tokens"] or 0) for r in done)
    intok = sum((r["prompt_tokens"] or 0) for r in done)
    tt = [r["ttft_s"] for r in done]
    return dict(rate=rate, offered=len(g), offered_rps=len(g)/wall, completed=len(done),
                completed_pct=100*len(done)/len(g), r429=len(r429), othererr=len(othererr),
                out_tps=outtok/wall, in_tps=intok/wall,
                ttft_p50=pct(tt,.50), ttft_p90=pct(tt,.90), ttft_p99=pct(tt,.99),
                med_tok_s=st.median([r["tok_per_s"] for r in done if r["tok_per_s"]]) if done else 0)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rates", default="1,2,3,4,6,8")
    ap.add_argument("--secs", type=int, default=240)
    ap.add_argument("--warm", type=int, default=120)
    ap.add_argument("--drain", type=int, default=180)
    ap.add_argument("--csv", default="./orbench.csv")
    a = ap.parse_args()
    rates = [float(x) for x in a.rates.split(",")]
    rng = random.Random(20260804)
    sessions = Sessions(rng)
    bench = Bench(a.csv)

    print(f"OPEN-LOOP Poisson arrivals · rates {rates} req/s · {a.secs}s each · warm {a.warm}s")
    print("scoring: router view — median tok/s per request (output/total time,"
          " TTFT included), TTFT percentiles, completion rate, 429s")
    print("mix: 60% chat 200-2k, 30% agentic 4k-32k multi-turn (growing prefix), 10% longctx 32k-200k")
    print(f"CSV -> {a.csv}\n", flush=True)

    limits = httpx.Limits(max_connections=2000, max_keepalive_connections=400)
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0), limits=limits) as client:
        print(f"warm-up {a.warm}s at {rates[0]} req/s (discarded)", flush=True)
        # tag -1.0 so the rows are discardable, but ARRIVE at a real rate.
        # Passing -1.0 as the rate would make expovariate return negatives, so
        # sleep() returns instantly and the warm-up floods the endpoint, leaving
        # every measured phase to start from a saturated backlog.
        await run_rate(client, bench, -1.0, a.warm, rng, sessions, a.drain,
                       arrival=rates[0])
        bench.rows = [r for r in bench.rows if r["rate"] != -1.0]

        results = []
        for rate in rates:
            print(f"\n--- offering {rate} req/s for {a.secs}s ---", flush=True)
            t0 = await run_rate(client, bench, rate, a.secs, rng, sessions, a.drain)
            s = summarise(bench, rate, t0)
            results.append(s)
            print(f"    => displayed tok/s {s['med_tok_s']:.1f}  completed {s['completed_pct']:.1f}%  "
                  f"429 {s['r429']}  TTFT p50 {s['ttft_p50']:.2f}s p99 {s['ttft_p99']:.2f}s", flush=True)

    print(f"\n{'='*94}\nRATE vs ROUTER-DISPLAYED STATS  (peak in-flight: {bench.peak_inflight})\n{'='*94}")
    print(f"  {'offered':>8s} {'actual':>7s} {'tok/s/req':>9s} {'complete':>9s} {'429':>6s} {'err':>5s} "
          f"{'out tok/s':>10s} {'in tok/s':>9s} {'TTFTp50':>8s} {'TTFTp90':>8s} {'TTFTp99':>8s}")
    for s in results:
        print(f"  {s['rate']:8.1f} {s['offered_rps']:7.2f} {s['med_tok_s']:9.1f} "
              f"{s['completed_pct']:8.1f}% {s['r429']:6d} {s['othererr']:5d} {s['out_tps']:10.1f} {s['in_tps']:9.0f} "
              f"{s['ttft_p50']:7.2f}s {s['ttft_p90']:7.2f}s {s['ttft_p99']:7.2f}s")
    print(f"\n{'='*94}\nPER-SLICE: who consumes the capacity\n{'='*94}")
    print(f"  {'slice':9s} {'reqs':>6s} {'%reqs':>6s} {'input tokens':>14s} {'%input':>7s} {'tok/s/req':>9s} {'TTFTp50':>8s}")
    tot_in = sum((r["prompt_tokens"] or 0) for r in bench.rows if r["status"] == 200)
    tot_n  = len([r for r in bench.rows if r["rate"] > 0])
    for nm, *_ in SLICES:
        g = [r for r in bench.rows if r["slice"] == nm and r["rate"] > 0]
        if not g: continue
        d = [r for r in g if r["status"] == 200 and r["ttft_s"] is not None]
        ti = sum((r["prompt_tokens"] or 0) for r in d)
        mt = st.median([r["tok_per_s"] for r in d if r["tok_per_s"]]) if d else 0.0
        print(f"  {nm:9s} {len(g):6d} {100*len(g)/max(tot_n,1):5.1f}% {ti:14,d} "
              f"{100*ti/max(tot_in,1):6.1f}% {mt:9.1f} {pct([r['ttft_s'] for r in d],.50):7.2f}s")
    print("\n  Operating point: highest rate where displayed tok/s stays competitive")
    print("  vs the model's provider page, TTFT is flat, completion ~100%, 429 ~ 0.")
    print("  (A router displays MEDIANS measured on routed traffic and routes on")
    print("   them; there is no pass/fail bar — do not introduce one here.)")
    bench.f.close()

asyncio.run(main())
