/* ============================================================
   api.js — 백엔드 REST 래퍼
   ------------------------------------------------------------
   · ARCHITECTURE.md 4장 스펙을 그대로 믿고 호출한다.
   · 에러를 절대 삼키지 않는다. 실패는 ApiError 로 감싸서 던지고,
     호출부가 화면 상단 배너에 한국어 안내를 띄운다.
   · 서버가 아직 구현하지 않은 엔드포인트는 `status.features` 로만 판단한다.
     플래그가 없으면 호출 자체를 하지 않는다 (없는 URL 을 찔러 콘솔에 404 를 남기지 않는다).
   · 백엔드가 아예 없을 때(연결 실패)만 mockdata 폴백을 허용하며,
     그 사실을 isFallback() 으로 노출해 화면에 "예시 데이터" 배지를 띄운다.
   ============================================================ */

import * as mock from './mockdata.js';

const BASE = '/api';
const TIMEOUT_MS = 20000;
const BACKTEST_TIMEOUT_MS = 700000;   // 서버 자체 타임아웃(600초)보다 길게 잡는다
const SYNC_TIMEOUT_MS = 660000;
/** 차트는 서버가 과거 연도 파일을 읽어야 할 수 있어 넉넉히 잡는다 */
const CHART_TIMEOUT_MS = 30000;

/** 백엔드 연결 실패로 폴백 데이터를 쓰고 있는가 */
let fallback = false;
let fallbackReason = '';

/** `GET /api/status` 가 알려 준 서버 기능 목록 */
let features = null;

export function isFallback() { return fallback; }
export function fallbackNote() { return fallbackReason; }

/**
 * 서버 기능 지원 여부.
 * 플래그가 아예 없는(구버전) 서버에서는 `dflt` 를 돌려준다.
 */
export function hasFeature(name, dflt = false) {
  if (fallback) return name !== 'ai_available';     // 폴백은 AI 만 빼고 흉내낼 수 있다
  if (!features || typeof features !== 'object') return dflt;
  const v = features[name];
  return v === undefined ? dflt : v === true;
}

export function setFeatures(f) { if (f && typeof f === 'object') features = f; }
export function getFeatures() { return features; }

/** 폴백 상태가 바뀌면 화면이 배지를 갱신할 수 있도록 알린다 */
function enterFallback(reason) {
  if (fallback) return;
  fallback = true;
  fallbackReason = reason;
  window.dispatchEvent(new CustomEvent('api:fallback', { detail: { reason } }));
}

/* ============================================================
   에러
   ============================================================ */

export class ApiError extends Error {
  constructor(message, opts = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = opts.status ?? 0;      // HTTP 상태 (0 = 연결 실패)
    this.errors = opts.errors ?? null;   // 422 검증 오류 [{path, message}]
    this.payload = opts.payload ?? null; // 원본 응답 본문
    this.path = opts.path ?? '';
    this.offline = opts.offline === true;
    this.detail = opts.detail ?? '';     // 서버가 준 기술적 원인 ("자세히" 로만 노출)
    this.kind = opts.kind ?? 'server';   // network|timeout|nodata|busy|slow|validation|server|aborted
    this.advice = opts.advice ?? '';     // 사용자가 다음에 할 일 (한국어)
    this.aborted = opts.aborted === true;
  }
}

/**
 * 상태 코드 → {kind, advice}.
 * 네트워크 오류와 서버 오류를 구분한다. 서버가 꺼진 경우와
 * 서버는 살아 있는데 실패한 경우는 사용자가 할 일이 완전히 다르다.
 */
function classify(status) {
  switch (status) {
    case 0:   return { kind: 'network', advice: '프로그램 서버가 꺼져 있는 것 같습니다. 바탕화면의 KRX백테스터.vbs 를 다시 실행한 뒤 이 화면을 새로고침하세요.' };
    case 400: return { kind: 'server', advice: '입력값을 확인한 뒤 다시 시도하세요.' };
    case 404: return { kind: 'server', advice: '삭제되었거나 이름이 바뀐 항목입니다. 목록을 새로고침해 보세요.' };
    case 409: return { kind: 'busy', advice: '같은 작업이 이미 실행 중입니다. 끝날 때까지 기다린 뒤 다시 시도하세요.' };
    case 422: return { kind: 'validation', advice: '표시된 항목을 고친 뒤 다시 시도하세요.' };
    case 500: return { kind: 'server', advice: '프로그램 내부 오류입니다. 아래 "자세히" 내용을 복사해 두면 원인 파악에 도움이 됩니다.' };
    case 503: return { kind: 'nodata', advice: '주가 데이터가 아직 없습니다. 프로젝트 폴더의 update_marcap.bat 을 실행해 데이터를 먼저 받으세요.' };
    case 504: return { kind: 'slow', advice: '시간이 너무 오래 걸렸습니다. 백테스트 기간을 줄이거나 종목 선정 조건을 좁혀서 다시 실행하세요.' };
    default:  return { kind: 'server', advice: '잠시 후 다시 시도하세요.' };
  }
}

/**
 * 사용자에게 보여줄 한 줄.
 * 서버 규약: `error` = 한국어 사용자 메시지, `detail` = 기술적 원인.
 */
function messageFor(status, body) {
  const msg = body && (body.error || body.detail || body.message);
  if (typeof msg === 'string' && msg.trim()) return msg.trim();
  switch (status) {
    case 400: return '요청이 올바르지 않습니다.';
    case 404: return '요청한 항목을 찾을 수 없습니다.';
    case 409: return '이미 처리 중입니다.';
    case 422: return '입력값 검증에 실패했습니다.';
    case 500: return '프로그램 내부 오류가 발생했습니다.';
    case 503: return '주가 데이터를 사용할 수 없습니다.';
    case 504: return '시간이 초과되었습니다.';
    default:  return `요청이 실패했습니다. (HTTP ${status})`;
  }
}

/* ============================================================
   저수준 fetch
   ============================================================ */

async function request(path, init = {}, timeoutMs = TIMEOUT_MS) {
  const url = BASE + path;
  const ctrl = new AbortController();
  const external = init.signal;
  if (external) {
    if (external.aborted) ctrl.abort();
    else external.addEventListener('abort', () => ctrl.abort(), { once: true });
  }
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);

  let res;
  try {
    res = await fetch(url, {
      ...init,
      signal: ctrl.signal,
      headers: {
        Accept: 'application/json',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...(init.headers || {}),
      },
    });
  } catch (e) {
    clearTimeout(timer);
    if (external && external.aborted) {
      throw new ApiError('사용자가 취소했습니다.', { status: 0, path, kind: 'aborted', aborted: true });
    }
    if (e && e.name === 'AbortError') {
      throw new ApiError(`서버 응답이 ${Math.round(timeoutMs / 1000)}초 안에 오지 않았습니다.`, {
        status: 0, path, kind: 'timeout',
        advice: '서버가 아직 작업 중일 수 있습니다. 잠시 기다렸다가 화면을 새로고침해 보세요.',
      });
    }
    const c = classify(0);
    throw new ApiError('프로그램 서버에 연결할 수 없습니다.', {
      status: 0, offline: true, path, kind: c.kind, advice: c.advice,
    });
  }
  clearTimeout(timer);

  const text = await res.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = { raw: text }; }
  }

  if (!res.ok) {
    const c = classify(res.status);
    throw new ApiError(messageFor(res.status, body), {
      status: res.status,
      errors: body && Array.isArray(body.errors) ? body.errors : null,
      payload: body,
      path,
      kind: c.kind,
      advice: c.advice,
      detail: body && typeof body.detail === 'string' && body.detail !== body.error ? body.detail : '',
    });
  }
  return body;
}

/**
 * 폴백을 허용하는 GET. 연결 실패(status 0)일 때만 mock 으로 대체한다.
 * 4xx/5xx 는 서버가 살아 있다는 뜻이므로 그대로 던져서 배너에 띄운다.
 */
async function getOrMock(path, mockFn, init, timeoutMs) {
  if (fallback) return mockFn();
  try {
    return await request(path, init, timeoutMs);
  } catch (e) {
    if (e instanceof ApiError && e.offline) {
      enterFallback(e.message + ' ' + e.advice);
      return mockFn();
    }
    throw e;
  }
}

function offlineWriteError(what) {
  return new ApiError(`${what}는 프로그램 서버가 켜져 있어야 가능합니다. 지금은 예시 데이터로 화면만 보고 있습니다.`, {
    status: 0, offline: true, kind: 'network',
    advice: '바탕화면의 KRX백테스터.vbs 를 실행한 뒤 이 화면을 새로고침하세요.',
  });
}

/* ============================================================
   4-1 / 4-2 데이터 상태
   ============================================================ */

export async function getStatus() {
  const st = await getOrMock('/status', () => mock.mockStatus());
  if (st && typeof st === 'object') setFeatures(st.features);
  return st;
}

/** POST /api/sync — 실패 응답 {ok:false,...} 도 ApiError 로 올린다 */
export async function postSync(signal) {
  if (fallback) {
    await new Promise((r) => setTimeout(r, 1500));   // 폴백에서도 진행 표시를 확인할 수 있게
    return mock.mockStatus();
  }
  const r = await request('/sync', { method: 'POST', signal }, SYNC_TIMEOUT_MS);
  if (r && r.ok === false) {
    throw new ApiError(r.error || '데이터 갱신에 실패했습니다.', {
      status: 200, payload: r, path: '/sync', kind: 'server',
      detail: r.log_tail || r.detail || '',
      advice: '프로젝트 폴더의 update_marcap.bat 을 직접 실행하면 자세한 오류를 볼 수 있습니다.',
    });
  }
  if (r && typeof r === 'object') setFeatures(r.features);
  return r;
}

/**
 * GET /api/sync/status — 진행률 폴링.
 * 현재 서버는 이 엔드포인트를 갖고 있지만 features 로 알려 주지는 않는다.
 * 그래서 기본값은 "있다"로 두되, 한 번이라도 실패하면 그 세션에서는 다시 부르지 않는다.
 * (없는 서버에서 매 2초마다 404 를 찍어 콘솔을 더럽히지 않기 위해)
 * @returns {Promise<object|null>} 지원하지 않으면 null
 */
let syncStatusOk = true;
export async function getSyncStatus() {
  if (fallback) return mock.mockSyncStatus(1);
  if (!syncStatusOk || !hasFeature('sync_status', true)) return null;
  try {
    return await request('/sync/status', {}, 8000);
  } catch (e) {
    // 404/405 는 "이 서버엔 없다"는 뜻이므로 더 이상 찌르지 않는다
    if (e instanceof ApiError && (e.status === 404 || e.status === 405)) syncStatusOk = false;
    return null;
  }
}

/* ============================================================
   4-3 전략
   ============================================================ */

export function listStrategies() {
  return getOrMock('/strategies', () => mock.mockStrategies());
}

export function getStrategy(id) {
  return getOrMock(`/strategies/${encodeURIComponent(id)}`, () => mock.mockStrategy(id));
}

export function putStrategy(id, strategy) {
  if (fallback) return Promise.reject(offlineWriteError('전략 저장'));
  return request(`/strategies/${encodeURIComponent(id)}`, {
    method: 'PUT', body: JSON.stringify(strategy),
  });
}

export function createStrategy(strategy) {
  if (fallback) return Promise.reject(offlineWriteError('전략 생성'));
  return request('/strategies', { method: 'POST', body: JSON.stringify(strategy) });
}

export function deleteStrategy(id) {
  if (fallback) return Promise.reject(offlineWriteError('전략 삭제'));
  return request(`/strategies/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export function validateStrategy(strategy) {
  if (fallback) return Promise.resolve({ valid: true, errors: [] });
  return request('/strategies/validate', { method: 'POST', body: JSON.stringify(strategy) });
}

/* ============================================================
   4-4 지표
   ============================================================ */

export function listIndicators() {
  return getOrMock('/indicators', () => mock.mockIndicators());
}

/* ============================================================
   4-5 백테스트
   ============================================================ */

/** 진행률 SSE 와 짝을 맞추기 위한 클라이언트 생성 job_id */
export function newJobId() {
  return 'job_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
}

/**
 * @param {object} strategy
 * @param {object} period
 * @param {object} opts {jobId, signal}
 */
export function runBacktest(strategy, period, opts = {}) {
  if (fallback) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => resolve(mock.mockBacktest(strategy)), 1400);
      if (opts.signal) {
        opts.signal.addEventListener('abort', () => {
          clearTimeout(t);
          reject(new ApiError('사용자가 취소했습니다.', { kind: 'aborted', aborted: true }));
        }, { once: true });
      }
    });
  }
  const body = { strategy, period: period || strategy.period };
  if (opts.jobId) body.job_id = opts.jobId;
  return request('/backtest', {
    method: 'POST', body: JSON.stringify(body), signal: opts.signal,
  }, BACKTEST_TIMEOUT_MS).then((r) => {
    // v2 §2-2: 취소는 오류(499)가 아니라 200 + cancelled:true 로 온다.
    // 오류 배너가 뜨지 않도록 aborted 로 표준화해서 던진다.
    if (r && r.ok === false && r.cancelled) {
      throw new ApiError(r.error || '사용자가 취소했습니다.', { kind: 'aborted', aborted: true, payload: r });
    }
    if (r && r.ok === false) {
      throw new ApiError(r.error || '백테스트에 실패했습니다.', {
        status: 200, payload: r, path: '/backtest', kind: 'server', detail: r.detail || '',
      });
    }
    return r;
  });
}

/**
 * 백테스트 진행률 SSE (`GET /api/backtest/progress/{job_id}`).
 * 프런트가 job_id 를 만들어 먼저 열고, 같은 job_id 로 백테스트를 실행한다.
 * 서버가 명시적으로 미지원이라고 알리면 열지 않는다.
 * @returns {EventSource|null}
 */
export function backtestProgressStream(jobId) {
  if (fallback || !jobId || typeof EventSource === 'undefined') return null;
  if (!hasFeature('backtest_progress_sse', true)) return null;
  try {
    return new EventSource(`${BASE}/backtest/progress/${encodeURIComponent(jobId)}`);
  } catch {
    return null;
  }
}

/**
 * POST /api/backtest/cancel/{job_id} (ARCHITECTURE-v2 §2-2)
 * 서버가 취소를 지원한다고 알린 경우에만 호출한다.
 * @returns {Promise<object|null>} 미지원이면 null
 */
export async function cancelBacktest(jobId) {
  if (fallback || !jobId) return null;
  if (!hasFeature('backtest_cancel', false)) return null;
  try {
    return await request(`/backtest/cancel/${encodeURIComponent(jobId)}`, { method: 'POST' }, 15000);
  } catch (e) {
    // 404 = 이미 끝났거나 없는 job. 취소 실패를 오류로 키우지 않는다.
    if (e instanceof ApiError && e.status === 404) return { ok: true, cancelled: false, reason: '이미 완료됨' };
    throw e;
  }
}

/**
 * POST /api/strategies/{id}/reset — params[].default 로 되돌리기 (v2 §1)
 * 서버가 아직 구현하지 않았으면(404/501) null 을 돌려주고,
 * 호출부가 클라이언트에서 직접 default 를 적용한다.
 * @returns {Promise<object|null>} 되돌린 전략, 미지원이면 null
 */
let resetUnsupported = false;   // 서버에 reset 이 없다는 걸 한 번 확인하면 다시 찌르지 않는다

export async function resetStrategy(id) {
  if (fallback || resetUnsupported) return null;
  // features 에 명시적으로 false 라고 적혀 있으면 아예 호출하지 않는다.
  if (!hasFeature('strategy_reset', true)) { resetUnsupported = true; return null; }
  try {
    return await request(`/strategies/${encodeURIComponent(id)}/reset`, { method: 'POST' }, 20000);
  } catch (e) {
    // 아직 구현하지 않은 서버 → 이번 세션 동안은 클라이언트 폴백만 쓴다
    if (e instanceof ApiError && (e.status === 404 || e.status === 501 || e.status === 405)) {
      resetUnsupported = true;
      return null;
    }
    throw e;
  }
}

/* ============================================================
   4-6 차트 / 종목 검색
   ============================================================ */

/**
 * @param {object} q {code, start, end, indicators, run_id, n, signal, timeout}
 *   start/end 를 주면 그 구간만 받는다. 전체 히스토리를 한 번에 받으면
 *   서버가 32개 연도 파일을 전부 읽어야 해서 10초를 넘긴다 (점진 로딩 참고).
 */
export function getChart(q = {}) {
  const p = new URLSearchParams();
  if (q.code) p.set('code', q.code);
  if (q.start) p.set('start', q.start);
  if (q.end) p.set('end', q.end);
  if (q.indicators && q.indicators.length) p.set('indicators', q.indicators.join(','));
  if (q.run_id) p.set('run_id', q.run_id);
  const qs = p.toString();
  return getOrMock(
    `/chart${qs ? '?' + qs : ''}`,
    () => mock.mockChart({ code: q.code, n: q.n, indicators: q.indicators, start: q.start, end: q.end }),
    q.signal ? { signal: q.signal } : undefined,
    q.timeout || CHART_TIMEOUT_MS,
  );
}

/**
 * GET /api/symbols?q=&limit= — 종목 검색.
 * 서버가 기능을 알리지 않으면 null 을 돌려주고, 호출부는 종목코드 직접 입력으로 대체한다.
 * @returns {Promise<{total:number, symbols:Array}|null>}
 */
export async function searchSymbols(q, limit = 30, signal) {
  if (fallback) return mock.mockSymbols(q, limit);
  if (!hasFeature('symbol_search', false)) return null;
  const p = new URLSearchParams();
  if (q) p.set('q', q);
  p.set('limit', String(limit));
  return request(`/symbols?${p.toString()}`, { signal }, 10000);
}

/* ============================================================
   4-7 AI 전략 생성
   ============================================================ */

/** 이 엔드포인트는 {ok:false} 를 정상 응답으로 취급한다 (API 키 없음 시나리오) */
export async function aiStrategy(prompt, base) {
  if (fallback) return mock.mockAiStrategy(prompt);
  return request('/ai/strategy', {
    method: 'POST',
    body: JSON.stringify(base ? { prompt, base } : { prompt }),
  }, 120000);
}

/* ============================================================
   유틸
   ============================================================ */

export function forceFallback(reason = '개발용 예시 데이터 모드입니다 (?mock=1).') {
  enterFallback(reason);
}
