# 05 — Benchmarking a Kimi-K3 endpoint

This is the methodology behind [`bench/orbench.py`](../bench/orbench.py). The
goal is to measure your Kimi-K3 endpoint the way a real router (e.g. OpenRouter)
would measure and route on it, and to find the request rate it can carry. You
generate your own numbers — none are reproduced here.

## Score on the router view, not a pass/fail goodput bar

A router does not publish a single "capacity" number. It observes routed traffic
and displays **medians**, then routes on them. Reproduce exactly that view:

- **Median per-request displayed tok/s** — output tokens ÷ *total* time,
  **including TTFT**. This is the headline number a provider page shows. It
  already penalises slow prefill without pretending a 100k-token prompt can emit
  a first token in two seconds.
- **TTFT p50 / p90 / p99** — the latency stat. Report the full distribution;
  tails matter to interactive users.
- **Completion rate** — the fraction of offered requests that were served (an
  uptime / availability proxy).
- **429 count** — how many requests the endpoint refused.

Do **not** reduce this to a single pass/fail goodput threshold (e.g. "every
request must exceed N tok/s"). A fixed bar condemns short-output tool calls that
are being served correctly in about a second, and it scores physics as failure:
a legitimately long prompt has a legitimately high TTFT. A router has no such
gate. Displayed tok/s (output ÷ total time) is the one throughput view that is
honest across prompt shapes, so it is the one to compare against a model's
provider page. Tokens/sec aggregated across the run is reported too, but it is
not the headline: it rewards long generations and punishes fast short ones.

## Open-loop, not closed-loop

- **Closed-loop** (a fixed pool of workers, each waiting for its response before
  sending the next) self-throttles. It can never send faster than the endpoint
  responds, so it can never overload the endpoint and can never find the rate at
  which it starts to fail. It systematically **overstates** sustainable
  capacity.
- **Open-loop** offers a request **rate** with **Poisson arrivals**, regardless
  of how many requests are already in flight — which is what a router actually
  does. If the endpoint slows down, the backlog grows and the failure shows up
  in the metrics instead of being hidden by the load generator throttling
  itself.

orbench drives Poisson arrivals at each `--rate` and sweeps a list of rates.

## Do not retry 429s

In open loop a 429 is a request the user did not get served. That is precisely
how a router sees it, so it must count against the endpoint. Retrying 429s
launders refused requests into eventual successes and hides the true carrying
capacity. orbench records the 429 and moves on.

## Traffic mix

Fixed prompt and completion sizes make runs non-comparable and unrepresentative.
orbench samples a heavy-tailed mix:

- **chat** (~60%) — short prompts (200–2k tokens), short-to-medium outputs.
- **agentic** (~30%) — larger prompts (4k–32k), run as **multi-turn sessions**
  whose prefix grows each turn, so prefix reuse is organic and partial rather
  than an artificial 0% or 100%.
- **longctx** (~10%) — very large prompts (32k–200k tokens).

Token lengths are drawn with triangular sampling so the tail is heavier than
uniform without being absurd. The per-slice breakdown at the end of a run shows
which slice consumes the input-token budget and how each slice's latency and
throughput behave.

## Reading the "knee"

Sweep the offered rate and read off the **operating point**: the highest offered
rate at which, simultaneously,

- displayed tok/s stays **competitive** with the model's provider page,
- TTFT stays **flat** (p50/p90/p99 not climbing as you add load),
- completion rate is **~100%**, and
- 429s are **~0**.

Below the knee, adding rate mostly adds throughput while latency stays flat.
Above the knee, TTFT tails blow up, 429s appear, and/or completion rate drops —
that rate is past what the endpoint can carry. Advertise the rate *below* the
knee, not the one where it breaks.

## Prefill/decode (PD) note

A prefill/decode-disaggregated serving setup is **prefill-bound**: the knee moves
with the **prefill:decode ratio** of your traffic. Prompt-heavy mixes (lots of
long-context or large agentic prompts) hit the prefill limit sooner and push the
knee to a lower request rate; decode-heavy mixes (long generations off short
prompts) push it higher. Because the knee depends on the mix, benchmark with a
traffic mix that resembles your real workload, and re-measure if that mix
shifts. The orbench per-slice breakdown helps you see whether prefill (input
tokens) or decode (output tokens) is the binding constraint at your operating
point.
