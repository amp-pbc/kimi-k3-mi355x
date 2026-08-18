# 01 — Host prep: GPU-direct RDMA (PeerDirect) for Mooncake KV transfer

This recipe covers the one host-level prerequisite that separates a working
aggregated Kimi-K3 deployment from a working **prefill/decode-disaggregated
(PD)** deployment on AMD MI355X (8x per node) Kubernetes hardware.

If you only run aggregated serving (one engine does both prefill and decode),
you can skip this doc. If you want PD — where a **prefill node** produces the
KV cache and hands it to a separate **decode node** over the RDMA fabric via
Mooncake — the NIC has to DMA directly out of GPU VRAM, and that requires
GPU-direct RDMA (PeerDirect) to be correctly enabled on every GPU host.

All kernel, driver, and OFED versions below are **examples — yours will
differ.** Substitute the versions your nodes actually report.

---

## Why PD needs PeerDirect

In a PD deployment the KV handoff moves blocks of key/value cache from the
prefill engine's GPU VRAM to the decode engine's GPU VRAM across the RDMA
fabric. For the NIC to read those bytes straight out of GPU VRAM (instead of
staging them through host memory), it must be able to DMA into the GPU's BAR
space. That capability is **GPU-direct RDMA**, and on this stack it is provided
by a **peer-memory client** that the GPU driver registers with the kernel's
RDMA core (`ib_uverbs` / `ib_core`).

If the peer-memory client is not registered, `ibv_reg_mr` on a VRAM address
fails, Mooncake cannot pin GPU buffers, and the KV transfer either errors out
(`Failed to register memory`) or hangs to the connector timeout. The decode
side then looks like a fabric failure even though the fabric is healthy.

---

## The real root cause on ROCm 7

There is a persistent myth that you need a separate `amdp2p` (a.k.a.
`ib_peer_mem`) kernel module. **On ROCm 7 there is no separate module.** The
peer-memory client is compiled *into* `amdgpu` itself (source
`amd/amdkfd/kfd_peerdirect.c`). Searching for `amdp2p.ko` will always come up
empty on this stack, and its absence proves nothing. Do not chase it.

The actual failure shows up in `dmesg` on the GPU host, repeating on every KFD
init retry:

```
failing symbol_get of non-GPLONLY symbol ib_register_peer_memory_client.
```

Two defects compound to produce it:

1. **`amdgpu-dkms` was built before the OFED tree existed on the box.** This is
   an image build-order defect. The amdgpu DKMS configure step probes the OFED
   tree (`/usr/src/ofa_kernel/x86_64/<kver>/Module.symvers`) for the
   peer-memory symbol and **direct-links** against it when found (the
   `HAVE_KFD_PEERDIRECT_SUPPORT` + `NEED_OFAPATH` path in
   `amd/dkms/m4/peer_direct.m4`). If the OFED tree was not present at build
   time, the symbol is not found, and the module falls back to resolving it at
   **runtime** via `symbol_request()` / `symbol_get`.

2. **Kernels >= 6.6 refuse `symbol_get` on non-GPL-only exports.** This OFED
   build exports `ib_register_peer_memory_client` as a plain `EXPORT_SYMBOL`
   (not `EXPORT_SYMBOL_GPL`), so the runtime fallback is rejected by the
   kernel. It can never succeed on a modern kernel.

So the module was built to look up the symbol at runtime, and the runtime
lookup is exactly what the kernel now forbids. That is the whole bug.

---

## The fix is a REBUILD, not an install

The important insight: **the direct-link branch is NOT subject to the GPL-only
restriction.** A module that resolves the symbol at link time via the OFED
`Module.symvers` never calls `symbol_get`, so kernel 6.6+ has no objection.

Once the OFED tree is present on the box
(`/usr/src/ofa_kernel/x86_64/<kver>/` with both `Module.symvers` carrying the
symbol and `include/rdma/peer_mem.h`), a **rebuild** of amdgpu against that
tree takes the direct-link branch. A plain `dkms install` of the prebuilt
module does not — you must `dkms build` first so the configure step re-probes
and finds the OFED tree.

Do this on each GPU node, one at a time, draining PD workers off the node
first.

```bash
# Versions here are EXAMPLES — yours will differ. Read them off the box:
#   modinfo amdgpu | grep ^version        -> the amdgpu/DKMS version
#   uname -r                              -> the running kernel
#   ls /usr/src/ofa_kernel/x86_64/        -> confirm the OFED tree exists

# 1. REBUILD amdgpu against the now-present OFED tree (example versions):
sudo dkms build amdgpu/6.16.13-EXAMPLE -k 6.8.0-EXAMPLE-generic --force

# 2. BEFORE installing, confirm the rebuild took the direct-link branch.
#    The freshly built .ko must now depend on ib_uverbs:
modinfo -F depends \
  /var/lib/dkms/amdgpu/6.16.13-EXAMPLE/6.8.0-EXAMPLE-generic/x86_64/module/amdgpu.ko* \
  | grep ib_uverbs
#    Empty output here = the rebuild did NOT find the OFED tree. Stop and fix
#    the tree before going further; installing now just reinstalls the broken
#    module.

# 3. Install the rebuilt module:
sudo dkms install amdgpu/6.16.13-EXAMPLE -k 6.8.0-EXAMPLE-generic --force
```

### Load amdgpu from the real root, not the initramfs

The rebuilt module now depends on `ib_uverbs`, and that dependency is **not
present in the initramfs**. If amdgpu is autoloaded from the initrd at early
boot, the modprobe fails **silently** — there is no dmesg receipt, and the node
comes up looking like the fix never happened.

Force amdgpu to load from the real root filesystem at boot, after the OFED
modules are available, by declaring it in `modules-load.d`:

```bash
echo amdgpu | sudo tee /etc/modules-load.d/amdgpu-peerdirect.conf
```

### Reboot IN-GUEST, never a provider hard reset

```bash
sudo reboot
```

Use an **in-guest reboot**. Do **not** use a provider-side hard reset / power
cycle. On these VMs a hard reset is an NVMe coin-flip that can corrupt or drop
the local weight RAID; the in-guest reboot is the safe path and is all that is
needed to pick up the new module and the `modules-load.d` entry.

---

## Verification receipts

After the reboot, confirm the fix landed. These are the receipts that actually
mean something:

```bash
# (host) THE receipt — PeerDirect initialized without the symbol_get failure:
sudo dmesg | grep -i "PeerDirect support"
# want: "PeerDirect support was initialized successfully"
# and:  zero "non-GPLONLY" failures

# (host) the rebuilt module direct-links ib_uverbs (proves the rebuild landed):
modinfo -F depends amdgpu | grep -o ib_uverbs
# want: ib_uverbs
```

If PD is already deployed, the decode-side counter is the most reliable
end-to-end health gate (0 = healthy):

```bash
kubectl -n <namespace> logs -c main -l <decode-role-selector> \
  | grep -c "Failed to register memory"
# want: 0
```

### CRITICAL: do NOT gate on `/sys/kernel/mm/memory_peers`

`/sys/kernel/mm/memory_peers` **never appears on this OFED build, even when
PeerDirect is working correctly.** Older preflight notes (including some
vendor-supplied ones) used its presence as the success gate; that gate is
wrong here. The module and sysfs probes read **identically** in the working and
broken states — AMD has confirmed both states look the same through those
probes. Gate only on the dmesg receipt, the `modinfo` dependency, and (once PD
is up) the decode-side `Failed to register memory` counter.

---

## The amdgpu device-plugin trap

Every time the driver comes up **after** the amdgpu device-plugin pod (which is
exactly what happens across a reboot), the node advertises `amd.com/gpu: 0` and
stays that way — the plugin enumerated GPUs before the driver was ready and
never re-checks. Bounce the plugin pod on that node:

```bash
kubectl delete pod -n kube-system <amdgpu-device-plugin pod on this node>
kubectl get node <GPU_NODE> -o jsonpath='{.status.allocatable.amd\.com/gpu}'
# want: 8
```

Recovery is fast (seconds). Expect to do this after every node reboot or
maintenance event.

---

## Persistence: this MUST live in the node image, not your hands

A node **recycle re-images from the provider template** and resurrects the
unbuilt state — the amdgpu module reverts to the pre-rebuild binary, the
`modules-load.d` entry is gone, and PD breaks again exactly as before. Any
hand-applied fix is lost the moment a node is recycled.

Therefore this rebuild + `modules-load.d` step **must land in the node image or
in the startup-script chain your provider runs on GPU-class nodes** (your
provider's node provisioning), not be applied by hand once. Treat the
hand-applied version as a bring-up hack; the durable fix is in the image build,
and it must fix the original build-order defect (build amdgpu-dkms *after* the
OFED tree is installed) so the rebuild is unnecessary on a fresh image.

---

## Residual risk (state it honestly)

These are provider VMs, and peer DMA — the NIC reading the GPU BAR through the
virtual root complex — is the part that can still fail even when the entire
software stack is correct. The rebuild is necessary and cheap, but it only
proves the *software* path. Whether the VM's DMA path actually moves VRAM is
proven only by the first successful `ibv_reg_mr` on a VRAM address plus a real
KV handoff.

If registration succeeds but transfers then throw protection errors, that is no
longer a driver problem — it is a virtualization/IOMMU conversation with your
provider about the guest's peer-DMA path.

---

## Related

- Fabric prerequisites and the Mooncake env knobs (`MC_GID_INDEX`,
  `MC_DISABLE_HIP_TRANSPORT`, `MC_ENABLE_DEST_DEVICE_AFFINITY`) live with the
  PD manifest.
- Traps that cost hours (ghost node-IP, storage, device-plugin, weight
  staging, aborted transfers): see [`06-traps.md`](06-traps.md).
- AMD's reference manifests, preflight scripts, and `KNOWN-ISSUES.md` for this
  class of cluster:
  `github.com/jiejingzhangamd/infera-kimi-cust-k1-k3-mi355`.
