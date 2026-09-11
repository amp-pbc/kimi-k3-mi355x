# Verifying that prefill/decode disaggregation actually works

See [KV-aware routing validation and load-test limitations](08-kv-aware-routing.md)
before comparing multi-worker performance.

A PD deployment can pass every naive check while doing no disaggregation at all.
All pods report Ready, health probes return 200, restart counts stay at 0, and
the model returns correct text, because a KV handoff that fails **open** simply
re-prefills locally on the decode node and returns the same answer, just without
the benefit of PD. `kubectl get pods` proves nothing here.

This page lists the gates that reliably distinguish a working PD deployment from
a broken one, and the misleading probes you must **not** gate on.

## Reliable gates

### 1. Decode side: zero memory-registration failures

The single most reliable in-cluster gate. On the **decode** worker, the count of
`Failed to register memory` in the engine log must be **0**:

```bash
kubectl -n infera logs -c main -l infera.amd.com/service=decode \
  | grep -c "Failed to register memory"
# 0 = healthy. Any nonzero value means peer-memory registration is broken
# (this read 576 in the broken state).
```

### 2. Decode side: external cache hit ~100% and own prompt throughput ~0

This is the positive proof that the KV cache came off the wire instead of being
recomputed. On the decode engine, look for:

- `External prefix cache hit rate` at roughly **100%**, and
- the decode engine's **own** `Avg prompt throughput` at roughly **0**.

Together these mean the decode node did no prefill of its own: it received the KV
blocks from the prefill node and went straight to generating. If the decode node
shows meaningful prompt throughput, it is re-prefilling locally and the handoff
is failing open.

### 3. Prefill side: all KV transfers successful, zero failed

On the **prefill** worker, the KV-transfer log line must report **N/N
successful, 0 failed** (e.g. `KV Transfer: 8/8 successful ... 0 failed`).
Any nonzero failure count means slices are dying on the fabric (revisit the
`MC_GID_INDEX` / rail-affinity env in
[03-serving-pd-dspark.md](03-serving-pd-dspark.md)).

### 4. DSpark healthy: zero eager-draft fallbacks

Speculative decoding is working only if the decode log contains **zero**
occurrences of `running the draft eagerly`. Any such line means DSpark fell back
to an eager path and speculation is not delivering its speedup.

### 5. Host receipts (run on each GPU node)

These confirm the peer-memory prerequisite landed at the host level:

```bash
# The GPU-direct RDMA receipt: peer-memory client registered with the RDMA core.
sudo dmesg | grep -i "PeerDirect support"
# want: "PeerDirect support was initialized successfully"

# The rebuilt amdgpu module directly depends on the RDMA uverbs module.
modinfo -F depends amdgpu | grep -o ib_uverbs
# want: "ib_uverbs" present (absent = the peer-memory rebuild did not land)
```

## Misleading probes — do NOT gate on these

Two commonly suggested checks read **identically** in the working and broken
states, so they carry no signal and have caused misdiagnoses:

- **`/sys/kernel/mm/memory_peers`** — with this OFED build this path does not
  appear even when peer memory is working correctly. Its absence is not a
  failure; do not treat it as a gate.
- **Module-presence probes** (e.g. searching for a standalone `amdp2p.ko`, or
  otherwise checking whether a peer-memory module is "loaded") — in this stack
  the peer-memory client is compiled into `amdgpu` itself, so a missing separate
  module proves nothing, and generic module-presence checks look the same
  whether registration succeeded or failed.

The vendor documents both of these ambiguous states in the AMD reference repo
`jiejingzhangamd/infera-kimi-cust-k1-k3-mi355` (KNOWN-ISSUES). Gate only on the
receipts in the "Reliable gates" section above: the decode-side
`Failed to register memory` count, the external cache hit / own-throughput pair,
the prefill KV-transfer success line, the absence of eager-draft fallbacks, and
the dmesg `PeerDirect support` receipt plus the `ib_uverbs` dependency.
