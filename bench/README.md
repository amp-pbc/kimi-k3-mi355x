# orbench — open-loop load test for a Kimi-K3 endpoint

`orbench.py` offers a Kimi-K3 chat-completions endpoint a target request *rate*
(Poisson arrivals, open loop) and reports the numbers a router actually displays
and routes on: median per-request displayed tok/s, TTFT percentiles, completion
rate, and 429 count. Sweep the rate to find the knee.

See [`../docs/05-benchmarking.md`](../docs/05-benchmarking.md) for the
methodology and how to read the results.

## Install

```bash
pip install -r requirements.txt
```

Only dependency beyond the Python standard library is `httpx`.

## Configure

All configuration is via environment variables:

| Var             | Default                                       | Meaning |
|-----------------|-----------------------------------------------|---------|
| `ORBENCH_BASE`  | `http://localhost:8000/v1/chat/completions`   | Full chat-completions URL of the endpoint under test. |
| `API_KEY`       | `""` (empty = no auth)                         | Bearer token. Leave empty for engines that require no auth. |
| `ORBENCH_MODEL` | `moonshotai/kimi-k3`                           | Model id sent in the request body. May differ between a gateway (public id) and a raw engine (`--served-model-name`). |

```bash
export ORBENCH_BASE="http://<endpoint>/v1/chat/completions"
export API_KEY="sk-..."         # omit if the endpoint does no auth
export ORBENCH_MODEL="moonshotai/kimi-k3"
```

## Run

```bash
python orbench.py --rates 0.5,1,1.5,2,3 --secs 120 --csv run.csv
```

Flags:

- `--rates`  comma-separated offered request rates (req/s) to sweep.
- `--secs`   measured seconds per rate.
- `--warm`   warm-up seconds at the first rate, discarded (default 120).
- `--drain`  seconds to wait for in-flight requests to finish after each rate (default 180).
- `--csv`    per-request output CSV (default `./orbench.csv`).

Output: a live progress line per 30s, a summary line per rate, a
rate-vs-displayed-stats table, and a per-slice breakdown. One row per request is
written to the CSV for offline analysis.

## Run it from INSIDE the cluster

For real numbers, run orbench from **inside the cluster** — e.g. as a Kubernetes
`Job` in the same namespace as the endpoint — so it hits the endpoint over the
in-cluster network. Do **not** drive a real benchmark through `kubectl
port-forward`: the port-forward tunnel is a single localhost hop with its own
buffering and connection limits, and it will cap throughput and distort TTFT
long before the endpoint does. Port-forward is fine for a quick smoke test only.
