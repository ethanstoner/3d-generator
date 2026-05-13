import os
import json
import uuid
import asyncio
import httpx
import websockets

CLIENT_ID = str(uuid.uuid4())

def get_comfyui_url():
    return os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")

def get_output_dir():
    return os.getenv("COMFYUI_OUTPUT_DIR", "")

async def is_online() -> bool:
    """Check if ComfyUI is reachable."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{get_comfyui_url()}/system_stats")
            return r.status_code == 200
    except Exception:
        return False

async def upload_image(file_bytes: bytes, filename: str) -> str:
    """Upload an image to ComfyUI. Returns the stored filename."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{get_comfyui_url()}/upload/image",
            files={"image": (filename, file_bytes, "image/png")},
            data={"overwrite": "true"},
        )
        r.raise_for_status()
        return r.json()["name"]

async def submit_workflow(workflow: dict) -> str:
    """Submit a workflow to ComfyUI. Returns prompt_id."""
    payload = {"prompt": workflow, "client_id": CLIENT_ID}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{get_comfyui_url()}/prompt", json=payload)
        r.raise_for_status()
        return r.json()["prompt_id"]

async def get_history(prompt_id: str) -> dict:
    """Get execution history for a prompt."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{get_comfyui_url()}/history/{prompt_id}")
        r.raise_for_status()
        return r.json().get(prompt_id, {})

async def download_output(filename: str, subfolder: str = "", filetype: str = "output") -> bytes:
    """Download an output file from ComfyUI."""
    params = {"filename": filename, "subfolder": subfolder, "type": filetype}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{get_comfyui_url()}/view", params=params)
        r.raise_for_status()
        return r.content

def list_output_files(prefix: str, ext: str) -> list[str]:
    """List output files matching prefix*.ext from ComfyUI output directory.
    Uses filesystem if COMFYUI_OUTPUT_DIR is set, otherwise returns empty."""
    output_dir_path = get_output_dir()
    if not output_dir_path:
        return []
    from pathlib import Path
    output_dir = Path(output_dir_path)
    if not output_dir.exists():
        return []
    return sorted([f.name for f in output_dir.glob(f"{prefix}*.{ext}")])

# Map workflow node IDs to human-readable stage descriptions
NODE_STAGES = {
    "14": "loading image",
    "58": "removing background",
    "37": "generating mesh (diffusion sampling)",
    "4":  "loading VAE",
    "9":  "decoding mesh (volume decoding)",
    "43": "post-processing mesh (removing floaters, smoothing)",
    "44": "exporting untextured model",
    "45": "UV unwrapping mesh",
    "19": "configuring camera views",
    "20": "generating textures (multi-view)",
    "21": "baking textures",
    "49": "inpainting texture gaps",
    "55": "saving texture image",
    "30": "configuring parameters",
}

# Pipeline execution order with weight (% of total time).
# Nodes with step-by-step progress get larger weights.
PIPELINE_ORDER = [
    ("14", 1),   # load image
    ("58", 3),   # remove background
    ("37", 30),  # mesh generation (30 diffusion steps)
    ("4",  1),   # load VAE
    ("9",  15),  # VAE decode (volume decoding)
    ("43", 5),   # postprocess mesh
    ("44", 2),   # export untextured
    ("45", 3),   # UV unwrap
    ("30", 1),   # configure params
    ("19", 1),   # camera config
    ("20", 20),  # multi-view texture gen (15 steps)
    ("21", 10),  # bake textures
    ("49", 6),   # inpaint
    ("55", 2),   # save image
]

# Build cumulative progress lookup: node_id -> (start%, end%)
_cumulative = 0
NODE_PROGRESS_RANGE: dict[str, tuple[float, float]] = {}
for _nid, _weight in PIPELINE_ORDER:
    NODE_PROGRESS_RANGE[_nid] = (_cumulative / 100, (_cumulative + _weight) / 100)
    _cumulative += _weight

async def submit_and_listen(workflow: dict, on_progress, timeout: float = 600):
    """Open WebSocket FIRST, then submit workflow, then listen for progress.
    This ensures we don't miss any progress events.
    on_progress(stage, step, total_steps, overall_progress) is called on each update.
    Returns prompt_id when complete.
    Raises RuntimeError on execution error or timeout."""
    ws_url = f"ws://{get_comfyui_url().replace('http://', '')}/ws?clientId={CLIENT_ID}"
    current_stage = "starting"
    current_node = None

    async with websockets.connect(ws_url) as ws:
        # Drain any initial status message ComfyUI sends on connect
        try:
            await asyncio.wait_for(ws.recv(), timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            pass

        # Now submit the workflow (WS is already listening)
        payload = {"prompt": workflow, "client_id": CLIENT_ID}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{get_comfyui_url()}/prompt", json=payload)
            r.raise_for_status()
            prompt_id = r.json()["prompt_id"]

        await on_progress("queued", 0, 0, 0)

        # Listen for progress
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            msg_type = msg.get("type")
            data = msg.get("data", {})

            # Modern ComfyUI: progress_state events with per-node progress
            if msg_type == "progress_state" and data.get("prompt_id") == prompt_id:
                nodes = data.get("nodes", {})
                for node_id, node_info in nodes.items():
                    state = node_info.get("state", "")
                    value = node_info.get("value", 0)
                    max_val = node_info.get("max", 1)

                    if state in ("executing", "running"):
                        current_node = node_id
                        current_stage = NODE_STAGES.get(node_id, f"processing node {node_id}")
                        step = int(value * max_val) if isinstance(value, float) and value <= 1 else int(value)
                        total = int(max_val)
                        if current_node in NODE_PROGRESS_RANGE:
                            start, end = NODE_PROGRESS_RANGE[current_node]
                            frac = value / max_val if max_val > 0 else 0
                            overall = start + frac * (end - start)
                        else:
                            overall = 0
                        await on_progress(current_stage, step, total, overall)

            # Legacy ComfyUI: progress events
            elif msg_type == "progress" and data.get("prompt_id") == prompt_id:
                step = data.get("value", 0)
                total = data.get("max", 1)
                if current_node and current_node in NODE_PROGRESS_RANGE:
                    start, end = NODE_PROGRESS_RANGE[current_node]
                    frac = step / total if total > 0 else 0
                    overall = start + frac * (end - start)
                else:
                    overall = 0
                await on_progress(current_stage, step, total, overall)

            elif msg_type == "executing" and data.get("prompt_id") == prompt_id:
                node_id = data.get("node")
                if node_id is None:
                    return prompt_id
                current_node = node_id
                current_stage = NODE_STAGES.get(node_id, f"processing node {node_id}")
                if node_id in NODE_PROGRESS_RANGE:
                    overall = NODE_PROGRESS_RANGE[node_id][0]
                else:
                    overall = 0
                await on_progress(current_stage, 0, 0, overall)

            elif msg_type == "execution_error" and data.get("prompt_id") == prompt_id:
                error_msg = data.get("exception_message", "unknown error")
                raise RuntimeError(f"ComfyUI error: {error_msg}")

    return prompt_id
