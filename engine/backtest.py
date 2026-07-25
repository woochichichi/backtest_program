"""백테스트 엔진 — 스캔 → 진입 → 청산 → 포트폴리오.

    from engine.backtest import run_backtest
    result = run_backtest(strategy, store, progress=None)

``result`` 는 ARCHITECTURE 4-5 의 ``POST /api/backtest`` 응답과 **키 이름까지 동일한 dict** 다.
서버는 그대로 직렬화만 하면 된다.

체결 규칙은 ARCHITECTURE 2장을 그대로 따른다.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .dsl import EvalContext, evaluate_condition, evaluate_operand
from .errors import DataUnavailable, DSLError, StrategyError
from .indicators import REGISTRY
from .metrics import _i, build_equity, by_stock_summary, compute_metrics, monthly_returns
from .validate import validate_strategy

__all__ = ["run_backtest", "EOK", "SAME_DAY_EXIT_MODES", "DEFAULT_SAME_DAY_EXIT"]

#: 1억
EOK = 100_000_000.0

#: signals 배열 최대 길이 (프런트 로그 탭 보호)
MAX_SIGNALS = 4000

#: ``execution.same_day_exit`` — 진입 체결 당일의 청산 평가 정책
SAME_DAY_EXIT_MODES = ("loss_only", "never", "always")

#: 기본값. 일봉만으로는 저가·고가 순서를 알 수 없으므로 보수적으로 잡는다.
DEFAULT_SAME_DAY_EXIT = "loss_only"

#: ``same_day_exit="loss_only"`` 에서 진입 당일 평가를 **막는** 청산 타입.
#: type 이 없는 커스텀 규칙은 손실 방향으로 간주해 당일 평가를 허용한다(보수적).
_PROFIT_EXIT_TYPES = frozenset({"take_profit"})

_PRICE_COLS = ["Open", "High", "Low", "Close", "Volume", "Amount"]

_MARKET_ALIASES = {
    "KOSPI": {"KOSPI", "STK", "유가증권", "유가증권시장"},
    "KOSDAQ": {"KOSDAQ", "KSQ", "코스닥", "코스닥시장"},
    "KONEX": {"KONEX", "KNX", "코넥스"},
}


# ======================================================================================
# 작은 헬퍼
# ======================================================================================


def _r(v, nd=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, nd)


def _d(v) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return pd.Timestamp(v).date()


class _Signals:
    """SCAN / MATCH / WATCH / FILL / POS / PNL / DONE 로그 수집기."""

    def __init__(self, limit: int = MAX_SIGNALS):
        self.items: List[dict] = []
        self.limit = limit
        self.truncated = False
        self.dropped = 0

    def add(self, day, level: str, code: Optional[str], message: str, hhmmss="09:00:00"):
        if len(self.items) >= self.limit:
            self.truncated = True
            self.dropped += 1
            return
        d = _d(day) if day is not None else None
        self.items.append(
            {
                "ts": f"{d.isoformat()} {hhmmss}" if d else "",
                "level": level,
                "code": code,
                "message": message,
            }
        )


def _fmt(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


# ======================================================================================
# 유니버스 / 기준일 스캔  (1단계 — 전부 벡터 연산)
# ======================================================================================


def _market_mask(series: pd.Series, markets: Sequence[str]) -> pd.Series:
    allowed = set()
    for m in markets or []:
        allowed |= _MARKET_ALIASES.get(str(m).upper(), {str(m).upper()})
    s = series.astype(str).str.upper().str.strip()
    return s.isin(allowed)


def _eligible_codes(panel: pd.DataFrame, universe: Mapping) -> Tuple[pd.Index, dict]:
    """시장 / 제외 조건을 종목 단위로 적용한다 (각 종목의 최신 행 기준)."""
    last = panel.drop_duplicates("Code", keep="last")
    stats = {"total": int(len(last))}
    keep = pd.Series(True, index=last.index)

    markets = universe.get("markets") or []
    if markets and "Market" in last.columns:
        mm = _market_mask(last["Market"], markets)
        if "MarketId" in last.columns:
            mm |= _market_mask(last["MarketId"], markets)
        keep &= mm

    ex = {str(e).upper() for e in (universe.get("exclude") or [])}
    name = last["Name"].astype(str) if "Name" in last.columns else pd.Series("", index=last.index)
    dept = last["Dept"].astype(str) if "Dept" in last.columns else pd.Series("", index=last.index)
    code = last["Code"].astype(str)

    if "PREFERRED" in ex:
        # 우선주는 표준코드 끝자리가 0 이 아니거나 종목명이 '우'/'우B'/'우C' 로 끝난다
        keep &= ~((code.str[-1] != "0") | name.str.contains(r"우(?:B|C)?$", regex=True))
    if "SPAC" in ex:
        keep &= ~name.str.contains("스팩|기업인수목적", regex=True)
    if "ETF" in ex:
        keep &= ~dept.str.contains("ETF", regex=False)
        keep &= ~name.str.contains(
            r"KODEX|TIGER|KBSTAR|ARIRANG|KINDEX|HANARO|SOL |ACE |PLUS |RISE ", regex=True
        )
    if "ETN" in ex:
        keep &= ~dept.str.contains("ETN", regex=False)
        keep &= ~name.str.contains("ETN", regex=False)
    if "REIT" in ex:
        keep &= ~name.str.contains("리츠", regex=False)
    if "ADMIN_ISSUE" in ex:
        keep &= ~dept.str.contains("관리", regex=False)
    if "TRADE_HALT" in ex:
        keep &= ~dept.str.contains("정지", regex=False)

    codes = pd.Index(last.loc[keep, "Code"].unique())
    stats["eligible"] = int(len(codes))
    return codes, stats


def _reference_events(panel: pd.DataFrame, universe: Mapping, start: dt.date) -> pd.DataFrame:
    """기준일 후보 (Code, Date) 를 벡터 연산으로 뽑는다."""
    rd = dict(universe.get("reference_day") or {})
    rule = str(rd.get("rule") or "amount_spike")
    g = panel.groupby("Code", sort=False)

    if rule == "none":
        mask = pd.Series(True, index=panel.index)
    elif rule == "amount_spike":
        spike = float(rd.get("spike_amount_krw_eok") or 0) * EOK
        mask = panel["Amount"] >= spike
        prev_max = rd.get("prev_day_amount_max_eok")
        if prev_max is not None:
            prev = g["Amount"].shift(1)
            mask &= prev.notna() & (prev <= float(prev_max) * EOK)
    elif rule == "volume_spike":
        n = int(rd.get("volume_ma_period") or 20)
        mult = float(rd.get("volume_mult") or 3.0)
        ma = g["Volume"].transform(lambda s: s.rolling(n, min_periods=n).mean())
        mask = ma.notna() & (panel["Volume"] >= mult * ma)
    elif rule == "range_breakout":
        n = int(rd.get("breakout_period") or rd.get("lookback_days") or 20)
        hh = g["High"].transform(lambda s: s.rolling(n, min_periods=n).max().shift(1))
        mask = hh.notna() & (panel["Close"] > hh)
    else:  # pragma: no cover - validate 에서 차단됨
        mask = pd.Series(False, index=panel.index)

    mask &= panel["Date"] >= pd.Timestamp(start)
    cols = [c for c in ("Code", "Date", "Amount", "Open", "High", "Low", "Close") if c in panel.columns]
    return panel.loc[mask, cols]


# ======================================================================================
# 지표 워밍업 구간 산정
# ======================================================================================


def _walk_indicator_specs(node: Any, out: List[Mapping]) -> None:
    if isinstance(node, Mapping):
        key = node.get("indicator") or node.get("type")
        if isinstance(key, str) and key.upper() in REGISTRY:
            out.append(node)
        for v in node.values():
            _walk_indicator_specs(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk_indicator_specs(v, out)


def _warmup_bars(strategy: Mapping) -> int:
    specs: List[Mapping] = []
    _walk_indicator_specs(strategy.get("entries"), specs)
    _walk_indicator_specs(strategy.get("exits"), specs)
    _walk_indicator_specs(strategy.get("indicators"), specs)
    longest = 0
    for s in specs:
        key = str(s.get("indicator") or s.get("type")).upper()
        reg = REGISTRY.get(key)
        if not reg:
            continue
        for p in reg["params"]:
            if p["type"] != "int":
                continue
            v = s.get(p["name"], p["default"])
            try:
                longest = max(longest, int(v))
            except (TypeError, ValueError):
                pass
    rd = (strategy.get("universe") or {}).get("reference_day") or {}
    try:
        longest = max(longest, int(rd.get("lookback_days") or 0))
    except (TypeError, ValueError):
        pass
    return max(longest + 10, 30)


# ======================================================================================
# 체결
# ======================================================================================


def _direction(rule: Mapping, side: str) -> str:
    """목표가를 위/아래 어느 쪽에서 만나는지. 갭 체결 판정에 쓴다."""
    when = rule.get("when")
    if isinstance(when, Mapping):
        op = str(when.get("op") or "").lower()
        if op in ("<", "<="):
            return "down"
        if op in (">", ">="):
            return "up"
        if op == "cross_below":
            return "down"
        if op == "cross_above":
            return "up"
    t = str(rule.get("type") or "")
    if t in ("stop_loss", "trailing_stop"):
        return "down"
    if t == "take_profit":
        return "up"
    return "down" if side == "buy" else "up"


def _fill_price(
    rule: Mapping,
    ctx: EvalContext,
    li: int,
    bar: Mapping[str, float],
    nxt: Optional[Mapping[str, float]],
    side: str,
    fill_model: str,
) -> Optional[float]:
    """ARCHITECTURE 2장 체결가 규칙.

    * ``touch``     — 목표가 그대로. 단 **갭으로 목표가를 지나쳤으면 당일 시가**
    * ``next_open`` — 다음 봉 시가
    * ``close``     — 당일 종가
    """
    if fill_model == "close":
        return float(bar["close"])
    if fill_model == "next_open":
        return float(nxt["open"]) if nxt else None

    spec = rule.get("price")
    if spec is None:
        return float(bar["close"])
    try:
        target = evaluate_operand(spec, ctx, li)
    except DSLError:
        return float(bar["close"])
    if target is None or not math.isfinite(float(target)):
        return float(bar["close"])
    target = float(target)

    direction = _direction(rule, side)
    o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
    if direction == "down":
        if o <= target:          # 갭하락으로 목표가를 지나쳐 시작 → 시가 체결
            return o
    else:
        if o >= target:          # 갭상승으로 목표가를 지나쳐 시작 → 시가 체결
            return o
    return min(max(target, l), h)


# ======================================================================================
# 메인
# ======================================================================================


def run_backtest(
    strategy: Mapping,
    store,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    t0 = time.perf_counter()
    run_id = "r_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    def report(done: int, message: str) -> None:
        if progress is not None:
            try:
                progress(int(done), 100, message)
            except Exception:  # pragma: no cover - 콜백 오류가 백테스트를 죽이면 안 된다
                pass

    ok, errors = validate_strategy(strategy)
    if not ok:
        raise StrategyError("전략 스키마 검증에 실패했습니다", errors)

    warnings: List[str] = []
    sig = _Signals()

    # ---------------------------------------------------------------- 설정 파싱
    market = dict(strategy.get("market") or {})
    universe = dict(strategy.get("universe") or {})
    portfolio = dict(strategy.get("portfolio") or {})
    execution = dict(strategy.get("execution") or {})
    period = dict(strategy.get("period") or {})

    entries = list(strategy.get("entries") or [])
    exits = list(strategy.get("exits") or [])
    exit_priority = list(strategy.get("exit_priority") or ["SL", "TP"])
    ordered_exits = _order_exits(exits, exit_priority)

    fill_model = str(execution.get("fill_model") or "touch")
    slippage = float(execution.get("slippage_pct") or 0.0) / 100.0
    fee = float(execution.get("fee_pct") or 0.0) / 100.0

    if "initial_capital_manwon" in portfolio:
        initial_capital = float(portfolio["initial_capital_manwon"]) * 10_000.0
    else:
        initial_capital = float(portfolio.get("initial_capital") or 100_000_000.0)
    max_positions = int(portfolio.get("max_positions") or 1)
    sizing = str(portfolio.get("position_sizing") or "equal_weight")
    allow_dup = bool(portfolio.get("allow_duplicate_symbol", False))
    valid_days = int(universe.get("valid_days_after_reference") or 60)
    ucondition = str(universe.get("condition") or "none")
    pullback_pct = universe.get("pullback_pct")

    same_day_exit = str(execution.get("same_day_exit") or DEFAULT_SAME_DAY_EXIT)
    if same_day_exit not in SAME_DAY_EXIT_MODES:
        same_day_exit = DEFAULT_SAME_DAY_EXIT

    requested_resolution = str(
        market.get("trade_resolution") or execution.get("resolution") or "1d"
    )
    if requested_resolution != "1d":
        warnings.append(
            f"{requested_resolution} 체결을 요청했지만 일봉 데이터만 있어 일봉으로 근사했습니다. "
            "실행 가정은 assumptions 를 확인하세요."
        )
    if same_day_exit == "always":
        warnings.append(
            "same_day_exit=always 는 진입 당일 익절을 허용합니다. "
            "일봉만으로는 저가·고가 순서를 알 수 없어 성과가 실제보다 좋게 나올 수 있습니다."
        )

    stats = {"same_day_entry_exit": 0, "same_day_entry_exit_pct": 0.0, "ambiguous_bars": 0}

    def make_assumptions() -> dict:
        return {
            "resolution": "1d",
            "requested_resolution": requested_resolution,
            "fill_model": fill_model,
            "same_day_exit": same_day_exit,
            "slippage_pct": _r(slippage * 100.0, 4),
            "fee_pct": _r(fee * 100.0, 4),
            "exit_priority": [r.get("id") for r in ordered_exits],
            "notes": _assumption_notes(
                requested_resolution, fill_model, same_day_exit, slippage, fee,
                ordered_exits, stats,
            ),
            "stats": dict(stats),
        }

    # ---------------------------------------------------------------- 기간
    report(2, "기간 확정")
    start = _d(period.get("start"))
    end_raw = period.get("end", "auto")
    if end_raw in (None, "", "auto"):
        end = store.latest_date()
    else:
        end = _d(end_raw)
    if end < start:
        raise DataUnavailable(f"백테스트 구간이 뒤집혔습니다: {start} ~ {end}")

    warm = _warmup_bars(strategy)
    hist_start = start - dt.timedelta(days=int(warm * 1.75) + 30)

    # ---------------------------------------------------------------- 패널 로드
    report(5, "시세 데이터 로드")
    panel = store.panel(hist_start, end)
    panel = panel.sort_values(["Code", "Date"], kind="stable").reset_index(drop=True)

    codes, ustats = _eligible_codes(panel, universe)
    if len(codes) < ustats["total"]:
        panel = panel[panel["Code"].isin(codes)]

    calendar = [d.date() for d in pd.DatetimeIndex(sorted(panel["Date"].unique()))]
    calendar = [d for d in calendar if start <= d <= end]
    if not calendar:
        raise DataUnavailable(f"{start} ~ {end} 구간에 거래일이 없습니다")

    sig.add(
        calendar[0],
        "SCAN",
        None,
        f"전체 {ustats['eligible']:,}종목 스캔 · {universe.get('reference_day', {}).get('rule', 'amount_spike')} 기준일 탐색",
    )

    # ---------------------------------------------------------------- 1단계 스캔
    report(12, "기준일 스캔")
    events = _reference_events(panel, universe, start)
    if events.empty:
        return _empty_result(
            run_id, t0, warnings, sig, calendar, initial_capital, start, end,
            "기준일 조건을 만족하는 종목이 없습니다.", make_assumptions(),
        )

    cand_codes = pd.Index(events["Code"].unique())
    report(20, f"후보 {len(cand_codes):,}종목")

    # ---------------------------------------------------------------- 후보 종목 봉 준비
    sub = panel[panel["Code"].isin(cand_codes)]
    recs: Dict[str, dict] = {}
    for code, gdf in sub.groupby("Code", sort=False):
        gdf = gdf.sort_values("Date", kind="stable")
        df = gdf.set_index("Date")[[c for c in _PRICE_COLS if c in gdf.columns]]
        recs[code] = {
            "df": df,
            "name": str(gdf["Name"].iloc[-1]) if "Name" in gdf.columns else code,
            "pos": {d.date(): i for i, d in enumerate(df.index)},
            "open": df["Open"].to_numpy("float64"),
            "high": df["High"].to_numpy("float64"),
            "low": df["Low"].to_numpy("float64"),
            "close": df["Close"].to_numpy("float64"),
            "volume": df["Volume"].to_numpy("float64") if "Volume" in df else np.zeros(len(df)),
            "amount": df["Amount"].to_numpy("float64") if "Amount" in df else np.zeros(len(df)),
            "ind": {},
            "ctx": None,
        }

    events_by_day: Dict[dt.date, List[Tuple[str, dict]]] = {}
    for row in events.itertuples(index=False):
        d = _d(row.Date)
        if d < start or d > end:
            continue
        events_by_day.setdefault(d, []).append(
            (
                row.Code,
                {
                    "open": float(row.Open),
                    "high": float(row.High),
                    "low": float(row.Low),
                    "close": float(row.Close),
                    "amount": float(getattr(row, "Amount", 0.0) or 0.0),
                },
            )
        )

    # ---------------------------------------------------------------- 2단계 시뮬레이션
    report(25, "체결 시뮬레이션")
    cash = initial_capital
    active: Dict[str, dict] = {}
    trades: List[dict] = []
    eq_values: List[float] = []
    dsl_error_logged = False
    n_days = len(calendar)

    for gi, day in enumerate(calendar):
        if progress is not None and (gi % 20 == 0 or gi == n_days - 1):
            report(25 + int(65 * (gi + 1) / n_days), f"{day.isoformat()} 시뮬레이션")

        # --- (a) 기준일 이벤트 등록 / 갱신 (조건 만족일이 여럿이면 가장 최근을 채택)
        for code, refbar in events_by_day.get(day, ()):
            rec = recs.get(code)
            if rec is None:
                continue
            li = rec["pos"].get(day)
            if li is None:
                continue
            w = active.get(code)
            if w is None:
                active[code] = _new_watch(code, day, li, gi, refbar)
                sig.add(
                    day, "MATCH", code,
                    f"{code} {rec['name']} — 기준일 {day.isoformat()} 거래대금 "
                    f"{refbar['amount'] / EOK:,.0f}억 → 후보 등록",
                )
                sig.add(
                    day, "WATCH", code,
                    f"{code} 목표 진입가 = 기준일 시가 {_fmt(refbar['open'])} · 감시 시작",
                )
            elif not w["fills"]:
                w.update(ref_date=day, ref_li=li, ref_gi=gi, ref=refbar)
                sig.add(
                    day, "MATCH", code,
                    f"{code} 기준일 갱신 → {day.isoformat()} "
                    f"(거래대금 {refbar['amount'] / EOK:,.0f}억, 더 최근 날짜 채택)",
                )

        # --- (b) 활성 후보 평가: 진입 → 청산 (같은 날 진입+청산 허용, 진입 먼저)
        for code in sorted(active):
            w = active.get(code)
            if w is None:
                continue
            rec = recs[code]
            li = rec["pos"].get(day)
            if li is None:
                continue  # 거래정지 등 — 해당일 봉 없음

            bar = _bar(rec, li)
            nxt = _bar(rec, li + 1) if li + 1 < len(rec["close"]) else None
            ctx = _ctx(rec, w)

            # 유효기간 만료
            if not w["fills"] and (gi - w["ref_gi"]) > valid_days:
                sig.add(day, "WATCH", code,
                        f"{code} 기준일 {w['ref_date'].isoformat()} 이후 {valid_days}영업일 내 미체결 → 후보 폐기")
                active.pop(code, None)
                continue

            # ---- 진입 (기준일 **다음** 거래일부터 감시한다)
            for rule in entries if gi > w["ref_gi"] else ():
                rid = rule.get("id")
                if rid in w["fills"]:
                    continue
                if any(r not in w["fills"] for r in (rule.get("requires") or [])):
                    continue
                first = not w["fills"]
                if first:
                    if len([1 for x in active.values() if x["qty"] > 0]) >= max_positions:
                        continue
                    if not allow_dup and w["qty"] <= 0 and _holding(active, code):
                        continue
                    if not _universe_gate(ucondition, pullback_pct, bar, w["ref"], fill_model):
                        continue

                ctx.params = dict(rule)
                try:
                    hit = evaluate_condition(rule.get("when"), ctx, li)
                except DSLError as e:
                    if not dsl_error_logged:
                        warnings.append(f"조건식 평가 실패(무시): {e}")
                        dsl_error_logged = True
                    continue
                if not hit:
                    continue

                px = _fill_price(rule, ctx, li, bar, nxt, "buy", fill_model)
                if px is None or px <= 0:
                    continue
                fill_date = day if fill_model != "next_open" else _next_day(rec, li)
                buy_px = px * (1.0 + slippage)

                equity_now = cash + _mtm(active, recs, day)
                if w["planned"] is None:
                    w["planned"] = _planned_position(sizing, portfolio, equity_now, max_positions)
                qty = _entry_qty(rule, sizing, portfolio, w["planned"], equity_now, buy_px, cash)
                if qty <= 0:
                    sig.add(day, "WATCH", code,
                            f"{code} {rule.get('label') or rid} 조건 성립했으나 현금 부족으로 미체결", "09:05:00")
                    continue

                cost = qty * buy_px
                cash -= cost
                w["qty"] += qty
                w["cost"] += cost
                w["fills"][rid] = {"price": float(px), "qty": int(qty), "date": fill_date}
                w["fill_log"].append(
                    {"rule": rid, "date": fill_date.isoformat(), "price": _r(buy_px, 2), "qty": int(qty)}
                )
                w["last_entry_date"] = fill_date
                if w["entry_date"] is None:
                    w["entry_date"] = fill_date
                    w["peak"] = bar["high"]

                sig.add(day, "FILL", code,
                        f"BUY {code} @ {_fmt(buy_px)} × {qty}주 (비중 {rule.get('size_pct', 100)}%) "
                        f"— {rule.get('label') or rid}", "15:30:00")
                sig.add(day, "POS", code,
                        f"{code} 평단 {_fmt(w['cost'] / w['qty'])} · 보유 {w['qty']}주", "15:30:00")

            # ---- 청산
            if w["qty"] > 0:
                w["peak"] = max(w["peak"] or bar["high"], bar["high"])
                ctx = _ctx(rec, w, bar=bar, day=day)
                entry_today = w["last_entry_date"] == day
                ambiguous_here = False
                for rule in ordered_exits:
                    if w["qty"] <= 0:
                        break
                    ctx.params = dict(rule)
                    hit, forced_price = _exit_hit(rule, ctx, li, bar, w, day)
                    if not hit:
                        continue
                    if entry_today:
                        # 진입과 청산이 같은 봉 안에서 모두 성립 — 일봉으로는 순서를 알 수 없다
                        ambiguous_here = True
                        if not _same_day_allowed(same_day_exit, rule):
                            sig.add(
                                day, "POS", code,
                                f"{code} {rule.get('label') or rule.get('id')} 조건이 진입 당일 성립했으나 "
                                f"same_day_exit={same_day_exit} 규칙에 따라 다음 거래일로 이월",
                                "15:31:00",
                            )
                            continue
                    px = forced_price
                    if px is None:
                        px = _fill_price(rule, ctx, li, bar, nxt, "sell", fill_model)
                    if px is None or px <= 0:
                        continue
                    exit_date = day if fill_model != "next_open" else _next_day(rec, li)
                    qty_out = _exit_qty(rule, w["qty"])
                    if qty_out <= 0:
                        continue
                    trade, proceeds = _close(
                        w, rec, code, rule, px, qty_out, exit_date, slippage, fee,
                        len(trades) + 1, rule.get("label") or rule.get("id"),
                    )
                    cash += proceeds
                    trades.append(trade)
                    sig.add(day, "FILL", code,
                            f"SELL {code} @ {_fmt(trade['exit_price'])} × {qty_out}주 "
                            f"— {trade['exit_reason']}", "15:30:00")
                    sig.add(day, "PNL", code,
                            f"{code} 실현손익 {trade['pnl']:+,.0f}원 ({trade['return_pct']:+.2f}%) "
                            f"· 보유 {trade['hold_days']}일", "15:30:00")
                if ambiguous_here:
                    stats["ambiguous_bars"] += 1
                if w["qty"] <= 0:
                    active.pop(code, None)

        eq_values.append(cash + _mtm(active, recs, day))

    # ---------------------------------------------------------------- 미청산 강제 청산
    report(92, "미청산 포지션 정리")
    last_day = calendar[-1]
    for code in sorted(active):
        w = active[code]
        if w["qty"] <= 0:
            continue
        rec = recs[code]
        li = _last_index_upto(rec, last_day)
        if li is None:
            continue
        px = float(rec["close"][li])
        qty_out = w["qty"]
        trade, proceeds = _close(
            w, rec, code, {"id": "EOD", "label": "기간종료"}, px, qty_out, last_day,
            slippage, fee, len(trades) + 1, "기간종료",
        )
        cash += proceeds
        trades.append(trade)
        sig.add(last_day, "FILL", code,
                f"SELL {code} @ {_fmt(trade['exit_price'])} × {qty_out}주 — 기간종료 강제청산",
                "15:30:00")
        sig.add(last_day, "PNL", code,
                f"{code} 실현손익 {trade['pnl']:+,.0f}원 ({trade['return_pct']:+.2f}%) "
                f"· 보유 {trade['hold_days']}일", "15:30:00")
        active.pop(code, None)
    eq_values[-1] = cash + _mtm(active, recs, last_day)

    # ---------------------------------------------------------------- 성과
    report(96, "성과 집계")
    metrics = compute_metrics(calendar, eq_values, trades, initial_capital, start, end)
    equity = build_equity(calendar, eq_values, initial_capital)
    monthly = monthly_returns(calendar, eq_values)

    sig.add(
        last_day, "DONE", None,
        f"누적 {metrics['trades']}거래 · 승률 {metrics['win_rate_pct']}% · "
        f"총수익률 {metrics['total_return_pct']:+}%",
        "15:30:00",
    )
    if sig.truncated:
        warnings.append(
            f"시그널 로그가 {MAX_SIGNALS}건을 넘어 이후 {sig.dropped:,}건은 생략했습니다."
        )

    stats["same_day_entry_exit"] = sum(1 for t in trades if t["hold_days"] == 0)
    stats["same_day_entry_exit_pct"] = (
        _r(stats["same_day_entry_exit"] / len(trades) * 100.0) if trades else 0.0
    )
    if stats["same_day_entry_exit"]:
        warnings.append(
            f"진입일과 청산일이 같은 거래가 {stats['same_day_entry_exit']:,}건"
            f"(전체 {len(trades):,}건 중 {stats['same_day_entry_exit_pct']}%)입니다. "
            "일봉만으로는 하루 안의 체결 순서를 확정할 수 없습니다."
        )

    report(100, "완료")
    return {
        "run_id": run_id,
        "elapsed_sec": _r(time.perf_counter() - t0, 3),
        "warnings": warnings,
        "assumptions": make_assumptions(),
        "metrics": metrics,
        "equity": equity,
        "monthly": monthly,
        "trades": trades,
        "by_stock": by_stock_summary(trades),
        "signals": sig.items,
    }


# ======================================================================================
# 내부 구현
# ======================================================================================


def _same_day_allowed(mode: str, rule: Mapping) -> bool:
    """진입 체결 당일에 이 청산 규칙을 평가해도 되는가.

    * ``always``    — 전부 허용 (낙관적)
    * ``never``     — 전부 차단 (가장 보수적)
    * ``loss_only`` — ``type: "take_profit"`` 만 차단. type 이 없는 커스텀 규칙은
      손실 방향으로 간주해 허용한다.
    """
    if mode == "always":
        return True
    if mode == "never":
        return False
    return str(rule.get("type") or "") not in _PROFIT_EXIT_TYPES


_FILL_MODEL_NOTES = {
    "touch": "체결가는 목표가 그대로 잡되, 갭으로 목표가를 지나쳐 시작한 날은 당일 시가로 체결했습니다.",
    "next_open": "조건이 성립한 다음 거래일 시가로 체결했습니다.",
    "close": "조건이 성립한 당일 종가로 체결했습니다.",
}

_SAME_DAY_NOTES = {
    "loss_only": (
        "진입 체결이 있었던 날에는 손절 계열만 평가하고 익절은 다음 거래일부터 평가했습니다 "
        "(same_day_exit=loss_only). 같은 봉에서 매수가와 익절가가 모두 닿았더라도 "
        "익절이 먼저였다고 단정할 수 없기 때문입니다."
    ),
    "never": (
        "진입 체결이 있었던 날에는 어떤 청산도 평가하지 않고 다음 거래일부터 판정했습니다 "
        "(same_day_exit=never). 세 가지 설정 중 가장 보수적입니다."
    ),
    "always": (
        "진입 체결이 있었던 날에도 익절·손절을 모두 평가했습니다 (same_day_exit=always). "
        "같은 봉에서 매수가와 익절가가 모두 닿으면 매수가 먼저였다고 가정하므로 "
        "결과가 실제보다 좋게 나올 수 있습니다."
    ),
}


def _assumption_notes(requested_resolution: str, fill_model: str, same_day_exit: str,
                      slippage: float, fee: float, ordered_exits: Sequence[Mapping],
                      stats: Mapping) -> List[str]:
    """사용자에게 그대로 보여줄 실행 가정 문장들."""
    notes = [
        "일봉 데이터만 사용했습니다. 하루 안에서 저가와 고가 중 무엇이 먼저였는지는 알 수 없습니다.",
    ]
    if requested_resolution != "1d":
        notes.append(
            f"전략은 {requested_resolution} 해상도 체결을 요청했지만 marcap 은 일봉만 제공합니다. "
            "분봉 매매는 추후 지원 예정이며, 그 전까지는 일봉 근사로 동작합니다."
        )
    notes.append(_FILL_MODEL_NOTES.get(fill_model, f"fill_model={fill_model} 로 체결했습니다."))
    notes.append(_SAME_DAY_NOTES.get(same_day_exit, ""))
    notes.append(
        f"매수는 체결가 +{slippage * 100:g}%, 매도는 -{slippage * 100:g}% 슬리피지를 적용하고 "
        f"매도 대금에 {fee * 100:g}% 비용(수수료+거래세)을 부과했습니다."
    )
    ids = [r.get("id") for r in ordered_exits]
    if len(ids) > 1:
        notes.append(
            f"같은 봉에서 여러 청산이 동시에 성립하면 {' → '.join(str(i) for i in ids)} 순서로 평가했습니다."
        )
    amb = int(stats.get("ambiguous_bars") or 0)
    if amb:
        notes.append(
            f"진입과 청산 조건이 같은 봉 안에서 모두 성립한 경우가 {amb:,}건 있었습니다. "
            "이 봉들은 순서를 확정할 수 없어 위 가정에 의존합니다. 건수가 많을수록 결과 신뢰도는 낮습니다."
        )
    notes.append("미청산 포지션은 백테스트 종료일 종가로 강제 청산했습니다 (exit_reason=기간종료).")
    return [n for n in notes if n]


def _order_exits(exits: Sequence[Mapping], priority: Sequence[str]) -> List[Mapping]:
    by_id = {r.get("id"): r for r in exits}
    ordered = [by_id[i] for i in priority if i in by_id]
    ordered += [r for r in exits if r.get("id") not in set(priority)]
    return ordered


def _new_watch(code: str, ref_date: dt.date, ref_li: int, ref_gi: int, refbar: dict) -> dict:
    return {
        "code": code,
        "ref_date": ref_date,
        "ref_li": ref_li,
        "ref_gi": ref_gi,
        "ref": refbar,
        "fills": {},
        "fill_log": [],
        "qty": 0,
        "cost": 0.0,
        "planned": None,
        "entry_date": None,
        "last_entry_date": None,
        "peak": None,
    }


def _bar(rec: dict, li: int) -> Dict[str, float]:
    return {
        "open": float(rec["open"][li]),
        "high": float(rec["high"][li]),
        "low": float(rec["low"][li]),
        "close": float(rec["close"][li]),
        "volume": float(rec["volume"][li]),
        "amount": float(rec["amount"][li]),
    }


def _next_day(rec: dict, li: int) -> dt.date:
    idx = rec["df"].index
    j = min(li + 1, len(idx) - 1)
    return idx[j].date()


def _last_index_upto(rec: dict, day: dt.date) -> Optional[int]:
    li = rec["pos"].get(day)
    if li is not None:
        return li
    idx = rec["df"].index
    pos = idx.searchsorted(pd.Timestamp(day), side="right") - 1
    return int(pos) if pos >= 0 else None


def _ctx(rec: dict, w: dict, bar: Optional[dict] = None, day: Optional[dt.date] = None) -> EvalContext:
    """종목별로 하나의 EvalContext 를 재사용한다 (봉/지표 캐시 유지)."""
    ctx = rec["ctx"]
    if ctx is None:
        ctx = EvalContext(rec["df"], indicator_cache=rec["ind"])
        rec["ctx"] = ctx
    ctx.ref = w["ref"]
    ctx.fills = w["fills"]
    ctx.position = _position(w, bar, day)
    return ctx


def _position(w: dict, bar: Optional[dict], day: Optional[dt.date]) -> dict:
    if w["qty"] <= 0:
        return {}
    avg = w["cost"] / w["qty"]
    pos = {
        "avg_price": avg,
        "qty": w["qty"],
        "cost": w["cost"],
        "entry_price": next(iter(w["fills"].values()))["price"] if w["fills"] else avg,
        "peak_price": w["peak"] if w["peak"] is not None else avg,
        "hold_days": (day - w["entry_date"]).days if (day and w["entry_date"]) else 0,
    }
    if bar is not None:
        pos["pnl_pct"] = (bar["close"] / avg - 1.0) * 100.0
        pos["pnl"] = (bar["close"] - avg) * w["qty"]
    else:
        pos["pnl_pct"] = 0.0
        pos["pnl"] = 0.0
    return pos


def _holding(active: Mapping[str, dict], code: str) -> bool:
    w = active.get(code)
    return bool(w and w["qty"] > 0)


def _universe_gate(condition: str, pullback_pct, bar: dict, ref: dict, fill_model: str) -> bool:
    """``universe.condition`` — 기준일 이후 감시 조건."""
    if condition in ("none", "", None):
        return True
    probe = bar["low"] if fill_model == "touch" else bar["close"]
    if condition == "close_below_reference_open":
        return probe <= ref.get("open", math.inf)
    if condition == "close_below_reference_low":
        return probe <= ref.get("low", math.inf)
    if condition == "pullback_pct":
        if pullback_pct is None:
            return True
        return probe <= ref.get("close", math.inf) * (1.0 + float(pullback_pct) / 100.0)
    return True


def _planned_position(sizing: str, portfolio: Mapping, equity: float, max_positions: int) -> float:
    """종목당 배정 자본 (ARCHITECTURE 2장 '분할 매수')."""
    if sizing == "fixed_amount":
        return float(portfolio.get("amount_manwon") or 0) * 10_000.0
    if sizing == "fixed_qty":
        return 0.0  # 수량 고정 — 금액 배정 사용 안 함
    return equity / max(int(max_positions), 1)


def _entry_qty(rule: Mapping, sizing: str, portfolio: Mapping, planned: float,
               equity: float, price: float, cash: float) -> int:
    size_pct = float(rule.get("size_pct", 100.0)) / 100.0
    if sizing == "fixed_qty":
        qty = int(float(portfolio.get("qty") or 0) * size_pct)
    else:
        size_of = str(rule.get("size_of") or "planned_position")
        base = equity if size_of == "equity" else planned
        qty = int((base * size_pct) // price)
    affordable = int(cash // price)
    return max(min(qty, affordable), 0)


def _exit_qty(rule: Mapping, held: int) -> int:
    pct = float(rule.get("size_pct", 100.0)) / 100.0
    if pct >= 1.0:
        return held
    return min(held, int(held * pct))


def _exit_hit(rule: Mapping, ctx: EvalContext, li: int, bar: dict, w: dict,
              day: dt.date) -> Tuple[bool, Optional[float]]:
    """청산 조건 판정. ``when`` 이 없으면 type 별 기본 동작으로 대체한다."""
    when = rule.get("when")
    t = str(rule.get("type") or "")
    avg = w["cost"] / w["qty"] if w["qty"] else 0.0

    if when is None:
        if t == "trailing_stop":
            trail = float(rule.get("trail_pct") or 0.0)
            target = (w["peak"] or bar["high"]) * (1.0 - trail / 100.0)
            if bar["low"] <= target:
                return True, min(max(target, bar["low"]), bar["high"]) if bar["open"] > target else bar["open"]
            return False, None
        if t == "time_exit":
            hold = (day - w["entry_date"]).days if w["entry_date"] else 0
            if hold >= int(rule.get("max_hold_days") or 0):
                return True, bar["close"]
            return False, None
        return False, None

    try:
        return bool(evaluate_condition(when, ctx, li)), None
    except DSLError:
        return False, None


def _close(w: dict, rec: dict, code: str, rule: Mapping, price: float, qty: int,
           exit_date: dt.date, slippage: float, fee: float, no: int,
           reason: str) -> Tuple[dict, float]:
    """포지션(또는 일부)을 청산하고 trade 레코드를 만든다."""
    sell_px = price * (1.0 - slippage)
    gross = sell_px * qty
    fees = gross * fee
    proceeds = gross - fees

    avg_paid = w["cost"] / w["qty"]
    cost_basis = avg_paid * qty
    pnl = proceeds - cost_basis
    ret_pct = (pnl / cost_basis * 100.0) if cost_basis else 0.0
    hold_days = (exit_date - w["entry_date"]).days if w["entry_date"] else 0

    w["qty"] -= qty
    w["cost"] -= cost_basis

    trade = {
        "no": no,
        "code": code,
        "name": rec["name"],
        "ref_date": w["ref_date"].isoformat(),
        "ref_amount_eok": _i(w["ref"].get("amount", 0.0) / EOK),
        "ref_open": _r(w["ref"].get("open"), 2),
        "fills": list(w["fill_log"]),
        "avg_price": _r(avg_paid, 2),
        "exit_date": exit_date.isoformat(),
        "exit_price": _r(sell_px, 2),
        "exit_rule": rule.get("id"),
        "exit_reason": reason,
        "hold_days": int(hold_days),
        "return_pct": _r(ret_pct),
        "pnl": _i(pnl),
    }
    return trade, proceeds


def _mtm(active: Mapping[str, dict], recs: Mapping[str, dict], day: dt.date) -> float:
    """보유 포지션 평가액."""
    total = 0.0
    for code, w in active.items():
        if w["qty"] <= 0:
            continue
        rec = recs[code]
        li = _last_index_upto(rec, day)
        if li is None:
            total += w["cost"]
        else:
            total += w["qty"] * float(rec["close"][li])
    return total


def _empty_result(run_id, t0, warnings, sig, calendar, initial_capital, start, end,
                  note, assumptions) -> dict:
    warnings = list(warnings) + [note]
    values = [initial_capital] * len(calendar)
    sig.add(calendar[-1] if calendar else None, "DONE", None, note, "15:30:00")
    return {
        "run_id": run_id,
        "elapsed_sec": _r(time.perf_counter() - t0, 3),
        "warnings": warnings,
        "assumptions": assumptions,
        "metrics": compute_metrics(calendar, values, [], initial_capital, start, end),
        "equity": build_equity(calendar, values, initial_capital),
        "monthly": monthly_returns(calendar, values),
        "trades": [],
        "by_stock": [],
        "signals": sig.items,
    }
