/* ============================================================
   panels.js — 좌측 파라미터 / 우측 성과 / 하단 탭 렌더링
   ------------------------------------------------------------
   DOM 조작만 담당한다. fetch 는 api.js, 상태 관리는 main.js.
   ============================================================ */

const $ = (id) => document.getElementById(id);
const nf = new Intl.NumberFormat('ko-KR');
export const fmt = (n) => (Number.isFinite(+n) ? nf.format(Math.round(+n)) : '—');
export const fmt1 = (n) => (Number.isFinite(+n) ? (+n).toFixed(1) : '—');
export const pct = (n) => (Number.isFinite(+n) ? ((+n >= 0 ? '+' : '') + (+n).toFixed(2) + '%') : '—');
export const pct1 = (n) => (Number.isFinite(+n) ? ((+n >= 0 ? '+' : '') + (+n).toFixed(1) + '%') : '—');
/** 원 단위 금액을 한국식 단위로. 1억 이상이면 억, 아니면 만. */
export const krw = (won) => {
  if (!Number.isFinite(+won)) return '—';
  const v = +won;
  if (Math.abs(v) >= 1e8) {
    const e = v / 1e8;
    return (Math.abs(e) >= 100 ? fmt(e) : e.toFixed(2).replace(/\.?0+$/, '')) + '억';
  }
  return fmt(v / 1e4) + '만';
};
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ============================================================
   1. 경로 기반 값 접근 — "entries[0].size_pct" 같은 문자열
   ============================================================ */

function tokens(path) {
  return String(path).replace(/\[(\d+)\]/g, '.$1').split('.').filter(Boolean);
}

export function getPath(obj, path) {
  let cur = obj;
  for (const k of tokens(path)) {
    if (cur === null || cur === undefined) return undefined;
    cur = cur[k];
  }
  return cur;
}

export function setPath(obj, path, value) {
  const ks = tokens(path);
  let cur = obj;
  for (let i = 0; i < ks.length - 1; i++) {
    const k = ks[i];
    if (cur[k] === null || typeof cur[k] !== 'object') {
      cur[k] = /^\d+$/.test(ks[i + 1]) ? [] : {};
    }
    cur = cur[k];
  }
  cur[ks[ks.length - 1]] = value;
  return obj;
}

/* ============================================================
   2. 좌측 — 전략 목록
   ============================================================ */

export function renderStrategyList(list, selectedId, onSelect) {
  const host = $('stgList');
  if (!list || !list.length) {
    host.innerHTML = '<div class="hint" style="padding:8px 2px">등록된 전략이 없습니다. 오른쪽 위 + 로 새 전략을 만드세요.</div>';
    return;
  }
  host.innerHTML = list.map((s, i) => {
    const sel = s.id === selectedId;
    const tag = s.enabled === false
      ? '<span class="tag off">비활성</span>'
      : (s.source === 'ai' ? '<span class="tag ai">AI</span>' : '<span class="tag live">등록</span>');
    return `<button type="button" class="stg" role="option" data-id="${esc(s.id)}" aria-selected="${sel}">
      <span class="stg-ic">S${i + 1}</span>
      <span class="stg-tx">
        <span class="stg-nm">${esc(s.name || s.id)} ${tag}</span>
        <span class="stg-ds">${esc(s.description || '설명 없음')}</span>
      </span>
    </button>`;
  }).join('');

  host.querySelectorAll('.stg').forEach((b) => {
    b.addEventListener('click', () => onSelect(b.dataset.id));
  });
}

/* ============================================================
   3. 좌측 — 파라미터 폼
   ============================================================ */

const DEBOUNCE_MS = 300;   // ARCHITECTURE 5-3

/**
 * 전략 객체를 폼에 채운다.
 * period.end 가 "auto" 면 날짜 입력은 비워 두고 placeholder 로 안내한다.
 */
export function fillForm(strategy) {
  document.querySelectorAll('#paramForm [data-path]').forEach((el) => {
    const raw = getPath(strategy, el.dataset.path);
    if (el.dataset.kind === 'markets') {
      el.value = Array.isArray(raw) ? raw.join(',') : (raw || 'KOSPI,KOSDAQ');
      if (!Array.from(el.options).some((o) => o.value === el.value)) el.value = 'KOSPI,KOSDAQ';
      return;
    }
    if (el.dataset.kind === 'autodate') {
      el.value = (raw && raw !== 'auto') ? String(raw) : '';
      el.placeholder = 'auto (최신 거래일)';
      return;
    }
    if (raw === undefined || raw === null) { el.value = ''; return; }
    el.value = String(raw);
  });
  clearFieldErrors();
}

/** 폼 값을 전략 객체에 반영한 새 객체를 만든다 (원본 불변) */
export function readForm(strategy) {
  const next = structuredClone(strategy);
  document.querySelectorAll('#paramForm [data-path]').forEach((el) => {
    const p = el.dataset.path;
    if (el.dataset.kind === 'markets') { setPath(next, p, el.value.split(',').filter(Boolean)); return; }
    if (el.dataset.kind === 'autodate') { setPath(next, p, el.value ? el.value : 'auto'); return; }
    if (el.type === 'number') {
      const v = el.value === '' ? null : Number(el.value);
      setPath(next, p, Number.isFinite(v) ? v : null);
      return;
    }
    setPath(next, p, el.value);
  });
  syncDerived(next);
  return next;
}

/**
 * 폼에 직접 대응하지 않지만 값이 연동되어야 하는 필드를 맞춘다.
 * (DSL 은 같은 숫자를 조건식과 파라미터 양쪽에 들고 있다)
 */
function syncDerived(s) {
  // market.trade_resolution 은 execution.resolution 과 항상 같다
  const res = getPath(s, 'execution.resolution');
  if (res) setPath(s, 'market.trade_resolution', res);
  // 손절 이동평균 기간은 조건식/체결가 양쪽 지표에 반영
  const ma = getPath(s, 'exits[1].ma_period');
  if (Number.isFinite(ma)) {
    for (const p of ['exits[1].when.right', 'exits[1].price']) {
      const node = getPath(s, p);
      if (node && typeof node === 'object' && node.indicator) node.period = ma;
    }
  }
  return s;
}

/** 폼 변경 → 300ms 디바운스 → 콜백 */
export function bindForm(onChange) {
  let timer = 0;
  const handler = () => {
    clearTimeout(timer);
    timer = setTimeout(onChange, DEBOUNCE_MS);
  };
  document.querySelectorAll('#paramForm [data-path]').forEach((el) => {
    el.addEventListener('input', handler);
    el.addEventListener('change', handler);
  });
}

/** 422 응답의 errors 를 해당 필드 옆에 표시 */
export function showFieldErrors(errors) {
  clearFieldErrors();
  const rest = [];
  for (const e of errors || []) {
    const path = String(e.path || '').replace(/^\$?\.?/, '');
    const el = document.querySelector(`#paramForm [data-path="${CSS.escape(path)}"]`);
    if (!el) { rest.push(e); continue; }
    el.setAttribute('aria-invalid', 'true');
    const msg = document.createElement('span');
    msg.className = 'fld-err';
    msg.dataset.err = '1';
    msg.textContent = e.message || '값이 올바르지 않습니다.';
    el.closest('.fld').after(msg);
    el.setAttribute('aria-describedby', (el.id || '') + '-err');
    msg.id = (el.id || '') + '-err';
  }
  return rest;   // 폼에 매핑되지 않은 오류는 호출부가 배너로 띄운다
}

export function clearFieldErrors() {
  document.querySelectorAll('#paramForm [aria-invalid]').forEach((el) => {
    el.removeAttribute('aria-invalid');
    el.removeAttribute('aria-describedby');
  });
  document.querySelectorAll('#paramForm [data-err]').forEach((el) => el.remove());
}

/* ============================================================
   4. 좌측 — 지표 칩 (/api/indicators 응답으로만 렌더. 하드코딩 금지)
   ============================================================ */

/**
 * 지표 인스턴스 → 차트 API 키.
 * ARCHITECTURE 4-6 의 `indicators=SMA:20,SMA:45` 형식을 정확히 맞춘다.
 * source / anchor 같은 문자열 파라미터는 키에 넣지 않는다 (숫자 파라미터만).
 */
const NUMERIC = new Set(['int', 'float', 'number']);
export function indKey(spec, params) {
  const ps = (spec.params || [])
    .filter((p) => NUMERIC.has(p.type) || (p.type === undefined && typeof params[p.name] === 'number'))
    .map((p) => params[p.name])
    .filter((v) => v !== undefined && v !== null);
  return ps.length ? `${spec.key}:${ps.join(',')}` : spec.key;
}

/** 지표 인스턴스 → 화면 라벨 */
export function indLabel(spec, params) {
  const ps = (spec.params || [])
    .filter((p) => NUMERIC.has(p.type) || (p.type === undefined && typeof params[p.name] === 'number'))
    .map((p) => params[p.name])
    .filter((v) => v !== undefined && v !== null);
  return ps.length ? `${spec.label || spec.key} ${ps.join('/')}` : (spec.label || spec.key);
}

/**
 * @param {Array} specs      /api/indicators 응답
 * @param {Array} active     [{key, type, params, label, overlay, slot}]
 * @param {object} handlers  {onToggleActive, onAddDefault, onAdd}
 * @param {function} slotColor  slot → CSS 색 (테마 반영)
 */
export function renderChips(specs, active, handlers, slotColor) {
  const host = $('indChips');
  const activeKeys = new Set(active.map((a) => a.key));
  const parts = [];

  // 1) 현재 켜져 있는 지표
  for (const a of active) {
    parts.push(`<button type="button" class="chip" aria-pressed="true" data-key="${esc(a.key)}" title="${esc(a.key)} — 클릭하면 끕니다">
      <span class="swatch" style="background:${esc(slotColor(a.slot))}"></span>${esc(a.label)}</button>`);
  }
  // 2) 아직 안 쓴 지표를 기본 파라미터로 제안
  for (const spec of specs) {
    const params = {};
    for (const p of spec.params || []) params[p.name] = p.default;
    const key = indKey(spec, params);
    if (activeKeys.has(key)) continue;
    parts.push(`<button type="button" class="chip" aria-pressed="false" data-spec="${esc(spec.key)}" title="${esc(spec.label || spec.key)} 추가 (기본 파라미터)">
      <span class="sw-dot"></span>${esc(indLabel(spec, params))}</button>`);
  }
  // 3) 파라미터 직접 입력
  parts.push(`<button type="button" class="chip add" id="chipAdd" aria-pressed="false" title="파라미터를 직접 지정해 지표를 추가합니다">
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>지표 추가</button>`);

  host.innerHTML = parts.join('');

  host.querySelectorAll('.chip').forEach((c) => {
    c.addEventListener('click', () => {
      if (c.id === 'chipAdd') { handlers.onAdd(); return; }
      if (c.dataset.key) { handlers.onToggleActive(c.dataset.key); return; }
      if (c.dataset.spec) { handlers.onAddDefault(c.dataset.spec); }
    });
  });
}

/* ============================================================
   5. 우측 — KPI / 종목별 기여도
   ============================================================ */

export function renderKpis(m) {
  if (!m) {
    for (const id of ['kRet', 'kCagr', 'kMdd', 'kSharpe', 'kWin', 'kPf']) {
      const el = $(id); el.textContent = '—'; el.className = 'v';
    }
    for (const id of ['kRetSub', 'kCagrSub', 'kMddSub', 'kWinSub', 'kPfSub']) $(id).textContent = '—';
    $('kPeriod').textContent = '백테스트 미실행';
    $('kWinBar').style.width = '0'; $('kLossBar').style.width = '0';
    return;
  }
  const set = (id, text, sign) => {
    const el = $(id);
    el.textContent = text;
    el.className = 'v' + (sign === undefined ? '' : (sign >= 0 ? ' pos' : ' neg'));
  };

  set('kRet', pct1(m.total_return_pct), m.total_return_pct);
  set('kCagr', pct1(m.cagr_pct), m.cagr_pct);
  set('kMdd', fmt1(m.mdd_pct) + '%', m.mdd_pct);
  set('kSharpe', Number.isFinite(m.sharpe) ? (+m.sharpe).toFixed(2) : '—');
  set('kWin', Number.isFinite(m.win_rate_pct) ? (+m.win_rate_pct).toFixed(1) + '%' : '—');
  set('kPf', Number.isFinite(m.profit_factor) ? (+m.profit_factor).toFixed(2) : '—');

  $('kRetSub').textContent = `${krw(m.initial_capital)} → ${krw(m.final_capital)}`;
  $('kCagrSub').textContent = m.period && m.period.years ? `${(+m.period.years).toFixed(2)}년` : '—';
  $('kMddSub').textContent = '최대 낙폭';
  $('kPfSub').textContent = `평균 ${pct1(m.avg_win_pct)} / ${pct1(m.avg_loss_pct)}`;

  const w = +m.wins || 0, l = +m.losses || 0, tot = w + l || 1;
  $('kWinBar').style.width = (w / tot * 100).toFixed(1) + '%';
  $('kLossBar').style.width = (l / tot * 100).toFixed(1) + '%';
  $('kWinSub').textContent = `${w}승 ${l}패 / ${m.trades ?? tot}`;

  $('kPeriod').textContent = m.period ? `${m.period.start} ~ ${m.period.end}` : '—';
}

export function renderByStock(rows) {
  const tb = $('byStock');
  if (!rows || !rows.length) {
    tb.innerHTML = '<tr class="empty"><td colspan="4">백테스트를 실행하면 종목별 기여도가 표시됩니다.</td></tr>';
    return;
  }
  tb.innerHTML = rows.slice().sort((a, b) => b.pnl - a.pnl).slice(0, 12).map((r) => `
    <tr data-code="${esc(r.code)}">
      <td class="l">${esc(r.name || r.code)}</td>
      <td>${r.trades}</td>
      <td>${Number.isFinite(r.win_rate) ? Math.round(r.win_rate) + '%' : '—'}</td>
      <td class="${r.pnl >= 0 ? 'pos' : 'neg'}">${r.pnl >= 0 ? '+' : ''}${fmt(r.pnl)}</td>
    </tr>`).join('');
  return tb;
}

/* ============================================================
   6. 하단 — 거래 내역
   ============================================================ */

const EXIT_LABEL = {
  TP: ['익절', 's'], SL: ['손절', 'x'], TIME: ['시간청산', 'x'],
  TRAIL: ['트레일링', 'x'], END: ['기간종료', 'x'],
};

/** 매수/매도는 색 외에 삼각형 + 라벨을 함께 쓴다 (색맹 대응) */
const TRI_UP = '<svg width="7" height="7" viewBox="0 0 10 10" fill="currentColor" aria-hidden="true"><path d="M5 1 9 8H1z"/></svg>';
const TRI_DN = '<svg width="7" height="7" viewBox="0 0 10 10" fill="currentColor" aria-hidden="true"><path d="M5 9 1 2h8z"/></svg>';

export function renderTrades(trades, onRowClick) {
  const tb = $('tradeBody');
  $('tradeCnt').textContent = trades ? trades.length : 0;
  if (!trades || !trades.length) {
    tb.innerHTML = '<tr class="empty"><td colspan="15">백테스트를 실행하면 거래 내역이 표시됩니다.</td></tr>';
    return;
  }
  const fillOf = (t, rule, idx) =>
    (t.fills || []).find((f) => f.rule === rule) || (t.fills || [])[idx] || null;

  tb.innerHTML = trades.map((t) => {
    const b1 = fillOf(t, 'B1', 0);
    const b2 = fillOf(t, 'B2', 1);
    const [exLabel, exCls] = EXIT_LABEL[t.exit_rule] || [t.exit_rule || '청산', 'x'];
    const win = (+t.return_pct) >= 0;
    return `<tr data-no="${t.no}" data-code="${esc(t.code)}" tabindex="0">
      <td class="l">${t.no}</td>
      <td class="l"><b>${esc(t.name || '')}</b> <span class="dim" style="font-family:var(--mono)">${esc(t.code)}</span></td>
      <td class="l">${esc(t.ref_date || '—')}</td>
      <td>${Number.isFinite(t.ref_amount_eok) ? fmt(t.ref_amount_eok) + '억' : '—'}</td>
      <td class="l">${b1 ? `<span class="mk b">${TRI_UP}B1</span> ${esc(b1.date)}` : '—'}</td>
      <td>${b1 ? fmt(b1.price) : '—'}</td>
      <td class="l">${b2 ? `<span class="mk b">${TRI_UP}B2</span> ${esc(b2.date)}` : '<span class="dim">—</span>'}</td>
      <td>${b2 ? fmt(b2.price) : '—'}</td>
      <td>${fmt(t.avg_price)}</td>
      <td class="l"><span class="mk ${exCls}">${TRI_DN}${esc(exLabel)}</span> ${esc(t.exit_date || '—')}</td>
      <td>${fmt(t.exit_price)}</td>
      <td class="l sub">${esc(t.exit_reason || '')}</td>
      <td>${t.hold_days ?? '—'}일</td>
      <td class="${win ? 'pos' : 'neg'}"><b>${pct(t.return_pct)}</b></td>
      <td class="${win ? 'pos' : 'neg'}">${(+t.pnl) >= 0 ? '+' : ''}${fmt(t.pnl)}</td>
    </tr>`;
  }).join('');

  tb.querySelectorAll('tr').forEach((tr) => {
    const go = () => {
      tb.querySelectorAll('tr').forEach((x) => x.classList.remove('sel'));
      tr.classList.add('sel');
      onRowClick(+tr.dataset.no);
    };
    tr.addEventListener('click', go);
    tr.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });
}

export function selectTradeRow(no) {
  const tb = $('tradeBody');
  tb.querySelectorAll('tr').forEach((x) => x.classList.remove('sel'));
  const tr = tb.querySelector(`tr[data-no="${no}"]`);
  if (tr) { tr.classList.add('sel'); tr.scrollIntoView({ block: 'nearest' }); }
}

/* ============================================================
   7. 하단 — 시그널 로그
   ============================================================ */

export function renderSignals(signals) {
  const host = $('sigLog');
  if (!signals || !signals.length) {
    host.innerHTML = '<span class="c">백테스트를 실행하면 스캔 · 후보 등록 · 체결 로그가 이곳에 출력됩니다.</span>';
    return;
  }
  host.innerHTML = signals.map((s) => {
    const lv = String(s.level || 'INFO').toUpperCase();
    const cls = ['MATCH', 'FILL', 'WARN', 'ERROR'].includes(lv) ? ` lv-${lv}` : '';
    return `<span class="c">${esc(s.ts || '')}</span>  <span class="k${cls}">[${esc(lv.padEnd(5))}]</span>  ` +
      (s.code ? `<span class="n">${esc(s.code)}</span> ` : '') + esc(s.message || '');
  }).join('\n');
}

/* ============================================================
   8. 하단 — 전략 JSON
   ============================================================ */

export function renderJson(obj) {
  const raw = JSON.stringify(obj, null, 2);
  const html = esc(raw)
    .replace(/&quot;([^&]*?)&quot;(\s*):/g, '<span class="k">&quot;$1&quot;</span>$2:')
    .replace(/:(\s*)&quot;([^&]*?)&quot;/g, ':$1<span class="s">&quot;$2&quot;</span>')
    .replace(/:(\s*)(-?\d+(?:\.\d+)?)/g, ':$1<span class="n">$2</span>')
    .replace(/:(\s*)(true|false|null)/g, ':$1<span class="n">$2</span>');
  $('jsonView').innerHTML = html;
}

/* ============================================================
   9. 하단 — 데이터 상태
   ============================================================ */

export function renderDataStatus(st, note) {
  const rows = [];
  const row = (k, v, n) => rows.push(
    `<tr class="empty"><td class="l">${esc(k)}</td><td class="l">${v}</td><td class="l sub">${esc(n || '')}</td></tr>`);

  if (!st) {
    $('dataBody').innerHTML = '<tr class="empty"><td colspan="3">데이터 상태를 불러오지 못했습니다.</td></tr>';
    return;
  }
  const ok = st.available !== false;
  row('데이터 사용 가능', ok
    ? '<span class="mk b">예</span>'
    : '<span class="mk x">아니오</span>',
    ok ? '' : 'update_marcap.bat 을 실행해 marcap 저장소를 내려받으세요');
  row('저장소', '<code>./marcap</code>', 'github.com/FinanceData/marcap');
  row('마지막 갱신', esc(st.last_sync || '—'), st.result ? `결과: ${esc(st.result)}` : '');
  row('최신 거래일', esc(st.latest_trade_date || '—'), st.git_rev ? `git ${esc(st.git_rev)}` : '');
  row('커버리지', `${esc(st.first_trade_date || '—')} ~ ${esc(st.latest_trade_date || '—')}`,
    `${st.file_count ?? '—'}개 연도 파일 · ${st.row_count ? fmt(st.row_count) + '행' : '—'}`);
  const auto = st.auto_sync || {};
  row('자동 갱신', auto.registered
    ? `<span class="mk b">등록됨</span> 매일 ${esc(auto.time || '—')}`
    : '<span class="mk x">미등록</span>',
    auto.task ? `작업 스케줄러 ${esc(auto.task)}` : 'setup_daily_update.bat 으로 등록');
  if (note) row('현재 모드', '<span class="mk s">예시 데이터</span>', note);

  $('dataBody').innerHTML = rows.join('');
}

/* ============================================================
   10. 하단 탭
   ============================================================ */

export function bindTabs(onSelect) {
  const tabs = Array.from(document.querySelectorAll('.tab'));
  const activate = (name) => {
    for (const t of tabs) t.setAttribute('aria-selected', String(t.dataset.pane === name));
    for (const p of document.querySelectorAll('.tabpane')) p.classList.toggle('on', p.id === name);
    if (onSelect) onSelect(name);
  };
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => activate(t.dataset.pane));
    t.addEventListener('keydown', (e) => {
      const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const nx = tabs[(i + d + tabs.length) % tabs.length];
      nx.focus(); activate(nx.dataset.pane);
    });
  });
  return activate;
}

export function markTabDirty(name, dirty) {
  const t = document.querySelector(`.tab[data-pane="${name}"]`);
  if (t) t.classList.toggle('dirty', !!dirty);
}

/* ============================================================
   11. 배너 / 토스트 / 모달 / 진행바
   ============================================================ */

const ICON = {
  warn: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  err: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5M12 8h.01"/>',
};

/**
 * 화면 상단 배너. 같은 id 는 덮어쓴다.
 * @param {string} id
 * @param {'warn'|'err'|'info'} kind
 * @param {string} html  이미 escape 된 안전한 HTML
 */
export function banner(id, kind, html, dismissible = true) {
  const host = $('banners');
  let el = host.querySelector(`[data-bid="${CSS.escape(id)}"]`);
  if (!el) {
    el = document.createElement('div');
    el.dataset.bid = id;
    host.appendChild(el);
  }
  el.className = `banner ${kind}`;
  el.innerHTML =
    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[kind] || ICON.info}</svg>` +
    `<div>${html}</div>` +
    (dismissible ? '<button class="close" type="button" aria-label="이 안내 닫기"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg></button>' : '');
  const x = el.querySelector('.close');
  if (x) x.addEventListener('click', () => el.remove());
  return el;
}

export function clearBanner(id) {
  const el = $('banners').querySelector(`[data-bid="${CSS.escape(id)}"]`);
  if (el) el.remove();
}

export function toast(msg, kind = '', ms = 2600) {
  const host = $('toastHost');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = `<span class="dot ${kind === 'err' ? 'err' : ''}" aria-hidden="true"></span><span>${esc(msg)}</span>`;
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add('on'));
  setTimeout(() => {
    el.classList.remove('on');
    setTimeout(() => el.remove(), 300);
  }, ms);
}

let modalPrevFocus = null;

/**
 * @param {string} title
 * @param {string} bodyHtml  이미 escape 된 안전한 HTML
 * @param {Array<{label,primary,onClick}>} actions
 */
export function modal(title, bodyHtml, actions = []) {
  modalPrevFocus = document.activeElement;
  $('modalTitle').textContent = title;
  $('modalBody').innerHTML = bodyHtml;
  const foot = $('modalFoot');
  foot.innerHTML = '';
  const acts = actions.length ? actions : [{ label: '확인', primary: true }];
  for (const a of acts) {
    const b = document.createElement('button');
    b.className = 'btn' + (a.primary ? ' btn-primary' : '');
    b.type = 'button';
    b.textContent = a.label;
    b.addEventListener('click', () => { if (a.onClick) a.onClick(); if (a.keepOpen !== true) closeModal(); });
    foot.appendChild(b);
  }
  $('modalBack').hidden = false;
  (foot.querySelector('.btn-primary') || foot.querySelector('.btn'))?.focus();
}

export function closeModal() {
  $('modalBack').hidden = true;
  if (modalPrevFocus && modalPrevFocus.focus) modalPrevFocus.focus();
  modalPrevFocus = null;
}

export function bindModal() {
  $('modalX').addEventListener('click', closeModal);
  $('modalBack').addEventListener('click', (e) => { if (e.target === $('modalBack')) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('modalBack').hidden) closeModal();
  });
}

/** 진행바. pct=null 이면 인디터미닛 */
export function progress(pct) {
  const el = $('prog');
  if (pct === false) {
    el.classList.remove('indet');
    el.style.width = '100%';
    el.setAttribute('aria-hidden', 'true');
    setTimeout(() => { el.style.width = '0'; }, 320);
    return;
  }
  el.setAttribute('aria-hidden', 'false');
  if (pct === null) { el.classList.add('indet'); return; }
  el.classList.remove('indet');
  el.style.width = Math.max(0, Math.min(100, pct)) + '%';
}

/* ============================================================
   12. 차트 범례 / 툴팁
   ============================================================ */

export function renderLegend(info, meta, slotColor, symbol) {
  const host = $('legend');
  if (!info) {
    host.innerHTML = symbol
      ? `<div class="row"><span class="k">${esc(symbol)}</span></div>`
      : '';
    return;
  }
  const rows = [
    `<div class="row">
      <span class="k">O</span><span>${fmt(info.o)}</span>
      <span class="k">H</span><span>${fmt(info.h)}</span>
      <span class="k">L</span><span>${fmt(info.l)}</span>
      <span class="k">C</span><span>${fmt(info.c)}</span>
      <span style="color:var(--${info.up ? 'up' : 'dn'})">${pct(info.chgPct)}</span>
    </div>`,
  ];
  for (const m of meta) {
    const v = info.indicators[m.key];
    rows.push(`<div class="row"><span class="sq" style="background:${esc(slotColor(m.slot))}"></span><span class="k">${esc(m.label)}</span><span>${v === null ? '—' : fmt(v)}</span></div>`);
  }
  host.innerHTML = rows.join('');
}

export function renderTooltip(info, hostRect) {
  const tt = $('tt');
  if (!info) { tt.style.display = 'none'; return; }
  const eokv = Number.isFinite(info.amt) ? (info.amt / 1e8) : NaN;
  const amtTx = Number.isFinite(eokv) ? (eokv >= 10 ? fmt(eokv) + '억' : eokv.toFixed(1) + '억') : '—';
  tt.innerHTML =
    `<div class="d">${esc(info.date)}</div>` +
    `<div class="l"><span>시</span><span>${fmt(info.o)}</span></div>` +
    `<div class="l"><span>고</span><span>${fmt(info.h)}</span></div>` +
    `<div class="l"><span>저</span><span>${fmt(info.l)}</span></div>` +
    `<div class="l"><span>종</span><span style="color:var(--${info.up ? 'up' : 'dn'})">${fmt(info.c)} (${pct(info.chgPct)})</span></div>` +
    `<div class="l"><span>거래대금</span><span${info.spike ? ' style="color:var(--sell)"' : ''}>${amtTx}${info.spike ? ' 급증' : ''}</span></div>` +
    (info.marker ? `<div class="ev" style="color:var(--${info.marker.type === 'buy' ? 'buy' : info.marker.type === 'sell' ? 'sell' : 'sell'})">${esc(info.marker.label)} · ${esc(info.marker.note)}</div>` : '');
  tt.style.display = 'block';
  const w = tt.offsetWidth, h = tt.offsetHeight;
  tt.style.left = Math.max(4, Math.min(info.px + 14, hostRect.width - w - 8)) + 'px';
  tt.style.top = Math.max(4, Math.min(info.py + 14, hostRect.height - h - 8)) + 'px';
}
