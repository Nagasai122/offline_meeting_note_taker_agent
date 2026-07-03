# Containerised deployment (Strand F, 2026-07 audit)

## What this gives you

- `llm` — llama.cpp server (CUDA), GPU passthrough, `/health` healthcheck.
- `app` — dashboard + transcription worker (CUDA for faster-whisper),
  healthcheck on `/api/briefing`, published on **127.0.0.1:8000 only**.
- `setup` (profile) — the one network-permitted step: downloads LLM weights
  and pre-fetches the Whisper model into a shared cache volume, then exits.

## Defence-in-depth zero-egress

The runtime services live on `agent-net` with `internal: true`: Docker
creates the bridge **without a default route**, so no process in either
container can reach anything off-box — independent of the application-level
guarantee (`trust_env=False`, loopback binds) verified in
`tests/security/test_zero_egress.py`.

Verify it yourself after `docker compose up`:

```
docker compose exec app python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)"
# must fail: no route to host / name resolution error
docker network inspect deploy_agent-net --format '{{.Internal}}'   # -> true
```

Health checks:

```
docker compose ps        # both services should report (healthy)
```

## Known limitations (stated plainly)

1. **Live audio capture is not containerised.** WASAPI loopback/microphone
   capture is host-hardware-bound; inside containers this stack serves the
   transcript-import workflow, transcription of uploaded audio, extraction,
   and the review/apply dashboard. Record on the host, or import transcripts.
2. **app↔llm wiring (resolved 2026-07-03):** the dashboard's outbound LLM
   probes/clients honour `settings.llm.host` (the compose file mounts
   `settings.container.toml` with `host = "llm"`), so the two-service
   layout works. The *listen* side is unchanged — `start_server` still
   refuses non-loopback binds; inside the llm container the bind is the
   container's own namespace on an internal-only network. One caveat: in
   container mode do not use the dashboard's "start LLM" System action
   (the llm service owns that process); the pipeline detects the running
   service and reuses it.
3. GPU passthrough requires Docker Desktop ≥ 4.19 with WSL2 backend on
   Windows, or nvidia-container-toolkit on Linux.
