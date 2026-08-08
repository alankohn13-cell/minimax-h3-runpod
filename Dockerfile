# MiniMax H3 worker for RunPod Serverless
#
# Base: runpod/worker-comfyui (AGPL-3.0) - ComfyUI as a serverless API.
# This Dockerfile adds:
#   1. ComfyUI >= 0.30.0 (native MiniMax H3 support - merged 2026-08-03)
#   2. MiniMax H3 models baked into the image (no network volume, $0 storage)
#   3. A custom handler that returns VIDEO outputs (the stock handler only
#      returns images and drops video files).
#
# Build once on GitHub Actions; the 55GB-ish image is pulled by RunPod workers
# on cold start. See runpod-deploy.md for the full step-by-step.
#
# NOTE on region: the MiniMax H3 Community License forbids running the weights
# in US/EU/UK/KR. Pick a data center outside those (JP/IN/AU/CA/BR/etc.) when
# creating the endpoint.

# --- Base image (latest worker-comfyui release, clean ComfyUI, no models) ---
# comfy-cli and runpod serverless SDK are preinstalled by the base image.
FROM runpod/worker-comfyui:5.8.6-base

# --- Optional HuggingFace token for gated models (not needed for Comfy-Org) ---
# Build with:  docker build --build-arg HF_TOKEN=hf_xxx .
ARG HF_TOKEN=""

# ---------------------------------------------------------------------------
# 1. Update ComfyUI to native MiniMax H3 support (0.30.0+)
# ---------------------------------------------------------------------------
# worker-comfyui 5.8.6 (2026-06-17) ships a ComfyUI older than the Aug 3 H3
# merge. Pull the latest ComfyUI so the MiniMaxH3ImageToVideo / VAEDecodeAudio /
# CreateVideo nodes exist. If the base image ever ships 0.30.0+, this becomes a
# no-op (git pull returns already-up-to-date).
WORKDIR /comfyui
# Deterministic update to ComfyUI v0.31.1 (first stable tag with MiniMax H3
# support + the Aug 6 audio sampler fix). NOTE: the old
# `git checkout $(git rev-list --tags --max-count=1)` left the worktree in a
# detached HEAD state and the following `git pull origin main --ff-only` failed
# with exit 1 ("You are not currently on a branch"), breaking the whole build.
# `git checkout -B <branch> <tag>` always forces a local branch onto the tag
# (works from detached HEAD too). pip here resolves to /opt/venv/bin/pip
# (base image PATH), which is the venv start.sh launches ComfyUI with.
RUN git fetch origin tag v0.31.1 \
    && git checkout -B runpod v0.31.1 \
    && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# 2. MiniMax H3 models (baked in - no network volume)
# ---------------------------------------------------------------------------
# Paths match ComfyUI's model folders. All from the official Comfy-Org/MiniMax-H3
# repo (public, no token required). INT8/convrot pruned + NVFP4 text encoder =
# ~40GB total, fits a 24GB 4090 with layerwise offload into system RAM.
# Downloads are combined in a single RUN (fewer layers + parallel wget, safer
# against the builder's 30-minute docker build timeout). `xargs -P 4` runs the
# four wget downloads concurrently; the test below catches any failed download.
# (wget is what the base image ships - it has no curl.)
RUN mkdir -p /comfyui/models/diffusion_models /comfyui/models/text_encoders /comfyui/models/vae \
    && printf '%s\n' \
       "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors|/comfyui/models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors" \
       "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|/comfyui/models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" \
       "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors|/comfyui/models/vae/minimax_h3_video_vae_fp16.safetensors" \
       "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors|/comfyui/models/vae/minimax_h3_audio_vae_fp32.safetensors" \
    | xargs -P 4 -I{} sh -c 'u="${1%%|*}"; f="${1##*|}"; wget -q --tries=3 -O "$f" "$u"' _ {} \
    && test -f /comfyui/models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors \
    && test -f /comfyui/models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors \
    && test -f /comfyui/models/vae/minimax_h3_video_vae_fp16.safetensors \
    && test -f /comfyui/models/vae/minimax_h3_audio_vae_fp32.safetensors \
    && echo "H3 models verified"

# --- Ref2VA (identity/voice reference path) - PHASE 2, uncomment when needed ---
# RUN comfy model download \
#       --url https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors \
#       --relative-path models/diffusion_models \
#       --filename minimax_h3_ref2va_pruned_int8_convrot.safetensors

# --- Spectrum acceleration (OPTIONAL, disabled by default) ---
# Installs the node but the workflow keeps it BYPASSED. Community tests report
# ~30% faster but motion/eye/finger degradation (see REFERENCIA doc). Enable it
# in the workflow only after an exact-seed A/B pass passes.
# RUN git clone https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3.git /comfyui/custom_nodes/ComfyUI-Spectrum-MiniMax-H3 \
#     || true

# ---------------------------------------------------------------------------
# 3. Custom handler - returns VIDEO outputs (stock handler drops them)
# ---------------------------------------------------------------------------
# The stock worker-comfyui handler only collects node_output["images"] and logs
# "unhandled output keys: videos". This handler also fetches and base64-encodes
# video/audio/gif outputs so the RunPod /run result carries the mp4.
COPY handler.py /handler.py

# --- Worker env defaults (overridable at endpoint creation) ---
ENV RUNPOD_INIT_TIMEOUT=1200 \
    COMFY_LOG_LEVEL=INFO

WORKDIR /
