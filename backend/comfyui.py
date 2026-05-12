import os
import json
import uuid
import asyncio
import httpx
import websockets

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
CLIENT_ID = str(uuid.uuid4())

async def is_online() -> bool:
    """Check if ComfyUI is reachable."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{COMFYUI_URL}/system_stats")
            return r.status_code == 200
    except Exception:
        return False

async def upload_image(file_bytes: bytes, filename: str) -> str:
    """Upload an image to ComfyUI. Returns the stored filename."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{COMFYUI_URL}/upload/image",
            files={"image": (filename, file_bytes, "image/png")},
            data={"overwrite": "true"},
        )
        r.raise_for_status()
        return r.json()["name"]

async def submit_workflow(workflow: dict) -> str:
    """Submit a workflow to ComfyUI. Returns prompt_id."""
    payload = {"prompt": workflow, "client_id": CLIENT_ID}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{COMFYUI_URL}/prompt", json=payload)
        r.raise_for_status()
        return r.json()["prompt_id"]

async def get_history(prompt_id: str) -> dict:
    """Get execution history for a prompt."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
        r.raise_for_status()
        return r.json().get(prompt_id, {})

async def download_output(filename: str, subfolder: str = "", filetype: str = "output") -> bytes:
    """Download an output file from ComfyUI."""
    params = {"filename": filename, "subfolder": subfolder, "type": filetype}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{COMFYUI_URL}/view", params=params)
        r.raise_for_status()
        return r.content

async def listen_progress(prompt_id: str, on_progress, timeout: float = 600):
    """Listen to WebSocket for progress updates on a specific prompt.
    on_progress(stage, step, total_steps) is called on each update.
    Returns when the prompt completes or fails.
    Raises RuntimeError on execution error or timeout (default 10 min)."""
    ws_url = f"ws://{COMFYUI_URL.replace('http://', '')}/ws?clientId={CLIENT_ID}"
    async with websockets.connect(ws_url) as ws:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            msg_type = msg.get("type")
            data = msg.get("data", {})

            if msg_type == "progress" and data.get("prompt_id") == prompt_id:
                step = data.get("value", 0)
                total = data.get("max", 1)
                await on_progress("generating 3d model", step, total)

            elif msg_type == "executing" and data.get("prompt_id") == prompt_id:
                if data.get("node") is None:
                    return

            elif msg_type == "execution_error" and data.get("prompt_id") == prompt_id:
                error_msg = data.get("exception_message", "unknown error")
                raise RuntimeError(f"ComfyUI error: {error_msg}")
