# Several servers, and ARM64 / DGX Spark

Applies to Sarness Engine 0.4.x (API 1.1) and the multi-server desktop. Single-server deployment is described in
[DEPLOYMENT.md](DEPLOYMENT.md); model measurements are in [MODELS.md](MODELS.md).

## 1. Model

The desktop connects to every saved server profile at once. Each engine is independent: it has its own models,
job queue, files, generations and conversations, and nothing is shared between engines. This is not distributed
inference: a request runs entirely on one server. The coding agent's tools always run on the desktop; only its
model calls go to a server.

| Concept | Where it lives |
|---|---|
| Profiles (name, transport, address, preferred workloads) | desktop, `~/.config/sarness/profiles.json` |
| Primary server | desktop; serves un-prefixed requests, projects and folders |
| Routing preferences (per model, fallback policy) | desktop local storage, `sarness.routing.v1` |
| Observed speed per server and model | desktop local storage, `sarness.routing.perf.v1` |
| Sessions (conversation, server, routing choice, uploaded inputs per server) | desktop local storage, `sarness.sessions.v2` |
| Jobs, generations, uploads, conversations | the engine that ran or received them |

The native process proxies each engine at `/e/<profile id>/v1/...` and its event stream at `/e/<profile id>/v1/events`;
events carry the profile id. Engines without `/v1/capabilities` (before 0.4.0) are used with a capabilities document
derived from `/v1/system`.

## 2. Routing

For each request the desktop picks a server in this order. A model is never replaced by a different one.

1. **Manual choice.** A server chosen in the conversation's Server field is used exactly; if it is offline or lacks
   the model the request fails with that reason.
2. **Compatibility.** The server must have the model installed and supported.
3. **Availability.** The server must be online.
4. **Preference.** A per-model preferred server, then servers marked as preferred for the workload (chat, code,
   vision, image, video). With the fallback policy *Wait for the preferred server*, other servers are not used.
   A server whose image/video queue has reached its soft limit loses its preference, so new work starts elsewhere
   instead of waiting behind it.
5. **Free compute.** A server with the model loaded, then one where it fits in free memory (unified memory counts
   available system RAM).
6. **Queue.** Fewer running and queued jobs.
7. **Performance.** Lower observed seconds per job for that model, once both servers have been measured.
8. **Fallback.** The primary server, then name order.

The chosen server and the reason are shown under the Server field. Once a server accepts a job, the job stays there;
a job is never migrated, and retries run on the same server. Job submission carries a client key on engines that
advertise `job_client_key`, so a submission retried after a network interruption is not run twice.

Conversations stay on the server that holds them while that server has the selected model. Otherwise the conversation
is copied to the routed server (export → import) and continues there; the original is kept. Uploaded inputs are copied
to the server that runs the work.

## 3. ARM64 servers (NVIDIA DGX Spark, GB10)

| Item | Value on the reference Spark |
|---|---|
| CPU / GPU | 20-core ARM64 (aarch64), NVIDIA GB10, compute capability 12.1 |
| Memory | 128 GB unified (CPU and GPU share system RAM; ~121 GB usable) |
| Driver / CUDA | 580, CUDA 13.0 (not modified by Sarness) |
| Python stack | Python 3.12, PyTorch 2.14.1+cu130 (official aarch64 wheel), diffusers 0.41, transformers 5.20 |
| llama.cpp | built from source for `121a-real` (CUDA 13) by the runtime installer; no prebuilt ARM64 CUDA release exists |

Behaviour specific to unified memory:

* **Memory accounting.** Free memory is `MemAvailable`, not `cudaMemGetInfo` (which ignores reclaimable page cache).
  Pipelines always stay fully resident; CPU offload saves nothing there and only slows generation.
* **Loading.** Weights are read in one pass instead of memory-mapped: copying mmap'd files into GPU memory faults
  them in page by page on one core. Diffusers loads use `disable_mmap` (text encoders are loaded separately because
  diffusers does not forward it to transformers models); llama-server gets `--load-mode dio` (or `none` / `--no-mmap`
  on builds that only have those). Measured: a 14 GB Qwen-Image transformer 79 s → 7 s; gpt-oss-120b 31–66 s → 9 s.
* **Eviction.** Cached models are evicted before a load, and evicted weights are dropped rather than copied to the CPU
  (which would land in the same RAM).
* **Large video models.** Wan 2.2 A14B stores each of its two 57 GB experts in FP8 after loading (computed in bf16).
  MiniMax H3 uses a pre-quantized FP8 transformer and an FP8, 50-layer text encoder (see MODELS.md).

Install on ARM64 exactly as on x86-64 (`install-engine.sh`); the script selects the CUDA wheel index for the
detected driver and builds llama.cpp from source when no prebuilt runtime matches.

## 4. Operations

* **Adding a server.** Settings → Servers → Add server. Each card shows hardware, memory (unified or VRAM + RAM), installed
  models, current workload, engine version and server identity, and has Connect, Disconnect and Make primary.
* **Offline servers.** Their work stays listed with a server label; new requests route elsewhere when another server
  has the model. Reconnection is automatic.
* **Downloads** continue on their own after an engine restart, oldest first; paused downloads stay paused. A model
  is not listed as installed until its download has finished.
