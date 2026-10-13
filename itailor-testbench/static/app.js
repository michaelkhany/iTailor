// iTailor testbench - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
'use strict';

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts) => {
  const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
};
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pretty = (o) => JSON.stringify(o, null, 2);

// ---------------------------------------------------------------- tabs
document.querySelectorAll('#tabs button').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('#tabs button').forEach((x) => x.classList.toggle('active', x === b));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + b.dataset.tab));
  const loaders = { env: loadEnv, models: null, runs: loadRuns, lessons: loadLessons, graph: loadGraph, files: loadFiles, settings: loadSettings };
  if (loaders[b.dataset.tab]) loaders[b.dataset.tab]();
}));

// ---------------------------------------------------------------- status
async function loadStatus() {
  try {
    const s = await api('/api/status');
    const m = s.settings;
    $('#status-line').textContent = `library ${s.version} from ${s.importedFrom} · workspace ${s.workspace} · model ${m.model || 'auto'} @ ${m.baseUrl || '(auto-discover)'} · key ${m.apiKeySet ? 'set' : 'NOT set'} · mode ${m.permissionMode}`;
  } catch (e) { $('#status-line').textContent = 'status failed: ' + e.message; }
}
loadStatus();

// ---------------------------------------------------------------- run
let current = null; // {id, source}
document.querySelectorAll('.examples a').forEach((a) => a.addEventListener('click', (e) => {
  e.preventDefault();
  $('#goal').value = a.dataset.goal;
  $('#checks').value = a.dataset.checks || '';
}));

function logEvent(ev) {
  const showAll = $('#show-all').checked;
  if (!showAll && ['usage', 'delta', 'output', 'heartbeat'].includes(ev.type)) return;
  const log = $('#log');
  const div = document.createElement('div');
  div.className = 'ev ' + ev.type;
  let detail = '';
  const extra = Object.assign({}, ev);
  ['seq', 'ts', 'type', 'message'].forEach((k) => delete extra[k]);
  if (Object.keys(extra).length) detail = `<details><summary>details</summary><pre>${esc(pretty(extra))}</pre></details>`;
  div.innerHTML = `<span class="t">${esc(ev.type)}</span>${esc(ev.message || '')}${detail}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function renderOversight(payload) {
  const box = $('#oversight');
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.requestId = payload.requestId;
  if (payload.requestType === 'consult') {
    const options = (payload.options || []).map((o) => `<button class="opt">${esc(o)}</button>`).join('');
    card.innerHTML = `<h4>The agent needs your decision</h4><div class="meta">trigger: ${esc(payload.trigger || '')} ${payload.why ? '· ' + esc(payload.why) : ''}</div>
      <div>${esc(payload.question)}</div><div class="actions">${options}<input class="free" placeholder="or type an answer" style="width:260px"><button class="send">answer</button><button class="skip">no answer</button></div>`;
    card.querySelectorAll('.opt').forEach((b) => b.addEventListener('click', () => answer(payload.requestId, { answer: b.textContent }, card)));
    card.querySelector('.send').addEventListener('click', () => answer(payload.requestId, { answer: card.querySelector('.free').value }, card));
    card.querySelector('.skip').addEventListener('click', () => answer(payload.requestId, { answer: null }, card));
  } else {
    const risk = payload.risk ? `<span class="risk-${esc(payload.risk.level)}">risk ${esc(payload.risk.level)} (score ${payload.risk.score})</span>` : '';
    const hints = (payload.hints || []).map((h) => `<div class="meta">note: ${esc(h)}</div>`).join('');
    card.innerHTML = `<h4>Permission: ${esc(payload.summary)}</h4><div class="meta">${esc(payload.kind)} · ${esc(payload.tool)} ${risk}</div>
      ${payload.detail ? `<div>${esc(payload.detail)}</div>` : ''}${payload.command ? `<div><code>${esc(payload.command)}</code></div>` : ''}${hints}
      <div class="actions"><button class="primary" data-d="allow">Allow once</button><button data-d="always">Always (this kind, this session)</button><button data-d="deny">Deny</button></div>`;
    card.querySelectorAll('[data-d]').forEach((b) => b.addEventListener('click', () => answer(payload.requestId, { decision: b.dataset.d }, card)));
  }
  box.appendChild(card);
}

async function answer(requestId, body, card) {
  if (!current) return;
  await api(`/api/run/${current.id}/answer`, { method: 'POST', body: JSON.stringify(Object.assign({ requestId }, body)) });
  card.remove();
}

function showResult(status, result) {
  const box = $('#result');
  box.classList.remove('hidden', 'ok', 'bad');
  box.classList.add(['succeeded', 'done'].includes(status) ? 'ok' : 'bad');
  let text = `status: ${status}\n`;
  if (result) {
    if (result.summary) text += `\n${result.summary}\n`;
    if (result.text) text += `\n${result.text}\n`;
    if (result.stdout) text += `\nstdout:\n${result.stdout}\n`;
    if (result.error) text += `\nerror: ${result.error}\n`;
    if (result.files && result.files.length) text += `\nfiles: ${result.files.join(', ')}\n`;
    if (result.report_path) text += `\nreport: ${result.report_path}\n`;
    if (result.checks && result.checks.length) text += `\nchecks: ${result.checks.map((c) => `${c.passed ? 'PASS' : 'FAIL'} ${c.name}`).join('; ')}\n`;
    if (result.usage) text += `\ntokens: ${result.usage.totalTokens} in ${result.usage.calls} call(s)`;
  }
  box.textContent = text;
}

$('#start').addEventListener('click', async () => {
  const goal = $('#goal').value.trim();
  if (!goal) return;
  $('#log').innerHTML = '';
  $('#oversight').innerHTML = '';
  $('#result').classList.add('hidden');
  const body = { goal, engine: $('#engine').value, mode: $('#mode').value || null, language: $('#language').value,
    model: $('#model').value.trim() || null, checks: $('#checks').value.split('\n').map((s) => s.trim()).filter(Boolean) };
  try {
    const run = await api('/api/run', { method: 'POST', body: JSON.stringify(body) });
    current = { id: run.id };
    $('#run-status').textContent = `run ${run.id} started`;
    $('#start').disabled = true; $('#stop').disabled = false;
    const source = new EventSource(`/api/run/${run.id}/events`);
    current.source = source;
    source.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.type === 'oversight') { renderOversight(ev); logEvent({ type: ev.requestType === 'consult' ? 'consult_request' : 'permission_request', message: ev.summary || ev.question }); return; }
      logEvent(ev);
      if (ev.type === 'bench_end') {
        showResult(ev.status, ev.result);
        $('#run-status').textContent = `run ${run.id}: ${ev.status}`;
        $('#start').disabled = false; $('#stop').disabled = true;
        source.close();
        loadStatus();
      }
    };
    source.onerror = () => { /* the stream closes when the run ends */ };
  } catch (e) { $('#run-status').textContent = 'failed to start: ' + e.message; }
});
$('#stop').addEventListener('click', async () => { if (current) await api(`/api/run/${current.id}/stop`, { method: 'POST', body: '{}' }); });
$('#clear-log').addEventListener('click', () => { $('#log').innerHTML = ''; });

// ---------------------------------------------------------------- env
async function loadEnv(refresh) {
  $('#env-summary').textContent = 'probing…';
  try {
    const e = await api('/api/env' + (refresh ? '?refresh=1' : ''));
    $('#env-summary').textContent = e.summary + `\n\n(cached: ${e.manifest.cached}, probe ${e.manifest.probeSeconds}s, generated ${e.manifest.generatedAt})`;
    $('#env-json').textContent = pretty(e.manifest);
  } catch (err) { $('#env-summary').textContent = 'failed: ' + err.message; }
}
$('#env-refresh').addEventListener('click', () => loadEnv(true));
$('#find-btn').addEventListener('click', async () => {
  const r = await api('/api/find?name=' + encodeURIComponent($('#find-name').value));
  $('#find-result').classList.remove('hidden');
  $('#find-result').textContent = pretty(r);
});

// ---------------------------------------------------------------- models
async function loadModels(probe) {
  $('#models-out').textContent = 'discovering… (listing models from every source; a probe takes longer)';
  try {
    const m = await api('/api/models' + (probe ? '?probe=1' : ''));
    const lines = ['SOURCES'];
    for (const s of m.sources) lines.push(`  - ${s.id} [${s.kind}] ${s.base_url || ''} ${s.api_key ? '(key set)' : ''} ${s.note || ''}${s.models.length ? ` - ${s.models.length} model(s)` : ''}${s.message && !s.models.length ? ' - ' + s.message : ''}`);
    lines.push('', 'RANKING (coder role)');
    for (const r of m.selection.ranked.slice(0, 20)) lines.push(`  ${r.score.toFixed(3)}  ${r.model}  ctx=${r.context || '?'} tools=${r.nativeTools}  ` + Object.entries(r.criteria).map(([k, v]) => `${k}=${v.toFixed(2)}`).join(' '));
    lines.push('', 'SELECTION');
    for (const l of m.selection.explanation) lines.push('  ' + l);
    lines.push('', 'ROLES ' + pretty(m.selection.roles), 'FALLBACKS ' + pretty(m.selection.fallbacks));
    $('#models-out').textContent = lines.join('\n');
  } catch (err) { $('#models-out').textContent = 'failed: ' + err.message; }
}
$('#models-refresh').addEventListener('click', () => loadModels(false));
$('#models-probe').addEventListener('click', () => loadModels(true));

// ---------------------------------------------------------------- runs
let runDetail = null, runSub = 'report';
async function loadRuns() {
  const r = await api('/api/runs');
  const ul = $('#runs-list');
  ul.innerHTML = '';
  for (const run of r.runs) {
    const li = document.createElement('li');
    li.innerHTML = `<span class="badge ${esc(run.status)}">${esc(run.status)}</span> ${esc(run.goal.slice(0, 60))}<div class="muted">${esc(run.id)}</div>`;
    li.addEventListener('click', async () => { runDetail = await api(`/api/runs/${run.id}/report`); renderRunDetail(); });
    ul.appendChild(li);
  }
}
function renderRunDetail() {
  if (!runDetail) return;
  const views = { report: runDetail.report || '(no report)', plan: runDetail.plan || '(no plan)', state: pretty(runDetail.state), transcript: (runDetail.transcript || []).map((e) => `${e.type}\t${e.message || ''}`).join('\n') };
  $('#run-detail').textContent = views[runSub];
}
document.querySelectorAll('.subtabs button').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('.subtabs button').forEach((x) => x.classList.toggle('active', x === b));
  runSub = b.dataset.sub; renderRunDetail();
}));
$('#runs-refresh').addEventListener('click', loadRuns);

// ---------------------------------------------------------------- lessons
async function loadLessons() {
  const l = await api('/api/lessons');
  const tbody = $('#lessons-table tbody');
  tbody.innerHTML = '';
  for (const lesson of l.lessons) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(lesson.id)}</td><td>${esc(lesson.kind)}</td><td>${esc(lesson.decision || lesson.fix)}</td><td><code>${esc(JSON.stringify(lesson.situation)).slice(0, 160)}</code></td><td>${lesson.confidence.toFixed(2)}</td><td><button class="small">forget</button></td>`;
    tr.querySelector('button').addEventListener('click', async () => { await api(`/api/lessons/${lesson.id}`, { method: 'DELETE' }); loadLessons(); });
    tbody.appendChild(tr);
  }
  $('#lessons-proposals').textContent = l.proposals.length ? l.proposals.map((p) => `${p.rule}  ->  permissions.${p.list}   (from ${p.lessonId}, used ${p.uses}x)`).join('\n') : '(none yet - answer "always" to a permission question to get one)';
}
$('#lessons-refresh').addEventListener('click', loadLessons);

// ---------------------------------------------------------------- graph
async function loadGraph(rebuild) {
  const g = await api('/api/graph' + (rebuild ? '?rebuild=1' : ''));
  $('#graph-meta').textContent = `${g.nodes} nodes, ${g.edges} edges · ${g.path}`;
  $('#graph-summary').textContent = g.summary || '(nothing recorded yet - run something first)';
  $('#graph-yaml').textContent = g.yaml || '(no file yet)';
}
$('#graph-refresh').addEventListener('click', () => loadGraph(false));
$('#graph-rebuild').addEventListener('click', () => loadGraph(true));

// ---------------------------------------------------------------- files
async function loadFiles() {
  const f = await api('/api/files');
  const ul = $('#files-list');
  ul.innerHTML = '';
  for (const file of f.files) {
    const li = document.createElement('li');
    li.innerHTML = `${esc(file.path)} <span class="muted">${file.size} B</span>`;
    li.addEventListener('click', async () => {
      const view = $('#file-view');
      if (/\.(png|jpg|jpeg|gif|svg|webp)$/i.test(file.path)) { view.innerHTML = `<img class="preview" src="/files/${encodeURI(file.path)}">`; return; }
      const r = await api('/api/file?path=' + encodeURIComponent(file.path));
      view.textContent = r.binary ? `(binary, ${r.size} bytes)` : r.text + (r.truncated ? '\n…[truncated]' : '');
    });
    ul.appendChild(li);
  }
}
$('#files-refresh').addEventListener('click', loadFiles);

// ---------------------------------------------------------------- settings
async function loadSettings() {
  const s = await api('/api/settings');
  $('#s-baseUrl').value = s.baseUrl || '';
  $('#s-model').value = s.model || 'auto';
  $('#s-apiKeyEnv').value = s.apiKeyEnv || '';
  $('#s-permissionMode').value = s.permissionMode || 'default';
  $('#s-sandboxMode').value = (s.sandbox && s.sandbox.mode) || '';
  $('#s-toolCalling').value = s.toolCalling || 'auto';
  $('#s-localOnly').checked = !!(s.privacy && s.privacy.localOnly);
  $('#s-approvePlans').checked = !!s.approvePlans;
  $('#s-webSearch').checked = s.webSearch !== false;
  $('#settings-json').textContent = pretty(s);
}
$('#settings-save').addEventListener('click', async () => {
  const body = { baseUrl: $('#s-baseUrl').value.trim(), model: $('#s-model').value.trim() || 'auto', apiKeyEnv: $('#s-apiKeyEnv').value.trim(),
    permissionMode: $('#s-permissionMode').value, sandboxMode: $('#s-sandboxMode').value, toolCalling: $('#s-toolCalling').value,
    localOnly: $('#s-localOnly').checked, approvePlans: $('#s-approvePlans').checked, webSearch: $('#s-webSearch').checked };
  if ($('#s-apiKey').value) body.apiKey = $('#s-apiKey').value;
  try { await api('/api/settings', { method: 'POST', body: JSON.stringify(body) }); $('#settings-msg').textContent = 'saved'; $('#s-apiKey').value = ''; loadSettings(); loadStatus(); }
  catch (e) { $('#settings-msg').textContent = 'failed: ' + e.message; }
});
