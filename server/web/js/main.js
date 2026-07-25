/* ============================================================
   main.js — 부트스트랩 / 애플리케이션 상태
   ------------------------------------------------------------
   흐름:
     status → indicators → strategies → strategy → chart
     [백테스트 실행] → 우측 KPI · 곡선 · 하단 표 갱신
   에러는 절대 삼키지 않는다. 무엇을 해야 하는지 한국어 배너로 띄운다.
   ============================================================ */

import * as api from './api.js';
import { ApiError } from './api.js';
import { initTheme, bindThemeToggle } from './theme.js';
import { CandleChart, MiniChart } from './chart.js';
import * as P from './panels.js';

const $ = (id) => document.getElementById(id);

/* ---------------- 상태 ---------------- */
const S = {
  status: null,
  strategies: [],
  strategyId: null,
  strategy: null,          // 서버에서 받은 원본 DSL
  draft: null,             // 폼 편집이 반영된 DSL
  dirty: false,
  specs: [],               // /api/indicators
  active: [],              // 화면에 그릴 지표 인스턴스
  result: null,            // /api/backtest 응답
  tradeNo: 0,
  rangeMonths: 6,
  symbol: { code: '', name: '' },
  running: false,
  chart: null, eq: null, mo: null,
};

/* ---------------- 공통 유틸 ---------------- */

const ymdInt = (s) => Number(String(s || '').replace(/-/g, '')) || 0;

function pad(n) { return String(n).padStart(2, '0'); }
function isoOf(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }

/** 조회 기간 계산. months=0 이면 전체. */
function rangeParams() {
  const endStr = (S.status && S.status.latest_trade_date) || isoOf(new Date());
  if (!S.rangeMonths) {
    const first = (S.status && S.status.first_trade_date) || '2015-01-02';
    return { start: first, end: endStr, n: 3000 };
  }
  const end = new Date(endStr + 'T00:00:00');
  const start = new Date(end);
  start.setMonth(start.getMonth() - S.rangeMonths);
  // 폴백 mockdata 가 만들 봉 수 (영업일 ≈ 21/월)
  return { start: isoOf(start), end: endStr, n: Math.round(S.rangeMonths * 21) };
}

/** 예상치 못한 실패를 화면에 드러낸다 */
function handleError(e, ctx) {
  console.error(`[${ctx}]`, e);
  const isApi = e instanceof ApiError;
  const msg = isApi ? e.message : (e && e.message) || String(e);
  const how = isApi && e.howTo ? e.howTo : '';
  P.banner('err-' + ctx, isApi && e.offline ? 'warn' : 'err',
    `<b>${P.esc(ctx)}</b> — ${P.esc(msg)}${how ? `<br>${P.esc(how)}` : ''}`);
  P.toast(msg, 'err', 4200);
}

/* ============================================================
   1. 데이터 상태
   ============================================================ */

async function loadStatus() {
  try {
    const st = await api.getStatus();
    S.status = st;
    applyStatus(st);
    P.clearBanner('err-데이터 상태 조회');
  } catch (e) {
    handleError(e, '데이터 상태 조회');
    P.renderDataStatus(null);
  }
}

function applyStatus(st) {
  $('lastSync').textContent = st.last_sync || '—';
  $('lastTrade').textContent = st.latest_trade_date || '—';
  const auto = st.auto_sync || {};
  $('autoTime').textContent = auto.time || '미등록';
  $('autoSync').checked = !!auto.registered;

  const dot = $('dataDot');
  dot.className = 'dot' + (st.available === false ? ' err' : (st.result === 'stale' ? ' stale' : ''));

  P.renderDataStatus(st, api.isFallback() ? api.fallbackNote() : '');

  if (st.available === false) {
    P.banner('no-data', 'err',
      '<b>marcap 데이터가 없습니다.</b> 종목 데이터 없이는 백테스트를 실행할 수 없습니다.<br>' +
      '프로젝트 폴더에서 <code>update_marcap.bat</code> 을 실행해 저장소를 내려받은 뒤 이 페이지를 새로고침하세요.',
      false);
  } else {
    P.clearBanner('no-data');
  }
}

async function doSync(btn) {
  btn.classList.add('spin'); btn.disabled = true;
  P.progress(null);
  $('dataDot').className = 'dot stale';
  try {
    const st = await api.postSync();
    S.status = st;
    applyStatus(st);
    P.toast(`데이터 갱신 완료 — 최신 거래일 ${st.latest_trade_date || '—'}`, 'ok');
    P.clearBanner('err-데이터 갱신');
  } catch (e) {
    handleError(e, '데이터 갱신');
    $('dataDot').className = 'dot err';
  } finally {
    btn.classList.remove('spin'); btn.disabled = false;
    P.progress(false);
  }
}

/** 브라우저는 Windows 작업 스케줄러를 건드릴 수 없다 → 실행 방법을 안내한다 */
function explainAutoSync(wantOn) {
  const reg = !!(S.status && S.status.auto_sync && S.status.auto_sync.registered);
  $('autoSync').checked = reg;   // 토글을 원래대로 되돌린다
  P.modal(
    wantOn ? '매일 자동 갱신 등록' : '매일 자동 갱신 해제',
    `<p>브라우저에서는 Windows 작업 스케줄러를 직접 등록하거나 해제할 수 없습니다.
     아래 배치 파일을 <b>관리자 권한 명령 프롬프트</b>에서 실행하세요.</p>
     <code class="cmd">${wantOn ? 'setup_daily_update.bat' : 'remove_daily_update.bat'}</code>
     <p>실행한 뒤 이 페이지를 새로고침하면 상태가 반영됩니다.
     현재 등록 상태: <b>${reg ? '등록됨' : '미등록'}</b>${reg && S.status.auto_sync.time ? ` (매일 ${P.esc(S.status.auto_sync.time)})` : ''}.</p>`,
    [{ label: '상태 새로고침', onClick: loadStatus }, { label: '닫기', primary: true }],
  );
}

/* ============================================================
   2. 지표
   ============================================================ */

async function loadIndicators() {
  try {
    S.specs = await api.listIndicators();
    P.clearBanner('err-지표 목록 조회');
  } catch (e) {
    handleError(e, '지표 목록 조회');
    S.specs = [];
  }
}

function specOf(type) {
  return S.specs.find((s) => s.key === type) || { key: type, label: type, params: [], overlay: true };
}

/** strategy.indicators → 화면/차트용 인스턴스 배열 */
function rebuildActive() {
  const list = (S.draft && Array.isArray(S.draft.indicators)) ? S.draft.indicators : [];
  S.active = list
    .filter((e) => e && e.plot !== false)
    .map((e, i) => {
      const spec = specOf(e.type || e.key);
      const params = {};
      for (const p of spec.params || []) params[p.name] = (e[p.name] ?? p.default);
      return {
        key: P.indKey(spec, params),
        type: spec.key,
        params,
        label: e.key || P.indLabel(spec, params),
        overlay: spec.overlay !== false,
        slot: i,
      };
    });
}

function drawChips() {
  P.renderChips(S.specs, S.active, {
    onToggleActive: (key) => {
      const idx = S.active.findIndex((a) => a.key === key);
      if (idx < 0) return;
      const a = S.active[idx];
      S.draft.indicators = (S.draft.indicators || []).filter((e) => {
        const spec = specOf(e.type || e.key);
        const params = {};
        for (const p of spec.params || []) params[p.name] = (e[p.name] ?? p.default);
        return P.indKey(spec, params) !== a.key;
      });
      afterIndicatorChange();
    },
    onAddDefault: (type) => {
      const spec = specOf(type);
      const entry = { key: '', type: spec.key, plot: true };
      for (const p of spec.params || []) entry[p.name] = p.default;
      entry.key = P.indLabel(spec, entry);
      (S.draft.indicators ||= []).push(entry);
      afterIndicatorChange();
    },
    onAdd: openIndicatorDialog,
  }, (slot) => (S.chart ? S.chart.slotColor(slot) : 'currentColor'));
}

function afterIndicatorChange() {
  markDirty();
  rebuildActive();
  drawChips();
  P.renderJson(S.draft);
  loadChart();
}

/** "+ 지표 추가" — 파라미터 입력 다이얼로그 */
function openIndicatorDialog() {
  if (!S.specs.length) {
    P.modal('지표 목록 없음',
      '<p>서버에서 지표 목록을 받지 못했습니다. <code>GET /api/indicators</code> 응답을 확인하세요.</p>');
    return;
  }
  const opts = S.specs.map((s) => `<option value="${P.esc(s.key)}">${P.esc(s.label || s.key)} (${P.esc(s.key)})</option>`).join('');
  P.modal('지표 추가',
    `<p>추가할 지표와 파라미터를 지정하세요. 목록은 <code>GET /api/indicators</code> 응답에서 가져옵니다.</p>
     <div class="fld"><label for="dlgType"><b>지표</b></label>
       <select class="inp wide" id="dlgType">${opts}</select><span class="unit"></span></div>
     <div id="dlgParams"></div>`,
    [
      { label: '취소' },
      {
        label: '추가', primary: true, keepOpen: true, onClick: () => {
          const type = $('dlgType').value;
          const spec = specOf(type);
          const entry = { key: '', type: spec.key, plot: true };
          for (const p of spec.params || []) {
            const el = $('dlgP_' + p.name);
            let v = el ? el.value : p.default;
            if (p.type === 'int') v = parseInt(v, 10);
            else if (p.type === 'float' || p.type === 'number') v = parseFloat(v);
            entry[p.name] = Number.isNaN(v) ? p.default : v;
          }
          entry.key = P.indLabel(spec, entry);
          (S.draft.indicators ||= []).push(entry);
          P.closeModal();
          afterIndicatorChange();
          P.toast(`${entry.key} 추가됨`, 'ok');
        },
      },
    ]);

  const paint = () => {
    const spec = specOf($('dlgType').value);
    $('dlgParams').innerHTML = (spec.params || []).map((p) => {
      if (Array.isArray(p.options)) {
        const os = p.options.map((o) => `<option value="${P.esc(o)}"${o === p.default ? ' selected' : ''}>${P.esc(o)}</option>`).join('');
        return `<div class="fld"><label for="dlgP_${P.esc(p.name)}"><b>${P.esc(p.name)}</b></label>
          <select class="inp wide" id="dlgP_${P.esc(p.name)}">${os}</select><span class="unit"></span></div>`;
      }
      return `<div class="fld"><label for="dlgP_${P.esc(p.name)}"><b>${P.esc(p.name)}</b></label>
        <input class="inp" id="dlgP_${P.esc(p.name)}" type="number"
          value="${P.esc(p.default)}"${p.min !== undefined ? ` min="${P.esc(p.min)}"` : ''}${p.max !== undefined ? ` max="${P.esc(p.max)}"` : ''}${p.step !== undefined ? ` step="${P.esc(p.step)}"` : ''}>
        <span class="unit"></span></div>`;
    }).join('') || '<p class="hint">이 지표는 파라미터가 없습니다.</p>';
  };
  $('dlgType').addEventListener('change', paint);
  paint();
}

/* ============================================================
   3. 전략
   ============================================================ */

async function loadStrategies(selectId) {
  try {
    S.strategies = await api.listStrategies();
    P.clearBanner('err-전략 목록 조회');
  } catch (e) {
    handleError(e, '전략 목록 조회');
    S.strategies = [];
  }
  const id = selectId || S.strategyId || (S.strategies[0] && S.strategies[0].id);
  P.renderStrategyList(S.strategies, id, selectStrategy);
  if (id && id !== S.strategyId) await selectStrategy(id);
}

async function selectStrategy(id) {
  if (S.dirty && id !== S.strategyId) {
    // 저장하지 않은 편집이 있으면 알려 준다 (조용히 버리지 않는다)
    const go = window.confirm('저장하지 않은 파라미터 변경이 있습니다. 버리고 다른 전략을 열까요?');
    if (!go) { P.renderStrategyList(S.strategies, S.strategyId, selectStrategy); return; }
  }
  S.strategyId = id;
  P.renderStrategyList(S.strategies, id, selectStrategy);
  try {
    const st = await api.getStrategy(id);
    S.strategy = st;
    S.draft = structuredClone(st);
    S.dirty = false;
    P.markTabDirty('pnJson', false);
    P.fillForm(S.draft);
    rebuildActive();
    drawChips();
    P.renderJson(S.draft);
    P.clearBanner('err-전략 불러오기');
    await loadChart();
  } catch (e) {
    handleError(e, '전략 불러오기');
  }
}

function markDirty() {
  S.dirty = true;
  P.markTabDirty('pnJson', true);
}

/** 폼 → draft 반영 (300ms 디바운스 후 호출됨) */
function onFormChange() {
  if (!S.draft) return;
  S.draft = P.readForm(S.draft);
  markDirty();
  P.renderJson(S.draft);
}

async function saveStrategy() {
  if (!S.draft) return;
  P.clearFieldErrors();
  P.progress(null);
  try {
    await api.putStrategy(S.strategyId, S.draft);
    S.strategy = structuredClone(S.draft);
    S.dirty = false;
    P.markTabDirty('pnJson', false);
    P.clearBanner('err-전략 저장');
    P.clearBanner('validate');
    P.toast('전략을 저장했습니다.', 'ok');
    await loadStrategies(S.strategyId);
  } catch (e) {
    if (e instanceof ApiError && e.status === 422 && e.errors) {
      const rest = P.showFieldErrors(e.errors);
      const list = rest.map((r) => `<li><code>${P.esc(r.path || '')}</code> ${P.esc(r.message || '')}</li>`).join('');
      P.banner('validate', 'err',
        `<b>전략 검증 실패 (${e.errors.length}건)</b> — 표시된 항목을 고친 뒤 다시 저장하세요.` +
        (list ? `<ul>${list}</ul>` : ''));
      P.toast(`검증 실패 ${e.errors.length}건`, 'err');
    } else {
      handleError(e, '전략 저장');
    }
  } finally {
    P.progress(false);
  }
}

async function cloneStrategy() {
  if (!S.draft) return;
  const copy = structuredClone(S.draft);
  copy.id = `${copy.id}_copy_${Date.now().toString(36)}`;
  copy.name = `${copy.name} (복사본)`;
  try {
    await api.createStrategy(copy);
    P.toast('전략을 복제했습니다.', 'ok');
    await loadStrategies(copy.id);
  } catch (e) {
    handleError(e, '전략 복제');
  }
}

function exportStrategy() {
  if (!S.draft) return;
  const blob = new Blob([JSON.stringify(S.draft, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${S.draft.id || 'strategy'}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  P.toast(`${a.download} 내려받기`, 'ok');
}

function newStrategy() {
  if (!S.strategies.length && !S.draft) {
    P.modal('새 전략', '<p>기준이 될 전략이 없습니다. 먼저 <code>strategies/</code> 폴더에 전략 JSON 을 하나 두거나, AI 전략 생성 탭을 사용하세요.</p>');
    return;
  }
  cloneStrategy();
}

/* ============================================================
   4. 차트
   ============================================================ */

/**
 * @param {object} o {code, start, end, n, name}  생략하면 현재 종목 + 기간 세그먼트 값
 */
async function loadChart(o = {}) {
  if (!S.chart) return;
  const r = rangeParams();
  const code = o.code || S.symbol.code || firstTradeCode() || '';
  const start = o.start || r.start, end = o.end || r.end, n = o.n || r.n;
  $('chartEmpty').hidden = false;
  $('chartEmptyMsg').textContent = '차트 데이터를 불러오는 중입니다…';
  try {
    const payload = await api.getChart({
      code, start, end, n,
      indicators: S.active.map((a) => a.key),
      run_id: S.result ? S.result.run_id : undefined,
    });
    S.symbol = { code: payload.code || code, name: payload.name || o.name || '' };
    $('symCode').textContent = S.symbol.code || '—';
    $('symName').textContent = S.symbol.name || (S.symbol.code ? '' : '종목을 선택하세요');
    S.chart.setData(payload, S.active);
    $('chartEmpty').hidden = !!(payload && payload.n);
    if (!(payload && payload.n)) $('chartEmptyMsg').textContent = '이 구간에 표시할 봉이 없습니다. 조회 기간을 넓혀 보세요.';
    P.renderLegend(null, [], () => '', `${S.symbol.code} ${S.symbol.name}`);
    P.clearBanner('err-차트 조회');
  } catch (e) {
    handleError(e, '차트 조회');
    $('chartEmpty').hidden = false;
    $('chartEmptyMsg').textContent = '차트를 불러오지 못했습니다. 화면 위 안내를 확인하세요.';
  }
}

function firstTradeCode() {
  return S.result && S.result.trades && S.result.trades.length ? S.result.trades[0].code : '';
}

/**
 * t 배열(YYYYMMDD 정수)에서 날짜에 해당하는 인덱스.
 * 배열 범위를 벗어나면 -1 을 돌려준다 (0 으로 뭉개면 엉뚱한 구간을 확대하게 된다).
 */
function indexOfDate(t, dateStr) {
  const target = ymdInt(dateStr);
  if (!target || !t || !t.length) return -1;
  if (target < t[0] || target > t[t.length - 1]) return -1;
  let lo = 0, hi = t.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (t[mid] < target) lo = mid + 1; else hi = mid;
  }
  return lo;
}

/** 거래의 시작/끝 날짜 (기준일 ~ 청산일) */
function tradeSpan(t) {
  const fills = t.fills || [];
  return {
    from: t.ref_date || (fills[0] && fills[0].date) || t.exit_date,
    to: t.exit_date || (fills[fills.length - 1] && fills[fills.length - 1].date) || t.ref_date,
  };
}

/** 날짜 문자열에 개월 수를 더한다 */
function shiftMonths(dateStr, months) {
  const d = new Date(dateStr + 'T00:00:00');
  if (Number.isNaN(d.getTime())) return dateStr;
  d.setMonth(d.getMonth() + months);
  return isoOf(d);
}

/**
 * 거래 내역 행 클릭 → 해당 종목·구간으로 차트 이동 + 마커 강조.
 * 거래 구간이 반드시 들어오도록 조회 기간을 거래 기준으로 다시 계산해서 받는다.
 */
async function gotoTrade(no) {
  const trades = (S.result && S.result.trades) || [];
  const t = trades.find((x) => x.no === no);
  if (!t) return;
  S.tradeNo = no;
  $('tradeIdxLabel').textContent = `${no} / ${trades.length}`;
  P.selectTradeRow(no);

  const span = tradeSpan(t);
  const d0 = S.chart.d;
  const covered = d0 && d0.n
    && S.symbol.code === t.code
    && indexOfDate(d0.t, span.from) >= 0
    && indexOfDate(d0.t, span.to) >= 0;

  if (!covered) {
    // 거래 앞뒤로 2개월씩 여유를 두고 다시 받는다 (await 필수 —
    // 기다리지 않으면 새 데이터가 도착하면서 setHighlight 가 지워진다)
    const start = shiftMonths(span.from, -2);
    const end = shiftMonths(span.to, 2);
    const days = Math.max(40, Math.round((ymdDate(end) - ymdDate(start)) / 86400000 * 0.69));
    await loadChart({ code: t.code, name: t.name, start, end, n: days });
  }
  focusTrade(t);
}

function ymdDate(s) { return new Date(s + 'T00:00:00').getTime(); }

function focusTrade(t) {
  const d = S.chart && S.chart.d;
  if (!d || !d.n) return;
  const span = tradeSpan(t);
  const from = indexOfDate(d.t, span.from);
  const to = indexOfDate(d.t, span.to);
  if (from < 0 || to < 0) {
    // 폴백 모드에서는 차트와 거래 내역이 서로 다른 예시 데이터라 날짜가 맞지 않는 게 정상이다
    P.toast(api.isFallback()
      ? '예시 데이터에서는 거래 구간과 차트 구간이 서로 맞지 않습니다. 실제 결과는 백엔드를 띄운 뒤 확인하세요.'
      : `${t.name || t.code} 거래 구간(${span.from} ~ ${span.to})이 차트 데이터 범위 밖입니다.`,
    api.isFallback() ? '' : 'err', 4000);
    return;
  }
  S.chart.focusRange(from, to);
  S.chart.setHighlight({ from, to });
}

function stepTrade(dir) {
  const trades = (S.result && S.result.trades) || [];
  if (!trades.length) { P.toast('먼저 백테스트를 실행하세요.'); return; }
  const cur = S.tradeNo || trades[0].no;
  const idx = Math.max(0, Math.min(trades.length - 1, trades.findIndex((x) => x.no === cur) + dir));
  gotoTrade(trades[idx].no);
}

function setRange(months) {
  S.rangeMonths = months;
  for (const b of $('rangeSeg').querySelectorAll('button')) {
    b.setAttribute('aria-pressed', String(Number(b.dataset.months) === months));
  }
  return loadChart();
}

/* ============================================================
   5. 백테스트
   ============================================================ */

async function runBacktest() {
  if (S.running || !S.draft) return;
  if (S.status && S.status.available === false) {
    P.toast('marcap 데이터가 없어 백테스트를 실행할 수 없습니다.', 'err', 4000);
    return;
  }
  S.running = true;
  const btn = $('btnRun');
  btn.disabled = true;
  $('btnRunLabel').textContent = '실행 중…';

  // 서버가 진행률 SSE 를 지원한다고 알린 경우에만 연결. 아니면 인디터미닛 진행바.
  let es = api.backtestProgressStream(S.status);
  let gotProgress = false;
  P.progress(null);
  if (es) {
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (Number.isFinite(d.done) && Number.isFinite(d.total) && d.total > 0) {
          gotProgress = true;
          P.progress(d.done / d.total * 100);
          if (d.message) $('btnRunLabel').textContent = String(d.message).slice(0, 18);
        }
      } catch { /* 진행률 파싱 실패는 무시하고 인디터미닛 유지 */ }
    };
    es.onerror = () => { es.close(); es = null; if (!gotProgress) P.progress(null); };
  }

  try {
    const res = await api.runBacktest(S.draft, S.draft.period);
    S.result = res;
    applyResult(res);
    P.clearBanner('err-백테스트 실행');
    P.toast(`백테스트 완료 — ${res.metrics.trades}거래 · ${P.pct1(res.metrics.total_return_pct)} · ${(res.elapsed_sec ?? 0).toFixed(1)}초`, 'ok', 3800);
  } catch (e) {
    if (e instanceof ApiError && e.status === 422 && e.errors) {
      P.showFieldErrors(e.errors);
      P.banner('validate', 'err', `<b>전략 검증 실패 (${e.errors.length}건)</b> — 파라미터를 고친 뒤 다시 실행하세요.`);
    } else {
      handleError(e, '백테스트 실행');
    }
  } finally {
    if (es) es.close();
    S.running = false;
    btn.disabled = false;
    $('btnRunLabel').textContent = '백테스트 실행';
    P.progress(false);
  }
}

function applyResult(res) {
  P.renderKpis(res.metrics);
  S.eq.set(res.equity || null);
  S.mo.set(res.monthly || null);
  P.renderByStock(res.by_stock);
  P.renderTrades(res.trades, gotoTrade);
  P.renderSignals(res.signals);

  if (Array.isArray(res.warnings) && res.warnings.length) {
    P.banner('warnings', 'warn',
      '<b>백테스트 주의사항</b><ul>' + res.warnings.map((w) => `<li>${P.esc(w)}</li>`).join('') + '</ul>');
  } else {
    P.clearBanner('warnings');
  }

  if (res.trades && res.trades.length) {
    $('tradeIdxLabel').textContent = `1 / ${res.trades.length}`;
    gotoTrade(res.trades[0].no);
  } else {
    $('tradeIdxLabel').textContent = '0 / 0';
    P.banner('no-trades', 'info', '<b>체결된 거래가 없습니다.</b> 조건이 너무 좁을 수 있습니다. 기준일 거래대금·탐색 기간·유효 기간을 완화해 보세요.');
  }
}

/* ============================================================
   6. AI 전략 생성
   ============================================================ */

async function runAi() {
  const prompt = $('aiIn').value.trim();
  const out = $('aiResult');
  if (!prompt) { P.toast('전략 설명을 입력하세요.', 'err'); $('aiIn').focus(); return; }

  const btn = $('btnAI');
  btn.disabled = true;
  $('aiStat').textContent = '변환 중… 자연어 → DSL v1';
  out.innerHTML = '';
  P.progress(null);
  try {
    const r = await api.aiStrategy(prompt, S.draft || undefined);
    if (r && r.ok) {
      S.draft = r.strategy;
      S.strategyId = r.strategy.id || S.strategyId;
      markDirty();
      P.fillForm(S.draft);
      rebuildActive();
      drawChips();
      P.renderJson(S.draft);
      const warn = (r.warnings || []).map((w) => `<li>${P.esc(w)}</li>`).join('');
      out.innerHTML = `<div class="ai-msg ok"><b>변환 완료</b>좌측 파라미터 패널에 로드했습니다. 확인 후 <b>저장</b>을 누르세요.${warn ? `<ul>${warn}</ul>` : ''}</div>`;
      $('aiStat').textContent = '변환 완료 · 스키마 검증 대기';
      P.toast('AI 전략 변환 완료', 'ok');
      await loadChart();
    } else {
      // ok:false 는 정상 응답이다. error 와 how_to 를 그대로 보여 준다.
      out.innerHTML = `<div class="ai-msg err"><b>${P.esc((r && r.error) || 'AI 전략 생성에 실패했습니다.')}</b>` +
        ((r && r.how_to) ? `<pre>${P.esc(r.how_to)}</pre>` : '') + '</div>';
      $('aiStat').textContent = '변환 실패';
    }
  } catch (e) {
    handleError(e, 'AI 전략 생성');
    out.innerHTML = `<div class="ai-msg err"><b>요청 실패</b>${P.esc(e.message)}</div>`;
    $('aiStat').textContent = '변환 실패';
  } finally {
    btn.disabled = false;
    P.progress(false);
  }
}

const AI_EXAMPLE = '20일 안에 거래대금 1000억 넘은 날이 있고, 그 전날은 200억 이하였던 종목 중에서 ' +
  '기준일 시가까지 눌린 종목을 시가에 절반 매수. 매수가보다 10% 더 빠지면 나머지 절반 매수. ' +
  '평단 +10%면 전량 익절, 45일선 닿으면 전량 손절.';

/* ============================================================
   7. 부트스트랩
   ============================================================ */

function wire() {
  bindThemeToggle($('themeSeg'));
  P.bindModal();

  P.bindTabs((name) => {
    if (name === 'pnJson' && S.draft) P.renderJson(S.draft);
    if (name === 'pnData') P.renderDataStatus(S.status, api.isFallback() ? api.fallbackNote() : '');
    // 숨겨져 있던 캔버스는 크기가 0이었을 수 있다
    if (name === 'pnTrades') { S.eq.schedule(); S.mo.schedule(); }
  });

  P.bindForm(onFormChange);

  $('btnSync').addEventListener('click', (e) => doSync(e.currentTarget));
  $('autoSync').addEventListener('click', (e) => { e.preventDefault(); explainAutoSync(!e.currentTarget.checked); });
  $('btnRun').addEventListener('click', runBacktest);
  $('btnSave').addEventListener('click', saveStrategy);
  $('btnClone').addEventListener('click', cloneStrategy);
  $('btnExport').addEventListener('click', exportStrategy);
  $('btnNewStrategy').addEventListener('click', newStrategy);
  $('btnAddInd').addEventListener('click', openIndicatorDialog);
  $('btnAI').addEventListener('click', runAi);
  $('btnAiExample').addEventListener('click', () => { $('aiIn').value = AI_EXAMPLE; $('aiIn').focus(); });
  $('prevTrade').addEventListener('click', () => stepTrade(-1));
  $('nextTrade').addEventListener('click', () => stepTrade(1));

  for (const b of $('rangeSeg').querySelectorAll('button')) {
    b.addEventListener('click', () => setRange(Number(b.dataset.months)));
  }

  // 폴백 진입 알림
  window.addEventListener('api:fallback', (e) => {
    $('mockBadge').hidden = false;
    P.banner('fallback', 'warn',
      `<b>백엔드에 연결하지 못했습니다.</b> ${P.esc(e.detail.reason)}<br>` +
      '지금 보이는 숫자는 화면 확인용 <b>예시 데이터</b>이며 실제 백테스트 결과가 아닙니다.',
      false);
  });

  // 삼키면 안 되는 에러들
  window.addEventListener('error', (e) => {
    P.banner('js-error', 'err', `<b>화면 오류</b> — ${P.esc(e.message || '알 수 없는 오류')}<br>브라우저 콘솔을 확인하세요.`);
  });
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason;
    P.banner('js-error', 'err', `<b>처리되지 않은 오류</b> — ${P.esc((r && r.message) || String(r))}`);
  });

  // 테마가 바뀌면 지표 칩의 색 표식도 다시 칠한다
  window.addEventListener('themechange', () => { if (S.specs.length || S.active.length) drawChips(); });
}

function setupCharts() {
  const wrap = $('chartWrap');
  S.chart = new CandleChart(wrap, {
    onHover: (info) => {
      const meta = S.active.map((a) => ({ key: a.key, label: a.label, slot: a.slot }));
      P.renderLegend(info, meta, (slot) => S.chart.slotColor(slot),
        `${S.symbol.code} ${S.symbol.name}`);
      P.renderTooltip(info, wrap.getBoundingClientRect());
    },
  });
  S.eq = new MiniChart($('eq'), 'equity');
  S.mo = new MiniChart($('mo'), 'monthly');

  // playwright 성능 측정용 훅
  window.__app = S;
  window.__chart = S.chart;
}

async function boot() {
  initTheme();
  setupCharts();
  wire();

  if (new URLSearchParams(location.search).has('mock')) {
    api.forceFallback('개발용 예시 데이터 모드입니다 (URL 에 ?mock=1 이 있습니다).');
    $('mockBadge').hidden = false;
  }

  P.renderKpis(null);
  P.renderTrades([], gotoTrade);
  P.renderSignals([]);
  P.renderByStock([]);

  await loadStatus();
  await loadIndicators();
  await loadStrategies();

  window.__ready = true;
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}
