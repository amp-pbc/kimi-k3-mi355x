# Kimi-K3 on AMD MI355X

Reference recipes for serving **Kimi-K3** (2.8T MoE, MXFP4) on **AMD MI355X**
GPUs (8× per node) under Kubernetes — from a simple aggregated baseline up to
**prefill/decode disaggregation (PD) with DSpark speculative decoding** over a
RoCE fabric.

This repo captures a *working* configuration and, more importantly, the
host-level and fabric fixes that PD actually depends on — the parts that aren't
in any vendor quickstart and cost the most time to discover.

> **Status / disclaimer.** Community reference published by AMP PBC, provided
> as-is with no warranty. It reflects a configuration validated on one MI355X
> cluster; kernel/driver/image versions and node topology will differ on yours,
> so treat every version string as an example. The container images referenced
> are **AMD / Infera-provided** (not distributed here). Cross-check engine
> arguments against AMD's reference repo:
> [`jiejingzhangamd/infera-kimi-cust-k1-k3-mi355`](https://github.com/jiejingzhangamd/infera-kimi-cust-k1-k3-mi355).

## Which serving mode?

| | **Aggregated** | **PD + DSpark** |
|---|---|---|
| Shape | each node = one full TP8 replica (prefill **and** decode), N replicas behind a round-robin router | separate **prefill** and **decode** roles; KV moves prefill→decode over RoCE via Mooncake |
| Host prep | none | **GPU-direct RDMA (PeerDirect)** must be enabled on the hosts ([docs/01](docs/01-host-prep.md)) |
| Min nodes | 1+ | 3+ to be efficient (PD is **prefill-bound**: ~2 prefill : 1 decode) |
| Use when | < 3 nodes, or you don't need PD; the safe default | you need lower decode latency under load and have the host access to enable PeerDirect |

vLLM-native distributed PD isn't supported yet
([vllm#50682](https://github.com/vllm-project/vllm/issues/50682)); the PD path
here uses the **AMD Infera operator + Mooncake**, not vLLM's own distributed
inference. If in doubt, start aggregated.

## Layout

```
docs/
  01-host-prep.md          enable GPU-direct RDMA / PeerDirect (required for PD)
  02-serving-aggregated.md the simple baseline
  03-serving-pd-dspark.md  prefill/decode disaggregation + DSpark, via Infera
  04-verification.md       how to confirm PD is actually working (reliable gates)
  05-benchmarking.md       how to load-test and score it (OpenRouter-style view)
  06-traps.md              cluster traps that cost hours
  08-kv-aware-routing.md   tokenizer, image fix, policy weights, validation
k8s/
  weights-stage-job.yaml   stage the ~1.5 TB checkpoint + DSpark draft to NVMe
  aggregated/              self-contained aggregated serving (DaemonSet + router)
  pd-dspark/               the InferaDeployment CR for PD + DSpark
bench/
  orbench.py               open-loop, mixed-traffic load generator
```

## Quickstart — aggregated

Review [model licensing](THIRD_PARTY_NOTICES.md) before downloading or serving.
The model license has separate commercial-use conditions.

```bash
# 1) stage weights onto each GPU node's NVMe (edit node labels / paths first)
kubectl apply -f k8s/weights-stage-job.yaml

# 2) serve: one TP8 worker per node labelled node-role.kubernetes.io/gpu-worker,
#    behind an Infera round-robin router
kubectl apply -f k8s/aggregated/kimi-k3-aggregated.yaml

# 3) smoke test (through your router/gateway Service)
curl "$ENDPOINT/v1/chat/completions" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"moonshotai/kimi-k3","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

## Quickstart — PD + DSpark

1. **Host prep first** — enable PeerDirect on every GPU node and fix the node
   IPs. PD silently hangs without it. See [docs/01](docs/01-host-prep.md) and
   [docs/06](docs/06-traps.md).
2. Install the Infera operator (Helm, into `infera-system`) and stage the
   weights **and** the DSpark draft on each role's node.
3. `kubectl apply -f k8s/pd-dspark/kimi-k3-pd-dspark.yaml` (fill in the
   placeholders — nodes, hostPath, images, `MC_GID_INDEX`).
4. **Verify the handoff** with the reliable gates in
   [docs/04](docs/04-verification.md) — do *not* trust `/sys/kernel/mm/memory_peers`.

For multi-worker routing, read [KV-aware configuration and validation](docs/08-kv-aware-routing.md).
The PD recipes pin the short-input routing fix; six-node performance still
requires a controlled rerun.

## Benchmarking

`bench/orbench.py` drives open-loop, mixed traffic and scores on the view an
OpenRouter-style monitor uses (median per-request tok/s incl. TTFT, TTFT
percentiles, completion rate, 429s). Run it **from inside the cluster**, not
through `kubectl port-forward`. See [docs/05](docs/05-benchmarking.md).

## Credits

Built on AMD's Infera serving stack (Infera router + `infera.engine.vllm`),
the Mooncake KV transfer engine, and DSpark speculative decoding. Kimi-K3 by
Moonshot AI.

## Licenses

AMP PBC's recipes, documentation, and benchmark code are licensed under
[Apache-2.0](LICENSE). This repository does not bundle model weights or
container images, and Apache-2.0 does not license those external artifacts.

The recipes download `moonshotai/Kimi-K3` and `Inferact/Kimi-K3-DSpark` from
Hugging Face. Both publish the custom [Kimi K3 License](licenses/Kimi-K3-LICENSE.txt),
including Moonshot AI's copyright notice and commercial-use conditions.
Keep each model's upstream `LICENSE` with its weights and other model files.
See [third-party notices and the licensing audit](THIRD_PARTY_NOTICES.md)
for sources, redistribution requirements, and the commercial thresholds.
