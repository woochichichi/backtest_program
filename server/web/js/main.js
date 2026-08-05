/* ============================================================
   main.js — 부트스트랩 / 애플리케이션 상태
   ------------------------------------------------------------
   흐름:
     status → (필요하면 자동 시세 갱신) → indicators → strategies → strategy → chart
     [백테스트 실행] → 실행 기준 · KPI · 곡선 · 거래 내역 갱신

   피드백 원칙:
     모든 비동기 동작은 대기/진행/성공/실패 네 가지 상태를 모두 화면에 드러낸다.
     성공은 토스트, 실패는 사라지지 않는 배너. 실패는 항상 "다음에 할 일"을 함께 쓴다.
   ============================================================ */

import * as api from './api.js';
import { ApiError } from './api.js';
import { initTheme, bindThemeToggle } from './theme.js';
import { CandleChart, MiniChart } from './chart.js';
import * as P from './panels.js';

const $ = (id) => document.getElementById(id);
const LS_AUTOSYNC = 'autoSyncOnStart';

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
  ran: false,              // 백테스트를 한 번이라도 끝냈는가 (빈 상태 문구 분기)
  tradeNo: 0,
  rangeMonths: 6,
  symbol: { code: '', name: '' },
  running: false,
  syncing: false,
  syncFailed: false,
  noAutoSymbol: false,   // 서버가 code 없는 /api/chart 를 거절했는가
  hiddenInd: new Set(),  // 차트에서만 숨긴 지표 키 (전략 파일은 건드리지 않는다)
  chartSeq: 0,           // 차트 요청 순번 — 늦게 도착한 예전 응답을 버린다
  chartAbort: null,      // 진행 중인 차트 요청 (종목 전환 시 실제로 끊는다)
  histExhausted: false,  // 더 받을 과거가 없다
  lastChartArgs: null,   // 재시도용
  sortKey: '', sortDir: 'asc',   // 거래 내역 정렬 (그룹은 깨지 않는다)
  abort: null,             // 실행 중인 백테스트의 AbortController
  jobId: null,             // 진행률 SSE / 취소용
  cancelling: false,
  longRunAck: false,       // 긴 구간 안내를 이미 확인했는가
  formMode: 'legacy',      // 'params' | 'legacy'
  chart: null, eq: null, mo: null,
};

/* ---------------- 공통 유틸 ---------------- */

const ymdInt = (s) => Number(String(s || '').replace(/-/g, '')) || 0;
const pad = (n) => String(n).padStart(2, '0');
const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const ymdDate = (s) => new Date(s + 'T00:00:00').getTime();

function autoSyncEnabled() {
  try { return localStorage.getItem(LS_AUTOSYNC) !== 'off'; } catch { return true; }
}
function setAutoSyncEnabled(on) {
  try { localStorage.setItem(LS_AUTOSYNC, on ? 'on' : 'off'); } catch { /* 저장 불가 환경 */ }
  const el = $('optAutoSync');
  if (el) el.checked = on;
}

/** 조회 기간 계산. months=0 이면 전체. */
/** 한 달에 들어가는 대략적인 영업일 수 (기간 버튼 → 봉 수 환산) */
const BARS_PER_MONTH = 21;

/** (폴백 mockdata 전용) 조회 기간 계산. 실서버에는 start/end 를 보내지 않는다. */
function rangeParams() {
  const endStr = (S.status && S.status.latest_trade_date) || isoOf(new Date());
  if (!S.rangeMonths) {
    const first = (S.status && S.status.first_trade_date) || '2015-01-02';
    return { start: first, end: endStr, n: 3000 };
  }
  const end = new Date(endStr + 'T00:00:00');
  const start = new Date(end);
  start.setMonth(start.getMonth() - S.rangeMonths);
  return { start: isoOf(start), end: endStr, n: Math.round(S.rangeMonths * 21) };
}

/**
 * 실패를 화면에 드러낸다.
 * error 는 크게, 조치는 그 아래, 기술적 detail 은 "자세히" 안에.
 */
function handleError(e, ctx) {
  if (e && e.aborted) return;                 // 사용자가 취소한 건 오류가 아니다
  // "데이터 없음" 은 이미 상단에 전용 안내가 떠 있다. 같은 말을 두 번 하지 않는다.
  if (e instanceof ApiError && e.kind === 'nodata' && document.querySelector('[data-bid="no-data"]')) {
    P.toast(e.message, 'err', 3600);
    return;
  }
  console.error(`[${ctx}]`, e);
  const isApi = e instanceof ApiError;
  const tone = isApi && (e.kind === 'network' || e.kind === 'nodata' || e.kind === 'slow') ? 'warn' : 'err';
  P.banner('err-' + ctx, tone, P.errorHtml(e, ctx));
  P.toast((e && e.message) || String(e), 'err', 4200);
}

/* ============================================================
   1. 데이터 상태 · 시세 갱신
   ============================================================ */

async function loadStatus() {
  try {
    const st = await api.getStatus();
    S.status = st;
    applyStatus(st);
    P.clearBanner('err-데이터 상태 확인');
    return st;
  } catch (e) {
    handleError(e, '데이터 상태 확인');
    P.renderDataStatus(null);
    return null;
  }
}

function applyStatus(st) {
  const f = P.freshness(st.last_sync);
  // 색만으로 전달하지 않기 위해 신선도 텍스트를 항상 함께 쓴다
  $('freshLabel').textContent = st.available === false ? '없음' : f.label;
  $('lastSync').textContent = st.last_sync ? String(st.last_sync).slice(0, 16) : '';
  $('lastTrade').textContent = st.latest_trade_date || '—';
  $('dataDot').className = 'dot' + (st.available === false ? ' err' : (f.tone === 'ok' ? '' : ' ' + f.tone));
  $('pillSync').title = st.available === false
    ? '주가 데이터가 아직 없습니다'
    : `마지막으로 시세를 받은 시각: ${st.last_sync || '기록 없음'} (${f.label})`;

  P.renderDataStatus(st, api.isFallback() ? api.fallbackNote() : '');

  if (st.available === false) {
    P.banner('no-data', 'err',
      '<b>주가 데이터가 아직 없습니다.</b> 데이터 없이는 백테스트를 실행할 수 없습니다.<br>' +
      '프로젝트 폴더에서 <code>update_marcap.bat</code> 을 실행해 데이터를 내려받은 뒤 이 화면을 새로고침하세요. ' +
      '용량이 약 1.8GB 라 처음 한 번은 시간이 걸립니다.',
      false);
  } else {
    P.clearBanner('no-data');
  }
  updateNextStep();
}

/**
 * 시세 갱신. 화면을 막지 않는다.
 * @param {object} o {auto:boolean}
 */
async function startSync(o = {}) {
  if (S.syncing) { P.toast('이미 시세를 받는 중입니다.'); return; }
  S.syncing = true;
  S.syncFailed = false;

  const btn = $('btnSync');
  btn.classList.add('spin'); btn.disabled = true;
  $('btnSyncLabel').textContent = '받는 중…';
  $('dataDot').className = 'dot stale';
  P.progress(null);

  const t0 = Date.now();
  let logTail = '';
  const paint = () => {
    const sec = Math.round((Date.now() - t0) / 1000);
    P.banner('sync', 'info',
      `<div class="sync-line"><span class="spinner" aria-hidden="true"></span>` +
      `<b>${o.auto ? '오늘 시세를 받는 중입니다' : '시세를 받는 중입니다'}</b>` +
      `<span class="sync-el">${sec}초 경과</span></div>` +
      `<div style="font-size:12px;color:var(--tx-sub);margin-top:2px">받는 동안에도 화면은 그대로 쓸 수 있습니다.</div>` +
      (logTail ? `<code class="sync-log">${P.esc(logTail)}</code>` : ''),
      false);
  };
  paint();
  const tick = setInterval(paint, 1000);

  // 서버가 진행 상황을 알려 주면 log_tail 까지 보여 준다.
  // 지원하지 않는 서버면 getSyncStatus() 가 null 을 돌려주고 경과 시간만 표시된다.
  const pollOnce = async () => {
    const s = await api.getSyncStatus();
    if (s && s.log_tail) { logTail = String(s.log_tail).split('\n').slice(-3).join('\n'); paint(); }
  };
  pollOnce();                              // 첫 진행 상황은 기다리지 않고 바로 보여 준다
  const poll = setInterval(pollOnce, 2000);

  try {
    const st = await api.postSync();
    S.status = st;
    applyStatus(st);
    P.clearBanner('sync');
    P.clearBanner('sync-failed');
    P.toast(`시세를 받았습니다 — 최신 거래일 ${st.latest_trade_date || '—'}`, 'ok', 3600);
  } catch (e) {
    S.syncFailed = true;
    const f = P.freshness(S.status && S.status.last_sync);
    P.clearBanner('sync');
    // 실패해도 앱은 계속 쓸 수 있다. 다만 어느 시점 데이터인지 분명히 밝힌다.
    P.banner('sync-failed', 'warn',
      `<b>오늘 시세를 받지 못했습니다. 지금은 ${P.esc(f.label)} 받은 데이터로 보고 있습니다.</b><br>` +
      P.errorHtml(e), false);
    P.toast('시세 갱신 실패 — 이전 데이터로 계속 사용합니다.', 'err', 4200);
    $('dataDot').className = 'dot err';
  } finally {
    clearInterval(tick);
    if (poll) clearInterval(poll);
    S.syncing = false;
    btn.classList.remove('spin'); btn.disabled = false;
    $('btnSyncLabel').textContent = '지금 갱신';
    P.progress(false);
    updateNextStep();
  }
}

/**
 * 켤 때 자동 갱신.
 * · available:false → 최초 1.8GB 다운로드라 자동으로 하지 않는다 (안내만)
 * · 오늘 이미 받았으면 하지 않는다
 */
function maybeAutoSync() {
  if (!S.status || S.status.available === false) return;
  if (!autoSyncEnabled()) return;
  if (P.freshness(S.status.last_sync).fresh) return;
  startSync({ auto: true });   // 일부러 await 하지 않는다 — 화면을 막지 않기 위해
}

/** 톱니 → 데이터 받기 설정. 두 가지 자동 갱신의 차이를 한 줄로 구분해 준다. */
function openSettings() {
  const auto = (S.status && S.status.auto_sync) || {};
  const on = autoSyncEnabled();
  P.modal('데이터 받기 설정',
    `<label class="opt" style="margin-bottom:14px">
       <span class="sw"><input type="checkbox" id="dlgAuto"${on ? ' checked' : ''}><i></i></span>
       <span class="opt-tx"><b>켤 때 자동으로 시세 받기</b>
         <span>프로그램을 열었을 때 오늘 시세를 아직 안 받았으면 자동으로 받습니다.
         이미 받아 둔 데이터를 갱신하는 것이라 보통 몇 초면 끝나고, 받는 동안에도 화면은 그대로 쓸 수 있습니다.</span>
       </span>
     </label>
     <div style="border-top:1px dashed var(--bd);padding-top:12px">
       <b style="font-size:12.5px">매일 정해진 시각에 자동으로 받기</b>
       <p style="font-size:12px;color:var(--tx-sub);margin-top:3px">
         현재 상태: <b>${auto.registered ? `등록됨 (매일 ${P.esc(auto.time || '')})` : '등록 안 됨'}</b>
       </p>
       <p style="font-size:12px;color:var(--tx-sub);margin-top:6px">
         이건 <b>프로그램을 켜지 않아도</b> 컴퓨터가 정해진 시각에 알아서 받는 기능입니다.
         위의 '켤 때 자동으로 받기'는 <b>프로그램을 열었을 때만</b> 동작하니 서로 다른 기능입니다.
       </p>
       <p style="font-size:12px;color:var(--tx-sub);margin-top:6px">
         브라우저에서는 Windows 작업 스케줄러를 건드릴 수 없어, 아래 파일을
         <b>관리자 권한 명령 프롬프트</b>에서 직접 실행해야 합니다.
       </p>
       <code class="cmd">${auto.registered ? 'remove_daily_update.bat' : 'setup_daily_update.bat'}</code>
     </div>`,
    [
      { label: '상태 새로고침', onClick: loadStatus },
      { label: '확인', primary: true, onClick: () => setAutoSyncEnabled($('dlgAuto').checked) },
    ]);
}

/* ============================================================
   2. 지금 할 일 안내
   ============================================================ */

function updateNextStep() {
  if (!S.status) return;
  if (S.status.available === false) {
    P.renderNextStep({
      num: '1', tone: 'err',
      title: '먼저 주가 데이터를 받아야 합니다',
      desc: '프로젝트 폴더의 <code>update_marcap.bat</code> 을 두 번 눌러 실행한 뒤, 이 화면을 새로고침하세요. 약 1.8GB 라 처음 한 번은 몇 분 걸립니다.',
    });
    return;
  }
  if (!S.ran) {
    P.renderNextStep({
      num: '1', tone: 'info',
      title: '왼쪽에서 전략을 고르고 [백테스트 실행] 을 누르세요',
      desc: '조건을 바꾸고 싶으면 왼쪽 파라미터를 수정하면 됩니다. 실행하면 언제 사고팔았는지와 수익률이 나옵니다.',
      action: { label: '백테스트 실행', act: 'run' },
    });
    return;
  }
  P.renderNextStep(null);   // 한 번 실행한 뒤에는 안내를 치운다
}

/* ============================================================
   3. 지표
   ============================================================ */

async function loadIndicators() {
  try {
    S.specs = await api.listIndicators();
    P.clearBanner('err-지표 목록 확인');
  } catch (e) {
    handleError(e, '지표 목록 확인');
    S.specs = [];
  }
}

function specOf(type) {
  return S.specs.find((s) => s.key === type) || { key: type, label: type, params: [], overlay: true };
}

/** 저장된 전략에 정의된 지표 키 집합 (화면에서 임시로 추가한 것과 구분) */
function strategyIndKeys() {
  const out = new Set();
  const list = (S.strategy && Array.isArray(S.strategy.indicators)) ? S.strategy.indicators : [];
  for (const e of list) {
    const spec = specOf(e.type || e.key);
    const params = {};
    for (const p of spec.params || []) params[p.name] = (e[p.name] ?? p.default);
    out.add(P.indKey(spec, params));
  }
  return out;
}

function rebuildActive() {
  const list = (S.draft && Array.isArray(S.draft.indicators)) ? S.draft.indicators : [];
  const fromStrat = strategyIndKeys();
  S.active = list
    .filter((e) => e && e.plot !== false)
    .map((e) => {
      const spec = specOf(e.type || e.key);
      const params = {};
      for (const p of spec.params || []) params[p.name] = (e[p.name] ?? p.default);
      const key = P.indKey(spec, params);
      return {
        key,
        type: spec.key,
        params,
        // 라벨은 전략 정의든 화면 추가든 같은 짧은 형식으로 통일한다
        label: P.indLabel(spec, params),
        longLabel: P.indLongLabel(spec, params),
        overlay: spec.overlay !== false,
        fromStrategy: fromStrat.has(key),
      };
    })
    .filter((a) => !S.hiddenInd.has(a.key))
    .map((a, i) => ({ ...a, slot: i }));
}

/** 이미 같은 종류·같은 파라미터의 지표가 있는가 */
function indExists(key) {
  return S.active.some((a) => a.key === key) || S.hiddenInd.has(key);
}

/** 지표를 draft 에서 완전히 제거한다 */
function removeIndicatorFromDraft(key) {
  S.draft.indicators = (S.draft.indicators || []).filter((e) => {
    const spec = specOf(e.type || e.key);
    const params = {};
    for (const p of spec.params || []) params[p.name] = (e[p.name] ?? p.default);
    return P.indKey(spec, params) !== key;
  });
}

/**
 * 지표 제거 요청.
 * 전략에 정의된 지표는 조용히 전략 파일을 바꾸지 않는다 — 무엇을 할지 물어본다.
 */
function requestRemoveIndicator(key) {
  const a = S.active.find((x) => x.key === key);
  if (!a) return;
  if (!a.fromStrategy) {
    removeIndicatorFromDraft(key);
    afterIndicatorChange(`${a.label} 제거됨`);
    return;
  }
  P.modal('전략에 정의된 지표입니다',
    `<p><b>${P.esc(a.label)}</b> 는 이 전략(<b>${P.esc((S.draft && S.draft.name) || '')}</b>)에 정의된 지표입니다.</p>
     <p>차트에서만 감출지, 전략에서도 지울지 골라 주세요.
     전략에서 지우면 <b>저장</b>할 때 전략 파일이 실제로 바뀝니다.</p>`,
    [
      { label: '취소' },
      {
        label: '차트에서만 숨기기', onClick: () => {
          S.hiddenInd.add(key);
          afterIndicatorChange(`${a.label} 숨김 (전략은 그대로)`);
        },
      },
      {
        label: '전략에서도 제거', primary: true, onClick: () => {
          removeIndicatorFromDraft(key);
          afterIndicatorChange(`${a.label} 전략에서 제거됨 — 저장해야 반영됩니다`);
        },
      },
    ]);
}

function drawChips() {
  P.renderChips(S.specs, S.active, {
    onToggleActive: (key) => requestRemoveIndicator(key),
    onAddDefault: (type) => {
      const spec = specOf(type);
      const entry = { key: '', type: spec.key, plot: true };
      for (const p of spec.params || []) entry[p.name] = p.default;
      entry.key = P.indLabel(spec, entry);
      const key = P.indKey(spec, entry);
      if (S.hiddenInd.has(key)) { S.hiddenInd.delete(key); afterIndicatorChange(`${entry.key} 다시 표시`); return; }
      if (indExists(key)) { P.toast(`${entry.key} 은 이미 추가되어 있습니다.`, 'err', 2600); return; }
      (S.draft.indicators ||= []).push(entry);
      afterIndicatorChange(`${entry.key} 표시`);
    },
    onAdd: openIndicatorManager,
  }, (slot) => (S.chart ? S.chart.slotColor(slot) : 'currentColor'));
}

function afterIndicatorChange(msg) {
  markDirty();
  rebuildActive();
  drawChips();
  P.renderJson(S.draft);
  if (msg) P.toast(msg, 'ok', 1600);
  loadChart();
}

function openIndicatorDialog() {
  if (!S.specs.length) {
    P.modal('지표 목록을 받지 못했습니다',
      '<p>프로그램 서버에서 지표 목록을 받지 못했습니다. 서버가 켜져 있는지 확인한 뒤 화면을 새로고침하세요.</p>');
    return;
  }
  const opts = S.specs.map((s) => `<option value="${P.esc(s.key)}">${P.esc(s.label || s.key)} (${P.esc(s.key)})</option>`).join('');
  P.modal('지표 추가',
    `<p>차트에 함께 그릴 지표를 고르세요. 목록은 프로그램이 실제로 계산할 수 있는 지표만 보여 줍니다.</p>
     <div class="fld"><label for="dlgType"><b>지표</b></label>
       <select class="inp wide" id="dlgType">${opts}</select><span class="unit"></span></div>
     <div id="dlgParams"></div>`,
    [
      { label: '취소' },
      {
        label: '추가', primary: true, keepOpen: true, onClick: () => {
          const spec = specOf($('dlgType').value);
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
          afterIndicatorChange(`${entry.key} 추가됨`);
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
    }).join('') || '<p class="hint">이 지표는 따로 정할 값이 없습니다.</p>';
  };
  $('dlgType').addEventListener('change', paint);
  paint();
}

/* ============================================================
   4. 전략
   ============================================================ */

async function loadStrategies(selectId) {
  P.renderStrategyListLoading();
  try {
    S.strategies = await api.listStrategies();
    P.clearBanner('err-전략 목록 확인');
  } catch (e) {
    handleError(e, '전략 목록 확인');
    S.strategies = [];
  }
  const id = selectId || S.strategyId || (S.strategies[0] && S.strategies[0].id);
  P.renderStrategyList(S.strategies, id, requestSelectStrategy);
  if (id && id !== S.strategyId) await selectStrategy(id);
  else P.renderStrategyList(S.strategies, S.strategyId, requestSelectStrategy);
}

/** 저장 안 된 변경이 있으면 확인 모달을 띄운 뒤 전환한다 */
function requestSelectStrategy(id) {
  if (id === S.strategyId) return;
  if (!S.dirty) { selectStrategy(id); return; }
  const target = S.strategies.find((x) => x.id === id);
  P.modal('저장하지 않은 변경이 있습니다',
    `<p>지금 <b>${P.esc((S.draft && S.draft.name) || S.strategyId)}</b> 의 파라미터를 고쳤지만 아직 저장하지 않았습니다.</p>
     <p><b>${P.esc((target && target.name) || id)}</b> 로 넘어가면 이 변경은 사라집니다.</p>`,
    [
      { label: '취소' },
      { label: '저장하고 이동', onClick: async () => { await saveStrategy({ silent: true }); selectStrategy(id); } },
      { label: '버리고 이동', primary: true, onClick: () => selectStrategy(id) },
    ]);
}

async function selectStrategy(id) {
  S.strategyId = id;
  P.renderStrategyList(S.strategies, id, requestSelectStrategy);
  try {
    const st = await api.getStrategy(id);
    S.strategy = st;
    S.draft = structuredClone(st);
    setDirty(false);
    S.formMode = P.renderParamForm(S.draft);   // params 있으면 선언 기반, 없으면 기존 폼
    P.bindForm(onFormChange);                  // 폼이 새로 그려졌으므로 리스너를 다시 건다
    P.fillForm(S.draft);
    P.renderFieldHints();
    checkForm();
    updateRevertButton();
    rebuildActive();
    drawChips();
    P.renderJson(S.draft);
    syncResolutionNote();
    P.clearBanner('err-전략 불러오기');
    await loadChart();
  } catch (e) {
    handleError(e, '전략 불러오기');
  }
}

function setDirty(on) {
  S.dirty = on;
  P.markTabDirty('pnJson', on);
  P.setDirtyBadge(on);
  updateRevertButton();
}

/**
 * 되돌리기 버튼의 라벨/활성 상태.
 * params 가 있으면 "PDF 기본값으로 되돌리기"(선언된 default 로),
 * 없으면 "저장된 값으로 되돌리기"(마지막 저장 상태로) — 동작이 다르므로 라벨도 달라야 한다.
 */
function updateRevertButton() {
  const btn = $('btnRevert'), lab = $('btnRevertLabel');
  if (!btn || !lab) return;
  if (P.hasParams(S.draft)) {
    const n = P.countChangedFromDefault(S.draft);
    lab.textContent = 'PDF 기본값으로 되돌리기';
    btn.title = n
      ? `${n}개 항목이 기본값과 다릅니다. 전략에 선언된 기본값으로 모두 되돌립니다.`
      : '모든 값이 이미 기본값과 같습니다.';
    btn.disabled = n === 0;
  } else {
    lab.textContent = '저장된 값으로';
    btn.title = '마지막으로 저장된 값으로 되돌립니다.';
    btn.disabled = !S.dirty;
  }
}
function markDirty() { setDirty(true); }

/** 폼 → draft 반영 (300ms 디바운스) */
function onFormChange() {
  if (!S.draft) return;
  S.draft = P.readForm(S.draft);
  markDirty();
  P.renderJson(S.draft);
  checkForm();
  updateRevertButton();
  syncResolutionNote();
}

/** 프런트에서 즉시 잡을 수 있는 문제를 인라인으로 보여 준다 */
function checkForm() {
  const issues = P.formIssues();
  P.showFormIssues(issues);
  const errs = issues.filter((i) => i.level === 'err').length;
  $('btnSave').classList.toggle('btn-danger', errs > 0);
  return issues;
}

/**
 * 전략 JSON 이 1분봉을 요청하더라도 화면은 "일봉으로 실행됨"을 정확히 보여준다.
 * 여기서 1분봉을 지원하는 듯한 인상을 주지 않는다.
 */
function syncResolutionNote() {
  const sel = $('p_res');
  const want = (S.draft && (S.draft.execution || {}).resolution) || '1d';
  // 셀렉트는 항상 일봉으로 보인다 (1분봉 option 은 disabled)
  if (sel) sel.value = '1d';
  const note = $('resNote');
  if (!note) return;   // params 기반 폼에는 이 안내가 없다
  note.innerHTML = want !== '1d'
    ? `<span class="soon">추후 지원 예정</span>
       이 전략의 JSON 에는 <b>${P.esc(want)}</b> 체결이 적혀 있지만, 분봉 데이터가 없어
       <b>일봉으로 계산</b>합니다. 결과의 '이 결과를 읽는 법' 에 실제 적용된 기준이 표시됩니다.`
    : `<span class="soon">추후 지원 예정</span>
       분봉 매매는 아직 준비 중입니다. 지금은 <b>일봉</b>으로만 계산하며,
       하루 안에서 저가와 고가 중 무엇이 먼저였는지는 알 수 없습니다.`;
}

async function saveStrategy(o = {}) {
  if (!S.draft) return;
  const issues = checkForm();
  const errs = issues.filter((i) => i.level === 'err');
  if (errs.length) {
    P.focusFirstError();
    P.toast(`고쳐야 할 값이 ${errs.length}개 있습니다.`, 'err');
    return;
  }
  // 되돌릴 수 없는 덮어쓰기라 확인을 받는다
  if (!o.silent && !o.confirmed) {
    const diffs = P.diffStrategies(S.strategy, S.draft);
    P.modal('이 내용으로 저장할까요?',
      `<p><b>${P.esc(S.draft.name || S.strategyId)}</b> 파일을 덮어씁니다. 이전 값은 되돌릴 수 없습니다.</p>` +
      P.renderDiffTable(diffs),
      [{ label: '취소' }, { label: '저장', primary: true, onClick: () => saveStrategy({ confirmed: true }) }]);
    return;
  }

  const btn = $('btnSave');
  btn.disabled = true; btn.textContent = '저장 중…';
  P.progress(null);
  try {
    await api.putStrategy(S.strategyId, S.draft);
    S.strategy = structuredClone(S.draft);
    setDirty(false);
    P.clearBanner('err-전략 저장');
    P.clearBanner('validate');
    P.toast('전략을 저장했습니다.', 'ok');
    await loadStrategies(S.strategyId);
  } catch (e) {
    if (e instanceof ApiError && e.status === 422 && e.errors) {
      const rest = P.showFieldErrors(e.errors);
      P.focusFirstError();
      const list = rest.map((r) => `<li><code>${P.esc(r.path || '')}</code> ${P.esc(r.message || '')}</li>`).join('');
      P.banner('validate', 'err',
        `<b>저장하지 못했습니다 — 값 ${e.errors.length}개를 고쳐야 합니다.</b>` +
        '<div class="err-advice">빨간색으로 표시된 항목을 고친 뒤 다시 저장하세요.</div>' +
        (list ? `<ul>${list}</ul>` : ''));
      P.toast(`고쳐야 할 값이 ${e.errors.length}개 있습니다.`, 'err');
    } else {
      handleError(e, '전략 저장');
    }
  } finally {
    btn.disabled = false; btn.textContent = '저장';
    P.progress(false);
  }
}

/**
 * 되돌리기.
 * params 가 있으면 params[].default 로 초기화한다 (서버 reset API 우선, 없으면 클라이언트 폴백).
 * params 가 없으면 기존처럼 마지막 저장 상태로 되돌린다.
 */
async function revertStrategy() {
  if (!S.draft) return;

  if (!P.hasParams(S.draft)) {
    const diffs = P.diffStrategies(S.draft, S.strategy);
    P.modal('저장된 값으로 되돌릴까요?',
      '<p>마지막으로 저장된 상태로 되돌립니다. 지금 고친 내용은 사라집니다.</p>' + P.renderDiffTable(diffs),
      [{ label: '취소' }, {
        label: '되돌리기', primary: true,
        onClick: () => applyRevert(structuredClone(S.strategy), '저장된 값으로 되돌렸습니다.', false),
      }]);
    return;
  }

  const target = P.applyParamDefaults(S.draft);
  const diffs = P.diffStrategies(S.draft, target);
  P.modal('기본값으로 되돌릴까요?',
    `<p>이 전략에 선언된 <b>기본값</b>(PDF 원문 기준)으로 모든 파라미터를 되돌립니다.</p>
     <p class="hint">되돌린 뒤에도 <b>저장</b>을 눌러야 파일에 반영됩니다.</p>` +
    P.renderDiffTable(diffs),
    [{ label: '취소' }, {
      label: '기본값으로 되돌리기', primary: true,
      onClick: async () => {
        // 서버에 reset API 가 있으면 그쪽을 쓴다 (없으면 null → 클라이언트에서 처리)
        let next = null;
        try {
          next = await api.resetStrategy(S.strategyId);
        } catch (e) {
          handleError(e, '기본값 되돌리기');
          return;
        }
        applyRevert(next && next.params ? next : target,
          next ? '서버에서 기본값으로 되돌렸습니다.' : '기본값으로 되돌렸습니다.', true);
      },
    }]);
}

/**
 * @param {boolean} dirty 기본값으로 되돌린 경우 true(저장 전 상태),
 *                        저장된 값으로 되돌린 경우 false(파일과 같아짐)
 */
function applyRevert(next, msg, dirty) {
  S.draft = next;
  P.fillForm(S.draft);
  checkForm();
  rebuildActive(); drawChips();
  P.renderJson(S.draft);
  syncResolutionNote();
  setDirty(!!dirty);
  updateRevertButton();
  P.toast(msg, 'ok');
  loadChart();
}

async function cloneStrategy() {
  if (!S.draft) return;
  const copy = structuredClone(S.draft);
  copy.id = `${copy.id}_copy_${Date.now().toString(36)}`;
  copy.name = `${copy.name} (복사본)`;
  const btn = $('btnClone');
  btn.disabled = true;
  try {
    await api.createStrategy(copy);
    P.toast('전략을 복제했습니다.', 'ok');
    await loadStrategies(copy.id);
  } catch (e) {
    handleError(e, '전략 복제');
  } finally { btn.disabled = false; }
}

function deleteStrategy() {
  if (!S.strategyId) return;
  if (S.strategies.length <= 1) {
    P.modal('삭제할 수 없습니다', '<p>전략이 하나뿐입니다. 최소 한 개는 남아 있어야 합니다.</p>');
    return;
  }
  P.modal('전략을 삭제할까요?',
    `<p><b>${P.esc((S.draft && S.draft.name) || S.strategyId)}</b> 를 삭제합니다.</p>
     <p style="color:var(--danger-tx)">삭제한 전략은 되돌릴 수 없습니다. 필요하면 먼저 <b>내보내기</b>로 파일을 저장해 두세요.</p>`,
    [
      { label: '취소' },
      { label: '먼저 내보내기', onClick: exportStrategy },
      {
        label: '삭제', primary: true, onClick: async () => {
          try {
            await api.deleteStrategy(S.strategyId);
            P.toast('전략을 삭제했습니다.', 'ok');
            S.strategyId = null; setDirty(false);
            await loadStrategies();
          } catch (e) { handleError(e, '전략 삭제'); }
        },
      },
    ]);
}

function exportStrategy() {
  if (!S.draft) return;
  const blob = new Blob([JSON.stringify(S.draft, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${S.draft.id || 'strategy'}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  P.toast(`${a.download} 파일로 저장했습니다.`, 'ok');
}

function newStrategy() {
  if (!S.strategies.length && !S.draft) {
    P.modal('새 전략', '<p>기준이 될 전략이 없습니다. AI 전략 생성 탭에서 한국어로 조건을 설명해 만들어 보세요.</p>');
    return;
  }
  cloneStrategy();
}

/* ============================================================
   5. 차트 · 종목 검색
   ============================================================ */

/** 기간 버튼(3M/6M/1Y/전체) → 보여 줄 봉 수. 0 이면 전체. */
function rangeBars() {
  return S.rangeMonths ? Math.round(S.rangeMonths * BARS_PER_MONTH) : 0;
}

/** 데이터를 다시 받지 않고 뷰포트만 기간 버튼에 맞춘다 */
function applyRangeViewport() {
  if (!S.chart || !S.chart.hasData()) return;
  const bars = rangeBars();
  if (!bars) S.chart.fitAll(false);
  else S.chart.showLast(bars, false);
}

/* ---------------- 점진 로딩 ----------------
   전체 히스토리를 한 번에 받으면 서버가 32개 연도 파일을 모두 읽어야 해서
   12초가 걸린다. 그래서:
     1) 최근 구간만 먼저 받아 즉시 그리고 (0.3초 수준)
     2) 나머지 과거는 배경으로 이어붙이고
     3) 아직 안 받은 과거로 팬하면 그때 추가로 받는다.
   붙이는 동안에도 팬·줌은 막지 않는다.                                     */

const CHART_INITIAL_YEARS = 2;    // 처음에 바로 받는 최근 구간
const CHART_CHUNK_YEARS = 5;      // 배경으로 이어받는 한 덩어리
const CHART_MAX_BARS = 12000;     // 무한정 붙이지 않는다 (약 48년)
const EDGE_TRIGGER_BARS = 60;     // 왼쪽 끝 이만큼 남으면 미리 받는다

let histBusy = false;             // 과거 구간 요청은 한 번에 하나만

function shiftYears(dateStr, years) {
  const d = new Date(dateStr + 'T00:00:00');
  if (Number.isNaN(d.getTime())) return dateStr;
  d.setFullYear(d.getFullYear() + years);
  return isoOf(d);
}
function prevDay(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  if (Number.isNaN(d.getTime())) return dateStr;
  d.setDate(d.getDate() - 1);
  return isoOf(d);
}
function firstAvailableDate() {
  return (S.status && S.status.first_trade_date) || '1995-01-01';
}
function latestDate() {
  return (S.status && S.status.latest_trade_date) || isoOf(new Date());
}

function setEdgeLoading(on, text) {
  const el = $('chartEdgeLoad');
  if (!el) return;
  el.hidden = !on;
  if (on) el.querySelector('.edge-tx').textContent = text || '이전 데이터를 불러오는 중';
}

/** 더 과거 구간이 남아 있는가 */
function hasMoreHistory() {
  if (!S.chart || !S.chart.hasData()) return false;
  if (S.chart.n >= CHART_MAX_BARS) return false;
  const oldest = S.chart.oldestDate;
  return !!oldest && oldest > firstAvailableDate();
}

/**
 * 과거 한 덩어리를 받아 앞에 붙인다.
 * @returns {Promise<number>} 붙은 봉 수 (0 이면 더 없음/실패)
 */
async function fetchOlderChunk(seq, code) {
  if (histBusy || !hasMoreHistory()) return 0;
  if (seq !== S.chartSeq) return 0;
  histBusy = true;
  setEdgeLoading(true);
  try {
    const end = prevDay(S.chart.oldestDate);
    const floor = firstAvailableDate();
    let start = shiftYears(end, -CHART_CHUNK_YEARS);
    if (start < floor) start = floor;
    if (start > end) return 0;

    const payload = await api.getChart({
      code, start, end,
      indicators: S.active.map((a) => a.key),
      run_id: S.result ? S.result.run_id : undefined,
      signal: S.chartAbort ? S.chartAbort.signal : undefined,
    });
    if (seq !== S.chartSeq) return 0;          // 종목이 바뀌었으면 버린다
    if (!payload || !payload.n) {
      S.histExhausted = true;                  // 이 구간에 데이터가 없다 = 상장 전
      return 0;
    }
    const added = S.chart.prependData(payload, S.active);
    if (!added) S.histExhausted = true;
    return added;
  } catch (e) {
    if (!(e && e.aborted)) {
      // 배경 작업이라 배너로 방해하지 않는다. 다음 팬에서 다시 시도된다.
      console.warn('[과거 구간 이어받기 실패]', e && e.message);
    }
    return 0;
  } finally {
    histBusy = false;
    setEdgeLoading(false);
  }
}

/** 배경으로 과거를 끝까지 이어붙인다 (사용자 조작을 막지 않는다) */
async function backfillHistory(seq, code) {
  let guard = 0;
  while (seq === S.chartSeq && !S.histExhausted && hasMoreHistory() && guard++ < 20) {
    const added = await fetchOlderChunk(seq, code);
    if (!added) break;
    // 다음 청크 전에 한 프레임 양보 — 팬·줌이 끊기지 않게 한다
    await new Promise((r) => setTimeout(r, 60));
  }
}

/** 왼쪽 끝 가까이 팬하면 즉시 이어받는다 */
function onChartViewport(i0) {
  if (i0 > EDGE_TRIGGER_BARS) return;
  if (S.histExhausted || histBusy || !hasMoreHistory()) return;
  fetchOlderChunk(S.chartSeq, S.symbol.code);
}

/**
 * 차트 로드.
 * 최근 구간을 먼저 받아 즉시 그리고, 과거는 배경으로 이어붙인다.
 * 기간 버튼은 데이터 재요청이 아니라 뷰포트 변경으로 처리한다.
 */
async function loadChart(o = {}) {
  if (!S.chart) return;
  const code = o.code || S.symbol.code || firstTradeCode() || '';
  $('chartNote').hidden = true;
  $('chartWrap').classList.remove('has-note');
  setEdgeLoading(false);

  // 서버가 "데이터 없음"이라고 이미 알려 줬으면 실패가 확정된 요청을 보내지 않는다
  // (콘솔에 503 을 남기지 않고, 같은 안내를 두 번 띄우지도 않는다)
  if (S.status && S.status.available === false) {
    S.chart.setData(null);
    $('symCode').textContent = '—';
    $('symName').textContent = '종목 선택';
    showChartEmpty({
      icon: 'alert',
      title: '주가 데이터가 없어 차트를 그릴 수 없습니다',
      desc: '화면 위 안내대로 update_marcap.bat 을 실행해 데이터를 받은 뒤 이 화면을 새로고침하세요.',
    });
    P.renderLegend(null, [], () => '', '');
    return;
  }

  // code 는 선택 파라미터다. 비워서 보내면 서버가 기본 종목(최근 실행의 첫 거래 종목
  // → 시가총액 1위)을 골라 준다. 이 기능이 없는 구버전 서버는 400/422 를 내므로
  // 그때는 한 번만 시도하고 이후에는 종목 선택 안내로 대체한다.
  if (!code && S.noAutoSymbol) {
    S.chart.setData(null);
    $('symCode').textContent = '—';
    $('symName').textContent = '종목 선택';
    showChartEmpty({
      icon: 'symbol',
      title: '아직 볼 종목이 없습니다',
      desc: '백테스트를 실행하면 첫 거래 종목이 자동으로 뜹니다. 지금 바로 특정 종목을 보고 싶으면 종목을 골라 주세요.',
      action: { label: '종목 고르기', act: 'pickSymbol', primary: true },
    });
    P.renderLegend(null, [], () => '', '');
    return;
  }

  showChartEmpty({
    icon: 'chart', title: '차트를 불러오는 중입니다…',
    desc: code ? `${code} 의 시세를 받고 있습니다.` : '표시할 종목을 고르는 중입니다.',
  });

  // 종목을 빠르게 여러 번 바꾸면 이전 요청은 실제로 끊는다
  if (S.chartAbort) S.chartAbort.abort();
  S.chartAbort = new AbortController();
  const signal = S.chartAbort.signal;
  const seq = ++S.chartSeq;
  S.histExhausted = false;
  S.lastChartArgs = o;            // 재시도 버튼용

  // 3초 넘게 걸리면 왜 기다리는지 알려 준다
  const slowTimer = setTimeout(() => {
    if (seq !== S.chartSeq) return;
    showChartEmpty({
      icon: 'chart', title: '데이터를 불러오는 중입니다…',
      desc: '처음 보는 종목이라 과거 시세를 읽고 있습니다. 잠시만 기다려 주세요.',
    });
  }, 3000);

  try {
    // 1) 최근 구간만 먼저 받는다 — 전체를 받으면 서버에서 10초를 넘긴다
    const end = latestDate();
    let start = shiftYears(end, -CHART_INITIAL_YEARS);
    const floor = firstAvailableDate();
    if (start < floor) start = floor;

    const payload = await api.getChart({
      code, start, end,
      n: Math.round(CHART_INITIAL_YEARS * 250),   // 폴백 mockdata 전용
      indicators: S.active.map((a) => a.key),
      run_id: S.result ? S.result.run_id : undefined,
      signal,
    });
    clearTimeout(slowTimer);
    // 지표를 연달아 바꾸거나 종목을 여러 번 누르면 요청이 겹친다. 예전 응답은 버린다.
    if (seq !== S.chartSeq) return;
    S.symbol = { code: payload.code || code, name: payload.name || o.name || '' };
    $('symCode').textContent = S.symbol.code || '—';
    $('symName').textContent = S.symbol.name || '이름 없음';
    S.chart.setData(payload, S.active);
    applyRangeViewport();         // 보이는 구간만 기간 버튼에 맞춘다

    // 2) 나머지 과거는 배경으로 이어붙인다 (await 하지 않는다 — 화면을 막지 않는다)
    if (payload && payload.n) backfillHistory(seq, S.symbol.code);

    if (payload && payload.n) {
      $('chartEmpty').hidden = true;
      // 서버가 요청한 기간을 다 주지 못했으면 알린다
      if (payload.truncated) {
        $('chartNote').hidden = false;
        $('chartWrap').classList.add('has-note');   // 범례를 아래로 밀어 겹침 방지
        $('chartNote').innerHTML =
          `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>` +
          `요청한 기간 전체가 표시되지 않았습니다 — 실제 표시 구간 ${P.esc(payload.start || '')} ~ ${P.esc(payload.end || '')}`;
      }
    } else {
      showChartEmpty({
        icon: 'search',
        title: '이 기간에는 거래된 기록이 없습니다',
        desc: '상장 전이거나 거래가 정지된 구간일 수 있습니다. 위의 기간 버튼에서 "전체" 를 눌러 보세요.',
      });
    }
    P.renderLegend(null, [], () => '', `${S.symbol.code} ${S.symbol.name}`);
    P.highlightSymbol(S.symbol.code);
    P.clearBanner('err-차트 불러오기');
  } catch (e) {
    clearTimeout(slowTimer);
    // 새 요청이 들어와 취소된 것은 오류가 아니다 (종목 연타 등)
    if ((e && e.aborted) || seq !== S.chartSeq) return;

    // 종목을 안 보내서 거절당한 경우는 오류가 아니라 "종목을 골라야 한다"는 뜻이다
    if (!code && e instanceof ApiError && (e.status === 400 || e.status === 422)) {
      S.noAutoSymbol = true;
      S.chart.setData(null);
      showChartEmpty({
        icon: 'symbol',
        title: '먼저 볼 종목을 골라 주세요',
        desc: '이 서버는 종목을 지정해야 차트를 보여 줍니다. 백테스트를 실행하면 첫 거래 종목이 자동으로 표시됩니다.',
        action: { label: '종목 고르기', act: 'pickSymbol', primary: true },
      });
      return;
    }
    handleError(e, '차트 불러오기');
    // 새로고침 말고 이 자리에서 다시 시도할 수 있게 한다
    showChartEmpty({
      icon: 'alert', tone: 'err',
      title: '차트를 불러오지 못했습니다',
      desc: (e && e.advice) || '잠시 후 다시 시도해 보세요.',
      action: { label: '다시 시도', act: 'retryChart', primary: true },
    });
  }
}

/** 실패한 차트 요청을 같은 조건으로 다시 시도 */
function retryChart() {
  P.clearBanner('err-차트 불러오기');
  loadChart(S.lastChartArgs || {});
}

function showChartEmpty(opts) {
  $('chartEmpty').hidden = false;
  $('chartEmptyBox').innerHTML = P.emptyState(opts);
}

function firstTradeCode() {
  return S.result && S.result.trades && S.result.trades.length ? S.result.trades[0].code : '';
}

/* ---------------- 종목 검색 ---------------- */

let symTimer = 0;
let symAbort = null;

function openSymbolPicker() {
  const pop = $('symPop');
  pop.hidden = false;
  $('symBtn').setAttribute('aria-expanded', 'true');
  const inp = $('symInput');
  inp.value = '';
  inp.focus();
  doSymbolSearch('');
}

function closeSymbolPicker() {
  $('symPop').hidden = true;
  $('symBtn').setAttribute('aria-expanded', 'false');
}

async function doSymbolSearch(q) {
  if (symAbort) symAbort.abort();
  symAbort = new AbortController();
  // 서버에 검색 기능이 없으면 코드 직접 입력으로 대체한다
  if (!api.hasFeature('symbol_search', false) && !api.isFallback()) {
    const code = String(q || '').trim();
    const valid = /^\d{6}$/.test(code);
    P.renderSymbolResults(
      valid ? [{ code, name: '입력한 종목코드', market: '', marcap_eok: null }] : [],
      valid ? 1 : 0, q, pickSymbol,
      valid ? '' : '이 서버는 아직 종목 이름 검색을 지원하지 않습니다. 6자리 종목코드를 직접 입력하세요. (예: 042700)');
    if (valid) P.renderSymbolResults([{ code, name: '이 코드로 차트 보기', market: '', marcap_eok: null }], 1, q, pickSymbol);
    return;
  }
  P.renderSymbolResults(null, 0, q, pickSymbol, '찾는 중…');
  try {
    const r = await api.searchSymbols(q, 30, symAbort.signal);
    if (!r) { P.renderSymbolResults([], 0, q, pickSymbol, '종목 검색을 사용할 수 없습니다.'); return; }
    P.renderSymbolResults(r.symbols || [], r.total || 0, q, pickSymbol);
  } catch (e) {
    if (e && e.aborted) return;
    P.renderSymbolResults([], 0, q, pickSymbol, `검색에 실패했습니다. ${(e && e.message) || ''}`);
  }
}

function pickSymbol(s) {
  closeSymbolPicker();
  S.symbol = { code: s.code, name: s.name || '' };
  S.chart.setHighlight(null);
  loadChart({ code: s.code, name: s.name });
  P.toast(`${s.name || s.code} 차트로 이동`, 'ok', 1800);
}

/* ---------------- 거래 이동 ---------------- */

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

function tradeSpan(t) {
  const fills = t.fills || [];
  return {
    from: t.ref_date || (fills[0] && fills[0].date) || t.exit_date,
    to: t.exit_date || (fills[fills.length - 1] && fills[fills.length - 1].date) || t.ref_date,
  };
}

function shiftMonths(dateStr, months) {
  const d = new Date(dateStr + 'T00:00:00');
  if (Number.isNaN(d.getTime())) return dateStr;
  d.setMonth(d.getMonth() + months);
  return isoOf(d);
}

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
    // 종목이 다르면 그 종목의 전체 히스토리를 새로 받는다.
    // await 필수 — 기다리지 않으면 새 데이터가 도착하며 setHighlight 가 지워진다.
    await loadChart({ code: t.code, name: t.name });
  }
  focusTrade(t);
}

function focusTrade(t) {
  const d = S.chart && S.chart.d;
  if (!d || !d.n) return;
  const span = tradeSpan(t);
  const from = indexOfDate(d.t, span.from);
  const to = indexOfDate(d.t, span.to);
  if (from < 0 || to < 0) {
    P.toast(api.isFallback()
      ? '예시 데이터에서는 거래 구간과 차트 구간이 서로 맞지 않습니다.'
      : `${t.name || t.code} 거래 구간(${span.from} ~ ${span.to})이 차트 범위 밖입니다.`,
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

/**
 * 기간 버튼. 데이터를 다시 받지 않고 뷰포트만 바꾼다.
 * (전체 히스토리를 이미 들고 있으므로 재요청이 필요 없다)
 */
function setRange(months) {
  S.rangeMonths = months;
  for (const b of $('rangeSeg').querySelectorAll('button')) {
    b.setAttribute('aria-pressed', String(Number(b.dataset.months) === months));
  }
  if (S.chart && S.chart.hasData()) { applyRangeViewport(); return Promise.resolve(); }
  return loadChart();
}

/* ---------------- 종목 선택 (모든 진입점 공통) ---------------- */

/**
 * 화면 어디서 종목을 누르든 이 함수 하나를 거친다.
 * 거래가 있으면 첫 거래 구간으로, 없으면 차트만 바꾼다.
 */
async function selectSymbol(code, opts = {}) {
  if (!code) return;
  const trades = (S.result && S.result.trades) || [];
  const first = trades.find((t) => t.code === code);
  if (first) {
    await gotoTrade(first.no);           // 차트 교체 + 구간 이동 + 마커 강조 + 행 선택
  } else {
    await loadChart({ code, name: opts.name });
    S.chart.setHighlight(null);
    S.tradeNo = 0;
    P.selectTradeRow(null);
    $('tradeIdxLabel').textContent = trades.length ? `— / ${trades.length}` : '—';
    if (S.ran) {
      P.toast(`${opts.name || code} 은 이번 백테스트에서 거래가 없습니다. 차트만 표시합니다.`, '', 3600);
    }
  }
  P.highlightSymbol(S.symbol.code);
}

/* ---------------- 지표 관리 ---------------- */

/** 현재 지표 목록 + 추가를 한 화면에서 처리한다 (추가만 되는 단방향을 없앤다) */
function openIndicatorManager() {
  const rows = () => {
    const items = [
      ...S.active.map((a) => ({ ...a, hidden: false })),
      ...[...S.hiddenInd].map((k) => ({ key: k, label: k, hidden: true, fromStrategy: true })),
    ];
    if (!items.length) return '<p class="hint">아직 추가된 지표가 없습니다.</p>';
    return `<div class="ind-list">${items.map((a) => `
      <div class="ind-row${a.hidden ? ' off' : ''}" data-k="${P.esc(a.key)}">
        <span class="ind-sw" style="background:${a.hidden ? 'transparent' : P.esc(S.chart.slotColor(a.slot))}"></span>
        <span class="ind-nm">${P.esc(a.longLabel || a.label)}
          ${a.fromStrategy ? '<span class="ind-tag">전략</span>' : '<span class="ind-tag ui">임시</span>'}
        </span>
        <button type="button" class="btn btn-sm" data-ind-vis="${P.esc(a.key)}">${a.hidden ? '표시' : '숨기기'}</button>
        <button type="button" class="btn btn-sm btn-danger" data-ind-del="${P.esc(a.key)}">삭제</button>
      </div>`).join('')}</div>`;
  };

  const opts = S.specs.map((sp) => `<option value="${P.esc(sp.key)}">${P.esc(sp.label || sp.key)} (${P.esc(sp.key)})</option>`).join('');
  P.modal('지표 관리',
    `<div class="ind-mgr-list">${rows()}</div>
     <div style="border-top:1px dashed var(--bd);margin-top:12px;padding-top:12px">
       <b style="font-size:12.5px">지표 추가</b>
       <div class="fld" style="margin-top:6px"><label for="dlgType"><b>지표</b></label>
         <select class="inp wide" id="dlgType">${opts}</select><span class="unit"></span></div>
       <div id="dlgParams"></div>
       <div id="dlgDup" class="fld-err" hidden></div>
     </div>`,
    [
      { label: '닫기' },
      {
        label: '추가', primary: true, keepOpen: true, onClick: () => {
          const spec = specOf($('dlgType').value);
          const entry = { key: '', type: spec.key, plot: true };
          for (const pp of spec.params || []) {
            const el = $('dlgP_' + pp.name);
            let v = el ? el.value : pp.default;
            if (pp.type === 'int') v = parseInt(v, 10);
            else if (pp.type === 'float' || pp.type === 'number') v = parseFloat(v);
            entry[pp.name] = Number.isNaN(v) ? pp.default : v;
          }
          entry.key = P.indLabel(spec, entry);
          const key = P.indKey(spec, entry);
          if (S.hiddenInd.has(key)) {
            S.hiddenInd.delete(key);
            P.closeModal(); afterIndicatorChange(`${entry.key} 다시 표시`);
            return;
          }
          if (indExists(key)) {
            const w = $('dlgDup');
            w.hidden = false;
            w.textContent = `${entry.key} 은 이미 추가되어 있습니다. 파라미터를 바꾸거나 위 목록에서 관리하세요.`;
            return;
          }
          (S.draft.indicators ||= []).push(entry);
          P.closeModal();
          afterIndicatorChange(`${entry.key} 추가됨`);
        },
      },
    ]);

  // 목록의 표시/삭제 버튼
  document.getElementById('modalBody').addEventListener('click', (e) => {
    const del = e.target.closest('[data-ind-del]');
    const vis = e.target.closest('[data-ind-vis]');
    if (del) { P.closeModal(); requestRemoveIndicator(del.dataset.indDel); return; }
    if (vis) {
      const k = vis.dataset.indVis;
      if (S.hiddenInd.has(k)) S.hiddenInd.delete(k); else S.hiddenInd.add(k);
      P.closeModal();
      afterIndicatorChange(S.hiddenInd.has(k) ? '지표를 숨겼습니다' : '지표를 다시 표시합니다');
    }
  });

  const paint = () => {
    const spec = specOf($('dlgType').value);
    $('dlgDup').hidden = true;
    $('dlgParams').innerHTML = (spec.params || []).map((pp) => {
      if (Array.isArray(pp.options)) {
        const os = pp.options.map((o) => `<option value="${P.esc(o)}"${o === pp.default ? ' selected' : ''}>${P.esc(o)}</option>`).join('');
        return `<div class="fld"><label for="dlgP_${P.esc(pp.name)}"><b>${P.esc(pp.name)}</b></label>
          <select class="inp wide" id="dlgP_${P.esc(pp.name)}">${os}</select><span class="unit"></span></div>`;
      }
      return `<div class="fld"><label for="dlgP_${P.esc(pp.name)}"><b>${P.esc(pp.name)}</b></label>
        <input class="inp" id="dlgP_${P.esc(pp.name)}" type="number"
          value="${P.esc(pp.default)}"${pp.min !== undefined ? ` min="${P.esc(pp.min)}"` : ''}${pp.max !== undefined ? ` max="${P.esc(pp.max)}"` : ''}${pp.step !== undefined ? ` step="${P.esc(pp.step)}"` : ''}>
        <span class="unit"></span></div>`;
    }).join('') || '<p class="hint">이 지표는 따로 정할 값이 없습니다.</p>';
  };
  $('dlgType').addEventListener('change', paint);
  paint();
}

/* ============================================================
   6. 백테스트
   ============================================================ */

function setRunning(on) {
  S.running = on;
  if (on) P.renderNextStep(null);      // 진행 패널과 겹치지 않게 안내를 잠시 치운다
  $('btnRun').disabled = on;
  $('btnRunIcon').style.display = on ? 'none' : '';
  $('btnCancelRun').hidden = !on;
  $('btnRunLabel').textContent = on ? '실행 중…' : '백테스트 실행';
}

/** 백테스트 구간이 몇 년인지 */
function periodYears(strategy) {
  const per = (strategy && strategy.period) || {};
  const start = per.start;
  const end = (!per.end || per.end === 'auto')
    ? ((S.status && S.status.latest_trade_date) || isoOf(new Date()))
    : per.end;
  if (!start || !end) return 0;
  const ms = ymdDate(end) - ymdDate(start);
  return ms > 0 ? ms / (365.25 * 86400000) : 0;
}

const LONG_RUN_YEARS = 5;

/** 5년을 넘는 구간은 실행 전에 미리 알려 준다 */
function runBacktest() {
  if (S.running || !S.draft) return;
  const years = periodYears(S.draft);
  if (years > LONG_RUN_YEARS && !S.longRunAck) {
    P.modal('시간이 걸릴 수 있습니다',
      `<p>백테스트 구간이 <b>약 ${years.toFixed(1)}년</b>입니다.</p>
       <p>구간이 길어 <b>수 분</b> 걸릴 수 있습니다. 진행 상황은 화면 위에 단계와 남은 시간으로 표시되며,
       <b>실행 중에 언제든 취소할 수 있습니다.</b></p>
       <p class="hint">기간을 줄이려면 파라미터의 백테스트 시작일을 뒤로 옮기세요.</p>`,
      [
        { label: '취소' },
        {
          label: '이대로 실행', primary: true, onClick: () => {
            S.longRunAck = true;          // 같은 세션에서 반복해 묻지 않는다
            doRunBacktest();
          },
        },
      ]);
    return;
  }
  doRunBacktest();
}

async function doRunBacktest() {
  if (S.running || !S.draft) return;
  if (S.status && S.status.available === false) {
    P.toast('주가 데이터가 없어 백테스트를 실행할 수 없습니다.', 'err', 4000);
    return;
  }
  const errs = checkForm().filter((i) => i.level === 'err');
  if (errs.length) {
    P.focusFirstError();
    P.toast(`고쳐야 할 값이 ${errs.length}개 있습니다.`, 'err');
    return;
  }
  if (S.syncing) P.toast('시세를 받는 중입니다. 어제까지 데이터로 실행합니다.', '', 4000);

  setRunning(true);
  S.cancelling = false;
  P.clearBanner('no-trades');
  P.clearBanner('cancelled');
  P.renderTrades([], gotoTrade, { loading: true });
  P.renderByStock([], { loading: true });
  P.renderSignals([], { loading: true });

  const jobId = api.newJobId();
  S.jobId = jobId;
  S.abort = new AbortController();
  const serverCancel = api.hasFeature('backtest_cancel', false);

  // 진행 상태 — 취소 버튼은 실행 즉시 노출한다
  const t0 = Date.now();
  const prog = { phase: '백테스트 준비 중', message: '', pct: null, etaSec: null };
  const paint = () => P.renderRunProgress({
    phase: S.cancelling ? '취소하는 중…' : prog.phase,
    message: prog.message,
    pct: prog.pct,
    elapsedSec: (Date.now() - t0) / 1000,
    etaSec: prog.etaSec,
    cancellable: !S.cancelling,
  });
  paint();
  const tick = setInterval(paint, 500);
  P.progress(null);

  // SSE: job_id 를 먼저 열고 같은 id 로 실행한다
  let es = api.backtestProgressStream(jobId);
  if (es) {
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.phase) prog.phase = String(d.phase);
        if (d.message) prog.message = String(d.message);
        if (Number.isFinite(d.done) && Number.isFinite(d.total) && d.total > 0) {
          prog.pct = (d.done / d.total) * 100;
          P.progress(prog.pct);
        }
        // eta_sec 이 null 이면 "계산 중…" 으로 남겨 둔다
        prog.etaSec = Number.isFinite(d.eta_sec) ? d.eta_sec : null;
        paint();
      } catch { /* 진행률 파싱 실패는 무시 */ }
    };
    es.onerror = () => { if (es) { es.close(); es = null; } };
  }

  try {
    const res = await api.runBacktest(S.draft, S.draft.period, { jobId, signal: S.abort.signal });
    S.result = res;
    S.ran = true;
    applyResult(res);
    P.clearBanner('err-백테스트 실행');
    P.toast(`백테스트 완료 — ${res.metrics.trades}거래 · ${P.pct1(res.metrics.total_return_pct)} · ${(res.elapsed_sec ?? 0).toFixed(1)}초`, 'ok', 3800);
  } catch (e) {
    if (e && e.aborted) {
      // 취소는 오류가 아니다. 빨간 배너 대신 중립 토스트.
      P.renderTrades(S.result ? S.result.trades : [], gotoTrade, { ran: S.ran });
      P.renderSignals(S.result ? S.result.signals : [], { ran: S.ran });
      P.renderByStock(S.result ? S.result.by_stock : [], { ran: S.ran });
      P.toast('백테스트를 취소했습니다.', '', 3000);
      if (!serverCancel) {
        // 서버 취소 API 가 없을 때만 사실대로 덧붙인다
        P.banner('cancelled', 'warn',
          '<b>화면에서 실행을 취소했습니다.</b>' +
          '<div class="err-advice">이 서버는 실행 취소 기능이 없어, 프로그램 서버에서는 계산이 계속 진행 중일 수 있습니다.</div>');
      }
    } else if (e instanceof ApiError && e.status === 422 && e.errors) {
      P.showFieldErrors(e.errors);
      P.focusFirstError();
      P.banner('validate', 'err',
        `<b>실행하지 못했습니다 — 값 ${e.errors.length}개를 고쳐야 합니다.</b>` +
        '<div class="err-advice">빨간색으로 표시된 항목을 고친 뒤 다시 실행하세요.</div>');
      P.renderTrades([], gotoTrade, { ran: false });
      P.renderSignals([], { ran: false });
      P.renderByStock([], { ran: false });
    } else {
      handleError(e, '백테스트 실행');
      P.renderTrades([], gotoTrade, { ran: false });
      P.renderSignals([], { ran: false });
      P.renderByStock([], { ran: false });
    }
  } finally {
    clearInterval(tick);
    P.renderRunProgress(null);
    if (es) es.close();
    S.abort = null; S.jobId = null; S.cancelling = false;
    setRunning(false);
    P.progress(false);
    updateNextStep();
  }
}

/** 취소 — 서버 취소 API 가 있으면 서버에 알리고, 없으면 요청만 끊는다 */
async function cancelBacktest() {
  if (!S.running || S.cancelling) return;
  S.cancelling = true;
  $('btnCancelRun').disabled = true;
  try {
    if (api.hasFeature('backtest_cancel', false) && S.jobId) {
      await api.cancelBacktest(S.jobId);
      // 서버가 취소 처리하면 실행 중인 POST 가 cancelled:true 로 돌아온다
    } else if (S.abort) {
      S.abort.abort();
    }
  } catch (e) {
    handleError(e, '백테스트 취소');
    if (S.abort) S.abort.abort();
  } finally {
    $('btnCancelRun').disabled = false;
  }
}

/** 현재 정렬을 적용해 거래 내역을 다시 그린다 */
function renderTradeTable() {
  const res = S.result;
  if (!res || !res.trades) { P.renderTrades([], gotoTrade, { ran: S.ran, hints: emptyHints() }); return; }
  const rows = P.sortTrades(res.trades, S.sortKey, S.sortDir);
  P.renderTrades(rows, gotoTrade, { ran: true, hints: emptyHints() });
  P.markSortHeader(S.sortKey, S.sortDir);
  if (S.tradeNo) P.selectTradeRow(S.tradeNo);
}

/** 머리글 클릭 — 같은 열을 다시 누르면 방향을 뒤집는다 */
function onSortTrades(key) {
  if (S.sortKey === key) S.sortDir = S.sortDir === 'asc' ? 'desc' : 'asc';
  else { S.sortKey = key; S.sortDir = key === 'no' || key === 'name' ? 'asc' : 'desc'; }
  renderTradeTable();
}

function applyResult(res) {
  P.renderAssumptions(res.assumptions, res.metrics, S.draft && S.draft.params);
  P.renderKpis(res.metrics);
  S.eq.set(res.equity || null);
  S.mo.set(res.monthly || null);
  P.renderByStock(res.by_stock, { ran: true });
  renderTradeTable();
  const nameByCode = new Map((res.trades || []).map((t) => [t.code, t.name]));
  for (const b of (res.by_stock || [])) if (b.code) nameByCode.set(b.code, b.name);
  P.renderSignals(res.signals, { ran: true, nameOf: (c) => nameByCode.get(c) });

  if (Array.isArray(res.warnings) && res.warnings.length) {
    P.banner('warnings', 'warn',
      '<b>결과를 볼 때 참고하세요</b><ul>' + res.warnings.map((w) => `<li>${P.esc(w)}</li>`).join('') + '</ul>');
  } else {
    P.clearBanner('warnings');
  }

  if (res.trades && res.trades.length) {
    $('tradeIdxLabel').textContent = `1 / ${res.trades.length}`;
    gotoTrade(res.trades[0].no).then(() => P.highlightSymbol(S.symbol.code));
  } else {
    $('tradeIdxLabel').textContent = '0 / 0';
  }
}

/** 거래 0건일 때 어떤 값을 풀면 되는지 지금 설정 기준으로 알려 준다 */
function emptyHints() {
  const v = (id) => Number(($(id) || {}).value);
  const out = [];
  if (Number.isFinite(v('p_amt'))) out.push(`기준일 거래대금을 ${P.fmt(v('p_amt'))}억 → ${P.fmt(Math.max(50, Math.round(v('p_amt') / 2)))}억 으로 낮춰 보세요`);
  if (Number.isFinite(v('p_look'))) out.push(`탐색 기간을 ${v('p_look')}일 → ${v('p_look') * 2}일 로 늘려 보세요`);
  if (Number.isFinite(v('p_prev'))) out.push(`전일 거래대금 상한 ${P.fmt(v('p_prev'))}억 이 너무 낮을 수 있습니다`);
  if (Number.isFinite(v('p_valid'))) out.push(`유효 기간을 ${v('p_valid')}일 → ${v('p_valid') * 2}일 로 늘리면 더 오래 기다립니다`);
  out.push('백테스트 구간의 시작일을 더 앞으로 당겨 보세요');
  return out;
}

/* ============================================================
   7. AI 전략 생성
   ============================================================ */

function updateAiNotice() {
  const host = $('aiNotice');
  if (!host) return;
  // 변환을 눌러야 알게 되면 안 되므로 미리 안내한다
  if (api.getFeatures() && api.hasFeature('ai_available', true) === false) {
    host.innerHTML =
      '<div class="ai-msg err"><b>AI 변환을 지금은 쓸 수 없습니다.</b>' +
      'ANTHROPIC_API_KEY 가 설정되지 않아 이 기능만 꺼져 있습니다. 나머지 기능은 정상입니다.' +
      '<pre>명령 프롬프트에서\n  setx ANTHROPIC_API_KEY sk-ant-...\n를 실행한 뒤 프로그램을 다시 시작하세요.</pre></div>';
    $('btnAI').disabled = true;
    $('btnAILabel').textContent = '사용 불가';
  } else {
    host.innerHTML = '';
    $('btnAI').disabled = false;
    $('btnAILabel').textContent = 'DSL로 변환';
  }
}

async function runAi() {
  const prompt = $('aiIn').value.trim();
  const out = $('aiResult');
  if (!prompt) { P.toast('먼저 전략 설명을 적어 주세요.', 'err'); $('aiIn').focus(); return; }

  const btn = $('btnAI');
  btn.disabled = true;
  $('btnAILabel').textContent = '변환 중…';
  $('aiStat').textContent = '한국어 설명을 전략 형식으로 바꾸는 중입니다…';
  out.innerHTML = `<div class="ai-msg"><span class="sk sk-line" style="width:70%"></span>
    <span class="sk sk-line" style="width:92%;margin-top:6px"></span></div>`;
  P.progress(null);
  try {
    const r = await api.aiStrategy(prompt, S.draft || undefined);
    if (r && r.ok) {
      // 덮어쓰기 전에 무엇이 바뀌는지 보여 준다
      const diffs = P.diffStrategies(S.draft, r.strategy);
      out.innerHTML = `<div class="ai-msg ok"><b>변환했습니다 — 아직 적용하지는 않았습니다.</b>
        아래 내용을 확인하고 [적용] 을 눌러야 왼쪽 파라미터가 바뀝니다.</div>`;
      $('aiStat').textContent = '변환 완료 — 적용 여부를 확인하세요';
      P.modal('이 내용으로 바꿀까요?',
        `<p>AI 가 만든 전략을 지금 파라미터에 덮어씁니다. 바뀌는 값은 아래와 같습니다.</p>` +
        P.renderDiffTable(diffs) +
        ((r.warnings || []).length
          ? `<p class="hint" style="margin-top:8px">참고: ${(r.warnings || []).map((w) => P.esc(w)).join(' / ')}</p>` : ''),
        [
          { label: '취소', onClick: () => { $('aiStat').textContent = '적용하지 않았습니다.'; } },
          {
            label: '적용', primary: true, onClick: async () => {
              S.draft = r.strategy;
              markDirty();
              P.fillForm(S.draft);
              P.renderFieldHints(); checkForm();
              rebuildActive(); drawChips();
              P.renderJson(S.draft);
              syncResolutionNote();
              out.innerHTML = '<div class="ai-msg ok"><b>왼쪽 파라미터에 적용했습니다.</b>내용을 확인한 뒤 [저장] 을 누르세요.</div>';
              $('aiStat').textContent = '적용됨 — 저장 전';
              P.toast('AI 전략을 적용했습니다.', 'ok');
              await loadChart();
            },
          },
        ]);
    } else {
      out.innerHTML = `<div class="ai-msg err"><b>${P.esc((r && r.error) || 'AI 전략 생성에 실패했습니다.')}</b>` +
        ((r && r.how_to) ? `<pre>${P.esc(r.how_to)}</pre>` : '') + '</div>';
      $('aiStat').textContent = '변환하지 못했습니다';
    }
  } catch (e) {
    handleError(e, 'AI 전략 생성');
    out.innerHTML = `<div class="ai-msg err">${P.errorHtml(e, '요청 실패')}</div>`;
    $('aiStat').textContent = '변환하지 못했습니다';
  } finally {
    btn.disabled = false;
    $('btnAILabel').textContent = 'DSL로 변환';
    P.progress(false);
  }
}

const AI_EXAMPLE = '20일 안에 거래대금 1000억 넘은 날이 있고, 그 전날은 200억 이하였던 종목 중에서 ' +
  '기준일 시가까지 눌린 종목을 시가에 절반 매수. 매수가보다 10% 더 빠지면 나머지 절반 매수. ' +
  '평단 +10%면 전량 익절, 45일선 닿으면 전량 손절.';

/* ============================================================
   8. 부트스트랩
   ============================================================ */

function wire() {
  bindThemeToggle($('themeSeg'));
  P.bindModal();
  P.bindAssumptions();

  // 빈 상태 / 안내 배너 안의 버튼을 한 곳에서 처리
  P.bindEmptyActions({
    run: runBacktest,
    pickSymbol: openSymbolPicker,
    sync: () => startSync({}),
    cancelRun: cancelBacktest,
    retryChart,
  });

  P.bindTradeSort(onSortTrades);
  P.restorePanelSizes();
  P.bindResizers();
  // 패널 크기가 바뀌면 캔버스도 다시 그린다 (ResizeObserver 가 잡지만 미니차트는 명시 호출)
  window.addEventListener('panelresize', () => { S.eq.schedule(); S.mo.schedule(); });

  P.bindTabs((name) => {
    if (name === 'pnJson' && S.draft) P.renderJson(S.draft);
    if (name === 'pnData') P.renderDataStatus(S.status, api.isFallback() ? api.fallbackNote() : '');
    if (name === 'pnTrades') { S.eq.schedule(); S.mo.schedule(); }
  });

  P.bindForm(onFormChange);

  $('btnSync').addEventListener('click', () => startSync({}));
  $('btnSettings').addEventListener('click', openSettings);
  $('optAutoSync').addEventListener('change', (e) => {
    setAutoSyncEnabled(e.currentTarget.checked);
    P.toast(e.currentTarget.checked
      ? '켤 때 자동으로 시세를 받습니다.'
      : '자동 갱신을 껐습니다. [지금 갱신] 으로만 받습니다.', 'ok');
  });
  $('btnRun').addEventListener('click', runBacktest);
  $('btnCancelRun').addEventListener('click', cancelBacktest);
  $('btnSave').addEventListener('click', () => saveStrategy());
  $('btnRevert').addEventListener('click', revertStrategy);
  $('btnClone').addEventListener('click', cloneStrategy);
  $('btnDelete').addEventListener('click', deleteStrategy);
  $('btnExport').addEventListener('click', exportStrategy);
  $('btnNewStrategy').addEventListener('click', newStrategy);
  $('btnAddInd').addEventListener('click', openIndicatorManager);
  $('btnAI').addEventListener('click', runAi);
  $('btnAiExample').addEventListener('click', () => { $('aiIn').value = AI_EXAMPLE; $('aiIn').focus(); });
  $('prevTrade').addEventListener('click', () => stepTrade(-1));
  $('nextTrade').addEventListener('click', () => stepTrade(1));

  for (const b of $('rangeSeg').querySelectorAll('button')) {
    b.addEventListener('click', () => setRange(Number(b.dataset.months)));
  }

  // 종목 검색
  $('symBtn').addEventListener('click', () => {
    if ($('symPop').hidden) openSymbolPicker(); else closeSymbolPicker();
  });
  $('symInput').addEventListener('input', (e) => {
    clearTimeout(symTimer);
    const q = e.target.value;
    symTimer = setTimeout(() => doSymbolSearch(q), 220);
  });
  $('symInput').addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeSymbolPicker(); $('symBtn').focus(); }
    if (e.key === 'Enter') {
      const first = $('symResults').querySelector('.sym-item');
      if (first) first.click();
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      $('symResults').querySelector('.sym-item')?.focus();
    }
  });
  document.addEventListener('click', (e) => {
    if (!$('symPop').hidden && !e.target.closest('.sym-picker')) closeSymbolPicker();
  });

  // 종목이 보이는 곳은 어디든 눌러서 차트를 바꾼다
  P.bindSymbolLinks((code, o) => selectSymbol(code, o));

  // 범례의 지표 삭제 / 접기·펼치기 (범례는 hover 마다 다시 그려지므로 위임 처리)
  document.addEventListener('click', (e) => {
    const x = e.target.closest('[data-ind-remove]');
    if (x) { e.preventDefault(); e.stopPropagation(); requestRemoveIndicator(x.dataset.indRemove); return; }
    const t = e.target.closest('[data-legend-toggle]');
    if (t) {
      e.preventDefault(); e.stopPropagation();
      P.toggleLegendExpanded();
      S.chart.requestOverlay();
    }
  });

  // 오류 배너의 "자세히"
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-more]');
    if (!b) return;
    const pre = b.parentElement.querySelector('.err-detail');
    if (!pre) return;
    pre.hidden = !pre.hidden;
    b.textContent = pre.hidden ? '자세히' : '접기';
  });

  window.addEventListener('api:fallback', (e) => {
    $('mockBadge').hidden = false;
    P.banner('fallback', 'warn',
      `<b>프로그램 서버에 연결하지 못했습니다.</b> ${P.esc(e.detail.reason)}<br>` +
      '지금 보이는 숫자는 화면 확인용 <b>예시 데이터</b>이며 실제 계산 결과가 아닙니다.',
      false);
    updateAiNotice();
  });

  window.addEventListener('error', (e) => {
    P.banner('js-error', 'err', `<b>화면 오류가 발생했습니다.</b><div class="err-advice">${P.esc(e.message || '알 수 없는 오류')} — 화면을 새로고침해 보세요.</div>`);
  });
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason;
    if (r && r.aborted) return;
    P.banner('js-error', 'err', `<b>처리되지 않은 오류</b><div class="err-advice">${P.esc((r && r.message) || String(r))}</div>`);
  });

  window.addEventListener('themechange', () => { if (S.specs.length || S.active.length) drawChips(); });

  // 저장 안 한 채로 창을 닫으려 하면 브라우저 기본 확인창을 띄운다
  window.addEventListener('beforeunload', (e) => {
    if (!S.dirty) return;
    e.preventDefault();
    e.returnValue = '';
  });
}

function setupCharts() {
  const wrap = $('chartWrap');
  S.chart = new CandleChart(wrap, {
    onHover: (info) => {
      // 차트가 실제로 그린 지표 메타를 그대로 쓴다 — S.active 와 어긋날 여지를 없앤다
      const meta = (info && info.indicatorMeta) || [];
      P.renderLegend(info, meta, (slot) => S.chart.slotColor(slot),
        `${S.symbol.code} ${S.symbol.name}`);
      P.renderTooltip(info, wrap.getBoundingClientRect());
    },
    // 아직 안 받은 과거로 팬하면 그때 이어받는다
    onViewport: (i0) => onChartViewport(i0),
  });
  S.eq = new MiniChart($('eq'), 'equity');
  S.mo = new MiniChart($('mo'), 'monthly');

  window.__app = S;      // playwright 성능 측정용 훅
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

  $('optAutoSync').checked = autoSyncEnabled();
  $('btnRevert').disabled = true;

  // 초기 빈 상태 (왜 비었는지 + 무엇을 하면 되는지)
  P.renderAssumptions(null, null, null);
  P.renderKpis(null);
  P.renderTrades([], gotoTrade, { ran: false });
  P.renderSignals([], { ran: false });
  P.renderByStock([], { ran: false });
  showChartEmpty({ icon: 'chart', title: '준비 중입니다…', desc: '프로그램 상태를 확인하고 있습니다.' });

  await loadStatus();
  updateAiNotice();
  maybeAutoSync();          // 화면을 막지 않도록 await 하지 않는다

  await loadIndicators();
  await loadStrategies();

  window.__ready = true;
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}
