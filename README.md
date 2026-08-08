# minimax-h3-runpod

MiniMax H3 worker for RunPod Serverless (ComfyUI + native H3 nodes).

Builds the Docker image used by the `minimax-h3` serverless endpoint:
- ComfyUI updated to native MiniMax H3 support
- H3 models baked into the image (FL2VA int8 + Qwen3-VL nvfp4 + VAEs)
- Custom `handler.py` that returns VIDEO outputs (stock handler drops them)

The endpoint client (`send_h3.py` / `send_h3.ps1`) lives in the Dynasty workspace
(`scripts/minimax-h3-runpod/`).

Region note: the MiniMax H3 Community License forbids running the weights in
US/EU/UK/KR. The endpoint must use a data center outside those regions.
