import os
import re
import json
import uuid
import time
import shutil
import random
import asyncio
import zipfile
import io
import base64
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Response, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from itsdangerous import URLSafeSerializer
from pydantic import BaseModel

from backend import comfyui
from backend import llm


class PromptHelpRequest(BaseModel):
    idea: str


class MetaUpdate(BaseModel):
    name: str = ""
    description: str = ""


class UploadedUpdate(BaseModel):
    uploaded: bool


class MultiDownload(BaseModel):
    job_ids: list[str] = []


def _safe_filename(name: str) -> str:
    """Turn an item name into a filesystem-safe zip stem (drops emoji/punctuation,
    spaces -> underscores). Returns '' if nothing usable remains."""
    name = re.sub(r"[^\w\s.-]", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:60].strip("._-")

SITE_PASSWORD = os.getenv("SITE_PASSWORD", "change-me")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
COOKIE_NAME = "session"
JOBS_DIR = Path(__file__).parent.parent / "jobs"
WORKFLOWS_DIR = Path(__file__).parent / "workflows"
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
ALLOWED_TRIANGLES = {4000, 10000, 20000, 40000}

signer = URLSafeSerializer(SECRET_KEY)

app = FastAPI()

# --- In-memory job store ---
# {job_id: {status, progress, stage, step, total_steps, prompt_id, error, files,
#           queue_position, batch_id, filename}}
jobs: dict[str, dict] = {}
job_queue: asyncio.Queue = asyncio.Queue()
queue_order: list[str] = []  # ordered list of queued job IDs for position tracking
active_job_id: str | None = None  # currently processing job

# GPU lock — serializes ComfyUI 3D jobs and Ollama prompt-help calls so they
# never compete for VRAM on the shared GPU.
gpu_lock = asyncio.Lock()
PROMPT_HELP_LOCK_TIMEOUT = 90  # seconds a prompt-help call will wait for the lock


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

def update_history(job_id: str, fields: dict):
    history = load_history()
    for entry in history:
        if entry.get("job_id") == job_id:
            entry.update(fields)
            break
    save_history(history)


# --- Pending queue (persistent JSON file) ---
# The jobs dict and the asyncio.Queue live in memory only, so a restart used to
# silently drop everything still waiting. A bulk upload is meant to be dropped
# off and collected hours later, so pending work has to survive a restart.
PENDING_FILE = JOBS_DIR / "pending.json"

def load_pending() -> list[dict]:
    if PENDING_FILE.exists():
        try:
            records = json.loads(PENDING_FILE.read_text())
            if isinstance(records, list):
                return [r for r in records if isinstance(r, dict)]
        except Exception:
            return []
    return []

def save_pending(records: list[dict]):
    # Never let a bad write take the server down — a lost pending record costs
    # one re-upload, an unhandled exception costs the whole queue.
    try:
        PENDING_FILE.write_text(json.dumps(records, indent=2))
    except OSError as e:
        print(f"[pending] could not write {PENDING_FILE.name}: {e}")

def append_pending(entry: dict):
    records = load_pending()
    records.append(entry)  # oldest first — restore replays in enqueue order
    save_pending(records)

def remove_pending(job_id: str):
    records = load_pending()
    remaining = [r for r in records if r.get("job_id") != job_id]
    if len(remaining) != len(records):
        save_pending(remaining)


# --- Prompt-help history (shared, persistent JSON file) ---
PROMPT_HISTORY_FILE = JOBS_DIR / "prompt_history.json"
PROMPT_HISTORY_MAX = 50

def load_prompt_history() -> list[dict]:
    if PROMPT_HISTORY_FILE.exists():
        try:
            return json.loads(PROMPT_HISTORY_FILE.read_text())
        except Exception:
            return []
    return []

def save_prompt_history(history: list[dict]):
    PROMPT_HISTORY_FILE.write_text(json.dumps(history, indent=2))

def append_prompt_history(entry: dict):
    history = load_prompt_history()
    history.insert(0, entry)  # newest first
    del history[PROMPT_HISTORY_MAX:]  # cap growth
    save_prompt_history(history)


# --- Queue Worker ---
async def queue_worker():
    """Process jobs one at a time from the queue."""
    global active_job_id
    while True:
        job_id, input_path, filename, triangles = await job_queue.get()
        active_job_id = job_id
        if job_id in queue_order:
            queue_order.remove(job_id)
        # Update positions for remaining queued jobs
        for i, qid in enumerate(queue_order):
            if qid in jobs:
                jobs[qid]["queue_position"] = i + 1
        try:
            async with gpu_lock:
                await _run_job(job_id, input_path, filename, triangles)
        except Exception as e:
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(e)
        finally:
            # Terminal either way (completed or failed) — drop the restart
            # record here so there's one place that can't be forgotten.
            remove_pending(job_id)
            active_job_id = None
            job_queue.task_done()


# --- Startup ---
@app.on_event("startup")
async def startup():
    JOBS_DIR.mkdir(exist_ok=True)
    # Read pending.json BEFORE the sweep and hand the ids over as protected.
    # A pending job's dir holds its input image so it is non-empty and the
    # sweep would keep it anyway — but relying on that is a trap waiting for
    # the day a zero-byte upload gets staged. Make the ordering explicit.
    pending = load_pending()
    _sweep_orphan_job_dirs({r.get("job_id") for r in pending})
    asyncio.create_task(queue_worker())
    await _restore_pending_jobs(pending)


async def _restore_pending_jobs(pending: list[dict]):
    """Re-enqueue jobs that were still waiting when the server went down, in
    their original order. The input image is already staged on disk, so nothing
    has to be re-uploaded."""
    restored = []
    for record in pending:
        try:
            job_id = record["job_id"]
            input_path = Path(record["input_path"])
            triangles = int(record.get("triangles", 4000))
        except Exception:
            continue  # a mangled record is not worth failing startup over
        if job_id in jobs or not input_path.exists():
            continue
        if triangles not in ALLOWED_TRIANGLES:
            triangles = 4000
        _register_job(
            job_id, record.get("filename") or input_path.name, triangles,
            record.get("batch_id"),
        )
        await job_queue.put((job_id, str(input_path), jobs[job_id]["filename"], triangles))
        restored.append(job_id)
    # Rewrite the file so records whose input vanished don't linger forever
    save_pending([r for r in pending if r.get("job_id") in restored])
    if restored:
        print(f"[pending] re-enqueued {len(restored)} job(s) from pending.json")


def _sweep_orphan_job_dirs(protected: set | None = None):
    """Remove EMPTY orphan job folders (left by failed/aborted generations or
    server restarts) that aren't referenced in history.json, so the box stays
    in sync with the site. Non-empty untracked folders are kept and logged —
    real model data is never auto-destroyed."""
    try:
        ids = {h.get("job_id") for h in load_history()}
    except Exception:
        return
    ids |= (protected or set())
    for d in JOBS_DIR.iterdir():
        if not d.is_dir() or d.name.startswith(".") or d.name in ids:
            continue
        try:
            if any(d.iterdir()):
                n = sum(1 for _ in d.iterdir())
                print(f"[sweep] kept untracked job dir {d.name} "
                      f"({n} file(s)) — not in history, not empty")
            else:
                d.rmdir()
                print(f"[sweep] removed empty orphan job dir: {d.name}")
        except OSError as e:
            print(f"[sweep] could not process {d.name}: {e}")


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
    # Mark the cookie Secure when the request arrived over HTTPS. Cloudflare
    # terminates TLS and proxies to us over plain HTTP, so trust its
    # X-Forwarded-Proto header; fall back to the request scheme for local dev
    # (http://127.0.0.1, where a Secure cookie would never be sent back).
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(
        COOKIE_NAME, token,
        httponly=True, samesite="lax", secure=secure, max_age=86400 * 7,
    )
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


@app.post("/api/prompt-help")
async def prompt_help(request: Request, body: PromptHelpRequest):
    check_auth(request)
    if not await comfyui.is_online():
        raise HTTPException(503, detail="gpu is offline")
    idea = body.idea.strip()
    if not idea or len(idea) > 200:
        raise HTTPException(400, detail="idea must be 1-200 chars")
    try:
        await asyncio.wait_for(gpu_lock.acquire(), timeout=PROMPT_HELP_LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(503, detail="gpu busy with a 3d generation, try again in a moment")
    try:
        result = await llm.refine_idea(idea)
    except RuntimeError as e:
        raise HTTPException(503, detail=str(e))
    finally:
        gpu_lock.release()

    append_prompt_history({
        "id": uuid.uuid4().hex[:8],
        "timestamp": int(time.time()),
        "idea": idea,
        "name": result.get("name", ""),
        "description": result.get("description", ""),
        "image_prompt": result.get("image_prompt", ""),
    })
    return result


@app.get("/api/prompt-history")
async def get_prompt_history(request: Request):
    check_auth(request)
    return load_prompt_history()


@app.delete("/api/prompt-history/{entry_id}")
async def delete_prompt_history(entry_id: str, request: Request):
    check_auth(request)
    history = load_prompt_history()
    save_prompt_history([h for h in history if h.get("id") != entry_id])
    return {"ok": True}


@app.get("/api/research-prompt")
async def research_prompt(request: Request):
    check_auth(request)
    return {"prompt": llm.build_research_prompt()}


def _validate_upload(filename: str, file_bytes: bytes) -> str:
    """Returns '' if the upload is usable, else the reason it isn't. Shared by
    the single and batch paths so both reject exactly the same things."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("png", "jpg", "jpeg", "webp"):
        return "unsupported image format (use PNG, JPG, or WEBP)"
    if len(file_bytes) > 20 * 1024 * 1024:
        return "image too large (max 20MB)"
    if not file_bytes:
        return "empty file"
    return ""


def _stage_input(job_id: str, file_bytes: bytes, filename: str) -> Path:
    """Write the upload to jobs/{job_id}/input.{ext} at ENQUEUE time. The queue
    then carries the path instead of the bytes — a 30-image batch sitting on the
    queue as bytes would pin up to 600MB of RAM until the worker drained it."""
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"
    path = job_dir / f"input.{ext}"
    path.write_bytes(file_bytes)
    return path


def _register_job(job_id: str, filename: str, triangles: int, batch_id: str | None):
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
        "triangles": triangles,
        "started_at": None,
        "finished_at": None,
        "batch_id": batch_id,
        "filename": filename,
    }


async def _enqueue_job(file_bytes: bytes, filename: str, triangles: int,
                       batch_id: str | None = None) -> str:
    """Stage the image, record the job, persist it for restart survival, and
    hand the worker the path. One code path for both /api/generate and
    /api/generate-batch."""
    job_id = uuid.uuid4().hex[:8]
    input_path = _stage_input(job_id, file_bytes, filename)
    _register_job(job_id, filename, triangles, batch_id)
    append_pending({
        "job_id": job_id,
        "batch_id": batch_id,
        "filename": filename,
        "input_path": str(input_path),
        "triangles": triangles,
        "enqueued_at": int(time.time()),
    })
    await job_queue.put((job_id, str(input_path), filename, triangles))
    return job_id


@app.post("/api/generate")
async def generate(
    request: Request,
    mode: str = Form(...),
    triangles: int = Form(4000),
    file: UploadFile | None = File(None),
):
    check_auth(request)

    if not await comfyui.is_online():
        raise HTTPException(503, detail="gpu is offline")

    if mode != "image" or not file:
        raise HTTPException(400, detail="upload an image")

    filename = file.filename or "input.png"
    file_bytes = await file.read()
    reason = _validate_upload(filename, file_bytes)
    if reason:
        raise HTTPException(400, detail=reason)

    if triangles not in ALLOWED_TRIANGLES:
        raise HTTPException(400, detail="invalid triangle count")

    job_id = await _enqueue_job(file_bytes, filename, triangles)
    return {"job_id": job_id}


@app.post("/api/generate-batch")
async def generate_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    triangles: int = Form(4000),
):
    """Bulk upload: queue many images in one call and walk away. Files are
    validated independently — one unusable file is skipped and reported, it
    never rejects the other 29."""
    check_auth(request)

    if not await comfyui.is_online():
        raise HTTPException(503, detail="gpu is offline")

    if triangles not in ALLOWED_TRIANGLES:
        raise HTTPException(400, detail="invalid triangle count")

    if not files:
        raise HTTPException(400, detail="upload at least one image")
    if len(files) > 50:
        raise HTTPException(400, detail="too many images (max 50 per batch)")

    batch_id = uuid.uuid4().hex[:8]
    queued: list[dict] = []
    skipped: list[dict] = []
    for f in files:
        filename = f.filename or "input.png"
        file_bytes = await f.read()
        reason = _validate_upload(filename, file_bytes)
        if reason:
            skipped.append({"filename": filename, "reason": reason})
            continue
        job_id = await _enqueue_job(file_bytes, filename, triangles, batch_id)
        queued.append({"job_id": job_id, "filename": filename})

    return {"batch_id": batch_id, "jobs": queued, "skipped": skipped}


@app.get("/api/queue")
async def get_queue(request: Request, batch_id: str | None = None):
    """One poll for a whole batch. The frontend would otherwise fire 30
    /api/jobs/{id} requests every couple of seconds."""
    check_auth(request)
    out = []
    for job_id, job in jobs.items():
        if batch_id and job.get("batch_id") != batch_id:
            continue
        out.append({
            "job_id": job_id,
            "batch_id": job.get("batch_id"),
            "filename": job.get("filename"),
            "status": job["status"],
            "progress": job["progress"],
            "stage": job["stage"],
            "queue_position": job.get("queue_position", 0),
            "error": job["error"],
            "triangles": job.get("triangles"),
        })
    return {"active": active_job_id, "jobs": out}


async def _run_job(job_id: str, input_path: str, filename: str, triangles: int):
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    job["started_at"] = time.time()
    try:
        # The input image was already staged to disk at enqueue time (and stays
        # there so the item can be named/described later, incl. from history).
        file_bytes = Path(input_path).read_bytes()

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

        # Override triangle count (postprocess max_facenum)
        workflow["30"]["inputs"]["value"] = triangles

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

        # 1) Collect files reported in the history API. The texture PNG always
        #    shows up here (from SaveImage). Some ComfyUI builds also report the
        #    exported GLBs here — when they do, grab them now and skip the
        #    filesystem/HTTP probing below entirely.
        for node_id, node_output in outputs.items():
            for key, items in node_output.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or "filename" not in item:
                        continue
                    fname = item["filename"]
                    subfolder = item.get("subfolder", "")
                    if fname.endswith(".png") and "Texture" in fname and "texture.png" not in collected_files:
                        data = await comfyui.download_output(fname, subfolder)
                        (job_dir / "texture.png").write_bytes(data)
                        collected_files.append("texture.png")
                    elif fname.endswith(".glb"):
                        # "Untextured" contains "Textured" as a substring — test it first.
                        local = "untextured.glb" if "Untextured" in fname else "textured.glb"
                        if local not in collected_files:
                            data = await comfyui.download_output(fname, subfolder)
                            (job_dir / local).write_bytes(data)
                            collected_files.append(local)

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
                # HTTP fallback (prod: output dir not visible over the LAN) —
                # find the highest-numbered Untextured GLB via concurrent probes.
                latest_untextured = await comfyui.find_latest_numbered_output("Untextured", "glb")
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
        job["finished_at"] = time.time()

        textured_path = job_dir / "textured.glb"
        size = textured_path.stat().st_size if textured_path.exists() else None
        duration = (
            round(job["finished_at"] - job["started_at"])
            if job.get("started_at")
            else None
        )

        # Save to persistent history
        append_history({
            "job_id": job_id,
            "batch_id": job.get("batch_id"),
            "timestamp": int(time.time()),
            "filename": filename,
            "files": collected_files,
            "triangles": triangles,
            "size": size,
            "duration": duration,
        })

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["stage"] = "failed"
        # A failed job never gets a history entry, so its folder would be an
        # unreachable orphan on the box. Clean it up at the source.
        shutil.rmtree(job_dir, ignore_errors=True)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    check_auth(request)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, detail="job not found")
    duration = None
    if job.get("started_at") and job.get("finished_at"):
        duration = round(job["finished_at"] - job["started_at"])
    textured_path = JOBS_DIR / job_id / "textured.glb"
    size = textured_path.stat().st_size if textured_path.exists() else None
    return {
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "step": job["step"],
        "total_steps": job["total_steps"],
        "files": job["files"],
        "error": job["error"],
        "queue_position": job.get("queue_position", 0),
        "triangles": job.get("triangles"),
        "size": size,
        "duration": duration,
        "name": job.get("name"),
        "description": job.get("description"),
        "uploaded": job.get("uploaded", False),
        "batch_id": job.get("batch_id"),
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
    # Per-job files never change once written — let the browser cache them
    # hard so re-viewing a model or browsing history is instant.
    return FileResponse(
        path,
        media_type=media,
        filename=filename,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/jobs/{job_id}/thumb")
async def get_thumb(job_id: str, request: Request):
    """Small thumbnail for the history list. Prefers the original input image
    (the actual subject — instantly recognizable) over the baked UV texture
    atlas, which looks like scrambled noise at 48px. Falls back to texture.png
    for older generations that predate input-image persistence."""
    check_auth(request)
    job_dir = JOBS_DIR / job_id
    inputs = sorted(job_dir.glob("input.*")) if job_dir.exists() else []
    path = inputs[0] if inputs else (job_dir / "texture.png")
    if not path.exists():
        raise HTTPException(404, detail="no thumbnail")
    ext = path.suffix.lower().lstrip(".")
    media = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'png'}"
    return FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


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
    # Name the zip after the item (if it has a saved name) so it's easy to find
    # in the user's downloads; fall back to the job id.
    name = (job or {}).get("name")
    if not name:
        name = next((h.get("name") for h in load_history() if h.get("job_id") == job_id), None)
    stem = _safe_filename(name) if name else ""
    zip_name = f"{stem}.zip" if stem else f"3d-model-{job_id}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.post("/api/download-multi")
async def download_multi(body: MultiDownload, request: Request):
    """One zip for a whole batch, a folder per item. Missing jobs are skipped
    rather than failing the zip — after a bulk run some items may have been
    deleted already, and that shouldn't cost the user the other 29."""
    check_auth(request)
    job_ids = body.job_ids
    if not job_ids:
        raise HTTPException(400, detail="no job ids given")
    if len(job_ids) > 50:
        raise HTTPException(400, detail="too many items (max 50 per download)")

    history = load_history()
    buf = io.BytesIO()
    used_folders: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for job_id in job_ids:
            job_dir = JOBS_DIR / job_id
            if not job_dir.exists():
                continue
            job = jobs.get(job_id)
            if job and job.get("files"):
                file_list = job["files"]
            else:
                file_list = [f.name for f in job_dir.iterdir() if f.is_file()]
            name = (job or {}).get("name")
            if not name:
                name = next((h.get("name") for h in history if h.get("job_id") == job_id), None)
            folder = (_safe_filename(name) if name else "") or job_id
            # Two items can share a saved name — suffix the id so the second
            # one doesn't silently overwrite the first inside the zip.
            if folder in used_folders:
                folder = f"{folder}-{job_id}"
            used_folders.add(folder)
            for fname in file_list:
                fpath = job_dir / fname
                if fpath.exists():
                    zf.write(fpath, f"{folder}/{fname}")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="3d-models.zip"'},
    )


@app.post("/api/jobs/{job_id}/name")
async def name_item(job_id: str, request: Request, vary: bool = False):
    # vary=true (sent by "re-suggest") asks for a fresh alternative instead of
    # the identical deterministic result; the first suggest uses vary=false.
    check_auth(request)
    if not await comfyui.is_online():
        raise HTTPException(503, detail="gpu is offline")
    job_dir = JOBS_DIR / job_id
    inputs = sorted(job_dir.glob("input.*")) if job_dir.exists() else []
    if not inputs:
        raise HTTPException(404, detail="no input image saved for this item (older generation)")
    image_b64 = base64.b64encode(inputs[0].read_bytes()).decode()

    try:
        await asyncio.wait_for(gpu_lock.acquire(), timeout=PROMPT_HELP_LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(503, detail="gpu busy with a 3d generation, try again in a moment")
    try:
        result = await llm.describe_image(image_b64, vary=vary)
    except RuntimeError as e:
        raise HTTPException(503, detail=str(e))
    finally:
        gpu_lock.release()

    if job_id in jobs:
        jobs[job_id]["name"] = result["name"]
        jobs[job_id]["description"] = result["description"]
    update_history(job_id, {"name": result["name"], "description": result["description"]})
    return result


@app.post("/api/jobs/{job_id}/meta")
async def set_meta(job_id: str, body: MetaUpdate, request: Request):
    """Save a user-edited name/description for an item. No GPU needed — it's
    just text. Persists to history so the item keeps its name on return, and
    the download zip is named after it."""
    check_auth(request)
    job_dir = JOBS_DIR / job_id
    in_history = any(h.get("job_id") == job_id for h in load_history())
    if job_id not in jobs and not in_history and not job_dir.exists():
        raise HTTPException(404, detail="item not found")
    name = body.name.strip()[:100]
    description = body.description.strip()[:300]
    if job_id in jobs:
        jobs[job_id]["name"] = name
        jobs[job_id]["description"] = description
    update_history(job_id, {"name": name, "description": description})
    return {"ok": True, "name": name, "description": description}


@app.post("/api/jobs/{job_id}/uploaded")
async def set_uploaded(job_id: str, body: UploadedUpdate, request: Request):
    """Mark an item as already uploaded to Roblox (or unmark it). This is a
    SHARED flag — there's one password/identity, so every friend sees it. It
    lets the group avoid two people uploading the same item. Persists to
    history so it survives restarts and shows in everyone's list."""
    check_auth(request)
    job_dir = JOBS_DIR / job_id
    in_history = any(h.get("job_id") == job_id for h in load_history())
    if job_id not in jobs and not in_history and not job_dir.exists():
        raise HTTPException(404, detail="item not found")
    if job_id in jobs:
        jobs[job_id]["uploaded"] = body.uploaded
    update_history(job_id, {"uploaded": body.uploaded})
    return {"ok": True, "uploaded": body.uploaded}


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
    # Delete the files on disk FIRST. If this fails, surface the error and
    # keep the history entry — never leave the site and the box out of sync
    # (i.e. entry gone from the UI but files still on the server).
    job_dir = JOBS_DIR / job_id
    if job_dir.exists():
        try:
            shutil.rmtree(job_dir)
        except OSError as e:
            raise HTTPException(500, detail=f"could not delete files on server: {e}")
    # Files are gone — now drop the history entry
    history = load_history()
    history = [h for h in history if h["job_id"] != job_id]
    save_history(history)
    # ...and drop the in-memory record too. Without this the job keeps reporting
    # status=completed with a stale file list for files that no longer exist, so
    # a deleted item reappears as a finished row in the batch tray (which reads
    # /api/queue) with a download that 404s.
    jobs.pop(job_id, None)
    if job_id in queue_order:
        queue_order.remove(job_id)
    remove_pending(job_id)
    return {"ok": True}


# Serve frontend static files with no-cache headers (must be last)
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        # Vendored third-party libs are version-pinned and large — cache hard
        # so the ~1 MB model-viewer isn't re-fetched every session.
        if path.startswith("/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.endswith((".html", ".js", ".css")) or path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["CDN-Cache-Control"] = "no-cache"
        return response

app.add_middleware(NoCacheStaticMiddleware)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
