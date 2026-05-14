# 3D Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a simple web app where friends upload an image and get a 3D GLB model via local ComfyUI + Hunyuan3D 2.1.

**Architecture:** FastAPI backend talks to ComfyUI at 127.0.0.1:8188 via REST + WebSocket. Vanilla HTML/CSS/JS frontend with Google model-viewer. Cookie-based shared password auth. Single job at a time.

**Tech Stack:** Python 3.12, FastAPI, httpx, websockets, itsdangerous, Google model-viewer web component

**Spec:** `docs/superpowers/specs/2026-05-12-3d-generator-design.md`

---

## File Structure

```
3d-generator/
  backend/
    main.py              # FastAPI app: all endpoints, static file serving, job state, cleanup
    comfyui.py           # ComfyUI client: health check, upload image, submit workflow, WebSocket progress, fetch outputs
    workflows/
      image_to_3d.json   # Hunyuan3D workflow template (based on linux-box roblox_ugc_hunyuan.json)
    requirements.txt
  frontend/
    index.html
    style.css
    app.js
  jobs/                  # gitignored, created at runtime
  .env
  .gitignore
  start.bat
```

---

### Task 1: Project Scaffold + Dependencies

**Files:**
- Create: `backend/requirements.txt`
- Create: `.env`
- Create: `.gitignore`
- Create: `start.bat`

- [ ] **Step 1: Create requirements.txt**

```
fastapi
uvicorn[standard]
python-dotenv
httpx
websockets
itsdangerous
python-multipart
```

- [ ] **Step 2: Create .env**

```
SITE_PASSWORD=letmein
SECRET_KEY=change-me-to-random-string
COMFYUI_URL=http://127.0.0.1:8188
```

- [ ] **Step 3: Create .gitignore**

```
jobs/
.env
__pycache__/
*.pyc
venv/
```

- [ ] **Step 4: Create start.bat**

```bat
@echo off
cd /d "%~dp0"
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload
```

- [ ] **Step 5: Install dependencies**

Run: `cd "C:/Users/ethan/Desktop/Coding Projects/Roblox & Gaming/3d-generator" && python -m pip install -r backend/requirements.txt`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: project scaffold with dependencies and config"
```

---

### Task 2: ComfyUI Client

**Files:**
- Create: `backend/comfyui.py`

This is the core module — handles all ComfyUI communication.

- [ ] **Step 1: Create comfyui.py with health check and model listing**

```python
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
                # Determine stage from node - Hy3DMeshGenerator is mesh gen, others are texture/bake
                await on_progress("generating 3d model", step, total)

            elif msg_type == "executing" and data.get("prompt_id") == prompt_id:
                if data.get("node") is None:
                    # Execution complete
                    return

            elif msg_type == "execution_error" and data.get("prompt_id") == prompt_id:
                error_msg = data.get("exception_message", "unknown error")
                raise RuntimeError(f"ComfyUI error: {error_msg}")
```

- [ ] **Step 2: Test health check manually**

Run: `cd "C:/Users/ethan/Desktop/Coding Projects/Roblox & Gaming/3d-generator" && python -c "import asyncio; from backend.comfyui import is_online; print(asyncio.run(is_online()))"`
Expected: `True` (if ComfyUI running) or `False` (if not)

- [ ] **Step 3: Commit**

```bash
git add backend/comfyui.py
git commit -m "feat: ComfyUI client with health check, upload, submit, progress, and output download"
```

---

### Task 3: Workflow Template

**Files:**
- Create: `backend/workflows/image_to_3d.json`

Based on the linux-box `roblox_ugc_hunyuan.json` workflow. The backend will swap `IMAGE_FILENAME` placeholder at runtime.

- [ ] **Step 1: Create the image-to-3D workflow JSON**

This is the exact ComfyUI API format workflow. Key nodes:
- Node 14: `Hy3D21LoadImageWithTransparency` — loads uploaded image (placeholder `IMAGE_FILENAME`)
- Node 58: `Image Remove Background (rembg)` — removes background
- Node 37: `Hy3DMeshGenerator` — generates mesh (30 steps, guidance 8.5, random seed)
- Node 4: `Hy3D21VAELoader` — loads VAE
- Node 9: `Hy3D21VAEDecode` — decodes latents to mesh
- Node 43: `Hy3D21PostprocessMesh` — cleanup (remove floaters, reduce faces to 4000, smooth normals)
- Node 45: `Hy3D21MeshUVWrap` — UV unwrap
- Node 19: `Hy3D21CameraConfig` — multi-view camera setup
- Node 20: `Hy3DMultiViewsGenerator` — generate texture views (15 steps, guidance 3)
- Node 21: `Hy3DBakeMultiViews` — bake textures
- Node 49: `Hy3DInPaint` — inpaint texture gaps
- Node 49: `Hy3DInPaint` — inpaints texture gaps and writes textured GLB via `output_mesh_name` = `Textured`
- Node 44: `Hy3D21ExportMesh` — export untextured GLB (prefix `Untextured`, from node 43 pre-texturing)
- Node 55: `SaveImage` — save texture PNG

```json
{
  "4": {
    "inputs": {"model_name": "hunyuan3d-vae-v2-1.ckpt"},
    "class_type": "Hy3D21VAELoader"
  },
  "9": {
    "inputs": {
      "box_v": 1.01, "octree_resolution": 256, "num_chunks": 64000,
      "mc_level": 0, "mc_algo": "mc", "enable_flash_vdm": true,
      "force_offload": false,
      "vae": ["4", 0], "latents": ["37", 0]
    },
    "class_type": "Hy3D21VAEDecode"
  },
  "14": {
    "inputs": {"image": "IMAGE_FILENAME"},
    "class_type": "Hy3D21LoadImageWithTransparency"
  },
  "19": {
    "inputs": {
      "camera_azimuths": "0, 90, 180, 270, 0, 180",
      "camera_elevations": "0, 0, 0, 0, 90, -90",
      "view_weights": "1, 0.5, 1, 0.5, 1, 1",
      "ortho_scale": 1.1
    },
    "class_type": "Hy3D21CameraConfig"
  },
  "20": {
    "inputs": {
      "view_size": 768, "steps": 15, "guidance_scale": 3,
      "texture_size": 1024, "unwrap_mesh": false,
      "seed": 0,
      "trimesh": ["45", 0], "camera_config": ["19", 0],
      "image": ["58", 0]
    },
    "class_type": "Hy3DMultiViewsGenerator"
  },
  "21": {
    "inputs": {
      "pipeline": ["20", 0], "camera_config": ["19", 0],
      "albedo": ["20", 1], "mr": ["20", 2]
    },
    "class_type": "Hy3DBakeMultiViews"
  },
  "30": {
    "inputs": {"value": 4000},
    "class_type": "INTConstant"
  },
  "37": {
    "inputs": {
      "model": "hunyuan3d-dit-v2-1.ckpt", "steps": 30,
      "guidance_scale": 8.5, "seed": 0,
      "attention_mode": "sdpa",
      "image": ["58", 0]
    },
    "class_type": "Hy3DMeshGenerator"
  },
  "43": {
    "inputs": {
      "remove_floaters": true, "remove_degenerate_faces": true,
      "reduce_faces": true, "max_facenum": ["30", 0],
      "smooth_normals": true,
      "trimesh": ["9", 0]
    },
    "class_type": "Hy3D21PostprocessMesh"
  },
  "44": {
    "inputs": {
      "filename_prefix": "Untextured",
      "file_format": "glb", "save_file": true,
      "trimesh": ["43", 0]
    },
    "class_type": "Hy3D21ExportMesh"
  },
  "45": {
    "inputs": {"trimesh": ["43", 0]},
    "class_type": "Hy3D21MeshUVWrap"
  },
  "49": {
    "inputs": {
      "output_mesh_name": "Textured",
      "pipeline": ["21", 0], "albedo": ["21", 1],
      "albedo_mask": ["21", 2], "mr": ["21", 3], "mr_mask": ["21", 4]
    },
    "class_type": "Hy3DInPaint"
  },
  "55": {
    "inputs": {
      "filename_prefix": "Texture",
      "images": ["49", 0]
    },
    "class_type": "SaveImage"
  },
  "58": {
    "inputs": {"image": ["14", 2]},
    "class_type": "Image Remove Background (rembg)"
  },
}
```

Note: Node 49 (`Hy3DInPaint`) writes the textured GLB directly via its `output_mesh_name` field — no separate export node needed. Node 44 exports the untextured mesh (from node 43, pre-texturing). Node 55 saves the texture PNG. Seeds (nodes 37, 20) are randomized at submit time.

- [ ] **Step 2: Commit**

```bash
git add backend/workflows/image_to_3d.json
git commit -m "feat: Hunyuan3D image-to-3D workflow template"
```

---

### Task 4: FastAPI Backend

**Files:**
- Create: `backend/__init__.py` (empty)
- Create: `backend/main.py`

- [ ] **Step 1: Create empty __init__.py**

```python
```

- [ ] **Step 2: Create main.py with all endpoints**

```python
import os
import json
import uuid
import time
import shutil
import random
import asyncio
import zipfile
import io
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Response, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from itsdangerous import URLSafeSerializer

from backend import comfyui

SITE_PASSWORD = os.getenv("SITE_PASSWORD", "letmein")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
COOKIE_NAME = "session"
JOBS_DIR = Path(__file__).parent.parent / "jobs"
WORKFLOWS_DIR = Path(__file__).parent / "workflows"
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

signer = URLSafeSerializer(SECRET_KEY)

app = FastAPI()

# --- In-memory job store ---
# {job_id: {status, progress, stage, step, total_steps, prompt_id, error, files}}
jobs: dict[str, dict] = {}
current_job_id: str | None = None  # only one job at a time


# --- Startup: clean old jobs ---
@app.on_event("startup")
async def startup():
    if JOBS_DIR.exists():
        cutoff = time.time() - 86400  # 24 hours
        for d in JOBS_DIR.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    JOBS_DIR.mkdir(exist_ok=True)


# --- Auth helpers ---
def check_auth(request: Request):
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        raise HTTPException(401, "not authenticated")
    try:
        signer.loads(cookie)
    except Exception:
        raise HTTPException(401, "invalid session")


@app.post("/api/auth")
async def auth(request: Request, response: Response):
    body = await request.json()
    if body.get("password") != SITE_PASSWORD:
        raise HTTPException(401, detail="invalid password")
    token = signer.dumps({"ok": True})
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=86400 * 7)
    return {"ok": True}


@app.get("/api/check-auth")
async def check_auth_endpoint(request: Request):
    try:
        check_auth(request)
        return {"authenticated": True}
    except HTTPException:
        return {"authenticated": False}


@app.get("/api/status")
async def gpu_status(request: Request):
    check_auth(request)
    online = await comfyui.is_online()
    return {"online": online}


@app.post("/api/generate")
async def generate(request: Request, mode: str = Form(...), file: UploadFile | None = File(None)):
    global current_job_id
    check_auth(request)

    if current_job_id and jobs.get(current_job_id, {}).get("status") in ("queued", "running"):
        raise HTTPException(429, detail="gpu is busy, try again shortly")

    if not await comfyui.is_online():
        raise HTTPException(503, detail="gpu is offline")

    if mode != "image" or not file:
        raise HTTPException(400, detail="upload an image")

    # Validate file type
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename else ""
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise HTTPException(400, detail="unsupported image format (use PNG, JPG, or WEBP)")

    # Read and validate size
    file_bytes = await file.read()
    if len(file_bytes) > 20 * 1024 * 1024:
        raise HTTPException(400, detail="image too large (max 20MB)")

    # Create job
    job_id = uuid.uuid4().hex[:8]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "stage": "uploading",
        "step": 0,
        "total_steps": 0,
        "prompt_id": None,
        "error": None,
        "files": [],
    }
    current_job_id = job_id

    # Run in background
    asyncio.create_task(_run_job(job_id, file_bytes, file.filename or "input.png"))
    return {"job_id": job_id}


async def _run_job(job_id: str, file_bytes: bytes, filename: str):
    global current_job_id
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    try:
        # Upload image to ComfyUI
        job["stage"] = "uploading image"
        comfy_filename = await comfyui.upload_image(file_bytes, filename)

        # Load and prepare workflow
        with open(WORKFLOWS_DIR / "image_to_3d.json") as f:
            workflow = json.load(f)

        # Swap in the uploaded filename
        workflow["14"]["inputs"]["image"] = comfy_filename

        # Randomize seeds
        workflow["37"]["inputs"]["seed"] = random.randint(0, 2**53)
        workflow["20"]["inputs"]["seed"] = random.randint(0, 2**53)

        # Submit to ComfyUI
        job["stage"] = "submitting"
        prompt_id = await comfyui.submit_workflow(workflow)
        job["prompt_id"] = prompt_id
        job["status"] = "running"
        job["stage"] = "generating 3d model"

        # Listen for progress
        async def on_progress(stage, step, total):
            job["stage"] = stage
            job["step"] = step
            job["total_steps"] = total
            if total > 0:
                job["progress"] = round(step / total, 2)

        await comfyui.listen_progress(prompt_id, on_progress)

        # Collect outputs
        job["stage"] = "collecting outputs"
        job["progress"] = 0.95
        history = await comfyui.get_history(prompt_id)
        outputs = history.get("outputs", {})

        collected_files = []

        # Collect all output files from history
        # Node 49 (Hy3DInPaint) writes textured GLB via output_mesh_name
        # Node 44 (Hy3D21ExportMesh) writes untextured GLB
        # Node 55 (SaveImage) writes texture PNG
        # Output keys vary by node type — scan all keys for GLB/PNG files
        for node_id, node_output in outputs.items():
            for key, items in node_output.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or "filename" not in item:
                        continue
                    fname = item["filename"]
                    subfolder = item.get("subfolder", "")
                    if fname.endswith(".glb") and "Textured" in fname and "Untextured" not in fname:
                        data = await comfyui.download_output(fname, subfolder)
                        (job_dir / "textured.glb").write_bytes(data)
                        collected_files.append("textured.glb")
                    elif fname.endswith(".glb") and "Untextured" in fname:
                        data = await comfyui.download_output(fname, subfolder)
                        (job_dir / "untextured.glb").write_bytes(data)
                        collected_files.append("untextured.glb")
                    elif fname.endswith(".png") and "Texture" in fname:
                        data = await comfyui.download_output(fname, subfolder)
                        (job_dir / "texture.png").write_bytes(data)
                        collected_files.append("texture.png")

        job["status"] = "completed"
        job["progress"] = 1.0
        job["stage"] = "done"
        job["files"] = collected_files

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["stage"] = "failed"
    finally:
        current_job_id = None


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    check_auth(request)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, detail="job not found")
    return {
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "step": job["step"],
        "total_steps": job["total_steps"],
        "files": job["files"],
        "error": job["error"],
    }


@app.get("/api/jobs/{job_id}/files/{filename}")
async def get_file(job_id: str, filename: str, request: Request):
    check_auth(request)
    if filename not in ("textured.glb", "untextured.glb", "texture.png"):
        raise HTTPException(400, detail="invalid filename")
    path = JOBS_DIR / job_id / filename
    if not path.exists():
        raise HTTPException(404, detail="file not found")
    media = "model/gltf-binary" if filename.endswith(".glb") else "image/png"
    return FileResponse(path, media_type=media, filename=filename)


@app.get("/api/jobs/{job_id}/download")
async def download_zip(job_id: str, request: Request):
    check_auth(request)
    job = jobs.get(job_id)
    if not job or job["status"] != "completed":
        raise HTTPException(404, detail="job not found or not completed")
    job_dir = JOBS_DIR / job_id
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in job["files"]:
            fpath = job_dir / fname
            if fpath.exists():
                zf.write(fpath, fname)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=3d-model-{job_id}.zip"},
    )


# Serve frontend static files (must be last)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
```

- [ ] **Step 3: Test server starts**

Run: `cd "C:/Users/ethan/Desktop/Coding Projects/Roblox & Gaming/3d-generator" && python -m uvicorn backend.main:app --host 0.0.0.0 --port 8080 &`
Then: `curl -s http://localhost:8080/api/check-auth`
Expected: `{"authenticated": false}`

Kill server after test.

- [ ] **Step 4: Commit**

```bash
git add backend/__init__.py backend/main.py
git commit -m "feat: FastAPI backend with auth, generate, job status, file serving, and zip download"
```

---

### Task 5: Frontend — HTML Structure

**Files:**
- Create: `frontend/index.html`

- [ ] **Step 1: Create index.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>3d generator</title>
    <link rel="stylesheet" href="/style.css">
    <script type="module" src="https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js"></script>
</head>
<body>
    <!-- Password Gate -->
    <main id="auth-gate">
        <h1>3d generator</h1>
        <p class="subtitle">enter password to continue</p>
        <div class="input-group">
            <input type="password" id="password-input" placeholder="password" autocomplete="off">
            <button id="auth-btn" onclick="authenticate()">enter</button>
        </div>
        <p id="auth-error" class="error-text hidden"></p>
    </main>

    <!-- Main App -->
    <main id="app" class="hidden">
        <div class="header-row">
            <h1>3d generator</h1>
            <div id="gpu-status" class="status-dot offline">
                <span class="dot"></span>
                <span id="gpu-status-text">checking...</span>
            </div>
        </div>
        <p class="subtitle">upload an image, get a 3d model</p>

        <!-- Upload Area -->
        <div id="upload-area" class="upload-area" onclick="document.getElementById('file-input').click()">
            <input type="file" id="file-input" accept=".png,.jpg,.jpeg,.webp" hidden>
            <p id="upload-text">drag & drop an image here, or click to browse</p>
            <img id="upload-preview" class="upload-preview hidden" alt="preview">
        </div>

        <button id="generate-btn" onclick="startGeneration()" disabled>generate</button>

        <!-- Progress -->
        <div id="progress-section" class="hidden">
            <div class="progress-bar">
                <div id="progress-fill" class="progress-fill"></div>
            </div>
            <p id="status-text" class="status-text"></p>
        </div>

        <!-- Error -->
        <div id="error-section" class="hidden">
            <p id="error-text" class="error-text"></p>
        </div>

        <!-- Preview -->
        <div id="preview-section" class="hidden">
            <div class="preview-tabs">
                <button class="tab active" data-tab="textured" onclick="switchTab('textured')">textured</button>
                <button class="tab" data-tab="untextured" onclick="switchTab('untextured')">untextured</button>
                <button class="tab" data-tab="texture" onclick="switchTab('texture')">texture</button>
            </div>

            <div id="tab-textured" class="tab-content active">
                <model-viewer id="viewer-textured" camera-controls auto-rotate shadow-intensity="1" exposure="1" alt="textured 3d model"></model-viewer>
            </div>

            <div id="tab-untextured" class="tab-content">
                <model-viewer id="viewer-untextured" camera-controls auto-rotate shadow-intensity="1" exposure="1" alt="untextured 3d model"></model-viewer>
            </div>

            <div id="tab-texture" class="tab-content">
                <img id="texture-image" class="texture-image" alt="texture map">
            </div>

            <a id="download-btn" class="download-btn">download zip</a>
        </div>
    </main>

    <script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add frontend/index.html
git commit -m "feat: frontend HTML structure with auth gate, upload, progress, preview, and download"
```

---

### Task 6: Frontend — CSS

**Files:**
- Create: `frontend/style.css`

- [ ] **Step 1: Create style.css matching lyric generator aesthetic**

```css
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #ffffff;
    color: #000000;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
}

main {
    max-width: 600px;
    width: 100%;
    padding: 40px 24px;
}

h1 {
    font-size: 24px;
    font-weight: 500;
    letter-spacing: -0.02em;
    margin-bottom: 8px;
}

.subtitle {
    font-size: 14px;
    color: #666;
    margin-bottom: 32px;
}

.header-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
}

.header-row h1 {
    margin-bottom: 0;
}

/* GPU Status */
.status-dot {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: #666;
}

.status-dot .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #ccc;
}

.status-dot.online .dot {
    background: #22c55e;
}

.status-dot.offline .dot {
    background: #ef4444;
}

/* Auth / Input */
.input-group {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
}

input[type="password"] {
    flex: 1;
    padding: 12px 16px;
    font-size: 14px;
    font-family: inherit;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    outline: none;
    transition: border-color 0.15s;
}

input[type="password"]:focus {
    border-color: #000;
}

input::placeholder {
    color: #aaa;
}

button {
    padding: 12px 24px;
    font-size: 14px;
    font-family: inherit;
    font-weight: 500;
    background: #000;
    color: #fff;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    transition: opacity 0.15s;
    white-space: nowrap;
}

button:hover {
    opacity: 0.8;
}

button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
}

/* Upload Area */
.upload-area {
    border: 2px dashed #e0e0e0;
    border-radius: 8px;
    padding: 40px 24px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s;
    margin-bottom: 16px;
    position: relative;
}

.upload-area:hover {
    border-color: #999;
}

.upload-area.dragover {
    border-color: #000;
    background: #fafafa;
}

.upload-area p {
    font-size: 14px;
    color: #999;
}

.upload-preview {
    max-width: 200px;
    max-height: 200px;
    border-radius: 4px;
    margin-top: 12px;
}

#generate-btn {
    width: 100%;
    margin-bottom: 24px;
}

/* Progress */
.progress-bar {
    height: 4px;
    background: #f0f0f0;
    border-radius: 2px;
    overflow: hidden;
    margin-bottom: 12px;
}

.progress-fill {
    height: 100%;
    background: #000;
    border-radius: 2px;
    width: 0%;
    transition: width 0.3s ease;
}

.status-text {
    font-size: 13px;
    color: #666;
    margin-bottom: 16px;
}

.error-text {
    font-size: 13px;
    color: #cc0000;
    line-height: 1.5;
}

/* Preview Tabs */
.preview-tabs {
    display: flex;
    gap: 0;
    margin-bottom: 16px;
    border-bottom: 1px solid #e0e0e0;
}

.tab {
    background: none;
    color: #999;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 8px 16px;
    font-size: 14px;
    font-weight: 400;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
}

.tab:hover {
    color: #000;
    opacity: 1;
}

.tab.active {
    color: #000;
    border-bottom-color: #000;
    font-weight: 500;
}

.tab-content {
    display: none;
}

.tab-content.active {
    display: block;
}

/* Model Viewer */
model-viewer {
    width: 100%;
    height: 400px;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    background: #fafafa;
    margin-bottom: 16px;
}

.texture-image {
    width: 100%;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    margin-bottom: 16px;
}

/* Download */
.download-btn {
    display: block;
    text-align: center;
    padding: 12px 32px;
    font-size: 14px;
    font-family: inherit;
    font-weight: 500;
    background: #000;
    color: #fff;
    text-decoration: none;
    border-radius: 8px;
    transition: opacity 0.15s;
    cursor: pointer;
}

.download-btn:hover {
    opacity: 0.8;
}

/* Utility */
.hidden {
    display: none !important;
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/style.css
git commit -m "feat: frontend CSS matching lyric generator light theme"
```

---

### Task 7: Frontend — JavaScript Logic

**Files:**
- Create: `frontend/app.js`

- [ ] **Step 1: Create app.js with all frontend logic**

```javascript
let selectedFile = null;
let activeJobId = null;
let pollInterval = null;
let statusInterval = null;
let gpuOnline = false;

// --- Auth ---
async function authenticate() {
    const pw = document.getElementById('password-input').value;
    const errEl = document.getElementById('auth-error');
    errEl.classList.add('hidden');
    try {
        const r = await fetch('/api/auth', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: pw }),
        });
        if (!r.ok) {
            errEl.textContent = 'wrong password';
            errEl.classList.remove('hidden');
            return;
        }
        showApp();
    } catch (e) {
        errEl.textContent = 'connection error';
        errEl.classList.remove('hidden');
    }
}

function showApp() {
    document.getElementById('auth-gate').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
    checkGpuStatus();
    statusInterval = setInterval(checkGpuStatus, 30000);
}

document.getElementById('password-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') authenticate();
});

// Check existing session on load
(async () => {
    try {
        const r = await fetch('/api/check-auth');
        const data = await r.json();
        if (data.authenticated) showApp();
    } catch (e) {}
})();

// --- GPU Status ---
async function checkGpuStatus() {
    const el = document.getElementById('gpu-status');
    const textEl = document.getElementById('gpu-status-text');
    try {
        const r = await fetch('/api/status');
        const data = await r.json();
        gpuOnline = data.online;
    } catch (e) {
        gpuOnline = false;
    }
    el.className = 'status-dot ' + (gpuOnline ? 'online' : 'offline');
    textEl.textContent = gpuOnline ? 'gpu online' : 'gpu offline';
    updateGenerateButton();
}

// --- File Upload ---
const uploadArea = document.getElementById('upload-area');
const fileInput = document.getElementById('file-input');

uploadArea.addEventListener('dragover', (e) => { e.preventDefault(); uploadArea.classList.add('dragover'); });
uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
uploadArea.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadArea.classList.remove('dragover');
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => { if (fileInput.files.length) handleFile(fileInput.files[0]); });

function handleFile(file) {
    const ext = file.name.split('.').pop().toLowerCase();
    if (!['png', 'jpg', 'jpeg', 'webp'].includes(ext)) { alert('Use PNG, JPG, or WEBP'); return; }
    if (file.size > 20 * 1024 * 1024) { alert('Max 20MB'); return; }
    selectedFile = file;
    document.getElementById('upload-preview').src = URL.createObjectURL(file);
    document.getElementById('upload-preview').classList.remove('hidden');
    document.getElementById('upload-text').textContent = file.name;
    updateGenerateButton();
}

function updateGenerateButton() {
    document.getElementById('generate-btn').disabled = !selectedFile || !gpuOnline || !!activeJobId;
}

// --- Generate ---
async function startGeneration() {
    document.getElementById('generate-btn').disabled = true;
    document.getElementById('progress-section').classList.remove('hidden');
    document.getElementById('error-section').classList.add('hidden');
    document.getElementById('preview-section').classList.add('hidden');
    document.getElementById('progress-fill').style.width = '0%';
    document.getElementById('status-text').textContent = 'uploading...';

    const fd = new FormData();
    fd.append('mode', 'image');
    fd.append('file', selectedFile);

    try {
        const r = await fetch('/api/generate', { method: 'POST', body: fd });
        if (!r.ok) { const d = await r.json(); showError(d.detail || 'failed'); return; }
        const data = await r.json();
        activeJobId = data.job_id;
        updateGenerateButton();
        pollInterval = setInterval(pollJob, 2000);
    } catch (e) {
        showError('connection error');
    }
}

async function pollJob() {
    if (!activeJobId) return;
    try {
        const r = await fetch(`/api/jobs/${activeJobId}`);
        const data = await r.json();
        document.getElementById('progress-fill').style.width = Math.round(data.progress * 100) + '%';
        document.getElementById('status-text').textContent = data.total_steps > 0
            ? `${data.stage}... step ${data.step}/${data.total_steps}`
            : data.stage + '...';

        if (data.status === 'completed') {
            clearInterval(pollInterval);
            document.getElementById('progress-fill').style.width = '100%';
            document.getElementById('status-text').textContent = 'done!';
            showPreview(activeJobId, data.files);
            activeJobId = null;
            updateGenerateButton();
        } else if (data.status === 'failed') {
            clearInterval(pollInterval);
            showError(data.error || 'generation failed');
            activeJobId = null;
            updateGenerateButton();
        }
    } catch (e) {}
}

function showError(msg) {
    document.getElementById('error-section').classList.remove('hidden');
    document.getElementById('error-text').textContent = msg;
    document.getElementById('progress-section').classList.add('hidden');
    activeJobId = null;
    updateGenerateButton();
}

// --- Preview ---
function showPreview(jobId, files) {
    document.getElementById('preview-section').classList.remove('hidden');
    if (files.includes('textured.glb'))
        document.getElementById('viewer-textured').src = `/api/jobs/${jobId}/files/textured.glb`;
    if (files.includes('untextured.glb'))
        document.getElementById('viewer-untextured').src = `/api/jobs/${jobId}/files/untextured.glb`;
    if (files.includes('texture.png'))
        document.getElementById('texture-image').src = `/api/jobs/${jobId}/files/texture.png`;
    document.getElementById('download-btn').href = `/api/jobs/${jobId}/download`;
    switchTab('textured');
}

function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/app.js
git commit -m "feat: frontend JS with auth, upload, polling, preview tabs, and download"
```

---

### Task 8: Integration Test

- [ ] **Step 1: Start ComfyUI**

User runs: `D:\Cursor\AI\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\run_nvidia_gpu.bat`

- [ ] **Step 2: Start FastAPI server**

Run: `cd "C:/Users/ethan/Desktop/Coding Projects/Roblox & Gaming/3d-generator" && python -m uvicorn backend.main:app --host 0.0.0.0 --port 8080`

- [ ] **Step 3: Verify GPU status endpoint**

Run: `curl -s http://localhost:8080/api/status` (should fail — no auth cookie)
Run: `curl -s -c cookies.txt -X POST http://localhost:8080/api/auth -H "Content-Type: application/json" -d '{"password":"letmein"}'`
Run: `curl -s -b cookies.txt http://localhost:8080/api/status`
Expected: `{"online": true}`

- [ ] **Step 4: Test in browser**

Open `http://localhost:8080` in browser. Verify:
1. Password gate appears
2. Enter "letmein" → main UI appears
3. GPU status shows green "gpu online"
4. Upload an image → preview appears, generate button enables
5. Click generate → progress bar updates → preview loads with 3D model → download works

- [ ] **Step 5: Test GPU offline detection**

Close ComfyUI. Wait 30 seconds. Verify status changes to red "gpu offline". Generate button should be disabled.

- [ ] **Step 6: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration test fixes"
```

---

### Task 9: Final Polish + README

- [ ] **Step 1: Test the full flow end-to-end one more time**

Upload image → generate → preview all three tabs → download zip → verify zip contains GLB + PNG files.

- [ ] **Step 2: Verify output node mapping is correct**

After a successful generation, check that the `/history/{prompt_id}` response maps correctly to textured.glb, untextured.glb, and texture.png. If the output keys don't match expectations (e.g., ComfyUI uses different keys than "gltf" or "mesh"), fix the output collection logic in `main.py:_run_job`.

- [ ] **Step 3: Commit final state**

```bash
git add -A
git commit -m "feat: 3d generator v1 complete - image to 3D via Hunyuan3D"
```
