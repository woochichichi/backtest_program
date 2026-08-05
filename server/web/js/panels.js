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
    if (el.dataset.type === 'bool') { el.checked = raw === true; return; }
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
    if (el.dataset.type === 'bool') { setPath(next, p, !!el.checked); return; }
    if (el.dataset.type === 'int') {
      const v = el.value === '' ? null : parseInt(el.value, 10);
      setPath(next, p, Number.isFinite(v) ? v : null);
      return;
    }
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

  // 규칙에 ma_period 가 있으면 그 규칙 안의 이동평균 지표 period 를 함께 맞춘다.
  // (전략1의 45일선 손절, 전략3의 20일선/5일선 규칙 모두 같은 방식으로 동작한다)
  for (const key of ['entries', 'exits']) {
    const arr = s[key];
    if (!Array.isArray(arr)) continue;
    for (const rule of arr) {
      if (!rule || typeof rule !== 'object') continue;
      const ma = Number(rule.ma_period);
      if (!Number.isFinite(ma)) continue;
      walkIndicatorNodes(rule, (node) => {
        if (node.indicator === 'SMA' || node.indicator === 'EMA' || node.indicator === 'WMA') {
          node.period = ma;
        }
      });
    }
  }
  return s;
}

/** 중첩 구조 안의 {indicator:...} 노드를 모두 찾아 콜백에 넘긴다 */
function walkIndicatorNodes(o, fn, depth = 0) {
  if (!o || typeof o !== 'object' || depth > 8) return;
  if (Array.isArray(o)) { for (const x of o) walkIndicatorNodes(x, fn, depth + 1); return; }
  if (o.indicator) fn(o);
  for (const k of Object.keys(o)) {
    if (k === 'indicator') continue;
    walkIndicatorNodes(o[k], fn, depth + 1);
  }
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

export function renderByStock(rows, ctx = {}) {
  const tb = $('byStock');
  if (ctx.loading) { tb.innerHTML = skeletonRows(4, 5); return; }
  if (!rows || !rows.length) {
    tb.innerHTML = `<tr class="empty"><td colspan="4">${emptyState(
      ctx.ran
        ? { icon: 'search', title: '기여도를 계산할 거래가 없습니다', desc: '체결된 거래가 하나도 없어 종목별 손익을 만들 수 없습니다.' }
        : { icon: 'chart', title: '아직 결과가 없습니다', desc: '백테스트를 실행하면 어떤 종목이 수익에 기여했는지 순서대로 보여줍니다.' },
    )}</td></tr>`;
    return;
  }
  tb.innerHTML = rows.slice().sort((a, b) => b.pnl - a.pnl).slice(0, 12).map((r) => `
    <tr data-code="${esc(r.code)}">
      <td class="l">${symLink(r.code, r.name || r.code)}</td>
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
  TP: ['익절', 's'], TP1: ['1차 익절', 's'], TP2: ['잔량 청산', 's'],
  SL: ['손절', 'x'], TIME: ['시간청산', 'x'],
  TRAIL: ['트레일링', 'x'], END: ['기간종료', 'x'],
};

/** 매수/매도는 색 외에 삼각형 + 라벨을 함께 쓴다 (색맹 대응) */
const TRI_UP = '<svg width="7" height="7" viewBox="0 0 10 10" fill="currentColor" aria-hidden="true"><path d="M5 1 9 8H1z"/></svg>';
const TRI_DN = '<svg width="7" height="7" viewBox="0 0 10 10" fill="currentColor" aria-hidden="true"><path d="M5 9 1 2h8z"/></svg>';

/**
 * @param {Array} trades
 * @param {function} onRowClick
 * @param {object} ctx {loading, ran, hints:string[]}  왜 비었는지 설명하기 위한 맥락
 */
export function renderTrades(trades, onRowClick, ctx = {}) {
  const tb = $('tradeBody');
  $('tradeCnt').textContent = trades ? trades.length : 0;
  if (ctx.loading) { tb.innerHTML = skeletonRows(15, 6); return; }
  if (!trades || !trades.length) {
    tb.innerHTML = `<tr class="empty"><td colspan="15">${emptyState(
      ctx.ran
        ? {
          icon: 'search',
          title: '조건에 맞는 종목이 없었습니다',
          desc: '백테스트는 정상적으로 끝났지만, 설정한 조건을 모두 만족하는 매매가 한 건도 없었습니다. 아래를 완화해 보세요.',
          hints: ctx.hints && ctx.hints.length ? ctx.hints : [
            '기준일 거래대금 기준을 낮춰 보세요 (예: 1,000억 → 500억)',
            '탐색 기간을 늘려 보세요 (예: 20일 → 40일)',
            '유효 기간을 늘리면 기준일 이후 더 오래 기다립니다',
            '백테스트 구간의 시작일을 앞당겨 보세요',
          ],
        }
        : {
          icon: 'play',
          title: '아직 백테스트를 실행하지 않았습니다',
          desc: '왼쪽에서 전략과 조건을 고른 뒤 실행하면, 언제 사고 언제 팔았는지가 여기에 한 줄씩 쌓입니다.',
          action: { label: '백테스트 실행', act: 'run', primary: true },
        },
    )}</td></tr>`;
    return;
  }
  const fillOf = (t, rule, idx) =>
    (t.fills || []).find((f) => f.rule === rule) || (t.fills || [])[idx] || null;

  // 같은 진입에서 나온 분할 청산은 group_id 로 묶어 한 덩어리로 보이게 한다.
  // (묶지 않으면 같은 종목이 두 줄로 보여 중복 거래로 오해한다)
  const gcount = {};
  for (const t of trades) if (t.group_id) gcount[t.group_id] = (gcount[t.group_id] || 0) + 1;
  let seen = {};

  tb.innerHTML = trades.map((t) => {
    const b1 = fillOf(t, 'B1', 0);
    const b2 = fillOf(t, 'B2', 1);
    const [exLabel, exCls] = EXIT_LABEL[t.exit_rule] || [t.exit_rule || '청산', 'x'];
    const win = (+t.return_pct) >= 0;
    const gid = t.group_id || '';
    const gn = gid ? (gcount[gid] || 1) : 1;
    const idx = gid ? (seen[gid] = (seen[gid] || 0) + 1) : 1;
    const grouped = gn > 1;
    const gcls = grouped ? ` grp-row${idx === 1 ? ' grp-first' : ''}${idx === gn ? ' grp-last' : ''}` : '';
    const gmark = grouped
      ? `<span class="grp-mark" title="같은 매수에서 나온 ${gn}번의 분할 매도 중 ${idx}번째">${idx}/${gn}</span>`
      : '';
    return `<tr class="${gcls.trim()}" data-no="${t.no}" data-code="${esc(t.code)}" data-gid="${esc(gid)}" tabindex="0">
      <td class="l">${t.no}</td>
      <td class="l">${gmark}<b>${esc(t.name || '')}</b> <span class="dim" style="font-family:var(--mono)">${esc(t.code)}</span></td>
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
      // 분할 청산은 한 거래이므로 같은 group 전체를 함께 선택 표시한다
      const gid = tr.dataset.gid;
      if (gid) tb.querySelectorAll(`tr[data-gid="${CSS.escape(gid)}"]`).forEach((x) => x.classList.add('sel'));
      else tr.classList.add('sel');
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
  if (!tr) return;
  const gid = tr.dataset.gid;
  if (gid) tb.querySelectorAll(`tr[data-gid="${CSS.escape(gid)}"]`).forEach((x) => x.classList.add('sel'));
  else tr.classList.add('sel');
  tr.scrollIntoView({ block: 'nearest' });
}

/* ============================================================
   7. 하단 — 시그널 로그
   ============================================================ */

export function renderSignals(signals, ctx = {}) {
  const host = $('sigLog');
  if (ctx.loading) {
    host.innerHTML = Array.from({ length: 6 }, (_, i) =>
      `<span class="sk sk-line" style="width:${[92, 78, 85, 64, 88, 71][i]}%"></span>`).join('\n');
    return;
  }
  if (!signals || !signals.length) {
    host.innerHTML = emptyState(ctx.ran
      ? { icon: 'search', title: '기록된 시그널이 없습니다', desc: '조건을 만족하는 종목이 없어 체결 로그도 비어 있습니다.' }
      : {
        icon: 'list',
        title: '아직 기록이 없습니다',
        desc: '백테스트를 실행하면 전 종목 스캔 → 후보 등록 → 매수·매도 체결까지 프로그램이 무엇을 했는지 시간 순서대로 남습니다.',
        action: { label: '백테스트 실행', act: 'run', primary: true },
      });
    return;
  }
  host.innerHTML = signals.map((s) => {
    const lv = String(s.level || 'INFO').toUpperCase();
    const cls = ['MATCH', 'FILL', 'WARN', 'ERROR'].includes(lv) ? ` lv-${lv}` : '';
    const body = linkifyCodes(esc(s.message || ''), ctx.nameOf);
    return `<span class="c">${esc(s.ts || '')}</span>  <span class="k${cls}">[${esc(lv.padEnd(5))}]</span>  ` +
      (s.code ? symLink(s.code, s.code, { cls: 'mono' }) + ' ' : '') + body;
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

/* ============================================================
   13. 빈 상태 / 로딩 스켈레톤
   ------------------------------------------------------------
   원칙: 비어 있으면 "왜 비었는지 + 무엇을 하면 되는지"를 반드시 쓴다.
   ============================================================ */

const EMPTY_ICON = {
  play:   '<circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l5.5-3.5z"/>',
  chart:  '<path d="M3 3v18h18"/><path d="M7 15v-4M11 17V8M15 15v-6M19 17v-3"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  list:   '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  symbol: '<path d="M3 3v18h18"/><path d="m7 13 3 3 4-6 3 3"/>',
  alert:  '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
};

/**
 * 빈 상태 블록 HTML.
 * @param {object} o {icon, title, desc, hints:string[], action:{label,act,primary}, tone}
 */
export function emptyState(o = {}) {
  const icon = EMPTY_ICON[o.icon] || EMPTY_ICON.chart;
  const hints = (o.hints || []).map((h) => `<li>${esc(h)}</li>`).join('');
  const act = o.action
    ? `<button type="button" class="btn btn-sm ${o.action.primary ? 'btn-primary' : ''}" data-act="${esc(o.action.act)}">${esc(o.action.label)}</button>`
    : '';
  return `<div class="empty-state${o.tone ? ' ' + o.tone : ''}">
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icon}</svg>
    <b>${esc(o.title || '')}</b>
    ${o.desc ? `<span>${esc(o.desc)}</span>` : ''}
    ${hints ? `<ul class="empty-hints">${hints}</ul>` : ''}
    ${act}
  </div>`;
}

/** 표 로딩 스켈레톤. 레이아웃이 밀리지 않도록 실제 행 높이를 유지한다. */
export function skeletonRows(cols, rows = 5) {
  const widths = [70, 92, 55, 80, 64, 88, 50, 76];
  let out = '';
  for (let r = 0; r < rows; r++) {
    let tds = '';
    for (let c = 0; c < cols; c++) {
      tds += `<td><span class="sk sk-line" style="width:${widths[(r + c) % widths.length]}%"></span></td>`;
    }
    out += `<tr class="sk-row" aria-hidden="true">${tds}</tr>`;
  }
  return out;
}

/** 좌측 전략 목록 스켈레톤 */
export function renderStrategyListLoading() {
  $('stgList').innerHTML = Array.from({ length: 3 }, () =>
    `<div class="stg sk-stg" aria-hidden="true">
       <span class="sk sk-box"></span>
       <span class="stg-tx"><span class="sk sk-line" style="width:64%"></span><span class="sk sk-line" style="width:92%;margin-top:5px"></span></span>
     </div>`).join('');
}

/** 빈 상태 안의 버튼을 한 곳에서 위임 처리한다 */
export function bindEmptyActions(handlers) {
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const fn = handlers[b.dataset.act];
    if (fn) { e.preventDefault(); fn(b); }
  });
}

/* ============================================================
   14. 실행 기준 (assumptions)
   ------------------------------------------------------------
   백테스트 숫자가 어떤 가정 위에 있는지 사용자가 항상 볼 수 있게 한다.
   ============================================================ */

const RES_LABEL = { '1d': '일봉', '1m': '1분봉', '1h': '1시간봉' };
const FILL_LABEL = {
  touch: '목표가에 닿으면 그 가격에 체결 (갭이면 시가)',
  next_open: '다음 거래일 시가에 체결',
  close: '조건 성립한 날 종가에 체결',
};
const SDE_LABEL = {
  loss_only: '진입 당일 익절 없음',
  never: '진입 당일 청산 없음',
  always: '진입 당일 익절 허용',
};
const SDE_DESC = {
  loss_only: '산 날에는 손절만 따지고 익절은 다음 거래일부터 봅니다 (보수적)',
  never: '산 날에는 아무 청산도 따지지 않습니다 (가장 보수적)',
  always: '산 날에도 익절을 인정합니다 (결과가 실제보다 좋게 나올 수 있음)',
};

/** 접힌 상태에서도 항상 보여줄 핵심 한 줄 */
export function assumptionsSummary(a) {
  if (!a) return '백테스트를 실행하면 어떤 기준으로 계산했는지 여기에 표시됩니다';
  const res = RES_LABEL[a.resolution] || a.resolution || '일봉';
  const sde = SDE_LABEL[a.same_day_exit] || a.same_day_exit || '';
  const cost = (Number(a.slippage_pct) || 0) + (Number(a.fee_pct) || 0);
  const ign = Array.isArray(a.ignored_filters) ? a.ignored_filters.length : 0;
  return `${res} 기준 · ${sde} · 비용 ${(+cost.toFixed(3))}% 반영`
    + (ign ? ` · 미적용 조건 ${ign}개` : '');
}

/**
 * @param {object} a  result.assumptions
 * @param {object} metrics result.metrics (거래 수 대비 비율 계산용)
 */
export function renderAssumptions(a, metrics, params) {
  // params 선언이 있으면 raw key 대신 사람이 읽는 라벨을 쓴다
  const labelOf = {};
  for (const prm of (params || [])) {
    if (prm && prm.key) labelOf[prm.key] = prm.label || prm.key;
  }
  const sum = $('assumeSum');
  const body = $('assumeBody');
  const flag = $('assumeFlag');
  const toggle = $('assumeToggle');
  if (!sum || !body) return;

  sum.textContent = assumptionsSummary(a);

  if (!a) {
    flag.innerHTML = '';
    toggle.disabled = true;
    body.innerHTML = '';
    return;
  }
  toggle.disabled = false;

  const trades = Number(metrics && metrics.trades) || 0;
  const st = a.stats || {};
  const amb = Number(st.ambiguous_bars) || 0;
  const ambRatio = trades > 0 ? (amb / trades) * 100 : 0;
  const ambHot = amb > 0 && ambRatio >= 20;     // 거래 수 대비 20% 이상이면 경고 톤
  const optimistic = a.same_day_exit === 'always';

  // 접힌 상태에서도 위험 신호는 보이게 한다
  const ignoredN = Array.isArray(a.ignored_filters) ? a.ignored_filters.length : 0;
  flag.innerHTML = ignoredN
    ? `<span class="flag danger">미적용 조건 ${ignoredN}</span>`
    : (optimistic
      ? '<span class="flag danger">낙관적 설정</span>'
      : (ambHot ? '<span class="flag warn">가정 의존 높음</span>' : ''));

  const rows = [];
  const row = (k, v, note, tone) => rows.push(
    `<div class="as-row${tone ? ' ' + tone : ''}"><span class="as-k">${esc(k)}</span>
     <span class="as-v">${v}</span>${note ? `<span class="as-n">${esc(note)}</span>` : ''}</div>`);

  const reqRes = a.requested_resolution || a.resolution;
  const downgraded = reqRes && reqRes !== a.resolution;
  row('시세 기준', `${esc(RES_LABEL[a.resolution] || a.resolution)}`,
    downgraded ? `전략은 ${RES_LABEL[reqRes] || reqRes}을 요청했지만 일봉으로 계산했습니다` : '하루 한 개의 봉으로 계산');
  row('체결 방식', esc(FILL_LABEL[a.fill_model] || a.fill_model || '—'));
  row('진입 당일 청산', esc(SDE_LABEL[a.same_day_exit] || a.same_day_exit || '—'),
    SDE_DESC[a.same_day_exit] || '', optimistic ? 'danger' : '');
  row('슬리피지', `${esc(a.slippage_pct)}%`, '살 때 +, 팔 때 − 로 불리하게 적용');
  row('수수료+세금', `${esc(a.fee_pct)}%`, '팔 때 한 번에 차감');
  if (Array.isArray(a.exit_priority) && a.exit_priority.length) {
    row('청산 우선순위', esc(a.exit_priority.join(' → ')),
      a.exit_priority.length > 1 ? '같은 날 둘 다 걸리면 앞쪽을 먼저 적용' : '');
  }

  const sameDay = Number(st.same_day_entry_exit) || 0;
  if (sameDay > 0) {
    row('산 날 바로 판 거래', `${fmt(sameDay)}건`,
      `전체 거래의 ${(Number(st.same_day_entry_exit_pct) || 0).toFixed(1)}%`);
  }
  if (amb > 0) {
    row('순서를 알 수 없던 봉', `${fmt(amb)}개`,
      trades > 0 ? `거래 ${fmt(trades)}건 대비 ${ambRatio.toFixed(0)}%` : '', ambHot ? 'warn' : '');
  }

  const notes = (a.notes || []).filter((n) => n && String(n).trim());
  const ignored = Array.isArray(a.ignored_filters) ? a.ignored_filters : [];

  body.innerHTML =
    (ignored.length
      ? `<div class="as-alert danger">
           <b>적용되지 않은 조건이 ${ignored.length}개 있습니다.</b>
           전략에는 있지만 데이터가 없어 계산에서 빠진 조건입니다.
           이 조건들이 걸러 냈어야 할 종목까지 포함되어 있으므로,
           <b>실제로 이 전략을 돌렸을 때보다 결과가 좋게 나올 수 있습니다.</b>
           <ul class="ign-list">${ignored.map((f) => `<li><b>${esc(labelOf[f.key] || f.key)}</b><span>${esc(f.reason)}</span></li>`).join('')}</ul>
         </div>`
      : '') +
    (optimistic
      ? `<div class="as-alert danger">
           <b>결과가 실제보다 좋게 나올 수 있는 설정입니다.</b>
           산 날에 바로 익절한 것으로 계산했습니다. 하루 안에서 매수가와 익절가 중
           무엇이 먼저 닿았는지는 일봉만으로 알 수 없기 때문에, 실제 매매에서는
           이만큼 수익이 나지 않을 수 있습니다.
         </div>`
      : '') +
    (ambHot
      ? `<div class="as-alert warn">
           <b>가정에 기댄 판정이 많습니다.</b>
           ${fmt(amb)}개 봉에서 매수가와 청산가가 같은 날 모두 닿아, 어느 쪽이 먼저인지
           가정해서 계산했습니다. 거래 수 대비 ${ambRatio.toFixed(0)}% 이므로 이 결과는
           참고용으로만 보세요.
         </div>`
      : '') +
    `<div class="as-rows">${rows.join('')}</div>` +
    (notes.length
      ? `<div class="as-notes"><div class="as-notes-t">계산에 사용한 가정</div><ul>${
        notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul></div>`
      : '');
}

/** 실행 기준 패널 접기/펴기 */
export function bindAssumptions() {
  const t = $('assumeToggle'), b = $('assumeBody');
  if (!t || !b) return;
  t.addEventListener('click', () => {
    const open = t.getAttribute('aria-expanded') === 'true';
    t.setAttribute('aria-expanded', String(!open));
    b.hidden = open;
  });
}

/* ============================================================
   15. 오류 표현 (error 크게 / detail 은 "자세히")
   ============================================================ */

/**
 * ApiError → 배너용 HTML.
 * error 는 크게, advice 는 그 아래, detail 은 접어 둔다.
 */
export function errorHtml(err, title) {
  const msg = (err && err.message) || String(err);
  const advice = (err && err.advice) || '';
  const detail = (err && err.detail) || '';
  return `${title ? `<b>${esc(title)}</b><br>` : ''}` +
    `<span class="err-msg">${esc(msg)}</span>` +
    (advice ? `<div class="err-advice">${esc(advice)}</div>` : '') +
    (detail
      ? `<button type="button" class="link-btn" data-more>자세히</button>
         <pre class="err-detail" hidden>${esc(detail)}</pre>`
      : '');
}

/* ============================================================
   16. 데이터 신선도
   ============================================================ */

/**
 * 마지막 갱신 시각 → 사람이 읽는 신선도.
 * 색만으로 전달하지 않기 위해 항상 텍스트 라벨을 함께 돌려준다.
 */
export function freshness(lastSync) {
  if (!lastSync) return { days: null, label: '받은 적 없음', tone: 'err', fresh: false };
  const d = new Date(String(lastSync).replace(' ', 'T'));
  if (Number.isNaN(d.getTime())) return { days: null, label: '알 수 없음', tone: 'stale', fresh: false };
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(new Date()) - startOf(d)) / 86400000);
  if (days <= 0) return { days: 0, label: '오늘', tone: 'ok', fresh: true };
  if (days === 1) return { days: 1, label: '어제', tone: 'stale', fresh: false };
  return { days, label: `${days}일 전`, tone: days >= 7 ? 'err' : 'stale', fresh: false };
}

/* ============================================================
   17. 종목 검색 결과
   ============================================================ */

export function renderSymbolResults(list, total, q, onPick, note) {
  const host = $('symResults');
  if (!host) return;
  if (!list) {
    host.innerHTML = `<div class="sym-note">${esc(note || '검색 중…')}</div>`;
    return;
  }
  if (!list.length) {
    host.innerHTML = `<div class="sym-note">'${esc(q)}' 에 해당하는 종목이 없습니다. 종목명 일부 또는 6자리 코드를 입력해 보세요.</div>`;
    return;
  }
  host.innerHTML =
    list.map((s, i) => `<button type="button" class="sym-item" role="option" data-code="${esc(s.code)}" data-name="${esc(s.name)}" tabindex="-1"${i === 0 ? ' data-first="1"' : ''}>
      <span class="sym-item-cd">${esc(s.code)}</span>
      <span class="sym-item-nm">${esc(s.name)}</span>
      <span class="sym-item-mk">${esc(s.market || '')}</span>
      ${Number.isFinite(+s.marcap_eok) ? `<span class="sym-item-mc">${fmt(s.marcap_eok)}억</span>` : ''}
    </button>`).join('') +
    (total > list.length ? `<div class="sym-note">전체 ${fmt(total)}건 중 ${list.length}건 표시 — 검색어를 더 입력하면 좁혀집니다.</div>` : '');

  host.querySelectorAll('.sym-item').forEach((b) => {
    b.addEventListener('click', () => onPick({ code: b.dataset.code, name: b.dataset.name }));
  });
}

/* ============================================================
   18. 전략 변경 diff (AI 변환 등 덮어쓰기 전 확인용)
   ============================================================ */

/** 중첩 객체를 leaf 경로 맵으로 평탄화 */
function flatten(obj, prefix = '', out = {}) {
  if (obj === null || typeof obj !== 'object') { out[prefix] = obj; return out; }
  if (Array.isArray(obj)) {
    obj.forEach((v, i) => flatten(v, `${prefix}[${i}]`, out));
    if (!obj.length) out[prefix] = '(빈 목록)';
    return out;
  }
  const keys = Object.keys(obj);
  if (!keys.length) { out[prefix] = '(빈 값)'; return out; }
  for (const k of keys) flatten(obj[k], prefix ? `${prefix}.${k}` : k, out);
  return out;
}

/** 사람이 읽을 수 있는 필드 이름 */
const PATH_LABEL = {
  'universe.reference_day.spike_amount_krw_eok': '기준일 거래대금(억)',
  'universe.reference_day.lookback_days': '탐색 기간(일)',
  'universe.reference_day.prev_day_amount_max_eok': '전일 거래대금 상한(억)',
  'universe.valid_days_after_reference': '유효 기간(일)',
  'universe.markets': '대상 시장',
  'entries[0].size_pct': '1차 매수 비중(%)',
  'entries[1].trigger_pct': '2차 트리거(%)',
  'entries[1].size_pct': '2차 매수 비중(%)',
  'exits[0].target_pct': '익절(%)',
  'exits[1].ma_period': '손절 이동평균(일)',
  'execution.resolution': '체결 해상도',
  'execution.slippage_pct': '슬리피지(%)',
  'execution.fee_pct': '수수료+세금(%)',
  'portfolio.initial_capital_manwon': '초기 자본(만원)',
  'portfolio.max_positions': '최대 동시 보유',
  'period.start': '시작일',
  'period.end': '종료일',
  name: '전략 이름',
  description: '설명',
};

/** @returns {Array<{path,label,before,after,kind}>} */
export function diffStrategies(before, after) {
  const A = flatten(before || {});
  const B = flatten(after || {});
  const paths = [...new Set([...Object.keys(A), ...Object.keys(B)])].sort();
  const out = [];
  for (const p of paths) {
    const a = A[p], b = B[p];
    if (JSON.stringify(a) === JSON.stringify(b)) continue;
    out.push({
      path: p,
      label: PATH_LABEL[p] || p,
      before: a === undefined ? null : a,
      after: b === undefined ? null : b,
      kind: a === undefined ? 'added' : (b === undefined ? 'removed' : 'changed'),
    });
  }
  return out;
}

const KIND_TX = { added: '추가', removed: '삭제', changed: '변경' };

export function renderDiffTable(diffs, limit = 40) {
  if (!diffs.length) {
    return '<p class="hint">바뀌는 값이 없습니다. 지금 설정과 동일합니다.</p>';
  }
  const shown = diffs.slice(0, limit);
  const cell = (v) => v === null || v === undefined
    ? '<span class="dim">없음</span>'
    : esc(typeof v === 'object' ? JSON.stringify(v) : String(v));
  return `<div class="diff-wrap"><table class="diff">
    <thead><tr><th class="l">항목</th><th class="l">지금</th><th class="l">바뀔 값</th></tr></thead>
    <tbody>${shown.map((d) => `<tr>
      <td class="l"><b>${esc(d.label)}</b>${d.label !== d.path ? `<br><span class="dim mono">${esc(d.path)}</span>` : ''}</td>
      <td class="l old">${cell(d.before)}</td>
      <td class="l new">${cell(d.after)} <span class="diff-kind ${d.kind}">${KIND_TX[d.kind]}</span></td>
    </tr>`).join('')}</tbody></table></div>` +
    (diffs.length > limit ? `<p class="hint">외 ${diffs.length - limit}개 항목이 더 바뀝니다.</p>` : '');
}

/* ============================================================
   19. 파라미터 폼 — 범위 안내 · 즉시 검증 · 저장 안 됨 표시
   ============================================================ */

/** 입력마다 허용 범위를 작은 글씨로 붙인다 (min/max/step 은 HTML 이 갖고 있다) */
export function renderFieldHints() {
  document.querySelectorAll('#paramForm input[type="number"][data-path]').forEach((el) => {
    if (el.parentElement.querySelector('.fld-range')) return;
    const min = el.getAttribute('min'), max = el.getAttribute('max');
    if (min === null && max === null) return;
    const unit = (el.parentElement.querySelector('.unit') || {}).textContent || '';
    const nice = (v) => {
      if (v === null || v === undefined) return null;
      const n = Number(v);
      return Number.isFinite(n) && Math.abs(n) >= 10000 ? n.toLocaleString('ko-KR') : String(v);
    };
    const s = document.createElement('span');
    s.className = 'fld-range';
    s.textContent = `${nice(min) ?? '−∞'} ~ ${nice(max) ?? '∞'}${unit ? ' ' + unit.trim() : ''}`;
    el.parentElement.querySelector('label')?.appendChild(s);
  });
}

/**
 * 저장 전에 프런트에서 바로 잡을 수 있는 문제들.
 * 서버 검증(422)을 대체하지 않고, 서버까지 가기 전에 알려 주는 용도다.
 * @returns {Array<{id, level, message}>}
 */
export function formIssues() {
  const issues = [];
  const byPath = {};
  document.querySelectorAll('#paramForm [data-path]').forEach((el) => { byPath[el.dataset.path] = el; });

  document.querySelectorAll('#paramForm input[type="number"][data-path]').forEach((el) => {
    if (el.disabled) return;                       // 적용되지 않는 항목은 검증하지 않는다
    if (el.value === '') { issues.push({ id: el.id, level: 'err', message: '값을 입력하세요.' }); return; }
    const v = Number(el.value);
    if (!Number.isFinite(v)) { issues.push({ id: el.id, level: 'err', message: '숫자만 입력할 수 있습니다.' }); return; }
    const min = el.getAttribute('min'), max = el.getAttribute('max');
    if (min !== null && v < Number(min)) issues.push({ id: el.id, level: 'err', message: `${min} 이상이어야 합니다. 지금은 ${v} 입니다.` });
    else if (max !== null && v > Number(max)) issues.push({ id: el.id, level: 'err', message: `${max} 이하여야 합니다. 지금은 ${v} 입니다.` });
  });

  // 분할 매수 비중 합 — entries[*].size_pct 를 path 로 모아 검사한다(전략 구조와 무관)
  const sizeEls = Object.keys(byPath)
    .filter((k) => /^entries\[\d+\]\.size_pct$/.test(k))
    .map((k) => byPath[k])
    .filter((el) => el && !el.disabled && el.value !== '');
  if (sizeEls.length > 1) {
    const sum = sizeEls.reduce((a, el) => a + Number(el.value), 0);
    if (Number.isFinite(sum) && Math.abs(sum - 100) > 0.001) {
      issues.push({
        id: sizeEls[sizeEls.length - 1].id, level: 'warn',
        message: sum < 100
          ? `분할 매수 비중 합이 ${+sum.toFixed(1)}% 입니다. 종목당 배정 자본의 ${+(100 - sum).toFixed(1)}% 는 사용하지 않습니다.`
          : `분할 매수 비중 합이 ${+sum.toFixed(1)}% 로 100% 를 넘습니다. 배정 자본보다 많이 사려고 합니다.`,
      });
    }
  }

  // 하락률 성격의 트리거는 음수여야 의미가 있다
  for (const key of Object.keys(byPath)) {
    if (!/trigger_pct$/.test(key)) continue;
    const el = byPath[key];
    if (!el || el.disabled || el.value === '') continue;
    const t = Number(el.value);
    if (Number.isFinite(t) && t >= 0) {
      issues.push({ id: el.id, level: 'warn', message: '이 값은 매수가 대비 하락률이라 보통 음수입니다 (예: -10).' });
    }
  }

  // 시작일 / 종료일 순서
  const sEl = byPath['period.start'], eEl = byPath['period.end'];
  if (sEl && eEl && sEl.value && eEl.value && sEl.value > eEl.value) {
    issues.push({ id: eEl.id, level: 'err', message: '종료일이 시작일보다 빠릅니다.' });
  }
  return issues;
}

/** formIssues() 결과를 각 필드 옆에 붙인다 */
export function showFormIssues(issues) {
  clearFieldErrors();
  for (const it of issues) {
    const el = $(it.id);
    if (!el) continue;
    if (it.level === 'err') el.setAttribute('aria-invalid', 'true');
    const msg = document.createElement('span');
    msg.className = 'fld-err' + (it.level === 'warn' ? ' warn' : '');
    msg.dataset.err = '1';
    msg.id = `${it.id}-err`;
    msg.textContent = it.message;
    el.closest('.fld').after(msg);
    el.setAttribute('aria-describedby', msg.id);
  }
  return issues;
}

/** 첫 오류 필드로 스크롤 + 포커스 */
export function focusFirstError() {
  const el = document.querySelector('#paramForm [aria-invalid="true"]');
  if (!el) return false;
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  setTimeout(() => el.focus({ preventScroll: true }), 220);
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1200);
  return true;
}

/** "저장 안 됨" 배지 */
export function setDirtyBadge(dirty) {
  const el = $('dirtyBadge');
  if (!el) return;
  el.hidden = !dirty;
}

/* ============================================================
   20. 상단 "지금 할 일" 안내
   ============================================================ */

/**
 * @param {object|null} step {title, desc, action:{label,act}, tone}
 *   null 이면 안내를 치운다.
 */
export function renderNextStep(step) {
  const host = $('nextStep');
  if (!host) return;
  if (!step) { host.hidden = true; host.innerHTML = ''; return; }
  host.hidden = false;
  host.className = 'next-step ' + (step.tone || 'info');
  host.innerHTML =
    `<span class="ns-num">${esc(step.num || '1')}</span>
     <div class="ns-tx"><b>${esc(step.title)}</b>${step.desc ? `<span>${step.desc}</span>` : ''}</div>
     ${step.action ? `<button type="button" class="btn btn-primary btn-sm" data-act="${esc(step.action.act)}">${esc(step.action.label)}</button>` : ''}`;
}

/* ============================================================
   21. params 선언 기반 파라미터 폼 (ARCHITECTURE-v2 §1)
   ------------------------------------------------------------
   전략 JSON 의 params 배열로 폼을 생성한다. 하드코딩 금지.
   params 가 없는 전략은 index.html 의 <template id="legacyForm"> 으로 폴백한다.
   ============================================================ */

/** 전략이 params 선언을 갖고 있는가 */
export function hasParams(strategy) {
  return !!(strategy && Array.isArray(strategy.params) && strategy.params.length);
}

/** "1. 종목 선정" → {num:'1', name:'종목 선정'} */
function splitGroup(g, fallbackNum) {
  const m = String(g || '').match(/^\s*(\d+)\s*[.)]\s*(.+)$/);
  return m ? { num: m[1], name: m[2] } : { num: String(fallbackNum), name: String(g || '기타') };
}

const PARAM_INPUT_TYPE = { number: 'number', int: 'number', percent: 'number', date: 'date' };

function paramField(p, i) {
  const id = 'pp_' + String(p.key || i).replace(/[^\w-]/g, '_');
  const off = p.available === false;
  const dis = off ? ' disabled' : '';
  const common =
    ` id="${esc(id)}" data-path="${esc(p.path)}" data-key="${esc(p.key)}" data-type="${esc(p.type || 'number')}"${dis}`;

  let control;
  if (p.type === 'bool') {
    control = `<span class="sw"><input type="checkbox"${common}><i></i></span>`;
  } else if (p.type === 'select') {
    const opts = (p.options || []).map((o) => {
      const val = (o && typeof o === 'object') ? o.value : o;
      const lab = (o && typeof o === 'object') ? (o.label ?? o.value) : o;
      return `<option value="${esc(val)}">${esc(lab)}</option>`;
    }).join('');
    control = `<select class="inp wide"${common}>${opts}</select>`;
  } else {
    const t = PARAM_INPUT_TYPE[p.type] || 'number';
    const attrs = [
      p.min !== undefined ? ` min="${esc(p.min)}"` : '',
      p.max !== undefined ? ` max="${esc(p.max)}"` : '',
      p.step !== undefined ? ` step="${esc(p.step)}"` : '',
    ].join('');
    control = `<input class="inp${t === 'date' ? ' wide' : ''}" type="${t}"${attrs}${common}>`;
  }

  return `<div class="fld${off ? ' fld-off' : ''}">
      <label for="${esc(id)}"><b>${esc(p.label || p.key)}</b>${p.help ? esc(p.help) : ''}</label>
      ${control}
      <span class="unit">${esc(p.unit || '')}</span>
    </div>` +
    (off
      ? `<p class="fld-unavail">
           <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>
           <span><b>적용되지 않음</b> ${esc(p.unavailable_reason || '이 조건에 필요한 데이터가 없습니다.')}</span>
         </p>`
      : '');
}

/**
 * 파라미터 폼을 그린다.
 * @returns {'params'|'legacy'} 어떤 방식으로 그렸는지
 */
export function renderParamForm(strategy) {
  const host = $('paramForm');
  if (!hasParams(strategy)) {
    // 하위 호환 — params 가 없는 전략은 기존 고정 폼을 쓴다
    host.innerHTML = '';
    const tpl = document.getElementById('legacyForm');
    if (tpl) host.appendChild(tpl.content.cloneNode(true));
    host.dataset.mode = 'legacy';
    return 'legacy';
  }

  const order = [];
  const byGroup = new Map();
  for (const p of strategy.params) {
    const g = p.group || '기타';
    if (!byGroup.has(g)) { byGroup.set(g, []); order.push(g); }
    byGroup.get(g).push(p);
  }

  let html = '';
  order.forEach((g, gi) => {
    const { num, name } = splitGroup(g, gi + 1);
    const items = byGroup.get(g);
    const offN = items.filter((x) => x.available === false).length;
    html += `<div class="grp">
      <div class="grp-t"><span class="n">${esc(num)}</span>${esc(name)}` +
      (offN ? `<span class="grp-off" title="데이터가 없어 적용되지 않는 항목">미적용 ${offN}</span>` : '') +
      `</div>`;
    items.forEach((p, i) => { html += paramField(p, `${gi}_${i}`); });
    html += '</div>';
  });

  // 차트 지표 칩은 params 와 별개로 항상 마지막에 둔다
  html += `<div class="grp">
    <div class="grp-t"><span class="n">${order.length + 1}</span>차트에 표시할 지표</div>
    <div class="chips" id="indChips" aria-label="차트에 표시할 지표"></div>
  </div>`;

  host.innerHTML = html;
  host.dataset.mode = 'params';
  return 'params';
}

/**
 * params[].default 를 각 path 에 다시 써넣은 새 전략 객체.
 * available:false 항목도 되돌린다. params 자체는 건드리지 않는다. (v2 §1)
 */
export function applyParamDefaults(strategy) {
  const next = structuredClone(strategy);
  for (const p of (strategy.params || [])) {
    if (!p || !p.path) continue;
    setPath(next, p.path, p.default);
  }
  return next;
}

/** 지금 값이 기본값과 다른 param 개수 */
export function countChangedFromDefault(strategy) {
  let n = 0;
  for (const p of (strategy && strategy.params) || []) {
    if (!p || !p.path) continue;
    if (JSON.stringify(getPath(strategy, p.path)) !== JSON.stringify(p.default)) n++;
  }
  return n;
}

/* ============================================================
   22. 백테스트 진행 상황 (단계 · % · 경과 · 남은 시간)
   ============================================================ */

/** 초 → "1분 12초" */
export function humanSec(sec) {
  if (!Number.isFinite(sec) || sec < 0) return '—';
  const s = Math.round(sec);
  if (s < 60) return `${s}초`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}분 ${r}초` : `${m}분`;
  const h = Math.floor(m / 60);
  return `${h}시간 ${m % 60}분`;
}

/**
 * @param {object} st {phase, message, pct, elapsedSec, etaSec, cancellable}
 */
export function renderRunProgress(st) {
  const host = $('runPanel');
  if (!host) return;
  if (!st) { host.hidden = true; host.innerHTML = ''; return; }
  host.hidden = false;

  const pct = Number.isFinite(st.pct) ? Math.max(0, Math.min(100, st.pct)) : null;
  const eta = st.etaSec === null || st.etaSec === undefined
    ? '계산 중…'
    : `약 ${humanSec(st.etaSec)} 남음`;

  // "종목별 매매 계산 중 (1,240/2,700 종목)" 처럼 단계명이 되풀이되면 뒷부분만 남긴다
  let msg = st.message || '';
  if (st.phase && msg.startsWith(st.phase)) msg = msg.slice(st.phase.length).trim();

  host.innerHTML =
    `<div class="run-top">
       <span class="spinner" aria-hidden="true"></span>
       <b class="run-phase">${esc(st.phase || '백테스트 실행 중')}</b>
       <span class="run-pct">${pct === null ? '' : pct.toFixed(0) + '%'}</span>
       <span class="run-meta">${esc(humanSec(st.elapsedSec))} 경과 · ${esc(eta)}</span>
       ${st.cancellable ? '<button type="button" class="btn btn-sm" data-act="cancelRun">취소</button>' : ''}
     </div>
     <div class="run-bar"><i class="${pct === null ? 'indet' : ''}" style="${pct === null ? '' : `width:${pct}%`}"></i></div>
     ${msg ? `<div class="run-msg">${esc(msg)}</div>` : ''}`;
}


/* ============================================================
   21. 종목 링크 — 화면 어디서든 종목을 누르면 차트가 따라온다
   ============================================================ */

/**
 * 클릭 가능한 종목 이름.
 * 모든 진입점이 같은 마크업을 쓰고, main.js 가 위임 처리로 selectSymbol 을 부른다.
 */
export function symLink(code, name, opts = {}) {
  if (!code) return esc(name || '');
  const label = name || code;
  return `<button type="button" class="symlink${opts.cls ? ' ' + opts.cls : ''}" data-symlink data-code="${esc(code)}"` +
    ` data-name="${esc(name || '')}" title="${esc(label)} (${esc(code)}) 차트 보기">${esc(label)}` +
    (opts.showCode ? `<span class="symlink-cd">${esc(code)}</span>` : '') + '</button>';
}

/** 로그 문자열 안의 6자리 종목코드를 링크로 바꾼다 (이미 escape 된 문자열에 적용) */
export function linkifyCodes(escaped, nameOf) {
  return String(escaped).replace(/(^|[^0-9A-Za-z])(\d{6})(?![0-9])/g, (m, pre, code) => {
    const nm = nameOf ? (nameOf(code) || '') : '';
    return pre + `<button type="button" class="symlink mono" data-symlink data-code="${code}" data-name="${esc(nm)}" title="${esc(nm || code)} 차트 보기">${code}</button>`;
  });
}

/** 현재 차트에 떠 있는 종목을 화면 전체에서 강조한다 */
export function highlightSymbol(code) {
  for (const el of document.querySelectorAll('[data-symlink]')) {
    el.classList.toggle('sym-on', !!code && el.dataset.code === code);
  }
  for (const tr of document.querySelectorAll('#byStock tr[data-code]')) {
    tr.classList.toggle('sel', !!code && tr.dataset.code === code);
  }
}

/** 종목 링크 클릭/키보드를 한 곳에서 위임 처리 */
export function bindSymbolLinks(onPick) {
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-symlink]');
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();          // 거래 내역 행 클릭과 중복 실행되지 않게
    onPick(b.dataset.code, { name: b.dataset.name });
  });
}
