# ASR rescue sidecar: SenseVoice-small via sherpa-onnx behind the one-shot HTTP
# contract of services/agent/src/providers/sensevoice.py (P1-02).
#
# Rebuilds production memoria-sensevoice-asr:20260901-pin-language: the same
# base image layers (python:3.11-slim pinned by digest), the same package
# versions (hash-locked), and the same scripts/run_sensevoice_asr.py. The model
# is mounted read-only at run time, never baked in; see models.sha256 and
# docs/runbooks/sensevoice-asr.md. Build from the repository root:
#   docker build --platform linux/amd64 -f infra/sensevoice-asr/Dockerfile -t memoria-sensevoice-asr:<tag> .
FROM python:3.11-slim@sha256:1042b61448fef4ba92d16a8c7eb4996d027568ce64792a7877fd88511e0af7c6
WORKDIR /app
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
COPY infra/sensevoice-asr/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -i "$PIP_INDEX_URL" -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY scripts/run_sensevoice_asr.py /app/run_sensevoice_asr.py
ENV SENSEVOICE_MODEL_DIR=/models/sensevoice
EXPOSE 8001
CMD ["python", "run_sensevoice_asr.py"]
