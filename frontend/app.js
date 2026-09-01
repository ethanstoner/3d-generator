let selectedFile = null;
let activeJobId = null;
let pollInterval = null;
let statusInterval = null;
let gpuOnline = false;

// Every picked image lives here as { id, file, url }. `url` is an object URL used
// for the local thumbnail and revoked as soon as the entry is dropped, so a
// 30-image selection doesn't leak. One entry = the original single-image flow;
// two or more switches the upload area into bulk mode.
let selectedFiles = [];
let fileSeq = 0;

// --- Bulk batch state ---
// Declared up here (not down in the batch section) so updateGenerateButton can
// read batchBusy from the very first checkGpuStatus call.
let activeBatchId = null;
let batchPollInterval = null;
let batchBusy = false;       // batch has at least one queued/running job
let batchJobs = [];
let batchSig = '';           // last-rendered job state, so polling doesn't redraw needlessly
let batchSelected = new Set();
let batchThumbs = {};        // filename -> object URL, for local previews in the tray
// View prefs persist: a 20-image batch runs for half an hour across several
// visits, so the tray must come back the way it was left, not reset every load.
let batchCollapsed = localStorage.getItem('batchCollapsed') === '1';
let batchFilter = localStorage.getItem('batchFilter') || 'all';
let batchView = localStorage.getItem('batchView') || '';   // '' = auto (grid once it's a big batch)

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
    resumeActiveJob();
    resumeActiveBatch();
}

// If a generation was in flight when the page was last open (refresh, phone
// sleep, accidental close), pick it back up instead of dropping the live view.
async function resumeActiveJob() {
    const id = localStorage.getItem('activeJobId');
    if (!id) return;
    try {
        const r = await fetch(`/api/jobs/${id}`);
        if (!r.ok) {
            // Backend no longer knows this job (most likely a server restart,
            // which wipes the in-memory job store). If it finished and made it
            // into history we can still show it; otherwise drop the stale id.
            await resumeFromHistory(id);
            localStorage.removeItem('activeJobId');
            return;
        }
        const data = await r.json();
        const stats = {
            triangles: data.triangles, size: data.size, duration: data.duration,
            name: data.name, description: data.description, uploaded: data.uploaded,
        };
        if (data.status === 'completed') {
            localStorage.removeItem('activeJobId');
            showPreview(id, data.files, stats);
        } else if (data.status === 'failed') {
            showError(data.error || 'generation failed');
        } else {
            // queued or running — re-attach the poller for live progress
            activeJobId = id;
            document.getElementById('progress-section').classList.remove('hidden');
            setProgressSticky(true);
            updateGenerateButton();
            pollJob();
            pollInterval = setInterval(pollJob, 2000);
        }
    } catch (e) {
        // transient network error — leave the id and retry on the next load
    }
}

async function resumeFromHistory(id) {
    try {
        const r = await fetch('/api/history');
        const data = await r.json();
        const item = data.find(h => h.job_id === id);
        if (item) {
            showPreview(id, item.files, {
                triangles: item.triangles, size: item.size, duration: item.duration,
                name: item.name, description: item.description, uploaded: item.uploaded,
            });
        }
    } catch (e) {}
}

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
    if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
});
fileInput.addEventListener('change', () => {
    if (fileInput.files.length) handleFiles(fileInput.files);
    fileInput.value = '';  // so re-picking the same file still fires change
});

const ALLOWED_EXT = ['png', 'jpg', 'jpeg', 'webp'];
const MAX_BYTES = 20 * 1024 * 1024;

// Returns a human-readable reason the file can't be used, or null if it's fine.
function fileProblem(file) {
    const ext = file.name.split('.').pop().toLowerCase();
    if (!ALLOWED_EXT.includes(ext)) return 'Use PNG, JPG, or WEBP';
    if (file.size > MAX_BYTES) return 'Max 20MB';
    return null;
}

// Single-file entry point, kept for the clipboard-paste handler.
function handleFile(file) { handleFiles([file]); }

function handleFiles(list) {
    const incoming = Array.from(list);
    if (!incoming.length) return;
    // Picking one image while at most one is selected replaces it — exactly the
    // old single-image behavior. Anything else adds to the pile.
    if (incoming.length === 1 && selectedFiles.length <= 1) clearSelection(true);

    const rejected = [];
    for (const file of incoming) {
        const problem = fileProblem(file);
        // A bad file is flagged, never fatal — the good ones still go through.
        if (problem) { rejected.push(`${file.name} — ${problem.toLowerCase()}`); continue; }
        const dupe = selectedFiles.some(e => e.file.name === file.name && e.file.size === file.size);
        if (dupe) continue;
        selectedFiles.push({ id: 'f' + (++fileSeq), file, url: URL.createObjectURL(file) });
    }
    // One file at a time still alerts like it always did; a multi-pick flags the
    // rejects inline instead of throwing a dialog per bad file.
    if (rejected.length && incoming.length === 1) alert(fileProblem(incoming[0]));
    renderRejected(incoming.length === 1 ? [] : rejected);
    renderSelection();
}

function renderRejected(rejected) {
    const el = document.getElementById('bulk-invalid');
    if (!rejected.length) { el.classList.add('hidden'); el.textContent = ''; return; }
    el.textContent = `skipped ${rejected.length} file${rejected.length > 1 ? 's' : ''}: ` + rejected.join(', ');
    el.classList.remove('hidden');
}

function clearSelection(skipRender) {
    selectedFiles.forEach(e => URL.revokeObjectURL(e.url));
    selectedFiles = [];
    if (!skipRender) { renderRejected([]); renderSelection(); }
}

function clearBulkSelection() { clearSelection(); }

function removeSelected(id) {
    const i = selectedFiles.findIndex(e => e.id === id);
    if (i === -1) return;
    URL.revokeObjectURL(selectedFiles[i].url);
    selectedFiles.splice(i, 1);
    renderSelection();
}

// One image = the original preview + filename. Two or more = the bulk grid.
function renderSelection() {
    const n = selectedFiles.length;
    const bulk = document.getElementById('bulk-select');
    const img = document.getElementById('upload-preview');
    const text = document.getElementById('upload-text');
    const grid = document.getElementById('bulk-grid');
    selectedFile = n ? selectedFiles[0].file : null;

    if (n >= 2) {
        img.classList.add('hidden');
        img.removeAttribute('src');
        text.textContent = 'drop more images here, or click to add';
        document.getElementById('bulk-count').textContent = `${n} images ready`;
        grid.innerHTML = selectedFiles.map(e => {
            const name = escapeHtml(e.file.name);
            return `<div class="bulk-thumb" title="${name}">
                <img src="${e.url}" alt="${name}">
                <button type="button" class="bulk-thumb-x" title="remove"
                        onclick="event.stopPropagation(); removeSelected('${e.id}')">&times;</button>
            </div>`;
        }).join('');
        bulk.classList.remove('hidden');
    } else {
        bulk.classList.add('hidden');
        grid.innerHTML = '';
        if (n === 1) {
            img.src = selectedFiles[0].url;
            img.classList.remove('hidden');
            text.textContent = selectedFiles[0].file.name;
        } else {
            img.classList.add('hidden');
            img.removeAttribute('src');
            text.textContent = 'drag & drop images here, or click to browse';
        }
    }
    updateGenerateButton();
}

function updateGenerateButton() {
    const n = selectedFiles.length;
    const btn = document.getElementById('generate-btn');
    btn.disabled = !n || !gpuOnline || !!activeJobId || batchBusy;
    btn.textContent = n >= 2 ? `generate ${n} models` : 'generate';
    const ph = document.getElementById('ph-refine-btn');
    if (ph) ph.disabled = !gpuOnline;
}

// While a generation is active, pin its progress bar to the top of the
// viewport so it stays visible even when you scroll down to browse history or
// edit/save an item (otherwise the bar sits at the top of the page, off-screen).
function setProgressSticky(on) {
    document.getElementById('progress-section').classList.toggle('sticky-progress', on);
}

// --- Generate ---
async function startGeneration() {
    // Two or more images go through the batch queue instead.
    if (selectedFiles.length >= 2) { startBatchGeneration(); return; }
    document.getElementById('generate-btn').disabled = true;
    document.getElementById('progress-section').classList.remove('hidden');
    setProgressSticky(true);
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
        // Remember it so a page refresh / phone sleep can re-attach to the
        // running job instead of losing the live view (see resumeActiveJob).
        localStorage.setItem('activeJobId', data.job_id);
        updateGenerateButton();
        pollInterval = setInterval(pollJob, 2000);
    } catch (e) {
        showError('connection error');
    }
}

// Median generation time measured over 60 real runs at 4k triangles, so a queue
// position is roughly that many seconds of waiting. Shared by the single-job bar
// and the batch tray.
const SECONDS_PER_JOB = 90;

function formatEta(sec) {
    return sec >= 60 ? `~${Math.floor(sec / 60)}m ${sec % 60}s` : `~${sec}s`;
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
            const etaStr = formatEta(data.queue_position * SECONDS_PER_JOB);
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
            localStorage.removeItem('activeJobId');
            setProgressSticky(false);
            document.getElementById('progress-fill').style.width = '100%';
            document.getElementById('status-text').textContent = 'done!';
            showPreview(activeJobId, data.files, {
                triangles: data.triangles,
                size: data.size,
                duration: data.duration,
                name: data.name,
                description: data.description,
                uploaded: data.uploaded,
            });
            activeJobId = null;
            updateGenerateButton();
            // If the history panel is open, refresh it so the model we just
            // finished shows up immediately (no need to close/reopen it).
            if (historyOpen) loadHistory();
        } else if (data.status === 'failed') {
            clearInterval(pollInterval);
            showError(data.error || 'generation failed');
            activeJobId = null;
            updateGenerateButton();
        }
    } catch (e) {}
}

function retryGeneration() {
    if (!selectedFile) {
        document.getElementById('error-text').textContent = 'select an image first';
        return;
    }
    if (!gpuOnline) {
        document.getElementById('error-text').textContent = 'gpu is offline';
        return;
    }
    document.getElementById('error-section').classList.add('hidden');
    startGeneration();
}

function showError(msg) {
    document.getElementById('error-section').classList.remove('hidden');
    document.getElementById('error-text').textContent = msg;
    document.getElementById('progress-section').classList.add('hidden');
    setProgressSticky(false);
    activeJobId = null;
    localStorage.removeItem('activeJobId');
    updateGenerateButton();
}

// --- Bulk batch queue ---
// The whole tray is driven by a single /api/queue poll — never one poll per job.

function releaseBatchThumbs() {
    Object.values(batchThumbs).forEach(u => URL.revokeObjectURL(u));
    batchThumbs = {};
}

async function startBatchGeneration() {
    const picked = selectedFiles.slice();
    if (!picked.length) return;

    document.getElementById('generate-btn').disabled = true;
    document.getElementById('error-section').classList.add('hidden');
    document.getElementById('preview-section').classList.add('hidden');
    // Bulk has its own progress in the tray — keep the single-job bar out of the
    // way so the two never fight for the top of the screen.
    document.getElementById('progress-section').classList.add('hidden');
    setProgressSticky(false);

    const tray = document.getElementById('batch-tray');
    tray.classList.remove('hidden');
    tray.classList.add('running');
    document.getElementById('batch-list').innerHTML = '';
    document.getElementById('batch-actions').classList.add('hidden');
    document.getElementById('batch-skipped').classList.add('hidden');
    document.getElementById('batch-bar-fill').style.width = '0%';
    document.getElementById('batch-summary').textContent = `uploading ${picked.length} images…`;

    const fd = new FormData();
    picked.forEach(e => fd.append('files', e.file));
    fd.append('triangles', String(selectedTriangles));

    try {
        const r = await fetch('/api/generate-batch', { method: 'POST', body: fd });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            throw new Error(d.detail || `upload failed (${r.status})`);
        }
        const data = await r.json();

        // Hand the local previews over to the tray (keyed by filename) and drop
        // the selection *without* revoking — the tray owns those URLs now.
        releaseBatchThumbs();
        picked.forEach(e => { batchThumbs[e.file.name] = e.url; });
        selectedFiles = [];
        renderRejected([]);
        renderSelection();

        activeBatchId = data.batch_id;
        // Remembered so closing the tab / coming back hours later re-attaches to
        // this batch instead of losing the list (see resumeActiveBatch).
        localStorage.setItem('activeBatchId', data.batch_id);
        batchSelected = new Set();
        batchJobs = [];
        batchSig = '';
        batchBusy = true;
        renderSkipped(data.skipped);
        pollBatch();
        batchPollInterval = setInterval(pollBatch, 2000);
    } catch (e) {
        tray.classList.add('hidden');
        showError(e.message || 'connection error');
    }
    updateGenerateButton();
}

async function pollBatch() {
    if (!activeBatchId) return;
    try {
        const r = await fetch(`/api/queue?batch_id=${encodeURIComponent(activeBatchId)}`);
        if (!r.ok) return;
        const data = await r.json();
        const jobs = (data.jobs || []).filter(j => !j.batch_id || j.batch_id === activeBatchId);
        if (!jobs.length) return;
        batchJobs = jobs;
        renderBatch();
        // Stop polling once nothing can change any more.
        if (jobs.every(isTerminal)) stopBatchPolling();
    } catch (e) {}
}

function isTerminal(job) {
    return job.status === 'completed' || job.status === 'failed';
}

function stopBatchPolling() {
    clearInterval(batchPollInterval);
    batchPollInterval = null;
    const wasBusy = batchBusy;
    batchBusy = false;
    renderBatch();          // drops the running styling, reveals "clear"
    updateGenerateButton();
    if (wasBusy && historyOpen) loadHistory();
}

function renderSkipped(skipped) {
    const el = document.getElementById('batch-skipped');
    if (!skipped || !skipped.length) { el.classList.add('hidden'); return; }
    el.textContent = 'skipped by the server: ' +
        skipped.map(s => `${s.filename} — ${s.reason}`).join(', ');
    el.classList.remove('hidden');
}

// A big batch is mostly identical rows, so past ~8 items the list stops being
// scannable and a thumbnail grid is the faster way to find one model.
function effectiveBatchView() {
    return batchView || (batchJobs.length > 8 ? 'grid' : 'list');
}

function matchesBatchFilter(j) {
    if (batchFilter === 'all') return true;
    if (batchFilter === 'pending') return !isTerminal(j);
    return j.status === batchFilter;
}

function toggleBatchCollapsed() {
    batchCollapsed = !batchCollapsed;
    localStorage.setItem('batchCollapsed', batchCollapsed ? '1' : '0');
    applyBatchCollapsed();
}

function applyBatchCollapsed() {
    const tray = document.getElementById('batch-tray');
    tray.classList.toggle('collapsed', batchCollapsed);
    const btn = document.getElementById('batch-collapse');
    if (btn) btn.setAttribute('aria-expanded', batchCollapsed ? 'false' : 'true');
}

function setBatchFilter(f) {
    batchFilter = f;
    localStorage.setItem('batchFilter', f);
    batchSig = '';   // filter changes the rendered set, so force a redraw
    renderBatch();
}

function toggleBatchView() {
    batchView = effectiveBatchView() === 'grid' ? 'list' : 'grid';
    localStorage.setItem('batchView', batchView);
    batchSig = '';
    renderBatch();
}

function renderBatch() {
    if (!batchJobs.length) return;
    const tray = document.getElementById('batch-tray');
    tray.classList.remove('hidden');
    tray.classList.toggle('running', batchBusy);
    applyBatchCollapsed();

    const total = batchJobs.length;
    const completed = batchJobs.filter(j => j.status === 'completed').length;
    const failed = batchJobs.filter(j => j.status === 'failed').length;
    document.getElementById('batch-bar-fill').style.width =
        Math.round(((completed + failed) / total) * 100) + '%';
    let summary = `${completed} of ${total} done`;
    if (failed) summary += ` · ${failed} failed`;
    summary += batchBusy ? ' · still working' : ' · all finished';
    document.getElementById('batch-summary').textContent = summary;

    const view = effectiveBatchView();
    const list = document.getElementById('batch-list');
    list.classList.toggle('as-grid', view === 'grid');
    const viewBtn = document.getElementById('batch-view-toggle');
    if (viewBtn) viewBtn.textContent = view === 'grid' ? 'list view' : 'grid view';

    document.querySelectorAll('.batch-filter').forEach(b =>
        b.classList.toggle('active', b.dataset.filter === batchFilter));

    const shown = batchJobs.filter(matchesBatchFilter);

    // Only redraw when something actually changed, so a poll never wipes out
    // whatever the user is in the middle of tapping.
    const sig = view + '|' + batchFilter + '|' + shown.map(j => [
        j.job_id, j.status, Math.round((j.progress || 0) * 100), j.stage, j.queue_position, j.error,
        batchSelected.has(j.job_id) ? 1 : 0,
    ].join('|')).join(';');
    if (sig !== batchSig) {
        batchSig = sig;
        list.innerHTML = shown.map(view === 'grid' ? batchTile : batchRow).join('');
    }

    const empty = document.getElementById('batch-empty');
    if (shown.length) {
        empty.classList.add('hidden');
    } else {
        empty.textContent = `nothing ${batchFilter === 'pending' ? 'waiting' : batchFilter} in this batch`;
        empty.classList.remove('hidden');
    }

    document.getElementById('batch-actions').classList.toggle('hidden', !completed);
    updateZipButton();
}

// Compact grid tile — the thumbnail is the identifier here, because every file
// in a bulk drop is called something like "Screenshot 2026-08-22 112346.png".
function batchTile(j) {
    const name = escapeHtml(j.filename || j.job_id);
    // A failed job's directory is removed server-side, so its thumb is a
    // guaranteed 404 — don't ask for one and litter the console.
    const thumb = batchThumbs[j.filename]
        ? `<img class="tile-img" src="${batchThumbs[j.filename]}" alt="${name}">`
        : (j.status === 'failed'
            ? ''
            : `<img class="tile-img" src="/api/jobs/${encodeURIComponent(j.job_id)}/thumb" alt="${name}"
                    onerror="this.style.visibility='hidden'">`);

    let overlay = '', cls = j.status;
    if (j.status === 'queued') {
        const pos = j.queue_position || 1;
        overlay = `<div class="tile-overlay"><span class="tile-pos">${pos > 1 ? '#' + pos : 'next'}</span></div>`;
    } else if (j.status === 'running') {
        const pct = Math.round((j.progress || 0) * 100);
        overlay = `<div class="tile-overlay"><span class="tile-pct">${pct}%</span>
                   <div class="tile-mini"><div class="tile-mini-fill" style="width:${pct}%"></div></div></div>`;
    } else if (j.status === 'failed') {
        overlay = `<div class="tile-overlay"><span class="tile-x">failed</span></div>`;
    }

    const check = j.status === 'completed'
        ? `<input type="checkbox" class="tile-check" aria-label="select ${name}"
                  ${batchSelected.has(j.job_id) ? 'checked' : ''}
                  onclick="event.stopPropagation(); toggleBatchSelect('${escapeHtml(j.job_id)}', this.checked)">`
        : '';
    const open = j.status === 'completed'
        ? ` onclick="openBatchItem('${escapeHtml(j.job_id)}')"` : '';
    const title = j.status === 'failed' ? escapeHtml(j.error || 'generation failed') : name;

    return `<div class="batch-tile ${cls}"${open} title="${title}">
        ${thumb}${overlay}${check}
    </div>`;
}

function batchRow(j) {
    const name = escapeHtml(j.filename || j.job_id);
    // Prefer the local object URL from this session; fall back to the server
    // thumb (which is all we have after a reload), then to an empty tile.
    const thumb = batchThumbs[j.filename]
        ? `<img class="batch-thumb" src="${batchThumbs[j.filename]}" alt="">`
        : (j.status === 'failed'
            ? '<div class="batch-thumb"></div>'
            : `<img class="batch-thumb" src="/api/jobs/${encodeURIComponent(j.job_id)}/thumb" alt=""
                    onerror="this.outerHTML='<div class=&quot;batch-thumb&quot;></div>'">`);

    let state, badge, mini = '';
    if (j.status === 'queued') {
        const pos = j.queue_position || 1;
        const eta = formatEta(pos * SECONDS_PER_JOB);
        state = pos > 1 ? `in queue (position ${pos}) — ${eta}` : `next up — ${eta}`;
        badge = '<span class="batch-badge queued">queued</span>';
    } else if (j.status === 'running') {
        const pct = Math.round((j.progress || 0) * 100);
        state = `${pct}% — ${escapeHtml(j.stage || 'working')}`;
        mini = `<div class="batch-mini"><div class="batch-mini-fill" style="width:${pct}%"></div></div>`;
        badge = '<span class="batch-badge running">rendering</span>';
    } else if (j.status === 'completed') {
        state = 'done — tap to view';
        badge = '<span class="batch-badge done">done</span>';
    } else {
        state = escapeHtml(j.error || 'generation failed');
        badge = '<span class="batch-badge failed">failed</span>';
    }

    const check = j.status === 'completed'
        ? `<input type="checkbox" class="batch-check" aria-label="select ${name}"
                  ${batchSelected.has(j.job_id) ? 'checked' : ''}
                  onclick="event.stopPropagation(); toggleBatchSelect('${escapeHtml(j.job_id)}', this.checked)">`
        : '<span class="batch-check-spacer"></span>';
    const open = j.status === 'completed'
        ? ` onclick="openBatchItem('${escapeHtml(j.job_id)}')"` : '';

    return `<div class="batch-item ${j.status}"${open}>
        ${check}${thumb}
        <div class="batch-info">
            <div class="batch-name">${name}</div>
            <div class="batch-state${j.status === 'failed' ? ' is-error' : ''}">${state}</div>
            ${mini}
        </div>
        ${badge}
    </div>`;
}

// Open a finished item in the existing preview without disturbing the tray.
async function openBatchItem(jobId) {
    try {
        const r = await fetch(`/api/jobs/${jobId}`);
        if (r.ok) {
            const d = await r.json();
            showPreview(jobId, d.files, {
                triangles: d.triangles, size: d.size, duration: d.duration,
                name: d.name, description: d.description, uploaded: d.uploaded,
            });
        } else {
            // Server restarted and forgot the job — history still has it.
            await resumeFromHistory(jobId);
        }
    } catch (e) { return; }
    document.getElementById('error-section').classList.add('hidden');
    document.getElementById('preview-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function toggleBatchSelect(jobId, on) {
    if (on) batchSelected.add(jobId); else batchSelected.delete(jobId);
    updateZipButton();
}

function toggleSelectAllDone() {
    const done = batchJobs.filter(j => j.status === 'completed').map(j => j.job_id);
    const allSelected = done.length > 0 && done.every(id => batchSelected.has(id));
    batchSelected = new Set(allSelected ? [] : done);
    batchSig = '';  // force a redraw so the checkboxes follow
    renderBatch();
}

function updateZipButton() {
    const n = batchSelected.size;
    const btn = document.getElementById('batch-zip-btn');
    btn.disabled = !n;
    btn.textContent = n ? `download ${n} as zip` : 'download selected as zip';
    const doneCount = batchJobs.filter(j => j.status === 'completed').length;
    const allBtn = document.querySelector('.batch-select-all');
    if (allBtn) allBtn.textContent = (doneCount && n >= doneCount) ? 'clear selection' : 'select all done';
}

async function downloadSelectedZip() {
    const ids = Array.from(batchSelected);
    if (!ids.length) return;
    const btn = document.getElementById('batch-zip-btn');
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'zipping…';
    let url = null;
    try {
        const r = await fetch('/api/download-multi', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ job_ids: ids }),
        });
        if (!r.ok) throw new Error('zip failed');
        const blob = await r.blob();
        url = URL.createObjectURL(blob);
        const cd = r.headers.get('content-disposition') || '';
        const m = cd.match(/filename="?([^";]+)"?/);
        const a = document.createElement('a');
        a.href = url;
        a.download = m ? m[1] : 'models.zip';
        document.body.appendChild(a);
        a.click();
        a.remove();
        btn.textContent = 'downloaded!';
    } catch (e) {
        btn.textContent = 'zip failed';
    } finally {
        // Give the browser a moment to start the download before dropping the blob
        if (url) setTimeout(() => URL.revokeObjectURL(url), 30000);
        setTimeout(() => { btn.textContent = original; updateZipButton(); }, 1800);
    }
}

// If a batch was submitted earlier (refresh, phone sleep, or just coming back
// hours later), rebuild the tray from the queue instead of losing the list.
async function resumeActiveBatch() {
    const id = localStorage.getItem('activeBatchId');
    if (!id) return;
    try {
        const r = await fetch(`/api/queue?batch_id=${encodeURIComponent(id)}`);
        if (!r.ok) {
            // Backend no longer knows this batch (most likely a restart, which
            // wipes the in-memory job store) — drop the stale id.
            localStorage.removeItem('activeBatchId');
            return;
        }
        const data = await r.json();
        const jobs = (data.jobs || []).filter(j => !j.batch_id || j.batch_id === id);
        if (!jobs.length) { localStorage.removeItem('activeBatchId'); return; }
        activeBatchId = id;
        batchJobs = jobs;
        batchSelected = new Set();
        batchSig = '';
        batchBusy = !jobs.every(isTerminal);
        renderBatch();
        if (batchBusy) batchPollInterval = setInterval(pollBatch, 2000);
        updateGenerateButton();
    } catch (e) {
        // transient network error — leave the id and retry on the next load
    }
}

function dismissBatch() {
    // Clearing only drops the local view — the GPU keeps working through the
    // queue server-side. Say so, because "clear" during a run reads as "cancel".
    if (batchBusy && !confirm(
        'hide this batch?\n\nthe remaining images keep generating on the server — ' +
        'you just stop seeing this list, and finished models still show up in history.'
    )) return;
    clearInterval(batchPollInterval);
    batchPollInterval = null;
    releaseBatchThumbs();
    activeBatchId = null;
    batchJobs = [];
    batchSelected = new Set();
    batchSig = '';
    batchBusy = false;
    localStorage.removeItem('activeBatchId');
    document.getElementById('batch-tray').classList.add('hidden');
    document.getElementById('batch-list').innerHTML = '';
    updateGenerateButton();
}

// --- Preview ---
// Hidden tabs are loaded lazily (first time they're opened) so the visible
// textured viewer doesn't fight the others for bandwidth on first paint.
let preview = { jobId: null, files: [], loaded: {} };

function showPreview(jobId, files, stats) {
    // fromHistory tracks whether this preview came from clicking a history item
    // (so closing history can close it) vs. a live generation result (which stays).
    preview = { jobId, files: files || [], loaded: {}, fromHistory: false };
    document.getElementById('preview-section').classList.remove('hidden');

    const tv = document.getElementById('viewer-textured');
    if (preview.files.includes('textured.glb')) {
        // Use the already-downloaded texture as a poster for instant feedback
        if (preview.files.includes('texture.png'))
            tv.poster = `/api/jobs/${jobId}/files/texture.png`;
        tv.src = `/api/jobs/${jobId}/files/textured.glb`;
        preview.loaded.textured = true;
    }
    document.getElementById('download-btn').href = `/api/jobs/${jobId}/download`;
    renderModelStats(stats);
    renderNameSection(stats);
    renderUploaded(stats && stats.uploaded);
    switchTab('textured');
}

// Reflect the shared "uploaded to Roblox" flag on the toggle button.
function renderUploaded(uploaded) {
    preview.uploaded = !!uploaded;
    const btn = document.getElementById('uploaded-btn');
    btn.disabled = false;
    btn.classList.toggle('is-uploaded', preview.uploaded);
    btn.textContent = preview.uploaded ? '✓ uploaded to Roblox — tap to undo' : 'mark as uploaded to Roblox';
}

async function toggleUploaded() {
    if (!preview.jobId) return;
    const btn = document.getElementById('uploaded-btn');
    const next = !preview.uploaded;
    btn.disabled = true;
    try {
        const r = await fetch(`/api/jobs/${preview.jobId}/uploaded`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uploaded: next }),
        });
        if (!r.ok) throw new Error('failed');
        renderUploaded(next);
        if (historyOpen) loadHistory();  // update the badge in the shared list
    } catch (e) {
        renderUploaded(preview.uploaded);  // revert button state on failure
    }
}

// Pre-fills the editable name/description fields with any saved values (e.g.
// when re-opening an item from history) and resets the save/AI buttons.
function renderNameSection(stats) {
    const nameInput = document.getElementById('item-name-input');
    const descInput = document.getElementById('item-desc-input');
    document.getElementById('name-error').classList.add('hidden');
    nameInput.value = (stats && stats.name) || '';
    descInput.value = (stats && stats.description) || '';
    const aiBtn = document.getElementById('name-btn');
    aiBtn.disabled = !gpuOnline;
    aiBtn.textContent = (nameInput.value || descInput.value) ? '✨ re-suggest with AI' : '✨ suggest with AI';
    setMetaSaved(true);
}

// Toggle the save button between "saved" (disabled) and "save" (a pending edit).
function setMetaSaved(saved) {
    const btn = document.getElementById('meta-save-btn');
    btn.disabled = saved;
    btn.textContent = saved ? 'saved' : 'save';
}

function onMetaEdit() {
    setMetaSaved(false);
}

// AI suggestion: reads the input image with the vision model and fills the
// fields. The /name endpoint persists to history, so the result is saved.
async function nameItem() {
    if (!preview.jobId) return;
    const btn = document.getElementById('name-btn');
    const errEl = document.getElementById('name-error');
    errEl.classList.add('hidden');
    btn.disabled = true;
    // If the item already has a name, this is a "re-suggest" — ask the server
    // for a fresh alternative (higher temperature) rather than the identical
    // deterministic first suggestion.
    const vary = !!document.getElementById('item-name-input').value.trim();
    btn.textContent = vary ? 're-suggesting…' : 'analyzing image…';
    try {
        const r = await fetch(`/api/jobs/${preview.jobId}/name?vary=${vary}`, { method: 'POST' });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            throw new Error(d.detail || 'request failed');
        }
        const data = await r.json();
        document.getElementById('item-name-input').value = data.name;
        document.getElementById('item-desc-input').value = data.description;
        preview.name = data.name;
        preview.description = data.description;
        setMetaSaved(true);  // /name already persisted to history
        btn.textContent = '✨ re-suggest with AI';
        if (historyOpen) loadHistory();
    } catch (e) {
        errEl.textContent = e.message;
        errEl.classList.remove('hidden');
        btn.textContent = '✨ suggest with AI';
    } finally {
        btn.disabled = !gpuOnline;
    }
}

// Save the user's own name/description. No GPU required — it's just text.
async function saveMeta() {
    if (!preview.jobId) return;
    const btn = document.getElementById('meta-save-btn');
    const errEl = document.getElementById('name-error');
    errEl.classList.add('hidden');
    const name = document.getElementById('item-name-input').value.trim();
    const description = document.getElementById('item-desc-input').value.trim();
    btn.disabled = true;
    btn.textContent = 'saving…';
    try {
        const r = await fetch(`/api/jobs/${preview.jobId}/meta`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description }),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            throw new Error(d.detail || 'save failed');
        }
        preview.name = name;
        preview.description = description;
        setMetaSaved(true);
        if (historyOpen) loadHistory();  // reflect the new name in the list
    } catch (e) {
        errEl.textContent = e.message;
        errEl.classList.remove('hidden');
        setMetaSaved(false);
    }
}

function renderModelStats(stats) {
    const el = document.getElementById('model-stats');
    if (!stats || (!stats.triangles && !stats.size && !stats.duration)) {
        el.classList.add('hidden');
        return;
    }
    const parts = [];
    if (stats.triangles)
        parts.push((stats.triangles >= 1000 ? Math.round(stats.triangles / 1000) + 'k' : stats.triangles) + ' tris');
    if (stats.size) {
        const mb = stats.size / (1024 * 1024);
        parts.push(mb >= 1 ? mb.toFixed(1) + ' MB' : Math.round(stats.size / 1024) + ' KB');
    }
    if (stats.duration) parts.push('generated in ' + Math.round(stats.duration) + 's');
    el.textContent = parts.join(' · ');
    el.classList.remove('hidden');
}

function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');

    // Lazy-load the hidden tabs the first time they're opened
    if (!preview.jobId) return;
    if (name === 'untextured' && !preview.loaded.untextured && preview.files.includes('untextured.glb')) {
        document.getElementById('viewer-untextured').src = `/api/jobs/${preview.jobId}/files/untextured.glb`;
        preview.loaded.untextured = true;
    } else if (name === 'texture' && !preview.loaded.texture && preview.files.includes('texture.png')) {
        document.getElementById('texture-image').src = `/api/jobs/${preview.jobId}/files/texture.png`;
        preview.loaded.texture = true;
    }
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
        // Closing history also closes a preview that was opened from a history
        // item — but leaves a freshly-generated result on screen.
        if (preview.fromHistory) {
            document.getElementById('preview-section').classList.add('hidden');
            preview = { jobId: null, files: [], loaded: {}, fromHistory: false };
        }
    }
}

// What's New is a modal overlay (fixed, in front of everything) so it never
// shifts the page layout. Clicking the link toggles it; the × button, the
// dimmed backdrop, and Esc all close it.
function toggleWhatsNew() {
    document.getElementById('whatsnew-modal').classList.toggle('hidden');
}

function closeWhatsNew() {
    document.getElementById('whatsnew-modal').classList.add('hidden');
}

function toggleHowTo() {
    document.getElementById('howto-modal').classList.toggle('hidden');
}

function closeHowTo() {
    document.getElementById('howto-modal').classList.add('hidden');
}

function openPromptHelper() {
    showPromptMain();  // always open on the main view, not the history list
    document.getElementById('prompthelp-modal').classList.remove('hidden');
}

function closePromptHelper() {
    document.getElementById('prompthelp-modal').classList.add('hidden');
}

// --- Prompt-help history (shared, server-backed) ---
let phHistoryCache = [];

function showPromptMain() {
    document.getElementById('ph-main').classList.remove('hidden');
    document.getElementById('ph-history-panel').classList.add('hidden');
    document.getElementById('ph-hist-toggle').classList.remove('active');
}

function togglePromptHistory() {
    const panel = document.getElementById('ph-history-panel');
    if (panel.classList.contains('hidden')) {
        document.getElementById('ph-main').classList.add('hidden');
        panel.classList.remove('hidden');
        document.getElementById('ph-hist-toggle').classList.add('active');
        loadPromptHistory();
    } else {
        showPromptMain();
    }
}

async function loadPromptHistory() {
    const list = document.getElementById('ph-history-list');
    list.innerHTML = '<p class="ph-hist-empty">loading…</p>';
    try {
        const r = await fetch('/api/prompt-history');
        phHistoryCache = await r.json();
        if (!phHistoryCache.length) {
            list.innerHTML = '<p class="ph-hist-empty">no prompt history yet</p>';
            return;
        }
        list.innerHTML = phHistoryCache.map(item => {
            const d = new Date(item.timestamp * 1000);
            const ds = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            return `<div class="ph-hist-item" onclick="usePromptHistory('${item.id}')">
                <div class="ph-hist-info">
                    <div class="ph-hist-name">${escapeHtml(item.name || item.idea)}</div>
                    <div class="ph-hist-idea">${escapeHtml(item.idea)}</div>
                    <div class="ph-hist-date">${ds}</div>
                </div>
                <button class="ph-hist-del" onclick="event.stopPropagation(); deletePromptHistory('${item.id}')" title="delete">×</button>
            </div>`;
        }).join('');
    } catch (e) {
        list.innerHTML = '<p class="ph-hist-empty">failed to load history</p>';
    }
}

function usePromptHistory(id) {
    const item = phHistoryCache.find(h => h.id === id);
    if (!item) return;
    document.getElementById('ph-idea').value = item.idea || '';
    document.getElementById('ph-name').textContent = item.name || '';
    document.getElementById('ph-description').textContent = item.description || '';
    document.getElementById('ph-image-prompt').textContent = item.image_prompt || '';
    document.getElementById('ph-result').classList.remove('hidden');
    document.getElementById('ph-error').classList.add('hidden');
    showPromptMain();
}

async function deletePromptHistory(id) {
    await fetch(`/api/prompt-history/${id}`, { method: 'DELETE' });
    loadPromptHistory();
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeWhatsNew();
        closePromptHelper();
        closeHowTo();
    }
});

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
            const upBadge = item.uploaded ? '<span class="history-uploaded">✓ uploaded</span>' : '';
            const stats = JSON.stringify({ triangles: item.triangles, size: item.size, duration: item.duration, name: item.name, description: item.description, uploaded: item.uploaded }).replace(/"/g, '&quot;');
            return `<div class="history-item${item.uploaded ? ' uploaded' : ''}" onclick="loadFromHistory('${item.job_id}', ${JSON.stringify(item.files).replace(/"/g, '&quot;')}, ${stats})">
                <img class="history-thumb" src="/api/jobs/${item.job_id}/thumb" alt="" onerror="this.outerHTML='<div class=&quot;history-thumb&quot;></div>'">
                <div class="history-info">
                    <div class="history-name">${escapeHtml(item.name || item.filename || item.job_id)}${upBadge}</div>
                    <div class="history-date">${dateStr}${triBadge}</div>
                </div>
                <button class="history-delete" onclick="event.stopPropagation(); deleteHistory('${item.job_id}')" title="delete">×</button>
            </div>`;
        }).join('');
    } catch (e) {
        list.innerHTML = '<p class="history-empty">failed to load history</p>';
    }
}

function loadFromHistory(jobId, files, stats) {
    showPreview(jobId, files, stats);
    preview.fromHistory = true;  // so closing history closes this preview
    // Keep the live progress bar visible if a generation is still running —
    // browsing history shouldn't hide an in-flight job's progress.
    if (!activeJobId) {
        document.getElementById('progress-section').classList.add('hidden');
    }
    document.getElementById('error-section').classList.add('hidden');
}

async function deleteHistory(jobId) {
    const r = await fetch(`/api/history/${jobId}`, { method: 'DELETE' });
    // If we're currently previewing the item that was just deleted, close the
    // preview — its files are gone from the server. (Only on a successful
    // delete; if the server kept it, leave the preview as-is.)
    if (r.ok && preview.jobId === jobId) {
        document.getElementById('preview-section').classList.add('hidden');
        preview = { jobId: null, files: [], loaded: {}, fromHistory: false };
    }
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

// Paste an image straight from the clipboard (Ctrl+V anywhere)
document.addEventListener('paste', (e) => {
    if (document.getElementById('app').classList.contains('hidden')) return;
    const items = (e.clipboardData || {}).items || [];
    for (const item of items) {
        if (item.type && item.type.startsWith('image/')) {
            const blob = item.getAsFile();
            if (blob) {
                const ext = (item.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
                handleFile(new File([blob], `pasted.${ext}`, { type: item.type }));
                e.preventDefault();
            }
            return;
        }
    }
});

document.addEventListener('click', (e) => {
    if (!e.target.classList || !e.target.classList.contains('ph-copy')) return;
    // data-copy targets a form field (read .value); data-target a text node.
    const valId = e.target.dataset.copy;
    const el = document.getElementById(valId || e.target.dataset.target);
    if (!el) return;
    const text = valId ? el.value : el.textContent;
    const original = e.target.textContent;
    navigator.clipboard.writeText(text).then(() => {
        e.target.textContent = 'copied!';
        setTimeout(() => { e.target.textContent = original; }, 1500);
    }).catch(() => {
        e.target.textContent = 'failed';
        setTimeout(() => { e.target.textContent = original; }, 1500);
    });
});
