# Third-party notices and model licensing

## Repository and model licenses

[LICENSE](LICENSE) covers AMP PBC's recipes, documentation, and benchmark code
under Apache-2.0. It does not grant rights to external models, serving engines,
container images, or other third-party software.

The recipes fetch two models from Hugging Face:

| Model | Publisher | Published model license |
|---|---|---|
| [Kimi-K3](https://huggingface.co/moonshotai/Kimi-K3) | Moonshot AI | [Kimi K3 License](https://huggingface.co/moonshotai/Kimi-K3/blob/f831ab66814297da540d832a5235f8e904f29d06/LICENSE) |
| [Kimi-K3-DSpark](https://huggingface.co/Inferact/Kimi-K3-DSpark) | Inferact | [Kimi K3 License](https://huggingface.co/Inferact/Kimi-K3-DSpark/blob/cf6b8244620e7ea4b0651d214f28e89eac75bed6/LICENSE) |

Both license files carry `Copyright (c) 2026 Moonshot AI` and were byte-identical
at these revisions when checked on 2026-09-09. The complete, unmodified text is
included in [licenses/Kimi-K3-LICENSE.txt](licenses/Kimi-K3-LICENSE.txt).
Its SHA-256 is
`20c797ce19af0c17de52c6afb144644768a591c521655f5ebf5712c9850f2887`.
The revision links record the audit sources; they do not pin the recipes'
downloads, which currently follow each model repository's default revision.
Retain and review the license supplied with the exact model revision you use.

## Copies and redistribution

Section 1 requires preservation of the copyright and permission notices in
copies or substantial portions of the covered software. The license's scope
includes weights, parameters, configuration, inference/training code, and
associated documentation. A model copy cannot be licensed solely under this
repository's Apache-2.0 license.

Keep the complete upstream `LICENSE` and any accompanying notices with each
model directory when staging, copying between nodes, creating images or
archives, or distributing models or derivatives. Keep the model card for
provenance. A link to the license, or a copy left only in this recipe checkout,
does not accompany a separately distributed model artifact.

The staging Job and worker init container use full `hf download` operations
without file filters, so new downloads include upstream license files. They
also check that both model directories have a nonempty `LICENSE`. A worker
with existing weights refuses to start if either license is missing or empty.
Restore the license from the **same model revision** that supplied those weights,
then retry. If that revision is unknown, establish its provenance before
redistribution; do not assume today's license describes an older checkpoint.

The aggregated router fetches tokenizer assets from the model repository at
runtime. If you package those assets separately, preserve the applicable model
license and notices in that package too. The PD router reads the staged model
directory. Check the same notice requirements when using a manually prepared
directory instead of the staging Job.

## Commercial use

The complete license controls; these are operational reminders, not a separate
grant of permission:

- **Section 2:** if the licensee or an affiliate operates a Model as a Service
  business and their aggregate revenue exceeds US$20 million over any consecutive
  12 months, a separate Moonshot AI agreement is required before commercial use
  of the model or derivatives. The threshold is aggregate licensee-and-affiliate
  revenue, not just revenue from Kimi K3. The section defines Model as a Service
  and excludes certain embedded end-user features and mere request relaying.
- **Section 3:** a commercial product or service using the model must prominently
  display `Kimi K3` in its UI if it exceeds 100 million monthly active users or
  US$20 million in monthly revenue.
- **Section 4:** sections 2 and 3 have exceptions for internal use as narrowly
  defined in the license and use through Moonshot AI's official products or
  certified inference partners. Self-hosting or listing on an inference
  marketplace does not by itself establish that an exception applies.

The repository cannot establish an operator's revenue, affiliate scope, user
counts, existing agreements, or certified-partner status. Have the responsible
business/legal owner verify those facts before asserting commercial compliance.
Moonshot AI lists `license@moonshot.ai` for licensing questions.

## Audit scope and findings

Reviewed on 2026-09-09 at repository commit
`633370ce146c6ea98274fcb8f99999cfd5356671`:

- All 15 tracked files and all reachable Git history (one commit), remote
  branches, tags, and GitHub releases. No model weights, model configuration or
  tokenizer payloads, LFS pointers, submodules, or release assets were present.
  The largest tracked file was a 24,023-byte serving manifest.
- Model acquisition in `k8s/weights-stage-job.yaml` and
  `k8s/aggregated/kimi-k3-aggregated.yaml`; local model mounts in the PD manifest;
  and the router's Hugging Face tokenizer reference.
- The two upstream model repositories and their complete license files at the
  revisions linked above. Both published `LICENSE` and `README.md`; neither
  listed a separate notice file at those revisions.

The audited repository distributed serving recipes, not model weights. No
missing license bundled with repository-hosted weights was found. The gaps were
an unexplained Apache/model licensing boundary, no local model license or
commercial-use guidance, and no notice check when reusing pre-staged weights.
This change adds the notices and checks without replacing the recipe license.

This audit does not certify an operating inference service, separately hosted
weights, private agreements, or the contents and licensing of the externally
referenced AMD/Infera, vLLM, and Mooncake images. Those artifacts retain their
respective terms and must be reviewed if repackaged or redistributed.
