import os
import json
import uuid
import time
import shutil
import random
import asyncio
import zipfile
import io
import httpx
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
# {job_id: {status, progress, stage, step, total_steps, prompt_id, error, files, queue_position}}
jobs: dict[str, dict] = {}
job_queue: asyncio.Queue = asyncio.Queue()
queue_order: list[str] = []  # ordered list of queued job IDs for position tracking
active_job_id: str | None = None  # currently processing job


# --- History (persistent JSON file) ---
HISTORY_FILE = JOBS_DIR / "history.json"

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []

def save_history(history: list[dict]):
    HISTORY_FILE.write_text(json.dumps(history, indent=2))

def append_history(entry: dict):
    history = load_history()
    history.insert(0, entry)  # newest first
    save_history(history)


# --- Queue Worker ---
async def queue_worker():
    """Process jobs one at a time from the queue."""
    global active_job_id
    while True:
        job_id, file_bytes, filename = await job_queue.get()
        active_job_id = job_id
        if job_id in queue_order:
            queue_order.remove(job_id)
        # Update positions for remaining queued jobs
        for i, qid in enumerate(queue_order):
            if qid in jobs:
                jobs[qid]["queue_position"] = i + 1
        try:
            await _run_job(job_id, file_bytes, filename)
        except Exception as e:
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(e)
        finally:
            active_job_id = None
            job_queue.task_done()


# --- Startup ---
@app.on_event("startup")
async def startup():
    JOBS_DIR.mkdir(exist_ok=True)
    asyncio.create_task(queue_worker())


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
    check_auth(request)

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

    # Position = items in queue + 1 (self) + 1 if a job is currently running
    position = job_queue.qsize() + 1 + (1 if active_job_id else 0)
    queue_order.append(job_id)

    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "stage": "queued",
        "step": 0,
        "total_steps": 0,
        "prompt_id": None,
        "error": None,
        "files": [],
        "queue_position": position,
    }

    # Add to queue (worker processes one at a time)
    await job_queue.put((job_id, file_bytes, file.filename or "input.png"))
    return {"job_id": job_id}


async def _run_job(job_id: str, file_bytes: bytes, filename: str):
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

        # Snapshot existing Untextured files so we can find the new one after
        existing_untextured = set(comfyui.list_output_files("Untextured", "glb"))

        # Submit and listen (WS opens before submission so we catch all progress)
        job["stage"] = "submitting"
        job["status"] = "running"

        async def on_progress(stage, step, total, overall):
            job["stage"] = stage
            job["step"] = step
            job["total_steps"] = total
            job["progress"] = round(overall, 2)

        prompt_id = await comfyui.submit_and_listen(workflow, on_progress)
        job["prompt_id"] = prompt_id

        # Collect outputs
        job["stage"] = "collecting outputs"
        job["progress"] = 0.95
        history = await comfyui.get_history(prompt_id)
        outputs = history.get("outputs", {})

        collected_files = []

        # 1) Collect files from history API (texture PNG from SaveImage)
        for node_id, node_output in outputs.items():
            for key, items in node_output.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or "filename" not in item:
                        continue
                    fname = item["filename"]
                    subfolder = item.get("subfolder", "")
                    if fname.endswith(".png") and "Texture" in fname:
                        data = await comfyui.download_output(fname, subfolder)
                        (job_dir / "texture.png").write_bytes(data)
                        collected_files.append("texture.png")

        # 2) GLB files are written directly to ComfyUI's output dir
        #    by Hy3DInPaint (Textured.glb) and Hy3D21ExportMesh (Untextured_NNNNN_.glb)
        #    They don't appear in the history API, so probe for them
        if "textured.glb" not in collected_files:
            from pathlib import Path as P
            if comfyui.get_output_dir():
                src = P(comfyui.get_output_dir()) / "Textured.glb"
                if src.exists():
                    shutil.copy2(str(src), str(job_dir / "textured.glb"))
                    collected_files.append("textured.glb")
            if "textured.glb" not in collected_files:
                try:
                    data = await comfyui.download_output("Textured.glb")
                    (job_dir / "textured.glb").write_bytes(data)
                    collected_files.append("textured.glb")
                except Exception as e:
                    print(f"Warning: could not download Textured.glb: {e}")

        if "untextured.glb" not in collected_files:
            from pathlib import Path as P
            if comfyui.get_output_dir():
                # Filesystem approach: diff before/after to find the new file
                all_untextured = set(comfyui.list_output_files("Untextured", "glb"))
                new_files = sorted(all_untextured - existing_untextured)
                if new_files:
                    src = P(comfyui.get_output_dir()) / new_files[-1]
                    if src.exists():
                        shutil.copy2(str(src), str(job_dir / "untextured.glb"))
                        collected_files.append("untextured.glb")
            if "untextured.glb" not in collected_files:
                # HTTP fallback: try downloading the latest Untextured file
                # Probe a range to find the highest numbered one
                latest_untextured = None
                async with httpx.AsyncClient(timeout=5.0) as client:
                    for i in range(200, 0, -1):
                        fname = f"Untextured_{i:05d}_.glb"
                        try:
                            r = await client.head(f"{comfyui.get_comfyui_url()}/view", params={"filename": fname, "type": "output"})
                            if r.status_code == 200:
                                latest_untextured = fname
                                break
                        except Exception:
                            continue
                if latest_untextured:
                    try:
                        data = await comfyui.download_output(latest_untextured)
                        (job_dir / "untextured.glb").write_bytes(data)
                        collected_files.append("untextured.glb")
                    except Exception:
                        pass

        job["status"] = "completed"
        job["progress"] = 1.0
        job["stage"] = "done"
        job["files"] = collected_files

        # Save to persistent history
        append_history({
            "job_id": job_id,
            "timestamp": int(time.time()),
            "filename": filename,
            "files": collected_files,
        })

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["stage"] = "failed"


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
        "queue_position": job.get("queue_position", 0),
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
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(404, detail="job not found")
    # Get file list from in-memory job or scan directory
    job = jobs.get(job_id)
    if job and job.get("files"):
        file_list = job["files"]
    else:
        file_list = [f.name for f in job_dir.iterdir() if f.is_file()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in file_list:
            fpath = job_dir / fname
            if fpath.exists():
                zf.write(fpath, fname)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=3d-model-{job_id}.zip"},
    )


@app.get("/api/history")
async def get_history_endpoint(request: Request):
    check_auth(request)
    history = load_history()
    # Filter out entries whose files have been deleted
    valid = []
    for entry in history:
        job_dir = JOBS_DIR / entry["job_id"]
        if job_dir.exists() and any(job_dir.iterdir()):
            valid.append(entry)
    return valid


@app.delete("/api/history/{job_id}")
async def delete_history_entry(job_id: str, request: Request):
    check_auth(request)
    # Remove files
    job_dir = JOBS_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    # Remove from history
    history = load_history()
    history = [h for h in history if h["job_id"] != job_id]
    save_history(history)
    return {"ok": True}


# Serve frontend static files with no-cache headers (must be last)
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.endswith((".html", ".js", ".css")) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["CDN-Cache-Control"] = "no-cache"
        return response

app.add_middleware(NoCacheStaticMiddleware)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
