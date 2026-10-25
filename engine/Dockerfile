# Build from the repo root: docker build -f engine/Dockerfile .
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install -U "pip>=24"

WORKDIR /app

# CPU-only torch, without the nvidia libraries
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
# torchaudio is needed by FunASR (SenseVoice); same CPU index so it matches torch
RUN pip install torchaudio --index-url https://download.pytorch.org/whl/cpu

COPY engine/requirements.txt /app/
RUN pip install -r requirements.txt && rm requirements.txt

COPY WhisperLive/whisper_live /app/whisper_live
COPY WhisperLive/run_server.py /app/

RUN useradd --create-home app
USER app

EXPOSE 9090 8000 9091

# Settings come from the compose file; models are mounted read-only at /models.
CMD ["python", "run_server.py", "--enable_rest", "--rest_port", "8000"]
