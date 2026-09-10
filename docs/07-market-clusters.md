# 07 — Market (dynamic) clusters: nodes are demand, weights live on the volume

The recipes in `k8s/aggregated/` and `k8s/pd-dspark/` assume a cluster whose
GPU nodes already exist, have names you can pin, and keep their local NVMe
between runs. A **market cluster** is the opposite on every count: it starts
with control-plane nodes and one CPU worker, and GPU nodes join only while a
workload asks for them, at the limit price you set, and are re-imaged when
they leave. This doc is the recipe for that kind of cluster; the manifests are
in [`k8s/market/`](../k8s/market/).

If you are on a National Compute cluster, its Getting Started page walks the
one-node aggregated version of this; this doc adds the six-node PD shape and
the traps.

---

## How a market cluster changes the recipe

Four things differ, and the `k8s/market/` manifests carry all four:

1. **A pod that requests 8 GPUs is the demand.** Kueue holds the pod, the
   market grants one whole node at your limit price, the node joins, the pod
   runs pinned to it. A DaemonSet never creates demand (no node, no pod), and
   a hostname `nodeSelector` names a node that does not exist yet. So the
   workers are Deployments selecting on the `node-role.kubernetes.io/gpu-worker`
   label, and `replicas` is the number of nodes.
2. **Nodes come back empty.** A node that leaves the cluster is recycled from
   the provider image, local NVMe included. The checkpoint therefore lives on
   the cluster's **shared volume** (`/mnt/shared`, mounted on every node,
   outlives every node), staged once; each worker copies it onto its node's
   NVMe in an initContainer and the engine loads from the NVMe as before.
3. **Host prep is the platform's job.** GPU-direct RDMA (PeerDirect, docs/01)
   and real node IPs (docs/06) are converged by the node image's startup
   chain before a node attaches. You verify receipts; you do not rebuild
   drivers.
4. **No operator.** On a Kueue-gated cluster the PD roles are plain
   Deployments (see the header of `k8s/market/pd-dspark.yaml` for why that is
   equivalent to the InferaDeployment and what the operator would have added).

Everything else — images, engine arguments, the AMD env block, the three
Mooncake pins — is identical to the static recipes.

---

## Prerequisites

- **A shared volume of 2 TiB or more** mounted at `/mnt/shared` on the CPU
  worker and every GPU node. The checkpoint plus draft need ~1.5 TB. A 1 TiB
  volume cannot hold it; the staging Job says so and stops.
- **A limit price set on the cluster.** Pods pull in nodes only while the
  market clears at or under it. On National Compute this is the Pricing tab
  (set once per cluster).
- **Kueue's pods-ready timeout must cover a cold start.** Kueue evicts an
  admitted workload whose pods are not `Ready` within
  `waitForPodsReady.timeout`, and for a Deployment pod that means the pod is
  deleted, recreated, and re-enters the market on a fresh node — which
  copies the weights again and never converges. A cold start here is the
  image pull (13.5 GiB) + the 1.5 TB copy + the engine load (10–14 min), so
  30 minutes is too short; budget 4 hours. This is a cluster-level setting
  (the platform's, on National Compute); ask before launching if you are not
  sure what it is.
- Six nodes for the PD shape below (4 prefill + 2 decode); eight also works
  (5 + 3 or 6 + 2). One node for the aggregated smoke test.

---

## Steps

### 1. Stage the weights onto the shared volume (once per cluster)

```bash
kubectl apply -f k8s/market/weights-to-shared.yaml
kubectl logs -f job/kimi-k3-weights
```

Runs on the CPU worker, so it starts immediately and runs while the GPU
nodes are still being provisioned. Writes `/mnt/shared/kimi-k3/weights/`
(`Kimi-K3/`, `Kimi-K3-DSpark/`, a `.staging` heartbeat while running,
`.complete` when done). Workers wait for the heartbeat and copy after
`.complete`; if the Job is deleted or its heartbeat goes stale (10 minutes),
workers fall back to downloading from the Hub onto their own NVMe.

### 2. Smoke test: one aggregated node (optional but recommended the first time)

```bash
kubectl apply -f k8s/market/aggregated.yaml
kubectl get pods -w
```

Proves the whole pipeline on one paid node: market grant, node join, weight
copy, engine load, router discovery. Then tear it down before PD:

```bash
kubectl delete -f k8s/market/aggregated.yaml
```

Deleting releases the node; PD gets fresh nodes and copies the weights again
from the volume. There is no way to hand a node from one workload to
another.

### 3. Launch PD + DSpark (6 nodes)

```bash
kubectl apply -f k8s/market/pd-dspark.yaml
kubectl get pods -w
```

What you will see, per worker pod:

| phase | reads as | typical |
|---|---|---|
| held by Kueue, market deciding, node provisioning | `Pending` (SchedulingGated) | 10–15 min |
| image pull + weight copy from the volume | `Init:0/2`, `Init:1/2` | 5 min + about 2 min (32 parallel streams read the export at ~14 GB/s; one stream took 45 min) |
| engine load, graph capture | `Running` `0/1` | 10–14 min |
| serving | `Running` `1/1` | |

The router pod (`infera`) is `Running 1/1` within a minute; it serves as soon
as one prefill and one decode worker register, and adds the rest as they
arrive. Requests before that return errors, not hangs.

### 4. Verify the host receipts on the first node that joins

Once a GPU node is `Ready`, from a privileged debug pod on it (or your own
access):

```bash
kubectl debug node/<GPU_NODE> -it --image=busybox --profile=sysadmin -- \
  chroot /host sh -c 'dmesg | grep -i "PeerDirect support"; modinfo -F depends amdgpu | grep -o ib_uverbs'
# want: "PeerDirect support was initialized successfully" and "ib_uverbs"
kubectl get nodes -o wide   # INTERNAL-IP must be the address on the node's NIC
```

If a receipt is missing, stop: PD will hang exactly as docs/01 and docs/06
describe. That is a platform node-image problem, not a manifest problem.

### 5. Verify the handoff

Everything in [docs/04](04-verification.md) applies, with `-n default` and the
same labels:

```bash
kubectl logs -c main -l infera.amd.com/service=decode | grep -c "Failed to register memory"   # want 0
kubectl logs -c main -l infera.amd.com/service=prefill | grep "KV Transfer"                  # want N/N successful, 0 failed
kubectl logs -c main -l infera.amd.com/service=decode | grep -c "running the draft eagerly"    # want 0
```

Smoke request:

```bash
kubectl port-forward svc/infera 8000:8000 &
curl localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"moonshotai/Kimi-K3","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

The router must spread the load. Under traffic every engine logs an
`Engine 000: ... Running: N reqs` line every 10 s; an engine that stays
silent is idle. The router logs one `pick ... picked=<ip>:30000` line per
request and role; the picks must rotate across the four prefill and the two
decode addresses and `request_blocks` must be non-zero (else see the
"Router pinned on one worker per role" trap below):

```bash
kubectl logs deploy/infera --since=5m | grep -oE 'role=(prefill|decode)|picked=[0-9.]+' | paste - - | sort | uniq -c
```

### 6. Load test, from inside the cluster

```bash
kubectl apply -f k8s/market/loadtest.yaml     # once every worker reads READY 1/1
kubectl logs -f job/kimi-k3-loadtest
```

`bench/orbench.py` as a Job on the CPU worker (the script rides in a
ConfigMap): open-loop Poisson arrivals across the realistic mix, sweeping
0.5, 1, 2 and 4 req/s by default (edit the `RATES`/`SECS`/`WARM`/`DRAIN`
env in the file), about 17 minutes. Read the operating point off the
rate-vs-stats table as docs/05 describes. The Job offers no load until every
worker is Ready and discovered by the router (it prints `workers ready: N/M`
every 30 s while waiting). The results outlive it: the full output lands at
`/mnt/shared/kimi-k3/loadtest/orbench-<time>.log` with the per-request CSV
beside it and `latest.log` pointing at the newest run; the Job stays an hour
after it finishes. Read a run any time:

```bash
kubectl apply -f k8s/market/loadtest-results.yaml    # RUN=latest, or a listed timestamp
kubectl logs -f job/kimi-k3-loadtest-results
```

### 7. Scale, or stop paying

```bash
kubectl scale deploy/kimi-k3-prefill --replicas=5     # each replica buys a node
kubectl delete -f k8s/market/pd-dspark.yaml           # releases all six nodes
```

The weights stay on the shared volume for the next start.

---

## Traps specific to market clusters

- **Pods-ready eviction loop.** See Prerequisites. Symptom: a worker pod is
  deleted around the 30-minute mark while still in `Init` or `0/1`, a new
  pod appears `Pending`, a new node arrives, repeat. Fix the cluster
  setting; no manifest change helps (a probe-less pod would read `Ready`
  before it serves, which defeats the `READY 1/1` story and the Service).
- **Switching modes re-stages.** Aggregated → PD (or back) releases every
  node; the new pods copy from the volume again. Budget the copy time; do
  not expect the second mode to come up faster.
- **Router pinned on one worker per role.** The kv-aware policy tokenizes
  every request and scores workers by cache misses plus load, both in token
  blocks. If the tokenizer does not load (the router logs `kv-aware: ...
  failed`; every `pick` line reads `request_blocks=0`) every worker ties at
  zero and the first one wins, forever: six nodes serve like one prefill
  and one decode while billing six, and only a load test shows it
  (2026-09-10: TTFT p50 81 s at 2 req/s, four engines silent). Kimi-K3's
  tokenizer is custom code (`tokenization_kimi.py` + `tiktoken.model`) the
  Hub id does not bring down, so the market router reads it from the staged
  weights on the shared volume (`--router-tokenizer-path
  /shared/kimi-k3/weights/Kimi-K3`). `--router-policy round-robin` is the
  stateless fallback while a tokenizer problem is open.
- **Partial PD.** Pods are admitted one at a time as nodes arrive. A single
  prefill or decode worker up on its own is not an error; the router waits
  for the other role. If one role never arrives, check the market (price,
  supply), not the fabric.
- **Weights on the volume, not the node, are the source of truth.** A node
  that already carries the weights (a re-used node) starts in minutes; do
  not read that as the normal case. And never point the engine at
  `/mnt/shared` directly: cold start over NFS balloons (docs/02), and six
  engines reading the same export concurrently balloons harder.
- **Everything else in [docs/06](06-traps.md) still applies**, including the
  ~20-minute stall a malformed request leaves behind on a PD engine.
