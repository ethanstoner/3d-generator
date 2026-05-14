# 3d generator

A small web app that turns an image into a 3D model (GLB). Friends drop in a PNG/JPG/WEBP, wait ~80 seconds, and download a textured `.glb`, an untextured `.glb`, and the texture map.

## screenshots

| password gate | main app |
| --- | --- |
| ![homepage](docs/screenshots/homepage.png) | ![main app](docs/screenshots/main-app.png) |

A finished generation, loaded back from history:

![past generation](docs/screenshots/past-generation.png)

## how it works

- **Frontend** — single-page vanilla HTML/CSS/JS, Google `<model-viewer>` for the 3D preview
- **Backend** — FastAPI with an async queue worker (one job at a time, position + ETA reported to the client)
- **3D pipeline** — a ComfyUI workflow running Hunyuan3D 2.1 image-to-3D on a separate GPU box
- **Auth** — shared password, signed cookie

## run it

```bash
cd backend
python -m venv ../venv
../venv/bin/pip install -r requirements.txt
# edit .env: SITE_PASSWORD, SECRET_KEY, COMFYUI_URL
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8090
```

`.env` lives at the project root:

```
SITE_PASSWORD=your-password
SECRET_KEY=something-random
COMFYUI_URL=http://your-comfyui-host:8188
```

Then open `http://localhost:8090`.

## endpoints

| method | path | description |
| --- | --- | --- |
| `POST` | `/api/auth` | validate password, set cookie |
| `GET` | `/api/status` | ComfyUI online check |
| `POST` | `/api/generate` | submit an image, returns `job_id` |
| `GET` | `/api/jobs/{id}` | poll status, progress, queue position |
| `GET` | `/api/jobs/{id}/files/{name}` | serve textured.glb / untextured.glb / texture.png |
| `GET` | `/api/jobs/{id}/download` | zip of all outputs |
| `GET` | `/api/history` | past generations |
| `DELETE` | `/api/history/{id}` | delete a past generation |
