"""Smoke tests for the non-GPU surface: auth, validation, the pure helpers, and
the pending-queue round trip. Nothing here needs ComfyUI, Ollama or a GPU, so it
runs on a clean clone. JOBS_DIR is redirected to a tmp dir before startup so the
real jobs/ directory is never touched.
"""
import os
import pytest

os.environ.setdefault("SITE_PASSWORD", "test-pw")
os.environ.setdefault("SECRET_KEY", "test-key")
os.environ.setdefault("COMFYUI_URL", "http://127.0.0.1:59999")  # deliberately dead
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:59998")

from fastapi.testclient import TestClient
from backend import main, llm

PASSWORD = os.environ["SITE_PASSWORD"]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    jobs = tmp_path_factory.mktemp("jobs")
    main.JOBS_DIR = jobs
    main.HISTORY_FILE = jobs / "history.json"
    main.PENDING_FILE = jobs / "pending.json"
    main.PROMPT_HISTORY_FILE = jobs / "prompt_history.json"
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def auth(client):
    client.post("/api/auth", json={"password": PASSWORD})
    return client


# --- pure helpers ---

@pytest.mark.parametrize("raw,expected", [
    ("Chubby Capybara! \U0001f9a6", "Chubby_Capybara"),
    ("\U0001f9a6!!!", ""),
    ("  spaced   out  ", "spaced_out"),
])
def test_safe_filename(raw, expected):
    assert main._safe_filename(raw) == expected


def test_safe_filename_has_no_path_separators():
    for raw in ("../../etc/passwd", "..\\windows\\system32"):
        out = main._safe_filename(raw)
        assert "/" not in out and "\\" not in out


def test_finalize_description_appends_store_cta():
    assert llm._finalize_description("cozy capybara").endswith(llm.MORE_SUFFIX)


@pytest.mark.parametrize("raw", [
    "cute thing - More: https://evil.example/x",
    "bare url http://evil.example/x",
])
def test_finalize_description_strips_model_invented_links(raw):
    out = llm._finalize_description(raw)
    assert "evil.example" not in out
    assert out.endswith(llm.MORE_SUFFIX)


def test_finalize_description_caps_length():
    out = llm._finalize_description("word " * 200)
    assert len(out) <= 160 + len(llm.MORE_SUFFIX) + 4


@pytest.mark.parametrize("filename,body,ok", [
    ("x.png", b"abc", True),
    ("x.jpg", b"abc", True),
    ("x.webp", b"abc", True),
    ("x.gif", b"abc", False),
    ("noext", b"abc", False),
    ("x.png", b"", False),
])
def test_validate_upload(filename, body, ok):
    assert (main._validate_upload(filename, body) == "") is ok


def test_validate_upload_rejects_oversized():
    # built here, not parametrized - pytest renders params into the test id
    assert main._validate_upload("x.png", b"a" * (21 * 1024 * 1024)) != ""


# --- auth ---

def test_unauthenticated_check_auth_is_falsy(client):
    assert not client.get("/api/check-auth").json().get("authenticated")


def test_protected_endpoint_requires_auth(client):
    client.cookies.clear()
    assert client.get("/api/history").status_code == 401


def test_auth_rejects_wrong_password(client):
    assert client.post("/api/auth", json={"password": "nope"}).status_code == 401


def test_auth_sets_httponly_cookie(client):
    r = client.post("/api/auth", json={"password": PASSWORD})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()


def test_cookie_not_secure_over_plain_http(client):
    r = client.post("/api/auth", json={"password": PASSWORD})
    assert "secure" not in r.headers["set-cookie"].lower()


def test_cookie_secure_behind_https_proxy(client):
    r = client.post("/api/auth", json={"password": PASSWORD},
                    headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()
    client.post("/api/auth", json={"password": PASSWORD})  # restore a usable session


# --- endpoints (GPU offline) ---

def test_history_returns_list(auth):
    r = auth.get("/api/history")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_status_reports_gpu_offline(auth):
    assert auth.get("/api/status").json()["online"] is False


def test_queue_endpoint_shape(auth):
    body = auth.get("/api/queue").json()
    assert "active" in body and isinstance(body["jobs"], list)


def test_generate_503s_when_gpu_offline(auth):
    r = auth.post("/api/generate", data={"mode": "image", "triangles": 4000},
                  files={"file": ("a.png", b"x", "image/png")})
    assert r.status_code == 503


@pytest.mark.parametrize("job_ids", [[], ["x"] * 51])
def test_download_multi_rejects_bad_batches(auth, job_ids):
    assert auth.post("/api/download-multi", json={"job_ids": job_ids}).status_code == 400


@pytest.mark.parametrize("path,body", [
    ("/api/jobs/deadbeef/meta", {"name": "n", "description": "d"}),
    ("/api/jobs/deadbeef/uploaded", {"uploaded": True}),
])
def test_unknown_job_404s(auth, path, body):
    assert auth.post(path, json=body).status_code == 404


def test_research_prompt_is_served(auth):
    assert len(auth.get("/api/research-prompt").json()["prompt"]) > 50


def test_frontend_index_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"<title>" in r.content


# --- pending queue (restart survival) ---

def test_pending_queue_round_trip(client):
    main.save_pending([])
    main.append_pending({"job_id": "aaa", "input_path": "x.png", "triangles": 4000})
    main.append_pending({"job_id": "bbb", "input_path": "x.png", "triangles": 4000})
    assert [r["job_id"] for r in main.load_pending()] == ["aaa", "bbb"]
    main.remove_pending("aaa")
    assert [r["job_id"] for r in main.load_pending()] == ["bbb"]
    main.save_pending([])


def test_backpack_rules_load_without_claude_md():
    """The rules must ship with the repo - llm.py used to read CLAUDE.md, which
    is untracked, so every fresh clone failed at import."""
    assert llm.RULES_FILE.exists()
    assert len(llm.BACKPACK_RULES) > 500
