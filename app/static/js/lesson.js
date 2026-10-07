/* LearnQuest — плеер урока */
'use strict';

const LESSON_LABEL = { intro_test: '🧪 Входной тест', terms: '✍️ Термины', terms_test: '🔎 Проверка терминов', practice: '🛠 Практика', methods_test: '🧩 Тест на методы', final_test: '🏁 Контрольный тест' };
let P = null;

route(/^#\/run\/(\d+)$/, async rid => {
  let v = S.lessonCtx && String(S.lessonCtx.run_id) === rid ? S.lessonCtx : await api('/runs/' + rid);
  S.lessonCtx = null;
  P = { rid: +rid, v, sel: null, answered: false, last: null, xp: v.xp || 0, hint: 0, busy: false };
  const onKey = e => playerKey(e);
  document.addEventListener('keydown', onKey);
  S.onLeave = () => { document.removeEventListener('keydown', onKey); P = null; };
  if (v.finished || !v.step) return finishLesson();
  drawPlayer();
});

function drawPlayer() {
  const v = P.v;
  render(app(), html`<div class="player">
    <div class="p-top"><button class="icon-btn" data-act="exitLesson" title="Выйти">✕</button><div class="bar"><i id="pbar" style="width:${Math.round(v.done / v.total * 100)}%"></i></div><div class="p-xp" id="pxp">⭐ ${P.xp}</div></div>
    <div class="p-body" id="pbody"></div>
    <div class="p-foot" id="pfoot"><div class="p-foot-in" id="pfootin"></div></div></div>`);
  drawStep();
}

function setBar() { const b = $('#pbar'); if (b) b.style.width = Math.round(P.v.done / P.v.total * 100) + '%'; const x = $('#pxp'); if (x) x.textContent = '⭐ ' + P.xp; }
function footer(inner, cls = '') { const f = $('#pfoot'); f.className = 'p-foot ' + cls; $('#pfootin').innerHTML = unraw(inner); }
function canSubmit() {
  const k = P.v.step.kind;
  if (k === 'read') return true;
  if (k === 'single' || k === 'image') return P.sel !== null;
  if (k === 'multi') return P.sel && P.sel.length > 0;
  const t = $('#ans'); return !!(t && t.value.trim().length);
}
function refreshBtn() { const b = $('#checkbtn'); if (b) b.disabled = !canSubmit(); }

function drawStep() {
  const v = P.v, s = v.step, k = s.kind; P.sel = (k === 'multi') ? [] : null; P.answered = false; P.hint = 0;
  const label = LESSON_LABEL[v.lesson.type] || '';
  let body = '';
  if (k === 'read') {
    body = html`<div class="p-kind">${label} · ${s.title}</div><div class="card term-card"><div class="term">${s.term}</div><div class="def">${s.definition}</div>${s.example ? html`<div class="ex">💡 ${s.example}</div>` : ''}</div>`;
  } else if (k === 'single' || k === 'multi' || k === 'image') {
    body = html`<div class="p-kind">${label} · ${s.title}${s.retry ? ' · повтор' : ''}</div>${s.svg ? html`<div class="img-box"><img alt="Иллюстрация к вопросу" src="${svgDataUrl(s.svg)}"></div>` : ''}<div class="q">${s.prompt}</div>
      <div class="opts" id="opts">${s.options.map((o, i) => html`<button class="opt" data-act="pick" data-i="${i}"><span class="k">${'ABCDEFGH'[i]}</span><span>${o}</span></button>`)}</div>`;
  } else if (k === 'fill') {
    body = html`<div class="p-kind">${label} · ${s.title}${s.retry ? ' · повтор' : ''}</div><div class="q fill-in">${s.prompt}</div><div class="field"><input type="text" id="ans" autocomplete="off" autofocus placeholder="${s.placeholder}"></div>`;
  } else if (k === 'write') {
    body = s.stage === 'tail'
      ? html`<div class="p-kind">${label} · ${s.title}${s.retry ? ' · повтор' : ''}</div><div class="head-text"><b>${s.term}</b> — ${s.head} <span class="gap">…</span></div><div class="field"><textarea id="ans" placeholder="${s.placeholder}" autofocus></textarea></div>`
      : html`<div class="p-kind">${label} · ${s.title}${s.retry ? ' · повтор' : ''}</div><div class="card term-card" style="padding:18px"><div class="small muted">Что это такое?</div><div class="term" style="margin:6px 0 0">${s.term}</div></div><div class="field mt"><textarea id="ans" placeholder="${s.placeholder}" autofocus></textarea></div>`;
  } else if (k === 'task') {
    body = html`<div class="p-kind">${label} · ${s.title}</div><div class="card"><div class="statement">${s.statement}</div>${s.starter ? html`<pre class="code">${s.starter}</pre>` : ''}<div id="hintbox" class="hints"></div></div>
      <div class="field mt"><label>Твоё решение${s.language ? ' (' + s.language + ')' : ''}</label><textarea id="ans" class="${s.task_kind === 'code' ? 'code' : ''}" placeholder="${s.placeholder}">${s.task_kind === 'code' && s.starter ? s.starter : ''}</textarea></div>`;
  }
  $('#pbody').innerHTML = unraw(body);
  const ans = $('#ans');
  if (ans) { ans.addEventListener('input', refreshBtn); setTimeout(() => ans.focus(), 50); if (k === 'task' && ans.value) ans.setSelectionRange(0, 0); }
  let left = '';
  if (k === 'write') left = html`<button class="btn ghost" data-act="dontKnow">Не помню</button>`;
  if (k === 'task') left = html`<button class="btn ghost" data-act="taskHint">💡 Подсказка</button><button class="btn ghost" data-act="taskSkip">Пропустить</button>`;
  footer(html`<div class="row grow">${left}</div><button class="btn lg" id="checkbtn" data-act="check" ${canSubmit() ? '' : 'disabled'}>${k === 'read' ? 'Запомнил' : k === 'task' ? 'Отправить на проверку' : 'Проверить'}</button>`);
}

actions.pick = b => {
  if (P.answered) return;
  const i = +b.dataset.i; play('tap');
  if (P.v.step.kind === 'multi') { const at = P.sel.indexOf(i); at >= 0 ? P.sel.splice(at, 1) : P.sel.push(i); b.classList.toggle('sel', at < 0); }
  else { P.sel = i; $$('.opt').forEach(o => o.classList.toggle('sel', o === b)); }
  refreshBtn();
};
actions.taskHint = () => {
  const hs = P.v.step.hints || []; if (!hs.length) return toast('Подсказок нет — действуй по условию!', { icon: '🙂' });
  if (P.hint < hs.length) { $('#hintbox').insertAdjacentHTML('beforeend', `<div class="hint">💡 ${esc(hs[P.hint])}</div>`); P.hint++; } else toast('Это все подсказки');
};
actions.dontKnow = () => { $('#ans').value = ''; submitAnswer({ text: '' }); };
actions.taskSkip = () => { if (confirm('Пропустить задание? Покажу разбор, но опыт не начислю.')) submitAnswer({ skip: true }); };
actions.check = () => {
  if (P.answered) return;
  const s = P.v.step, k = s.kind;
  if (k === 'read') return submitAnswer({});
  if (!canSubmit()) return;
  if (k === 'single' || k === 'image' || k === 'multi') return submitAnswer({ choice: P.sel });
  return submitAnswer({ text: $('#ans').value });
};

async function submitAnswer(payload) {
  if (P.busy) return; P.busy = true;
  const btn = $('#checkbtn'), s = P.v.step;
  if (btn) busy(btn, true, s.kind === 'task' ? 'ИИ проверяет…' : '');
  let res;
  try { res = await api(`/runs/${P.rid}/answer`, { json: { ...payload, idx: s.idx } }); }
  catch (e) { P.busy = false; if (btn) busy(btn, false); refreshBtn(); return toast(e.message, { icon: '⚠️' }); }
  P.busy = false; P.answered = true;
  if (res.self_check) return selfCheck(res);
  P.v = { ...res.next, lesson: P.v.lesson }; P.last = res; P.xp += res.xp || 0;
  if (s.kind === 'read') { setBar(); return afterFeedback(res); }
  showFeedback(res, s);
}

function selfCheck(res) {
  $('#pbody').insertAdjacentHTML('beforeend', `<div class="card mt"><b>${esc(res.feedback)}</b><pre class="code">${esc(res.correct_answer)}</pre></div>`);
  footer(html`<div class="fb"><h3>Оцени себя</h3></div><button class="btn bad sm" data-act="selfScore" data-v="0">Не решил</button><button class="btn sm" style="background:var(--warn);box-shadow:0 4px 0 #b88a00;color:#2b2000" data-act="selfScore" data-v="0.5">Частично</button><button class="btn good sm" data-act="selfScore" data-v="1">Решил</button>`);
  P.answered = false; P.waitSelf = true;
}
actions.selfScore = async b => {
  const res = await api(`/runs/${P.rid}/answer`, { json: { self: +b.dataset.v } });
  P.v = { ...res.next, lesson: P.v.lesson }; P.last = res; P.xp += res.xp || 0; P.answered = true; P.waitSelf = false;
  showFeedback(res, { kind: 'task' });
};

function showFeedback(res, s) {
  const k = s.kind, ok = res.correct, half = res.partial;
  const cls = ok ? 'ok' : half ? 'half' : 'no';
  play(ok ? 'ok' : half ? 'half' : 'bad');
  if (!ok && !half) { const b = $('#pbody'); b.classList.remove('shake'); void b.offsetWidth; b.classList.add('shake'); }
  setBar();
  if (res.xp) xpPop('+' + res.xp + ' XP', $('#checkbtn') || $('#pfoot'));
  const head = ok ? pick(['Верно!', 'Отлично!', 'Супер!', 'Точно!']) : half ? 'Почти!' : pick(['Неверно', 'Не совсем', 'Ошибочка']);
  const icon = ok ? '🎉' : half ? '🤏' : '💥';
  let detail = '', bodyExtra = '';
  if (k === 'single' || k === 'multi' || k === 'image') {
    const chosen = new Set(Array.isArray(P.sel) ? P.sel : [P.sel]), right = new Set(res.correct_idx || []);
    $$('.opt').forEach((o, i) => { o.classList.remove('sel'); if (right.has(i)) o.classList.add('right'); else if (chosen.has(i)) o.classList.add('wrong'); });
    $('#opts').classList.add('locked');
    if (!ok) detail = html`<div class="ans">Правильный ответ: ${res.correct_answer}</div>`;
    if (res.explanation) detail = html`${detail}<div class="expl">💬 ${res.explanation}</div>`;
  } else if (k === 'fill') {
    if (!ok) detail = html`<div class="ans">Правильно: ${res.correct_answer}</div>`;
    if (res.explanation) detail = html`${detail}<div class="expl">💬 ${res.explanation}</div>`;
    const a = $('#ans'); if (a) a.disabled = true;
  } else if (k === 'write') {
    const a = $('#ans'); if (a) a.disabled = true;
    bodyExtra = html`<div class="card mt flat"><div class="small muted">${P.v && res.full ? 'Полное определение' : 'Эталон'}</div><div style="font-weight:800;font-size:17px">${res.full || res.correct_answer}</div>${res.explanation ? html`<div class="small muted mt">💡 ${res.explanation}</div>` : ''}</div>`;
    if (!ok) detail = html`<div class="ans small">Сверься с эталоном выше</div>`;
  } else if (k === 'task') {
    const a = $('#ans'); if (a) a.disabled = true;
    bodyExtra = html`<div class="card mt flat"><b>Разбор</b><p style="margin:6px 0">${res.feedback}</p>${(res.missing || []).length ? html`<ul class="small muted" style="margin:0 0 8px 18px;padding:0">${res.missing.map(m => html`<li>${m}</li>`)}</ul>` : ''}<details ${ok ? '' : 'open'}><summary class="small muted" style="cursor:pointer">Эталонное решение</summary><pre class="code">${res.correct_answer}</pre></details></div>`;
  }
  if (bodyExtra) $('#pbody').insertAdjacentHTML('beforeend', unraw(bodyExtra));
  if (res.requeued) detail = html`${detail}<div class="expl">🔁 Вернёмся к этому в конце урока</div>`;
  const over = res.can_override ? html`<button class="btn ghost sm" data-act="override">Я был прав ✅</button>` : '';
  footer(html`<div class="fb"><h3>${icon} ${head}${res.xp ? html` <span class="small">+${res.xp} XP</span>` : ''}</h3>${detail}</div>${over}<button class="btn lg ${ok ? 'good' : half ? '' : 'bad'}" id="nextbtn" data-act="next">${res.finished ? 'Завершить' : 'Дальше'}</button>`, cls);
  const nb = $('#nextbtn'); if (nb) nb.focus();
}
function pick(a) { return a[(Math.random() * a.length) | 0]; }

actions.override = async b => {
  try { const r = await api(`/runs/${P.rid}/override`, { method: 'POST' }); P.xp += r.xp; setBar(); b.remove(); P.v.total = r.total; toast('Засчитано — доверяю!', { icon: '🤝', ms: 1800 }); play('ok');
    const f = $('#pfoot'); f.className = 'p-foot ok'; const h = $('#pfootin h3'); if (h) h.innerHTML = '🎉 Засчитано'; if (r.finished && $('#nextbtn')) $('#nextbtn').textContent = 'Завершить';
    P.last.finished = r.finished; P.v.finished = false; refreshProgress(r.total);
  } catch (e) { toast(e.message); }
};
function refreshProgress(total) { P.v.total = total; setBar(); }

actions.next = () => afterFeedback(P.last);
function afterFeedback(res) {
  if (res.finished || !P.v.step) return finishLesson();
  drawStep();
}
actions.exitLesson = () => {
  if (!confirm('Выйти из урока? Прогресс сохранится — продолжишь с этого места.')) return;
  history.length > 1 ? history.back() : (location.hash = '#/');
};

function playerKey(e) {
  if (!P || !$('#pbody')) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (e.key === 'Enter') {
    if (tag === 'textarea' && !(e.ctrlKey || e.metaKey)) return;
    if (tag === 'button' && !P.answered) return;
    e.preventDefault();
    if (P.answered) actions.next(); else if (!$('#checkbtn')?.disabled) actions.check();
    return;
  }
  if (tag === 'input' || tag === 'textarea' || P.answered) return;
  const k = P.v.step && P.v.step.kind;
  if (k === 'single' || k === 'multi' || k === 'image') {
    const i = '1234567'.indexOf(e.key) >= 0 && e.key ? +e.key - 1 : 'abcdefg'.indexOf(e.key.toLowerCase());
    const o = $$('.opt')[i]; if (o && i >= 0) actions.pick(o);
  }
}

/* ---------- итоги ---------- */
async function finishLesson() {
  render(app(), html`<div class="loader-screen"><div><span class="spinner"></span><p class="muted mt">Подсчитываю результаты…</p></div></div>`);
  let r;
  try { r = await api(`/runs/${P ? P.rid : location.hash.split('/').pop()}/finish`, { method: 'POST' }); }
  catch (e) { toast(e.message); location.hash = '#/'; return; }
  const s = r.summary, c = r.course, pct = Math.round(s.score * 100);
  await refreshMe();
  const emoji = pct >= 90 ? '🏆' : pct >= 70 ? '🎉' : pct >= 50 ? '💪' : '🌱';
  const msg = pct >= 90 ? 'Блестяще!' : pct >= 70 ? 'Хороший результат!' : pct >= 50 ? 'Неплохо, есть куда расти' : 'Ничего страшного — повторение закрепит материал';
  const isFinal = s.lesson_type === 'final_test';
  const needMore = isFinal && s.score < 0.6;
  render(app(), html`<div class="result" style="max-width:640px;margin:0 auto;padding:18px">
      <div class="big-emoji">${emoji}</div><h1>${s.replay ? 'Повторение завершено' : 'Урок пройден!'}</h1><p class="muted">${s.lesson_title} · ${msg}</p>
      <div class="stat-row"><div class="card"><b>${pct}%</b><span class="muted small">результат</span></div><div class="card"><b>+${s.xp_total_gained}</b><span class="muted small">XP</span></div><div class="card"><b>${s.correct}/${s.answers}</b><span class="muted small">верных</span></div></div>
      ${s.streak_bonus ? html`<p class="small">🔥 Бонус за серию: +${s.streak_bonus} XP</p>` : ''}
      <div class="card mb" style="text-align:left"><div class="row spread"><b>Уровень ${s.level.level}${s.level_up ? ' 🚀 новый!' : ''}</b><span class="small muted">${s.level.into}/${s.level.span} XP</span></div><div class="bar mt"><i id="lvlbar" style="width:0"></i></div>
        <div class="row spread mt small"><span>🔥 Серия: <b>${s.streak.current}</b> ${plural(s.streak.current, 'день', 'дня', 'дней')}</span><span>⭐ За курс: <b>${c.xp}</b> XP · ${c.progress.percent}%</span></div></div>
      ${needMore ? html`<div class="note mb">Контрольный тест пока не сдан: я подготовил короткую работу над ошибками и повторный тест. Это нормально — так материал закрепляется.</div>` : ''}
      ${isFinal && !needMore ? html`<div class="note mb">🎊 Модуль закрыт! Анализирую твои результаты и готовлю следующий — уроки подстроятся под то, как ты справился.</div>` : ''}
      <div class="row" style="justify-content:center"><a class="btn lg" href="#/learn/${c.id}">${c.status === 'completed' ? 'К итогам' : 'Дальше'}</a><a class="btn ghost" href="#/course/${c.id}">К курсу</a></div></div>`);
  setTimeout(() => { const b = $('#lvlbar'); if (b) b.style.width = s.level.pct + '%'; }, 80);
  if (pct >= 70 || s.level_up) { confetti(pct >= 90 ? 200 : 120); }
  play(s.level_up ? 'level' : 'done');
  (s.achievements || []).forEach((a, i) => setTimeout(() => { toast(a.desc, { icon: a.icon, title: 'Достижение: ' + a.title, cls: 'ach-t', ms: 6000 }); play('level'); confetti(60); }, 900 + i * 900));
}
