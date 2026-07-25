/**
 * mockdata.js — 백엔드 미가동 시 UI가 그대로 렌더되도록 하는 결정론적 폴백 데이터 생성기.
 *
 * 규칙
 *  - 순수 ES 모듈. import 없음, 빌드 도구 없음, 외부 의존 없음.
 *  - 브라우저 기본 난수 함수는 쓰지 않는다. 모든 난수는 시드 고정 LCG에서 나온다.
 *    각 생성 함수는 진입 시 자기 시드를 리셋하므로 호출 순서가 결과를 바꾸지 않는다.
 *  - 모듈 최상위에는 순수 상수/헬퍼만 둔다. 부작용(side effect) 없음.
 *  - 응답 모양은 ARCHITECTURE.md 4장을 그대로 따른다.
 */

/* ────────────────────────────── 공용 헬퍼 (순수) ────────────────────────────── */

/** 선형 합동 생성기(LCG). 시드가 같으면 항상 같은 수열을 낸다. */
function makeRng(seed) {
  let s = (seed >>> 0) & 0x7fffffff;
  if (s === 0) s = 1;
  return function rnd() {
    s = (s * 1664525 + 1013904223) & 0x7fffffff;
    return s / 0x7fffffff; // [0, 1)
  };
}

/** 균등난수 3개를 더해 만든 유사 정규분포(-1.5 ~ +1.5). 캔들 변동에 쓴다. */
function gauss(rnd) {
  return rnd() + rnd() + rnd() - 1.5;
}

/** [min, max] 구간으로 눌러 담는다. */
function clamp(v, min, max) {
  return v < min ? min : v > max ? max : v;
}

/** 소수 n자리 반올림. 부동소수 오차를 한 곳에서만 만든다. */
function round(v, digits) {
  const p = Math.pow(10, digits || 0);
  return Math.round(v * p) / p;
}

/** 3자리 콤마. toLocaleString 대신 직접 구현해 환경 의존성을 없앤다. */
function fmtWon(v) {
  return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function pad2(v) {
  return v < 10 ? '0' + v : String(v);
}

/** Date(UTC) → 20260724 정수 */
function toYmdInt(d) {
  return d.getUTCFullYear() * 10000 + (d.getUTCMonth() + 1) * 100 + d.getUTCDate();
}

/** Date(UTC) → "2026-07-24" */
function toYmdDash(d) {
  return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
}

function isWeekend(d) {
  const w = d.getUTCDay();
  return w === 0 || w === 6;
}

function addDays(d, n) {
  return new Date(d.getTime() + n * 86400000);
}

/** 주말을 건너뛰며 end에서 n개 영업일을 거꾸로 세어 오름차순 배열로 돌려준다. */
function tradingDaysEndingAt(endDate, n) {
  const out = [];
  let cur = new Date(endDate.getTime());
  while (isWeekend(cur)) cur = addDays(cur, -1);
  while (out.length < n) {
    out.push(new Date(cur.getTime()));
    cur = addDays(cur, -1);
    while (isWeekend(cur)) cur = addDays(cur, -1);
  }
  out.reverse();
  return out;
}

/** start부터 end까지(포함) 주말을 뺀 영업일 배열. */
function tradingDaysBetween(startDate, endDate) {
  const out = [];
  let cur = new Date(startDate.getTime());
  while (cur.getTime() <= endDate.getTime()) {
    if (!isWeekend(cur)) out.push(new Date(cur.getTime()));
    cur = addDays(cur, 1);
  }
  return out;
}

/** "2026-07-24" → Date(UTC) */
function parseYmd(str) {
  const p = String(str).split('-');
  return new Date(Date.UTC(Number(p[0]), Number(p[1]) - 1, Number(p[2])));
}

/** 종가 배열의 단순이동평균. 워밍업 구간은 null. O(n) 롤링 합. */
function smaSeries(close, period) {
  const n = close.length;
  const out = new Array(n).fill(null);
  if (period <= 0 || period > n) return out;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    sum += close[i];
    if (i >= period) sum -= close[i - period];
    if (i >= period - 1) out[i] = round(sum / period, 2);
  }
  return out;
}

/** 지수이동평균. 첫 값은 period 구간 SMA로 시딩한다. */
function emaSeries(close, period) {
  const n = close.length;
  const out = new Array(n).fill(null);
  if (period <= 0 || period > n) return out;
  const k = 2 / (period + 1);
  let sum = 0;
  for (let i = 0; i < period; i++) sum += close[i];
  let prev = sum / period;
  out[period - 1] = round(prev, 2);
  for (let i = period; i < n; i++) {
    prev = close[i] * k + prev * (1 - k);
    out[i] = round(prev, 2);
  }
  return out;
}

/** 기본 마지막 거래일. 2026-07-24(금). */
const LAST_TRADE_DATE = '2026-07-24';

/** 백테스트 trades 생성에 쓰는 실존 코드/종목명 풀. */
const STOCK_POOL = [
  ['042700', '한미반도체'],
  ['005930', '삼성전자'],
  ['000660', 'SK하이닉스'],
  ['247540', '에코프로비엠'],
  ['086520', '에코프로'],
  ['196170', '알테오젠'],
  ['112040', '위메이드'],
  ['035420', 'NAVER'],
  ['035720', '카카오'],
  ['373220', 'LG에너지솔루션'],
  ['009150', '삼성전기'],
  ['066970', '엘앤에프'],
  ['277810', '레인보우로보틱스'],
  ['039030', '이오테크닉스'],
  ['058470', '리노공업'],
  ['357780', '솔브레인'],
  ['108320', 'LX세미콘'],
  ['095340', 'ISC'],
  ['240810', '원익IPS'],
  ['403870', 'HPSP'],
];

/** ARCHITECTURE 2장이 요구하는 1분봉 근사 경고 문구. */
const WARN_1M_APPROX = '1분봉 데이터가 없어 일봉 근사로 체결했습니다.';

/* ────────────────────────────── 4-1. GET /api/status ────────────────────────────── */

/**
 * 데이터가 정상적으로 붙어 있는 것처럼 보이는 상태 페이로드.
 * available:true, auto_sync.registered:true 로 고정한다.
 */
export function mockStatus() {
  return {
    available: true,
    last_sync: '2026-07-25 08:12:04',
    result: 'updated',
    latest_trade_date: LAST_TRADE_DATE,
    first_trade_date: '1995-05-02',
    file_count: 32,
    git_rev: 'a1b2c3d',
    row_count: 11043912,
    auto_sync: { registered: true, time: '18:30', task: 'KRXBacktesterDataSync' },
  };
}

/* ────────────────────────────── 4-3. 전략 목록 / 전문 ────────────────────────────── */

/**
 * GET /api/strategies 의 요약 목록.
 * 첫 항목의 id는 반드시 "strategy1" (프런트가 초기 선택에 쓴다).
 */
export function mockStrategies() {
  return [
    {
      id: 'strategy1',
      name: '전략1 — 거래대금 급증 눌림목',
      description:
        '최근 20영업일 안에 일 거래대금 1,000억 이상이 발생한 날(기준일)을 찾는다. 기준일 직전일 거래대금이 200억 이하이고, 기준일 이후 주가가 기준일 시가까지 하락하면 시가에 50% 매수한다. 1차 매수가 대비 -10%에서 나머지 50%를 추가 매수한다. 평단가 +10%에서 전량 익절하고, 45일 이동평균선에 도달하면 전량 손절한다.',
      enabled: true,
      updated_at: '2026-07-25',
    },
    {
      id: 'strategy2',
      name: '전략2 — 골든크로스 추세추종',
      description:
        '5일 이동평균이 20일 이동평균을 상향 돌파하는 날 종가에 전량 진입한다. 청산은 ATR(14) 3배 추적손절과 보유 40영업일 시간청산을 함께 쓴다.',
      enabled: true,
      updated_at: '2026-07-20',
    },
    {
      id: 'strategy3',
      name: '전략3 — RSI 과매도 반등 (비활성)',
      description:
        'RSI(14)가 30 아래로 내려간 뒤 다시 30을 상향 돌파하면 진입하고, RSI 65 도달 또는 -7% 손절로 청산한다.',
      enabled: false,
      updated_at: '2026-06-11',
    },
  ];
}

/**
 * GET /api/strategies/{id} 의 DSL 전문.
 * "strategy1" 은 strategies/strategy1.json 과 동일한 객체를 인라인 리터럴로 돌려준다.
 * 그 외 id는 SCHEMA.md 구조를 지키는 골든크로스 + ATR 추적손절 전략을 돌려준다.
 */
export function mockStrategy(id) {
  if (id === undefined || id === null || id === 'strategy1') {
    return {
      schema: 'krx-backtest-strategy/v1',
      id: 'strategy1',
      name: '전략1 — 거래대금 급증 눌림목',
      description:
        '최근 20영업일 안에 일 거래대금 1,000억 이상이 발생한 날(기준일)을 찾는다. 기준일 직전일 거래대금이 200억 이하이고, 기준일 이후 주가가 기준일 시가까지 하락하면 시가에 50% 매수한다. 1차 매수가 대비 -10%에서 나머지 50%를 추가 매수한다. 평단가 +10%에서 전량 익절하고, 45일 이동평균선에 도달하면 전량 손절한다.',
      author: 'user',
      created_at: '2026-07-25',
      enabled: true,

      market: {
        country: 'KR',
        asset: 'stock',
        bar: '1d',
        trade_resolution: '1m',
      },

      universe: {
        markets: ['KOSPI', 'KOSDAQ'],
        exclude: ['ETF', 'ETN', 'SPAC', 'PREFERRED', 'ADMIN_ISSUE', 'TRADE_HALT'],
        reference_day: {
          rule: 'amount_spike',
          lookback_days: 20,
          spike_amount_krw_eok: 1000,
          prev_day_amount_max_eok: 200,
          comment:
            'lookback_days 안에서 거래대금 >= spike_amount 인 날을 기준일로 삼는다. 기준일 직전 영업일 거래대금이 prev_day_amount_max 이하여야 한다. 조건을 만족하는 날이 복수면 가장 최근 날을 기준일로 채택한다.',
        },
        condition: 'close_below_reference_open',
        valid_days_after_reference: 60,
      },

      entries: [
        {
          id: 'B1',
          label: '1차 매수 (기준일 시가 도달)',
          when: { op: '<=', left: 'low', right: 'ref.open' },
          price: 'ref.open',
          size_pct: 50,
          size_of: 'planned_position',
        },
        {
          id: 'B2',
          label: '2차 매수 (1차 매수가 -10%)',
          requires: ['B1'],
          trigger_pct: -10,
          when: {
            op: '<=',
            left: 'low',
            right: { expr: 'fill.B1.price * (1 + trigger_pct / 100)' },
          },
          price: { expr: 'fill.B1.price * (1 + trigger_pct / 100)' },
          size_pct: 50,
          size_of: 'planned_position',
        },
      ],

      exits: [
        {
          id: 'TP',
          label: '익절 (평단 +10%)',
          type: 'take_profit',
          target_pct: 10,
          when: {
            op: '>=',
            left: 'high',
            right: { expr: 'position.avg_price * (1 + target_pct / 100)' },
          },
          price: { expr: 'position.avg_price * (1 + target_pct / 100)' },
          size_pct: 100,
        },
        {
          id: 'SL',
          label: '손절 (45일선 도달)',
          type: 'stop_loss',
          ma_period: 45,
          when: {
            op: '<=',
            left: 'low',
            right: { indicator: 'SMA', period: 45, source: 'close' },
          },
          price: { indicator: 'SMA', period: 45, source: 'close' },
          size_pct: 100,
        },
      ],

      exit_priority: ['SL', 'TP'],

      indicators: [
        { key: 'MA45', type: 'SMA', period: 45, source: 'close', plot: true, color: '#ba68c8' },
        { key: 'MA20', type: 'SMA', period: 20, source: 'close', plot: true, color: '#ffb74d' },
      ],

      portfolio: {
        initial_capital_manwon: 10000,
        max_positions: 10,
        position_sizing: 'equal_weight',
        allow_duplicate_symbol: false,
      },

      execution: {
        resolution: '1m',
        fill_model: 'touch',
        slippage_pct: 0.1,
        fee_pct: 0.23,
        comment: 'fee_pct는 매도 시 증권거래세 0.18% + 양방향 수수료 근사치를 합산한 값이다.',
      },

      period: {
        start: '2025-01-02',
        end: 'auto',
      },
    };
  }

  // strategy1 외의 id는 두 번째 전략(골든크로스 MA5/MA20 + ATR 추적손절)을 돌려준다.
  return {
    schema: 'krx-backtest-strategy/v1',
    id: String(id),
    name: '전략2 — 골든크로스 추세추종',
    description:
      '5일 이동평균이 20일 이동평균을 상향 돌파하는 날 종가에 전량 진입한다. 청산은 ATR(14) 3배 추적손절과 보유 40영업일 시간청산을 함께 쓴다.',
    author: 'user',
    created_at: '2026-07-20',
    enabled: true,

    market: {
      country: 'KR',
      asset: 'stock',
      bar: '1d',
      trade_resolution: '1d',
    },

    universe: {
      markets: ['KOSPI', 'KOSDAQ'],
      exclude: ['ETF', 'ETN', 'SPAC', 'PREFERRED', 'ADMIN_ISSUE', 'TRADE_HALT'],
      reference_day: {
        rule: 'none',
      },
      condition: 'none',
      valid_days_after_reference: 10,
    },

    entries: [
      {
        id: 'B1',
        label: '골든크로스 진입 (MA5 > MA20)',
        when: {
          op: 'and',
          conditions: [
            {
              op: 'cross_above',
              left: { indicator: 'SMA', period: 5, source: 'close' },
              right: { indicator: 'SMA', period: 20, source: 'close' },
            },
            { op: '>=', left: 'amount', right: 3000000000 },
          ],
        },
        price: 'close',
        size_pct: 100,
        size_of: 'planned_position',
      },
    ],

    exits: [
      {
        id: 'TS',
        label: 'ATR 추적손절 (ATR14 × 3)',
        type: 'trailing_stop',
        atr_period: 14,
        atr_mult: 3,
        when: {
          op: '<=',
          left: 'low',
          right: { expr: 'position.max_close - 3 * atr14' },
        },
        price: { expr: 'position.max_close - 3 * atr14' },
        size_pct: 100,
      },
      {
        id: 'TIME',
        label: '시간청산 (보유 40영업일)',
        type: 'time_exit',
        max_hold_days: 40,
        when: { op: '>=', left: 'position.hold_days', right: 40 },
        price: 'close',
        size_pct: 100,
      },
    ],

    exit_priority: ['TS', 'TIME'],

    indicators: [
      { key: 'MA5', type: 'SMA', period: 5, source: 'close', plot: true, color: '#4fc3f7' },
      { key: 'MA20', type: 'SMA', period: 20, source: 'close', plot: true, color: '#ffb74d' },
      { key: 'ATR14', type: 'ATR', period: 14, plot: false, color: '#81c784' },
    ],

    portfolio: {
      initial_capital_manwon: 5000,
      max_positions: 8,
      position_sizing: 'equal_weight',
      allow_duplicate_symbol: false,
    },

    execution: {
      resolution: '1d',
      fill_model: 'close',
      slippage_pct: 0.1,
      fee_pct: 0.23,
    },

    period: {
      start: '2024-01-02',
      end: 'auto',
    },
  };
}

/* ────────────────────────────── 4-4. GET /api/indicators ────────────────────────────── */

/**
 * 지표 칩/추가 다이얼로그를 그리는 데 쓰는 스펙 목록.
 * SCHEMA.md 지표 표의 14개 타입을 모두 담는다.
 *  - fields : 단일 시리즈면 null, 서브 필드가 있으면 이름 배열
 *  - overlay: 가격 패널 위에 그리면 true, 별도 패널 오실레이터면 false
 */
export function mockIndicators() {
  const source = {
    name: 'source',
    type: 'enum',
    default: 'close',
    options: ['close', 'open', 'high', 'low', 'hl2', 'ohlc4'],
  };
  const period = (def, min, max) => ({
    name: 'period',
    type: 'int',
    default: def,
    min: min === undefined ? 2 : min,
    max: max === undefined ? 400 : max,
    step: 1,
  });

  return [
    {
      key: 'SMA',
      label: '단순이동평균',
      params: [period(20), Object.assign({}, source)],
      fields: null,
      overlay: true,
    },
    {
      key: 'EMA',
      label: '지수이동평균',
      params: [period(20), Object.assign({}, source)],
      fields: null,
      overlay: true,
    },
    {
      key: 'WMA',
      label: '가중이동평균',
      params: [period(20), Object.assign({}, source)],
      fields: null,
      overlay: true,
    },
    {
      key: 'BBANDS',
      label: '볼린저 밴드',
      params: [
        period(20),
        { name: 'stddev', type: 'float', default: 2, min: 0.5, max: 5, step: 0.1 },
        Object.assign({}, source),
      ],
      fields: ['upper', 'middle', 'lower'],
      overlay: true,
    },
    {
      key: 'RSI',
      label: '상대강도지수',
      params: [period(14)],
      fields: null,
      overlay: false,
    },
    {
      key: 'MACD',
      label: '이동평균 수렴확산',
      params: [
        { name: 'fast', type: 'int', default: 12, min: 2, max: 200, step: 1 },
        { name: 'slow', type: 'int', default: 26, min: 3, max: 400, step: 1 },
        { name: 'signal', type: 'int', default: 9, min: 2, max: 100, step: 1 },
      ],
      fields: ['macd', 'signal', 'hist'],
      overlay: false,
    },
    {
      key: 'STOCH',
      label: '스토캐스틱',
      params: [
        { name: 'k', type: 'int', default: 14, min: 2, max: 200, step: 1 },
        { name: 'd', type: 'int', default: 3, min: 1, max: 100, step: 1 },
        { name: 'smooth', type: 'int', default: 3, min: 1, max: 100, step: 1 },
      ],
      fields: ['k', 'd'],
      overlay: false,
    },
    {
      key: 'ATR',
      label: '평균 실체범위',
      params: [period(14)],
      fields: null,
      overlay: false,
    },
    {
      key: 'ADX',
      label: '추세 강도',
      params: [period(14)],
      fields: ['adx', 'pdi', 'mdi'],
      overlay: false,
    },
    {
      key: 'CCI',
      label: '상품채널지수',
      params: [period(20)],
      fields: null,
      overlay: false,
    },
    {
      key: 'OBV',
      label: '누적 거래량',
      params: [],
      fields: null,
      overlay: false,
    },
    {
      key: 'VWAP',
      label: '거래량가중평균가',
      params: [
        {
          name: 'anchor',
          type: 'enum',
          default: 'session',
          options: ['session', 'week', 'month', 'year'],
        },
      ],
      fields: null,
      overlay: true,
    },
    {
      key: 'ENVELOPE',
      label: '이격도 밴드',
      params: [
        period(20),
        { name: 'pct', type: 'float', default: 5, min: 0.5, max: 50, step: 0.5 },
        Object.assign({}, source),
      ],
      fields: ['upper', 'middle', 'lower'],
      overlay: true,
    },
    {
      key: 'DONCHIAN',
      label: '돈치안 채널',
      params: [period(20)],
      fields: ['upper', 'lower'],
      overlay: true,
    },
  ];
}

/* ────────────────────────────── 4-6. GET /api/chart ────────────────────────────── */

/**
 * 차트 페이로드. opts = {code, name, n, indicators} 모두 선택.
 *
 * 생성 절차
 *  1) 2026-07-24에서 주말을 빼며 n영업일을 거꾸로 세어 t를 만든다.
 *  2) 거래대금 급증일(기준일)을 미리 정하고, 그 뒤 눌림 → 회복 드리프트를 스크립트로 깐다.
 *     이렇게 해야 strategy1 스토리(기준일 → B1 → B2 → 익절)가 자연스럽게 성립한다.
 *  3) OHLC를 정수로 만들되 low <= min(o,c), high >= max(o,c)를 마지막에 강제한다.
 *  4) 마커/밴드/레벨은 실제 생성된 봉을 훑어 찾는다(못 찾으면 최소한으로 보정).
 *
 * 전 구간 O(n)이라 n=3000 이상도 싸다.
 */
export function mockChart(opts) {
  const o0 = opts || {};
  const n = Math.max(1, Math.floor(o0.n === undefined ? 3000 : o0.n));
  const code = o0.code || '042700';
  const name = o0.name || '한미반도체';

  // 지표 키 목록 정규화: 배열 또는 "SMA:20,SMA:45" 문자열 허용
  let indKeys = o0.indicators;
  if (typeof indKeys === 'string') indKeys = indKeys.split(',');
  if (!Array.isArray(indKeys) || indKeys.length === 0) indKeys = ['SMA:20', 'SMA:45'];
  indKeys = indKeys.map((k) => String(k).trim()).filter((k) => k.length > 0);

  const rnd = makeRng(20260724); // 함수 진입 시 시드 리셋 → 호출 순서 무관

  // 1) 날짜 축
  const days = tradingDaysEndingAt(parseYmd(LAST_TRADE_DATE), n);
  const t = new Array(n);
  for (let i = 0; i < n; i++) t[i] = toYmdInt(days[i]);

  // 2) 기준일(거래대금 급증) 위치를 먼저 정한다.
  //    간격 456봉 정도면 각 스토리(최대 ~130봉)가 겹치지 않는다.
  const spikeCount = Math.max(0, Math.min(6, Math.floor((n - 200) / 380)));
  const spikes = [];
  if (spikeCount > 0) {
    const first = 100;
    const step = Math.max(140, Math.floor((n - 160 - first) / spikeCount));
    for (let k = 0; k < spikeCount; k++) {
      const idx = first + k * step;
      if (idx >= 1 && idx < n - 20) spikes.push(idx);
    }
  }
  const spikeSet = {};
  for (let k = 0; k < spikes.length; k++) spikeSet[spikes[k]] = true;

  // 기준일 이후 드리프트 스크립트 (눌림 → 추가 눌림 → 회복)
  const drift = new Array(n).fill(0);
  let regimeDrift = 0.0006;
  let regimeVol = 0.018;
  const vol = new Array(n).fill(regimeVol);
  for (let i = 0; i < n; i++) {
    if (i % 90 === 0) {
      regimeDrift = -0.0016 + rnd() * 0.0038; // 국면별 드리프트
      regimeVol = 0.012 + rnd() * 0.018;
    }
    drift[i] = regimeDrift;
    vol[i] = regimeVol;
  }
  for (let k = 0; k < spikes.length; k++) {
    const sp = spikes[k];
    for (let j = sp + 1; j < Math.min(n, sp + 29); j++) drift[j] = -0.0085;
    for (let j = sp + 29; j < Math.min(n, sp + 51); j++) drift[j] = -0.0045;
    for (let j = sp + 51; j < Math.min(n, sp + 106); j++) drift[j] = 0.0075;
  }

  // 3) OHLCV 생성
  const o = new Array(n);
  const h = new Array(n);
  const l = new Array(n);
  const c = new Array(n);
  const v = new Array(n);
  const amt = new Array(n);

  const PRICE_MIN = 8000;
  const PRICE_MAX = 90000;
  let price = 18000 + Math.floor(rnd() * 22000); // 18,000 ~ 40,000원 출발

  for (let i = 0; i < n; i++) {
    const prevClose = i === 0 ? price : c[i - 1];

    // 시가: 전일 종가에서 소폭 갭
    let op = prevClose * (1 + gauss(rnd) * vol[i] * 0.35);

    // 종가: 국면 드리프트 + 변동성. 기준일은 거래대금 급증과 함께 크게 오른다.
    let ret = drift[i] + gauss(rnd) * vol[i];
    if (spikeSet[i]) ret = 0.14 + rnd() * 0.06;
    let cp = prevClose * (1 + ret);

    // 가격대 유지: 경계에서 반사시킨다.
    if (cp < PRICE_MIN) cp = PRICE_MIN + (PRICE_MIN - cp) * 0.5;
    if (cp > PRICE_MAX) cp = PRICE_MAX - (cp - PRICE_MAX) * 0.5;
    cp = clamp(cp, PRICE_MIN, PRICE_MAX);
    op = clamp(op, PRICE_MIN, PRICE_MAX);

    const hi = Math.max(op, cp) * (1 + rnd() * vol[i] * 0.8);
    const lo = Math.min(op, cp) * (1 - rnd() * vol[i] * 0.8);

    let oi = Math.round(op);
    let ci = Math.round(cp);
    let hi2 = Math.ceil(hi);
    let li2 = Math.floor(lo);

    // 정수 반올림 후에도 고가/저가 관계가 반드시 성립하도록 강제
    if (li2 < 1) li2 = 1;
    li2 = Math.min(li2, oi, ci);
    hi2 = Math.max(hi2, oi, ci);

    o[i] = oi;
    h[i] = hi2;
    l[i] = li2;
    c[i] = ci;

    // 거래량/거래대금(원 단위). amt ≈ v × 중간가
    const mid = Math.round((hi2 + li2 + ci * 2) / 4);
    let volume = Math.round((150000 + rnd() * 900000) * (1 + vol[i] * 8));
    let amount = volume * mid;

    if (spikeSet[i]) {
      // 거래대금 급증: 1,000억 ~ 2,200억
      amount = Math.round(1.0e11 + rnd() * 1.2e11);
      volume = Math.round(amount / mid);
      amount = volume * mid;
    } else if (spikeSet[i + 1]) {
      // 기준일 직전일은 조용해야 한다: 80억 ~ 180억 (<= 200억)
      amount = Math.round(8.0e9 + rnd() * 1.0e10);
      volume = Math.max(1, Math.round(amount / mid));
      amount = volume * mid;
    }
    v[i] = volume;
    amt[i] = amount;
  }

  // 4) strategy1 스토리 → markers / bands / levels
  const markers = [];
  const bands = [];
  const levels = [];

  for (let k = 0; k < spikes.length; k++) {
    const ri = spikes[k];
    const refOpen = o[ri];
    const refAmtEok = Math.round(amt[ri] / 1e8);

    // B1: 기준일 이후 60영업일 안에 저가가 기준일 시가에 닿는 첫 봉
    let b1 = -1;
    const b1Limit = Math.min(n - 1, ri + 60);
    for (let j = ri + 1; j <= b1Limit; j++) {
      if (l[j] <= refOpen) { b1 = j; break; }
    }
    if (b1 < 0 && ri + 12 < n) {
      // 자연 도달이 없으면 최소한으로 저가만 낮춰 스토리를 성립시킨다(고가/시가/종가는 그대로).
      b1 = ri + 12;
      l[b1] = Math.min(l[b1], refOpen);
    }
    if (b1 < 0) continue;
    const b1Price = refOpen;

    // B2: B1 체결가 -10%에 저가가 닿는 첫 봉 (없으면 생략)
    const b2Price = Math.round(b1Price * 0.9);
    let b2 = -1;
    const b2Limit = Math.min(n - 1, b1 + 60);
    for (let j = b1 + 1; j <= b2Limit; j++) {
      if (l[j] <= b2Price) { b2 = j; break; }
    }

    const lastBuy = b2 > 0 ? b2 : b1;
    const avg = b2 > 0 ? Math.round((b1Price + b2Price) / 2) : b1Price;
    const tpPrice = Math.round(avg * 1.1);

    // S: 평단 +10%에 고가가 닿는 첫 봉
    let sell = -1;
    const sellLimit = Math.min(n - 1, lastBuy + 120);
    for (let j = lastBuy + 1; j <= sellLimit; j++) {
      if (h[j] >= tpPrice) { sell = j; break; }
    }
    if (sell < 0 && lastBuy + 15 < n) {
      sell = lastBuy + 15;
      h[sell] = Math.max(h[sell], tpPrice); // 고가만 올린다 → OHLC 정합 유지
    }

    markers.push({
      i: ri,
      type: 'ref',
      price: refOpen,
      label: '기준일',
      note: '거래대금 ' + fmtWon(refAmtEok) + '억 급증 · 기준일 시가 ' + fmtWon(refOpen),
    });
    markers.push({
      i: b1,
      type: 'buy',
      price: b1Price,
      label: 'B1',
      note: '기준일 시가 도달 · 50% 매수 @ ' + fmtWon(b1Price),
    });
    if (b2 > 0) {
      markers.push({
        i: b2,
        type: 'buy',
        price: b2Price,
        label: 'B2',
        note: '1차 매수가 -10% 도달 · 50% 추가 매수 @ ' + fmtWon(b2Price),
      });
    }
    if (sell > 0) {
      markers.push({
        i: sell,
        type: 'sell',
        price: tpPrice,
        label: 'S',
        note: '평단 ' + fmtWon(avg) + ' +10% 도달 · 전량 익절 @ ' + fmtWon(tpPrice),
      });
      bands.push({ from: b1, to: sell, type: 'holding' });
    }
    levels.push({ price: refOpen, from: ri, label: '기준일 시가', type: 'ref_open' });
  }

  // 마커 인덱스는 오름차순이어야 한다(스토리가 겹쳐도 안전하도록 정렬).
  markers.sort((a, b) => a.i - b.i);

  // 5) 지표: "SMA:20" 같은 키 → 실제 계산 결과(워밍업 구간 null)
  const indicators = {};
  for (let k = 0; k < indKeys.length; k++) {
    const key = indKeys[k];
    const parts = key.split(':');
    const type = (parts[0] || 'SMA').toUpperCase();
    const period = Math.max(1, Math.floor(Number(parts[1]) || 20));
    indicators[key] = type === 'EMA' ? emaSeries(c, period) : smaSeries(c, period);
  }

  return { code, name, n, t, o, h, l, c, v, amt, indicators, markers, bands, levels };
}

/* ────────────────────────────── 4-5. POST /api/backtest ────────────────────────────── */

/**
 * 백테스트 결과. 화면에서 지표/자산곡선/월별/거래내역이 나란히 보이므로
 * 아래 관계가 반드시 성립하도록 "계산해서" 만든다(따로따로 난수 X).
 *
 *   metrics.trades          === trades.length
 *   metrics.wins + losses   === metrics.trades
 *   metrics.total_return_pct === equity.values 마지막 값 - 100
 *   metrics.mdd_pct         === min(equity.drawdown)
 *   metrics.final_capital   === round(initial_capital × (1 + total_return_pct/100))
 *   by_stock                 는 trades 배열을 집계해서 만든다
 */
export function mockBacktest(strategy) {
  const rnd = makeRng(19950502); // 함수 진입 시 시드 리셋

  // 자본 / 기간: 전략이 주어지면 그 값을 존중한다.
  const manwon =
    strategy && strategy.portfolio && typeof strategy.portfolio.initial_capital_manwon === 'number'
      ? strategy.portfolio.initial_capital_manwon
      : 10000;
  const initialCapital = Math.round(manwon * 10000);
  const maxPositions =
    strategy && strategy.portfolio && strategy.portfolio.max_positions
      ? strategy.portfolio.max_positions
      : 10;

  const periodIn = (strategy && strategy.period) || {};
  const startStr = periodIn.start || '2025-01-02';
  const endStr = !periodIn.end || periodIn.end === 'auto' ? LAST_TRADE_DATE : periodIn.end;

  const startDate = parseYmd(startStr);
  const endDate = parseYmd(endStr);
  const days = tradingDaysBetween(startDate, endDate);
  const len = days.length;
  const dates = new Array(len);
  for (let i = 0; i < len; i++) dates[i] = toYmdDash(days[i]);

  // ── 자산곡선: 100 기준 랜덤워크(드리프트 +) → 값·낙폭 모두 2자리 반올림
  const values = new Array(len);
  const drawdown = new Array(len);
  let eq = 100;
  let peak = 100;
  for (let i = 0; i < len; i++) {
    if (i > 0) eq = eq * (1 + 0.0009 + gauss(rnd) * 0.0095);
    values[i] = round(eq, 2);
    if (values[i] > peak) peak = values[i];
    drawdown[i] = round((values[i] / peak - 1) * 100, 2);
  }

  const lastVal = values[len - 1];
  const totalReturnPct = round(lastVal - 100, 2);
  let mdd = 0;
  for (let i = 0; i < len; i++) if (drawdown[i] < mdd) mdd = drawdown[i];

  const calDays = Math.round((endDate.getTime() - startDate.getTime()) / 86400000);
  const years = round(calDays / 365.25, 2);
  const cagr = years > 0 ? round((Math.pow(lastVal / 100, 1 / years) - 1) * 100, 2) : 0;

  // 샤프: 일간 수익률의 평균/표준편차 × sqrt(252)
  let mean = 0;
  const rets = new Array(Math.max(0, len - 1));
  for (let i = 1; i < len; i++) {
    rets[i - 1] = values[i] / values[i - 1] - 1;
    mean += rets[i - 1];
  }
  mean = rets.length ? mean / rets.length : 0;
  let variance = 0;
  for (let i = 0; i < rets.length; i++) variance += (rets[i] - mean) * (rets[i] - mean);
  variance = rets.length > 1 ? variance / (rets.length - 1) : 0;
  const sd = Math.sqrt(variance);
  const sharpe = sd > 0 ? round((mean / sd) * Math.sqrt(252), 2) : 0;

  // ── 월별 수익률: 자산곡선에서 직접 뽑는다(월은 유일하고 구간 내에 있다)
  const monthly = [];
  let prevMonthEnd = 100;
  let curMonth = dates[0].slice(0, 7);
  let curLast = values[0];
  for (let i = 0; i < len; i++) {
    const m = dates[i].slice(0, 7);
    if (m !== curMonth) {
      monthly.push({ month: curMonth, return_pct: round((curLast / prevMonthEnd - 1) * 100, 2) });
      prevMonthEnd = curLast;
      curMonth = m;
    }
    curLast = values[i];
  }
  monthly.push({ month: curMonth, return_pct: round((curLast / prevMonthEnd - 1) * 100, 2) });

  // ── 거래 내역 27건. 승 17 / 패 10 이 되도록 미리 패턴을 깔아둔다.
  const TRADE_N = 27;
  const WIN_N = 17;
  const winFlags = new Array(TRADE_N).fill(false);
  for (let i = 0; i < TRADE_N; i++) winFlags[i] = (i * WIN_N) % TRADE_N < WIN_N;
  // 정확히 17승이 되도록 보정
  let wcount = 0;
  for (let i = 0; i < TRADE_N; i++) if (winFlags[i]) wcount++;
  for (let i = 0; wcount > WIN_N && i < TRADE_N; i++) {
    if (winFlags[i]) { winFlags[i] = false; wcount--; }
  }
  for (let i = 0; wcount < WIN_N && i < TRADE_N; i++) {
    if (!winFlags[i]) { winFlags[i] = true; wcount++; }
  }

  const plannedPosition = initialCapital / maxPositions;
  const trades = [];
  const step = Math.max(1, Math.floor((len - 80) / TRADE_N));
  let cursor = 12;

  for (let k = 0; k < TRADE_N; k++) {
    const stock = STOCK_POOL[k % STOCK_POOL.length];
    const refIdx = Math.min(len - 60, cursor + 1 + Math.floor(rnd() * 3));
    const b1Idx = Math.min(len - 30, refIdx + 5 + Math.floor(rnd() * 10));
    const hasB2 = rnd() < 0.45;
    const b2Idx = Math.min(len - 20, b1Idx + 4 + Math.floor(rnd() * 10));
    const lastBuy = hasB2 ? b2Idx : b1Idx;
    const exitIdx = Math.min(len - 1, lastBuy + 6 + Math.floor(rnd() * 22));
    cursor = Math.min(len - 80, cursor + step);

    const refOpen = 12000 + Math.floor(rnd() * 58000);
    const refAmountEok = 1000 + Math.floor(rnd() * 1500);

    const p1 = refOpen;
    const q1 = Math.max(1, Math.floor((plannedPosition * 0.5) / p1));
    const fills = [{ rule: 'B1', date: dates[b1Idx], price: p1, qty: q1 }];
    let qty = q1;
    let cost = p1 * q1;
    if (hasB2) {
      const p2 = Math.round(p1 * 0.9);
      const q2 = Math.max(1, Math.floor((plannedPosition * 0.5) / p2));
      fills.push({ rule: 'B2', date: dates[b2Idx], price: p2, qty: q2 });
      qty += q2;
      cost += p2 * q2;
    }
    const avgPrice = Math.round(cost / qty); // 수량 가중 평균

    let exitRule;
    let exitReason;
    let exitPrice;
    if (winFlags[k]) {
      if (k % 7 === 3) {
        exitRule = 'TIME';
        exitReason = '보유기간 만료 · 익절 청산';
        exitPrice = Math.round(avgPrice * (1 + 0.012 + rnd() * 0.055));
      } else {
        exitRule = 'TP';
        exitReason = '평단 +10% 익절';
        exitPrice = Math.round(avgPrice * 1.1);
      }
    } else {
      if (k % 9 === 5) {
        exitRule = 'TIME';
        exitReason = '보유기간 만료 · 손실 청산';
        exitPrice = Math.round(avgPrice * (1 - 0.012 - rnd() * 0.03));
      } else {
        exitRule = 'SL';
        exitReason = '45일선 도달 손절';
        exitPrice = Math.round(avgPrice * (1 - 0.02 - rnd() * 0.07));
      }
    }
    // 반올림으로 손익 부호가 뒤집히지 않도록 최소 1원은 벌리고 간다.
    if (winFlags[k] && exitPrice <= avgPrice) exitPrice = avgPrice + 1;
    if (!winFlags[k] && exitPrice >= avgPrice) exitPrice = avgPrice - 1;

    const holdDays = Math.round(
      (parseYmd(dates[exitIdx]).getTime() - parseYmd(dates[b1Idx]).getTime()) / 86400000
    );

    trades.push({
      no: k + 1,
      code: stock[0],
      name: stock[1],
      ref_date: dates[refIdx],
      ref_amount_eok: refAmountEok,
      ref_open: refOpen,
      fills: fills,
      avg_price: avgPrice,
      exit_date: dates[exitIdx],
      exit_price: exitPrice,
      exit_rule: exitRule,
      exit_reason: exitReason,
      hold_days: holdDays,
      return_pct: round((exitPrice / avgPrice - 1) * 100, 1),
      pnl: Math.round((exitPrice - avgPrice) * qty),
    });
  }

  // ── trades 배열에서 되계산하는 지표들
  let wins = 0;
  let losses = 0;
  let sumWinPnl = 0;
  let sumLossPnl = 0;
  let sumWinPct = 0;
  let sumLossPct = 0;
  let sumHold = 0;
  for (let i = 0; i < trades.length; i++) {
    const tr = trades[i];
    sumHold += tr.hold_days;
    if (tr.pnl > 0) {
      wins++;
      sumWinPnl += tr.pnl;
      sumWinPct += tr.return_pct;
    } else {
      losses++;
      sumLossPnl += tr.pnl;
      sumLossPct += tr.return_pct;
    }
  }

  // ── by_stock: 반드시 trades를 집계해서 만든다
  const byStockMap = {};
  const byStockOrder = [];
  for (let i = 0; i < trades.length; i++) {
    const tr = trades[i];
    if (!byStockMap[tr.code]) {
      byStockMap[tr.code] = { code: tr.code, name: tr.name, trades: 0, wins: 0, pnl: 0 };
      byStockOrder.push(tr.code);
    }
    const agg = byStockMap[tr.code];
    agg.trades += 1;
    if (tr.pnl > 0) agg.wins += 1;
    agg.pnl += tr.pnl;
  }
  const byStock = byStockOrder.map((codeKey) => {
    const agg = byStockMap[codeKey];
    return {
      code: agg.code,
      name: agg.name,
      trades: agg.trades,
      win_rate: round((agg.wins / agg.trades) * 100, 1),
      pnl: agg.pnl,
    };
  });
  byStock.sort((a, b) => b.pnl - a.pnl);

  // ── 신호 로그: 앞쪽 거래 몇 건에서 뽑아 쓴다
  const signals = [];
  const levelsSeq = ['MATCH', 'INFO', 'MATCH', 'WARN', 'INFO', 'MATCH', 'INFO', 'MATCH'];
  for (let i = 0; i < Math.min(8, trades.length); i++) {
    const tr = trades[i];
    const lv = levelsSeq[i];
    let msg;
    if (lv === 'MATCH') {
      msg =
        '기준일 포착 — 거래대금 ' +
        fmtWon(tr.ref_amount_eok) +
        '억, 기준일 시가 ' +
        fmtWon(tr.ref_open) +
        '원';
    } else if (lv === 'WARN') {
      msg = '기준일 이후 60영업일 경과 임박 — 후보 폐기 예정';
    } else {
      msg = 'B1 체결 @ ' + fmtWon(tr.fills[0].price) + '원 · ' + tr.fills[0].qty + '주';
    }
    signals.push({
      ts: tr.ref_date + ' 09:0' + (i % 6) + ':0' + (i % 10),
      level: lv,
      code: tr.code,
      message: msg,
    });
  }

  return {
    run_id: 'r_20260725_081204',
    elapsed_sec: 2.31,
    warnings: [WARN_1M_APPROX],
    metrics: {
      total_return_pct: totalReturnPct,
      cagr_pct: cagr,
      mdd_pct: mdd,
      sharpe: sharpe,
      win_rate_pct: round((wins / trades.length) * 100, 1),
      profit_factor: sumLossPnl !== 0 ? round(sumWinPnl / Math.abs(sumLossPnl), 2) : 0,
      trades: trades.length,
      wins: wins,
      losses: losses,
      avg_win_pct: wins ? round(sumWinPct / wins, 1) : 0,
      avg_loss_pct: losses ? round(sumLossPct / losses, 1) : 0,
      avg_hold_days: trades.length ? round(sumHold / trades.length, 1) : 0,
      initial_capital: initialCapital,
      final_capital: Math.round(initialCapital * (1 + totalReturnPct / 100)),
      period: { start: startStr, end: endStr, years: years },
    },
    equity: { dates: dates, values: values, drawdown: drawdown },
    monthly: monthly,
    trades: trades,
    by_stock: byStock,
    signals: signals,
  };
}

/* ────────────────────────────── 4-7. POST /api/ai/strategy ────────────────────────────── */

/**
 * 폴백 경로에는 정의상 API 키가 없다. 그래서 항상 실패 형태를 돌려주고,
 * 대신 어떻게 켜는지를 how_to에 한국어로 적어 준다.
 */
export function mockAiStrategy(prompt) {
  return {
    ok: false,
    error: 'ANTHROPIC_API_KEY 가 설정되지 않았습니다.',
    how_to:
      'AI 전략 생성을 쓰려면 서버를 띄우기 전에 환경변수 ANTHROPIC_API_KEY 를 설정해야 합니다.\n' +
      '1) Windows(명령 프롬프트): setx ANTHROPIC_API_KEY "sk-ant-..." 를 실행한 뒤 창을 새로 엽니다.\n' +
      '2) Windows(PowerShell): $env:ANTHROPIC_API_KEY = "sk-ant-..." (현재 세션에만 적용)\n' +
      '3) macOS / Linux: export ANTHROPIC_API_KEY="sk-ant-..." 를 셸이나 ~/.bashrc 에 넣습니다.\n' +
      '4) 설정 후 서버를 재시작하면(run_server.bat 또는 uvicorn 재실행) 이 기능이 활성화됩니다.\n' +
      '키가 없어도 서버는 정상 기동하며 이 엔드포인트만 실패합니다. ' +
      '입력한 프롬프트는 그대로 보존되어 있으니 키 설정 후 다시 시도하세요.' +
      (prompt ? '\n\n입력한 프롬프트: ' + String(prompt) : ''),
  };
}
