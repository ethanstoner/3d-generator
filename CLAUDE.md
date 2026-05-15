# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Friends-only, password-gated web app that turns an image into a downloadable GLB via a local ComfyUI + Hunyuan3D 2.1 pipeline. Adds an Ollama-backed "prompt helper" that refines rough ideas into polished image-generation prompts (for friends to paste into ChatGPT/Gemini/Claude).

## Architecture

Three processes work together; they all share one RTX 4090:

- **ComfyUI** (Windows, port 8188) — runs the Hunyuan3D 2.1 image→3D workflow defined in `backend/workflows/image_to_3d.json`. Started via the portable launcher in `D:\Cursor\AI\ComfyUI_windows_portable_nvidia\...`.
- **Ollama** (Windows, port 11435 — *not* default 11434) — serves `llama3.2:3b` for the prompt helper. Custom port comes from `OLLAMA_HOST` user env var; bound to `0.0.0.0` so the Linux backend can reach it over the LAN.
- **FastAPI backend** (`backend/main.py`) — runs locally on port 8080 (dev) and on the Linux box (`192.168.1.169`) port 8090 (prod). Auth-gated by signed cookie. Holds an `asyncio.Queue` for 3D jobs (one at a time) and an `asyncio.Lock` (`gpu_lock`) that **serializes ComfyUI jobs and Ollama prompt-help calls so they never compete for VRAM**.

Frontend is single-file vanilla HTML/CSS/JS (`frontend/`), no build step. Cache-busted via `?v=N` on `/style.css` and `/app.js` — bump on every change.

### Key flows

- `POST /api/generate` → enqueues; queue worker holds `gpu_lock`, mutates `workflow["30"]["inputs"]["value"] = triangles` (postprocess `max_facenum`), submits to ComfyUI, streams progress via WebSocket.
- `POST /api/prompt-help` → checks ComfyUI is online (same GPU rule), tries to acquire `gpu_lock` with 90s timeout, calls `llm.refine_idea`. Ollama payload uses `"format": "json"` for guaranteed parseable output and `"keep_alive": 0` so the model unloads from VRAM after responding.
- `GET /api/research-prompt` → returns a static preset (no LLM call); friend pastes into Claude/ChatGPT/Gemini.

### Load-bearing behavior

- `backend/llm.py` reads the `## Backpack Generation Rules` section from this file at import time using regex `^##\s+Backpack Generation Rules\s*\n(.*?)(?=^##\s|\Z)`. **Never rename that heading or insert another `## ` heading inside it** — both modes (LLM system prompt + research preset) source from this single block.
- `backend/llm.py` enforces that every `image_prompt` ends with the `NEGATIVE_CLAUSE` ("No straps, no zippers, ... just a single solid sculpted figurine.") for Roblox UGC safety. The few-shot examples demonstrate the pattern — keep them in sync if the clause changes.
- The triangle-count chips (4k/10k/20k/40k) are validated server-side against `ALLOWED_TRIANGLES` in `backend/main.py`. Adding a value requires updating both the set and the frontend chip list.

## Commands

**Start everything (Windows, all-or-nothing):**
```
start_all.bat
```
Launches Ollama + ComfyUI + backend. Detects already-running services. If anything fails, kills whatever this script started. Logs go to `.launcher-logs/`. Stop everything with `stop_all.bat`.

**Local dev only (backend with reload):**
```
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8080 --reload
```
Requires `.env` (see below) and ComfyUI + Ollama already running.

**Required `.env` at project root** (gitignored):
```
SITE_PASSWORD=...
SECRET_KEY=...
COMFYUI_URL=http://127.0.0.1:8188
COMFYUI_OUTPUT_DIR=D:/Cursor/AI/ComfyUI_windows_portable_nvidia/ComfyUI_windows_portable/ComfyUI/output
OLLAMA_URL=http://127.0.0.1:11435     # optional; defaults to this
OLLAMA_MODEL=llama3.2:3b              # optional; defaults to this
```

**No automated tests.** Verification is manual via `curl`/PowerShell + browser.

## Deployment

Live site: **https://qorlyt.com/** (Cloudflare Tunnel → Linux box `192.168.1.169:8090`).

The Linux backend reaches ComfyUI and Ollama on the Windows PC over the LAN by **hostname**, not IP — the Windows PC's IP is DHCP-assigned and a lease change (e.g. `.83`→`.84`) silently breaks the live site ("gpu offline" on qorlyt.com while local works fine). The router's DNS keeps the hostname (`jeffy`) → IP mapping current across lease changes. Linux `.env` has:
```
COMFYUI_URL=http://jeffy:8188
OLLAMA_URL=http://jeffy:11435
```
If `jeffy` ever stops resolving from the Linux box, fall back to the current LAN IP (find it on Windows with `Get-NetIPAddress -AddressFamily IPv4`) and update both URLs.

**Deploy code change to live:**
```bash
scp <changed-files> linux-box:~/3d-generator/<dest>/
ssh linux-box "ps aux | grep 'uvicorn.*8090' | grep -v grep | awk '{print \$2}' | xargs -r kill && \
  sleep 2 && cd ~/3d-generator && \
  nohup venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8090 > /tmp/3d-gen-backend.log 2>&1 & disown"
```
Frontend-only changes don't need a backend restart (StaticFiles reads from disk per request) — but bump the `?v=` cache-buster in `index.html` so browsers pick up the new assets.

## Backpack Generation Rules

These rules govern prompt creation for image-generation models (ChatGPT / Gemini / Claude image gen). The image is then fed into Hunyuan3D 2.1 to produce a GLB used as a Roblox UGC back accessory.

### IDEA SELECTION — what makes a good backpack

Must be trending RIGHT NOW:
- Currently airing/recently released anime, TV shows, movies
- Trending games (especially what Roblox kids are playing — Steal a Brainrot, Grow A Garden, Fisch, etc.)
- Pop Mart / blind box / collectible toy trends (Labubu, Sonny Angel, Skullpanda, etc.)
- Viral memes (Italian Brainrot characters)
- Upcoming holidays & seasonal events
- Cultural moments / music releases / viral TikTok stuff

Must appeal to Roblox audience:
- Skews younger (Gen Alpha)
- Brand-recognizable IPs > generic concepts
- Cute/plush/kawaii sells huge
- Funny/meme characters sell huge

Must fit the proven aesthetic:
- Smooth glossy sculptural OR simple rounded companion
- Chunky toy-like proportions
- High visual recognizability at small scale

### SUBJECT RULES — what the backpack itself can be

ONE single subject only. Never combine two items (no "book AND sword" — pick one).

Frame it as a plush/sculpted figurine character, not a traditional backpack. The Roblox attachment is handled in-engine — the 3D mesh is just the standalone toy form.

Good silhouettes:
- Plush characters in sitting/upright poses
- Smooth glossy sculptural shapes
- Rounded companion creatures with simple features
- Chunky geometry, thick proportions

### NEVER include these (image→3D pipeline can't handle them)

- Straps, strap loops, sculpted straps
- Zippers, buckles, clips, mechanical hardware
- Fabric, cloth, capes — these don't convert to 3D
- Flame effects, fire, glow effects
- Particles, transparency, wispy/thin elements
- Cards, flat thin objects
- Baskets/containers with many small items inside
- Rocky/rough textures, crystal shards
- Multiple subjects in one image
- Fine mechanical detail

### IMAGE GENERATION RULES

Background: Solid white background (clean cutout). No environment, no scenery, no props besides the subject.

Composition: Front-facing, centered in frame, soft diffused studio lighting.

Style anchors that work:
- "premium Pop Mart vinyl plush"
- "high-end collectible toy"
- "smooth glossy figurine"
- "premium Funko Pop"
- "high-quality vinyl figure"
- "premium Japanese arcade prize plush"

Surface descriptors: smooth, glossy, matte, fuzzy, plush, chunky toy proportions, exaggerated rounded proportions.

### PROMPT STRUCTURE

Natural language, NOT comma-separated tags. 50–150 words. Describe like explaining to a human.

Always:
1. NAME the character AND franchise (lets the AI use its own knowledge).
2. Include detailed visual backup (exact colors, shapes, proportions, materials) in case the AI gets details wrong.
3. End with composition/lighting line: "Solid white background, 3D render style, front-facing centered composition, soft studio lighting".

The prompt must be fully self-contained — never reference an "attached image", "reference image", or "based on this image".
