let selectedFile = null;
let activeJobId = null;
let pollInterval = null;
let statusInterval = null;
let gpuOnline = false;

const ALLOWED_TRIANGLES = [4000, 10000, 20000, 40000];
let selectedTriangles = parseInt(localStorage.getItem('triangles') || '4000', 10);
if (!ALLOWED_TRIANGLES.includes(selectedTriangles)) selectedTriangles = 4000;

function selectTriangles(n) {
    selectedTriangles = n;
    localStorage.setItem('triangles', String(n));
    document.querySelectorAll('.preset-chip').forEach(c => {
        c.classList.toggle('active', parseInt(c.dataset.value, 10) === n);
    });
}

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
    selectTriangles(selectedTriangles);
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
    fd.append('triangles', String(selectedTriangles));

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
        const pct = Math.round(data.progress * 100);
        document.getElementById('progress-fill').style.width = pct + '%';
        let statusMsg;
        const progressBar = document.getElementById('progress-fill');
        if (data.status === 'queued') {
            const etaSec = data.queue_position * 80;
            const etaStr = etaSec >= 60
                ? `~${Math.floor(etaSec / 60)}m ${etaSec % 60}s`
                : `~${etaSec}s`;
            if (data.queue_position > 1) {
                statusMsg = `waiting in queue (position ${data.queue_position}) — ${etaStr}`;
            } else {
                statusMsg = `waiting for gpu — ${etaStr}`;
            }
            progressBar.classList.add('pulsing');
        } else {
            progressBar.classList.remove('pulsing');
            statusMsg = `${pct}% — ${data.stage}`;
            if (data.total_steps > 0) {
                statusMsg += ` (${data.step}/${data.total_steps})`;
            }
        }
        document.getElementById('status-text').textContent = statusMsg;

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

// --- History ---
let historyOpen = false;

function toggleHistory() {
    historyOpen = !historyOpen;
    const list = document.getElementById('history-list');
    if (historyOpen) {
        list.classList.remove('hidden');
        loadHistory();
    } else {
        list.classList.add('hidden');
    }
}

async function loadHistory() {
    const list = document.getElementById('history-list');
    try {
        const r = await fetch('/api/history');
        const data = await r.json();
        if (!data.length) {
            list.innerHTML = '<p class="history-empty">no generations yet</p>';
            return;
        }
        list.innerHTML = data.map(item => {
            const date = new Date(item.timestamp * 1000);
            const dateStr = date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
            const triLabel = item.triangles
                ? (item.triangles >= 1000 ? Math.round(item.triangles / 1000) + 'k' : String(item.triangles)) + ' tris'
                : '';
            const triBadge = triLabel ? `<span class="history-tris">${triLabel}</span>` : '';
            const hasThumb = item.files && item.files.includes('texture.png');
            return `<div class="history-item" onclick="loadFromHistory('${item.job_id}', ${JSON.stringify(item.files).replace(/"/g, '&quot;')})">
                ${hasThumb ? `<img class="history-thumb" src="/api/jobs/${item.job_id}/files/texture.png" alt="">` : '<div class="history-thumb"></div>'}
                <div class="history-info">
                    <div class="history-name">${item.filename || item.job_id}</div>
                    <div class="history-date">${dateStr}${triBadge}</div>
                </div>
                <button class="history-delete" onclick="event.stopPropagation(); deleteHistory('${item.job_id}')" title="delete">×</button>
            </div>`;
        }).join('');
    } catch (e) {
        list.innerHTML = '<p class="history-empty">failed to load history</p>';
    }
}

function loadFromHistory(jobId, files) {
    showPreview(jobId, files);
    document.getElementById('progress-section').classList.add('hidden');
    document.getElementById('error-section').classList.add('hidden');
}

async function deleteHistory(jobId) {
    await fetch(`/api/history/${jobId}`, { method: 'DELETE' });
    loadHistory();
}

// --- Prompt Helper ---
let cachedResearchPrompt = null;

async function refinePrompt() {
    const idea = document.getElementById('ph-idea').value.trim();
    const errEl = document.getElementById('ph-error');
    const resultEl = document.getElementById('ph-result');
    const btn = document.getElementById('ph-refine-btn');
    errEl.classList.add('hidden');
    if (!idea) { errEl.textContent = 'type an idea first'; errEl.classList.remove('hidden'); return; }
    btn.disabled = true;
    btn.textContent = 'thinking...';
    try {
        const r = await fetch('/api/prompt-help', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ idea }),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            throw new Error(d.detail || 'request failed');
        }
        const data = await r.json();
        document.getElementById('ph-name').textContent = data.name;
        document.getElementById('ph-description').textContent = data.description;
        document.getElementById('ph-image-prompt').textContent = data.image_prompt;
        resultEl.classList.remove('hidden');
    } catch (e) {
        errEl.textContent = e.message;
        errEl.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        btn.textContent = 'generate prompts';
    }
}

async function copyResearchPrompt() {
    const btn = document.getElementById('ph-research-btn');
    const original = btn.textContent;
    try {
        if (!cachedResearchPrompt) {
            const r = await fetch('/api/research-prompt');
            if (!r.ok) throw new Error('failed to load');
            cachedResearchPrompt = (await r.json()).prompt;
        }
        await navigator.clipboard.writeText(cachedResearchPrompt);
        btn.textContent = 'copied!';
        setTimeout(() => { btn.textContent = original; }, 1500);
    } catch (e) {
        btn.textContent = 'copy failed';
        setTimeout(() => { btn.textContent = original; }, 1500);
    }
}

document.addEventListener('click', (e) => {
    if (!e.target.classList || !e.target.classList.contains('ph-copy')) return;
    const targetId = e.target.dataset.target;
    const text = document.getElementById(targetId).textContent;
    const original = e.target.textContent;
    navigator.clipboard.writeText(text).then(() => {
        e.target.textContent = 'copied!';
        setTimeout(() => { e.target.textContent = original; }, 1500);
    }).catch(() => {
        e.target.textContent = 'failed';
        setTimeout(() => { e.target.textContent = original; }, 1500);
    });
});
