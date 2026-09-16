# 10 — Decode context parallelism on MI355X

**Status, 2026-09-16:** We independently tested single-node TP8/DCP1 versus
TP8/DCP8 using the upstream ROCm vLLM 0.29.0 image below. Both passed six
correctness smoke checks and all 1,344 measured requests completed. DCP8
increased reported KV capacity by 3.985×, but showed no material throughput
gain on the synthetic input-heavy workload. See the
[measured results and limitations](benchmarks/dcp-2026-09-16.md). This remains
an opt-in aggregated configuration and does not validate the repository's
multi-node Infera + Mooncake + DSpark path.

DCP partitions the attention KV cache by token position across the existing
tensor-parallel group. TP8/DCP8 uses eight GPUs in total. It can free cache
capacity for more concurrent long-context requests, but adds communication.
Cache capacity and output throughput are separate measurements.

## What changed upstream

- [vLLM #51705](https://github.com/vllm-project/vllm/pull/51705) merged on
  2026-08-31 and is included in
  [v0.29.0](https://github.com/vllm-project/vllm/releases/tag/v0.29.0), not
  v0.28.0. It adds the ROCm AITER MLA DCP integration, including causal
  multi-token verification. The v0.29.0 backend declares
  `can_return_lse_for_decode = True` and obtains decode LSE through AITER's
  native entry point. An older wrapper that drops LSE is no longer a reason to
  describe **all** AITER MLA builds as unable to run DCP.
- A matching AITER build is still necessary. The backend explicitly rejects
  an AITER decode call that returns no LSE. Use the complete pinned image;
  upgrading only the Python `vllm` package inside the old DSpark container is
  not a validated upgrade procedure.
- With DCP plus speculative decoding, the multi-token verification route can
  use AITER's segmented Triton kernel even when the selected attention backend
  is named `ROCM_AITER_MLA`.
  [vLLM #56861](https://github.com/vllm-project/vllm/pull/56861) adds an optimized
  ASM route and remains **open** as of this date. Its described route has
  FP8-KV and verification-shape constraints; merging it would not by itself
  validate every BF16/auto or DSpark configuration.

Therefore, basic DCP enablement is available now, while optimized speculative
verification and our external KV-transfer integration need separate validation.
See the [versioned backend source](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/attention/backends/mla/rocm_aiter_mla.py)
and [DCP explanation](https://docs.vllm.ai/en/v0.29.0/serving/context_parallel_deployment/).

## Pinned single-node configuration

Use one otherwise idle node with **8 × MI355X (gfx950)**. Start a container
through your normal customer deployment workflow with all eight GPUs exposed
and sufficient model/cache storage. The command below runs **inside** it.
Review the [model license](../THIRD_PARTY_NOTICES.md) before downloading.

```text
vllm/vllm-openai-rocm:v0.29.0@sha256:e5e47f6aaab675c252c381f0dac237b31b10d87bb74d092b07fb4065efd7f5a1
```

This is the digest AMD supplied under `latest`; the registry's `v0.29.0` tag
was checked to resolve to the same digest. Pin it rather than using a moving
`latest` tag. Keep the AITER/ROCm dependencies supplied with this image.
The model is the full MXFP4 `moonshotai/Kimi-K3` checkpoint at revision
`f831ab66814297da540d832a5235f8e904f29d06`.

```bash
export VLLM_ROCM_USE_AITER=1
export VLLM_ROCM_USE_AITER_MLA=1
export VLLM_ROCM_USE_AITER_FP4BMM=1
export AITER_SITUV2_A8W4=1
export VLLM_ROCM_USE_AITER_MOE_SITUV2_A8W4=1
export AITER_BF16_FP8_MOE_BOUND=0
export SAFETENSORS_FAST_GPU=1

# Use 1 for the comparison baseline, then restart with 8 for the DCP arm.
export DCP_SIZE=8

vllm serve moonshotai/Kimi-K3 \
  --revision f831ab66814297da540d832a5235f8e904f29d06 \
  --served-model-name kimi-k3 \
  --trust-remote-code \
  --tensor-parallel-size 8 \
  --decode-context-parallel-size "$DCP_SIZE" \
  --distributed-executor-backend mp \
  --max-model-len 32768 \
  --max-num-batched-tokens 4096 \
  --max-num-seqs 128 \
  --gpu-memory-utilization 0.95 \
  --kv-cache-dtype auto \
  --mm-encoder-tp-mode data \
  --enable-auto-tool-choice \
  --tool-call-parser kimi_k3 \
  --reasoning-parser kimi_k3 \
  --port 8000
```

**Keep both SiTU variables enabled together:** `AITER_SITUV2_A8W4=1` and
`VLLM_ROCM_USE_AITER_MOE_SITUV2_A8W4=1`. AMD warned that enabling only one in
this environment can silently produce incorrect K3 output. Readiness and
HTTP 200 alone are not correctness checks.

This launch deliberately has no speculative configuration, draft model,
Infera overlay, or external KV connector. It selects AITER MLA via the supplied
environment; confirm `ROCM_AITER_MLA` in the actual engine logs. Preserve logs
showing vLLM/AITER versions, TP/DCP sizes, cache dtype, model revision, and
reported KV capacity for each arm. For a later speculative run, also inspect
the actual verification kernel route: the backend name alone is insufficient.

## What AMD measured

These are AMD's reported results from the engineering discussion, not new
benchmark results produced by this repository. The test used short inputs,
128 output tokens, a 32,768-token maximum model length, and BF16/auto KV cache.
The exact input lengths, request count, and raw latency distributions were
not supplied, so these numbers are not a reproducible performance claim yet.

| Measurement | DCP1 | DCP8 | Change |
|---|---:|---:|---:|
| Output tok/s, concurrency 1 | 56.6 | 52.6 | −7.1% |
| Output tok/s, concurrency 8 | 371 | 338 | −8.9% |
| Reported KV capacity, tokens | 2.23M | 8.60M | 3.86× |

AMD described the capacity gain as roughly **3.85×**; the table's 3.86× is
calculated from the rounded token counts. DCP8 does not guarantee eight times
usable KV capacity: K3 also has hybrid state and other allocations that do not
all shrink with the attention cache. The reported short-context throughput
regression is consistent with added communication; it does not establish the
effect on the customer's long-context workload. A GB300 speedup is a reason
to measure that workload on MI355X, not a transferable throughput guarantee.

## Controlled retest

1. On the same node and pinned image, compare DCP1 against DCP8 with every
   other setting fixed, including model revision, KV dtype, context cap,
   batch limits, and the seven environment variables above. Warm compilation
   and representative shapes on both arms before measuring steady state.
2. First check answer correctness with scored known-answer prompts and the
   same evaluation set on both arms. Exercise reasoning and tool-call parsing.
   Treat corrupt answers, ROCm resource/memory faults, or worker restarts as a
   failed arm even if some requests finish quickly.
3. Reproduce the short-context control at concurrency 1 and 8 with the same
   input/output lengths. Obtain the customer's exact input sequence lengths,
   output sequence lengths, concurrency/arrival pattern, and prefix reuse
   before testing the intended long-context benefit.
4. The 32K command cannot serve a 64K input fixture. Raise the context limit
   on **both** arms and recheck startup memory and correctness for that separate
   experiment; leave room for output tokens. Do not copy the 0.95 memory
   utilization setting into a draft-loaded PD deployment.
5. Use identical recorded requests, one outstanding turn per session, matched
   cold/warm prefix conditions, and complete draining between load steps.
   Repeat and alternate arm order. Record completed output tokens divided by
   time through the final completion, TTFT/inter-token latency, errors, queue
   delay, preemptions, restarts, and actual cache capacity. Report logical input
   throughput separately because it can include cached tokens.

The existing [controlled replay guide](09-controlled-rerun.md) supplies the
session-pacing and receipt methodology, but `controlled_replay.py run` requires
exactly four prefill and two decode workers behind Infera. It is **not** a
single-node benchmark driver. Its input-heavy fixture also includes 32K–64K
inputs, which exceed this initial server's context cap. Use a suitable
single-endpoint harness or adapt the fixture/driver explicitly; do not present
a failed topology or context-limit check as a DCP result.

## Upgrading the multi-node PD + DSpark runbook

The current manifests pin the custom
`johnqin2025/kimi-k3-dspark:1.1.0-mi355x-rocm7.2.3-20260802` image; the team
reports that stack is based on vLLM 0.28. The published tags checked on
2026-09-16 did not identify a replacement based on 0.29.0. The upstream image
above is not a validated replacement for that patched runtime.

Before changing the PD image or adding DCP to its launch arguments, obtain:

- An immutable replacement DSpark image digest, its vLLM/AITER/Mooncake
  revisions, and the compatible Infera overlay/operator versions. Record any
  backports, including the status and applicability of #56861.
- AMD's tested prefill/decode DCP sizes, cache dtype and block layout, hybrid
  state handling, speculative target/draft configuration, and transfer settings.
  The prefill/decode KV layout must agree with the connector; enabling DCP on
  one role by assumption is not sufficient.
- Correctness and GPU-direct handoff receipts on the upgraded pair, followed
  by a controlled 4-prefill/2-decode run on the customer workload. Recheck
  tokenizer loading, KV events, routing, restart behavior, and the
  [PD verification gates](04-verification.md), retaining the current pins for
  rollback.

Basic AITER DCP support no longer needs to be implemented from scratch. The
remaining work is obtaining and validating the complete PD/DSpark build, plus
measuring the performance of its selected kernels. This guide does not change
the existing serving-image pins or enable DCP on the deployed PD recipe.
