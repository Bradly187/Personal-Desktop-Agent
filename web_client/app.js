// ============================================================================
// Desktop Agent — Web Client for iPad Safari
// Protocol-compatible with ipad_bridge.py WebSocket server
// ============================================================================

// --- Settings ---
const DEFAULTS = {
  serverHost: '192.168.18.2',
  serverPort: 8765,
  trackpadSpeed: 2.5,
  keywordEnabled: true,
  keywordList: ['click', 'scroll', 'open', 'close', 'copy', 'paste', 'undo'],
  commandButtons: [
    { label: 'Click', action: 'CLICK', params: {} },
    { label: 'Right Click', action: 'CLICK', params: { button: 'right' } },
    { label: 'Scroll ↓', action: 'SCROLL', params: { direction: 'down' } },
    { label: 'Scroll ↑', action: 'SCROLL', params: { direction: 'up' } },
    { label: 'Undo', action: 'HOTKEY', params: { keys: 'ctrl,z' } },
    { label: 'Screenshot', action: 'SCREENSHOT', params: {} },
    { label: 'Tab', action: 'HOTKEY', params: { keys: 'tab' } },
    { label: 'Enter', action: 'HOTKEY', params: { keys: 'enter' } },
    { label: 'Escape', action: 'HOTKEY', params: { keys: 'escape' } },
    { label: 'Space', action: 'HOTKEY', params: { keys: 'space' } },
    { label: 'Kiro', action: 'OPEN', params: { target: 'Kiro' } },
    { label: 'Claude', action: 'OPEN', params: { target: 'Claude' } },
    { label: 'Chrome', action: 'OPEN', params: { target: 'Google Chrome' } },
  ],
};

function loadSettings() {
  const v = localStorage.getItem('da_v');
  if (v !== '5') { localStorage.clear(); localStorage.setItem('da_v', '5'); return { ...DEFAULTS }; }
  const s = localStorage.getItem('da_s');
  return s ? { ...DEFAULTS, ...JSON.parse(s) } : { ...DEFAULTS };
}
function saveSettings() { localStorage.setItem('da_s', JSON.stringify(settings)); }
let settings = loadSettings();

// --- WebSocket ---
let ws = null, msgCounter = 0, reconnectAttempt = 0, reconnectTimer = null;

function wsURL() { return `ws://${settings.serverHost}:${settings.serverPort}/ws`; }

function connect() {
  if (ws && ws.readyState <= 1) return;
  setBanner('connecting');
  try { ws = new WebSocket(wsURL()); } catch(e) { setBanner('disconnected'); return; }
  ws.onopen = () => { reconnectAttempt = 0; setBanner('connected'); };
  ws.onclose = () => { ws = null; if(selectMode) toggleSelectOff(); scheduleReconnect(); };
  ws.onerror = () => {};
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
}

function disconnect() { clearTimeout(reconnectTimer); if(ws){ws.onclose=null;ws.close();ws=null;} setBanner('disconnected'); }

function scheduleReconnect() {
  const delay = Math.min(2**reconnectAttempt * 1000, 30000);
  reconnectAttempt++;
  setBanner('connecting', `Reconnecting (${reconnectAttempt})…`);
  reconnectTimer = setTimeout(connect, delay);
}

function send(obj) { if(ws&&ws.readyState===1) ws.send(JSON.stringify(obj)); }

function sendCommand(action, text, params={}) {
  const id = `msg-${++msgCounter}`;
  const payload = { type:'touch_command', id, action };
  if(text) payload.text = text;
  if(Object.keys(params).length) {
    const p = {...params};
    if(p.keys && typeof p.keys==='string') p.keys = p.keys.split(',');
    payload.params = p;
  }
  send(payload);
  return id;
}

// --- Incoming ---
function handleMessage(msg) {
  if(msg.type==='ack' && msg.error) showStatus(`Error: ${msg.error}`, 3000);
  else if(msg.type==='status') showStatus(`${msg.active_window||''} · ${msg.cursor?.x||0},${msg.cursor?.y||0}`);
}

// --- UI ---
function setBanner(state, text) {
  document.getElementById('banner').className = state;
  document.getElementById('banner-text').textContent = text || ({connected:'Connected',connecting:'Connecting…',disconnected:'Disconnected'}[state]||'');
}
function showStatus(t, dur=5000) { const el=document.getElementById('status-bar'); el.textContent=t; if(dur)setTimeout(()=>{if(el.textContent===t)el.textContent='';},dur); }

// --- Tabs ---
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(`page-${tab.dataset.page}`).classList.add('active');
  });
});

// ============================================================================
// COMMANDS — data-driven sortable grid
// ============================================================================

// Default button definitions (order matters — this is the default layout)
const DEFAULT_BUTTON_ORDER = [
  { id: 'copy',       label: '📋 Copy',    action: 'HOTKEY',      params: {keys:'ctrl,c'}, style: 'green-border' },
  { id: 'paste',      label: '📌 Paste',   action: 'HOTKEY',      params: {keys:'ctrl,v'}, style: 'accent-border' },
  { id: 'undo',       label: '↩ Undo',     action: 'HOTKEY',      params: {keys:'ctrl,z'} },
  { id: 'scrollup',   label: '⬆️ Up',      action: 'SCROLL',      params: {direction:'up'}, holdScroll: true },
  { id: 'scrolldn',   label: '⬇️ Down',    action: 'SCROLL',      params: {direction:'down'}, holdScroll: true },
  { id: 'tab',        label: '⇥ Tab',      action: 'HOTKEY',      params: {keys:'tab'} },
  { id: 'enter',      label: '⏎ Enter',    action: 'HOTKEY',      params: {keys:'enter'} },
  { id: 'esc',        label: '✕ Esc',      action: 'HOTKEY',      params: {keys:'escape'} },
  { id: 'space',      label: '␣ Space',    action: 'HOTKEY',      params: {keys:'space'} },
  { id: 'kiro',       label: '🟢 Kiro',    action: 'OPEN',        params: {target:'Kiro'} },
  { id: 'claude',     label: '🟠 Claude',  action: 'OPEN',        params: {target:'Claude'} },
  { id: 'chrome',     label: '🌐 Chrome',  action: 'OPEN',        params: {target:'Google Chrome'} },
  { id: 'select',     label: '⬚ Select',   action: '_SELECT',     params: {} },
  { id: 'screenshot', label: '📷 Shot',    action: 'SCREENSHOT',  params: {} },
  { id: 'click',      label: '👆 Click',   action: 'CLICK',       params: {}, style: 'large' },
  { id: 'rclick',     label: '👆 Right',   action: 'CLICK',       params: {button:'right'}, style: 'large' },
];

function getButtonOrder() {
  const saved = localStorage.getItem('da_btn_order');
  if (saved) {
    try {
      const ids = JSON.parse(saved);
      // Rebuild from saved order, adding any new buttons at the end
      const map = Object.fromEntries(DEFAULT_BUTTON_ORDER.map(b => [b.id, b]));
      const ordered = ids.map(id => map[id]).filter(Boolean);
      // Add any buttons not in saved order (new additions)
      DEFAULT_BUTTON_ORDER.forEach(b => { if (!ids.includes(b.id)) ordered.push(b); });
      return ordered;
    } catch(e) {}
  }
  return [...DEFAULT_BUTTON_ORDER];
}

function saveButtonOrder() {
  const grid = document.getElementById('sortable-grid');
  const ids = Array.from(grid.children).map(el => el.dataset.btnId);
  localStorage.setItem('da_btn_order', JSON.stringify(ids));
}

let sortableInstance = null;
let layoutLocked = true;

function renderCommands() {
  const grid = document.getElementById('sortable-grid');
  grid.innerHTML = '';
  const buttons = getButtonOrder();

  buttons.forEach(btn => {
    const el = document.createElement('button');
    el.className = 'grid-btn';
    if (btn.style) btn.style.split(' ').forEach(c => el.classList.add(c));
    el.dataset.btnId = btn.id;
    el.textContent = btn.label;
    grid.appendChild(el);
    wireGridButton(el, btn);
  });

  initSortable();
  initLayoutLock();
}

function wireGridButton(el, btn) {
  if (btn.action === '_SELECT') {
    // Select mode — special handling
    el.addEventListener('touchstart', e => {
      e.preventDefault();
      if (layoutLocked) handleSelectToggle(el);
    }, {passive:false});
    return;
  }

  if (btn.holdScroll) {
    // Hold-to-repeat scroll
    let iv = null, holdTime = 0;
    const start = () => {
      if (!layoutLocked) return;
      el.style.background = 'var(--accent)';
      holdTime = 0;
      const go = () => { holdTime += 100; sendCommand('SCROLL', 'scroll', {...btn.params, amount: String(holdTime<500?5:holdTime<1500?15:30)}); };
      go(); iv = setInterval(go, 100);
    };
    const stop = () => { el.style.background = ''; if(iv){clearInterval(iv);iv=null;} };
    el.addEventListener('touchstart', e => { e.preventDefault(); start(); }, {passive:false});
    el.addEventListener('touchend', e => { e.preventDefault(); stop(); }, {passive:false});
    el.addEventListener('touchcancel', stop);
    return;
  }

  // Standard button
  el.addEventListener('touchstart', e => {
    e.preventDefault();
    if (!layoutLocked) return; // Don't fire commands in edit mode
    el.style.background = 'var(--accent)';
    sendCommand(btn.action, el.textContent.trim(), btn.params);
    setTimeout(() => el.style.background = '', 150);
  }, {passive:false});
}

function initSortable() {
  const grid = document.getElementById('sortable-grid');
  sortableInstance = new Sortable(grid, {
    animation: 200,
    delay: 300,
    delayOnTouchOnly: true,
    ghostClass: 'sortable-ghost',
    chosenClass: 'sortable-chosen',
    disabled: true, // Start locked
    onEnd: () => saveButtonOrder(),
  });
}

function initLayoutLock() {
  const lockBtn = document.getElementById('layout-lock');
  const resetBtn = document.getElementById('layout-reset');
  const grid = document.getElementById('sortable-grid');

  lockBtn.addEventListener('click', () => {
    layoutLocked = !layoutLocked;
    sortableInstance.option('disabled', layoutLocked);
    grid.classList.toggle('editing', !layoutLocked);
    lockBtn.textContent = layoutLocked ? '🔒 Locked' : '🔓 Editing';
    lockBtn.style.borderColor = layoutLocked ? 'var(--surface2)' : 'var(--accent)';
    lockBtn.style.color = layoutLocked ? 'var(--text2)' : 'var(--accent)';
    resetBtn.style.display = layoutLocked ? 'none' : 'inline-block';
  });

  resetBtn.addEventListener('click', () => {
    localStorage.removeItem('da_btn_order');
    renderCommands();
    showStatus('Layout reset to default', 2000);
  });
}

// Select mode (preserved from before)
let selectMode = false, selectTimer = null;

function handleSelectToggle(btn) {
  if (!selectMode) {
    selectMode = true;
    btn.style.background = 'var(--accent)'; btn.style.borderColor = 'var(--accent)'; btn.style.color = '#fff';
    btn.textContent = '⬛ Stop';
    sendCommand('MOUSEDOWN', 'mousedown');
    selectTimer = setTimeout(() => { if(selectMode) toggleSelectOff(); }, 10000);
  } else {
    toggleSelectOff();
  }
}

function toggleSelectOff() {
  selectMode = false; clearTimeout(selectTimer);
  const btn = document.querySelector('[data-btn-id="select"]');
  if (btn) {
    btn.style.background = ''; btn.style.borderColor = ''; btn.style.color = '';
    btn.textContent = '⬚ Select';
  }
  sendCommand('MOUSEUP', 'mouseup');
}

// Copy/Paste debounce is now handled by the grid button wiring (HOTKEY with ctrl,c / ctrl,v)

// (Select mode and Copy/Paste are now handled by the sortable grid above)

// ============================================================================
// TRACKPAD — minimal latency, no throttle on moves
// ============================================================================
function initTrackpad() {
  const area = document.getElementById('trackpad-area');
  if(!area) { console.error('trackpad-area not found!'); return; }
  let tracking = false, lastX=0, lastY=0, moved=false, startTime=0, fingerCount=0;

  area.addEventListener('pointerdown', e => {
    e.preventDefault();
    area.style.background = 'var(--surface2)';
    try { area.setPointerCapture(e.pointerId); } catch(ex) {}
    fingerCount = 1;
    lastX = e.clientX;
    lastY = e.clientY;
    tracking = true;
    moved = false;
    startTime = Date.now();
  });

  area.addEventListener('pointermove', e => {
    if(!tracking) return;
    e.preventDefault();

    const rawDx = e.clientX - lastX;
    const rawDy = e.clientY - lastY;
    lastX = e.clientX;
    lastY = e.clientY;

    if(Math.abs(rawDx)>80 || Math.abs(rawDy)>80) return;

    const dx = Math.round(rawDx * settings.trackpadSpeed);
    const dy = Math.round(rawDy * settings.trackpadSpeed);
    if(dx===0 && dy===0) return;

    moved = true;
    send({type:'trackpad', event:'move', dx, dy});
  });

  area.addEventListener('pointerup', e => {
    e.preventDefault();
    area.style.background = 'var(--surface)';
    tracking = false;
    if(!moved && (Date.now()-startTime)<250) {
      msgCounter++;
      send({type:'trackpad', id:`tp-${msgCounter}`, event:'tap', button:'left'});
    }
    moved = false;
  });

  area.addEventListener('pointercancel', () => { tracking=false; moved=false; area.style.background='var(--surface)'; });

  // Fallback: also listen for touchmove in case pointermove doesn't fire on iOS
  area.addEventListener('touchmove', e => {
    e.preventDefault();
    if(!tracking) return;
    const t = e.touches[0];
    if(!t) return;

    const rawDx = t.clientX - lastX;
    const rawDy = t.clientY - lastY;
    lastX = t.clientX;
    lastY = t.clientY;

    if(Math.abs(rawDx)>80 || Math.abs(rawDy)>80) return;

    const dx = Math.round(rawDx * settings.trackpadSpeed);
    const dy = Math.round(rawDy * settings.trackpadSpeed);
    if(dx===0 && dy===0) return;

    moved = true;
    send({type:'trackpad', event:'move', dx, dy});
  }, {passive:false});
}

// ============================================================================
// KEYPAD
// ============================================================================
let expression='', lastAnswer='';
const BASIC_KEYS=[['AC','⌫','()','÷'],['7','8','9','×'],['4','5','6','−'],['1','2','3','+'],['0','.','ANS','Send']];

function renderKeypad() {
  const grid = document.getElementById('keypad-grid');
  grid.innerHTML = '';
  BASIC_KEYS.flat().forEach(label => {
    const btn = document.createElement('button');
    btn.className = 'key-btn'+(label==='Send'?' accent wide':'');
    btn.textContent = label;
    btn.addEventListener('click', ()=>handleKey(label));
    grid.appendChild(btn);
  });
}
function handleKey(k) {
  switch(k) {
    case 'AC': expression=''; break;
    case '⌫': expression=expression.slice(0,-1); break;
    case '()': { const o=(expression.match(/\(/g)||[]).length,c=(expression.match(/\)/g)||[]).length; expression+=o>c?')':'('; break; }
    case 'ANS': expression+=lastAnswer||'0'; break;
    case 'Send': { const r=evalExpr(expression); if(r!==null)lastAnswer=fmtNum(r); sendCommand('DICTATE',expression); expression=''; break; }
    default: expression+=k;
  }
  document.getElementById('expr').textContent=expression||'0';
  const r=evalExpr(expression);
  document.getElementById('preview').textContent=r!==null?`= ${fmtNum(r)}`:'';
}
function evalExpr(e){try{return Function(`"use strict";return(${e.replace(/×/g,'*').replace(/÷/g,'/').replace(/−/g,'-').replace(/π/g,'Math.PI').replace(/\^/g,'**')})`)();}catch{return null;}}
function fmtNum(n){return Number.isInteger(n)&&Math.abs(n)<1e12?String(n):n.toPrecision(6).replace(/\.?0+$/,'');}

// ============================================================================
// WRITE (Scribble)
// ============================================================================
function sendScribble() {
  const input = document.getElementById('scribble-input');
  const text = input.value.trim();
  if(text) { sendCommand('TYPE',text,{text}); input.value=''; showStatus(`Typed: "${text}"`,2000); }
}
function sendScribbleAs(action) {
  const input = document.getElementById('scribble-input');
  const text = input.value.trim();
  if(!text) return;
  if(action==='HOTKEY') { sendCommand('HOTKEY',text,{keys:text.replace(/\+/g,' ').split(/\s+/).join(',')}); }
  else { sendCommand(action,text); }
  input.value='';
  showStatus(`${action}: "${text}"`,2000);
}

// ============================================================================
// PICKUP DETECTION — auto-switch to Write tab when iPad is lifted
// ============================================================================
let pickupActive = false, isHeld = false;

function initPickupDetection() {
  if(typeof DeviceMotionEvent !== 'undefined' && typeof DeviceMotionEvent.requestPermission === 'function') {
    // iOS requires permission — we'll request it from Settings tab
    return;
  }
  _startPickupListener();
}

function requestMotionPermission() {
  if(typeof DeviceMotionEvent.requestPermission === 'function') {
    DeviceMotionEvent.requestPermission().then(state => {
      if(state==='granted') _startPickupListener();
    });
  }
}

function _startPickupListener() {
  if(pickupActive) return;
  pickupActive = true;
  window.addEventListener('devicemotion', onPickupMotion);
}

function onPickupMotion(e) {
  const g = e.accelerationIncludingGravity;
  if(!g) return;

  // When flat on table: z ≈ -9.8, y ≈ 0
  // When held upright (portrait): y ≈ -9.8, z ≈ 0
  // Threshold: if |y| > 6 and |z| < 5 → held upright
  const absY = Math.abs(g.y || 0);
  const absZ = Math.abs(g.z || 0);

  const nowHeld = absY > 6 && absZ < 5;

  if(nowHeld && !isHeld) {
    // Just picked up — switch to Write tab
    isHeld = true;
    switchToTab('write');
    showStatus('📝 Writing mode', 2000);
  } else if(!nowHeld && isHeld) {
    // Put back down — switch to Control tab
    isHeld = false;
    switchToTab('commands');
    showStatus('🖱️ Control mode', 2000);
  }
}

function switchToTab(page) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const tab = document.querySelector(`[data-page="${page}"]`);
  if(tab) tab.classList.add('active');
  const pg = document.getElementById(`page-${page}`);
  if(pg) pg.classList.add('active');
}

// ============================================================================
// VOICE KEYWORDS
// ============================================================================
let recognition=null, keywordActive=false;
function startKeywords() {
  if(!settings.keywordEnabled||keywordActive) return;
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR) return;
  recognition=new SR(); recognition.continuous=true; recognition.interimResults=true; recognition.lang='en-US';
  recognition.onresult=(e)=>{
    const t=e.results[e.results.length-1][0].transcript.toLowerCase();
    for(const kw of settings.keywordList){if(t.includes(kw.toLowerCase())){send({type:'keyword',id:`kw-${++msgCounter}`,word:kw,confidence:0.85});showStatus(`"${kw}"`,2000);recognition.stop();setTimeout(()=>{if(keywordActive)recognition.start();},300);return;}}
  };
  recognition.onend=()=>{if(keywordActive)setTimeout(()=>recognition.start(),200);};
  recognition.onerror=()=>{};
  recognition.start(); keywordActive=true;
}

// ============================================================================
// SETTINGS
// ============================================================================
function renderSettings() {
  document.getElementById('page-settings').innerHTML = `
    <div class="settings-section"><h3>Connection</h3>
      <div class="setting-row"><label>Host</label><input type="text" id="s-host" value="${settings.serverHost}"></div>
      <div class="setting-row"><label>Port</label><input type="number" id="s-port" value="${settings.serverPort}"></div>
      <button class="reconnect-btn" onclick="settings.serverHost=document.getElementById('s-host').value;settings.serverPort=parseInt(document.getElementById('s-port').value);saveSettings();disconnect();reconnectAttempt=0;connect();">Reconnect</button>
    </div>
    <div class="settings-section"><h3>Trackpad</h3>
      <div class="setting-row"><label>Speed</label><input type="range" id="s-speed" min="0.5" max="5" step="0.1" value="${settings.trackpadSpeed}"></div>
    </div>
    <div class="settings-section"><h3>Pickup Detection</h3>
      <div class="setting-row"><label>Auto-switch to Write when lifted</label><button class="reconnect-btn" style="width:auto;margin:0;padding:8px 16px" onclick="requestMotionPermission()">Enable</button></div>
    </div>
    <div class="settings-section"><h3>Voice Keywords</h3>
      <div class="setting-row"><label>Enable</label><label class="toggle"><input type="checkbox" id="s-voice" ${settings.keywordEnabled?'checked':''}><span class="slider"></span></label></div>
      <div class="setting-row"><label>Keywords</label><input type="text" id="s-kw" value="${settings.keywordList.join(', ')}" style="width:200px"></div>
    </div>`;
  document.getElementById('s-speed')?.addEventListener('change',e=>{settings.trackpadSpeed=parseFloat(e.target.value);saveSettings();});
  document.getElementById('s-voice')?.addEventListener('change',e=>{settings.keywordEnabled=e.target.checked;saveSettings();});
  document.getElementById('s-kw')?.addEventListener('change',e=>{settings.keywordList=e.target.value.split(',').map(s=>s.trim()).filter(Boolean);saveSettings();});
}

// ============================================================================
// INIT
// ============================================================================
document.addEventListener('DOMContentLoaded', () => {
  renderCommands();
  renderKeypad();
  renderSettings();
  initTrackpad();
  initPickupDetection();
  connect();
  setTimeout(()=>{ if(settings.keywordEnabled) startKeywords(); }, 1000);
});
