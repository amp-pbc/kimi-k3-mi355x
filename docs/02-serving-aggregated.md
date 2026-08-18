# Serving Kimi-K3, aggregated baseline

This is the simplest way to serve Kimi-K3 on MI355X and the recommended
starting point. Each GPU node runs one complete TP8 replica of the model that
performs **both** prefill and decode. Multiple independent replicas sit behind a
router (or any load balancer) that fans requests out round-robin.

```
                 client / router (round-robin)
                    │            │
                    ▼            ▼
        ┌─ <GPU_NODE> ──┐   ┌─ <GPU_NODE> ──┐   ...  N nodes
        │  TP8 replica  │   │  TP8 replica  │
        │  prefill +    │   │  prefill +    │
        │  decode       │   │  decode       │
        │  8x MI355X    │   │  8x MI355X    │
        └───────────────┘   └───────────────┘
```

Every replica is self-contained: it holds the full model, computes its own
prefill, and decodes its own tokens. Nodes never talk to each other, so there is
no KV-cache handoff, no RDMA fabric dependency, and no cross-node bootstrap.
Adding capacity is just adding another labeled node; removing one drops a
replica. The router load-balances across whatever replicas are registered.

## When to use aggregated serving

- **Fewer than 3 GPU nodes.** Prefill/decode disaggregation (PD) only pays off
  once you can dedicate separate nodes to each role; see
  [03-serving-pd-dspark.md](03-serving-pd-dspark.md) for the sizing argument.
- **You don't need PD.** If your workload is not decode-latency-bound, one
  TP8 replica per node is simpler and has fewer moving parts.
- **You want the safe default.** Vanilla vLLM's native distributed
  prefill/decode is **not yet supported** on this stack. Disaggregation is done
  by the AMD Infera operator plus Mooncake, not by vLLM itself. Aggregated
  serving avoids that machinery entirely and is the correct baseline unless you
  have specifically decided you need PD.

## No host RDMA prep required

Because replicas are independent and never move KV between nodes, aggregated
serving does **not** need the GPU-direct RDMA / peer-memory host work described
in [01-host-prep.md](01-host-prep.md). That host prep is a hard prerequisite for
PD only. For aggregated serving you need nothing beyond the AMD GPU device
plugin exposing `amd.com/gpu: 8` on each node and the model weights staged
locally.

## The manifest

The aggregated deployment is at
[`k8s/aggregated/kimi-k3-aggregated.yaml`](../k8s/aggregated/kimi-k3-aggregated.yaml).
It defines, in one file:

- A **router** Deployment (the Infera router in `--router-policy round-robin`,
  `--discovery-backend kubernetes` mode). It runs on a CPU node, tokenizes each
  prompt, and dispatches to workers it discovers through the Kubernetes API. No
  operator and no CRD are involved; this is a plain Deployment.
- A **worker** DaemonSet: one TP8 replica per GPU-labeled node. Each worker
  loads the full model, self-registers with the router via a Pod annotation, and
  serves both prefill and decode. Joining a labeled node schedules a new worker
  automatically; the worker stages its own weights on first start if
  `/mnt/nvme/kimi-k3` is empty.

Key facts about the manifest:

- Base image `johnqin2025/kimi-k3-dspark` with the `inferaimage/infera-overlay`
  init container. The engine is launched through `infera-exec`, which patches
  the vLLM scheduler and Mooncake at pod start, so the command must go through
  it rather than calling `python3` directly.
- Model id `moonshotai/Kimi-K3`, `--tensor-parallel-size 8`, FP8 kernels via the
  AITER env block, 1M context (`--max-model-len 1048576`), prefix caching on,
  and the `kimi_k3` reasoning and tool-call parsers.
- Weights are mounted from the local NVMe array at `/mnt/nvme/kimi-k3`. Use a
  local device, never a shared/NFS mount, or cold start balloons.

Point your router or client at the Infera server Service to send traffic. To
graduate to prefill/decode disaggregation later, scale this deployment to zero
to free the GPUs, then follow [03-serving-pd-dspark.md](03-serving-pd-dspark.md).
