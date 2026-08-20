/* ============================================================
   chart.js — 고성능 캔들 차트
   ------------------------------------------------------------
   ARCHITECTURE.md 5-2 요구사항 구현:
    1) 캔버스 2장 (base / overlay). mousemove 는 overlay 만 다시 그린다.
    2) 모든 시계열은 TypedArray. 객체 배열을 만들지 않는다.
    3) 가시 구간 [i0, i1) 만 순회한다.
    4) 가시 봉 수 > 픽셀 폭이면 픽셀당 min/max/first/last 로 집계한다.
    5) 상승/하락을 각각 한 번의 beginPath 로 배칭한다.
    6) mousemove/wheel/resize 는 rAF 한 프레임에 1회로 합친다.
    7) devicePixelRatio 상한 2.
   ============================================================ */

import { colors as themeColors } from './theme.js';

const PL = 8;    // 좌 패딩
const PR = 64;   // 우측 가격축
const PT = 10;   // 상단 패딩
const PB = 22;   // 하단 시간축
const PANE_GAP = 12;
const MIN_BARS = 15;          // 최대 확대 시 남는 봉 수
const OSC_PANE_H = 56;        // 오실레이터 패널 1개 높이
const MAX_OSC_PANES = 2;
//: 이동평균선에서 이어 그릴 최대 결측 봉 수.
//: 거래정지는 길어야 며칠이라 이 정도면 선이 끊기지 않고,
//: 그보다 긴 공백(워밍업 부족 등)은 억지로 잇지 않는다.
const MA_BRIDGE_MAX_GAP = 10;

const MK_REF = 0, MK_BUY = 1, MK_SELL = 2, MK_OTHER = 3;
const MK_TYPE = { ref: MK_REF, buy: MK_BUY, sell: MK_SELL };

/* ---------------- 포맷 유틸 ---------------- */
const nf = new Intl.NumberFormat('ko-KR');
const fmtNum = (n) => (Number.isFinite(n) ? nf.format(Math.round(n)) : '—');

/** YYYYMMDD 정수 → 'YYYY-MM-DD' */
export function ymd(t) {
  const y = (t / 10000) | 0, m = ((t / 100) | 0) % 100, d = t % 100;
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}
/** YYYYMMDD 정수 → 축 라벨 */
function axisLabel(t, spanDays) {
  const y = (t / 10000) | 0, m = ((t / 100) | 0) % 100, d = t % 100;
  if (spanDays > 900) return `${String(y).slice(2)}/${String(m).padStart(2, '0')}`;
  return `${String(m).padStart(2, '0')}/${String(d).padStart(2, '0')}`;
}
/** 원 → '1,432억' */
function eok(won) {
  if (!Number.isFinite(won)) return '—';
  const e = won / 1e8;
  if (e >= 10000) return (e / 10000).toFixed(2) + '조';
  if (e >= 10) return fmtNum(e) + '억';
  return e.toFixed(1) + '억';
}

/** 가격축 눈금 간격을 사람이 읽기 좋은 값으로 */
function niceStep(range, target) {
  if (!(range > 0)) return 1;
  const raw = range / Math.max(1, target);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return step * mag;
}

/** null 이 섞인 배열 → Float64Array (null/undefined → NaN) */
function toF64(arr, n) {
  const out = new Float64Array(n);
  if (!arr) { out.fill(NaN); return out; }
  for (let i = 0; i < n; i++) {
    const v = arr[i];
    out[i] = (v === null || v === undefined) ? NaN : +v;
  }
  return out;
}

/* ============================================================
   CandleChart
   ============================================================ */
export class CandleChart {
  /**
   * @param {HTMLElement} host  position:relative 인 컨테이너
   * @param {object} opts {onHover, onViewport}
   */
  constructor(host, opts = {}) {
    this.host = host;
    this.onHover = opts.onHover || null;
    this.onViewport = opts.onViewport || null;

    this.base = document.createElement('canvas');
    this.base.className = 'base';
    this.base.setAttribute('role', 'img');
    this.base.setAttribute('aria-label', '캔들 차트');
    this.overlay = document.createElement('canvas');
    this.overlay.className = 'overlay';
    this.overlay.setAttribute('aria-hidden', 'true');
    host.prepend(this.overlay);
    host.prepend(this.base);

    this.bctx = this.base.getContext('2d', { alpha: false });
    this.octx = this.overlay.getContext('2d');

    // 데이터 끝에 닿았을 때 알려 주는 가장자리 표시 (아무 반응이 없으면 "고장났다"고 느낀다)
    this.edgeL = document.createElement('div');
    this.edgeL.className = 'edge-glow l';
    this.edgeL.innerHTML = '<span>가장 오래된 데이터입니다</span>';
    this.edgeR = document.createElement('div');
    this.edgeR.className = 'edge-glow r';
    this.edgeR.innerHTML = '<span>가장 최근 데이터입니다</span>';
    host.appendChild(this.edgeL);
    host.appendChild(this.edgeR);
    this._edgeTimer = 0;

    this.d = null;                 // 데이터
    this.i0 = 0; this.i1 = 0;      // 가시 구간 [i0, i1)
    this.hover = -1;               // 호버 중인 봉 인덱스
    this.hoverY = -1;
    this.highlight = null;         // {from, to} 강조 구간
    this._colors = null;           // 테마 색 캐시 (테마 변경 시 null)
    this._geo = null;              // 마지막 레이아웃
    this._w = 0; this._h = 0; this._dpr = 0;

    this._needBase = false;
    this._needOverlay = false;
    this._raf = 0;

    // 다운샘플 버퍼 — 재할당하지 않고 재사용한다
    this._cap = 0;
    this._cO = this._cH = this._cL = this._cC = this._cAmt = this._cX = null;
    this._cIdx = null; this._cFlag = null;
    this._cols = 0;

    /** 성능 계측 (playwright 벤치마크가 읽는다) */
    this.stats = { baseMs: 0, overlayMs: 0, baseCount: 0, overlayCount: 0, lastCols: 0, lastVisible: 0 };

    this._bind();
  }

  /* ---------------- 데이터 ---------------- */

  /**
   * /api/chart 응답을 TypedArray 로 변환해 보관한다.
   * @param {object} p ARCHITECTURE 4-6 페이로드
   * @param {Array<{key,slot,overlay,label}>} indicatorMeta 그릴 지표 메타 (색 슬롯 포함)
   */
  setData(p, indicatorMeta = []) {
    if (!p || !p.n) { this.d = null; this.i0 = this.i1 = 0; this.requestBase(); return; }
    this.d = this._build(p, indicatorMeta);
    this.hover = -1;
    this.highlight = null;
    this.fitAll(false);
  }

  /**
   * 더 오래된 구간을 앞에 이어 붙인다 (점진 로딩).
   * 화면이 튀지 않도록 뷰포트·강조·호버 인덱스를 붙인 개수만큼 밀어 준다.
   * @returns {number} 실제로 앞에 붙은 봉 수 (0 이면 새로 붙은 게 없다)
   */
  prependData(p, indicatorMeta = []) {
    if (!p || !p.n) return 0;
    if (!this.d || !this.d.n) { this.setData(p, indicatorMeta); return this.d ? this.d.n : 0; }

    const old = this._build(p, indicatorMeta);
    const cur = this.d;

    // 겹치는 구간은 버린다. 기존 첫 봉보다 과거인 것만 남긴다.
    const firstT = cur.t[0];
    let cut = old.n;
    while (cut > 0 && old.t[cut - 1] >= firstT) cut--;
    if (cut <= 0) return 0;

    const n = cut + cur.n;
    const catI32 = (a, b) => { const o = new Int32Array(n); o.set(a.subarray(0, cut)); o.set(b, cut); return o; };
    const catF64 = (a, b) => { const o = new Float64Array(n); o.set(a.subarray(0, cut)); o.set(b, cut); return o; };
    const catU8 = (a, b) => { const o = new Uint8Array(n); o.set(a.subarray(0, cut)); o.set(b, cut); return o; };

    // 지표는 양쪽 키의 합집합. 한쪽에만 있으면 없는 구간은 NaN 으로 채운다.
    const byKey = new Map();
    for (const x of cur.ind) byKey.set(x.key, { meta: x, cur: x.arr, old: null });
    for (const x of old.ind) {
      const e = byKey.get(x.key);
      if (e) e.old = x.arr;
      else byKey.set(x.key, { meta: x, cur: null, old: x.arr });
    }
    const ind = [];
    for (const [key, e] of byKey) {
      const arr = new Float64Array(n);
      if (e.old) arr.set(e.old.subarray(0, cut)); else arr.fill(NaN, 0, cut);
      if (e.cur) arr.set(e.cur, cut); else arr.fill(NaN, cut, n);
      ind.push({ ...e.meta, key, arr });
    }
    ind.sort((a, b) => a.slot - b.slot);

    // 마커: 과거 청크의 것(인덱스 < cut) + 기존 것(cut 만큼 이동)
    const keepOld = [];
    for (let k = 0; k < old.markers.n; k++) if (old.markers.i[k] < cut) keepOld.push(k);
    const mn = keepOld.length + cur.markers.n;
    const markers = {
      n: mn,
      i: new Int32Array(mn), type: new Uint8Array(mn), price: new Float64Array(mn),
      label: new Array(mn), note: new Array(mn),
    };
    keepOld.forEach((k, j) => {
      markers.i[j] = old.markers.i[k]; markers.type[j] = old.markers.type[k];
      markers.price[j] = old.markers.price[k];
      markers.label[j] = old.markers.label[k]; markers.note[j] = old.markers.note[k];
    });
    for (let k = 0; k < cur.markers.n; k++) {
      const j = keepOld.length + k;
      markers.i[j] = cur.markers.i[k] + cut; markers.type[j] = cur.markers.type[k];
      markers.price[j] = cur.markers.price[k];
      markers.label[j] = cur.markers.label[k]; markers.note[j] = cur.markers.note[k];
    }

    const shiftBand = (b) => ({ ...b, from: (b.from | 0) + cut, to: (b.to | 0) + cut });
    const shiftLevel = (l) => ({ ...l, from: (l.from | 0) + cut });

    this.d = {
      code: cur.code, name: cur.name, n,
      t: catI32(old.t, cur.t),
      o: catF64(old.o, cur.o), h: catF64(old.h, cur.h),
      l: catF64(old.l, cur.l), c: catF64(old.c, cur.c),
      v: catF64(old.v, cur.v), amt: catF64(old.amt, cur.amt),
      flag: catU8(old.flag, cur.flag),
      ind, markers,
      bands: old.bands.filter((b) => (b.to | 0) < cut).concat(cur.bands.map(shiftBand)),
      levels: old.levels.filter((l) => (l.from | 0) < cut).concat(cur.levels.map(shiftLevel)),
    };

    // 보고 있던 구간이 그대로 유지되도록 인덱스를 민다 (화면이 튀면 안 된다)
    this.i0 += cut; this.i1 += cut;
    if (this.highlight) { this.highlight.from += cut; this.highlight.to += cut; }
    if (this.hover >= 0) this.hover += cut;
    this.requestBase();
    return cut;
  }

  /** 로드된 가장 오래된 / 최신 봉 날짜 (YYYY-MM-DD) */
  get oldestDate() { return this.d && this.d.n ? ymd(this.d.t[0]) : null; }
  get newestDate() { return this.d && this.d.n ? ymd(this.d.t[this.d.n - 1]) : null; }

  /** 응답 페이로드 → 내부 데이터 객체 (setData / prependData 공용) */
  _build(p, indicatorMeta = []) {
    const n = p.n | 0;

    const t = new Int32Array(n);
    for (let i = 0; i < n; i++) t[i] = p.t[i] | 0;

    const o = toF64(p.o, n), h = toF64(p.h, n), l = toF64(p.l, n), c = toF64(p.c, n);
    const v = toF64(p.v, n), amt = toF64(p.amt, n);

    // 봉 플래그. 1=상승, 2=거래대금 급증(1,000억 이상), 4=거래정지
    // 거래정지일은 서버가 o/h/l/c 를 null 로 내려주므로 여기서 NaN 이 된다.
    // 그런 봉은 색도 크기도 정할 수 없으니 아예 그리지 않고 빈 칸으로 남긴다.
    const flag = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      const O = o[i], H = h[i], L = l[i], Cc = c[i];
      if (!(O === O && H === H && L === L && Cc === Cc)) { flag[i] |= 4; continue; }
      if (Cc >= O) flag[i] |= 1;
      if (amt[i] >= 1e11) flag[i] |= 2;
    }

    // 지표: 응답에 있는 키만, indicatorMeta 순서대로
    const ind = [];
    const src = p.indicators || {};
    for (const meta of indicatorMeta) {
      const raw = src[meta.key];
      if (!raw) continue;
      ind.push({
        key: meta.key, label: meta.label || meta.key, slot: meta.slot | 0,
        overlay: meta.overlay !== false, fromStrategy: meta.fromStrategy === true,
        arr: toF64(raw, n),
      });
    }
    // 메타에 없지만 응답에 있는 지표도 버리지 않는다
    let slot = ind.length;
    for (const key of Object.keys(src)) {
      if (ind.some((x) => x.key === key)) continue;
      ind.push({ key, label: key, slot: slot++, overlay: true, arr: toF64(src[key], n) });
    }

    // 마커 — 개수가 적어 TypedArray + 병렬 문자열 배열로 둔다
    const ms = Array.isArray(p.markers) ? p.markers.filter((m) => m && m.i >= 0 && m.i < n) : [];
    const markers = {
      n: ms.length,
      i: new Int32Array(ms.length),
      type: new Uint8Array(ms.length),
      price: new Float64Array(ms.length),
      label: new Array(ms.length),
      note: new Array(ms.length),
    };
    ms.forEach((m, k) => {
      markers.i[k] = m.i | 0;
      markers.type[k] = MK_TYPE[m.type] ?? MK_OTHER;
      markers.price[k] = Number.isFinite(+m.price) ? +m.price : NaN;
      markers.label[k] = m.label || '';
      markers.note[k] = m.note || '';
    });

    return {
      code: p.code || '', name: p.name || '', n,
      t, o, h, l, c, v, amt, flag, ind, markers,
      bands: Array.isArray(p.bands) ? p.bands : [],
      levels: Array.isArray(p.levels) ? p.levels : [],
    };
  }

  hasData() { return !!(this.d && this.d.n); }

  /** 로드된 전체 봉 수 (뷰포트가 아니라 데이터 전체) */
  get n() { return this.d ? this.d.n : 0; }

  /** 화면에 보이는 봉 수 */
  get visibleCount() { return this.i1 - this.i0; }

  /** 가장 최근 `count` 개만 보이도록 뷰포트를 맞춘다 (데이터 재요청 없음) */
  showLast(count, notify = true) {
    if (!this.d) return;
    const n = this.d.n;
    const c = Math.max(MIN_BARS, Math.min(n, Math.round(count) || n));
    this.setViewport(n - c, n, notify);
  }

  /**
   * 차트에 처음 들어왔을 때 딱 한 번만 조작 방법을 알려 준다.
   * localStorage 에 기록해 두 번째부터는 띄우지 않는다.
   */
  _maybeShowHint() {
    if (this._hintShown || !this.hasData()) return;
    try { if (localStorage.getItem('chartHintSeen') === '1') { this._hintShown = true; return; } } catch { /* 접근 불가 */ }
    this._hintShown = true;
    if (!this._hintEl) {
      this._hintEl = document.createElement('div');
      this._hintEl.className = 'chart-firsthint';
      this._hintEl.innerHTML =
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M5 12h14M9 8l-4 4 4 4M15 8l4 4-4 4"/></svg>' +
        '<span><b>드래그</b>로 좌우 이동 · <b>휠</b>로 확대 · <b>더블클릭</b>으로 전체 보기</span>';
      this.host.appendChild(this._hintEl);
    }
    this._hintEl.classList.add('on');
    clearTimeout(this._hintTimer);
    this._hintTimer = setTimeout(() => this._hideHint(), 5000);
    try { localStorage.setItem('chartHintSeen', '1'); } catch { /* 접근 불가 */ }
  }

  _hideHint() {
    if (this._hintEl) this._hintEl.classList.remove('on');
    clearTimeout(this._hintTimer);
  }

  /** 데이터 가장자리에 닿았음을 잠깐 표시한다 */
  _flashEdge(side) {
    const el = side === 'left' ? this.edgeL : this.edgeR;
    const other = side === 'left' ? this.edgeR : this.edgeL;
    other.classList.remove('on');
    // 이미 켜져 있으면 애니메이션을 다시 시작시킨다
    el.classList.remove('on');
    void el.offsetWidth;
    el.classList.add('on');
    clearTimeout(this._edgeTimer);
    this._edgeTimer = setTimeout(() => el.classList.remove('on'), 900);
  }

  /* ---------------- 뷰포트 ---------------- */

  /** 전체 보기 */
  fitAll(notify = true) {
    if (!this.d) return;
    this.setViewport(0, this.d.n, notify);
  }

  setViewport(i0, i1, notify = true) {
    if (!this.d) return;
    const n = this.d.n;
    let a = Math.round(i0), b = Math.round(i1);
    if (b - a < MIN_BARS) {
      const mid = (a + b) / 2;
      a = Math.round(mid - MIN_BARS / 2);
      b = a + MIN_BARS;
    }
    if (b - a > n) { a = 0; b = n; }
    if (a < 0) { b -= a; a = 0; }
    if (b > n) { a -= (b - n); b = n; }
    if (a < 0) a = 0;
    if (a === this.i0 && b === this.i1) return false;   // 클램프되어 움직이지 못했다
    this.i0 = a; this.i1 = b;
    this.requestBase();
    if (notify && this.onViewport) this.onViewport(a, b);
    return true;
  }

  /** 왼쪽/오른쪽 데이터 끝에 붙어 있는가 */
  atStart() { return this.i0 <= 0; }
  atEnd() { return this.d ? this.i1 >= this.d.n : false; }

  /** 특정 구간이 화면에 여유 있게 들어오도록 이동 */
  focusRange(from, to, pad = 0.35) {
    if (!this.d) return;
    const a = Math.max(0, Math.min(from, to));
    const b = Math.min(this.d.n, Math.max(from, to) + 1);
    const span = Math.max(MIN_BARS, b - a);
    const p = Math.round(span * pad);
    this.setViewport(a - p, b + p);
  }

  /** 거래 강조 (거래내역 행 클릭) */
  setHighlight(hl) {
    this.highlight = hl && Number.isFinite(hl.from) ? { from: hl.from, to: hl.to ?? hl.from } : null;
    this.requestOverlay();
  }

  /* ---------------- 렌더 스케줄 (rAF 코얼레싱) ---------------- */

  requestBase() { this._needBase = true; this._needOverlay = true; this._kick(); }
  requestOverlay() { this._needOverlay = true; this._kick(); }

  _kick() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      if (this._needBase) { this._needBase = false; this._drawBase(); }
      if (this._needOverlay) { this._needOverlay = false; this._drawOverlay(); }
    });
  }

  /** 테마가 바뀌면 색 캐시를 버리고 base 를 다시 그린다 */
  invalidateTheme() { this._colors = null; this.requestBase(); }

  get colors() {
    if (!this._colors) this._colors = themeColors();
    return this._colors;
  }

  /* ---------------- 캔버스 크기 ---------------- */

  _resize() {
    const r = this.host.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);   // 요구사항 7: 상한 2
    const w = Math.max(1, Math.round(r.width));
    const h = Math.max(1, Math.round(r.height));
    if (w === this._w && h === this._h && dpr === this._dpr) return false;
    this._w = w; this._h = h; this._dpr = dpr;
    for (const cv of [this.base, this.overlay]) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    this.bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.octx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return true;
  }

  /* ---------------- 레이아웃 ---------------- */

  _layout() {
    const w = this._w, h = this._h;
    const plotW = Math.max(10, w - PL - PR);

    const oscs = this.d ? this.d.ind.filter((x) => !x.overlay).slice(0, MAX_OSC_PANES) : [];
    const oscH = oscs.length * (OSC_PANE_H + PANE_GAP);
    let volH = Math.max(44, Math.min(84, Math.round(h * 0.17)));
    let priceH = h - PT - PB - volH - PANE_GAP - oscH;
    if (priceH < 90) {                    // 창이 낮으면 보조 패널을 줄인다
      volH = Math.max(28, volH - (90 - priceH));
      priceH = h - PT - PB - volH - PANE_GAP - oscH;
    }
    const priceY = PT;
    const volY = priceY + priceH + PANE_GAP;
    const oscY = volY + volH + PANE_GAP;

    return { w, h, plotW, priceY, priceH, volY, volH, oscs, oscY, oscPaneH: OSC_PANE_H };
  }

  /* ---------------- 컬럼 집계 (요구사항 3·4) ---------------- */

  _buildColumns(g) {
    const d = this.d, i0 = this.i0, i1 = this.i1;
    const nVis = i1 - i0;
    // 픽셀 폭을 넘으면 픽셀당 1컬럼으로 집계, 아니면 봉당 1컬럼
    const cols = Math.max(1, Math.min(nVis, Math.floor(g.plotW)));

    if (cols > this._cap) {
      const cap = Math.max(cols, 1024);
      this._cO = new Float64Array(cap); this._cH = new Float64Array(cap);
      this._cL = new Float64Array(cap); this._cC = new Float64Array(cap);
      this._cAmt = new Float64Array(cap); this._cX = new Float64Array(cap);
      this._cIdx = new Int32Array(cap); this._cFlag = new Uint8Array(cap);
      this._cap = cap;
    }
    const cO = this._cO, cH = this._cH, cL = this._cL, cC = this._cC;
    const cAmt = this._cAmt, cIdx = this._cIdx, cFlag = this._cFlag, cX = this._cX;

    const o = d.o, hi = d.h, lo = d.l, c = d.c, amt = d.amt, flag = d.flag;
    const cw = g.plotW / cols;

    // 초기화 (가시 컬럼만)
    for (let b = 0; b < cols; b++) {
      cH[b] = -Infinity; cL[b] = Infinity; cAmt[b] = 0; cFlag[b] = 0; cIdx[b] = -1;
      cX[b] = PL + (b + 0.5) * cw;
    }

    let pMin = Infinity, pMax = -Infinity, aMax = 0;
    const scale = cols / nVis;
    for (let i = i0; i < i1; i++) {
      let b = ((i - i0) * scale) | 0;
      if (b >= cols) b = cols - 1;
      // 거래정지 봉은 집계에 넣지 않는다. 넣으면 NaN 이 섞여 컬럼 하나가 통째로 깨진다.
      if (flag[i] & 4) continue;
      // 컬럼 플래그 8 = "이 컬럼에 그릴 봉이 하나라도 있다"
      if ((cFlag[b] & 8) === 0) { cO[b] = o[i]; cFlag[b] |= 8; }   // first (유효 봉 기준)
      cC[b] = c[i];                                       // last
      const H = hi[i], L = lo[i];
      if (H > cH[b]) cH[b] = H;
      if (L < cL[b]) cL[b] = L;
      if (H > pMax) pMax = H;
      if (L < pMin) pMin = L;
      const A = amt[i];
      if (A === A) {                                      // NaN 거래대금은 건너뛴다
        if (A > cAmt[b]) cAmt[b] = A;
        if (A > aMax) aMax = A;
      }
      cFlag[b] |= (flag[i] & 2);                          // 급증 표시는 OR
      cIdx[b] = i;                                        // 대표 인덱스 (마지막 봉)
    }
    // 상승/하락은 집계된 open/close 로 다시 판정 (그릴 봉이 있는 컬럼만)
    for (let b = 0; b < cols; b++) {
      if ((cFlag[b] & 8) === 0) continue;
      if (cC[b] >= cO[b]) cFlag[b] |= 1;
    }

    this._cols = cols;
    return { cols, cw, pMin, pMax, aMax };
  }

  /* ---------------- base 렌더 ---------------- */

  _drawBase() {
    this._resize();
    const t0 = performance.now();
    const ctx = this.bctx, C = this.colors;
    const w = this._w, h = this._h;

    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, w, h);

    if (!this.hasData()) { this._geo = null; this.stats.baseMs = performance.now() - t0; return; }

    const g = this._layout();
    const agg = this._buildColumns(g);
    const { cols, cw } = agg;

    // ---- 가격 범위: 가시 봉 + 가시 지표 + 레벨 ----
    let pMin = agg.pMin, pMax = agg.pMax;
    for (const ind of this.d.ind) {
      if (!ind.overlay) continue;
      const a = ind.arr;
      for (let i = this.i0; i < this.i1; i++) {
        const v = a[i];
        if (v !== v) continue;               // NaN
        if (v < pMin) pMin = v;
        if (v > pMax) pMax = v;
      }
    }
    // 레벨(기준일 시가선)은 **화면에 들어온 것만** 범위에 넣는다.
    // 과거를 이어붙이면 레벨이 수십 개가 되는데, 전부 넣으면 세로 축이 터져
    // 정작 보고 있는 캔들이 아래쪽에 납작하게 눌린다.
    if (pMax > pMin) {
      const span = pMax - pMin;
      const loLimit = pMin - span * 0.5, hiLimit = pMax + span * 0.5;
      for (const lv of this.d.levels) {
        const p = +lv.price;
        if (!Number.isFinite(p)) continue;
        const from = lv.from | 0;
        if (from >= this.i1) continue;             // 아직 시작되지 않은 레벨
        if (p < loLimit || p > hiLimit) continue;  // 화면 가격대와 너무 동떨어진 레벨
        if (p < pMin) pMin = p;
        if (p > pMax) pMax = p;
      }
    }
    // 가시 구간이 전부 거래정지면 pMin/pMax 가 Infinity 로 남는다. 그대로 두면
    // 스케일이 NaN 이 되어 화면이 통째로 사라지므로 여기서 안전한 값으로 되돌린다.
    if (!Number.isFinite(pMin) || !Number.isFinite(pMax)) { pMin = 0; pMax = 1; }
    if (!(pMax > pMin)) { pMax = pMin + 1; }
    const padP = (pMax - pMin) * 0.06;
    pMin -= padP; pMax += padP;

    const priceSpan = pMax - pMin;
    const Y = (p) => g.priceY + (pMax - p) / priceSpan * g.priceH;
    const X = (i) => PL + (i - this.i0 + 0.5) * (g.plotW / (this.i1 - this.i0));

    const geo = { ...g, ...agg, pMin, pMax, Y, X, aMax: agg.aMax || 1 };
    this._geo = geo;

    ctx.font = '10px ' + MONO;
    ctx.textBaseline = 'alphabetic';

    this._drawGrid(ctx, C, geo);
    this._drawBands(ctx, C, geo);
    this._drawVolume(ctx, C, geo);
    this._drawCandles(ctx, C, geo);
    this._drawIndicators(ctx, C, geo);
    this._drawLevels(ctx, C, geo);
    this._drawOscillators(ctx, C, geo);
    this._drawMarkers(ctx, C, geo);
    this._drawAxes(ctx, C, geo);

    this.stats.baseMs = performance.now() - t0;
    this.stats.baseCount++;
    this.stats.lastCols = cols;
    this.stats.lastVisible = this.i1 - this.i0;
    void cw;
  }

  _drawGrid(ctx, C, g) {
    const step = niceStep(g.pMax - g.pMin, 6);
    const first = Math.ceil(g.pMin / step) * step;
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath();
    for (let p = first; p <= g.pMax; p += step) {
      const y = Math.round(g.Y(p)) + 0.5;
      ctx.moveTo(PL, y); ctx.lineTo(g.w - PR, y);
    }
    ctx.stroke();

    // 가격축 라벨
    ctx.fillStyle = C.axisTx; ctx.textAlign = 'left';
    for (let p = first; p <= g.pMax; p += step) {
      const y = Math.round(g.Y(p));
      if (y < g.priceY - 2 || y > g.priceY + g.priceH + 2) continue;
      ctx.fillText(fmtNum(p), g.w - PR + 6, y + 3.5);
    }

    // 시간축 세로선
    const nVis = this.i1 - this.i0;
    const targetTicks = Math.max(3, Math.min(9, Math.floor(g.plotW / 110)));
    const stride = Math.max(1, Math.ceil(nVis / targetTicks));
    ctx.strokeStyle = C.grid;
    ctx.beginPath();
    for (let i = this.i0 + stride - (this.i0 % stride || stride); i < this.i1; i += stride) {
      const x = Math.round(g.X(i)) + 0.5;
      ctx.moveTo(x, g.priceY); ctx.lineTo(x, g.volY + g.volH);
    }
    ctx.stroke();
  }

  _drawBands(ctx, C, g) {
    if (!this.d.bands.length) return;
    ctx.fillStyle = C.hold;
    for (const b of this.d.bands) {
      const from = Math.max(this.i0, b.from | 0), to = Math.min(this.i1 - 1, b.to | 0);
      if (to < from) continue;
      const x0 = g.X(from) - (g.plotW / (this.i1 - this.i0)) / 2;
      const x1 = g.X(to) + (g.plotW / (this.i1 - this.i0)) / 2;
      ctx.fillRect(x0, g.priceY, Math.max(1, x1 - x0), g.priceH);
    }
  }

  /** 요구사항 5: 상승/하락/급증 각각 한 번의 path 로 배칭 */
  _drawVolume(ctx, C, g) {
    const cols = g.cols, cw = g.plotW / cols;
    const bw = Math.max(1, cw * 0.7);
    const amt = this._cAmt, flag = this._cFlag, cx = this._cX;
    const base = g.volY + g.volH;
    const scale = g.volH / (g.aMax || 1);

    for (const pass of [0, 1, 2]) {           // 0=하락 1=상승 2=급증
      ctx.beginPath();
      let any = false;
      for (let b = 0; b < cols; b++) {
        const f = flag[b];
        if ((f & 8) === 0) continue;          // 거래정지만 있는 컬럼은 비워 둔다
        const isHi = (f & 2) !== 0;
        const isUp = (f & 1) !== 0;
        const which = isHi ? 2 : (isUp ? 1 : 0);
        if (which !== pass) continue;
        const bh = amt[b] * scale;
        if (!(bh > 0)) continue;
        // 정수 정렬 — 안티에일리어싱된 가장자리를 없애 채우기 비용을 낮춘다
        const y = Math.round(base - bh);
        ctx.rect(Math.round(cx[b] - bw / 2), y, Math.max(1, Math.round(bw)), Math.max(1, Math.round(base) - y));
        any = true;
      }
      if (!any) continue;
      ctx.fillStyle = pass === 2 ? C.volHi : pass === 1 ? C.volUp : C.volDn;
      ctx.fill();
    }

    // 1,000억 기준선 — 전략1의 거래대금 임계값
    const yRef = base - 1e11 * scale;
    if (yRef > g.volY - 1) {
      ctx.save();
      ctx.strokeStyle = C.volHi; ctx.globalAlpha = .65; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(PL, Math.round(yRef) + .5); ctx.lineTo(g.w - PR, Math.round(yRef) + .5); ctx.stroke();
      ctx.restore();
      ctx.fillStyle = C.volHi; ctx.textAlign = 'left';
      ctx.fillText('1,000억', g.w - PR + 6, Math.round(yRef) + 3.5);
    }
    ctx.fillStyle = C.axisTx; ctx.textAlign = 'left';
    ctx.fillText('거래대금 · 최대 ' + eok(g.aMax), PL + 4, g.volY + 11);
  }

  /** 요구사항 4·5: 다운샘플 + 배칭 */
  _drawCandles(ctx, C, g) {
    const cols = g.cols, cw = g.plotW / cols;
    const O = this._cO, H = this._cH, L = this._cL, Cl = this._cC, F = this._cFlag, cx = this._cX;
    const Y = g.Y;
    const thin = cw < 2.6;                  // 폭이 좁으면 몸통 없이 고저선만
    const bw = Math.max(1, Math.floor(cw * 0.72));

    if (thin) {
      /* 다운샘플 구간에서는 stroke 대신 정수 좌표 fillRect 를 쓴다.
         세로선을 path 로 stroke 하면 안티에일리어싱 때문에 소프트웨어
         래스터라이저에서 비용이 몇 배로 뛴다. 정수 정렬된 사각형은 AA 가 없다. */
      for (const up of [0, 1]) {
        ctx.beginPath();
        let any = false;
        for (let b = 0; b < cols; b++) {
          if ((F[b] & 8) === 0) continue;     // 거래정지만 있는 컬럼은 빈 칸
          if (((F[b] & 1) !== 0) !== !!up) continue;
          const x = Math.round(cx[b]);
          const y0 = Math.round(Y(H[b])), y1 = Math.round(Y(L[b]));
          ctx.rect(x, y0, 1, Math.max(1, y1 - y0));
          any = true;
        }
        if (!any) continue;
        ctx.fillStyle = up ? C.up : C.dn;
        ctx.fill();
      }
      return;
    }

    // --- 심지: 상승 1회, 하락 1회 ---
    ctx.lineWidth = 1;
    for (const up of [0, 1]) {
      ctx.beginPath();
      let any = false;
      for (let b = 0; b < cols; b++) {
        if ((F[b] & 8) === 0) continue;       // 거래정지만 있는 컬럼은 빈 칸
        if (((F[b] & 1) !== 0) !== !!up) continue;
        const x = Math.round(cx[b]) + 0.5;
        ctx.moveTo(x, Y(H[b])); ctx.lineTo(x, Y(L[b]));
        any = true;
      }
      if (!any) continue;
      ctx.strokeStyle = up ? C.upWick : C.dnWick;
      ctx.stroke();
    }

    // --- 몸통: 상승 1회, 하락 1회 ---
    for (const up of [0, 1]) {
      ctx.beginPath();
      let any = false;
      for (let b = 0; b < cols; b++) {
        if ((F[b] & 8) === 0) continue;       // 거래정지만 있는 컬럼은 빈 칸
        if (((F[b] & 1) !== 0) !== !!up) continue;
        const yo = Y(O[b]), yc = Y(Cl[b]);
        const top = Math.min(yo, yc);
        ctx.rect(Math.round(cx[b] - bw / 2), Math.round(top), bw, Math.max(1, Math.round(Math.abs(yc - yo))));
        any = true;
      }
      if (!any) continue;
      ctx.fillStyle = up ? C.up : C.dn;
      ctx.fill();
    }
  }

  _drawIndicators(ctx, C, g) {
    const Y = g.Y, X = g.X;
    const nVis = this.i1 - this.i0;
    // 가시 봉이 픽셀보다 많으면 선도 픽셀 단위로 솎아낸다
    const stride = Math.max(1, Math.floor(nVis / Math.max(1, g.plotW)));
    ctx.lineWidth = 1.4;
    // 점이 수백 개인 폴리라인에서 round join 은 눈에 띄는 이득 없이 래스터 비용만 올린다
    ctx.lineJoin = 'bevel';
    for (const ind of this.d.ind) {
      if (!ind.overlay) continue;
      const a = ind.arr;
      ctx.strokeStyle = C.maSlots[ind.slot % C.maSlots.length];
      ctx.beginPath();
      let started = false, drew = false, gap = 0;
      for (let i = this.i0; i < this.i1; i += stride) {
        const v = a[i];
        if (v !== v) { gap++; continue; }
        const x = X(i), y = Y(v);
        // 거래정지처럼 **짧은** 결측 구간은 건너뛰고 선을 이어 준다.
        // 그 며칠은 시세 자체가 없을 뿐 이동평균이 실제로 끊긴 게 아니다.
        // 반대로 긴 결측(지표 워밍업 부족 등)은 이어 버리면 없는 추세를
        // 지어내는 셈이라, 그대로 끊어 둔다.
        if (started && gap > MA_BRIDGE_MAX_GAP) started = false;
        if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
        gap = 0;
        drew = true;
      }
      if (drew) ctx.stroke();
    }
  }

  _drawLevels(ctx, C, g) {
    if (!this.d.levels.length) return;
    ctx.save();
    ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
    ctx.font = 'bold 10px ' + MONO;
    // 화면 왼쪽 밖으로 한 화면 이상 지난 기준선은 그리지 않는다.
    // (과거를 이어붙일수록 옛날 기준선이 쌓여 화면이 어지러워진다)
    const staleBefore = this.i0 - (this.i1 - this.i0);
    for (const lv of this.d.levels) {
      const p = +lv.price;
      if (!Number.isFinite(p)) continue;
      if ((lv.from | 0) < staleBefore || (lv.from | 0) >= this.i1) continue;
      const y = g.Y(p);
      if (y < g.priceY - 2 || y > g.priceY + g.priceH + 2) continue;
      const from = Math.max(this.i0, lv.from | 0);
      const x0 = from <= this.i0 ? PL : g.X(from);
      if (x0 > g.w - PR) continue;
      ctx.strokeStyle = C.ref; ctx.globalAlpha = .8;
      ctx.beginPath(); ctx.moveTo(x0, Math.round(y) + .5); ctx.lineTo(g.w - PR, Math.round(y) + .5); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = C.ref; ctx.textAlign = 'left';
      ctx.fillText(`${lv.label || '기준선'} ${fmtNum(p)}`, x0 + 5, y - 5);
    }
    ctx.restore();
    ctx.font = '10px ' + MONO;
  }

  _drawOscillators(ctx, C, g) {
    if (!g.oscs.length) return;
    const X = g.X;
    ctx.font = '10px ' + MONO;
    g.oscs.forEach((ind, k) => {
      const y0 = g.oscY + k * (g.oscPaneH + PANE_GAP);
      const a = ind.arr;
      let mn = Infinity, mx = -Infinity;
      for (let i = this.i0; i < this.i1; i++) {
        const v = a[i];
        if (v !== v) continue;
        if (v < mn) mn = v; if (v > mx) mx = v;
      }
      if (!(mx > mn)) { mn -= 1; mx += 1; }
      const pad = (mx - mn) * .08; mn -= pad; mx += pad;
      const Yo = (v) => y0 + (mx - v) / (mx - mn) * g.oscPaneH;

      ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PL, Math.round(y0) + .5); ctx.lineTo(g.w - PR, Math.round(y0) + .5);
      ctx.moveTo(PL, Math.round(y0 + g.oscPaneH) + .5); ctx.lineTo(g.w - PR, Math.round(y0 + g.oscPaneH) + .5);
      ctx.stroke();

      const stride = Math.max(1, Math.floor((this.i1 - this.i0) / Math.max(1, g.plotW)));
      ctx.strokeStyle = C.maSlots[ind.slot % C.maSlots.length];
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      let started = false, drew = false;
      for (let i = this.i0; i < this.i1; i += stride) {
        const v = a[i];
        if (v !== v) { started = false; continue; }
        const x = X(i), y = Yo(v);
        if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
        drew = true;
      }
      if (drew) ctx.stroke();

      ctx.fillStyle = C.axisTx; ctx.textAlign = 'left';
      ctx.fillText(ind.label, PL + 4, y0 + 11);
      ctx.textAlign = 'left';
      ctx.fillText(fmtNum(mx), g.w - PR + 6, y0 + 8);
      ctx.fillText(fmtNum(mn), g.w - PR + 6, y0 + g.oscPaneH);
    });
  }

  /** 마커: 색만이 아니라 삼각형 방향 + 라벨(B1/B2/S)을 함께 그린다 (접근성) */
  _drawMarkers(ctx, C, g) {
    const M = this.d.markers;
    if (!M.n) return;
    const Y = g.Y, X = g.X;
    for (let k = 0; k < M.n; k++) {
      const i = M.i[k];
      if (i < this.i0 || i >= this.i1) continue;
      const x = X(i);
      const type = M.type[k];

      if (type === MK_REF) {
        ctx.save();
        ctx.strokeStyle = C.ref; ctx.lineWidth = 1.4; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, g.priceY); ctx.lineTo(Math.round(x) + .5, g.priceY + g.priceH); ctx.stroke();
        ctx.restore();
        badge(ctx, x, g.priceY + 12, M.label[k] || '기준일', C.ref, C.markerTx);
        continue;
      }
      const buy = type === MK_BUY;
      const p = Number.isFinite(M.price[k]) ? M.price[k] : this.d.c[i];
      const y = Y(p) + (buy ? 17 : -17);
      ctx.fillStyle = buy ? C.buy : C.sell;
      ctx.beginPath();
      if (buy) { ctx.moveTo(x, y - 8); ctx.lineTo(x - 6, y + 3); ctx.lineTo(x + 6, y + 3); }
      else { ctx.moveTo(x, y + 8); ctx.lineTo(x - 6, y - 3); ctx.lineTo(x + 6, y - 3); }
      ctx.closePath(); ctx.fill();
      badge(ctx, x, buy ? y + 17 : y - 15, M.label[k] || (buy ? 'B' : 'S'), buy ? C.buy : C.sell, C.markerTx);
    }
  }

  _drawAxes(ctx, C, g) {
    const nVis = this.i1 - this.i0;
    const t = this.d.t;
    const spanDays = Math.abs(dateDiff(t[this.i0], t[this.i1 - 1]));
    const targetTicks = Math.max(3, Math.min(9, Math.floor(g.plotW / 110)));
    const stride = Math.max(1, Math.ceil(nVis / targetTicks));
    ctx.fillStyle = C.axisTx; ctx.textAlign = 'center'; ctx.font = '10px ' + MONO;
    for (let i = this.i0 + stride - (this.i0 % stride || stride); i < this.i1; i += stride) {
      ctx.fillText(axisLabel(t[i], spanDays), g.X(i), g.h - 7);
    }
  }

  /* ---------------- overlay 렌더 (mousemove 마다) ---------------- */

  _drawOverlay() {
    const t0 = performance.now();
    const ctx = this.octx, C = this.colors, g = this._geo;
    ctx.clearRect(0, 0, this._w, this._h);
    if (!g || !this.hasData()) { this.stats.overlayMs = performance.now() - t0; return; }

    // 강조 구간 (거래내역 행 클릭)
    if (this.highlight) {
      const from = Math.max(this.i0, this.highlight.from);
      const to = Math.min(this.i1 - 1, this.highlight.to);
      if (to >= from) {
        const cw = g.plotW / (this.i1 - this.i0);
        const x0 = g.X(from) - cw / 2, x1 = g.X(to) + cw / 2;
        ctx.save();
        ctx.strokeStyle = C.holdBd; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
        ctx.strokeRect(Math.round(x0) + .5, g.priceY + .5, Math.max(2, x1 - x0), g.priceH - 1);
        ctx.restore();
      }
    }

    const i = this.hover;
    if (i < this.i0 || i >= this.i1) { this.stats.overlayMs = performance.now() - t0; this.stats.overlayCount++; return; }

    const d = this.d;
    const x = Math.round(g.X(i)) + 0.5;
    const yPrice = g.Y(d.c[i]);

    ctx.save();
    ctx.strokeStyle = C.cross; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, g.priceY); ctx.lineTo(x, g.h - PB);
    const yh = Math.round(this.hoverY >= 0 ? this.hoverY : yPrice) + .5;
    if (yh > g.priceY && yh < g.priceY + g.priceH) { ctx.moveTo(PL, yh); ctx.lineTo(g.w - PR, yh); }
    ctx.stroke();
    ctx.restore();

    // 호버 봉 강조 (색 대비가 낮은 테마에서도 위치를 알 수 있게)
    const cw = g.plotW / (this.i1 - this.i0);
    if (cw >= 2.6) {
      ctx.strokeStyle = C.hilite; ctx.lineWidth = 1;
      ctx.strokeRect(Math.round(g.X(i) - cw * 0.36) + .5, Math.round(g.Y(d.h[i])) + .5,
        Math.max(2, Math.round(cw * 0.72)), Math.max(2, Math.round(g.Y(d.l[i]) - g.Y(d.h[i]))));
    }

    ctx.font = 'bold 10px ' + MONO;
    // 가격축 라벨
    const yLab = this.hoverY >= 0 && this.hoverY > g.priceY && this.hoverY < g.priceY + g.priceH
      ? this.hoverY : yPrice;
    const pLab = g.pMax - (yLab - g.priceY) / g.priceH * (g.pMax - g.pMin);
    tagBox(ctx, g.w - PR + 2, yLab, PR - 6, fmtNum(pLab), C.labelBg, C.labelTx, 'left');
    // 시간축 라벨
    ctx.textAlign = 'center';
    tagBoxCentered(ctx, x, g.h - PB + 9, ymd(d.t[i]).slice(2), C.labelBg, C.labelTx);
    ctx.font = '10px ' + MONO;

    this.stats.overlayMs = performance.now() - t0;
    this.stats.overlayCount++;
  }

  /* ---------------- 이벤트 ---------------- */

  _bind() {
    const host = this.host;

    this._ro = new ResizeObserver(() => this.requestBase());
    this._ro.observe(host);

    this._onTheme = () => this.invalidateTheme();
    window.addEventListener('themechange', this._onTheme);

    let dragging = false, lastX = 0, anchorI0 = 0, anchorI1 = 0;
    /** 동시에 눌린 포인터들 — 두 개면 핀치 줌 */
    const pts = new Map();
    let pinch = null;   // {dist, i0, i1, frac}

    host.addEventListener('pointerdown', (e) => {
      // 차트 위에 겹쳐 놓은 안내 패널의 버튼(다시 시도 / 종목 고르기 등)은 그대로 눌려야 한다.
      // 여기서 포인터를 캡처해 버리면 이어지는 click 이 캔버스로 리타깃되어 버튼이 죽는다.
      if (e.target.closest('button, a, input, select, textarea, [data-act]')) return;
      if (!this.hasData()) return;
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });

      if (pts.size === 2) {
        // 두 손가락 → 핀치 줌 시작. 팬은 중단한다.
        dragging = false;
        host.classList.remove('panning');
        const [a, b] = [...pts.values()];
        const r = host.getBoundingClientRect();
        const midX = (a.x + b.x) / 2 - r.left;
        pinch = {
          dist: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)),
          i0: this.i0, i1: this.i1,
          frac: this._geo ? Math.max(0, Math.min(1, (midX - PL) / this._geo.plotW)) : 0.5,
        };
        return;
      }
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      dragging = true; lastX = e.clientX;
      anchorI0 = this.i0; anchorI1 = this.i1;
      try { host.setPointerCapture(e.pointerId); } catch { /* 캡처 실패해도 팬은 동작한다 */ }
      host.classList.add('panning');
      this._hideHint();
    });

    host.addEventListener('pointermove', (e) => {
      if (pts.has(e.pointerId)) pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const r = host.getBoundingClientRect();
      const px = e.clientX - r.left;
      this.hoverY = e.clientY - r.top;

      // ---- 핀치 줌 ----
      if (pinch && pts.size === 2) {
        const [a, b] = [...pts.values()];
        const d = Math.max(1, Math.hypot(a.x - b.x, a.y - b.y));
        const span = pinch.i1 - pinch.i0;
        const next = Math.max(MIN_BARS, Math.min(this.d.n, Math.round(span * (pinch.dist / d))));
        const anchor = pinch.i0 + pinch.frac * span;
        this.setViewport(anchor - pinch.frac * next, anchor - pinch.frac * next + next);
        return;
      }

      // ---- 드래그 팬 ----
      if (dragging && this._geo) {
        const dx = e.clientX - lastX;
        // 드래그 시작 시점의 봉 폭을 기준으로 삼아야 팬 도중 가속되지 않는다
        const cw = this._geo.plotW / Math.max(1, anchorI1 - anchorI0);
        const barsMoved = Math.round(-dx / cw);
        if (barsMoved !== 0) {
          lastX = e.clientX;
          const moved = this.setViewport(this.i0 + barsMoved, this.i1 + barsMoved);
          // 클램프되어 못 움직였으면 왜 안 되는지 보여 준다
          if (!moved) this._flashEdge(barsMoved < 0 ? 'left' : 'right');
        }
      }

      const i = this.indexAt(px);
      const changed = i !== this.hover;
      this.hover = i;
      // 요구사항 6: 즉시 그리지 않고 rAF 한 프레임에 1회
      this.requestOverlay();
      if (this.onHover) this.onHover(this._hoverInfo(i, px, this.hoverY, changed));
    });

    const endDrag = (e) => {
      pts.delete(e.pointerId);
      if (pts.size < 2) pinch = null;
      if (!dragging) return;
      dragging = false;
      host.classList.remove('panning');
      try { host.releasePointerCapture(e.pointerId); } catch { /* 이미 해제됨 */ }
    };
    host.addEventListener('pointerup', endDrag);
    host.addEventListener('pointercancel', endDrag);

    host.addEventListener('pointerleave', () => {
      this.hover = -1; this.hoverY = -1;
      this.requestOverlay();
      if (this.onHover) this.onHover(null);
    });

    // 처음 차트에 들어왔을 때 한 번만 조작 방법을 알려 준다
    host.addEventListener('pointerenter', () => this._maybeShowHint(), { once: false });

    host.addEventListener('wheel', (e) => {
      if (!this.hasData()) return;
      e.preventDefault();
      const r = host.getBoundingClientRect();
      const px = e.clientX - r.left;
      const g = this._geo;
      const frac = g ? Math.max(0, Math.min(1, (px - PL) / g.plotW)) : 0.5;
      const nVis = this.i1 - this.i0;
      const anchor = this.i0 + frac * nVis;
      const k = Math.exp((e.deltaY > 0 ? 1 : -1) * 0.16);
      const next = Math.max(MIN_BARS, Math.min(this.d.n, Math.round(nVis * k)));
      this.setViewport(anchor - frac * next, anchor - frac * next + next);
    }, { passive: false });

    host.addEventListener('dblclick', () => this.fitAll());

    // 키보드 팬/줌 (접근성)
    host.tabIndex = 0;
    host.addEventListener('keydown', (e) => {
      if (!this.hasData()) return;
      const nVis = this.i1 - this.i0;
      const step = Math.max(1, Math.round(nVis * 0.1));
      if (e.key === 'ArrowLeft') {
        if (!this.setViewport(this.i0 - step, this.i1 - step)) this._flashEdge('left');
        e.preventDefault();
      } else if (e.key === 'ArrowRight') {
        if (!this.setViewport(this.i0 + step, this.i1 + step)) this._flashEdge('right');
        e.preventDefault();
      } else if (e.key === '+' || e.key === '=') { this.zoomBy(0.8); e.preventDefault(); }
      else if (e.key === '-' || e.key === '_') { this.zoomBy(1.25); e.preventDefault(); }
      else if (e.key === 'Home') { this.setViewport(0, nVis); this._flashEdge('left'); e.preventDefault(); }
      else if (e.key === 'End') { this.setViewport(this.d.n - nVis, this.d.n); this._flashEdge('right'); e.preventDefault(); }
      else if (e.key === '0') { this.fitAll(); e.preventDefault(); }
    });
  }

  zoomBy(k) {
    const nVis = this.i1 - this.i0;
    const mid = (this.i0 + this.i1) / 2;
    const next = Math.max(MIN_BARS, Math.min(this.d ? this.d.n : nVis, Math.round(nVis * k)));
    this.setViewport(mid - next / 2, mid + next / 2);
  }

  /** 화면 x 좌표 → 봉 인덱스 */
  indexAt(px) {
    if (!this.d || !this._geo) return -1;
    const g = this._geo;
    const cw = g.plotW / (this.i1 - this.i0);
    const i = this.i0 + Math.floor((px - PL) / cw);
    return (i < this.i0 || i >= this.i1) ? -1 : i;
  }

  _hoverInfo(i, px, py, changed) {
    if (i < 0 || !this.d) return null;
    const d = this.d;
    // 키로만 담으면 같은 지표가 두 번 있을 때 서로 덮어써서 값이 사라진다.
    // 범례는 순서가 보장되는 indicatorValues 를 쓴다.
    const ind = {};
    const indicatorValues = [];
    for (const x of d.ind) {
      const v = x.arr[i];
      const val = (v === v) ? v : null;
      ind[x.key] = val;
      indicatorValues.push(val);
    }
    let marker = null;
    const M = d.markers;
    for (let k = 0; k < M.n; k++) {
      if (M.i[k] === i) { marker = { type: ['ref', 'buy', 'sell', 'other'][M.type[k]], label: M.label[k], note: M.note[k], price: M.price[k] }; break; }
    }
    const halted = (d.flag[i] & 4) !== 0;
    // 거래정지일 직전 종가를 찾는다. 바로 앞도 정지면 등락률은 계산하지 않는다.
    const prevC = i > 0 ? d.c[i - 1] : d.c[i];
    const chgPct = (!halted && Number.isFinite(prevC) && prevC)
      ? (d.c[i] / prevC - 1) * 100 : 0;
    return {
      i, changed, px, py,
      date: ymd(d.t[i]),
      halted,
      o: d.o[i], h: d.h[i], l: d.l[i], c: d.c[i], v: d.v[i], amt: d.amt[i],
      chgPct,
      spike: !halted && (d.flag[i] & 2) !== 0,
      up: (d.flag[i] & 1) !== 0,
      indicators: ind,
      indicatorValues,
      indicatorMeta: d.ind.map((x) => ({
        key: x.key, label: x.label, slot: x.slot, fromStrategy: x.fromStrategy === true,
      })),
      marker,
    };
  }

  /** 지표 색(현재 테마 기준)을 범례가 쓸 수 있게 노출 */
  slotColor(slot) {
    const C = this.colors;
    return C.maSlots[slot % C.maSlots.length];
  }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    window.removeEventListener('themechange', this._onTheme);
    this.base.remove(); this.overlay.remove();
  }
}

const MONO = 'ui-monospace, "JetBrains Mono", Consolas, monospace';

/* ---------------- 캔버스 소품 ---------------- */

function badge(ctx, cx, cy, text, bg, fg) {
  ctx.save();
  ctx.font = 'bold 9.5px ' + MONO;
  const wd = ctx.measureText(text).width + 10;
  ctx.fillStyle = bg;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(cx - wd / 2, cy - 7, wd, 14, 3);
  else ctx.rect(cx - wd / 2, cy - 7, wd, 14);
  ctx.fill();
  ctx.fillStyle = fg; ctx.textAlign = 'center'; ctx.fillText(text, cx, cy + 3.5);
  ctx.restore();
}

function tagBox(ctx, x, y, w, text, bg, fg, align) {
  ctx.fillStyle = bg;
  ctx.fillRect(x, y - 8, w, 16);
  ctx.fillStyle = fg; ctx.textAlign = align;
  ctx.fillText(text, x + 4, y + 3.5);
}

function tagBoxCentered(ctx, cx, cy, text, bg, fg) {
  const w = ctx.measureText(text).width + 12;
  ctx.fillStyle = bg;
  ctx.fillRect(cx - w / 2, cy - 8, w, 16);
  ctx.fillStyle = fg; ctx.textAlign = 'center';
  ctx.fillText(text, cx, cy + 3.5);
}

/** YYYYMMDD 두 개의 대략적인 일수 차 (축 라벨 형식 결정용) */
function dateDiff(a, b) {
  const da = new Date((a / 10000) | 0, (((a / 100) | 0) % 100) - 1, a % 100);
  const db = new Date((b / 10000) | 0, (((b / 100) | 0) % 100) - 1, b % 100);
  return (db - da) / 86400000;
}

/* ============================================================
   우측 패널용 미니 차트 (수익 곡선 / 월별 수익률)
   base 하나만 쓰고, 테마 변경 시 다시 그린다.
   ============================================================ */
export class MiniChart {
  constructor(canvas, kind) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.kind = kind;            // 'equity' | 'monthly'
    this.data = null;
    this._raf = 0;
    this._ro = new ResizeObserver(() => this.schedule());
    this._ro.observe(canvas.parentElement || canvas);
    this._onTheme = () => this.schedule();
    window.addEventListener('themechange', this._onTheme);
  }

  set(data) { this.data = data; this.schedule(); }

  schedule() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); });
  }

  _fit() {
    const r = this.cv.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    if (this.cv.width !== Math.round(w * dpr) || this.cv.height !== Math.round(h * dpr)) {
      this.cv.width = Math.round(w * dpr); this.cv.height = Math.round(h * dpr);
    }
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w, h };
  }

  draw() {
    const { w, h } = this._fit();
    const ctx = this.ctx, C = themeColors();
    ctx.clearRect(0, 0, w, h);
    if (!this.data) {
      ctx.fillStyle = C.txDim; ctx.font = '11px ' + MONO; ctx.textAlign = 'center';
      ctx.fillText('백테스트를 실행하세요', w / 2, h / 2 + 4);
      return;
    }
    if (this.kind === 'equity') this._equity(ctx, C, w, h);
    else this._monthly(ctx, C, w, h);
  }

  _equity(ctx, C, w, h) {
    const { values, drawdown } = this.data;
    const n = values.length;
    if (n < 2) return;
    let hi = -Infinity, lo = Infinity;
    for (let i = 0; i < n; i++) { const v = values[i]; if (v > hi) hi = v; if (v < lo) lo = v; }
    lo = Math.min(lo, 100); hi = Math.max(hi, 100);
    const pad = (hi - lo) * .08 || 1;
    hi += pad; lo -= pad;
    const X = (i) => 4 + i / (n - 1) * (w - 8);
    const Y = (p) => 6 + (hi - p) / (hi - lo) * (h - 26);

    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath();
    for (const gg of [0, .5, 1]) { const y = 6 + (h - 26) * gg; ctx.moveTo(0, y + .5); ctx.lineTo(w, y + .5); }
    ctx.stroke();

    ctx.save();
    ctx.strokeStyle = C.baseLine; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(0, Y(100) + .5); ctx.lineTo(w, Y(100) + .5); ctx.stroke();
    ctx.restore();

    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, C.eqFill0); grad.addColorStop(1, C.eqFill1);
    ctx.beginPath(); ctx.moveTo(X(0), Y(values[0]));
    for (let i = 1; i < n; i++) ctx.lineTo(X(i), Y(values[i]));
    ctx.lineTo(X(n - 1), h - 20); ctx.lineTo(X(0), h - 20); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    ctx.beginPath();
    for (let i = 0; i < n; i++) { const x = X(i), y = Y(values[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
    ctx.strokeStyle = C.eqLine; ctx.lineWidth = 1.6; ctx.stroke();

    if (drawdown && drawdown.length === n) {
      let mi = 0, mv = 0;
      for (let i = 0; i < n; i++) if (drawdown[i] < mv) { mv = drawdown[i]; mi = i; }
      ctx.fillStyle = C.dn;
      ctx.beginPath(); ctx.arc(X(mi), Y(values[mi]), 3, 0, Math.PI * 2); ctx.fill();
      ctx.font = '9.5px ' + MONO; ctx.fillStyle = C.txSub; ctx.textAlign = 'left';
      ctx.fillText('MDD ' + mv.toFixed(1) + '%', Math.min(X(mi) + 6, w - 62), Y(values[mi]) + 3);
    }
    ctx.font = '9.5px ' + MONO; ctx.fillStyle = C.txSub; ctx.textAlign = 'right';
    ctx.fillText('최종 ' + values[n - 1].toFixed(1), w - 4, 14);
  }

  _monthly(ctx, C, w, h) {
    const rows = this.data;
    const n = rows.length;
    if (!n) return;
    let mx = 0;
    for (const r of rows) mx = Math.max(mx, Math.abs(r.return_pct));
    mx = (mx || 1) * 1.15;
    const bw = (w - 8) / n, zero = 8 + (h - 30) / 2;

    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, zero + .5); ctx.lineTo(w, zero + .5); ctx.stroke();

    // 양/음 각각 1회 fill
    for (const up of [1, 0]) {
      ctx.beginPath(); let any = false;
      rows.forEach((r, i) => {
        const v = r.return_pct;
        if ((v >= 0) !== !!up) return;
        const x = 4 + i * bw, bh = Math.abs(v) / mx * ((h - 30) / 2);
        ctx.rect(x + 1, v >= 0 ? zero - bh : zero, Math.max(1, bw - 3), Math.max(1, bh));
        any = true;
      });
      if (!any) continue;
      ctx.fillStyle = up ? C.up : C.dn; ctx.fill();
    }

    ctx.font = '9px ' + MONO; ctx.fillStyle = C.txSub; ctx.textAlign = 'center';
    const step = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(w / 34))));
    rows.forEach((r, i) => {
      if (i % step) return;
      const m = String(r.month || '');
      ctx.fillText(m.slice(5) === '01' ? m.slice(2, 4) + '.01' : m.slice(5), 4 + i * bw + bw / 2, h - 4);
    });
  }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    window.removeEventListener('themechange', this._onTheme);
  }
}
