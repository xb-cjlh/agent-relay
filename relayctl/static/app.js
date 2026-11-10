'use strict';
const $ = id => document.getElementById(id);
const icons = {
  plus:'<path d="M12 5v14M5 12h14"/>', search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  monitor:'<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  folder:'<path d="M3 8V5h6l3 3h9v12H3Z"/>', logout:'<path d="M9 4H4v16h5m6-13 5 5-5 5m-5-5h10"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>', menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
  up:'<path d="M12 19V5m-5 5 5-5 5 5"/>', down:'<path d="M12 5v14m-5-5 5 5 5-5"/>',
  stop:'<rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor" stroke="none"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
  key:'<circle cx="8" cy="8" r="4"/><path d="m11 11 9 9m-4-4 3-3m-6 0 3-3"/>',
  agent:'<rect x="4" y="6" width="16" height="14" rx="4"/><path d="M12 3v3M8 11v2m8-2v2m-8 4h8"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.agent}</svg>`;
document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const kindName = kind => kind === 'claude' ? 'Claude Code' : 'Codex';
const kindBadge = kind => `<span class="agent-badge agent-${kind === 'claude' ? 'claude' : 'codex'}" title="${kindName(kind)}" aria-label="${kindName(kind)}">${kind === 'claude' ? 'Claude' : 'Codex'}</span>`;
const date = t => new Date(t * 1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
const size = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MiB' : n >= 1024 ? (n / 1024).toFixed(1) + ' KiB' : n + ' B';
const terminal = new Set(['succeeded','failed','cancelled']);
let signedIn = false;
let currentUser = null;
const imageDrafts = new Map();
const imageUrls = new Map();
let uploadingImages = 0;
const imageUploads = new Set();
let state = {devices:[], agents:[], jobs:[], artifacts:[]};
let selected = sessionStorage.getItem('relay-selected') || '';
let deviceFilter = sessionStorage.getItem('relay-device-filter') || '';
let messages = new Map();
let activeJobs = [];
let approvals = [];
let refreshing = false, generation = 0, conversationRequest = 0;
let firstMessages = true, sending = false, connected = true, loadingOlder = false;
let toastTimer;
let sessionRows = [], sessionCursor = null, sessionThrough = null, sessionHasMore = false;
let sessionLoading = true, sessionMoreLoading = false, sessionError = false, sessionRevision = 0;
let refreshRequest = 0, refreshPromise = null, searchTimer;
const drafts = new Map();

function showScreen(id) {
  for (const screen of ['session-loading','login','workspace']) $(screen).hidden = screen !== id;
}

function loadingError() {
  $('session-loading-message').textContent = '暂时无法加载，请重试';
  $('retry-session').hidden = false;
}

function toast(text) {
  $('toast').textContent = text; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5000);
}

async function api(path, body, raw=false) {
  const response = await fetch('/api/' + path, {method:body === undefined ? 'GET' : 'POST',
    headers:{'Content-Type':'application/json','X-Relay-Request':'1'}, credentials:'same-origin',
    ...(body === undefined ? {} : {body:JSON.stringify(body)}), redirect:'error', signal:AbortSignal.timeout(15000)});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && signedIn) logout(true);
    throw Object.assign(new Error(data.error || `请求失败 (${response.status})`),{status:response.status});
  }
  return raw ? response : response.json();
}

async function logout(localOnly=false) {
  if (!localOnly) await fetch('/api/auth/logout',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Request':'1'},body:'{}'}).catch(()=>{});
  signedIn = false; currentUser = null; generation++; conversationRequest++;
  for (const upload of imageUploads) upload.abort();
  for (const key of Object.keys(sessionStorage)) if (key.startsWith('relay-')) sessionStorage.removeItem(key);
  sessionRevision++; refreshRequest++; refreshing = false; sessionRows = []; sessionThrough = sessionCursor = null; sessionHasMore = false; sessionLoading = true; sessionMoreLoading = false; sessionError = false;
  selected = ''; deviceFilter = ''; drafts.clear(); imageDrafts.clear(); imageUrls.clear(); messages.clear(); activeJobs=[]; approvals=[]; renderApprovals(); state={devices:[],agents:[],jobs:[],artifacts:[]};
  showScreen('login');
  $('new-key-secret').textContent = ''; $('new-key').hidden = true;
  $('key-list').replaceChildren(); $('messages').replaceChildren(); $('attachment-list').replaceChildren();
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  toggleSidebar(false);
}

function setConnected(value) {
  connected = value;
  $('connection-dot').className = 'dot ' + (value ? 'online' : 'offline');
  $('connection-label').textContent = value ? '已连接' : '连接中断，正在重试';
}

function sessionParameters() {
  const params = new URLSearchParams();
  if (deviceFilter) params.set('device',deviceFilter);
  const query = $('search').value.trim();
  if (query) params.set('q',query);
  return params;
}

function refresh() {
  if (!signedIn || sessionMoreLoading) return Promise.resolve();
  if (refreshing) return refreshPromise;
  refreshing = true;
  const current = generation, revision = sessionRevision, request = ++refreshRequest;
  const params = sessionParameters(); params.set('view','web');
  if (sessionThrough) params.set('through',sessionThrough);
  const remembered = (deviceFilter && sessionStorage.getItem('relay-selected-device:' + deviceFilter)) || selected;
  if (remembered) params.set('selected',remembered);
  refreshPromise = (async () => {
    try {
      const next = await api('state?' + params);
      if (current !== generation || revision !== sessionRevision || request !== refreshRequest) return;
      sessionRows = next.agents;
      sessionCursor = next.next_cursor; sessionHasMore = !!next.has_more;
      sessionLoading = false; sessionError = false;
      state = next;
      if (next.selected_agent && !state.agents.some(agent => agent.id === next.selected_agent.id)) state.agents = [...state.agents,next.selected_agent];
      setConnected(true); showScreen('workspace');
      if (deviceFilter && !state.devices.some(device => device.id === deviceFilter)) {
        deviceFilter = ''; sessionStorage.removeItem('relay-device-filter'); resetSessions(); return;
      }
      renderDeviceFilter(); renderSessions(); renderDevices(); renderFiles(); updateOptions();
      if (!scopedAgents().some(agent => agent.id === selected)) selectAgent(preferredAgent(),false,false);
      renderHeader();
      if (selected && !loadingOlder) await loadConversation();
    } catch (error) {
      if (current !== generation || revision !== sessionRevision || request !== refreshRequest) return;
      sessionLoading = false; sessionError = true; renderSessions(); setConnected(false);
      if (!$('login').hidden) $('auth-error').textContent = error.message;
      if (!$('session-loading').hidden) loadingError();
      renderHeader();
    } finally { if (request === refreshRequest) refreshing = false; }
  })();
  return refreshPromise;
}

function resetSessions() {
  sessionRevision++; refreshRequest++; refreshing = false;
  sessionRows = []; sessionCursor = sessionThrough = null; sessionHasMore = false;
  sessionLoading = true; sessionMoreLoading = false; sessionError = false;
  $('session-list').scrollTop = 0; renderSessions();
  refresh();
}

async function loadMoreSessions() {
  if (!signedIn || sessionLoading || sessionMoreLoading || (!sessionHasMore && !sessionError)) return;
  if (!sessionCursor) { sessionError = false; refresh(); return; }
  const current = generation, revision = sessionRevision;
  sessionMoreLoading = true; sessionError = false; renderSessionPager();
  try {
    if (refreshing) await refreshPromise;
    if (current !== generation || revision !== sessionRevision || !sessionCursor) return;
    const params = sessionParameters(); params.set('before',sessionCursor);
    const reply = await api('agents?' + params);
    if (current !== generation || revision !== sessionRevision) return;
    sessionRows = [...new Map([...sessionRows,...reply.agents].map(agent => [agent.id,agent])).values()].sort(newestFirst);
    const active = state.agents.find(agent => agent.id === selected);
    state.agents = [...sessionRows];
    if (active && !state.agents.some(agent => agent.id === active.id)) state.agents.push(active);
    sessionCursor = reply.next_cursor; sessionHasMore = !!reply.has_more;
    const last = sessionRows[sessionRows.length - 1];
    if (last) sessionThrough = activityTime(last) + '|' + last.id;
    renderSessions();
  } catch (error) {
    if (current === generation && revision === sessionRevision) { sessionError = true; toast(error.message); }
  } finally {
    if (current === generation && revision === sessionRevision) { sessionMoreLoading = false; renderSessionPager(); }
  }
}

function renderSessionPager() {
  const button = $('load-more-sessions');
  button.hidden = sessionLoading || (!sessionHasMore && !sessionError);
  button.disabled = sessionMoreLoading;
  button.textContent = sessionMoreLoading ? '正在加载…' : sessionError ? '加载失败，点击重试' : '加载更多';
}

function scopedAgents() { return state.agents.filter(agent => !deviceFilter || agent.device_id === deviceFilter); }

const activityTime = agent => Number(agent.last_activity || agent.created_at || 0);
const newestFirst = (a,b) => activityTime(b) - activityTime(a) || a.id.localeCompare(b.id);

function preferredAgent() {
  const agents = scopedAgents();
  const remembered = (deviceFilter && sessionStorage.getItem('relay-selected-device:' + deviceFilter)) || selected;
  return agents.find(agent => agent.id === remembered)?.id || agents.slice().sort(newestFirst)[0]?.id || '';
}

function renderDeviceFilter() {
  const select = $('device-filter');
  const html = `<option value="">全部设备 · ${state.devices.reduce((sum,device) => sum + Number(device.session_count || 0),0)} 个会话</option>` + state.devices.map(device =>
    `<option value="${esc(device.id)}">${esc(device.name)} · ${device.session_count || 0} 个会话 · ${device.online ? '在线' : '离线'}</option>`).join('');
  if (select.dataset.rendered !== html) { select.innerHTML = html; select.dataset.rendered = html; }
  select.value = deviceFilter;
}

function setDeviceFilter(id) {
  deviceFilter = state.devices.some(device => device.id === id) ? id : '';
  sessionStorage.setItem('relay-device-filter',deviceFilter);
  clearTimeout(searchTimer); $('search').value = '';
  renderDeviceFilter();
  if (deviceFilter && state.agents.find(agent => agent.id === selected)?.device_id !== deviceFilter) selectAgent('',false,false);
  resetSessions();
}

function sessionName(agent) {
  const project = String(agent.cwd || '').replace(/\/$/,'').split('/').pop();
  if (agent.origin === 'native') return project || agent.name;
  const generated = /^(?:codex|claude) · (.+) · [a-z0-9]+$/.exec(agent.name || '');
  return generated ? generated[1] : agent.name;
}

function calendarDay(value) { return Date.UTC(value.getFullYear(),value.getMonth(),value.getDate()); }
function activityGroup(timestamp,now) {
  const days = (calendarDay(now) - calendarDay(new Date(timestamp * 1000))) / 86400000;
  return days <= 0 ? '今天' : days === 1 ? '昨天' : days < 7 ? '近 7 天' : days < 30 ? '近 30 天' : '更早';
}

function sessionItem(agent,device,now) {
  const timestamp = activityTime(agent), when = new Date(timestamp * 1000);
  const recent = calendarDay(now) - calendarDay(when) <= 86400000;
  const time = when.toLocaleString('zh-CN',recent ? {hour:'2-digit',minute:'2-digit',hour12:false} : {month:'2-digit',day:'2-digit',...(when.getFullYear() === now.getFullYear() ? {} : {year:'numeric'})});
  const fullName = [kindName(agent.kind),agent.name,device.name,agent.cwd,agent.session_id].filter(Boolean).join(' · ');
  return `<button class="session-item${selected === agent.id ? ' selected' : ''}" data-agent="${esc(agent.id)}" aria-current="${selected === agent.id ? 'true' : 'false'}" title="${esc(fullName)}"><span class="session-item-top">${kindBadge(agent.kind)}<span class="session-name">${esc(sessionName(agent))}</span>${agent.busy ? '<span class="dot busy" title="正在回复"></span>' : ''}</span><span class="session-item-meta">${!deviceFilter ? `<span class="session-device" title="${esc(device.name)} · ${device.online ? '在线' : '离线'}"><span class="dot ${device.online ? 'online' : 'offline'}"></span><span>${esc(device.name)}</span></span>` : ''}<span class="session-preview">${esc(agent.preview || agent.cwd)}</span><time datetime="${when.toISOString()}" title="${esc(when.toLocaleString('zh-CN'))}">${esc(time)}</time></span></button>`;
}

function renderSessions() {
  const query = $('search').value.trim().toLowerCase(), now = new Date();
  const devices = new Map(state.devices.map(device => [device.id,device]));
  const groups = new Map();
  for (const agent of [...sessionRows].sort(newestFirst)) {
    const device = devices.get(agent.device_id);
    if (!device) continue;
    const group = activityGroup(activityTime(agent),now);
    if (!groups.has(group)) groups.set(group,[]);
    groups.get(group).push(sessionItem(agent,device,now));
  }
  const sections = [...groups].map(([label,items]) => `<section class="session-group" aria-label="${label}"><h2 class="session-group-heading">${label}</h2>${items.join('')}</section>`).join('');
  const list = $('session-items');
  const html = sections || (sessionLoading ? '<p class="sidebar-empty" role="status">正在加载会话…</p>' : sessionError ? '' : `<p class="sidebar-empty">${query ? '没有匹配的会话。' : deviceFilter ? '这台设备还没有同步到会话，可先在设备上使用 Codex / Claude Code，或新建会话。' : '还没有会话。连接设备后，会自动发现当前用户各目录下的已有会话。'}</p>`);
  if (list.dataset.rendered !== html) { list.innerHTML = html; list.dataset.rendered = html; }
  renderSessionPager();
}

function selectAgent(id,closeSidebar=true,load=true) {
  if (selected) { drafts.set(selected, $('prompt').value); sessionStorage.setItem('relay-draft:' + selected, $('prompt').value); }
  selected = id; conversationRequest++; messages = new Map(); activeJobs = []; approvals = []; renderApprovals(); firstMessages = true;
  $('messages').replaceChildren(); $('new-messages').hidden = true; $('load-older').hidden = true;
  $('prompt').value = drafts.get(id) ?? sessionStorage.getItem('relay-draft:' + id) ?? '';
  sessionStorage.setItem('relay-selected', id);
  const agent = state.agents.find(agent => agent.id === id);
  if (agent) sessionStorage.setItem('relay-selected-device:' + agent.device_id,id);
  $('thread').hidden = !id; $('empty-conversation').hidden = !!id;
  renderSessions(); renderHeader(); if (closeSidebar) toggleSidebar(false);
  if (id && load) loadConversation().catch(error => toast(error.message));
}

function renderHeader() {
  const agent = state.agents.find(a => a.id === selected);
  const device = state.devices.find(d => d.id === agent?.device_id);
  const filteredDevice = state.devices.find(d => d.id === deviceFilter);
  $('empty-description').textContent = filteredDevice ? `${filteredDevice.name} 还没有同步到会话。可在这台设备上新建会话，或切换其他设备。` : '选择已有会话，或在任意设备上新建会话。';
  $('thread').hidden = !agent; $('empty-conversation').hidden = !!agent;
  $('conversation-title').textContent = agent?.name || '会话';
  $('conversation-kind').textContent = agent ? kindName(agent.kind) : '';
  $('conversation-kind').className = `agent-badge agent-${agent?.kind === 'claude' ? 'claude' : 'codex'}`;
  $('conversation-kind').hidden = !agent;
  $('conversation-meta').textContent = agent ? `${agent.device_name} · ${agent.cwd}` : '';
  $('conversation-meta').title = agent?.cwd || '';
  const running = activeJobs.some(j => ['running','cancelling'].includes(j.status));
  const busy = running || agent?.busy;
  const readOnly = agent && agent.controllable !== undefined && !agent.controllable;
  const upgradeRequired = agent && !device?.capabilities.unlimited_tasks;
  $('conversation-status').innerHTML = agent ? `<span class="dot ${device?.online && connected ? busy ? 'busy' : 'online' : 'offline'}"></span>${!connected ? '连接中断' : !device?.online ? '设备离线' : approvals.length ? '等待审批' : busy ? '正在回复' : '在线'}` : '';
  const permissionCapable = device?.capabilities[agent?.kind + '_permissions'];
  const mode = $('permission-mode'), capable = permissionCapable && device?.capabilities.writable;
  mode.hidden = !agent;
  mode.options[0].textContent = device?.capabilities.writable ? '设备默认 · 可写' : '设备默认 · 只读';
  mode.options[1].disabled = mode.options[2].disabled = !capable;
  const modeKey = 'relay-permission-mode:' + selected;
  let savedMode = sessionStorage.getItem(modeKey) || 'full';
  // "device" was the old implicit default. Treat that legacy value as
  // unset once, while preserving a device choice made after this change.
  if (savedMode === 'device' && sessionStorage.getItem(modeKey + ':explicit') !== '1') savedMode = 'full';
  mode.value = capable && ['device','ask','full'].includes(savedMode) ? savedMode : 'device';
  mode.title = !permissionCapable ? '请升级目标设备 Relay 至 0.6.0 并重启后台进程' : !device?.capabilities.writable ? '请先在目标设备启用 --allow-agent-writes' : '执行权限从下一条消息生效';
  $('permission-note').hidden = mode.hidden || mode.value === 'device';
  $('permission-note').textContent = mode.value === 'full' ? '完全访问：允许以设备用户权限访问文件和网络，不再逐项审批。仅对随后发送的消息生效。' : (agent?.kind === 'claude' ? '按需审批：遵循 Claude 本机权限规则，需要确认的工具调用会显示在此处。' : '按需审批：项目内操作可直接执行，需要额外权限时在此确认。');
  $('prompt').disabled = !agent || !!readOnly;
  $('send-button').disabled = sending || uploadingImages > 0 || !agent || readOnly || upgradeRequired || (!$('prompt').value.trim() && !(imageDrafts.get(selected)?.length));
  $('attach-image').disabled = sending || uploadingImages > 0 || !agent || readOnly || !device?.capabilities.image_input;
  $('attach-image').title = device?.capabilities.image_input ? '添加图片' : '请升级目标设备 Relay 至 0.5.0 并重启，以发送图片';
  renderAttachments();
  $('stop-generation').hidden = !activeJobs.some(j => j.status === 'running');
  const queued = activeJobs.filter(j => j.status === 'queued').length;
  const reason = {outside_roots:'设备端仍是旧版本，请更新 CLI 并重启后台进程。',cwd_missing:'工作目录已不存在。',cli_missing:'设备上未检测到对应的 Agent CLI。'};
  $('queue-hint').textContent = !agent ? '' : readOnly ? '仅可查看 · ' + (reason[agent.control_reason] || '当前会话无法远程操作。') : upgradeRequired ? '请将目标设备 Relay 升级至 0.6.1 并重启后台进程，以取消旧版执行时限。' : !connected ? '连接恢复后会自动同步，输入的内容会保留。' : !device?.online ? '设备离线，发送的消息将在上线后执行。' : queued ? `${queued} 条消息等待执行，将按顺序继续对话。` : agent.external_busy ? '当前会话正在另一端回复，可以继续追加消息。' : '';
}

async function loadConversation(before) {
  if (!selected || !signedIn) return;
  const id = selected, current = generation, request = ++conversationRequest;
  const reply = await api(`agents/${encodeURIComponent(id)}/messages${before ? '?before=' + encodeURIComponent(before) : ''}`);
  if (selected !== id || generation !== current || request !== conversationRequest) return;
  const scroller = $('message-scroll');
  const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100;
  const oldHeight = scroller.scrollHeight, oldTop = scroller.scrollTop;
  let changed = false;
  for (const id of reply.suppressed_message_ids || []) messages.delete(id);
  for (const message of reply.messages) {
    if (JSON.stringify(messages.get(message.id)) !== JSON.stringify(message)) changed = true;
    messages.set(message.id, message);
  }
  activeJobs = reply.active_jobs;
  approvals = reply.approvals || [];
  if (before || firstMessages) $('load-older').hidden = !reply.has_more;
  renderMessages(); renderApprovals(); renderHeader();
  if (before) scroller.scrollTop = oldTop + scroller.scrollHeight - oldHeight;
  else if (firstMessages || atBottom) scrollBottom();
  else if (changed) $('new-messages').hidden = false;
  firstMessages = false;
}

function renderApprovals() {
  const container = $('approval-list');
  const signature = JSON.stringify(approvals);
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  container.hidden = !approvals.length;
  container.innerHTML = approvals.map(approval => {
    const details = approval.details;
    const title = approval.method === 'claude/tool/requestApproval' ? `允许 Claude 使用 ${details.tool_name || '工具'}？` : {'item/commandExecution/requestApproval':'允许执行此操作？','item/fileChange/requestApproval':'允许修改这些文件？','item/permissions/requestApproval':'允许本轮使用这些权限？'}[approval.method] || '需要你的审批';
    const preview = details.command || details.item?.command || details.input?.command || '';
    const extra = {};
    for (const key of ['grantRoot','networkApprovalContext','additionalPermissions','permissions']) if (details[key]) extra[key] = details[key];
    if (details.item?.changes) extra.changes = details.item.changes;
    if (details.input) extra.input = details.input;
    if (details.blocked_path) extra.blocked_path = details.blocked_path;
    const options = details.availableDecisions;
    const canAccept = !options?.length || options.includes('accept');
    return `<section class="approval-card"><strong>${esc(title)}</strong>${details.reason ? `<p>${esc(details.reason)}</p>` : ''}${details.cwd ? `<p class="approval-cwd">${esc(details.cwd)}</p>` : ''}${preview ? `<pre>${esc(preview)}</pre>` : ''}${Object.keys(extra).length ? `<details${preview ? '' : ' open'}><summary>权限与变更详情</summary><pre>${esc(JSON.stringify(extra,null,2))}</pre></details>` : ''}<div class="approval-actions"><button type="button" class="button secondary" data-approval="${esc(approval.id)}" data-job="${esc(approval.job_id)}" data-decision="decline">拒绝</button>${canAccept ? `<button type="button" class="button primary" data-approval="${esc(approval.id)}" data-job="${esc(approval.job_id)}" data-decision="accept">允许此次操作</button>` : '<span>此请求不支持单次允许，请在本机处理。</span>'}</div></section>`;
  }).join('');
}

$('permission-mode').addEventListener('change',() => {
  if (selected) {
    const key = 'relay-permission-mode:' + selected;
    sessionStorage.setItem(key,$('permission-mode').value);
    sessionStorage.setItem(key + ':explicit','1');
  }
  renderHeader();
});
$('approval-list').addEventListener('click',async event => {
  const button = event.target.closest('[data-approval]');
  if (!button) return;
  const card = button.closest('.approval-card');
  card.querySelectorAll('button').forEach(el => { el.disabled = true; });
  try {
    await api(`jobs/${encodeURIComponent(button.dataset.job)}/approvals/${encodeURIComponent(button.dataset.approval)}`,{decision:button.dataset.decision});
    await loadConversation();
  } catch(error) {
    toast(error.message); card.querySelectorAll('button').forEach(el => { el.disabled = false; });
  }
});

// HTML stays literal; model output cannot introduce page elements or scripts.
const markdown = window.markdownit({html:false, breaks:true, linkify:true}).disable('image');
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet('target','_blank');
  tokens[index].attrSet('rel','noopener noreferrer');
  return renderer.renderToken(tokens,index,options);
};
markdown.renderer.rules.fence = (tokens, index) => {
  const token = tokens[index];
  const language = token.info.trim().split(/\s+/)[0];
  return `<div class="code-block"><div class="code-heading"><span>${esc(language)}</span><button type="button" data-copy-code>复制</button></div><pre><code>${esc(token.content)}</code></pre></div>`;
};
markdown.renderer.rules.table_open = () => '<div class="markdown-table-wrap" tabindex="0" role="region" aria-label="表格"><table class="markdown-table">';
markdown.renderer.rules.table_close = () => '</table></div>\n';
// Table alignment uses classes so it also works under the page's strict CSP.
for (const tag of ['th_open','td_open']) markdown.renderer.rules[tag] = (tokens, index, options, env, renderer) => {
  const token = tokens[index], alignment = /^text-align:(left|center|right)$/.exec(token.attrGet('style') || '');
  if (alignment) {
    token.attrs = token.attrs.filter(([name]) => name !== 'style');
    token.attrSet('class','markdown-align-' + alignment[1]);
  }
  return renderer.renderToken(tokens,index,options);
};
function richText(text) { return markdown.render(String(text)); }

function renderMessages() {
  const container = $('messages'), agent = state.agents.find(a => a.id === selected);
  const ordered = [...messages.values()].sort((a,b) => a.created_at - b.created_at || a.id.localeCompare(b.id));
  const visible = ordered.filter(m => !(m.role === 'assistant' && !m.content && m.status === 'queued'));
  const existing = new Map([...container.children].map(node => [node.dataset.id,node]));
  let cursor = container.firstElementChild;
  for (const message of visible) {
    let node = existing.get(message.id);
    if (!node) { node = document.createElement('article'); node.dataset.id = message.id; }
    const signature = JSON.stringify([message,agent?.kind]);
    if (node.dataset.signature !== signature) {
      node.className = 'message ' + message.role;
      const writerConflict = message.role === 'assistant' && message.status === 'failed' && agent?.kind === 'codex' && [message.content,message.diagnostics].some(value=>value?.includes('already has an active writer'));
      const error = writerConflict && message.error?.includes('exit status') ? '更新并重启目标设备的 Relay 后，可通过 Codex 共享服务继续。若设备未提供共享服务，请先在本机退出这个会话再发送。' : message.error;
      const parts = message.role === 'assistant' && message.segments?.length && !writerConflict
        ? message.segments.map(segment => ({content: message.content.slice(segment.start,segment.end),created_at: segment.created_at})) : [message];
      const body = parts.map(part => {
        let text;
        if (writerConflict) text = `<p class="session-conflict">本次指令未执行：原会话仍由本机 Codex 持有。</p>${message.content ? `<details class="execution-details"><summary>技术详情</summary><pre>${esc(message.content)}</pre></details>` : ''}`;
        else if (part.content) text = message.role === 'user' ? esc(part.content) : richText(part.content);
        else if (['running','cancelling'].includes(message.status)) text = `<span class="typing">${message.status === 'cancelling' ? '正在停止…' : '正在回复…'}</span>`;
        else text = message.status === 'failed' ? '' : message.status === 'cancelled' ? '已停止。' : '本轮已结束，没有文本输出。';
        if (message.role === 'user' && message.images?.length) text += `<div class="message-images">${message.images.map(img => `<a href="/api/images/${encodeURIComponent(img.id)}" data-view-image="${esc(img.id)}" target="_blank" rel="noopener noreferrer"><img data-image-id="${esc(img.id)}" alt="${esc(img.name)}" loading="lazy" crossorigin="anonymous" referrerpolicy="no-referrer"></a>`).join('')}</div>`;
        if (message.role === 'user') return `<div class="message-body">${text}</div>`;
        return `<section class="message-part"><div class="message-byline">${icon('agent')}<span>${kindName(agent?.kind)}</span><time>${date(part.created_at)}</time></div><div class="message-body">${text}</div></section>`;
      }).join('');
      const queued = message.role === 'user' && message.status === 'queued';
      node.innerHTML = `${body}${queued ? `<div class="message-state">等待执行 <button data-cancel="${esc(message.job_id)}">取消</button></div>` : ''}${message.role === 'assistant' && message.content && ['running','cancelling'].includes(message.status) ? '<div class="message-state"><span class="dot busy"></span>正在回复</div>' : ''}${message.role === 'assistant' && error ? `<div class="message-error">${esc(error)}</div>` : ''}`;
      if (message.role === 'assistant' && message.diagnostics) node.innerHTML += `<details class="execution-details"><summary>执行日志</summary><pre>${esc(message.diagnostics)}</pre></details>`;
      node.dataset.signature = signature;
    }
    if (node !== cursor) container.insertBefore(node,cursor);
    cursor = node.nextElementSibling;
    existing.delete(message.id);
  }
  for (const node of existing.values()) node.remove();
  loadImages(container);
  $('thread-empty').hidden = visible.length > 0;
}

function scrollBottom() {
  $('message-scroll').scrollTop = $('message-scroll').scrollHeight;
  $('new-messages').hidden = true;
}

function options(id, values) {
  const select = $(id), old = select.value;
  const html = '<option value="">选择设备</option>' + values.map(d => `<option value="${esc(d.id)}">${esc(d.name)}${d.online ? '' : '（离线）'}</option>`).join('');
  if (select.dataset.options === html) return;
  select.innerHTML = html; select.dataset.options = html;
  if (values.some(d => d.id === old)) select.value = old;
  else if (values.length) select.value = values[0].id;
}
function updateOptions() { options('agent-device',state.devices); options('file-device',state.devices); }

function renderDevices() {
  $('device-count').textContent = state.devices.length;
  $('device-list').innerHTML = state.devices.map(d => `<div class="device-entry"><div class="device-entry-heading"><span class="dot ${d.online ? 'online' : 'offline'}"></span>${esc(d.name)}<small>${d.online ? '在线' : '离线'} · ${d.capabilities.discovery_scope === 'user' ? '整机发现' : '目录发现'}</small></div><p class="device-roots">${d.capabilities.access_scope === 'user' ? '可访问当前系统用户可用的所有目录' : '请升级设备端 CLI'}</p><div class="device-entry-actions"><span>${d.session_count || 0} 个会话</span><button class="button secondary" data-view-device="${esc(d.id)}">查看会话</button></div></div>`).join('') || '<p class="sidebar-empty">还没有连接设备。</p>';
}

function renderFiles() {
  const pending = state.jobs.filter(j => j.kind === 'file_read' && j.status !== 'succeeded');
  $('file-list').innerHTML = pending.slice(0,5).map(j => `<div class="file-entry"><div><span class="file-entry-name">${esc(j.payload.path)}</span><small class="${j.status === 'failed' ? 'error' : ''}">${esc(j.error || ({queued:'等待设备上线或领取',running:'正在取回…',cancelling:'正在取消…',cancelled:'已取消'}[j.status]) || j.status)}</small></div>${!terminal.has(j.status) ? `<button class="button secondary" data-cancel="${esc(j.id)}">取消</button>` : ''}</div>`).join('') + state.artifacts.map(f => `<div class="file-entry"><div><span class="file-entry-name">${esc(f.name)}</span><small>${size(f.size)} · ${date(f.created_at)}</small></div><button class="button secondary" data-download="${esc(f.id)}">下载</button></div>`).join('');
}

async function downloadFile(id) {
  const file = state.artifacts.find(f => f.id === id);
  if (!file) throw new Error('文件尚未同步，请稍后重试');
  const response = await api('files/' + id, undefined, true), data = await response.arrayBuffer();
  if (!crypto.subtle) throw new Error('浏览器文件校验需要 HTTPS 或 localhost，也可使用 CLI 下载');
  const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(x => x.toString(16).padStart(2,'0')).join('');
  if (hash !== file.sha256) throw new Error('文件校验失败');
  const url = URL.createObjectURL(new Blob([data],{type:'application/octet-stream'}));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = file.name; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}

function toggleSidebar(open) { $('sidebar').classList.toggle('open',open); $('sidebar-backdrop').hidden = !open; }
function currentDevice() { return deviceFilter || state.agents.find(a => a.id === selected)?.device_id || state.devices[0]?.id || ''; }
function newSession() {
  if (!state.devices.length) { $('devices-dialog').showModal(); return; }
  updateOptions(); $('agent-device').value = currentDevice(); suggestDirectory(); $('agent-dialog').showModal();
}
function suggestDirectory() {
  const device = state.devices.find(d => d.id === $('agent-device').value);
  if (device) { $('agent-cwd').value = device.capabilities.home || device.roots[0] || ''; $('agent-kind').value = device.capabilities.codex ? 'codex' : 'claude'; }
}
async function submit(form, action) {
  const button = form.querySelector('button[type=submit]'); button.disabled = true;
  try { await action(); } catch(error) { toast(error.message); }
  finally { button.disabled = false; }
}

$('logout').addEventListener('click',() => logout());
$('search').addEventListener('input',() => {
  clearTimeout(searchTimer);
  // Invalidate pending responses immediately, before the debounce expires.
  sessionRevision++; refreshRequest++; refreshing = false;
  sessionRows = []; sessionCursor = sessionThrough = null; sessionHasMore = false;
  sessionLoading = true; sessionMoreLoading = false; sessionError = false;
  renderSessions();
  searchTimer = setTimeout(resetSessions,200);
});
$('load-more-sessions').addEventListener('click',loadMoreSessions);
$('session-list').addEventListener('scroll',() => {
  const pane = $('session-list');
  if (pane.scrollTop > 0 && pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120 && !sessionError) loadMoreSessions();
});
$('device-filter').addEventListener('change',e => setDeviceFilter(e.target.value));
$('new-agent').addEventListener('click',newSession); $('empty-new-agent').addEventListener('click',newSession);
$('devices-button').addEventListener('click',() => { toggleSidebar(false); $('devices-dialog').showModal(); });
$('files-button').addEventListener('click',() => { $('file-device').value = currentDevice(); toggleSidebar(false); $('files-dialog').showModal(); });
$('agent-device').addEventListener('change',suggestDirectory);
$('agent-form').addEventListener('submit',e => {
  e.preventDefault();
  submit(e.currentTarget,async () => {
    const cwd = $('agent-cwd').value.trim(), kind = $('agent-kind').value;
    const name = `${kind} · ${cwd.replace(/\/$/,'').split('/').pop().slice(0,35)} · ${Date.now().toString(36)}`;
    const agent = await api('agents',{device:$('agent-device').value,kind,name,cwd});
    const device = state.devices.find(d => d.id === agent.device_id);
    const created = {...agent,device_name:device.name,last_activity:agent.created_at};
    state.agents.push(created); sessionRows.unshift(created);
    $('agent-dialog').close();
    if (deviceFilter && deviceFilter !== agent.device_id) setDeviceFilter(agent.device_id);
    renderDeviceFilter(); selectAgent(agent.id); $('prompt').focus(); await refresh();
  });
});
$('prompt').addEventListener('input',() => {
  if (selected) { drafts.set(selected,$('prompt').value); sessionStorage.setItem('relay-draft:' + selected,$('prompt').value); }
  renderHeader();
});
$('prompt').addEventListener('keydown',e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault(); if (!$('send-button').disabled) $('send-form').requestSubmit();
  }
});
$('send-form').addEventListener('submit',async e => {
  e.preventDefault();
  const text = $('prompt').value.trim(), id = selected, images = [...(imageDrafts.get(selected)||[])];
  if ((!text && !images.length) || uploadingImages || !id || sending || $('prompt').disabled) return;
  sending = true; renderHeader();
  try {
    await api('jobs',{kind:'prompt',agent:id,prompt:text,images:images.map(img=>img.id),permission_mode:$('permission-mode').hidden ? 'device' : $('permission-mode').value});
    imageDrafts.set(id,(imageDrafts.get(id)||[]).filter(img=>!images.some(sent=>sent.id===img.id)));
    if (selected === id && $('prompt').value.trim() === text) { $('prompt').value = ''; drafts.delete(id); sessionStorage.removeItem('relay-draft:' + id); }
    if (selected === id) { await loadConversation(); scrollBottom(); if (!matchMedia('(pointer: coarse)').matches) $('prompt').focus(); }
    refresh();
  } catch(error) { toast(error.message); }
  finally { sending = false; renderHeader(); }
});
$('stop-generation').addEventListener('click',async () => {
  try { for (const job of activeJobs.filter(j => j.status === 'running')) await api('jobs/' + job.id + '/cancel',{}); await loadConversation(); }
  catch(error) { toast(error.message); }
});
$('load-older').addEventListener('click',async () => {
  const oldest = [...messages.values()].sort((a,b) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1))[0];
  const before = oldest.created_at + '|' + oldest.id;
  loadingOlder = true; $('load-older').disabled = true;
  try { await loadConversation(before); } catch(error) { toast(error.message); }
  finally { loadingOlder = false; $('load-older').disabled = false; }
});
$('new-messages').addEventListener('click',scrollBottom);
$('message-scroll').addEventListener('scroll',() => {
  const pane = $('message-scroll'); if (pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80) $('new-messages').hidden = true;
});
$('file-form').addEventListener('submit',e => {
  e.preventDefault();
  submit(e.currentTarget,async () => {
    await api('jobs',{kind:'file_read',device:$('file-device').value,path:$('file-path').value});
    $('file-path').value = ''; await refresh();
  });
});
$('open-sidebar').addEventListener('click',() => toggleSidebar(true));
$('close-sidebar').addEventListener('click',() => toggleSidebar(false));
$('sidebar-backdrop').addEventListener('click',() => toggleSidebar(false));
document.addEventListener('click',async e => {
  const target = e.target.closest('[data-view-image],[data-remove-image],[data-agent],[data-view-device],[data-close],[data-cancel],[data-download],[data-copy-code]');
  if (!target) return;
  try {
    if (target.dataset.viewImage) {
      e.preventDefault();
      const img=document.createElement('img'); img.dataset.imageId=target.dataset.viewImage;
      img.alt=target.querySelector('img').alt;img.crossOrigin='anonymous';img.referrerPolicy='no-referrer';
      $('image-title').textContent=img.alt;$('image-view').replaceChildren(img);
      $('image-dialog').showModal();loadImages($('image-view'));
    }
    if (target.dataset.removeImage) {imageDrafts.set(selected,(imageDrafts.get(selected)||[]).filter(img=>img.id!==target.dataset.removeImage));renderHeader();}
    if (target.dataset.agent) selectAgent(target.dataset.agent);
    if (target.dataset.viewDevice) {
      setDeviceFilter(target.dataset.viewDevice); $('devices-dialog').close();
      if (matchMedia('(max-width: 760px)').matches) toggleSidebar(true);
    }
    if (target.dataset.close) $(target.dataset.close).close();
    if (target.dataset.cancel) { await api('jobs/' + target.dataset.cancel + '/cancel',{}); await refresh(); }
    if (target.dataset.download) await downloadFile(target.dataset.download);
    if (target.hasAttribute('data-copy-code')) { await navigator.clipboard.writeText(target.closest('.code-block').querySelector('pre').textContent); toast('已复制'); }
  } catch(error) { toast(error.message); }
});
document.addEventListener('keydown',e => { if (e.key === 'Escape') toggleSidebar(false); });
document.addEventListener('visibilitychange',() => { if (!document.hidden) refresh(); });
$('connect-code').textContent = `请阅读 ${location.origin}/install-agent.md，帮我安装并连接 Relay CLI 和 agent-bridge skill，设置后台自启并同步整机会话，令牌优先复用本机已有配置，没有则提示我在终端隐藏输入。`;
if (selected) $('prompt').value = sessionStorage.getItem('relay-draft:' + selected) || '';
setInterval(() => { if (!document.hidden) refresh(); },1000);

function renderAttachments() {
  const images = imageDrafts.get(selected) || [];
  const signature = JSON.stringify(images);
  const list = $('attachment-list'); list.hidden = images.length === 0;
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.innerHTML = images.map(img=>`<div class="attachment"><img data-image-id="${esc(img.id)}" alt="${esc(img.name)}" crossorigin="anonymous" referrerpolicy="no-referrer"><button type="button" data-remove-image="${esc(img.id)}" aria-label="移除 ${esc(img.name)}">×</button></div>`).join('');
  loadImages(list);
}

function loadImages(container) {
  for (const img of container.querySelectorAll('img[data-image-id]:not([data-loading])')) {
    img.dataset.loading = 'true';
    loadImage(img);
  }
}
$('image-dialog').addEventListener('close',()=>$('image-view').replaceChildren());

async function loadImage(img,retried=false) {
  const id=img.dataset.imageId, account=generation;
  try {
    let entry=imageUrls.get(id);
    if (!entry || entry.expiresAt < Date.now()+30000) {
      if (!entry?.pending) {
        const pending=api(`images/${encodeURIComponent(id)}/url`);
        entry={pending,expiresAt:0}; imageUrls.set(id,entry);
      }
      const grant=await entry.pending;
      if (account!==generation) return;
      imageUrls.set(id,grant); entry=grant;
    }
    if (account!==generation || !img.isConnected) return;
    img.onerror=()=>{
      // Lazy loading can begin after a signature has expired. Refresh it once.
      if (!retried && account===generation && img.isConnected) {imageUrls.delete(id);loadImage(img,true);}
      else img.title='图片暂时无法加载，请刷新页面重试';
    };
    img.src=entry.url;
  } catch(error) {
    if(account===generation) {imageUrls.delete(id);img.title='图片暂时无法加载，请刷新页面重试';}
  }
}
async function addImages(files) {
  const id=selected, account=generation;
  if (!id || $('attach-image').disabled) return;
  const list=[...files].filter(file=>file.type.startsWith('image/'));
  if (!list.length) return;
  if ((imageDrafts.get(id)?.length||0)+list.length>4) {toast('每条消息最多 4 张图片');return;}
  uploadingImages++; renderHeader();
  const controller=new AbortController();imageUploads.add(controller);
  const timeout=setTimeout(()=>controller.abort(),120000);
  try {
    for (const file of list) {
      const prepared=await RelayImages.prepare(file,controller.signal);
      if(account!==generation) return;
      const current=imageDrafts.get(id)||[];
      if(current.reduce((sum,img)=>sum+img.size,0)+prepared.size>12*1024*1024)throw new Error('每条消息的图片合计最大 12 MiB');
      const image=await RelayImages.upload(prepared,async(path,body)=>{
        if(account!==generation)throw new DOMException('账号已退出','AbortError');
        return api(path,body);
      },controller.signal);
      if(account!==generation) return;
      imageDrafts.set(id,[...current,image]);renderAttachments();
    }
  } catch(error) {if(account===generation)toast(error.name==='AbortError'?'图片上传已取消或超时，请重试':error.message);}
  finally {clearTimeout(timeout);imageUploads.delete(controller);uploadingImages--;renderHeader();}
}
$('attach-image').addEventListener('click',()=>$('image-input').click());
$('image-input').addEventListener('change',()=>{addImages($('image-input').files);$('image-input').value='';});
$('prompt').addEventListener('paste',e=>{const images=[...(e.clipboardData?.files||[])].filter(f=>f.type.startsWith('image/'));if(images.length){e.preventDefault();addImages(images);}});
$('send-form').addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});
$('send-form').addEventListener('drop',e=>{if(e.dataTransfer?.files.length){e.preventDefault();addImages(e.dataTransfer.files);}});
function updateViewport() {
  if(!matchMedia('(max-width: 760px)').matches) return;
  const viewport=window.visualViewport;
  // Ignore pinch zoom; preserve the user's zoom gesture and accessibility controls.
  if(viewport && Math.abs(viewport.scale-1)>0.05) return;
  document.documentElement.style.setProperty('--viewport-height',(viewport?.height||innerHeight)+'px');
  document.documentElement.style.setProperty('--viewport-top',(viewport?.offsetTop||0)+'px');
}
window.visualViewport?.addEventListener('resize',updateViewport);
window.visualViewport?.addEventListener('scroll',updateViewport);
window.addEventListener('resize',updateViewport);
document.addEventListener('focusin',()=>requestAnimationFrame(updateViewport));
updateViewport();
