/* ResearchForge frontend.
 *
 * Plain ES2020, no framework, no build step. Every value rendered here comes
 * from the API; nothing is mocked. When the API reports that evidence is
 * insufficient, that message is shown verbatim.
 */

'use strict';

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  let value = bytes, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toFixed(value < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
}

function fmtDate(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').replace('Z', ' UTC');
}

function pct(value) {
  if (value === null || value === undefined) return '—';
  return `${Math.round(Number(value) * 100)}%`;
}

const VERIFICATION_BADGE = {
  verified: 'ok',
  partially_verified: 'info',
  conflict: 'bad',
  inconclusive: 'grey',
  inaccessible: 'warn',
  skipped: 'grey',
  failed: 'bad',
  unverified: 'grey',
};

function verificationBadge(status, label) {
  const kind = VERIFICATION_BADGE[status] || 'grey';
  return `<span class="badge ${kind}">${esc(label || status || 'unverified')}</span>`;
}

const STATE_BADGE = {
  discovered: 'grey',
  metadata_verified: 'info',
  download_verified: 'info',
  contents_inspected: 'ok',
  compatibility_assessed: 'ok',
  approved: 'ok',
};

function toast(message, kind) {
  const node = $('toast');
  node.textContent = message;
  node.className = `toast show ${kind || ''}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.className = 'toast'; }, 6000);
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch (err) { payload = { raw: text }; }
  }
  if (!response.ok) {
    const message = (payload && payload.error && payload.error.message) || response.statusText;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function setBanner(html, kind) {
  const node = $('banner');
  if (!html) { node.style.display = 'none'; return; }
  node.style.display = 'block';
  node.className = `banner ${kind || ''}`;
  node.innerHTML = html;
}

function openModal(html) {
  $('modal-body').innerHTML = html;
  $('modal').classList.add('open');
}

$('modal-close').addEventListener('click', () => $('modal').classList.remove('open'));
$('modal').addEventListener('click', (event) => {
  if (event.target === $('modal')) $('modal').classList.remove('open');
});

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------

const state = {
  meta: null,
  adapters: [],
  health: null,
  projects: [],
  projectId: null,
  snapshot: null,
  jobId: null,
  jobTimer: null,
  selection: new Set(),
  matrix: [],
  manuscript: null,
  profile: null,
};

/* where the record profile sends you back to */
let recordReturnTo = 'dashboard';

// ---------------------------------------------------------------------------
// navigation
// ---------------------------------------------------------------------------

$$('#nav button').forEach((button) => {
  button.addEventListener('click', () => {
    $$('#nav button').forEach((other) => other.classList.remove('active'));
    button.classList.add('active');
    $$('.panel').forEach((panel) => panel.classList.remove('active'));
    $(`panel-${button.dataset.panel}`).classList.add('active');
    window.scrollTo({ top: 0, behavior: 'auto' });
  });
});

/* Panels that have no nav button (the record profile) are opened by name, and
   the sidebar keeps whatever the researcher was last looking at. */
function showPanel(name) {
  const button = $(`#nav button[data-panel="${name}"]`);
  if (button) { button.click(); return; }
  $$('.panel').forEach((panel) => panel.classList.remove('active'));
  const panel = $(`panel-${name}`);
  if (panel) panel.classList.add('active');
  window.scrollTo({ top: 0, behavior: 'auto' });
}

function activePanelName() {
  const active = document.querySelector('.panel.active');
  return active ? active.id.replace(/^panel-/, '') : 'dashboard';
}

// ---------------------------------------------------------------------------
// views, local workspace profile, gate
// ---------------------------------------------------------------------------

const PROFILE_KEY = 'rf.profile';

/* The ten stages of the real pipeline, with the module that implements each
   one. Used by both the dashboard summariser and the "How it works" panel. */
const PIPELINE = [
  ['Your question is parsed', 'Plain language becomes a structured specification: topic, objective, domain, intent (datasets / literature / both), keywords, data types, modalities, geography, time range and access requirements. Assumptions it had to make are listed, not hidden.', 'services/parser.py'],
  ['A query plan is built', 'The question is rewritten into several query variants per source, each tagged with where it came from. Adapters that do not fit the intent are excluded, with a stated reason.', 'services/planning.py'],
  ['Official public APIs are contacted', 'Each adapter handles its own pagination, retries, back-off and rate limits. Every page requested, every empty page and every failure is written to the search ledger. A failed source is recorded as a failure, never as "nothing exists".', 'sources/*.py'],
  ['Everything is normalised', 'Nine different response shapes collapse into one NormalizedRecord: identifier, DOI, title, abstract, authors, year, venue, publisher, licence, access, citation count, concepts, dataset fields and the raw payload.', 'sources/base.py'],
  ['Duplicates are merged deterministically', 'Same DOI means same record. Otherwise title + year, then a token-overlap check at 0.87. Merges are recorded as duplicate_of, so the merge is countable and reversible rather than a silent delete.', 'services/dedupe.py'],
  ['Identity and links are verified', 'DOIs are resolved through the registrars, links are probed, and licence and access terms are compared. Each outcome is stored per check: verified, partially verified, conflict, inconclusive, inaccessible or failed.', 'services/verification.py'],
  ['Sufficiency is judged', 'The pipeline decides whether the scope it actually reached is enough to answer: sufficient, unverified candidates, insufficient evidence, or search incomplete. A failed source is never counted as evidence of absence.', 'services/evidence.py'],
  ['Evidence is extracted, verbatim', 'Passages are lifted from retrieved text with a locator and an origin marker. Metadata copied from a single source is labelled as provenance, explicitly not independent confirmation.', 'services/evidence.py'],
  ['Everything is stored and reproducible', 'Projects, runs, the ledger, records, verifications, downloads with SHA-256, gap candidates and manuscripts all land in local SQLite. Reopen any project later and the reasoning is still there.', 'db.py · services/store.py'],
  ['You get the answer, or the truth', 'Results appear with their verification status and evidence attached. If nothing survives the checks, you get the honest line below instead of a fabricated substitute.', 'api/routes.py'],
];

const EXAMPLE_QUESTIONS = [
  'Find publicly available datasets for detecting fraudulent financial transactions',
  'Datasets for crop yield prediction from satellite imagery',
  'Public datasets of air quality measurements in South Asia',
];

const QUICK_ACTIONS = [
  ['datasets', 'Datasets', 'Verified dataset records'],
  ['literature', 'Literature', 'Papers and reports retrieved'],
  ['matrix', 'Evidence matrix', 'Field-by-field comparison'],
  ['gaps', 'Gap explorer', 'Only evidence-backed gaps'],
  ['manuscript', 'Manuscript', '12 guided sections'],
  ['downloads', 'Downloads', 'Size and SHA-256 checked'],
];

function loadProfile() {
  try {
    const raw = localStorage.getItem(PROFILE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (error) {
    return null;
  }
}

function saveProfile(profile) {
  try { localStorage.setItem(PROFILE_KEY, JSON.stringify(profile)); } catch (error) { /* storage disabled */ }
}

function showView(name) {
  $$('.view').forEach((view) => view.classList.remove('active'));
  const view = $(`view-${name}`);
  if (view) view.classList.add('active');
  window.scrollTo({ top: 0, behavior: 'auto' });
}

function applyProfile() {
  const profile = state.profile;
  const chip = $('profile-chip');
  if (profile && profile.name) {
    chip.textContent = profile.name;
    chip.title = `${profile.name}${profile.role ? ' · ' + profile.role : ''} — open profile menu`;
    $('dash-name').textContent = profile.name;
    $('dash-sub').textContent = profile.role
      ? `${profile.role} · here is where you are right now.`
      : 'Here is where you are right now.';
  } else {
    chip.textContent = 'Profile';
    chip.title = 'Open the profile menu';
    $('dash-name').textContent = 'there';
    $('dash-sub').textContent = 'Here is where you are right now.';
  }
}

function enterWorkspace(profile) {
  state.profile = profile;
  saveProfile(profile);
  $('view-app').classList.add('ready');
  showView('app');
  applyProfile();
  renderDashboard();
  showPanel('dashboard');
}

/* First visit shows the gate form; a returning visitor goes straight through. */
function enterWorkspaceOrGate() {
  if (state.profile && state.profile.name) {
    enterWorkspace(state.profile);
    toast(`Welcome back, ${state.profile.name}.`, 'ok');
    return;
  }
  $('view-app').classList.remove('ready');
  showView('app');
  $('gate-name').value = '';
  $('gate-role').value = '';
  $('gate-name').focus();
}

function goToLanding() {
  const profile = state.profile || {};
  $('gate-name').value = profile.name || '';
  $('gate-role').value = profile.role || '';
  $('view-app').classList.remove('ready');
  showView('landing');
}

function openProfileMenu() {
  const profile = state.profile || {};
  openModal(`
    <h2>Your workspace profile</h2>
    <p class="small muted" style="margin-top:0">Local to this browser. It is never sent to any research source, and there is no password involved.</p>
    <dl class="kv">
      <dt>Name</dt><dd>${esc(profile.name || 'not set')}</dd>
      <dt>Role</dt><dd>${esc(profile.role || 'not set')}</dd>
      <dt>Workspace opened</dt><dd>${esc(profile.since ? fmtDate(profile.since) : 'not recorded')}</dd>
    </dl>
    <h3>Change it</h3>
    <label for="pm-name">Name</label>
    <input type="text" id="pm-name" maxlength="60" value="${esc(profile.name || '')}">
    <label for="pm-role">Role</label>
    <input type="text" id="pm-role" maxlength="60" value="${esc(profile.role || '')}">
    <div class="row tight" style="margin-top:14px">
      <button class="btn" id="pm-save" type="button">Save</button>
      <button class="btn ghost" id="pm-home" type="button">Back to landing page</button>
      <button class="btn danger" id="pm-clear" type="button">Forget this profile</button>
    </div>
    <p class="tiny muted" style="margin-top:12px">Projects, records and evidence live in the local SQLite database, not in this profile. Forgetting the profile deletes no research.</p>
  `);
  $('pm-save').addEventListener('click', () => {
    const name = $('pm-name').value.trim();
    if (!name) { toast('Please enter a name.', 'bad'); return; }
    state.profile = {
      name,
      role: $('pm-role').value.trim(),
      since: (state.profile && state.profile.since) || new Date().toISOString(),
    };
    saveProfile(state.profile);
    applyProfile();
    renderDashboard();
    $('modal').classList.remove('open');
    toast('Profile updated.', 'ok');
  });
  $('pm-home').addEventListener('click', () => {
    $('modal').classList.remove('open');
    goToLanding();
  });
  $('pm-clear').addEventListener('click', () => {
    try { localStorage.removeItem(PROFILE_KEY); } catch (error) { /* ignore */ }
    state.profile = null;
    $('modal').classList.remove('open');
    applyProfile();
    renderDashboard();
    goToLanding();
    toast('Profile forgotten. Nothing was deleted from your database.', '');
  });
}

// ---------------------------------------------------------------------------
// dashboard
// ---------------------------------------------------------------------------

function renderDashboard() {
  const health = state.health;
  const global = (health && health.counts) || {};
  const snapshot = state.snapshot;
  const local = (snapshot && snapshot.counts) || {};
  const projectId = state.projectId;
  const scope = projectId ? `in project #${projectId}` : 'across the database';

  const tiles = [
    [global.projects || 0, 'Projects', 'questions you have run'],
    [projectId ? (local.records || 0) : (global.records || 0), 'Records', scope],
    [projectId ? (local.datasets || 0) : 0, 'Datasets', projectId ? scope : 'no project loaded'],
    [projectId ? (local.evidence || 0) : (global.evidence || 0), 'Evidence', projectId ? 'quoted passages' : scope],
  ];
  $('dash-tiles').innerHTML = tiles.map(([value, label, note]) => `
    <div class="tile">
      <span class="tile-num">${esc(value)}</span>
      <span class="tile-label">${esc(label)}</span>
      <span class="tiny muted">${esc(note)}</span>
    </div>`).join('');

  $('dash-examples').innerHTML = EXAMPLE_QUESTIONS.map((question) =>
    `<button class="dash-example" type="button" data-example="${esc(question)}">${esc(question)}</button>`).join('');
  $('dash-examples').querySelectorAll('[data-example]').forEach((button) => {
    button.addEventListener('click', () => {
      $('question').value = button.dataset.example;
      showPanel('workspace');
      $('question').focus();
    });
  });

  $('dash-pipeline').innerHTML = PIPELINE.map(([title]) => `<li>${esc(title)}</li>`).join('');

  const run = snapshot && snapshot.latest_run;
  $('dash-project-line').textContent = projectId
    ? `Project #${projectId}${run ? ` · latest run #${run.id} (${run.status})` : ' · no run yet'}.`
    : 'No project loaded yet. Run your first question and its results will appear here.';

  $('dash-quick').innerHTML = QUICK_ACTIONS.map(([panel, title, note]) =>
    `<button class="quick-card" type="button" data-goto="${esc(panel)}"><span class="qc-title">${esc(title)}</span><span class="qc-note">${esc(note)}</span></button>`).join('');
  $('dash-quick').querySelectorAll('[data-goto]').forEach((button) => {
    button.addEventListener('click', () => {
      if (!projectId) { toast('Run a question first — there is no project loaded yet.', 'bad'); return; }
      showPanel(button.dataset.goto);
    });
  });

  const projects = state.projects.slice(0, 4);
  $('dash-projects').innerHTML = projects.length ? projects.map((project) => `
    <button class="quick-card" style="margin-bottom:8px;width:100%" type="button" data-open-project="${project.id}">
      <span class="qc-title">#${esc(project.id)} · ${esc(project.title || '(untitled)')}</span>
      <span class="qc-note">${esc(project.question)}</span>
      <span class="qc-note">${project.latest_run ? `latest run #${esc(project.latest_run.id)} · ${esc(project.latest_run.status)}` : 'no run yet'}</span>
    </button>`).join('')
    : '<p class="muted small">No project yet. Ask a research question to create one.</p>';
  $('dash-projects').querySelectorAll('[data-open-project]').forEach((button) => {
    button.addEventListener('click', async () => {
      await openProject(Number(button.dataset.openProject));
      showPanel('datasets');
    });
  });

  const records = ((snapshot && snapshot.records) || []).filter((record) => !record.duplicate_of).slice(0, 5);
  $('dash-records').innerHTML = records.length ? records.map((record) => `
    <button class="quick-card" style="margin-bottom:8px;width:100%" type="button" data-open-record="${record.id}">
      <span class="qc-title">${esc(String(record.title || '(untitled)').slice(0, 96))}</span>
      <span class="qc-note">${esc(record.kind)} · ${esc(record.source)}${record.year ? ` · ${esc(record.year)}` : ''}${record.doi ? ` · ${esc(record.doi)}` : ''}</span>
    </button>`).join('')
    : '<p class="muted small">No records stored for the loaded project yet.</p>';
  $('dash-records').querySelectorAll('[data-open-record]').forEach((button) => {
    button.addEventListener('click', () => showRecordDetail(Number(button.dataset.openRecord)));
  });
}

function renderHowTo() {
  $('howto-body').innerHTML = `
    <div class="card">
      <p class="small" style="margin:0">Every stage below is implemented in this codebase. The module name tells you which file to read to check it. Nothing here is planned, mocked or aspirational.</p>
    </div>
    <ol class="pipe">${PIPELINE.map(([title, body, module], index) => `
      <li class="pipe-step">
        <span class="pipe-num">${index + 1}</span>
        <h4>${esc(title)}</h4>
        <p>${esc(body)}</p>
        <span class="pipe-mod">${esc(module)}</span>
      </li>`).join('')}</ol>
    <div class="card" style="margin-top:22px">
      <h3 style="margin-top:0">Where each part is visible in the app</h3>
      <div class="table-wrap"><table><tbody>
        <tr><td><strong>Run &amp; timeline</strong></td><td class="small">Live job progress, and the coverage and sufficiency verdict of the latest run.</td></tr>
        <tr><td><strong>Sources &amp; queries</strong></td><td class="small">The search ledger: every source, query, page and exclusion, each with its reason.</td></tr>
        <tr><td><strong>Adapters &amp; logs</strong></td><td class="small">Which adapters are available right now, and the raw structured log stream.</td></tr>
        <tr><td><strong>Policy &amp; evidence rules</strong></td><td class="small">The exact vocabulary of every state the pipeline is allowed to report.</td></tr>
      </tbody></table></div>
    </div>`;
}

function wireShell() {
  ['btn-enter', 'btn-enter-top', 'btn-enter-bottom'].forEach((id) => {
    $(id).addEventListener('click', enterWorkspaceOrGate);
  });
  $('btn-gate-back').addEventListener('click', goToLanding);
  $('btn-home').addEventListener('click', goToLanding);
  $('profile-chip').addEventListener('click', openProfileMenu);
  $('gate-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const name = $('gate-name').value.trim();
    if (!name) { toast('Please enter a name to open your workspace.', 'bad'); return; }
    enterWorkspace({ name, role: $('gate-role').value.trim(), since: new Date().toISOString() });
    toast(`Welcome, ${name}. Your dashboard is ready.`, 'ok');
  });
  $('record-back').addEventListener('click', () => showPanel(recordReturnTo));
  $('dash-start').addEventListener('click', () => {
    showPanel('workspace');
    $('question').focus();
  });
  $('dash-view-project').addEventListener('click', () => showPanel('projects'));
  $('dash-howto').addEventListener('click', () => showPanel('howto'));
}

// ---------------------------------------------------------------------------
// bootstrap
// ---------------------------------------------------------------------------

async function init() {
  state.profile = loadProfile();
  recordReturnTo = 'dashboard';
  applyProfile();
  wireShell();

  try {
    const [health, meta, adapters] = await Promise.all([
      api('/api/health'), api('/api/meta'), api('/api/adapters'),
    ]);
    state.health = health;
    state.meta = meta;
    state.adapters = adapters.adapters || [];
    $('health-chip').className = 'badge ok';
    $('health-chip').textContent = `online · ${health.adapters.available.length} adapters`;
    /* The landing page quotes the backend's own constant rather than a copy. */
    $('land-message').textContent = meta.insufficient_dataset_message;
    $('land-live').textContent = `Searched live — ${health.adapters.available.length} adapters available right now`;
  } catch (error) {
    $('health-chip').className = 'badge bad';
    $('health-chip').textContent = 'backend unreachable';
    setBanner(`The backend could not be reached: ${esc(error.message)}`, 'bad');
    toast('The backend is unreachable, so search and results are unavailable. Start the server with run.bat.', 'bad');
    return;
  }

  renderPolicy();
  renderHowTo();
  renderAdapterPicker();
  renderAdapterTable();
  applyProfile();
  await loadProjects();
  await refreshLogs();
}

async function loadProjects() {
  const payload = await api('/api/projects');
  state.projects = payload.projects || [];
  renderProjects();
  if (!state.projectId && state.projects.length) {
    await openProject(state.projects[0].id);
  } else if (state.projectId) {
    await openProject(state.projectId);
  }
}

// ---------------------------------------------------------------------------
// workspace
// ---------------------------------------------------------------------------

function renderAdapterPicker() {
  const node = $('adapter-picker');
  const available = state.adapters.filter((adapter) => adapter.available);
  if (!available.length) {
    node.innerHTML = '<span class="small muted">No adapter initialised. Check the Adapters &amp; logs panel.</span>';
    return;
  }
  node.innerHTML = available.map((adapter) => {
    const note = adapter.requires_credentials && !adapter.credentials_present
      ? ' — works without a key but may be rate limited' : '';
    return `<span class="chip selectable" data-adapter="${esc(adapter.name)}" title="${esc((adapter.notes || '') + note)}">${esc(adapter.label)}</span>`;
  }).join('');
  node.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => chip.classList.toggle('on'));
  });
}

function selectedAdapters() {
  return Array.from(document.querySelectorAll('#adapter-picker .chip.on')).map((chip) => chip.dataset.adapter);
}

function researchPayload() {
  const question = $('question').value.trim();
  if (question.length < 3) throw new Error('Please enter a research question of at least 3 characters.');
  const adapters = selectedAdapters();
  return {
    question,
    project_id: null,
    intent: $('opt-intent').value || null,
    depth: Number($('opt-depth').value) || 0,
    page_size: Number($('opt-page').value) || 0,
    verify_limit: Number($('opt-verify').value) || 0,
    adapters: adapters.length ? adapters : null,
    follow_citations: $('opt-citations').checked,
    fetch_fulltext: $('opt-fulltext').checked,
    use_llm: $('opt-llm').checked,
  };
}

$('btn-preview').addEventListener('click', async () => {
  let payload;
  try { payload = researchPayload(); } catch (error) { toast(error.message, 'bad'); return; }
  $('plan-card').style.display = 'block';
  $('plan-body').innerHTML = '<p class="muted small">Planning…</p>';
  try {
    const result = await api('/api/research/plan', {
      method: 'POST',
      body: JSON.stringify({
        question: payload.question, depth: payload.depth, page_size: payload.page_size,
        adapters: payload.adapters, intent: payload.intent, use_llm: payload.use_llm,
      }),
    });
    const spec = result.spec || {};
    const plan = result.plan || {};
    const queries = (plan.queries || []).map((query) =>
      `<li><code>${esc(query.text)}</code> <span class="tiny muted">(${esc(query.origin || '')} · ${esc(query.intent || '')})</span></li>`).join('');
    const adapters = (plan.adapters || []).map((name) => `<span class="chip">${esc(name)}</span>`).join('');
    const excluded = (plan.excluded_adapters || []).map((item) =>
      `<li><code>${esc(item.adapter)}</code> — ${esc(item.reason)}</li>`).join('');
    $('plan-body').innerHTML = `
      <dl class="kv">
        <dt>Topic</dt><dd>${esc(spec.topic)}</dd>
        <dt>Objective</dt><dd>${esc(spec.objective)}</dd>
        <dt>Domain</dt><dd>${esc(spec.domain || 'unspecified')}</dd>
        <dt>Intent</dt><dd>${esc(spec.intent)}</dd>
        <dt>Keywords</dt><dd>${(spec.keywords || []).map((k) => `<span class="chip">${esc(k)}</span>`).join(' ') || '—'}</dd>
        <dt>Data types</dt><dd>${(spec.data_types || []).map(esc).join(', ') || 'not specified'}</dd>
        <dt>Modalities</dt><dd>${(spec.modalities || []).map(esc).join(', ') || 'not specified'}</dd>
        <dt>Geography</dt><dd>${esc(spec.geography || 'not specified')}</dd>
        <dt>Time range</dt><dd>${esc(JSON.stringify(spec.time_range || {}))}</dd>
        <dt>Access requirements</dt><dd>${(spec.access_requirements || []).map(esc).join(', ') || 'none stated'}</dd>
        <dt>Assumptions</dt><dd>${(spec.assumptions || []).map(esc).join('<br>') || 'none'}</dd>
      </dl>
      ${spec.clarification_question ? `<div class="banner warn"><strong>Clarification suggested:</strong> ${esc(spec.clarification_question)}</div>` : ''}
      <h3>Sources planned</h3><div class="chips">${adapters || '<span class="small muted">none</span>'}</div>
      ${excluded ? `<h3>Sources excluded</h3><ul class="small">${excluded}</ul>` : ''}
      <h3>Query variants (${(plan.queries || []).length})</h3><ul class="small">${queries || '<li class="muted">none</li>'}</ul>
      <h3>Expansion rationale</h3><ul class="small">${(plan.rationale || []).map((r) => `<li>${esc(r)}</li>`).join('') || '<li class="muted">none recorded</li>'}</ul>
      <p class="tiny muted">${esc(result.note || '')}</p>`;
  } catch (error) {
    $('plan-body').innerHTML = `<p class="small" style="color:var(--bad)">Planning failed: ${esc(error.message)}</p>`;
  }
});

$('btn-run').addEventListener('click', async () => {
  let payload;
  try { payload = researchPayload(); } catch (error) { toast(error.message, 'bad'); return; }
  $('btn-run').disabled = true;
  try {
    const result = await api('/api/research', { method: 'POST', body: JSON.stringify(payload) });
    state.jobId = result.job_id;
    $('job-status').className = 'badge info';
    $('job-status').textContent = 'queued';
    $('job-message').textContent = 'Job queued…';
    $('job-bar').style.width = '2%';
    $('btn-cancel').disabled = false;
    showPanel('run');
    pollJob();
  } catch (error) {
    $('btn-run').disabled = false;
    toast(`Could not start the run: ${error.message}`, 'bad');
  }
});

$('btn-cancel').addEventListener('click', async () => {
  if (!state.jobId) return;
  try {
    const result = await api(`/api/jobs/${state.jobId}/cancel`, { method: 'POST' });
    toast(result.cancelled ? 'Cancellation requested; the run will stop at the next checkpoint.' : `Not cancelled: ${result.reason || result.status}`, result.cancelled ? '' : 'bad');
  } catch (error) {
    toast(error.message, 'bad');
  }
});

function pollJob() {
  clearTimeout(state.jobTimer);
  if (!state.jobId) return;
  const tick = async () => {
    try {
      const job = await api(`/api/jobs/${state.jobId}`);
      renderJob(job);
      if (job.project_id && job.project_id !== state.projectId) {
        await openProject(job.project_id, { silent: true });
      }
      if (['queued', 'running', 'cancelling'].includes(job.status)) {
        state.jobTimer = setTimeout(tick, 1200);
      } else {
        $('btn-run').disabled = false;
        $('btn-cancel').disabled = true;
        const projectId = job.project_id || state.projectId;
        if (projectId) await openProject(projectId, { silent: true });
        await loadProjects();
        if (job.status === 'completed') toast('Run completed. See the Sources & queries and Datasets panels.', 'ok');
        if (job.status === 'failed') toast(`Run failed: ${job.error ? job.error.split('\n')[0] : 'unknown error'}`, 'bad');
        if (job.status === 'cancelled') toast('Run cancelled. Partial results were kept.', '');
      }
    } catch (error) {
      $('job-message').textContent = `Could not read job status: ${error.message}`;
      state.jobTimer = setTimeout(tick, 3000);
    }
  };
  tick();
}

function renderJob(job) {
  const kind = { queued: 'grey', running: 'info', cancelling: 'warn', completed: 'ok', failed: 'bad', cancelled: 'grey', interrupted: 'warn' }[job.status] || 'grey';
  $('job-status').className = `badge ${kind}`;
  $('job-status').textContent = `${job.kind} · ${job.status}`;
  $('job-bar').style.width = `${Math.round((job.progress || 0) * 100)}%`;
  $('job-message').textContent = job.error ? `Failed: ${job.error.split('\n')[0]}` : (job.message || '');
}

// ---------------------------------------------------------------------------
// voice dictation
// ---------------------------------------------------------------------------

(function setupVoice() {
  const button = $('btn-voice');
  const status = $('voice-status');
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    status.textContent = 'Voice dictation is not available in this browser (the Web Speech API is required). You can still type, or paste transcribed text.';
    button.disabled = true;
    return;
  }
  let recognition = null;
  let listening = false;
  button.addEventListener('click', () => {
    if (listening) { recognition.stop(); return; }
    recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = navigator.language || 'en-US';
    recognition.onstart = () => { listening = true; button.textContent = '⏹ Stop dictation'; status.textContent = 'Listening… speak your research question.'; };
    recognition.onerror = (event) => { status.textContent = `Dictation error: ${event.error}`; };
    recognition.onend = () => { listening = false; button.textContent = '🎙 Start dictation'; };
    recognition.onresult = (event) => {
      let finalText = '';
      for (let index = event.resultIndex; index < event.results.length; index++) {
        if (event.results[index].isFinal) finalText += event.results[index][0].transcript;
      }
      if (finalText) {
        const box = $('question');
        box.value = `${box.value} ${finalText}`.trim();
      }
    };
    recognition.start();
  });
})();

// ---------------------------------------------------------------------------
// project loading and rendering
// ---------------------------------------------------------------------------

async function openProject(projectId, options) {
  state.projectId = projectId;
  state.selection.clear();
  localStorage.setItem('rf_project', String(projectId));
  try {
    state.snapshot = await api(`/api/projects/${projectId}/snapshot`);
  } catch (error) {
    toast(`Could not load project ${projectId}: ${error.message}`, 'bad');
    return;
  }
  $('project-chip').className = 'badge info';
  $('project-chip').textContent = `project #${projectId}`;
  renderBanner();
  renderRunSummary();
  renderLedger();
  renderDownloads();
  renderManuscript(state.snapshot.manuscript);
  renderGaps(state.snapshot.gaps || []);
  renderDashboard();
  if (!options || !options.silent) await Promise.all([loadDatasets(), loadLiterature()]);
}

function renderBanner() {
  const snapshot = state.snapshot;
  if (!snapshot) { setBanner(''); return; }
  const run = snapshot.latest_run;
  if (!run) {
    setBanner('This project has no search run yet. Run a research question to populate it.', 'warn');
    return;
  }
  const scope = run.scope || {};
  const sufficiency = scope.sufficiency || {};
  const counts = snapshot.counts || {};
  if (sufficiency.status === 'insufficient_evidence') {
    const message = state.meta ? state.meta.insufficient_dataset_message : 'There is insufficient data worldwide. No dataset.';
    setBanner(`<strong>${esc(message)}</strong><br>${esc(sufficiency.message || '')}`, 'bad');
    return;
  }
  if (sufficiency.status === 'search_incomplete') {
    setBanner(`<strong>Search incomplete.</strong> ${esc(sufficiency.message || '')} These results are not evidence of absence.`, 'warn');
    return;
  }
  const failed = (scope.sources_failed || []).map((item) => item.adapter);
  const parts = [
    `Run #${run.id} · ${esc(run.status)} · ${(scope.sources_searched || []).length} source(s) answered`,
    `${counts.records || 0} record(s) (${counts.papers || 0} papers, ${counts.datasets || 0} datasets, ${counts.duplicates || 0} merged duplicates)`,
    `${counts.evidence || 0} evidence reference(s)`,
    `sufficiency: ${esc(sufficiency.status || 'not assessed')}`,
  ];
  if (failed.length) parts.push(`<span style="color:var(--warn)">sources failed: ${esc(failed.join(', '))}</span>`);
  setBanner(parts.join(' &nbsp;·&nbsp; '), failed.length ? 'warn' : '');
  if (failed.length) {
    setBanner(`${parts.join(' &nbsp;·&nbsp; ')}<br><span class="small">Results are partial. A failed source is <em>not</em> evidence that data does not exist — see the Sources &amp; queries panel.</span>`, 'warn');
  }
}

function renderRunSummary() {
  const node = $('run-summary');
  const snapshot = state.snapshot;
  if (!snapshot || !snapshot.latest_run) { node.innerHTML = '<p class="muted small">No run yet.</p>'; return; }
  const run = snapshot.latest_run;
  const scope = run.scope || {};
  const sufficiency = scope.sufficiency || {};
  const counts = snapshot.counts || {};
  node.innerHTML = `
    <dl class="kv">
      <dt>Question</dt><dd>${esc(run.question)}</dd>
      <dt>Status</dt><dd>${esc(run.status)} ${esc(run.finished_at ? '· finished ' + fmtDate(run.finished_at) : '')}</dd>
      <dt>Sources answered</dt><dd>${(scope.sources_searched || []).map((s) => `<span class="chip">${esc(s)}</span>`).join(' ') || 'none'}</dd>
      <dt>Sources failed</dt><dd>${(scope.sources_failed || []).map((item) => `<span class="chip">${esc(item.adapter)}: ${esc(item.reason)}</span>`).join(' ') || 'none'}</dd>
      <dt>Pages / queries</dt><dd>${esc(scope.pages_retrieved || 0)} page(s), ${esc(scope.queries_executed || 0)} query execution(s), ${esc((scope.unique_queries || []).length)} unique query text(s)</dd>
      <dt>Results seen</dt><dd>${esc(scope.results_seen || 0)} raw result(s), ${esc(scope.empty_pages || 0)} empty page(s)</dd>
      <dt>Records stored</dt><dd>${esc(counts.records || 0)} (${esc(counts.papers || 0)} papers · ${esc(counts.datasets || 0)} datasets · ${esc(counts.duplicates || 0)} merged duplicates)</dd>
      <dt>Evidence</dt><dd>${esc(counts.evidence || 0)} reference(s)</dd>
      <dt>Sufficiency</dt><dd>${esc(sufficiency.status || 'not assessed')} — ${esc(sufficiency.message || '')}</dd>
      <dt>Excluded sources</dt><dd>${(scope.excluded_adapters || []).map((item) => `<span class="chip">${esc(item.adapter)}: ${esc(item.reason)}</span>`).join(' ') || 'none'}</dd>
      <dt>Coverage statement</dt><dd class="small muted">${esc(scope.coverage_statement || '')}</dd>
    </dl>
    ${(sufficiency.explanation || []).length ? `<details><summary>Sufficiency explanation (${sufficiency.explanation.length} lines)</summary><ul class="small">${sufficiency.explanation.map((line) => `<li>${esc(line)}</li>`).join('')}</ul></details>` : ''}
    ${(sufficiency.next_steps || []).length ? `<details><summary>Suggested next steps</summary><ul class="small">${sufficiency.next_steps.map((line) => `<li>${esc(line)}</li>`).join('')}</ul></details>` : ''}
    ${(run.failures || []).length ? `<details><summary>Failure details</summary><ul class="small">${run.failures.map((f) => `<li><code>${esc(f.adapter)}</code> — ${esc(f.reason)}</li>`).join('')}</ul></details>` : ''}`;
}

// ---------------------------------------------------------------------------
// sources & queries
// ---------------------------------------------------------------------------

function renderLedger() {
  const snapshot = state.snapshot;
  const ledger = snapshot && snapshot.ledger;
  if (!ledger) {
    $('ledger-sources').innerHTML = '<p class="muted small">No ledger for this project yet.</p>';
    $('ledger-entries').innerHTML = '';
    return;
  }
  const sources = ledger.sources || [];
  $('ledger-sources').innerHTML = sources.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>Source</th><th>Families</th><th class="num">Queries</th><th class="num">Pages</th><th class="num">Results</th><th class="num">New records</th><th class="num">Empty</th><th class="num">Errors</th><th class="num">Time</th><th>Statuses</th></tr></thead>
      <tbody>${sources.map((source) => `
        <tr>
          <td><strong>${esc(source.source)}</strong>${source.error_detail ? `<div class="tiny" style="color:var(--warn)">${esc(source.error_detail.slice(0, 200))}</div>` : ''}</td>
          <td>${(source.families || []).map((f) => `<span class="chip">${esc(f)}</span>`).join(' ')}</td>
          <td class="num">${esc(source.queries)}</td>
          <td class="num">${esc(source.pages)}</td>
          <td class="num">${esc(source.results)}</td>
          <td class="num">${esc(source.new_records)}</td>
          <td class="num">${esc(source.empty)}</td>
          <td class="num">${source.errors ? `<span style="color:var(--bad)">${esc(source.errors)}</span>` : '0'}</td>
          <td class="num">${esc(source.duration_ms)} ms</td>
          <td class="tiny">${Object.entries(source.statuses || {}).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(' · ')}</td>
        </tr>`).join('')}</tbody>
    </table></div>` : '<p class="muted small">No source answered in this run.</p>';

  const entries = ledger.entries || [];
  $('ledger-entries').innerHTML = entries.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>Source</th><th>Family</th><th>Query</th><th class="num">Page</th><th class="num">Results</th><th class="num">New</th><th>Status</th><th class="num">Time</th><th>Detail / exclusions</th></tr></thead>
      <tbody>${entries.map((entry) => `
        <tr>
          <td>${esc(entry.source)}</td>
          <td class="tiny">${esc(entry.family)}</td>
          <td class="tiny"><code>${esc(entry.query)}</code></td>
          <td class="num">${esc(entry.page)}</td>
          <td class="num">${esc(entry.result_count)}</td>
          <td class="num">${esc(entry.new_records)}</td>
          <td>${esc(entry.status)}</td>
          <td class="num">${esc(entry.duration_ms)}</td>
          <td class="tiny">${esc(entry.detail || '')}${(entry.exclusions || []).length ? `<details><summary>${entry.exclusions.length} exclusion(s)</summary><ul>${entry.exclusions.map((x) => `<li>${esc(x.title)} — ${esc(x.reason || '')}</li>`).join('')}</ul></details>` : ''}</td>
        </tr>`).join('')}</tbody>
    </table></div>` : '<p class="muted small">No ledger entries.</p>';
}

async function loadLedgerEntries() {
  if (!state.projectId) return;
  try {
    const payload = await api(`/api/projects/${state.projectId}/ledger`);
    const entries = payload.entries || [];
    if (entries.length) renderLedger();
  } catch (error) { /* no run yet */ }
}

// ---------------------------------------------------------------------------
// datasets
// ---------------------------------------------------------------------------

function datasetFilters() {
  return {
    kind: 'dataset',
    q: $('f-q').value.trim() || null,
    modality: $('f-modality').value.trim() || null,
    access: $('f-access').value || null,
    license_contains: $('f-license').value.trim() || null,
    min_year: $('f-minyear').value ? Number($('f-minyear').value) : null,
    max_year: $('f-maxyear').value ? Number($('f-maxyear').value) : null,
    dataset_state: $('f-state').value || null,
    min_relevance: $('f-rel').value ? Number($('f-rel').value) : 0,
    limit: 200,
  };
}

async function loadDatasets() {
  const node = $('ds-list');
  if (!state.projectId) { node.innerHTML = '<p class="muted small">No project loaded.</p>'; return; }
  const filters = datasetFilters();
  const query = Object.entries(filters)
    .filter(([, value]) => value !== null && value !== '' && value !== 0 || value === 0)
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join('&');
  try {
    const payload = await api(`/api/projects/${state.projectId}/records?${query}`);
    state.datasetTotal = payload.total_matching;
    $('ds-count').textContent = `${payload.returned} of ${payload.total_matching} matching dataset record(s)`;
    renderDatasetList(payload.records || []);
  } catch (error) {
    node.innerHTML = `<p class="small" style="color:var(--bad)">${esc(error.message)}</p>`;
  }
}

function renderDatasetList(records) {
  const node = $('ds-list');
  if (!records.length) {
    node.innerHTML = `<div class="card"><p class="muted small">No dataset record matches the current filters, in what was actually retrieved. This is a statement about this search scope only — it is not evidence that no dataset exists worldwide. Widen the search (more sources, greater depth) before drawing any conclusion.</p></div>`;
    return;
  }
  node.innerHTML = records.map((record) => {
    const dataset = record.dataset || {};
    const selected = state.selection.has(record.id);
    const samples = dataset.samples !== undefined && dataset.samples !== null ? dataset.samples : null;
    const size = record.declared_size_bytes ? fmtBytes(record.declared_size_bytes) : (dataset.size_bytes ? fmtBytes(dataset.size_bytes) : null);
    const files = (dataset.files || []).length;
    return `<div class="record ${selected ? 'selected' : ''}" data-record="${record.id}">
      <h4>${esc(record.title || '(untitled)')}</h4>
      <div class="meta">
        <span class="chip">${esc(record.source)}</span>
        ${record.year ? `<span class="chip">${esc(record.year)}</span>` : ''}
        <span class="chip">relevance ${pct(record.relevance)}</span>
        ${verificationBadge(record.verification_status)}
        ${record.dataset_state ? `<span class="badge ${STATE_BADGE[record.dataset_state] || 'grey'}">${esc(record.dataset_state.replace(/_/g, ' '))}</span>` : ''}
      </div>
      <div class="meta">
        <strong>Access:</strong> ${esc(record.access || 'unknown')} ·
        <strong>Licence:</strong> ${record.license ? esc(record.license) : 'not published by the source'} ·
        <strong>Samples:</strong> ${samples !== null ? esc(samples) : 'not published'} ·
        <strong>Modality:</strong> ${esc(record.modality || dataset.modality || 'not published')} ·
        <strong>Format:</strong> ${esc(dataset.format || 'not published')} ·
        <strong>Size:</strong> ${size || 'not published'} ·
        <strong>Declared files:</strong> ${files}
      </div>
      ${dataset.description ? `<div class="small muted">${esc(String(dataset.description).slice(0, 320))}</div>` : ''}
      ${dataset.quality_concerns ? `<div class="small" style="color:var(--warn)">Quality concerns reported by source: ${esc(dataset.quality_concerns)}</div>` : ''}
      <div class="actions">
        <label class="tiny"><input type="checkbox" data-select="${record.id}" ${selected ? 'checked' : ''}> select for comparison</label>
        <button class="btn small ghost" data-detail="${record.id}" type="button">Inspect</button>
        ${record.downloadable ? `<button class="btn small" data-download="${record.id}" type="button">Download &amp; validate</button>` : ''}
        ${record.url ? `<a class="btn small ghost" href="${esc(record.url)}" target="_blank" rel="noopener">Open source record</a>` : ''}
        <span class="tiny muted">${record.evidence_count || 0} evidence reference(s)${record.downloadable ? '' : ` · ${esc(record.download_note || '')}`}</span>
      </div>
    </div>`;
  }).join('');

  node.querySelectorAll('[data-select]').forEach((box) => box.addEventListener('change', () => {
    const id = Number(box.dataset.select);
    if (box.checked) state.selection.add(id); else state.selection.delete(id);
    $('ds-selected-count').textContent = String(state.selection.size);
  }));
  node.querySelectorAll('[data-detail]').forEach((button) => button.addEventListener('click', () => showRecordDetail(Number(button.dataset.detail))));
  node.querySelectorAll('[data-download]').forEach((button) => button.addEventListener('click', () => startDownload(Number(button.dataset.download))));
}

$('btn-filter').addEventListener('click', loadDatasets);
$('btn-clear-sel').addEventListener('click', () => {
  state.selection.clear();
  $('ds-selected-count').textContent = '0';
  loadDatasets();
});

$('btn-compat').addEventListener('click', async () => {
  if (!state.projectId) { toast('Load a project first.', 'bad'); return; }
  if (!state.selection.size) { toast('Select at least one dataset record first.', 'bad'); return; }
  try {
    const job = await api(`/api/projects/${state.projectId}/compatibility`, {
      method: 'POST',
      body: JSON.stringify({ record_ids: Array.from(state.selection), intended_use: '' }),
    });
    state.jobId = job.job_id;
    showPanel('run');
    pollJob();
  } catch (error) {
    toast(error.message, 'bad');
  }
});

async function startDownload(recordId) {
  try {
    const job = await api(`/api/datasets/${recordId}/download`, { method: 'POST', body: JSON.stringify({ record_id: recordId }) });
    state.jobId = job.job_id;
    showPanel('run');
    pollJob();
    toast(`Download job started for record #${recordId}.`, 'ok');
  } catch (error) {
    toast(`Download not started: ${error.message}`, 'bad');
  }
}

// ---------------------------------------------------------------------------
// record detail
// ---------------------------------------------------------------------------

async function showRecordDetail(recordId) {
  const from = activePanelName();
  if (from !== 'record') recordReturnTo = from;
  const body = $('record-body');
  body.innerHTML = '<p class="muted small">Loading record…</p>';
  showPanel('record');
  let payload;
  try {
    payload = await api(`/api/records/${recordId}`);
  } catch (error) {
    body.innerHTML = `<p class="small" style="color:var(--bad)">${esc(error.message)}</p>`;
    return;
  }
  const record = payload.record;
  const dataset = record.dataset || {};
  const checks = payload.verifications || [];
  const evidence = payload.evidence || [];
  const downloads = payload.downloads || [];
  const stateInfo = state.meta ? state.meta.dataset_state_meanings : {};
  const kindLabel = record.kind === 'dataset' ? 'Dataset' : record.kind === 'paper' ? 'Paper' : (record.kind || 'Record');
  $('record-origin').textContent = `opened from ${from}`;

  body.innerHTML = `
    <header class="rec-head">
      <span class="rec-kind">${esc(kindLabel)} · ${esc(record.source)}</span>
      <h2 class="rec-title">${esc(record.title || '(untitled)')}</h2>
      <p class="rec-sub">${esc(record.source)}:${esc(record.source_id)} · retrieved ${esc(fmtDate(record.retrieved_at))}</p>
      <div class="chips" style="margin-bottom:10px">
        ${verificationBadge(record.verification_status)}
        ${record.dataset_state ? `<span class="badge ${STATE_BADGE[record.dataset_state] || 'grey'}">${esc(record.dataset_state.replace(/_/g, ' '))}</span>` : ''}
        <span class="chip">access: ${esc(record.access || 'unknown')}</span>
        <span class="chip">licence: ${esc(record.license || 'not published')}</span>
        <span class="chip">relevance: ${pct(record.relevance)}</span>
        <span class="chip">evidence: ${evidence.length}</span>
      </div>
      <div class="rec-linkrow">
        ${record.doi ? `<a class="btn small ghost" href="https://doi.org/${esc(record.doi)}" target="_blank" rel="noopener">Resolve DOI</a>` : ''}
        ${record.url ? `<a class="btn small ghost" href="${esc(record.url)}" target="_blank" rel="noopener">Open source</a>` : ''}
        ${record.pdf_url ? `<a class="btn small ghost" href="${esc(record.pdf_url)}" target="_blank" rel="noopener">Open PDF</a>` : ''}
        <button class="btn small ghost" id="record-back-bottom" type="button">← Back to ${esc(from)}</button>
      </div>
    </header>
    ${record.dataset_state && stateInfo[record.dataset_state] ? `<div class="banner">${esc(stateInfo[record.dataset_state])}</div>` : ''}

    <h3>Identifiers and links</h3>
    <dl class="kv">
      <dt>DOI</dt><dd>${record.doi ? esc(record.doi) : 'not published'}</dd>
      <dt>Source URL</dt><dd>${record.url ? `<a href="${esc(record.url)}" target="_blank" rel="noopener">${esc(record.url)}</a>` : 'not published'}</dd>
      <dt>PDF URL</dt><dd>${record.pdf_url ? `<a href="${esc(record.pdf_url)}" target="_blank" rel="noopener">${esc(record.pdf_url)}</a>` : 'not published'}</dd>
      <dt>Publisher / venue</dt><dd>${esc(record.publisher || record.venue || 'not published')}</dd>
      <dt>Year</dt><dd>${record.year || 'not published'}</dd>
      <dt>Authors</dt><dd>${(record.authors || []).map(esc).join(', ') || 'not published'}</dd>
      <dt>Citations</dt><dd>${record.citation_count !== null && record.citation_count !== undefined ? esc(record.citation_count) : 'not published'}</dd>
    </dl>

    ${record.abstract ? `<h3>Abstract</h3><p class="small">${esc(record.abstract)}</p>` : '<h3>Abstract</h3><p class="small muted">No abstract was published by the source.</p>'}

    ${record.kind === 'dataset' ? `
      <h3>Dataset fields (as published)</h3>
      <dl class="kv">
        <dt>Samples / records</dt><dd>${dataset.samples !== undefined && dataset.samples !== null ? esc(dataset.samples) : 'not published'}</dd>
        <dt>Classes / labels</dt><dd>${dataset.classes !== undefined && dataset.classes !== null ? esc(JSON.stringify(dataset.classes)) : 'not published'}</dd>
        <dt>Modality</dt><dd>${esc(dataset.modality || record.modality || 'not published')}</dd>
        <dt>Format</dt><dd>${esc(dataset.format || 'not published')}</dd>
        <dt>Size</dt><dd>${dataset.size_bytes ? fmtBytes(dataset.size_bytes) : 'not published'}</dd>
        <dt>Methodology</dt><dd>${esc(dataset.methodology || 'not published')}</dd>
        <dt>Geography</dt><dd>${esc(dataset.geography || 'not published')}</dd>
        <dt>Time period</dt><dd>${esc(dataset.time_period || 'not published')}</dd>
        <dt>Version</dt><dd>${esc(dataset.version || 'not published')}</dd>
        <dt>Updated</dt><dd>${esc(dataset.updated || 'not published')}</dd>
        <dt>Provenance</dt><dd>${esc(dataset.provenance || 'not published')}</dd>
        <dt>Quality concerns</dt><dd>${esc(dataset.quality_concerns || 'none reported by the source')}</dd>
        <dt>Related publications</dt><dd>${(dataset.related_publications || []).map(esc).join('<br>') || 'none published'}</dd>
        <dt>Declared files</dt><dd>${(dataset.files || []).length ? (dataset.files || []).map((f) => `<div class="tiny">${esc(f.name || f.url)}${f.size_bytes ? ` · ${fmtBytes(f.size_bytes)}` : ''}${f.checksum ? ` · checksum ${esc(f.checksum)}` : ''}</div>`).join('') : 'not published'}</dd>
      </dl>
      <h3>Download availability</h3>
      <p class="small">${payload.downloadable && payload.downloadable[0] ? `Attemptable: ${esc(payload.downloadable[1])}` : `Not downloadable automatically: ${esc((payload.downloadable && payload.downloadable[1]) || 'unknown')}`}</p>
      ${downloads.length ? `<div class="table-wrap"><table><thead><tr><th>URL</th><th>Status</th><th class="num">Written</th><th>SHA-256</th><th>Error</th></tr></thead><tbody>${downloads.map((d) => `<tr><td class="tiny">${esc(d.url)}</td><td>${esc(d.status)}</td><td class="num">${esc(fmtBytes(d.bytes_written || 0))}</td><td class="tiny">${esc(d.sha256 || '—')}</td><td class="tiny" style="color:var(--warn)">${esc(d.error || '')}</td></tr>`).join('')}</tbody></table></div>` : '<p class="small muted">No download has been attempted for this record.</p>'}
    ` : ''}

    <h3>Verification checks (${checks.length})</h3>
    ${checks.length ? `<div class="table-wrap"><table><thead><tr><th>Check</th><th>Status</th><th>Method</th><th>Detail</th><th>Evidence</th><th>Checked</th></tr></thead><tbody>${checks.map((check) => `
      <tr><td>${esc(check.check_name)}</td><td>${verificationBadge(check.status, check.status)}</td><td class="tiny">${esc(check.method)}</td><td class="tiny">${esc(check.detail)}</td><td class="tiny">${check.evidence_url ? `<a href="${esc(check.evidence_url)}" target="_blank" rel="noopener">link</a>` : '—'}</td><td class="tiny">${esc(fmtDate(check.checked_at))}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="small muted">No verification was performed for this record yet. "No verification" is not the same as "verified".</p>'}

    <h3>Evidence (${evidence.length})</h3>
    ${evidence.length ? evidence.map((item) => `
      <div class="passage">
        <div><strong>${esc(item.claim)}</strong>: ${esc(item.passage)}</div>
        <div class="where">locator: ${esc(item.locator || 'not recorded')} · field: ${esc(item.field || '—')} · origin: ${esc(item.origin)} · retrieved ${esc(fmtDate(item.retrieved_at))}${item.url ? ` · <a href="${esc(item.url)}" target="_blank" rel="noopener">source</a>` : ''}</div>
      </div>`).join('')
      : '<p class="small muted">No evidence passage was extracted for this record. No claims are made about it.</p>'}
    ${record.abstract ? '<p class="tiny muted">Metadata-level passages come from the source\'s own metadata. That is provenance, not independent confirmation.</p>' : ''}
  `;
  const backBottom = $('record-back-bottom');
  if (backBottom) backBottom.addEventListener('click', () => showPanel(recordReturnTo));
}

// ---------------------------------------------------------------------------
// literature
// ---------------------------------------------------------------------------

async function loadLiterature() {
  const node = $('lit-list');
  if (!state.projectId) { node.innerHTML = '<p class="muted small">No project loaded.</p>'; return; }
  const params = new URLSearchParams({ kind: 'paper', limit: '200' });
  if ($('lit-q').value.trim()) params.set('q', $('lit-q').value.trim());
  if ($('lit-ver').value) params.set('verification', $('lit-ver').value);
  if ($('lit-minyear').value) params.set('min_year', $('lit-minyear').value);
  if ($('lit-rel').value) params.set('min_relevance', $('lit-rel').value);
  try {
    const payload = await api(`/api/projects/${state.projectId}/records?${params.toString()}`);
    const records = payload.records || [];
    node.innerHTML = records.length ? records.map((record) => {
      const raw = record.raw || {};
      return `<div class="record">
        <h4>${esc(record.title || '(untitled)')}</h4>
        <div class="meta">
          <span class="chip">${esc(record.source)}</span>
          ${record.year ? `<span class="chip">${esc(record.year)}</span>` : ''}
          <span class="chip">relevance ${pct(record.relevance)}</span>
          <span class="chip">citations ${record.citation_count !== null && record.citation_count !== undefined ? esc(record.citation_count) : 'n/a'}</span>
          ${verificationBadge(record.verification_status)}
          <span class="chip">${record.evidence_count || 0} evidence</span>
          ${raw.expansion ? `<span class="chip">${esc(raw.expansion)}</span>` : ''}
        </div>
        <div class="small muted">${esc((record.authors || []).slice(0, 6).join(', ') || 'authors not published')} · ${esc(record.venue || record.publisher || 'venue not published')}</div>
        <div class="actions">
          <button class="btn small ghost" data-detail="${record.id}" type="button">Evidence &amp; verification</button>
          ${record.doi ? `<a class="btn small ghost" href="https://doi.org/${esc(record.doi)}" target="_blank" rel="noopener">Resolve DOI</a>` : ''}
          ${record.url ? `<a class="btn small ghost" href="${esc(record.url)}" target="_blank" rel="noopener">Open source</a>` : ''}
          ${record.pdf_url ? `<a class="btn small ghost" href="${esc(record.pdf_url)}" target="_blank" rel="noopener">Open PDF</a>` : ''}
        </div>
      </div>`;
    }).join('') : '<div class="card"><p class="muted small">No literature record matches the filters among the records actually retrieved.</p></div>';
    node.querySelectorAll('[data-detail]').forEach((button) => button.addEventListener('click', () => showRecordDetail(Number(button.dataset.detail))));
  } catch (error) {
    node.innerHTML = `<p class="small" style="color:var(--bad)">${esc(error.message)}</p>`;
  }
}

$('btn-lit-filter').addEventListener('click', loadLiterature);

// ---------------------------------------------------------------------------
// evidence matrix
// ---------------------------------------------------------------------------

function renderMatrix(entries) {
  state.matrix = entries;
  const node = $('matrix-table');
  if (!entries.length) {
    node.innerHTML = '<div class="card"><p class="muted small">No literature record with retrievable content is stored for this project, so no matrix can be built.</p></div>';
    return;
  }
  const columns = [
    ['title', 'Record'], ['research_question', 'Research question'], ['claimed_contribution', 'Contribution'],
    ['methods', 'Methods'], ['datasets_used', 'Datasets'], ['baselines', 'Baselines'], ['evaluation_metrics', 'Metrics'],
    ['results_reported', 'Results reported'], ['uncertainty', 'Uncertainty'], ['data_availability', 'Data availability'],
    ['code_availability', 'Code availability'], ['replication_evidence', 'Replication'], ['conflicting_findings', 'Conflicting findings'],
  ];
  node.innerHTML = `<table>
    <thead><tr>${columns.map(([, label]) => `<th>${esc(label)}</th>`).join('')}<th>Limitations</th><th>Future work</th></tr></thead>
    <tbody>${entries.map((entry) => `<tr>
      <td><a href="#" data-detail="${entry.record_id}">${esc(String(entry.title).slice(0, 70))}</a><div class="tiny muted">${esc(entry.source)} ${esc(entry.year || '')}</div></td>
      ${columns.slice(1).map(([key]) => `<td class="tiny">${esc(String(entry[key] || '').slice(0, 260))}</td>`).join('')}
      <td class="tiny">${(entry.stated_limitations || []).map((s) => esc(s.slice(0, 160))).join('<br>') || '—'}</td>
      <td class="tiny">${(entry.stated_future_work || []).map((s) => esc(s.slice(0, 160))).join('<br>') || '—'}</td>
    </tr>`).join('')}</tbody></table>`;
  node.querySelectorAll('[data-detail]').forEach((link) => link.addEventListener('click', (event) => {
    event.preventDefault();
    showRecordDetail(Number(link.dataset.detail));
  }));
}

$('btn-matrix').addEventListener('click', async () => {
  if (!state.projectId) { toast('Load a project first.', 'bad'); return; }
  try {
    const payload = await api(`/api/projects/${state.projectId}/evidence-matrix`);
    renderMatrix(payload.entries || []);
    toast(`${(payload.entries || []).length} matrix entries built.`, 'ok');
  } catch (error) { toast(error.message, 'bad'); }
});

// ---------------------------------------------------------------------------
// gaps
// ---------------------------------------------------------------------------

function renderGaps(gaps) {
  const node = $('gap-list');
  if (!gaps || !gaps.length) {
    node.innerHTML = `<div class="card"><p class="muted small">No gap candidate is stored for this project. Gaps are only reported when retrieved text supports them; a topic returning few results is never presented as unexplored.</p></div>`;
    return;
  }
  node.innerHTML = gaps.map((gap) => `
    <div class="card">
      <div class="chips" style="margin-bottom:6px">
        <span class="chip">${esc(gap.gap_type)}</span>
        <span class="chip">${(gap.supporting_records || []).length} supporting record(s)</span>
        <span class="chip">${(gap.counterevidence || []).length} counter-evidence item(s)</span>
        <span class="chip">${esc(gap.confidence_basis || '')}</span>
      </div>
      <p><strong>${esc(gap.proposed_gap)}</strong></p>
      <p class="small muted">${esc(gap.why_it_matters || '')}</p>
      <details><summary>Supporting records and passages</summary>
        <ul class="small">${(gap.supporting_records || []).map((record) => `<li>${esc(record.title)} <span class="tiny muted">(${esc(record.source)}${record.year ? ', ' + esc(record.year) : ''})</span>${record.url ? ` — <a href="${esc(record.url)}" target="_blank" rel="noopener">source</a>` : ''}</li>`).join('')}</ul>
        ${(gap.supporting_passages || []).map((passage) => `<div class="passage">${esc(passage.text)}<div class="where">${esc(passage.locator || '')} · ${esc(passage.title || '')}${passage.url ? ` · <a href="${esc(passage.url)}" target="_blank" rel="noopener">source</a>` : ''}</div></div>`).join('')}
      </details>
      <details><summary>Counter-evidence found in the same search</summary>
        ${(gap.counterevidence || []).length ? `<ul class="small">${gap.counterevidence.map((item) => `<li>${esc(item.title)} — ${esc(String(item.claim || '').slice(0, 220))}${item.url ? ` — <a href="${esc(item.url)}" target="_blank" rel="noopener">source</a>` : ''}</li>`).join('')}</ul>` : '<p class="small muted">None found in the retrieved set. That is not proof that none exists.</p>'}
      </details>
      <dl class="kv">
        <dt>Feasible research question</dt><dd>${esc(gap.feasible_research_question || '')}</dd>
        <dt>Proposed methodology</dt><dd>${esc(gap.proposed_methodology || '')}</dd>
        <dt>Additional requirements</dt><dd>${(gap.additional_requirements || []).map(esc).join('<br>') || '—'}</dd>
        <dt>Uncertainty</dt><dd>${(gap.uncertainty || []).map(esc).join('<br>') || '—'}</dd>
      </dl>
    </div>`).join('');
}

$('btn-gaps').addEventListener('click', async () => {
  if (!state.projectId) { toast('Load a project first.', 'bad'); return; }
  try {
    const job = await api(`/api/projects/${state.projectId}/gaps`, { method: 'POST', body: JSON.stringify({ run_id: null }) });
    state.jobId = job.job_id;
    showPanel('run');
    pollJob();
  } catch (error) { toast(error.message, 'bad'); }
});

// ---------------------------------------------------------------------------
// manuscript
// ---------------------------------------------------------------------------

const SECTION_BADGE = {
  evidence_supported: 'ok',
  needs_user_input: 'info',
  needs_research: 'warn',
  cannot_complete_yet: 'grey',
};

function renderManuscript(manuscript) {
  state.manuscript = manuscript;
  const sectionsNode = $('manuscript-sections');
  const checklistNode = $('checklist');
  if (!manuscript) {
    sectionsNode.innerHTML = '<div class="card"><p class="muted small">No manuscript has been assembled yet. Press "Assemble manuscript" to build it from the stored, cited evidence.</p></div>';
    checklistNode.innerHTML = '<p class="muted small">Not built yet.</p>';
    return;
  }
  const counts = manuscript.status_counts || {};
  checklistNode.innerHTML = `
    <div class="chips" style="margin-bottom:10px">${Object.entries(counts).map(([key, value]) => `<span class="badge ${SECTION_BADGE[key] || 'grey'}">${esc(key.replace(/_/g, ' '))}: ${esc(value)}</span>`).join('')}</div>
    <table><tbody>${(manuscript.checklist || []).map((item) => `<tr><td style="width:28px">${item.satisfied ? '✅' : '⬜'}</td><td>${esc(item.item)}<div class="tiny muted">${esc(item.evidence)}</div></td></tr>`).join('')}</tbody></table>
    <p class="tiny muted" style="margin-top:8px">${esc(manuscript.disclaimer || '')}</p>`;

  sectionsNode.innerHTML = (manuscript.sections || []).map((section) => `
    <div class="card">
      <h3 style="margin-top:0">${esc(section.title)}</h3>
      <div class="section-status">${verificationBadge(SECTION_BADGE[section.status] === 'ok' ? 'verified' : 'inconclusive', section.status_label || section.status)}</div>
      <p class="small">${esc(section.summary)}</p>
      ${(section.paragraphs || []).map((paragraph) => `<p class="small">${esc(paragraph)}</p>`).join('')}
      ${section.table ? `<div class="table-wrap"><table><thead><tr>${section.table.headers.map((header) => `<th>${esc(header)}</th>`).join('')}</tr></thead><tbody>${section.table.rows.map((row) => `<tr>${row.map((cell) => `<td class="tiny">${esc(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : ''}
      ${(section.pending_items || []).length ? `<details open><summary>Pending / requires input</summary><ul class="small">${section.pending_items.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></details>` : ''}
      ${(section.citations || []).length ? `<div class="tiny muted">Citations: ${(section.citations || []).map(esc).join(' ')}</div>` : ''}
    </div>`).join('');

  sectionsNode.insertAdjacentHTML('beforeend', `
    <div class="card">
      <h3 style="margin-top:0">References (${(manuscript.references || []).length})</h3>
      <div class="table-wrap"><table><thead><tr><th>#</th><th>Reference</th><th>Identifier</th><th>Verification</th><th>Retrieved</th></tr></thead>
      <tbody>${(manuscript.references || []).map((reference) => `<tr>
        <td class="num">${esc(reference.index)}</td>
        <td class="tiny">${esc(reference.text)} ${reference.url ? `<a href="${esc(reference.url)}" target="_blank" rel="noopener">link</a>` : ''}</td>
        <td class="tiny">${esc(reference.identifier)}</td>
        <td>${verificationBadge(reference.verification, reference.verification)}</td>
        <td class="tiny">${esc(fmtDate(reference.retrieved_at))}</td>
      </tr>`).join('')}</tbody></table></div>
    </div>`);
}

$('btn-manuscript').addEventListener('click', async () => {
  if (!state.projectId) { toast('Load a project first.', 'bad'); return; }
  try {
    const job = await api(`/api/projects/${state.projectId}/manuscript`, { method: 'POST', body: JSON.stringify({ run_id: null }) });
    state.jobId = job.job_id;
    showPanel('run');
    pollJob();
  } catch (error) { toast(error.message, 'bad'); }
});

$$('[data-export]').forEach((button) => button.addEventListener('click', () => {
  if (!state.projectId) { toast('Load a project first.', 'bad'); return; }
  window.open(`/api/projects/${state.projectId}/manuscript/export?fmt=${button.dataset.export}`, '_blank');
}));

// ---------------------------------------------------------------------------
// downloads
// ---------------------------------------------------------------------------

function renderDownloads() {
  const snapshot = state.snapshot;
  const node = $('dl-list');
  if (!snapshot) { node.innerHTML = ''; return; }
  const downloads = snapshot.downloads || [];
  const verified = downloads.filter((item) => item.status === 'verified').length;
  const failed = downloads.filter((item) => item.status === 'failed').length;
  const bytes = downloads.reduce((sum, item) => sum + (item.bytes_written || 0), 0);
  $('dl-stats').textContent = `${downloads.length} download attempt(s) · ${verified} verified · ${failed} failed · ${fmtBytes(bytes)} written`;
  node.innerHTML = downloads.length ? `<div class="card"><div class="table-wrap"><table>
    <thead><tr><th>Record</th><th>URL</th><th>Status</th><th class="num">Size</th><th>SHA-256</th><th>Type</th><th>Started</th><th>Error</th></tr></thead>
    <tbody>${downloads.map((item) => {
      const record = (snapshot.records || []).find((r) => r.id === item.record_id);
      return `<tr>
        <td class="tiny">${esc(record ? String(record.title).slice(0, 60) : `#${item.record_id}`)}</td>
        <td class="tiny">${esc(String(item.url).slice(0, 90))}</td>
        <td>${esc(item.status)}</td>
        <td class="num">${esc(fmtBytes(item.bytes_written || 0))}${item.bytes_total ? ` / ${esc(fmtBytes(item.bytes_total))}` : ''}</td>
        <td class="tiny">${esc(item.sha256 || 'not recorded')}</td>
        <td class="tiny">${esc(item.content_type || '')}</td>
        <td class="tiny">${esc(fmtDate(item.started_at))}</td>
        <td class="tiny" style="color:var(--warn)">${esc(item.error || '')}</td>
      </tr>`;
    }).join('')}</tbody></table></div></div>`
    : '<div class="card"><p class="muted small">No download has been attempted in this project.</p></div>';
}

$('btn-manifest').addEventListener('click', async () => {
  if (!state.projectId) { toast('Load a project first.', 'bad'); return; }
  try {
    const manifest = await api(`/api/projects/${state.projectId}/manifest`);
    openModal(`<h2>Collection manifest</h2><p class="small muted">${esc(manifest.disclaimer)}</p>
      <div class="chips" style="margin:8px 0">${Object.entries(manifest.totals || {}).map(([key, value]) => `<span class="chip">${esc(key)}: ${esc(value)}</span>`).join('')}</div>
      <pre class="log" style="max-height:420px">${esc(JSON.stringify(manifest, null, 2))}</pre>`);
  } catch (error) { toast(error.message, 'bad'); }
});

$('btn-manifest-dl').addEventListener('click', () => {
  if (!state.projectId) { toast('Load a project first.', 'bad'); return; }
  window.open(`/api/projects/${state.projectId}/manifest/download`, '_blank');
});

$('btn-purge').addEventListener('click', async () => {
  try {
    const result = await api('/api/downloads/purge-partial', { method: 'POST' });
    toast(`${result.removed_partial_files} partial file(s) removed.`, 'ok');
  } catch (error) { toast(error.message, 'bad'); }
});

// ---------------------------------------------------------------------------
// projects
// ---------------------------------------------------------------------------

function renderProjects() {
  const node = $('projects-list');
  if (!state.projects.length) {
    node.innerHTML = '<div class="card"><p class="muted small">No project yet. Ask a research question to create one.</p></div>';
    return;
  }
  node.innerHTML = state.projects.map((project) => {
    const run = project.latest_run;
    return `<div class="record ${project.id === state.projectId ? 'selected' : ''}">
      <h4>#${esc(project.id)} · ${esc(project.title || '(untitled)')}</h4>
      <div class="meta">${esc(project.question)}</div>
      <div class="meta">
        created ${esc(fmtDate(project.created_at))} · updated ${esc(fmtDate(project.updated_at))}
        ${run ? ` · latest run #${esc(run.id)} (${esc(run.status)})` : ' · no run yet'}
      </div>
      <div class="actions">
        <button class="btn small" data-open="${project.id}" type="button">Open</button>
        <button class="btn small ghost" data-run="${project.id}" type="button">Re-run this question</button>
        <button class="btn small danger" data-delete="${project.id}" type="button">Delete</button>
      </div>
    </div>`;
  }).join('');
  node.querySelectorAll('[data-open]').forEach((button) => button.addEventListener('click', async () => {
    await openProject(Number(button.dataset.open));
    showPanel('datasets');
  }));
  node.querySelectorAll('[data-run]').forEach((button) => button.addEventListener('click', () => {
    const project = state.projects.find((item) => item.id === Number(button.dataset.run));
    if (!project) return;
    $('question').value = project.question;
    showPanel('workspace');
    toast('Question loaded into the workspace. Press "Run research" to search again.', '');
  }));
  node.querySelectorAll('[data-delete]').forEach((button) => button.addEventListener('click', async () => {
    const projectId = Number(button.dataset.delete);
    if (!window.confirm(`Delete project #${projectId} and all of its records? This cannot be undone.`)) return;
    try {
      await api(`/api/projects/${projectId}`, { method: 'DELETE' });
      if (state.projectId === projectId) { state.projectId = null; state.snapshot = null; $('project-chip').className = 'badge grey'; $('project-chip').textContent = 'no project'; }
      await loadProjects();
      toast(`Project #${projectId} deleted.`, 'ok');
    } catch (error) { toast(error.message, 'bad'); }
  }));
}

// ---------------------------------------------------------------------------
// policy
// ---------------------------------------------------------------------------

function renderPolicy() {
  const meta = state.meta;
  if (!meta) return;
  $('policy-body').innerHTML = `
    <div class="banner"><strong>Central principle.</strong> ${esc(meta.policy)}</div>
    <h3>Exact response when no suitable dataset is found</h3>
    <p class="mono" style="background:var(--panel-2);padding:8px;border-radius:6px">${esc(meta.insufficient_dataset_message)}</p>
    <p class="small muted">That message is returned only when the configured scope was adequately explored and no suitable, accessible, verifiable dataset was found. A failed source is never counted as evidence of absence.</p>
    <h3>Dataset stages</h3>
    <table><tbody>${Object.entries(meta.dataset_state_meanings || {}).map(([key, value]) => `<tr><td><code>${esc(key)}</code></td><td class="small">${esc(value)}</td></tr>`).join('')}</tbody></table>
    <h3>Verification statuses</h3>
    <div class="chips">${(meta.verification_statuses || []).map((status) => verificationBadge(status, status)).join(' ')}</div>
    <h3>Evidence sufficiency statuses</h3>
    <div class="chips">${(meta.sufficiency_statuses || []).map((status) => `<span class="chip">${esc(status)}</span>`).join(' ')}</div>
    <h3>Manuscript section statuses</h3>
    <table><tbody>${Object.entries(meta.manuscript_section_statuses || {}).map(([key, value]) => `<tr><td><code>${esc(key)}</code></td><td class="small">${esc(value)}</td></tr>`).join('')}</tbody></table>
    <h3>What the platform will not do</h3>
    <ul class="small">
      <li>It does not invent datasets, papers, citations, counts, results, significance, ethics approvals or novelty.</li>
      <li>It does not treat metadata copied from one source as independent confirmation.</li>
      <li>It does not bypass logins, paywalls or licence restrictions.</li>
      <li>It does not claim internet-wide coverage: results are always scoped to the adapters that actually answered.</li>
      <li>It does not write a results or conclusion section before experiments exist.</li>
    </ul>`;
}

function renderAdapterTable() {
  const node = $('adapter-table');
  const rows = state.adapters.map((adapter) => `
    <tr>
      <td><strong>${esc(adapter.label)}</strong><div class="tiny muted">${esc(adapter.name)}</div></td>
      <td>${adapter.available ? '<span class="badge ok">available</span>' : '<span class="badge bad">unavailable</span>'}</td>
      <td>${(adapter.families || []).map((f) => `<span class="chip">${esc(f)}</span>`).join(' ')}</td>
      <td class="tiny">${esc(adapter.kind || '')}</td>
      <td class="tiny">${adapter.requires_credentials ? (adapter.credentials_present ? 'key configured' : `no key (${esc(adapter.credential_hint || 'optional')})`) : 'no key required'}</td>
      <td class="tiny">${esc(adapter.notes || '')}${adapter.docs_url ? ` <a href="${esc(adapter.docs_url)}" target="_blank" rel="noopener">docs</a>` : ''}</td>
    </tr>`).join('');
  node.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Adapter</th><th>State</th><th>Families</th><th>Kind</th><th>Credentials</th><th>Notes</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

// ---------------------------------------------------------------------------
// logs
// ---------------------------------------------------------------------------

async function refreshLogs() {
  try {
    const payload = await api('/api/logs?limit=250');
    const view = $('log-view');
    const wasAtBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 20;
    view.textContent = (payload.logs || []).map((item) => {
      const extras = Object.entries(item).filter(([key]) => !['ts', 'level', 'logger', 'event'].includes(key))
        .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`).join(' ');
      return `${item.ts} ${item.level.padEnd(7)} ${item.logger} — ${item.event}${extras ? ' ' + extras : ''}`;
    }).join('\n') || 'No log events recorded yet.';
    if ($('logs-autoscroll').checked && wasAtBottom) view.scrollTop = view.scrollHeight;
  } catch (error) {
    $('log-view').textContent = `Could not load logs: ${error.message}`;
  }
}

$('btn-logs').addEventListener('click', refreshLogs);
setInterval(() => { if ($('panel-ops').classList.contains('active')) refreshLogs(); }, 5000);

// ---------------------------------------------------------------------------
// go
// ---------------------------------------------------------------------------

init().catch((error) => {
  setBanner(`Startup failed: ${esc(error.message)}`, 'bad');
});
