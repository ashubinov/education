/* LearnQuest — ядро фронтенда: API, тема, звуки, конфетти, роутер, утилиты */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const raw = s => ({ __raw: String(s) });
/** Шаблонные строки с авто-экранированием: html`<b>${userText}</b>` */
function html(strings, ...vals) {
  let out = '';
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) {
      const v = vals[i];
      const one = x => (x && x.__raw !== undefined) ? x.__raw : (x === false || x == null) ? '' : esc(x);
      out += Array.isArray(v) ? v.map(one).join('') : one(v);
    }
  });
  return raw(out);
}
const unraw = x => (x && x.__raw !== undefined) ? x.__raw : esc(x);

const S = { user: null, timers: [], settings: null, reminderTimer: null, lessonCtx: null };
const actions = {};

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const o = { credentials: 'same-origin', ...opts };
  if (o.json !== undefined) { o.method = o.method || 'POST'; o.headers = { 'Content-Type': 'application/json' }; o.body = JSON.stringify(o.json); delete o.json; }
  const r = await fetch('/api' + path, o);
  let data = null;
  try { data = await r.json(); } catch (e) { /* пусто */ }
  if (r.status === 401 && !path.startsWith('/auth') && path !== '/me') { S.user = null; location.hash = '#/auth'; throw new Error('Нужно войти'); }
  if (!r.ok) { const err = new Error((data && (data.detail?.[0]?.msg || data.detail)) || ('Ошибка ' + r.status)); err.status = r.status; throw err; }
  return data;
}

/* ---------- утилиты ---------- */
function toast(text, opts = {}) {
  const t = document.createElement('div');
  t.className = 'toast ' + (opts.cls || '');
  t.innerHTML = (opts.icon ? `<div class="ic">${esc(opts.icon)}</div>` : '') + `<div>${opts.title ? `<b>${esc(opts.title)}</b><br>` : ''}${esc(text)}</div>`;
  $('#toasts').appendChild(t);
  setTimeout(() => { t.style.transition = '.3s'; t.style.opacity = 0; t.style.transform = 'translateX(110%)'; setTimeout(() => t.remove(), 320); }, opts.ms || 3800);
}
function plural(n, one, few, many) { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? many : b > 1 && b < 5 ? few : b === 1 ? one : many; }
function fmtDate(iso) { const d = new Date(iso + 'T00:00:00'); return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }); }
function todayISO() { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
function addDays(iso, n) { const d = new Date(iso + 'T00:00:00'); d.setDate(d.getDate() + n); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
function every(fn, ms) { const id = setInterval(fn, ms); S.timers.push(id); return id; }
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; if (S.onLeave) { try { S.onLeave(); } catch (e) { } S.onLeave = null; } }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function render(root, h) { root.innerHTML = unraw(h); }
function busy(btn, on, text) {
  if (on) { btn.dataset.t = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>' + (text ? ' ' + esc(text) : ''); }
  else { btn.disabled = false; if (btn.dataset.t) btn.innerHTML = btn.dataset.t; }
}
function svgDataUrl(svg) { return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg); }

/* ---------- тема ---------- */
function hexToHsl(hex) {
  const n = parseInt(hex.slice(1), 16), r = (n >> 16) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b); let h = 0, s = 0; const l = (mx + mn) / 2;
  if (mx !== mn) {
    const d = mx - mn; s = l > .5 ? d / (2 - mx - mn) : d / (mx + mn);
    h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4; h *= 60;
  }
  return [h, s * 100, l * 100];
}
function hsl(h, s, l) { return `hsl(${h.toFixed(0)} ${s.toFixed(0)}% ${l.toFixed(0)}%)`; }
function lum(hex) { const n = parseInt(hex.slice(1), 16); const f = c => { c /= 255; return c <= .03928 ? c / 12.92 : Math.pow((c + .055) / 1.055, 2.4); }; return .2126 * f(n >> 16) + .7152 * f((n >> 8) & 255) + .0722 * f(n & 255); }
function applyTheme(color, mode) {
  color = /^#[0-9a-f]{6}$/i.test(color) ? color : '#7c5cff';
  const [h, s, l] = hexToHsl(color), root = document.documentElement.style;
  root.setProperty('--accent', color);
  root.setProperty('--accent-d', hsl(h, Math.min(100, s + 4), Math.max(14, l - 14)));
  const n = parseInt(color.slice(1), 16);
  root.setProperty('--accent-l', `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},.16)`);
  root.setProperty('--accent-ink', lum(color) > .42 ? '#14171f' : '#ffffff');
  const dark = mode === 'dark' || (mode === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches) || !mode;
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const m = $('meta[name=theme-color]'); if (m) m.content = dark ? '#12141c' : '#f3f5fb';
}

/* ---------- звуки ---------- */
let _ac;
function tone(freqs, dur = .11, type = 'sine', vol = .07) {
  _ac = _ac || new (window.AudioContext || window.webkitAudioContext)();
  const t0 = _ac.currentTime;
  freqs.forEach((f, i) => {
    const o = _ac.createOscillator(), g = _ac.createGain();
    o.type = type; o.frequency.value = f; o.connect(g); g.connect(_ac.destination);
    const s = t0 + i * dur; g.gain.setValueAtTime(0, s); g.gain.linearRampToValueAtTime(vol, s + .015); g.gain.exponentialRampToValueAtTime(.0001, s + dur * 1.6);
    o.start(s); o.stop(s + dur * 1.7);
  });
}
const SFX = { ok: () => tone([660, 990], .09), half: () => tone([520, 600], .1), bad: () => tone([240, 170], .14, 'triangle', .08), done: () => tone([523, 659, 784, 1047], .12), level: () => tone([392, 523, 659, 784, 1047], .1), tap: () => tone([440], .05, 'sine', .04) };
function play(n) { if (S.user && S.user.sound) { try { SFX[n](); } catch (e) { } } }

/* ---------- конфетти ---------- */
function confetti(n = 140) {
  const c = $('#confetti'), x = c.getContext('2d'); c.width = innerWidth; c.height = innerHeight;
  const cols = ['#ff5c6c', '#ffc233', '#3ddc84', '#4cc9f0', '#b388ff', getComputedStyle(document.documentElement).getPropertyValue('--accent')];
  const ps = Array.from({ length: n }, () => ({ x: innerWidth / 2 + (Math.random() - .5) * 200, y: innerHeight * .55, vx: (Math.random() - .5) * 16, vy: -Math.random() * 16 - 4, w: 6 + Math.random() * 6, h: 4 + Math.random() * 5, r: Math.random() * 6, vr: (Math.random() - .5) * .4, c: cols[(Math.random() * cols.length) | 0] }));
  let f = 0;
  (function step() {
    x.clearRect(0, 0, c.width, c.height); f++;
    ps.forEach(p => { p.vy += .35; p.x += p.vx; p.y += p.vy; p.vx *= .99; p.r += p.vr; x.save(); x.translate(p.x, p.y); x.rotate(p.r); x.fillStyle = p.c; x.globalAlpha = Math.max(0, 1 - f / 140); x.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); x.restore(); });
    if (f < 150) requestAnimationFrame(step); else x.clearRect(0, 0, c.width, c.height);
  })();
}
function xpPop(text, el) {
  const r = (el || document.body).getBoundingClientRect(), d = document.createElement('div');
  d.className = 'xp-pop'; d.textContent = text; d.style.left = Math.max(10, r.left + r.width / 2 - 20) + 'px'; d.style.top = Math.max(60, r.top - 10) + 'px';
  document.body.appendChild(d); setTimeout(() => d.remove(), 1000);
}

/* ---------- модальные окна ---------- */
function openModal(content, { onClose } = {}) {
  closeModal();
  const bg = document.createElement('div'); bg.className = 'modal-bg';
  bg.innerHTML = `<div class="card modal">${unraw(content)}</div>`;
  bg.addEventListener('mousedown', e => { if (e.target === bg) closeModal(); });
  $('#modal-root').appendChild(bg); S.modalClose = onClose;
  return bg.firstElementChild;
}
function closeModal() { const r = $('#modal-root'); if (r.firstChild) { r.innerHTML = ''; if (S.modalClose) { S.modalClose(); S.modalClose = null; } } }
actions.closeModal = closeModal;

/* ---------- каркас страниц ---------- */
function hud() {
  const u = S.user; if (!u) return '';
  const lv = u.level, st = u.streak;
  return html`<div class="hud">
    <div class="hud-streak ${st.current ? '' : 'off'}" title="Серия: ${st.current} дн. (рекорд ${st.best})"><span class="flame">🔥</span>${st.current}</div>
    <div class="hud-level" title="${lv.xp} XP"><span class="lvl-badge">Ур. ${lv.level}</span><div class="bar thin grow" style="width:90px"><i style="width:${lv.pct}%"></i></div><span class="xp small muted">${lv.into}/${lv.span}</span></div>
    <a href="#/settings" class="avatar" title="${u.display_name}">${u.avatar}</a>
  </div>`;
}
function shell(active, inner) {
  const nav = [['#/', '🏠', 'Курсы', 'home'], ['#/stats', '📊', 'Статистика', 'stats'], ['#/settings', '⚙️', 'Настройки', 'settings']];
  return html`<header class="topbar"><div class="topbar-in">
      <a class="logo" href="#/"><span>🦉</span><span>Learn<b>Quest</b></span></a>
      <nav class="nav">${nav.map(n => html`<a href="${n[0]}" class="${active === n[3] ? 'on' : ''}">${n[1]} ${n[2]}</a>`)}</nav>
      <div id="hud">${hud()}</div></div></header>
    <main>${inner}</main>
    <nav class="bottom-nav">${nav.map(n => html`<a href="${n[0]}" class="${active === n[3] ? 'on' : ''}"><span>${n[1]}</span>${n[2]}</a>`)}</nav>`;
}
function refreshHud() { const h = $('#hud'); if (h) h.innerHTML = unraw(hud()); }
async function refreshMe() { try { S.user = await api('/me'); refreshHud(); } catch (e) { /* ignore */ } }

/* ---------- роутер ---------- */
const routes = [];
function route(re, fn) { routes.push([re, fn]); }
async function router() {
  clearTimers(); closeModal();
  const h = location.hash || '#/';
  if (!S.user) {
    try { S.user = await api('/me'); applyTheme(S.user.theme_color, S.user.theme_mode); startReminders(); }
    catch (e) { if (h !== '#/auth') { location.hash = '#/auth'; return; } }
  }
  if (S.user && h === '#/auth') { location.hash = '#/'; return; }
  window.scrollTo(0, 0);
  for (const [re, fn] of routes) {
    const m = h.match(re);
    if (m) { try { await fn(...m.slice(1)); } catch (e) { if (e.message !== 'Нужно войти') { console.error(e); $('#app').innerHTML = `<main><div class="card center"><h2>😵 Что-то пошло не так</h2><p class="muted">${esc(e.message)}</p><a class="btn" href="#/">На главную</a></div></main>`; } } return; }
  }
  location.hash = '#/';
}

/* ---------- напоминания в браузере ---------- */
function startReminders() {
  if (S.reminderTimer) return;
  const check = async () => {
    if (!S.user) return;
    try {
      const r = await api('/reminder');
      S.reminder = r;
      const key = 'lq_notified_' + todayISO();
      if (r.due && !localStorage.getItem(key) && 'Notification' in window && Notification.permission === 'granted') {
        localStorage.setItem(key, '1');
        const n = new Notification(r.title, { body: r.body, icon: "data:image/svg+xml," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🦉</text></svg>") });
        n.onclick = () => { window.focus(); location.hash = '#/'; n.close(); };
      }
      const b = $('#reminder-banner');
      if (b) b.classList.toggle('hidden', !r.due);
    } catch (e) { /* ignore */ }
  };
  S.reminderTimer = setInterval(check, 60000); check();
}

/* ---------- запуск ---------- */
document.addEventListener('click', e => {
  const t = e.target.closest('[data-act]');
  if (t && actions[t.dataset.act]) { e.preventDefault(); actions[t.dataset.act](t, e); }
});
function boot() {
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => S.user && applyTheme(S.user.theme_color, S.user.theme_mode));
  window.addEventListener('hashchange', router);
  router();
}
