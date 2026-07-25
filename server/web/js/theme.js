/* ============================================================
   theme.js — 다크/라이트 테마 전환 + 캔버스 색상 브리지
   ------------------------------------------------------------
   · 최초 진입은 prefers-color-scheme 를 따른다.
   · 사용자가 직접 고르면 localStorage.theme 에 저장하고 그 뒤로는 그 값을 쓴다.
   · 캔버스는 CSS 를 못 읽으므로 getComputedStyle 로 토큰을 읽어 캐시한다.
     테마가 바뀌면 캐시를 통째로 버리고 'themechange' 이벤트를 쏜다.
   ============================================================ */

const STORAGE_KEY = 'theme';
const THEMES = ['dark', 'light'];

/* 캔버스가 쓰는 토큰 목록. 여기 없는 색을 캔버스에서 쓰지 않는다. */
const CHART_TOKENS = [
  'ch-bg', 'ch-grid', 'ch-grid-str', 'ch-axis-tx', 'ch-axis-bg', 'ch-label-bg', 'ch-label-tx',
  'ch-up', 'ch-dn', 'ch-up-wick', 'ch-dn-wick', 'ch-vol-up', 'ch-vol-dn', 'ch-vol-hi',
  'ch-cross', 'ch-buy', 'ch-sell', 'ch-ref', 'ch-hold', 'ch-hold-bd', 'ch-marker-tx', 'ch-hilite',
  'ch-ma-1', 'ch-ma-2', 'ch-ma-3', 'ch-ma-4', 'ch-ma-5', 'ch-ma-6',
  'ch-eq-line', 'ch-eq-fill-0', 'ch-eq-fill-1', 'ch-dd-fill', 'ch-base-line',
  // 캔버스에서 UI 색이 필요한 경우 (ch- 접두어가 없으므로 이름 충돌 주의)
  'tx', 'tx-sub', 'tx-dim', 'bd', 'bg-1', 'bg-2', 'bg-3',
];

/** 테마별 색상 캐시. { dark: {...}, light: {...} } */
const cache = Object.create(null);

/** 지표 라인에 순서대로 배정할 색 토큰 */
export const MA_SLOTS = ['ch-ma-1', 'ch-ma-2', 'ch-ma-3', 'ch-ma-4', 'ch-ma-5', 'ch-ma-6'];

let current = 'dark';
const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: light)') : null;

/** 저장된 사용자 선택. 없으면 null. */
function stored() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return THEMES.includes(v) ? v : null;
  } catch {
    return null; // 사생활 보호 모드 등에서 localStorage 접근이 막힐 수 있다
  }
}

function systemTheme() {
  return media && media.matches ? 'light' : 'dark';
}

/** 현재 테마 이름 */
export function getTheme() {
  return current;
}

/** 사용자가 명시적으로 고른 적이 있는가 */
export function isExplicit() {
  return stored() !== null;
}

/**
 * 테마 적용.
 * @param {'dark'|'light'} name
 * @param {boolean} persist  사용자 조작이면 true (localStorage 에 저장)
 */
export function setTheme(name, persist = true) {
  if (!THEMES.includes(name)) name = 'dark';
  const changed = current !== name || document.documentElement.dataset.theme !== name;
  current = name;
  document.documentElement.dataset.theme = name;
  document.documentElement.style.colorScheme = name;

  if (persist) {
    try { localStorage.setItem(STORAGE_KEY, name); } catch { /* 저장 실패는 무시 — 세션 내에서는 동작한다 */ }
  }

  if (changed) {
    // 캐시 무효화가 먼저다. 리스너가 colors() 를 부르면 새 값이 나와야 한다.
    delete cache[name];
    window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: name } }));
  }
  return name;
}

/** 부트스트랩. index.html 의 인라인 스크립트가 이미 data-theme 을 심어 뒀을 수 있다. */
export function initTheme() {
  const pre = document.documentElement.dataset.theme;
  const t = stored() || (THEMES.includes(pre) ? pre : systemTheme());
  current = t;
  document.documentElement.dataset.theme = t;
  document.documentElement.style.colorScheme = t;

  // 사용자가 고른 적이 없으면 OS 설정 변화를 따라간다.
  if (media && typeof media.addEventListener === 'function') {
    media.addEventListener('change', () => {
      if (!isExplicit()) setTheme(systemTheme(), false);
    });
  }
  return t;
}

/**
 * 캔버스용 색상 팔레트. 테마별로 1회만 getComputedStyle 을 호출한다.
 * 반환 객체의 키는 CHART_TOKENS 에서 'ch-' 접두어를 뺀 camelCase.
 *   --ch-up      -> colors().up
 *   --ch-ma-1    -> colors().ma1
 *   --tx-sub     -> colors().txSub
 */
export function colors() {
  const t = current;
  if (cache[t]) return cache[t];

  const cs = getComputedStyle(document.documentElement);
  const out = Object.create(null);
  for (const token of CHART_TOKENS) {
    const raw = cs.getPropertyValue('--' + token).trim();
    out[camel(token)] = raw || '#888888';
  }
  // 지표 슬롯을 배열로도 제공 (인덱스로 돌려쓰기 편하도록)
  out.maSlots = MA_SLOTS.map((k) => out[camel(k)]);
  cache[t] = out;
  return out;
}

/** --ch-ma-1 → ma1 / --tx-sub → txSub */
function camel(token) {
  const s = token.startsWith('ch-') ? token.slice(3) : token;
  return s.replace(/-(\w)/g, (_, c) => c.toUpperCase());
}

/** 테마 무관하게 캐시를 비운다 (폰트/사용자 CSS 변경 등 특수 상황용) */
export function invalidateColors() {
  for (const k of Object.keys(cache)) delete cache[k];
}

/**
 * 헤더의 세그먼트 컨트롤을 실제로 동작시킨다.
 * @param {HTMLElement} root  버튼들을 담고 있는 .seg 컨테이너
 */
export function bindThemeToggle(root) {
  if (!root) return;
  const buttons = Array.from(root.querySelectorAll('button[data-theme]'));

  const sync = () => {
    for (const b of buttons) {
      b.setAttribute('aria-pressed', String(b.dataset.theme === current));
    }
  };

  for (const b of buttons) {
    b.addEventListener('click', () => {
      setTheme(b.dataset.theme, true);
      sync();
    });
  }
  window.addEventListener('themechange', sync);
  sync();
}
