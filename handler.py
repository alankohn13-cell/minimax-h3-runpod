"""
Custom worker-comfyui handler that ALSO returns VIDEO outputs.

Why this file exists:
  The stock runpod/worker-comfyui handler only collects node_output["images"]
  and logs "unhandled output keys: ['videos']" for MiniMax H3's video files, so
  a workflow producing an mp4 returns nothing useful. This handler keeps the
  stock behaviour for images and additionally collects every file-like output
  key (videos, gifs, audio) from history and base64-encodes them.

Deployment:
  COPY handler.py /handler.py  in the Dockerfile (the base image entrypoint
  runs /handler.py).
"""

import base64
import json
import logging
import os
import socket
import time
import traceback
import urllib.parse
import uuid

import requests
import runpod
import websocket

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1:8188")
COMFY_API_AVAILABLE_INTERVAL_MS = int(
    os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", 50)
)
COMFY_API_AVAILABLE_MAX_RETRIES = int(
    os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", 0)
)
COMFY_API_FALLBACK_MAX_RETRIES = 500
COMFY_PID_FILE = "/tmp/comfyui.pid"

WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"

# File-like history output keys we collect and return (base64).
# "videos" = native SaveVideo/CreateVideo output, "gifs" = legacy animated
# output, "audio" = decoded audio files.
OUTPUT_FILE_KEYS = ("images", "videos", "gifs", "audio")


def _comfy_server_status():
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {"reachable": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _get_comfyui_pid():
    try:
        with open(COMFY_PID_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_comfyui_process_alive():
    pid = _get_comfyui_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def check_server(url, retries=0, delay=50):
    print(f"worker-comfyui - Checking API server at {url}...")
    delay = max(1, delay)
    attempt = 0
    while True:
        process_status = _is_comfyui_process_alive()
        if process_status is False:
            print("worker-comfyui - ComfyUI process has exited. Server will not become reachable.")
            return False
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print("worker-comfyui - API is reachable")
                return True
        except (requests.Timeout, requests.RequestException):
            pass
        attempt += 1
        fallback = retries if retries > 0 else COMFY_API_FALLBACK_MAX_RETRIES
        if process_status is None and attempt >= fallback:
            print(f"worker-comfyui - Failed to connect to server after {fallback} attempts.")
            return False
        time.sleep(delay / 1000)


def upload_images(images):
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}
    responses, upload_errors = [], []
    print(f"worker-comfyui - Uploading {len(images)} image(s)...")
    for image in images:
        try:
            name = image["name"]
            image_data_uri = image["image"]
            if "," in image_data_uri:
                base64_data = image_data_uri.split(",", 1)[1]
            else:
                base64_data = image_data_uri
            blob = base64.b64decode(base64_data)
            files = {"image": (name, blob, "image/png"), "overwrite": (None, "true")}
            response = requests.post(f"http://{COMFY_HOST}/upload/image", files=files, timeout=30)
            response.raise_for_status()
            responses.append(f"Successfully uploaded {name}")
        except Exception as e:
            upload_errors.append(f"Error uploading {image.get('name', 'unknown')}: {e}")
    if upload_errors:
        return {"status": "error", "message": "Some images failed to upload", "details": upload_errors}
    return {"status": "success", "message": "All images uploaded successfully", "details": responses}


def queue_workflow(workflow, client_id):
    payload = {"prompt": workflow, "client_id": client_id}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    response = requests.post(f"http://{COMFY_HOST}/prompt", data=data, headers=headers, timeout=30)
    if response.status_code == 400:
        error_data = response.json()
        node_errors = error_data.get("node_errors", {})
        parts = [f"Node {nid} ({k}): {v}" for nid, errs in node_errors.items() for k, v in errs.items()]
        msg = "Workflow validation failed:\n" + "\n".join(parts) if parts else response.text
        raise ValueError(msg)
    response.raise_for_status()
    return response.json()


def get_history(prompt_id):
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def get_output_file(filename, subfolder, image_type):
    params = urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder, "type": image_type}
    )
    try:
        response = requests.get(f"http://{COMFY_HOST}/view?{params}", timeout=120)
        response.raise_for_status()
        return response.content
    except Exception as e:
        print(f"worker-comfyui - Error fetching {filename}: {e}")
        return None


def _attempt_websocket_reconnect(ws_url, max_attempts, delay_s, initial_error):
    print(f"worker-comfyui - Websocket closed: {initial_error}. Reconnecting...")
    last_error = initial_error
    for attempt in range(max_attempts):
        status = _comfy_server_status()
        if not status["reachable"]:
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            print("worker-comfyui - Websocket reconnected successfully.")
            return new_ws
        except (websocket.WebSocketException, ConnectionRefusedError, socket.timeout, OSError) as err:
            last_error = err
            if attempt < max_attempts - 1:
                time.sleep(delay_s)
    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_error}"
    )


def validate_input(job_input):
    if job_input is None:
        return None, "Please provide input"
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"
    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"
    images = job_input.get("images")
    if images is not None and not (
        isinstance(images, list) and all("name" in i and "image" in i for i in images)
    ):
        return None, "'images' must be a list of objects with 'name' and 'image' keys"
    return {"workflow": workflow, "images": images}, None


def handler(job):
    job_input = job["input"]
    job_id = job["id"]

    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    workflow = validated_data["workflow"]
    input_images = validated_data.get("images")

    if not check_server(
        f"http://{COMFY_HOST}/",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return {"error": f"ComfyUI server ({COMFY_HOST}) not reachable after multiple retries."}

    if input_images:
        upload_result = upload_images(input_images)
        if upload_result["status"] == "error":
            return {"error": "Failed to upload one or more input images", "details": upload_result["details"]}

    ws = None
    client_id = str(uuid.uuid4())
    prompt_id = None
    output_data = []
    errors = []

    try:
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)

        try:
            queued_workflow = queue_workflow(workflow, client_id)
            prompt_id = queued_workflow.get("prompt_id")
            if not prompt_id:
                raise ValueError(f"Missing 'prompt_id' in queue response: {queued_workflow}")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Error queuing workflow: {e}")

        print(f"worker-comfyui - Waiting for workflow execution ({prompt_id})...")
        execution_done = False
        while True:
            try:
                out = ws.recv()
                if not isinstance(out, str):
                    continue
                message = json.loads(out)
                msg_type = message.get("type")
                if msg_type == "executing":
                    data = message.get("data", {})
                    if data.get("node") is None and data.get("prompt_id") == prompt_id:
                        print(f"worker-comfyui - Execution finished for prompt {prompt_id}")
                        execution_done = True
                        break
                elif msg_type == "execution_error":
                    data = message.get("data", {})
                    if data.get("prompt_id") == prompt_id:
                        errors.append(
                            f"Workflow execution error: node={data.get('node_id')} "
                            f"type={data.get('node_type')} msg={data.get('exception_message')}"
                        )
                        break
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException as err:
                ws = _attempt_websocket_reconnect(
                    ws_url, WEBSOCKET_RECONNECT_ATTEMPTS, WEBSOCKET_RECONNECT_DELAY_S, err
                )
                continue
            except json.JSONDecodeError:
                continue

        history = get_history(prompt_id)
        if prompt_id not in history:
            return {"error": f"Prompt ID {prompt_id} not found in history.", "details": errors or None}

        outputs = history.get(prompt_id, {}).get("outputs", {})
        if not outputs:
            errors.append(f"No outputs found in history for prompt {prompt_id}.")

        for node_id, node_output in outputs.items():
            for key in OUTPUT_FILE_KEYS:
                files = node_output.get(key)
                if not files:
                    continue
                for info in files:
                    filename = info.get("filename")
                    subfolder = info.get("subfolder", "")
                    file_type = info.get("type")
                    if file_type == "temp" or not filename:
                        continue
                    data = get_output_file(filename, subfolder, file_type)
                    if not data:
                        errors.append(f"Failed to fetch {filename} from /view endpoint.")
                        continue
                    ext = os.path.splitext(filename)[1] or (".mp4" if key in ("videos", "gifs") else ".bin")
                    if os.environ.get("BUCKET_ENDPOINT_URL"):
                        import tempfile
                        from runpod.serverless.utils import rp_upload
                        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                            tmp.write(data)
                            tmp_path = tmp.name
                        try:
                            url = rp_upload.upload_image(job_id, tmp_path)
                            output_data.append({"filename": filename, "type": "s3_url", "data": url})
                        finally:
                            os.remove(tmp_path)
                    else:
                        b64 = base64.b64encode(data).decode("utf-8")
                        kind = "videos" if key in ("videos", "gifs") else ("audio" if key == "audio" else "images")
                        output_data.append({"filename": filename, "type": "base64", "data": b64, "kind": kind})
                    print(f"worker-comfyui - Collected {key} file: {filename}")

    except websocket.WebSocketException as e:
        print(traceback.format_exc())
        return {"error": f"WebSocket communication error: {e}"}
    except requests.RequestException as e:
        print(traceback.format_exc())
        return {"error": f"HTTP communication error with ComfyUI: {e}"}
    except ValueError as e:
        print(traceback.format_exc())
        return {"error": str(e)}
    except Exception as e:
        print(traceback.format_exc())
        return {"error": f"An unexpected error occurred: {e}"}
    finally:
        if ws and ws.connected:
            ws.close()

    if not output_data and errors:
        return {"error": "Job processing failed", "details": errors}
    if not output_data:
        return {"error": "Job completed but produced no output files."}

    videos = [o for o in output_data if o.get("kind") == "videos"]
    audio = [o for o in output_data if o.get("kind") == "audio"]
    images = [o for o in output_data if o.get("kind") == "images"]
    result = {}
    if videos:
        result["videos"] = videos
    if audio:
        result["audio"] = audio
    if images:
        result["images"] = images
    if errors:
        result["errors"] = errors
    print(f"worker-comfyui - Job completed: {len(videos)} video(s), {len(images)} image(s).")
    return result


print("worker-comfyui - Starting handler...")
runpod.serverless.start({"handler": handler})
