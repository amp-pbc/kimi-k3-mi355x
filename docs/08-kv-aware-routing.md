# 08 — KV-aware routing: configuration and validation

For six nodes at a 2:1 ratio, validate **four prefill workers and two decode
workers** separately. Worker readiness, KV transfer, prefix reuse, and routing
balance are different checks. A healthy `/health` response proves none of the
last three. This guide records the September 10, 2026 source/image audit; a
six-node performance rerun is still required before claiming an improvement.

## Required configuration

The PD manifests explicitly use the Python `infera.server` backend with:

```text
--router-backend python
--router-policy kv-aware
--router-tokenizer-path /shared/kimi-k3/weights/Kimi-K3
--kv-overlap-weight 1.0
--kv-prefill-overlap-weight 1.0
--kv-decode-overlap-weight 1.0
--kv-event-transport zmq
```

The tokenizer path above is for market/shared-volume deployments. The static
operator recipe mounts the same model files at `/models/Kimi-K3` instead.
Always use the path **inside the router container**, with the complete tokenizer
from the same model revision as the workers: configuration, chat template,
`tokenization_kimi.py`, `tiktoken.model`, and their dependencies. Preserve the
upstream model license with these files (see [notices](../THIRD_PARTY_NOTICES.md)).

In this overlay, resolving the Hub ID `moonshotai/Kimi-K3` downloads only
`tokenizer*` and `chat_template*`. Those patterns omit the custom Python and
`tiktoken.model`. A valid Hub ID is therefore insufficient. The market PD
recipe already gained the shared-volume path in September 10's tokenizer fix;
that fix alone does not replace an older overlay image.

The aggregated recipes deliberately select `round-robin`; their overlap
weight has no effect. Before opting into `kv-aware`, stage the complete
tokenizer on storage accessible to their CPU router and change its path.
Do not mount GPU-node NVMe on a CPU node that lacks those files.

## Image fix and tuning are separate

Pin the router **and workers** to the inspected overlay:

```text
inferaimage/infera-overlay:v0.2.9@sha256:90e822bc207e337b67cb387807c6fb8c4a038d9a88a0d10cacb458f853a68be3
```

The prior PD pin, `sha256:2efc5f96d18754ca425ed6cc19de5a12503b0ef75a16eae0096a16075a696708`,
contains Python policy code that records no recent load when the hasher returns
zero blocks. Symmetric idle workers then tie and the first worker keeps winning.
The v0.2.9 image charges one synthetic load block for an unknown/zero-block
request, while retaining zero missed-block cost for an actual full cache hit.
This addresses short-input balancing; it cannot restore missing tokenizer data
or KV events. PR #99's active-block **metric** correction was a separate fix.

A reduced test using `_record_dispatch` extracted from both image payloads
selected `[448, 0, 0, 0]` on the old code and `[112, 112, 112, 112]` on v0.2.9
for 448 sequential zero-block requests across four symmetric workers. This
isolates the policy change; it is not an end-to-end GPU benchmark.

The Python policy minimizes `weight * missed_blocks + load`, where load includes
active blocks and decaying recent dispatch cost. Higher weights favor cache
locality. The shipped global default is **1.0**; unset role weights inherit it.
The recipe sets all three explicitly to preserve that baseline. AMD's argument
help suggests **20.0 for prefill and 2.0 for decode** as tuning candidates, not
as defaults or a validated optimum for this workload. Compare them only after
routing validation. Role values override the global value: changing just
`--kv-overlap-weight` to `0.01` leaves explicit role weights unchanged.

These fixes apply to Python `infera.server`. The Rust router has a separate
implementation; switching backends is a separate experiment, not a prerequisite.

## Validation before a throughput comparison

1. **Capture the actual deployment.** Record router/worker image digests, full
   commands, model revisions, router count, and discovered role/IP mapping.
   Require all four prefill and both decode workers Ready and registered before
   measuring six-node capacity. Confirm the router loaded its tokenizer
   successfully and KV-event subscriptions receive data from the expected
   workers. Persistent subscriber errors or snapshot 404s need investigation;
   pod readiness alone does not clear them.
2. **Test short requests for balancing.** AMD reported a minimum routing block
   of **768 tokens** for this K3 configuration. Confirm the block size advertised
   by the running workers; count tokens after the actual chat template, not
   characters or words. Below one block, `request_blocks=0` is expected and
   provides no prefix-affinity signal. On the fixed image, sequential independent
   short prompts should not be permanently pinned to the first symmetric
   worker of each role. Compare with `round-robin` using the same workload.
3. **Test long requests for locality.** Use multiple independent sessions with
   several thousand tokens of stable common prefix within each session. Give
   each session a distinct prefix near its beginning. Wait for each response
   before sending that session's next turn. Confirm nonzero request blocks,
   published KV events, and engine-side prefix hits on repeats. K3's hybrid
   cache alignment can limit reuse; one hashable block does not guarantee an
   engine cache hit. A warmed session staying on one worker can be correct;
   exact rotation or equal shares is not a KV-aware acceptance criterion.
4. **Measure each worker directly.** Snapshot each engine's `/metrics` before
   and after each arm, retaining worker IP and role. Do not scrape a
   load-balanced Service and call it per-worker data. Use
   `vllm:request_success_total` (with finish reasons) or request logs for finished
   requests. Read the image's metric `HELP` text: `prefix_cache_queries_total`
   and `prefix_cache_hits_total` describe cache query/hit volume, not HTTP
   request counts; current vLLM measures prefix queries/hits in tokens.
   Report hit/query deltas and request distribution separately, within each
   role. Reject intervals with worker restarts/counter resets.
5. **Verify PD separately.** Use [04-verification](04-verification.md) to check
   successful KV transfers, decode-side external cache hits, and absence of
   memory-registration errors and eager-draft fallbacks. Cache-aware selection
   cannot fix broken GPU-direct RDMA, local re-prefill, or DSpark fallback.
6. **Repeat under controlled conditions.** Use the same recorded requests,
   arrival schedule and generation limits; label cold and warm cache runs.
   Start each arm only after the previous requests drain and engine running/
   waiting gauges return to baseline. Repeat and alternate policy order.
   Report latency percentiles, completed/failed/unfinished requests, router CPU
   throttling, and per-role load alongside throughput. Keep router replicas
   fixed: their load accounting is local to each router process.

## Limits of the bundled load test

`bench/orbench.py` and its market/Getting Started copies offer a synthetic mix,
not a controlled cache-affinity replay. In the audited versions, a session can
be reused while its previous turn is still in flight; the drain timeout does
not cancel/join remaining requests before the next rate; and an HTTP 200 with
a first SSE line can be counted as success even if the stream later fails.
The fixed random seed also reuses prompts across runs while engine caches
persist. These can change measured traffic and cache warmth between runs.

Treat existing sweep results as exploratory. For a publishable comparison,
use a replay that enforces causal sessions and complete-stream accounting,
run one rate per process, and wait for engine queues to empty between arms.
Record unfinished requests as failures. A routing fix is not evidence that
this benchmark's repeatability issues have been resolved.

## Sources

- Infera v0.2.9 [argument defaults](https://github.com/AMD-AGI/Infera/blob/731adeede9009b425afe17e403ec9e47500d4546/infera/server/args.py),
  [Python policy](https://github.com/AMD-AGI/Infera/blob/731adeede9009b425afe17e403ec9e47500d4546/infera/router/policy/kv_event_aware.py),
  and [tokenizer resolver](https://github.com/AMD-AGI/Infera/blob/731adeede9009b425afe17e403ec9e47500d4546/infera/common/tokenizer.py).
  The image audit compared actual Python payloads, rather than inferring the
  old image's behavior from a tag name.
- [vLLM metrics](https://docs.vllm.ai/en/latest/design/metrics/), including the
  prefix-cache metric definitions. Check the deployed fork's `HELP` text too.
- AMD's support guidance supplied for this audit: 768-token minimum in the
  tested K3 setup, v0.2.9 short-input fix, and the required tokenizer-path fix.
