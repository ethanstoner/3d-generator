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
        async def on_progress(stage, step, total, overall):
            job["stage"] = stage
            job["step"] = step
            job["total_steps"] = total
            job["progress"] = round(overall, 2)

        await comfyui.listen_progress(prompt_id, on_progress)

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
            try:
                data = await comfyui.download_output("Textured.glb")
                (job_dir / "textured.glb").write_bytes(data)
                collected_files.append("textured.glb")
            except Exception as e:
                print(f"Warning: could not download Textured.glb: {e}")

        if "untextured.glb" not in collected_files:
            # Find the latest Untextured_NNNNN_.glb by probing numbered suffixes
            candidates = await comfyui.find_recent_outputs("Untextured", "glb", 0)
            if candidates:
                # Use the last (highest numbered) one
                latest = sorted(candidates)[-1]
                try:
                    data = await comfyui.download_output(latest)
                    (job_dir / "untextured.glb").write_bytes(data)
                    collected_files.append("untextured.glb")
                except Exception as e:
                    print(f"Warning: could not download {latest}: {e}")

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
