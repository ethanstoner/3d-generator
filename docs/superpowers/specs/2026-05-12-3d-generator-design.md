# 3D Generator - Design Spec

## Overview

A simple web app that lets friends generate 3D models (GLB) from text prompts or image uploads, powered by a local ComfyUI instance with an RTX 4090. Accessed via Cloudflare tunnel.

## Architecture

- **Backend:** FastAPI (Python)
- **Frontend:** Vanilla HTML/CSS/JS (single page), Google `<model-viewer>` web component
- **3D Pipeline:** ComfyUI at `127.0.0.1:8188` (Flux text-to-image + Hunyuan3D 2.1 image-to-3D)
- **Auth:** Shared password, cookie-based
- **Deployment:** Cloudflare tunnel for external access

## Style

Matches the lyric generator aesthetic:
- White background (`#ffffff`), black text (`#000000`)
- Inter font family
- 1px `#e0e0e0` borders, `8px` border-radius
- Black buttons with white text, 0.8 opacity on hover
- Thin black progress bar on `#f0f0f0` track
- Centered layout, max-width 600px (slightly wider than lyric generator's 520px to fit 3D viewer)
- Minimal, no framework
- `<model-viewer>` via CDN: `https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js`

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/auth` | No | Validate password, set cookie |
| `GET` | `/api/models` | Yes | List available checkpoints from ComfyUI |
| `POST` | `/api/generate` | Yes | Submit text-to-3D or image-to-3D job |
| `GET` | `/api/jobs/{id}` | Yes | Poll job status + progress percentage |
| `GET` | `/api/jobs/{id}/files/{filename}` | Yes | Serve individual output file (for model-viewer) |
| `GET` | `/api/jobs/{id}/download` | Yes | Download ZIP of all outputs |

### Request/Response Schemas

**POST `/api/auth`**
- Request: `{ "password": "..." }`
- Response: `{ "ok": true }` + sets signed cookie
- Error: `{ "error": "invalid password" }` (401)

**GET `/api/models`**
- Response: `{ "online": true, "models": ["flux1-dev-fp8.safetensors", ...] }`
- Offline: `{ "online": false, "models": [] }`

**POST `/api/generate`**
- Text mode: JSON `{ "mode": "text", "model": "flux1-dev-fp8.safetensors", "prompt": "..." }`
- Image mode: multipart form — field `mode` = `"image"`, field `file` = image upload (PNG/JPG/WEBP, max 20MB)
- Image is uploaded to ComfyUI via `POST http://127.0.0.1:8188/upload/image` before workflow submission
- Response: `{ "job_id": "abc123" }`
- Busy: `{ "error": "gpu is busy, try again shortly" }` (429)
- Offline: `{ "error": "gpu is offline" }` (503)

**GET `/api/jobs/{id}`**
- Response: `{ "status": "running", "progress": 0.4, "stage": "generating image", "step": 12, "total_steps": 30 }`
- Completed: `{ "status": "completed", "files": ["textured.glb", "untextured.glb", "texture.png"] }`
- Failed: `{ "status": "failed", "error": "..." }`

## Authentication

- `.env` contains `SITE_PASSWORD=...` and `SECRET_KEY=...` (random string for cookie signing)
- `POST /api/auth` validates password, sets `session` cookie (signed via `itsdangerous`, httponly)
- All other endpoints check for valid cookie, return 401 if missing/invalid
- Frontend shows password gate on load, hides it after auth

## Frontend Flow

### Password Gate
- Centered card: password input + "enter" button
- On success: cookie set, gate hidden, main UI shown

### Main UI (single column, centered)
1. **Header:** "3d generator" title + subtitle
2. **GPU status:** Green/red dot + "gpu online"/"gpu offline" text
3. **Mode tabs:** "text to 3d" / "image to 3d" (underline-style active indicator)
4. **Input area:**
   - Text mode: model dropdown (populated from `/api/models`) + prompt textarea
   - Image mode: dashed-border drag-and-drop / click-to-upload area with image preview
   - "generate" button (disabled when GPU offline or job in progress)
5. **Progress section** (hidden when idle):
   - Thin black progress bar (same as lyric generator)
   - Gray status text ("generating image... step 12/30")
6. **Preview section** (hidden until job completes):
   - Three text tabs: "textured" / "untextured" / "texture"
   - Textured + Untextured tabs: `<model-viewer>` with auto-rotate, orbit controls, zoom
   - Texture tab: flat PNG image display
7. **Download button:** Black "download zip" button

## ComfyUI Integration

### Health Check
- Backend pings `http://127.0.0.1:8188/system_stats` with 3-second timeout
- Frontend polls `/api/models` on load and every 30 seconds; if unreachable, shows "GPU Offline"
- Job submission returns error if ComfyUI is down

### Model Discovery
- `GET http://127.0.0.1:8188/object_info/CheckpointLoaderSimple` returns available checkpoints
- Backend parses and returns model list to frontend for dropdown

### Workflow Templates
Two JSON workflow templates stored in backend:

**Text-to-3D workflow:**
1. CheckpointLoaderSimple (user-selected model)
2. CLIPTextEncode (user prompt)
3. KSampler (25 steps, CFG 1.0, dpmpp_2m) — CFG 1.0 is correct for Flux models
4. VAE Decode
5. Hunyuan3D (30 steps, guidance 8.5)
6. GLB output (textured + untextured + texture PNG)

**Image-to-3D workflow:**
1. LoadImage (user-uploaded image)
2. Hunyuan3D (30 steps, guidance 8.5)
3. GLB output (textured + untextured + texture PNG)

Backend swaps in user parameters before submitting to ComfyUI `/prompt` endpoint.

### Progress Tracking
- Backend opens one persistent WebSocket to `ws://127.0.0.1:8188/ws?clientId={client_id}` on startup
- Uses a unique `client_id` to match progress events to submitted jobs (ComfyUI tags progress messages with the `client_id` from the `/prompt` submission)
- Reconnects automatically if the WebSocket drops
- **Two-stage progress for text-to-3D:** image generation (0-45%) + 3D generation (45-100%). Status text updates to reflect current stage ("generating image... step 12/25" then "generating 3d model... step 8/30")
- **Single-stage progress for image-to-3D:** 3D generation only (0-100%)
- Frontend polls `GET /api/jobs/{id}` every 2 seconds

### Output Collection
- On job completion, backend calls `GET http://127.0.0.1:8188/history/{prompt_id}` to get output filenames
- Downloads each output file via `GET http://127.0.0.1:8188/view?filename={name}&subfolder={sub}&type=output`
- Files saved to `jobs/{job_id}/` directory (renamed to standard names):
  - `textured.glb`
  - `untextured.glb`
  - `texture.png`
- ZIP created on demand for download

## Job Lifecycle

```
queued -> running -> completed
                  -> failed
```

- **queued:** Submitted to ComfyUI, got `prompt_id`
- **running:** WebSocket reports progress steps
- **completed:** Outputs collected and saved
- **failed:** Error message stored and shown

## Concurrency

- One job at a time (single GPU)
- If a job is already running, new submissions return "GPU is busy, try again shortly"
- No queue — fail fast, user retries manually

## File Structure

```
3d-generator/
  backend/
    main.py              # FastAPI app, all endpoints, serves frontend/ as static files at /
    comfyui.py           # ComfyUI client (REST + WebSocket)
    workflows/
      text_to_3d.json    # Flux -> Hunyuan3D workflow template
      image_to_3d.json   # Image -> Hunyuan3D workflow template
    requirements.txt     # fastapi, uvicorn, python-dotenv, httpx, websockets
  frontend/
    index.html           # Single page app
    style.css            # Lyric generator-matching styles
    app.js               # All frontend logic
  jobs/                  # Generated output files (gitignored)
  .env                   # SITE_PASSWORD=..., SECRET_KEY=...
  .gitignore
  start.bat              # Starts FastAPI on port 8080
```

## Startup Flow

1. User runs `run_nvidia_gpu.bat` to start ComfyUI
2. User runs `start.bat` in project root to start FastAPI
3. User starts Cloudflare tunnel pointing to `localhost:8080`
4. Friends access the tunnel URL, enter shared password, generate

## Dependencies

- fastapi
- uvicorn[standard]
- python-dotenv
- httpx (for ComfyUI REST calls)
- websockets (for ComfyUI progress tracking)
- itsdangerous (for cookie signing)

## Cleanup

- Jobs older than 24 hours are deleted on server startup
- `jobs/` directory is gitignored
