# 09 — Controlled six-node rerun

This procedure is prepared for a dedicated **4-prefill / 2-decode** test. It is
not a recorded performance result. Restore access and confirm the target
cluster and its organization before applying anything. MI355 testing in this
workspace normally belongs in `amp-dev`; use an explicitly selected context.
Do not create or replace six GPU workers in a different tenant by assumption.

The full market serving manifests are introduced by [recipe PR #2](https://github.com/amp-pbc/kimi-k3-mi355x/pull/2).
Use that reviewed checkout until it lands on main; the replay Job alone does
not provision the six model workers.

## Deploy and establish the baseline

1. Inspect existing workloads, shared weights and GPU allocation. Save the
   existing serving manifests and router command for rollback. Stop any old
   load generator before a comparison; keep other client traffic off the test.
2. Apply the reviewed `k8s/market/pd-dspark.yaml` on the intended test cluster.
   It uses the fixed v0.2.9 overlay, complete shared-volume tokenizer, Python
   router, and explicit KV-aware weights 1/1. A fresh deployment needs six
   eight-GPU nodes and staged weights. Replacing existing GPU pods entails
   another model load; a policy-only comparison changes the CPU router only.
3. Require four Ready prefill workers and two Ready decode workers, all six
   registered at `/v1/workers`. Confirm the running images, tokenizer-success
   log, block size, KV-event receipt, and [PD handoff gates](04-verification.md).
   Save pod UIDs, restart counts and actual init-container image IDs. The
   replay checks active role counts and engine metrics, but cannot prove
   Kubernetes readiness or absence of transport errors on its own.

## Request fixtures and measurements

`bench/controlled_replay.py` uses the corrected streaming implementation in
`bench/orbench.py`. It sends a fixed recorded request body for every turn and
allows only one request per session at a time. Late turns retain their target
arrival time; dispatch delays are recorded, not hidden. Responses do not change
later request bodies. The built-in long fixture uses a fixed recorded `OK`
assistant response, so it is a synthetic prefix-locality check rather than a
production-agent trace. A real recorded chat workload can use the same JSON
schema. Keep model/generation parameters and fixture seed fixed across policies.

The long fixture asks for only 32 output tokens. It tests routing and prefix
reuse, **not decode saturation or representative production capacity**. After
routing passes, repeat with a recorded workload matching real input/output
lengths before publishing a throughput claim.

Example preparation (local files only):

```bash
python3 bench/controlled_replay.py generate \
  --scenario short --sessions 448 --rate 0.5 --salt arm00001 \
  --out /tmp/kimi-short.replay.json.gz
python3 bench/controlled_replay.py generate \
  --scenario long --sessions 32 --turns 4 --rate 0.5 --salt arm00001 \
  --out /tmp/kimi-long.replay.json.gz
```

The short fixture has 448 independent sessions, one request each. Validate its
actual token counts against the advertised 768-token block size. The long
fixture has 128 requests with approximately 9,000 synthetic input tokens per
session. Use engine-reported usage, not that estimate, in the report.

The `.gz` output is compressed so the default fixtures fit in a Kubernetes
ConfigMap. Keep the entire input ConfigMap under its 1 MiB limit. Larger
production traces should be staged on the shared volume instead.

For each arm, create a new early-prefix salt of the same format to avoid reusing
that arm's prompt prefixes from a previous policy/run. This is **fresh-prefix**
testing, not an empty-engine-cache claim. For a warm measurement, repeat the
same fixture with the same policy after the first pass drains. Record both
fixture hashes and compare input-token distributions. A seed alone never
clears server caches.

## Run each arm inside the cluster

The commands below are a template. Set `KIMI_CONTEXT` to the verified test
context and choose one fixture. Capture the actual deployment after its rollout
finishes, not just the desired file from Git. The manifest saved below is
provenance; the replay does not apply it or assert it matches the arm label.

```bash
KIMI_CONTEXT='<verified-test-context>'
kubectl --context "$KIMI_CONTEXT" -n default get deployments \
  -l app.kubernetes.io/part-of=kimi-k3 -o yaml > /tmp/kimi-deployment.yaml
kubectl --context "$KIMI_CONTEXT" -n default create configmap kimi-k3-controlled-replay \
  --from-file=orbench.py=bench/orbench.py \
  --from-file=controlled_replay.py=bench/controlled_replay.py \
  --from-file=fixture.json.gz=/tmp/kimi-long.replay.json.gz \
  --from-file=deployment.yaml=/tmp/kimi-deployment.yaml \
  --dry-run=client -o yaml | kubectl --context "$KIMI_CONTEXT" apply -f -
```

Edit `k8s/market/controlled-replay.yaml` locally to give each run a unique Job
name and accurate `ARM` / `CACHE_STATE`, then apply it with the same explicit
context. Do not overwrite the input ConfigMap while a prior Job is running.
The Job runs one arm, does not retry failures, and leaves receipts in
`/mnt/shared/kimi-k3/controlled-replay/<pod-uid>/` plus a sibling `.log`.

Each arm records its exact fixture, supplied deployment snapshot, worker
registration/IP/role mapping, per-request CSV, raw before/after engine metrics,
request dispatch delays, latency percentiles and a JSON summary. It requires
idle running/waiting gauges before and after the replay and rejects missing
required metrics, changed registrations, detected counter resets, failed or
undispatched requests. `valid: true` means those checks passed; it is not a
verdict that throughput is good or that the arm label matches the live policy.

An arm can take up to the last scheduled arrival plus the drain allowance and
the before/after idle checks. The Job has a one-hour hard deadline. Keep the
fixture duration inside it. A drain timeout cancels and joins client tasks;
the subsequent idle check verifies server queues. Never proceed to the next
arm while the engines are still serving an earlier one.

## Comparison order and acceptance

First compare `round-robin` versus `kv-aware` with prefill/decode weights **1/1**.
Set the router's explicit `--router-policy` value in a saved manifest and roll
out only `Deployment/infera`; do not restart the six GPU engines merely to
switch policy. Wait for router discovery and KV-event synchronization before
sending the next arm. Keep router replicas, worker pool and generation settings
fixed. Preserve the original policy for restoration after the test.

Use at least three fresh-prefix/warm pairs per policy, alternating which
policy goes first between repetitions. Repeat the workload in both orders to
expose cache-state/order effects. Record seed, salt, image digests, topology,
policy, weights and actual request-token distributions for every arm. Test
**20/2** weights later as a separate tuning comparison, not alongside an image
or tokenizer change.

- Short independent prompts: no permanent first-worker-only selection within
  either role. Exact equal shares are not required.
- Repeated long prefixes: nonzero router blocks/events and engine prefix hits;
  warmed affinity can correctly favor particular workers. Compare within roles.
- PD: successful KV transfers with no memory-registration errors or eager
  DSpark fallbacks; otherwise routing is not the only variable.
- Report completion/failure/undispatched counts, TTFT p50/p90/p99, dispatch
  delay, output throughput, per-worker request and prefix-hit/query deltas,
  and router CPU throttling. Prefix-cache counters measure tokens, not requests.
- Publish the individual repeated results and their variation. No speedup or
  capacity claim is supported until these tests actually run on all six nodes.

The ordinary synthetic `orbench.py` sweep now enforces causal session reuse,
requires complete SSE streams, and aborts after an incomplete drain. Its live
session selection can still change with response timing. Use the fixed replay
for matched-input policy comparisons.
