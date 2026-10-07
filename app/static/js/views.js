/* LearnQuest — экраны: вход, курсы, курс, подготовка урока, статистика, настройки */
'use strict';

const app = () => $('#app');
const TIPS = [
  'Лучше заниматься каждый день по чуть-чуть, чем раз в неделю по три часа.',
  'Ошибка — это нормально: именно на них мозг запоминает лучше всего.',
  'Не знаешь ответ? Попробуй вспомнить сначала — а потом подсматривай.',
  'Пять минут в день — 30 часов за год.',
  'Объясни термин своими словами — так он запомнится крепче.',
  'Серия дней — твоя суперсила: не давай огню погаснуть 🔥',
];

/* =============================== вход =============================== */
route(/^#\/auth$/, async () => {
  S.authTab = S.authTab || 'login';
  drawAuth();
});
function drawAuth() {
  const reg = S.authTab === 'register';
  render(app(), html`<div class="auth-wrap"><div class="auth">
    <div class="hero"><span class="mascot">🦉</span><h1>Learn<span style="color:var(--accent)">Quest</span></h1><p class="muted">Загрузи материалы — получи курс, XP и серию дней</p></div>
    <form class="card" id="authform" autocomplete="on">
      <div class="tabs"><button type="button" data-act="authTab" data-tab="login" class="${reg ? '' : 'on'}">Вход</button><button type="button" data-act="authTab" data-tab="register" class="${reg ? 'on' : ''}">Регистрация</button></div>
      ${reg ? html`<div class="field"><label>Как тебя зовут</label><input type="text" name="display_name" maxlength="40" placeholder="Например, Андрей"></div>` : ''}
      <div class="field"><label>Логин</label><input type="text" name="username" required autocomplete="username" placeholder="латиница, цифры, . _ -"></div>
      <div class="field"><label>Пароль</label><input type="password" name="password" required minlength="6" autocomplete="${reg ? 'new-password' : 'current-password'}" placeholder="не короче 6 символов"></div>
      <div class="err" id="autherr"></div>
      <button class="btn block lg" type="submit">${reg ? 'Создать аккаунт' : 'Войти'}</button>
    </form></div></div>`);
  $('#authform').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.target)), btn = $('button[type=submit]', e.target);
    busy(btn, true);
    try {
      S.user = await api(reg ? '/auth/register' : '/auth/login', { json: fd });
      applyTheme(S.user.theme_color, S.user.theme_mode); startReminders(); location.hash = '#/';
    } catch (err) { $('#autherr').textContent = err.message; busy(btn, false); }
  });
}
actions.authTab = b => { S.authTab = b.dataset.tab; drawAuth(); };
actions.logout = async () => { await api('/auth/logout', { method: 'POST' }); S.user = null; clearInterval(S.reminderTimer); S.reminderTimer = null; location.hash = '#/auth'; };

/* =============================== дашборд =============================== */
function courseCard(c) {
  const p = c.progress;
  if (c.status === 'processing') return html`<div class="card course-card"><div class="row"><div class="course-icon">⏳</div><div><h3>${c.title}</h3><div class="muted small"><span class="status-dot"></span> ${c.status_text || 'Готовлю курс'}<span class="dots"></span></div></div></div>
      <div class="bar thin"><i style="width:35%;animation:pulse 1.2s infinite"></i></div><div class="muted small">ИИ читает материалы и составляет план. Обычно 20–60 секунд.</div></div>`;
  if (c.status === 'error') return html`<div class="card course-card" style="border-color:var(--bad)"><div class="row"><div class="course-icon">⚠️</div><h3>${c.title}</h3></div><div class="err small">${c.error}</div>
      <div class="row"><button class="btn sm" data-act="retryCourse" data-id="${c.id}">Повторить</button><button class="btn sm ghost" data-act="deleteCourse" data-id="${c.id}">Удалить</button></div></div>`;
  const g = c.goal;
  return html`<div class="card course-card" data-act="openCourse" data-id="${c.id}">
    <div class="row"><div class="course-icon">${c.icon}</div><div class="grow"><h3 class="clamp">${c.title}</h3><div class="muted small clamp">${c.next_title ? 'Далее: ' + c.next_title : c.description}</div></div></div>
    <div><div class="row spread small"><b>${c.status === 'completed' ? 'Курс пройден 🏆' : p.percent + '%'}</b><span class="muted">${p.lessons_done} из ~${p.lessons_total} ур.</span></div><div class="bar ${c.status === 'completed' ? 'good' : ''}"><i style="width:${p.percent}%"></i></div></div>
    <div class="row wrap gap-s"><span class="chip accent">⭐ ${c.xp} XP</span>${c.streak.current ? html`<span class="chip warn">🔥 ${c.streak.current}</span>` : ''}${g && g.status === 'ok' ? html`<span class="chip ${g.reached_today ? 'good' : ''}">🎯 ${g.reached_today ? 'на сегодня готово' : '≈' + g.per_day_minutes + ' мин/день'}</span>` : ''}${g && g.status === 'overdue' ? html`<span class="chip warn">⏰ срок прошёл</span>` : ''}</div>
    <button class="btn block" data-act="learn" data-id="${c.id}">${c.status === 'completed' ? 'Повторить' : p.lessons_done ? 'Продолжить' : 'Начать'}</button></div>`;
}
route(/^#\/$/, async () => {
  render(app(), shell('home', html`<div class="center muted mt-l"><span class="spinner"></span></div>`));
  const [courses, stats] = await Promise.all([api('/courses'), api('/stats')]);
  S.courses = courses;
  const goals = courses.filter(c => c.goal && c.goal.status === 'ok');
  const need = goals.reduce((a, c) => a + c.goal.per_day_minutes, 0), spent = goals.reduce((a, c) => a + c.goal.spent_today, 0);
  const todayLessons = stats.week.find(d => d.today)?.lessons || 0;
  const pct = goals.length ? Math.min(100, Math.round(spent / Math.max(need, 1) * 100)) : (todayLessons ? 100 : 0);
  render(app(), shell('home', html`
    <div id="reminder-banner" class="banner ${S.reminder && S.reminder.due ? '' : 'hidden'}"><span style="font-size:30px">🔔</span><div class="grow"><b>${S.reminder ? S.reminder.title : ''}</b><div class="small">${S.reminder ? S.reminder.body : ''}</div></div></div>
    <div class="card quest mb">
      <div class="ring">${ringSvg(pct)}<div class="t"><div><div style="font-size:26px">${pct}%</div><div class="small muted">сегодня</div></div></div></div>
      <div class="grow">
        <h2>${S.user.streak.done_today ? 'Серия на сегодня сохранена! 🔥' : 'Привет, ' + S.user.display_name + '!'}</h2>
        <p class="muted">${goals.length ? `Для твоих целей сегодня нужно ≈ ${need} мин (занимался: ${Math.round(spent * 10) / 10} мин).` : (todayLessons ? 'Отлично, сегодня уже были уроки. Продолжай!' : 'Пройди хотя бы один урок — и серия продолжится.')}</p>
        <div class="week-dots">${stats.week.map(d => html`<div class="d ${d.lessons ? 'on' : ''} ${d.today ? 'today' : ''}"><i>${d.lessons ? '🔥' : ''}</i>${d.label}</div>`)}</div>
      </div></div>
    <div class="row spread mb"><h2 style="margin:0">Мои курсы</h2><button class="btn sm" data-act="addCourse">＋ Добавить курс</button></div>
    <div class="grid" id="course-grid">${gridInner(courses)}</div>`));
  every(async () => {
    if (!S.courses.some(c => c.status === 'processing')) return;
    S.courses = await api('/courses'); const g = $('#course-grid'); if (g) g.innerHTML = unraw(gridInner(S.courses));
    if (!S.courses.some(c => c.status === 'processing')) refreshMe();
  }, 2500);
});
function gridInner(courses) {
  return html`${courses.map(courseCard)}<div class="card add-card" data-act="addCourse"><div><div class="plus">＋</div><b>Добавить курс</b><div class="small">загрузи PDF, DOCX, TXT, MD…</div></div></div>`;
}
function ringSvg(pct, size = 120, color = 'var(--accent)') {
  const r = size / 2 - 10, c = 2 * Math.PI * r;
  return raw(`<svg width="${size}" height="${size}"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--bg3)" stroke-width="12"/><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="12" stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - pct / 100)}" style="transition:stroke-dashoffset .8s"/></svg>`);
}
actions.openCourse = b => { location.hash = '#/course/' + b.dataset.id; };
actions.learn = (b, e) => { e.stopPropagation(); location.hash = '#/learn/' + b.dataset.id; };
actions.retryCourse = async b => { await api(`/courses/${b.dataset.id}/retry`, { method: 'POST' }).catch(e => toast(e.message)); router(); };
actions.deleteCourse = async (b, e) => {
  e && e.stopPropagation();
  if (!confirm('Удалить курс и весь прогресс по нему?')) return;
  await api('/courses/' + b.dataset.id, { method: 'DELETE' }); toast('Курс удалён'); if (location.hash.startsWith('#/course/')) location.hash = '#/'; else router();
};

/* ---------- добавление курса ---------- */
actions.addCourse = () => {
  S.newFiles = [];
  const m = openModal(html`<h2>Новый курс</h2><p class="muted">Загрузи конспекты, лекции, книги — я составлю по ним курс: модули, уроки, тесты и практику.</p>
    <div class="drop" id="drop"><div class="big">📚</div><b>Перетащи файлы сюда или нажми</b><div class="small muted">PDF, DOCX, TXT, MD, HTML, PPTX, RTF, CSV и другие текстовые · до ${25} МБ каждый</div></div>
    <input type="file" id="fileinput" multiple class="hidden"><div class="files" id="filelist"></div>
    <div class="field mt"><label>Название (необязательно)</label><input type="text" id="ctitle" maxlength="100" placeholder="Придумаю сам по содержанию"></div>
    <div class="err" id="cerr"></div>
    <div class="row mt" style="justify-content:flex-end"><button class="btn ghost" data-act="closeModal">Отмена</button><button class="btn" id="createbtn" data-act="createCourse" disabled>Создать курс</button></div>`);
  const drop = $('#drop', m), inp = $('#fileinput', m);
  drop.onclick = () => inp.click();
  inp.onchange = () => { addFiles(inp.files); inp.value = ''; };
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => addFiles(e.dataTransfer.files));
};
function addFiles(list) {
  for (const f of list) if (!S.newFiles.some(x => x.name === f.name && x.size === f.size)) S.newFiles.push(f);
  $('#filelist').innerHTML = S.newFiles.map((f, i) => `<div class="f"><span>📄 ${esc(f.name)} <span class="muted small">${(f.size / 1024).toFixed(0)} КБ</span></span><button class="icon-btn" data-act="rmFile" data-i="${i}" style="font-size:16px">✕</button></div>`).join('');
  $('#createbtn').disabled = !S.newFiles.length;
}
actions.rmFile = b => { S.newFiles.splice(+b.dataset.i, 1); addFiles([]); };
actions.createCourse = async b => {
  const fd = new FormData(); S.newFiles.forEach(f => fd.append('files', f)); fd.append('title', $('#ctitle').value);
  busy(b, true, 'Загружаю…');
  try {
    const r = await api('/courses', { method: 'POST', body: fd });
    closeModal(); (r.warnings || []).forEach(w => toast(w, { icon: '⚠️' })); toast('Курс создаётся — ИИ читает материалы', { icon: '🧠' });
    if (location.hash === '#/' || location.hash === '') router(); else location.hash = '#/';
  } catch (e) { $('#cerr').textContent = e.message; busy(b, false); }
};

/* =============================== страница курса =============================== */
route(/^#\/course\/(\d+)$/, async id => {
  render(app(), shell('home', html`<div class="center muted mt-l"><span class="spinner"></span></div>`));
  await drawCourse(id);
  every(async () => { if (S.courseView && (S.courseView.status === 'processing')) drawCourse(id); }, 2500);
});
async function drawCourse(id) {
  const c = await api('/courses/' + id); S.courseView = c;
  const p = c.progress, g = c.goal;
  const goalCard = g ? goalHtml(c) : html`<div class="card"><h3>🎯 Цель курса</h3><p class="muted">Выбери срок — я посчитаю, сколько минут в день нужно учиться, чтобы успеть.</p>
      <div class="row wrap gap-s"><button class="btn sm ghost" data-act="goalQuick" data-d="7">7 дней</button><button class="btn sm ghost" data-act="goalQuick" data-d="14">14 дней</button><button class="btn sm ghost" data-act="goalQuick" data-d="30">30 дней</button></div>
      <div class="row mt"><input type="date" id="goaldate" min="${todayISO()}" value="${addDays(todayISO(), 21)}"><button class="btn sm" data-act="goalSave">Поставить</button></div></div>`;
  if (c.status === 'processing') { render(app(), shell('home', html`<div class="loader-screen"><div><div class="mascot">🧠</div><h2>Собираю курс «${c.title}»<span class="dots"></span></h2><p class="muted">${c.status_text}</p></div></div>`)); return; }
  if (c.status === 'error') { render(app(), shell('home', html`<div class="card center"><h2>⚠️ Не получилось собрать курс</h2><p class="err">${c.error}</p><div class="row" style="justify-content:center"><button class="btn" data-act="retryCourse" data-id="${c.id}">Повторить</button><a class="btn ghost" href="#/settings">Настройки</a></div></div>`)); return; }
  render(app(), shell('home', html`
    <div class="card mb"><div class="course-head"><div class="course-icon">${c.icon}</div><div class="grow"><h1 style="margin-bottom:4px">${c.title}</h1><p class="muted" style="margin:0">${c.description}</p>
      <div class="row wrap gap-s mt"><span class="chip accent">⭐ ${c.xp} XP</span><span class="chip">📘 ${c.modules.length} ${plural(c.modules.length, 'модуль', 'модуля', 'модулей')}</span><span class="chip">✅ ${c.lessons_done_total} ${plural(c.lessons_done_total, 'урок', 'урока', 'уроков')}</span>${c.streak.current ? html`<span class="chip warn">🔥 ${c.streak.current}</span>` : ''}</div></div>
      <div class="col" style="min-width:190px"><button class="btn lg block" data-act="learn" data-id="${c.id}">${c.status === 'completed' ? '🏆 Повторить' : p.lessons_done ? '▶ Продолжить' : '▶ Начать'}</button><button class="btn sm ghost block" data-act="deleteCourse" data-id="${c.id}">Удалить курс</button></div></div>
      <div class="mt"><div class="row spread small"><b>Прогресс: ${p.percent}%</b><span class="muted">пройдено ${p.lessons_done} из ~${p.lessons_total} ур. · осталось ≈ ${p.minutes_left} мин</span></div><div class="bar ${c.status === 'completed' ? 'good' : ''}"><i style="width:${p.percent}%"></i></div></div></div>
    <div class="two"><div>
      <h2>Путь обучения</h2>
      <div class="path">${c.modules.map((m, i) => html`<div class="mod ${m.state}"><div class="node">${m.state === 'done' ? '✓' : m.state === 'locked' ? '🔒' : i + 1}</div>
        <div class="card body ${m.state === 'current' ? 'glow' : 'flat'}"><div class="row spread wrap"><h3 style="margin:0">${m.kind === 'review' ? '🔁 ' : ''}${m.title}</h3>${m.state === 'done' && m.accuracy != null ? html`<span class="chip good">точность ${m.accuracy}%</span>` : m.state === 'current' && m.planned ? html`<span class="chip accent">${m.lessons_done}/${m.lessons_total} ур.</span>` : ''}</div>
        <p class="muted small" style="margin:6px 0 0">${m.summary}</p>
        ${m.terms.length ? html`<div class="terms">${m.terms.map(t => html`<span class="chip">${t}</span>`)}</div>` : ''}
        ${m.message ? html`<div class="note mt">💬 ${m.message}</div>` : ''}</div></div>`)}</div>
      <p class="muted small mt">Уроки подстраиваются под твои ответы, поэтому весь список заранее не показывается — просто нажимай «Продолжить».</p>
    </div><div class="col">${goalCard}
      <div class="card"><h3>📈 Неделя по курсу</h3>${barChart(c.week, 'xp', 'XP')}</div>
      ${c.notes ? html`<div class="card"><h3>🧭 Что я заметил о тебе</h3><p class="small" style="margin:0">${c.notes}</p></div>` : ''}
      <div class="card flat"><h3>📎 Материалы</h3>${c.sources.map(s => html`<div class="small row spread"><span>📄 ${s.filename}</span><span class="muted">${Math.round(s.chars / 1000)}k</span></div>`)}</div></div></div>`));
}
function goalHtml(c) {
  const g = c.goal, pct = g.per_day_minutes ? Math.min(100, Math.round(g.spent_today / g.per_day_minutes * 100)) : 100;
  return html`<div class="card glow"><div class="row spread"><h3 style="margin:0">🎯 Цель: до ${fmtDate(g.date)}</h3><button class="icon-btn" data-act="goalClear" title="Убрать цель">✕</button></div>
    ${g.status === 'done' ? html`<p class="mt">Курс уже пройден — цель достигнута! 🏆</p>` : html`
    <div class="row mt"><div class="ring" style="width:100px;height:100px">${ringSvg(pct, 100, g.reached_today ? 'var(--good)' : 'var(--accent)')}<div class="t"><div><div style="font-size:22px">${g.spent_today >= 10 ? Math.round(g.spent_today) : g.spent_today}</div><div class="small muted">из ${g.per_day_minutes} мин</div></div></div></div>
    <div class="grow small"><p style="margin:0 0 4px"><b>${g.status === 'overdue' ? 'Срок прошёл' : g.reached_today ? 'Норма на сегодня выполнена! ✅' : 'Сегодня нужно ≈ ' + g.per_day_minutes + ' мин'}</b></p>
      <p class="muted" style="margin:0">${g.status === 'overdue' ? 'Продли срок цели.' : `Осталось ${g.days_left} ${plural(g.days_left, 'день', 'дня', 'дней')} · ≈ ${g.lessons_left} ${plural(g.lessons_left, 'урок', 'урока', 'уроков')} · ≈ ${g.minutes_left} мин. Это ≈ ${g.lessons_per_day} ур. в день.`}</p></div></div>
    ${g.hard ? html`<div class="note mt" style="background:var(--warn-l)">⚠️ Темп очень плотный (больше 1,5 часа в день). Подумай, не сдвинуть ли срок.</div>` : ''}`}
    <div class="row mt"><input type="date" id="goaldate" min="${todayISO()}" value="${g.date}"><button class="btn sm ghost" data-act="goalSave">Изменить</button></div></div>`;
}
actions.goalQuick = b => { const i = $('#goaldate'); i.value = addDays(todayISO(), +b.dataset.d); actions.goalSave(); };
actions.goalSave = async () => {
  const id = S.courseView.id;
  try { await api(`/courses/${id}/goal`, { method: 'PUT', json: { date: $('#goaldate').value } }); toast('Цель сохранена', { icon: '🎯' }); await drawCourse(id); refreshMe(); }
  catch (e) { toast(e.message, { icon: '⚠️' }); }
};
actions.goalClear = async () => { await api(`/courses/${S.courseView.id}/goal`, { method: 'PUT', json: { date: null } }); await drawCourse(S.courseView.id); };

/* ---------- столбчатая диаграмма (SVG) ---------- */
function barChart(data, key, label) {
  const W = 360, H = 170, pad = 24, bw = 30, gap = (W - pad * 2 - bw * data.length) / (data.length - 1 || 1);
  const mx = Math.max(...data.map(d => d[key]), key === 'xp' ? 50 : 3);
  const bars = data.map((d, i) => {
    const h = Math.max(d[key] ? 6 : 2, (H - 50) * d[key] / mx), x = pad + i * (bw + gap), y = H - 24 - h;
    return `<g><rect x="${x}" y="${y}" width="${bw}" height="${h}" rx="8" fill="${d.today ? 'var(--accent)' : d[key] ? 'var(--accent-d)' : 'var(--bg3)'}"><title>${esc(d.day)}: ${d.xp} XP, ${d.lessons} ур., ${d.minutes} мин</title></rect>
      <text x="${x + bw / 2}" y="${y - 6}" text-anchor="middle" font-size="12" font-weight="800" fill="var(--text)">${d[key] || ''}</text>
      <text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle" font-size="12" font-weight="800" fill="${d.today ? 'var(--accent)' : 'var(--muted)'}">${d.label}</text></g>`;
  }).join('');
  return raw(`<svg viewBox="0 0 ${W} ${H}" width="100%" class="chart-bars" role="img" aria-label="${esc(label)} за неделю">${bars}</svg>`);
}

/* =============================== подготовка урока =============================== */
route(/^#\/learn\/(\d+)$/, async id => {
  let alive = true; S.onLeave = () => { alive = false; };
  let tip = 0, shown = false;
  const draw = (title, text, extra = '') => {
    render(app(), html`<div class="loader-screen"><div><div class="mascot">🦉</div><h2>${title}<span class="dots"></span></h2><p class="muted">${text}</p><div class="tip">💡 ${TIPS[tip % TIPS.length]}</div>${raw(extra)}</div></div>`);
    shown = true;
  };
  draw('Готовлю урок', 'Собираю задания под твой уровень');
  const tipTimer = setInterval(() => { tip++; const t = $('.tip'); if (t) t.innerHTML = '💡 ' + esc(TIPS[tip % TIPS.length]); }, 6000); S.timers.push(tipTimer);
  let retry = 0;
  while (alive) {
    let n;
    try { n = await api(`/courses/${id}/next${retry ? '?retry=1' : ''}`); retry = 0; }
    catch (e) { render(app(), shell('home', html`<div class="card center"><h2>😵 ${e.message}</h2><a class="btn" href="#/">На главную</a></div>`)); return; }
    if (!alive) return;
    if (n.state === 'ready') {
      try {
        const v = await api(`/lessons/${n.lesson_id}/start`, { method: 'POST', json: {} });
        S.lessonCtx = v; location.hash = '#/run/' + v.run_id; return;
      } catch (e) { if (e.status !== 409) { toast(e.message); return; } }
    } else if (n.state === 'completed') {
      render(app(), shell('home', html`<div class="result"><div class="big-emoji">🏆</div><h1>Курс пройден!</h1><p class="muted">Ты прошёл все модули. Можно вернуться к курсу и повторить любые темы.</p><a class="btn lg" href="#/course/${id}">К курсу</a></div>`)); confetti(220); play('level'); return;
    } else if (n.state === 'error') {
      render(app(), shell('home', html`<div class="card center"><h2>⚠️ Курс не собран</h2><p class="err">${n.error}</p><a class="btn" href="#/course/${id}">К курсу</a></div>`)); return;
    } else if (n.state === 'failed') {
      render(app(), shell('home', html`<div class="card center"><h2>⚠️ Не удалось подготовить урок</h2><p class="err">${n.error || ''}</p><div class="row" style="justify-content:center"><button class="btn" id="retrybtn">Повторить</button><a class="btn ghost" href="#/settings">Настройки</a></div></div>`));
      await new Promise(res => { $('#retrybtn').onclick = () => { retry = 1; draw('Готовлю урок', 'Пробую ещё раз'); res(); }; S.onLeave = () => { alive = false; res(); }; });
      continue;
    } else if (n.state === 'processing') draw('Собираю курс', n.text || '');
    else if (n.state === 'preparing') draw('Готовлю следующий модуль', 'Учитываю, как ты справился с предыдущим');
    await sleep(1400);
  }
});

/* =============================== статистика =============================== */
route(/^#\/stats$/, async () => {
  S.statCourse = S.statCourse || '';
  await drawStats();
});
async function drawStats() {
  const st = await api('/stats' + (S.statCourse ? '?course_id=' + S.statCourse : ''));
  S.statMetric = S.statMetric || 'xp';
  const days = st.heatmap.days, cols = Math.ceil(days.length / 7);
  const months = []; let last = -1;
  for (let c = 0; c < cols; c++) { const d = days[c * 7]; const m = new Date(d.day + 'T00:00:00').getMonth(); months.push(m !== last ? new Date(d.day + 'T00:00:00').toLocaleDateString('ru-RU', { month: 'short' }) : ''); last = m; }
  const today = todayISO();
  const fmtTitle = d => `${fmtDate(d.day)}: ${d.lessons} ${plural(d.lessons, 'урок', 'урока', 'уроков')}, ${d.xp} XP, ${d.minutes} мин`;
  render(app(), shell('stats', html`
    <div class="row spread wrap mb"><h1 style="margin:0">Статистика</h1>
      <select id="statcourse" style="width:auto;min-width:200px"><option value="">Все курсы</option>${st.courses.map(c => html`<option value="${c.id}" ${String(c.id) === String(S.statCourse) ? 'selected' : ''}>${c.icon} ${c.title}</option>`)}</select></div>
    <div class="tiles mb">
      <div class="card tile"><b>🔥 ${st.streak.current}</b><span>дней подряд</span></div>
      <div class="card tile"><b>🏅 ${st.streak.best}</b><span>рекорд серии</span></div>
      <div class="card tile"><b>✅ ${st.totals.lessons}</b><span>уроков</span></div>
      <div class="card tile"><b>⭐ ${st.totals.xp}</b><span>очков опыта</span></div>
      <div class="card tile"><b>⏱ ${st.totals.minutes}</b><span>минут</span></div>
      <div class="card tile"><b>🎯 ${st.totals.answers}</b><span>ответов</span></div></div>
    <div class="card mb"><div class="row spread wrap"><h3 style="margin:0">Активность за год</h3><span class="muted small">${st.streak.days_total} ${plural(st.streak.days_total, 'день', 'дня', 'дней')} с уроками</span></div>
      <div class="heat-scroll mt"><div style="display:flex;gap:8px"><div class="small muted" style="display:grid;grid-template-rows:18px repeat(7,13px);gap:3px;font-size:10px"><span></span>${['Пн', '', 'Ср', '', 'Пт', '', 'Вс'].map(x => html`<span style="line-height:13px">${x}</span>`)}</div>
        <div><div class="heat-months">${months.map(m => html`<span>${m}</span>`)}</div>
        <div class="heat">${days.map(d => html`<i class="l${d.level} ${d.day === today ? 'today' : ''} ${d.day > today ? 'fut' : ''}" title="${fmtTitle(d)}"></i>`)}</div></div></div></div>
      <div class="heat-legend">меньше ${[0, 1, 2, 3, 4].map(l => html`<i style="background:var(--g${l})"></i>`)} больше</div></div>
    <div class="two mb"><div class="card"><div class="row spread"><h3 style="margin:0">Неделя</h3><div class="tabs" style="margin:0;width:230px">${[['xp', 'XP'], ['lessons', 'Уроки'], ['minutes', 'Минуты']].map(([k, n]) => html`<button class="${S.statMetric === k ? 'on' : ''}" data-act="metric" data-k="${k}" style="padding:6px">${n}</button>`)}</div></div>
        <div class="mt">${barChart(st.week, S.statMetric, S.statMetric)}</div></div>
      <div class="card"><h3>Уровень ${st.level.level}</h3><div class="bar"><i style="width:${st.level.pct}%"></i></div><p class="muted small mt">${st.level.into} / ${st.level.span} XP до уровня ${st.level.level + 1}</p>
        <h3 class="mt">Опыт по курсам</h3>${st.courses.length ? st.courses.map(c => html`<div class="row spread small mb" style="margin-bottom:6px"><span>${c.icon} ${c.title}</span><b>⭐ ${c.xp}</b></div>`) : html`<p class="muted small">Курсов пока нет.</p>`}</div></div>
    <h2>Достижения <span class="muted small">${st.achievements.filter(a => a.unlocked).length}/${st.achievements.length}</span></h2>
    <div class="ach-grid">${st.achievements.map(a => html`<div class="ach ${a.unlocked ? '' : 'locked'}"><div class="ic">${a.icon}</div><div><b>${a.title}</b><span>${a.desc}</span></div></div>`)}</div>`));
  $('#statcourse').onchange = e => { S.statCourse = e.target.value; drawStats(); };
  S.lastStats = st;
}
actions.metric = b => { S.statMetric = b.dataset.k; drawStats(); };

/* =============================== настройки =============================== */
const COLORS = ['#7c5cff', '#3b82f6', '#06b6d4', '#10b981', '#84cc16', '#f59e0b', '#f97316', '#ef4444', '#ec4899', '#a855f7'];
const AVATARS = ['🦊', '🐼', '🦉', '🐙', '🦄', '🐲', '🐱', '🐶', '🦁', '🐸', '🚀', '🧙'];
route(/^#\/settings$/, async () => { await drawSettings(); });
async function drawSettings() {
  const [set, tg] = await Promise.all([api('/settings'), api('/telegram/status')]);
  S.settings = set; const u = S.user;
  render(app(), shell('settings', html`<h1>Настройки</h1><div class="col" style="max-width:760px">
    <div class="card"><h3>👤 Профиль</h3><div class="field"><label>Имя</label><div class="row"><input type="text" id="dname" value="${u.display_name}" maxlength="40"><button class="btn sm" data-act="saveName">Сохранить</button></div></div>
      <div class="field"><label>Аватар</label><div class="emoji-pick">${AVATARS.map(a => html`<button class="${u.avatar === a ? 'on' : ''}" data-act="setAvatar" data-v="${a}">${a}</button>`)}</div></div></div>
    <div class="card"><h3>🎨 Внешний вид</h3><div class="field"><label>Цвет интерфейса</label><div class="swatches">${COLORS.map(c => html`<div class="swatch ${u.theme_color.toLowerCase() === c ? 'on' : ''}" style="background:${c}" data-act="setColor" data-v="${c}"></div>`)}<input type="color" id="customcolor" value="${u.theme_color}" style="width:44px;height:44px;border:0;background:none;padding:0;cursor:pointer" title="Свой цвет"></div></div>
      <div class="field"><label>Тема</label><div class="tabs" style="max-width:340px">${[['dark', '🌙 Тёмная'], ['light', '☀️ Светлая'], ['auto', '🖥 Авто']].map(([k, n]) => html`<button class="${u.theme_mode === k ? 'on' : ''}" data-act="setMode" data-v="${k}">${n}</button>`)}</div></div>
      <div class="row spread"><span><b>Звуковые эффекты</b><div class="small muted">Сигналы за верные и неверные ответы</div></span><label class="switch"><input type="checkbox" id="snd" ${u.sound ? 'checked' : ''}><span></span></label></div></div>
    <div class="card"><h3>🔔 Напоминания</h3><div class="row spread"><span><b>Напоминать о занятиях</b><div class="small muted">Если сегодня ещё не занимался — уведомление в браузере и в Telegram</div></span><label class="switch"><input type="checkbox" id="remon" ${u.reminders_on ? 'checked' : ''}><span></span></label></div>
      <div class="row mt wrap"><div class="field" style="margin:0"><label>Время</label><input type="time" id="remtime" value="${u.reminder_time}" style="width:140px"></div><button class="btn sm" data-act="saveRem" style="align-self:flex-end">Сохранить</button>
      <button class="btn sm ghost" data-act="askNotif" style="align-self:flex-end">Разрешить уведомления в браузере</button></div>
      <p class="small muted mt" style="margin-bottom:0">Браузерные уведомления приходят, пока вкладка открыта (можно свёрнутой). Чтобы получать напоминания всегда — подключи Telegram.</p></div>
    <div class="card" id="tgcard"><h3>✈️ Telegram-бот</h3>${tgHtml(tg)}</div>
    ${set.is_admin ? html`<div class="card"><h3>🤖 Модель ИИ и ключи</h3>
      <div class="row wrap gap-s mb"><span class="chip ${set.llm.configured ? 'good' : 'warn'}">${set.llm.mock ? 'демо-режим (без ИИ)' : set.llm.configured ? 'ключ OpenRouter задан ' + (set.llm.key_hint || '') : 'ключ не задан'}</span>${set.llm.last_model ? html`<span class="chip">последняя модель: ${set.llm.last_model}</span>` : ''}</div>
      <div class="field"><label>Ключ OpenRouter (openrouter.ai/keys)</label><input type="password" id="orkey" placeholder="sk-or-v1-… (оставь пустым, чтобы не менять)" autocomplete="off"></div>
      <div class="field"><label>Модели по приоритету (необязательно, через запятую)</label><input type="text" id="models" value="${(set.llm.models || []).join(', ')}" placeholder="авто: бесплатные DeepSeek → Qwen"></div>
      <div class="field"><label>Токен Telegram-бота (от @BotFather)</label><input type="password" id="tgtoken" placeholder="${set.telegram.configured ? 'токен задан — оставь пустым, чтобы не менять' : '123456:ABC…'}" autocomplete="off"></div>
      <div class="row"><button class="btn" data-act="saveAdmin">Сохранить</button><button class="btn ghost" data-act="testLLM">Проверить модель</button></div><div id="llmres" class="small mt"></div></div>` : ''}
    <div class="row"><button class="btn ghost" data-act="logout">Выйти из аккаунта</button></div></div>`));
  const snd = $('#snd'); if (snd) snd.onchange = async () => { await saveMe({ sound: snd.checked }); };
  const cc = $('#customcolor'); if (cc) cc.oninput = () => applyTheme(cc.value, S.user.theme_mode), cc.onchange = () => saveMe({ theme_color: cc.value }, true);
  if (tg.configured && !tg.linked && S.tgPolling) every(async () => { const s = await api('/telegram/status'); if (s.linked) { S.tgPolling = false; toast('Telegram подключён!', { icon: '✈️' }); drawSettings(); refreshMe(); } }, 3000);
}
function tgHtml(tg) {
  if (!tg.configured) return html`<p class="muted">Токен бота не задан. ${S.user.is_admin ? 'Вставь токен от @BotFather в блоке ниже.' : 'Попроси администратора добавить токен в настройках.'}</p>`;
  if (tg.linked) return html`<p>✅ Аккаунт подключён${tg.bot ? html` к боту <b>@${tg.bot}</b>` : ''}. Можно учиться прямо в Telegram, а напоминания придут туда же.</p>${tg.bot ? html`<a class="btn sm" href="https://t.me/${tg.bot}" target="_blank" rel="noopener">Открыть бота</a>` : ''} <button class="btn sm ghost" data-act="tgUnlink">Отключить</button>`;
  return html`${tg.error ? html`<p class="err small">Бот: ${tg.error}</p>` : ''}<p class="muted">Учись в мессенджере: уроки, тесты, статистика и напоминания — прямо в Telegram.</p><button class="btn" data-act="tgLink">Подключить</button><div id="tglink"></div>`;
}
async function saveMe(patch, quiet) {
  S.user = await api('/me', { method: 'PUT', json: patch }); applyTheme(S.user.theme_color, S.user.theme_mode); refreshHud();
  if (!quiet) toast('Сохранено', { icon: '✅', ms: 1500 });
}
actions.saveName = () => saveMe({ display_name: $('#dname').value });
actions.setAvatar = async b => { await saveMe({ avatar: b.dataset.v }, true); drawSettings(); };
actions.setColor = async b => { await saveMe({ theme_color: b.dataset.v }, true); drawSettings(); };
actions.setMode = async b => { await saveMe({ theme_mode: b.dataset.v }, true); drawSettings(); };
actions.saveRem = () => saveMe({ reminders_on: $('#remon').checked, reminder_time: $('#remtime').value });
actions.askNotif = async () => {
  if (!('Notification' in window)) return toast('Браузер не поддерживает уведомления');
  const p = await Notification.requestPermission();
  if (p === 'granted') { new Notification('LearnQuest', { body: 'Уведомления включены. До встречи на уроке! 🦉' }); toast('Уведомления разрешены', { icon: '🔔' }); }
  else toast('Уведомления отклонены — разрешить можно в настройках браузера', { icon: '🔕', ms: 5000 });
};
actions.tgLink = async b => {
  try {
    const r = await api('/telegram/link', { method: 'POST' }); S.tgPolling = true;
    $('#tglink').innerHTML = `<div class="mt"><p>1. ${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">Открой бота @${esc(r.bot)}</a> и нажми «Start»` : 'Открой своего бота в Telegram'}.</p><p>2. Если бот не подхватил код сам, отправь ему:</p><div class="code-box">/start ${esc(r.code)}</div><p class="muted small mt">Жду подключения<span class="dots"></span></p></div>`;
    b.classList.add('hidden');
    every(async () => { const s = await api('/telegram/status'); if (s.linked) { S.tgPolling = false; toast('Telegram подключён!', { icon: '✈️' }); drawSettings(); refreshMe(); } }, 3000);
  } catch (e) { toast(e.message, { icon: '⚠️' }); }
};
actions.tgUnlink = async () => { await api('/telegram/unlink', { method: 'POST' }); drawSettings(); refreshMe(); };
actions.saveAdmin = async () => {
  const body = { llm_models: $('#models').value }; if ($('#orkey').value.trim()) body.openrouter_key = $('#orkey').value.trim(); if ($('#tgtoken').value.trim()) body.telegram_token = $('#tgtoken').value.trim();
  try { await api('/settings', { method: 'PUT', json: body }); toast('Настройки сохранены', { icon: '✅' }); drawSettings(); } catch (e) { toast(e.message, { icon: '⚠️' }); }
};
actions.testLLM = async b => {
  busy(b, true, 'Проверяю'); const r = await api('/settings/test', { method: 'POST' }); busy(b, false);
  $('#llmres').innerHTML = r.ok ? `<span style="color:var(--good)">✅ Работает. Модель: <b>${esc(r.model)}</b></span>` : `<span class="err">❌ ${esc(r.error)}</span>`;
};
