# Changelog

All notable changes to this project. Dates are when the change went live on
**https://qorlyt.com/**. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/).

## 2026-08-22

### Added
- **Bulk upload** — queue up to 50 images in one go (`POST /api/generate-batch`)
  and poll the whole batch with a single `GET /api/queue`. A batch tray shows
  what is waiting, rendering and done, with filters, a grid/list toggle and a
  collapse state that persists across reloads. Selected results download as one
  zip (`POST /api/download-multi`), a folder per item.
- **Restart-surviving queue** — queued work is mirrored to `jobs/pending.json`
  and replayed in order on startup. Uploads are staged to disk at enqueue time,
  so the queue carries a path instead of pinning every queued image in RAM.
- **Editable item metadata** — name and description are now text fields that
  persist (`POST /api/jobs/{id}/meta`); AI naming fills them in rather than
  being the only source. Single-item zips are named after the item.
- **Uploaded-to-Roblox flag** — a shared marker (`POST /api/jobs/{id}/uploaded`)
  so the group doesn't upload the same item twice.
- **History thumbnails** — `GET /api/jobs/{id}/thumb` serves the input image;
  the baked UV atlas was unreadable at 48px.
- **How-to guide** listing the required Roblox Studio plugins.
- **Smoke tests** (`tests/test_smoke.py`) covering auth, upload validation, the
  filename/description helpers and the pending-queue round trip. No GPU needed.

### Fixed
- **A fresh clone could not start at all.** `backend/llm.py` read its prompt
  rules out of `CLAUDE.md`, which is deliberately untracked, and raised at
  import time when it was missing. The rules now ship in
  `backend/rules/backpack_rules.md`.
- **Session cookie is now `Secure`** when the request arrives over HTTPS, taken
  from `X-Forwarded-Proto` since Cloudflare terminates TLS upstream.
- **Vision model described the prompt, not the image.** Concrete example
  subjects in the system prompt were being parroted, so unrelated items came
  back named after them. Examples removed; the store link is appended in code.
- **Queue ETA was 12% short** — it assumed 80s per job where the measured median
  across 60 runs is 90.5s.
- Deleting from history now also drops the in-memory job, which otherwise kept
  reporting a completed status for files that no longer existed.
- The launcher no longer hardcodes one machine's Ollama and ComfyUI paths.
- Backend dependencies are pinned.

## 2026-05-15

### Added
- **AI item naming** — a "name this item" button runs the input image through
  a local vision model (`llama3.2-vision:11b`, on-demand, under the shared GPU
  lock) and returns a Roblox-style name + description. Input images are now
  persisted per job so items can be (re)named later, including from history.
  `POST /api/jobs/{id}/name`.
- **Prompt-help history** — successful prompt-help results are saved server-side
  (shared, capped at 50) and browsable from the prompt-help modal.
  `GET/DELETE /api/prompt-history`.
- **Model stats line** — every result shows triangle count, file size, and
  generation time under the viewer (live generations and new history entries).
- **Paste-from-clipboard** — Ctrl+V an image anywhere on the page to load it.
- **Retry button** — re-run a failed generation from the error state without
  re-selecting the image.

### Changed
- **Much faster model loading.** `@google/model-viewer` is now self-hosted and
  served same-origin with an `immutable` cache instead of fetched from the
  unpkg CDN on every visit; hidden preview tabs (untextured / texture) load
  lazily on first open instead of competing for bandwidth; per-job files send
  `Cache-Control: ...immutable` so re-viewing and history browsing are
  instant; the textured viewer shows the texture as a poster during WebGL
  warm-up. First load dropped from ~5s to well under 1s.
- Progress bar glides across the 2s poll interval instead of snapping.

### Fixed
- **History delete now reliably removes files from the server.** Dropped
  `rmtree(ignore_errors=True)` so a failed delete surfaces instead of
  silently leaving orphaned files while removing the entry from the UI;
  files are deleted before the history entry so the site and disk never
  drift apart. Failed generations now clean up their own folder, and a
  startup sweep removes empty orphan job folders (non-empty untracked
  folders are kept and logged, never auto-destroyed).
- **Live site "gpu offline" while local worked.** The Linux backend hardcoded
  the Windows GPU box's DHCP-assigned LAN IP, which silently broke on a lease
  change. Now addressed by a stable hostname so DHCP changes no longer
  take the site down.
- Spacing of the model stats line so it no longer crowds the download button.

## 2026-05-14

### Added
- **Prompt helper.** Describe a rough idea and get back a polished
  name / description / image-generation prompt (Ollama `llama3.2:3b`), built
  to the backpack/UGC generation rules. Plus a one-click "research prompt"
  preset to paste into Claude/ChatGPT/Gemini with web search.
- **Triangle count presets** — choose mesh density (4k / 10k / 20k / 40k);
  choice persists and shows as a badge in history.
- Unified launcher (`start_all`) — starts Ollama + ComfyUI + backend with
  all-or-nothing semantics; `stop_all` to tear down.
- Upload tip + queue ETA for friend onboarding.

### Changed
- GPU lock serializes ComfyUI 3D jobs and Ollama prompt-help calls so they
  never compete for VRAM on the shared GPU.
- Prompt-help is gated on ComfyUI being online; refine button disables when
  the GPU is offline.
- UI polish: texture viewer locked to 400px, prompt-helper layout no longer
  jumps, prompt-helper buttons restyled as outline (not chunky CTAs).

## 2026-05-13

### Added
- **Persistent generation history** — every result stored on disk with
  thumbnails; re-open any past model or delete it.
- **Job queue** — multiple friends can submit at once; jobs run one at a time
  with live queue-position tracking.

### Fixed
- Correctly identify the new untextured GLB by diffing the output directory
  before/after generation, with an HTTP fallback when the filesystem isn't
  reachable from the Linux box.

## 2026-05-12

### Added
- Initial release: image → 3D (GLB) via local ComfyUI + Hunyuan3D 2.1.
- Real-time progress with detailed stage labels and step counts over a
  persistent WebSocket to ComfyUI.
- Three preview tabs (textured / untextured / texture map), zip download.
- Shared-password auth (signed 7-day cookie).
