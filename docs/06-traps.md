# 06 — Traps that cost hours

Every item here cost real debugging time on live MI355X (8x per node)
Kubernetes hardware. They share a common shape: the symptom points somewhere
other than the cause. Read this before you spend an afternoon chasing the wrong
layer.

Versions, addresses, and node names in examples are **placeholders — yours will
differ.**

---

## 1. Ghost node-IP: the registered address does not exist on the box

**Symptom.** Cross-node pod traffic black-holes. Direct `kubectl port-forward`
to an engine works fine, so the engine "looks healthy," but the router (or any
pod on another node) times out talking to it. In a PD deployment the decode
node hangs on the Mooncake bootstrap all the way to the connector timeout
(~480 s), which is easy to misread as the RDMA / PeerDirect fix having failed.

**Root cause.** Each node's RKE2 `node-ip` pins an InternalIP that **does not
exist on any interface of the box.** These stale pins typically come from a
declared node inventory (your provider's node provisioning) that was not
updated after a migration churned every worker's DHCP lease, so the cluster was
built on stale addresses. Flannel then advertises those ghost IPs as its VXLAN
VTEP endpoints, and every cross-node pod packet is sent to an address that
exists nowhere on the fabric — it black-holes. `kubectl` itself keeps working
because RKE2 tunnels the API server to the kubelet and never touches the pod
network; that is why port-forwards succeed while pod-to-pod dies.

**Why it ALSO breaks PD.** The PD workers run `hostNetwork: true`, and a
hostNetwork pod's `status.podIP` **is the node's registered InternalIP** — the
ghost. The manifest passes `--data-parallel-address $(POD_IP)` precisely so
Mooncake advertises a reachable bootstrap address instead of `0.0.0.0`. With a
ghost node-IP it instead advertises an address that exists on no interface, and
the decode side hangs to the connector timeout — the same failure shape as the
`0.0.0.0` trap. Fix the node-IPs **before** the first post-PeerDirect PD
attempt, or the hang will be blamed on the RDMA fix.

**Diagnose.**

```bash
# Each flannel public-ip must be an address that actually exists on that
# node's primary NIC (e.g. ens3):
kubectl get nodes -o custom-columns='NAME:.metadata.name,\
FLANNEL:.metadata.annotations.flannel\.alpha\.coreos\.com/public-ip'

# On a node, the VTEP local IP must exist on the real NIC:
ip -d link show flannel.1 | grep local
ip -4 addr show ens3
```

**Durable fix.** Correct `node-ip` in each node's RKE2 config
(`/etc/rancher/rke2/config.yaml`) to the address that actually exists on the
box, and restart the agent. This needs host access.

**Stopgap** (reverts if the agent re-registers, so it is not a fix):

```bash
kubectl annotate node <NODE> \
  flannel.alpha.coreos.com/public-ip-overwrite=<REAL_NIC_IP> --overwrite
kubectl delete pod -n kube-system <rke2-canal pod on that node>
```

---

## 2. `local-path` StorageClass writes to the small root disk, not the NVMe

**Symptom.** A PVC reports plenty of capacity (e.g. `2Ti`), then the weight
download dies around ~80 GB and the node's **root filesystem** fills up.

**Root cause.** The default `local-path` StorageClass provisions under a path
on each node's **small root disk** (order of ~100 GB), not the large NVMe RAID.
The big storage — the multi-TB `/dev/md0` NVMe RAID — is mounted elsewhere
(e.g. `/mnt/nvme`). A `local-path` PVC happily advertises the RAID-sized
capacity and then runs out of space on root.

**Fix.** For the Kimi-K3 weights, use a **`hostPath` on the NVMe RAID**
(`/mnt/nvme/kimi-k3`), not a PVC. This is the right choice regardless:
`local-path` is `ReadWriteOnce` and node-pinned anyway, so the PVC indirection
buys nothing. Keep a free-space guard on the download job (refuse to start
unless it sees ~1.7 TB free) so this cannot recur silently. Note that using
hostPath pins the pod to the node that holds the weights.

---

## 3. amdgpu device-plugin drops GPUs after node maintenance

**Symptom.** After a reboot or any node maintenance, the node reports
`amd.com/gpu: 0` **forever**, and GPU pods stay Pending.

**Root cause.** If the amdgpu driver comes up **after** the device-plugin pod,
the plugin enumerated zero GPUs and never re-checks. A reboot (including the
in-guest reboot the PeerDirect rebuild requires) reliably triggers this.

**Fix.** Bounce the plugin pod on the affected node; it re-enumerates in
seconds.

```bash
kubectl delete pod -n kube-system <amdgpu-device-plugin pod on the node>
kubectl get node <GPU_NODE> -o jsonpath='{.status.allocatable.amd\.com/gpu}'
# want: 8
```

Expect to do this after every reboot or maintenance event on a GPU node.

---

## 4. Weights are ~1.5 TB and must be staged per node

**Symptom.** A newly added GPU node cannot serve, or a pod scheduled to a fresh
node is Pending / crash-looping on missing weights.

**Root cause.** There is no shared filesystem across nodes here; weights live
on each node's **local** NVMe RAID. The Kimi-K3 checkpoint is ~1.5 TB and does
**not** replicate itself. Every GPU node needs its own full copy staged to
`/mnt/nvme/kimi-k3` before it can serve.

**Notes.**

- Staging is faster than you might fear (a fast object pull can move the full
  checkpoint in roughly the tens of minutes per node), so re-staging after a
  disk wipe is cheaper than it sounds — but it is still a hard prerequisite,
  not an afterthought.
- This is the long pole for scaling: a new node needs a full weight stage
  **plus** engine cold start before it can take traffic, so reactive
  autoscaling is not viable. Stage deliberately, ahead of need.
- If you run more than one weight consumer on a node (e.g. a second reference
  copy), watch that both copies fit the RAID before assuming disk pressure is
  something else.

---

## 5. A malformed request can stall a PD engine for ~20 minutes

**Symptom.** Valid traffic to a PD deployment stalls for roughly 20 minutes and
looks exactly like an RDMA / fabric failure — but the fabric is healthy and
nothing was reconfigured.

**Root cause.** A malformed or invalid request that reaches a PD engine can
leave an **aborted Mooncake transfer** in flight. The stuck transfer holds
resources and stalls otherwise-valid traffic behind it until it finally times
out and unwinds (~20 min). Because the shape mimics a fabric problem, it sends
you debugging the wrong layer entirely.

**Fix.** **Validate requests strictly upstream** — enforce schema and bounds at
the gateway / admission layer so malformed requests never reach a PD engine in
the first place. This is the cheapest possible guard and it removes an entire
class of phantom "fabric failure" incidents.

**Related.** PD concurrency tolerance is workload-shaped: prefill-bound traffic
saturates a single prefill node fast, so do not raise the admission cap for PD
based on mixed-output sweeps. When in doubt, keep the cap and validate hard.

---

## See also

- Host-level PeerDirect / GPU-direct RDMA enablement (the prerequisite for PD):
  [`01-host-prep.md`](01-host-prep.md).
- AMD's reference manifests, preflight scripts, and `KNOWN-ISSUES.md` for this
  class of cluster:
  `github.com/jiejingzhangamd/infera-kimi-cust-k1-k3-mi355`.
