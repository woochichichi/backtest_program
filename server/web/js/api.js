/* ============================================================
   api.js — 백엔드 REST 래퍼
   ------------------------------------------------------------
   · ARCHITECTURE.md 4장 스펙을 그대로 믿고 호출한다.
   · 에러를 절대 삼키지 않는다. 실패는 ApiError 로 감싸서 던지고,
     호출부가 화면 상단 배너에 한국어 안내를 띄운다.
   · 백엔드가 아예 없을 때(연결 실패)만 mockdata 폴백을 허용하며,
     그 사실을 isFallback() 으로 노출해 화면에 "예시 데이터" 배지를 띄운다.
   ============================================================ */

import * as mock from './mockdata.js';

const BASE = '/api';
const TIMEOUT_MS = 20000;
const BACKTEST_TIMEOUT_MS = 300000; // 백테스트는 오래 걸릴 수 있다

/** 백엔드 연결 실패로 폴백 데이터를 쓰고 있는가 */
let fallback = false;
/** 폴백 전환 사유 (배너 문구용) */
let fallbackReason = '';

export function isFallback() { return fallback; }
export function fallbackNote() { return fallbackReason; }

/** 폴백 상태가 바뀌면 화면이 배지를 갱신할 수 있도록 알린다 */
function enterFallback(reason) {
  if (fallback) return;
  fallback = true;
  fallbackReason = reason;
  window.dispatchEvent(new CustomEvent('api:fallback', { detail: { reason } }));
}

export class ApiError extends Error {
  /**
   * @param {string} message  사용자에게 보여줄 한국어 메시지
   * @param {object} opts
   */
  constructor(message, opts = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = opts.status ?? 0;      // HTTP 상태 (0 = 연결 실패)
    this.errors = opts.errors ?? null;   // 422 검증 오류 [{path, message}]
    this.payload = opts.payload ?? null; // 원본 응답 본문
    this.path = opts.path ?? '';
    this.offline = opts.offline === true;
    this.howTo = opts.howTo ?? '';       // "무엇을 해야 하는지" 안내
  }
}

/**
 * 상태 코드별 기본 한국어 문구.
 * 서버 규약: `error` = 사용자에게 보여줄 한국어, `detail` = 기술적 원인.
 * 따라서 error 를 먼저 쓰고, 없을 때만 detail 로 내려간다.
 */
function messageFor(status, path, body) {
  const msg = body && (body.error || body.detail || body.message);
  if (typeof msg === 'string' && msg.trim()) return msg.trim();
  switch (status) {
    case 400: return `요청이 올바르지 않습니다. (${path})`;
    case 404: return `요청한 리소스를 찾을 수 없습니다. (${path})`;
    case 422: return '전략 검증에 실패했습니다. 아래 표시된 항목을 확인하세요.';
    case 409: return `이미 처리 중이거나 충돌이 발생했습니다. (${path})`;
    case 500: return `서버 내부 오류가 발생했습니다. 서버 콘솔 로그를 확인하세요. (${path})`;
    case 503: return '서버가 아직 준비되지 않았습니다. 잠시 후 다시 시도하세요.';
    default:  return `요청이 실패했습니다. (HTTP ${status} · ${path})`;
  }
}

/**
 * 저수준 fetch.
 * @param {string} path      '/status' 처럼 /api 이후 경로
 * @param {object} init
 * @param {number} timeoutMs
 */
async function request(path, init = {}, timeoutMs = TIMEOUT_MS) {
  const url = BASE + path;
  const ctrl = new AbortController();
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
    const aborted = e && e.name === 'AbortError';
    throw new ApiError(
      aborted
        ? `서버 응답이 ${Math.round(timeoutMs / 1000)}초 안에 오지 않았습니다. (${path})`
        : '백엔드 서버에 연결할 수 없습니다.',
      {
        status: 0,
        offline: true,
        path,
        howTo: aborted
          ? '서버가 무거운 작업 중일 수 있습니다. 서버 콘솔을 확인한 뒤 다시 시도하세요.'
          : 'run_server.bat 을 실행해 FastAPI 서버를 먼저 띄우세요.',
      },
    );
  }
  clearTimeout(timer);

  const text = await res.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = { raw: text }; }
  }

  if (!res.ok) {
    throw new ApiError(messageFor(res.status, path, body), {
      status: res.status,
      errors: body && Array.isArray(body.errors) ? body.errors : null,
      payload: body,
      path,
      // detail 은 원인 설명이라 error 와 다를 때만 안내로 덧붙인다
      howTo: body && typeof body.detail === 'string' && body.detail !== body.error ? body.detail : '',
    });
  }
  return body;
}

/**
 * 폴백을 허용하는 GET. 연결 실패(status 0)일 때만 mock 으로 대체한다.
 * 4xx/5xx 는 서버가 살아 있다는 뜻이므로 그대로 던져서 배너에 띄운다.
 */
async function getOrMock(path, mockFn, init) {
  if (fallback) return mockFn();
  try {
    return await request(path, init);
  } catch (e) {
    if (e instanceof ApiError && e.offline) {
      enterFallback(e.message + ' ' + e.howTo);
      return mockFn();
    }
    throw e;
  }
}

/* ---------------- 4-1 / 4-2 데이터 상태 ---------------- */

export function getStatus() {
  return getOrMock('/status', () => mock.mockStatus());
}

/** POST /api/sync — 실패 응답 {ok:false,...} 도 ApiError 로 올린다 */
export async function postSync() {
  const r = await request('/sync', { method: 'POST' }, 180000);
  if (r && r.ok === false) {
    throw new ApiError(r.error || '데이터 갱신에 실패했습니다.', {
      status: 200, payload: r, path: '/sync',
      howTo: r.log_tail ? `로그 마지막 줄:\n${r.log_tail}` : 'update_marcap.bat 을 직접 실행해 오류를 확인하세요.',
    });
  }
  return r;
}

/* ---------------- 4-3 전략 ---------------- */

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

function offlineWriteError(what) {
  return new ApiError(`${what}는 백엔드 서버가 있어야 가능합니다. 지금은 예시 데이터로 화면만 보고 있습니다.`, {
    status: 0, offline: true, howTo: 'run_server.bat 을 실행한 뒤 페이지를 새로고침하세요.',
  });
}

/* ---------------- 4-4 지표 ---------------- */

export function listIndicators() {
  return getOrMock('/indicators', () => mock.mockIndicators());
}

/* ---------------- 4-5 백테스트 ---------------- */

export function runBacktest(strategy, period) {
  if (fallback) {
    // 폴백에서도 화면 흐름은 확인할 수 있어야 하므로 약간의 지연을 흉내낸다.
    return new Promise((resolve) => setTimeout(() => resolve(mock.mockBacktest(strategy)), 700));
  }
  return request('/backtest', {
    method: 'POST',
    body: JSON.stringify({ strategy, period: period || strategy.period }),
  }, BACKTEST_TIMEOUT_MS);
}

/**
 * 백테스트 진행률 SSE.
 * ARCHITECTURE 4장에 SSE 엔드포인트가 정의되어 있지 않으므로 무조건 열지 않는다.
 * (없는 URL 로 EventSource 를 열면 콘솔에 404 에러가 남는다)
 * `GET /api/status` 응답에 features.backtest_progress_sse: true 가 있을 때만 연결하고,
 * 그 외에는 null 을 돌려 호출부가 인디터미닛 진행바를 쓰게 한다.
 * @returns {EventSource|null}
 */
export function backtestProgressStream(status, runId) {
  const on = status && status.features && status.features.backtest_progress_sse === true;
  if (!on || fallback || typeof EventSource === 'undefined') return null;
  try {
    return new EventSource(`${BASE}/backtest/progress${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`);
  } catch {
    return null;
  }
}

/* ---------------- 4-6 차트 ---------------- */

/**
 * @param {object} q {code, start, end, indicators:string[], run_id}
 */
export function getChart(q = {}) {
  const p = new URLSearchParams();
  if (q.code) p.set('code', q.code);
  if (q.start) p.set('start', q.start);
  if (q.end) p.set('end', q.end);
  if (q.indicators && q.indicators.length) p.set('indicators', q.indicators.join(','));
  if (q.run_id) p.set('run_id', q.run_id);
  const qs = p.toString();
  return getOrMock(`/chart${qs ? '?' + qs : ''}`, () => mock.mockChart({
    code: q.code, n: q.n, indicators: q.indicators,
  }));
}

/* ---------------- 4-7 AI 전략 생성 ---------------- */

/** 이 엔드포인트는 {ok:false} 를 정상 응답으로 취급한다 (API 키 없음 시나리오) */
export async function aiStrategy(prompt, base) {
  if (fallback) return mock.mockAiStrategy(prompt);
  return request('/ai/strategy', {
    method: 'POST',
    body: JSON.stringify(base ? { prompt, base } : { prompt }),
  }, 120000);
}

/* ---------------- 유틸 ---------------- */

/** 폴백 상태를 강제로 켠다 (개발용 ?mock=1) */
export function forceFallback(reason = '개발용 예시 데이터 모드입니다 (?mock=1).') {
  enterFallback(reason);
}
