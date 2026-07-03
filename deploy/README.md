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
2. **app↔llm service wiring needs one code change before the two-service
   layout is fully live.** `cli/web.py`'s pipeline probes and
   `llm/server_manager.start_server` currently assume a co-located LLM on
   `127.0.0.1` (`start_server` deliberately raises `UnsafeBindAddressError`
   for anything non-loopback — correct for bare metal, blocking for the
   compose service name `llm`). Until the maintainer signs off on honouring
   `settings.llm.host` for *outbound client connections only* (the listen
   bind must stay loopback/internal), run single-host mode: `docker compose
   up llm` + bare-metal `meeting-agent web` pointed at `127.0.0.1:8080`,
   which works today with zero code changes.
3. GPU passthrough requires Docker Desktop ≥ 4.19 with WSL2 backend on
   Windows, or nvidia-container-toolkit on Linux.
