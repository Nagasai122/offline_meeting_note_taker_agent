# meeting-agent web dashboard + transcription worker.
#
# CUDA runtime base so faster-whisper (CTranslate2) can use the GPU that is
# passed through via the compose `deploy.resources.reservations.devices`
# block. The image contains no model weights: weights are mounted read-only
# from the host (populated by the one-off `setup` service, the single
# network-permitted step).
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.11 python3-pip git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python

WORKDIR /app
COPY pyproject.toml ./
COPY agent ./agent
COPY audio_capture ./audio_capture
COPY cli ./cli
COPY concurrency ./concurrency
COPY config ./config
COPY llm ./llm
COPY mcp_server ./mcp_server
COPY transcribe ./transcribe
COPY scripts ./scripts
COPY static ./static

# Install with --no-deps resolution against the lockless pyproject; network
# is available ONLY at image build time. At runtime the containers run on an
# internal-only network (see docker-compose.yml) with no route out.
RUN pip install --no-cache-dir .

# Belt-and-braces: even if a dependency tries to phone home at runtime, the
# offline env vars are baked in (the compose network policy is the real wall).
ENV HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    VLLM_NO_USAGE_STATS=1

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/briefing', timeout=2)" || exit 1

CMD ["python", "-m", "cli.main", "web"]
