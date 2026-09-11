# Serving Kimi-K3 with prefill/decode disaggregation and DSpark

This recipe splits inference across two roles on separate GPU nodes: a
**prefill** node computes the KV cache for each prompt, hands it to a **decode**
node over RDMA, and the decode node generates tokens with **DSpark** speculative
decoding. It targets decode-latency-bound workloads, where keeping prefill from
stealing decode cycles measurably improves per-request output speed.

Read [01-host-prep.md](01-host-prep.md) first: PD has a hard host-level
prerequisite (GPU-direct RDMA / peer memory) that aggregated serving does not.
If you have fewer than 3 GPU nodes or do not need PD, use
[02-serving-aggregated.md](02-serving-aggregated.md) instead.

On a **market (dynamic) cluster** the same roles ship as plain Deployments in
`k8s/market/pd-dspark.yaml` (no operator, no hostnames, weights from the shared
volume); see [07-market-clusters.md](07-market-clusters.md). This doc's
fabric and engine facts apply unchanged.

## Architecture

Disaggregation here is **not** vLLM-native distributed inference. Vanilla
vLLM's native distributed prefill/decode is not supported on this stack. The AMD
**Infera operator** plus **Mooncake** do the disaggregation.

```
                     client / router
                          │
                          ▼
        ┌─ Infera router (infera.server) ─────────────┐
        │  policy kv-aware by default; force           │
        │  round-robin until a role scales to 2+       │
        └───────┬─────────────────────────┬────────────┘
                ▼                          ▼
   ┌─ <PREFILL_NODE> ─────────┐   ┌─ <DECODE_NODE> ──────────┐
   │  PREFILL role            │   │  DECODE role             │
   │  kv_role: kv_producer    │   │  kv_role: kv_consumer    │
   │  infera.engine.vllm TP8  │   │  infera.engine.vllm TP8  │
   │  DSpark draft loaded     │   │  DSpark draft loaded     │
   │  8x MI355X               │   │  8x MI355X               │
   │                          │   │                          │
   │  KV blocks ══════════════════════════════════►         │
   │       (MooncakeConnector over RoCE, no TCP fallback)    │
   └──────────────────────────┘   └──────────────────────────┘
```

You deploy a single custom resource, `kind: InferaDeployment`. The Infera
operator (Helm-installed into an `infera-system` namespace, with the
`inferadeployments.infera.amd.com` CRD) watches the CR and reconciles it into
three Deployments/Services: the router (`componentType: server`), the prefill
worker (`role: prefill`), and the decode worker (`role: decode`). It wires the
`kv_producer`/`kv_consumer` roles and the Mooncake bootstrap for you.

The KV cache moves prefill -> decode over the RoCE fabric via the
`MooncakeConnector` (`--kv-transfer-config`). There is no TCP fallback: the two
nodes **must** sit on a mutually routable RoCE fabric, which is why host prep and
the fabric env below are load-bearing.

## The three required Mooncake fabric env vars

These three env vars must be set on **both** the prefill and decode worker
containers. Each was necessary in live bring-up; **removing any one reproduces a
silent KV-handoff hang** (the request never completes and never errors, all pods
stay Ready, health checks pass, and the decode engine logs no inference). Do not
diagnose a PD hang from `kubectl get pods` alone.

- **`MC_GID_INDEX=1`** — Mooncake's auto-GID selection picks GID index 0, which
  on these NICs is the link-local `fe80::` GID rather than the routable RoCE v2
  GID. When every rail is L3-routed, using GID 0 kills every transfer slice
  (transport retry exceeded / CQE error 12) while VRAM registration and
  discovery still look fine: the wire is dead but nothing errors. Set this to
  **your device's RoCE v2 GID index** (index 1 on the ionic NICs here; confirm
  yours with `show_gids` and pick the v2, routable entry).
- **`MC_DISABLE_HIP_TRANSPORT=1`** — the intra-node HIP/XGMI shortcut advertises
  an empty segment that the peer rejects cross-node. Disabling it forces real
  RDMA over the wire.
- **`MC_ENABLE_DEST_DEVICE_AFFINITY=1`** — the rail planes are RDMA-isolated
  (same-name rail to same-name rail carries data; cross-rail handshakes succeed
  then move zero bytes to timeout). ICMP routes between planes but RoCE does
  not. This knob makes Mooncake pair same-**name** NICs (rail-optimized
  topology) so every slice stays on its own rail.

## hostNetwork and the bootstrap address

Both workers run **`hostNetwork: true`** because the RDMA rails are host
interfaces; the flannel/CNI pod network cannot reach them, so without host
networking Mooncake has no path to the peer even though the engine looks
perfectly healthy. Set `dnsPolicy: ClusterFirstWithHostNet` alongside it.

The engine must pass **`--data-parallel-address $(POD_IP)`** (with `POD_IP`
injected from `status.podIP`). Mooncake's bootstrap server binds `0.0.0.0` but
*advertises* this address; left at the default `0.0.0.0` the prefiller registers
`http://0.0.0.0:8998`, the decoder cannot reach it, and the request hangs to the
480 s connector timeout while both workers look healthy.

Because these are `hostNetwork` pods, `status.podIP` is the node's **registered**
InternalIP. If a node's registered IP does not exist on any real interface,
Mooncake advertises an unreachable address and you get the same hang. This is the
ghost node-IP trap; make sure each node's registered IP is real before the first
PD attempt. See [06-traps.md](06-traps.md).

## DSpark speculative decoding

DSpark raises per-request decode speed. Its `--speculative-config` (method
`dspark`, the draft model path, `num_speculative_tokens`, and the MLA attention
backend) must be set on **both** roles, and the draft model must be staged on
**both** nodes.

Putting speculation only on decode looks correct (the prefiller never samples)
but does not work. vLLM merges the draft's layers into the same KV-cache
namespace as the target's, so a speculating decoder registers more layer names
than a non-speculating prefiller has, and even with the layer lists forced to
agree, speculation changes the decoder's block accounting so the two sides
disagree on how many blocks a request needs. The prefiller must be
speculation-aware so both sides compute the block count identically; loading the
draft there is the price of that agreement, not redundancy. Both failure modes
**hang** rather than error.

## Sizing: PD is prefill-bound

At 1 prefill + 1 decode the prefill engine saturates (roughly 100% busy) while
the decode engine sits around half-utilized. The efficient shape is therefore
**2 prefill : 1 decode**, which needs 3 or more GPU nodes. Below 3 nodes, prefer
aggregated serving; a 1P1D deployment works but leaves the decode node
underused. Scale the prefill `replicas` first when adding capacity.

## Operational prerequisite: reject malformed requests upstream

The PD engine validates requests **after** prefill. A malformed request that
gets past your router therefore leaves an aborted Mooncake transfer behind, and
aborted transfers stall valid traffic for roughly 20 minutes and mimic a fabric
failure. Enforce strict request validation at the router/gateway **before**
anything reaches the PD engine. This is not optional hardening; it is a
correctness requirement for keeping PD healthy under real traffic.

## The manifest

The full custom resource is at
[`k8s/pd-dspark/kimi-k3-pd-dspark.yaml`](../k8s/pd-dspark/kimi-k3-pd-dspark.yaml).
Notes for adapting it:

- Base image `johnqin2025/kimi-k3-dspark`, overlay `inferaimage/infera-overlay`,
  launched through `infera-exec`. Set `INFERA_REQUIRE_NATIVE=mooncake` so a
  payload missing the Mooncake transport fails at startup instead of quietly
  serving with no KV transfer.
- Set the prefill and decode `nodeSelector` to your two GPU nodes
  (`<PREFILL_NODE>` / `<DECODE_NODE>`), and mount each node's **local** weights
  path at `/mnt/nvme/kimi-k3`; the paths need not match between nodes, and must
  not be a shared/NFS mount.
- Both workers need `privileged`/`IPC_LOCK`, `/dev/infiniband`, a large
  `/dev/shm`, and the host `libionic.so` bind-mount so `libibverbs` matches the
  host ionic kernel ABI. Without a matching libionic, no RDMA devices are found
  and Mooncake falls back to HIP IPC, which cannot open a peer node's handle, so
  PD dies while a single-node smoke test passes.
- `--gpu-memory-utilization 0.88` is the value validated on this image at 1M
  context with the draft loaded; do not carry a higher number over from a
  non-DSpark recipe or KV allocation OOMs.

Once deployed, point your router/client at the Infera server Service. Then
verify the handoff is actually happening; passing health checks and correct
answers do **not** prove PD works. Follow
[04-verification.md](04-verification.md).
