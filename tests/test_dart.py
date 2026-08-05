"""DART 연동 테스트 — 실제 API 호출 없이 fixture 로만 검증한다.

이 환경에서는 ``opendart.fss.or.kr`` 이 막혀 있어 실제 호출로 검증할 수 없다.
그래서 HTTP 계층을 통째로 대체(``fetch=``)하고, 응답 본문만 가짜로 만든다.

특히 다음 두 가지는 **틀리면 백테스트 결과가 뒤집히는** 항목이라 촘촘히 본다.

1. as-of 조회 (룩어헤드 편향 차단)
2. 누적 손익 → 분기 손익 차분
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.dart import DartStore  # noqa: E402
from engine.errors import DataUnavailable  # noqa: E402
from tools import fetch_dart as fd  # noqa: E402

EOK = 100_000_000

#: 코디네이터가 정한 "오늘"
TODAY = dt.date(2026, 8, 5)


# ======================================================================================
# fixture 빌더
# ======================================================================================


def corp_code_zip(entries) -> bytes:
    """``[(corp_code, corp_name, stock_code, modify_date), ...]`` → corpCode.xml ZIP bytes."""
    parts = ["<?xml version='1.0' encoding='UTF-8'?>", "<result>"]
    for corp, name, stock, modify in entries:
        parts.append(
            "<list>"
            f"<corp_code>{corp}</corp_code>"
            f"<corp_name>{name}</corp_name>"
            f"<stock_code>{stock}</stock_code>"
            f"<modify_date>{modify}</modify_date>"
            "</list>"
        )
    parts.append("</result>")
    xml = "".join(parts).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


def acct(account_nm, amount, *, corp, stock, year, quarter, rcept, fs="CFS", sj="BS"):
    """``fnlttMultiAcnt`` list 항목 1건."""
    return {
        "rcept_no": rcept,
        "bsns_year": str(year),
        "corp_code": corp,
        "stock_code": stock,
        "reprt_code": fd.REPRT_CODE_BY_QUARTER[quarter],
        "account_nm": account_nm,
        "fs_div": fs,
        "fs_nm": "연결재무제표" if fs == "CFS" else "재무제표",
        "sj_div": sj,
        "sj_nm": "재무상태표" if sj == "BS" else "손익계산서",
        "thstrm_nm": f"제 {year} 기",
        "thstrm_amount": f"{amount:,}" if isinstance(amount, int) else str(amount),
        "frmtrm_amount": "",
        "ord": "1",
        "currency": "KRW",
    }


def company_items(
    corp, stock, year, quarter, rcept,
    *, fs="CFS",
    current_assets=200 * EOK, current_liab=100 * EOK,
    total_assets=500 * EOK, total_liab=150 * EOK, total_equity=350 * EOK,
    revenue=300 * EOK, op_income=30 * EOK, net_income=25 * EOK,
):
    """한 회사·한 보고서의 주요계정 전체."""
    kw = dict(corp=corp, stock=stock, year=year, quarter=quarter, rcept=rcept, fs=fs)
    return [
        acct("유동자산", current_assets, sj="BS", **kw),
        acct("유동부채", current_liab, sj="BS", **kw),
        acct("자산총계", total_assets, sj="BS", **kw),
        acct("부채총계", total_liab, sj="BS", **kw),
        acct("자본총계", total_equity, sj="BS", **kw),
        acct("매출액", revenue, sj="IS", **kw),
        acct("영업이익", op_income, sj="IS", **kw),
        acct("당기순이익", net_income, sj="IS", **kw),
    ]


class FakeDart:
    """HTTP 계층 대역. ``fetch=`` 로 넣어 쓴다."""

    def __init__(
        self,
        corps,
        *,
        max_batch=None,
        rate_limit_after=None,
        no_data_quarters=(),
        maintenance_after=None,
        item_builder=None,
    ):
        #: ``[(corp_code, corp_name, stock_code), ...]``
        self.corps = list(corps)
        self.zip_blob = corp_code_zip(
            [(c, n, s, "20260101") for c, n, s in self.corps]
        )
        self.stock_of = {c: s for c, n, s in self.corps}
        self.max_batch = max_batch
        self.rate_limit_after = rate_limit_after
        self.no_data_quarters = set(no_data_quarters)
        self.maintenance_after = maintenance_after
        self.item_builder = item_builder
        self.calls = []           # 전체 호출
        self.acnt_calls = []      # fnlttMultiAcnt 호출만

    # -- 호출 -----------------------------------------------------------------------
    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if url == fd.CORP_CODE_URL:
            return self.zip_blob
        if url != fd.MULTI_ACNT_URL:  # pragma: no cover
            raise AssertionError(f"예상 못한 URL: {url}")

        self.acnt_calls.append(dict(params))
        n = len(self.acnt_calls)
        if self.rate_limit_after is not None and n > self.rate_limit_after:
            return self._json("020", "요청 제한을 초과하였습니다.")
        if self.maintenance_after is not None and n > self.maintenance_after:
            return self._json("800", "시스템 점검중입니다.")

        corp_codes = [c for c in params["corp_code"].split(",") if c]
        if self.max_batch is not None and len(corp_codes) > self.max_batch:
            return self._json("100", "부적절한 필드입니다.")

        year = int(params["bsns_year"])
        quarter = fd.QUARTER_BY_REPRT_CODE[params["reprt_code"]]
        if (year, quarter) in self.no_data_quarters:
            return self._json("013", "조회된 데이타가 없습니다.")

        items = []
        for corp in corp_codes:
            stock = self.stock_of.get(corp, "")
            if self.item_builder is not None:
                items += list(self.item_builder(corp, stock, year, quarter) or [])
            else:
                items += company_items(
                    corp, stock, year, quarter,
                    rcept=f"{year + (1 if quarter == 4 else 0)}0515000001",
                )
        if not items:
            return self._json("013", "조회된 데이타가 없습니다.")
        return self._json("000", "정상", items)

    @staticmethod
    def _json(status, message, items=None):
        payload = {"status": status, "message": message}
        if items is not None:
            payload["list"] = items
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_corps(n, start=1):
    """``[(corp_code, name, stock_code), ...]`` n 개."""
    return [
        (f"{start + i:08d}", f"테스트{start + i}", f"{100000 + start + i:06d}")
        for i in range(n)
    ]


def write_store(root: Path, rows) -> DartStore:
    """행 dict 리스트 → ``dart/fundamentals-YYYY.parquet`` 를 만들고 스토어를 돌려준다."""
    root.mkdir(parents=True, exist_ok=True)
    df = fd.add_quarter_columns(fd.to_frame(rows))
    for year, part in df.groupby(df["year"].astype("int64")):
        part.reset_index(drop=True).to_parquet(
            root / f"fundamentals-{int(year)}.parquet", index=False
        )
    return DartStore(root=root)


def row(code, year, quarter, disclosed, op_income, **kw):
    """저장 규격 행 1건 (금액 기본값은 부채비율/유동비율이 통과하도록)."""
    base = {
        "code": code,
        "corp_code": kw.get("corp_code", "00000001"),
        "year": year,
        "quarter": quarter,
        "disclosed_at": disclosed,
        "fs_div": kw.get("fs_div", "CFS"),
        "currency": "KRW",
        "유동자산": kw.get("유동자산", 200 * EOK),
        "유동부채": kw.get("유동부채", 100 * EOK),
        "자산총계": kw.get("자산총계", 500 * EOK),
        "부채총계": kw.get("부채총계", 150 * EOK),
        "자본총계": kw.get("자본총계", 350 * EOK),
        "매출액": kw.get("매출액", 300 * EOK),
        "영업이익": op_income,           # 누적값 (add_quarter_columns 가 차분한다)
        "당기순이익": kw.get("당기순이익", 25 * EOK),
        "debt_ratio_pct": None,
        "current_ratio_pct": None,
        "op_income_quarter": None,
    }
    return base


# ======================================================================================
# 1. corpCode ZIP 파싱
# ======================================================================================


def test_corp_code_zip_비상장은_버린다():
    blob = corp_code_zip(
        [
            ("00126380", "삼성전자", "005930", "20240101"),
            ("00164779", "SK하이닉스", "000660", "20240101"),
            ("00999999", "비상장회사", "", "20240101"),
            ("00888888", "공백코드회사", "      ", "20240101"),
        ]
    )
    df = fd.parse_corp_code_zip(blob)
    assert list(df["code"]) == ["000660", "005930"]
    assert set(df["corp_code"]) == {"00126380", "00164779"}
    assert "비상장회사" not in set(df["corp_name"])


def test_corp_code_zip_같은_종목코드는_최신만_남긴다():
    blob = corp_code_zip(
        [
            ("00000001", "옛법인", "005930", "20200101"),
            ("00126380", "삼성전자", "005930", "20240101"),
        ]
    )
    df = fd.parse_corp_code_zip(blob)
    assert len(df) == 1
    assert df.iloc[0]["corp_code"] == "00126380"


def test_corp_code_zip이_아니면_status를_읽어_예외로():
    xml = b"<result><status>010</status><message>\xeb\x93\xb1\xeb\xa1\x9d</message></result>"
    with pytest.raises(fd.AuthError):
        fd.parse_corp_code_zip(xml)


# ======================================================================================
# 2. fnlttMultiAcnt 파싱 · CFS 우선 / OFS 폴백
# ======================================================================================


def test_multi_acnt_정상_파싱():
    items = company_items("00126380", "005930", 2024, 4, "20250311000123")
    payload = {"status": "000", "message": "정상", "list": items}
    parsed = fd.parse_multi_acnt(json.dumps(payload).encode("utf-8"))
    assert len(parsed) == 8

    rows = fd.rows_from_items(parsed, 2024, 4)
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "005930"
    assert r["corp_code"] == "00126380"
    assert r["fs_div"] == "CFS"
    assert r["disclosed_at"] == "2025-03-11"   # rcept_no 앞 8자리
    assert r["부채총계"] == 150 * EOK
    assert r["영업이익"] == 30 * EOK


def test_CFS가_있으면_CFS를_쓴다():
    items = (
        company_items("00126380", "005930", 2024, 4, "20250311000123",
                      fs="OFS", total_liab=999 * EOK)
        + company_items("00126380", "005930", 2024, 4, "20250311000123",
                        fs="CFS", total_liab=150 * EOK)
    )
    rows = fd.rows_from_items(items, 2024, 4)
    assert len(rows) == 1
    assert rows[0]["fs_div"] == "CFS"
    assert rows[0]["부채총계"] == 150 * EOK


def test_CFS가_없으면_OFS로_폴백():
    items = company_items("00126380", "005930", 2024, 4, "20250311000123",
                          fs="OFS", total_liab=777 * EOK)
    rows = fd.rows_from_items(items, 2024, 4)
    assert rows[0]["fs_div"] == "OFS"
    assert rows[0]["부채총계"] == 777 * EOK


def test_종목코드가_없으면_corp_map으로_붙인다():
    items = company_items("00126380", "", 2024, 4, "20250311000123")
    assert fd.rows_from_items(items, 2024, 4) == []   # 붙일 수 없으면 버린다
    rows = fd.rows_from_items(items, 2024, 4, corp_to_code={"00126380": "005930"})
    assert rows[0]["code"] == "005930"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,234,567", 1234567),
        ("-1,234", -1234),
        ("△500", -500),
        ("(2,000)", -2000),
        ("", None),
        ("-", None),
        (None, None),
        ("0", 0),
    ],
)
def test_금액_문자열_파싱(raw, expected):
    assert fd.parse_amount(raw) == expected


def test_계정명_변형_정규화():
    assert fd.normalize_account("영업이익(손실)") == "영업이익"
    assert fd.normalize_account("당기순이익(손실)") == "당기순이익"
    assert fd.normalize_account("수익(매출액)") == "매출액"
    assert fd.normalize_account("법인세차감전 순이익") is None


# ======================================================================================
# 3. status 013 은 오류가 아니다 / 020 은 중단
# ======================================================================================


def test_status_013은_빈리스트():
    payload = {"status": "013", "message": "조회된 데이타가 없습니다."}
    assert fd.parse_multi_acnt(json.dumps(payload).encode("utf-8")) == []


def test_status_013은_수집을_멈추지_않는다(tmp_path):
    corps = make_corps(4)
    fake = FakeDart(corps, no_data_quarters={(2024, 1), (2024, 2)})
    rc = fd.run_fetch(
        "KEY", 2024, 2024, out_dir=tmp_path / "dart", batch=2,
        fetch=fake, today=TODAY, refresh_recent=0,
    )
    assert rc == 0
    store = DartStore(root=tmp_path / "dart")
    got = store.load()
    # 1분기·반기는 013 이라 비고, 3분기·사업보고서만 들어온다
    assert set(zip(got["year"].astype(int), got["quarter"].astype(int))) == {(2024, 3), (2024, 4)}


def test_status_020은_중단하고_상태를_저장한다(tmp_path):
    out = tmp_path / "dart"
    corps = make_corps(4)
    # corpCode 1회 + fnlttMultiAcnt 2회까지만 성공 (배치 2 → 한 분기당 2호출)
    fake = FakeDart(corps, rate_limit_after=2)
    rc = fd.run_fetch(
        "KEY", 2024, 2024, out_dir=out, batch=2, fetch=fake,
        today=TODAY, refresh_recent=0,
    )
    assert rc == 6, "020 이면 종료 코드 6 으로 중단해야 한다"

    state = json.loads((out / fd.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["quarters"]["2024-1"]["complete"] is True   # 첫 분기는 끝났다
    assert "2024-2" not in state["quarters"]                 # 두 번째에서 끊겼다
    assert state["api_calls_date"] == TODAY.isoformat()

    # 다시 실행하면 끝난 분기는 건너뛰고 2분기부터 이어받는다
    fake2 = FakeDart(corps)
    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=2, fetch=fake2,
                 today=TODAY, refresh_recent=0)
    years = {int(p["bsns_year"]) for p in fake2.acnt_calls}
    quarters = {fd.QUARTER_BY_REPRT_CODE[p["reprt_code"]] for p in fake2.acnt_calls}
    assert years == {2024}
    assert 1 not in quarters, "이미 받은 2024-1 을 다시 요청하면 안 된다"
    assert quarters == {2, 3, 4}


def test_status_800은_점검중으로_중단(tmp_path):
    fake = FakeDart(make_corps(2), maintenance_after=0)
    rc = fd.run_fetch("KEY", 2024, 2024, out_dir=tmp_path / "dart", batch=2,
                      fetch=fake, today=TODAY, refresh_recent=0)
    assert rc == 7


# ======================================================================================
# 4. 배치 크기 적응
# ======================================================================================


def test_status_100이면_배치를_절반으로_줄인다():
    corps = make_corps(100)
    fake = FakeDart(corps, max_batch=25)
    items, final_batch, calls = fd.fetch_quarter(
        "KEY", [c for c, _n, _s in corps], 2024, 4, batch=100, fetch=fake
    )
    assert final_batch == 25, "100 → 50 → 25 로 두 번 줄여야 한다"
    rows = fd.rows_from_items(items, 2024, 4)
    assert len(rows) == 100, "줄인 뒤에도 전 종목을 다 받아야 한다"
    # 거부된 2회(100개, 50개) + 성공 4회(25개씩)
    assert calls == 6


def test_배치_1에서도_100이_오면_예외():
    fake = FakeDart(make_corps(2), max_batch=0)
    with pytest.raises(fd.BadFieldError):
        fd.fetch_quarter("KEY", ["00000001"], 2024, 4, batch=1, fetch=fake)


# ======================================================================================
# 5. 분기 차분 (누적 → 분기)
# ======================================================================================


def test_누적을_분기로_차분한다():
    cum = {1: 100, 2: 250, 3: 420, 4: 600}
    assert fd.quarterly_from_cumulative(cum) == {1: 100, 2: 150, 3: 170, 4: 180}


def test_중간분기가_비면_None():
    cum = {1: 100, 2: None, 3: 420, 4: 600}
    got = fd.quarterly_from_cumulative(cum)
    assert got[1] == 100
    assert got[2] is None
    assert got[3] is None, "직전(반기) 누적을 모르면 3분기도 모른다"
    assert got[4] == 180


def test_1분기가_비면_2분기도_모른다():
    got = fd.quarterly_from_cumulative({2: 250, 3: 420, 4: 600})
    assert got[1] is None
    assert got[2] is None
    assert got[3] == 170


def test_연결별도가_섞이면_차분하지_않는다():
    cum = {1: 100, 2: 250}
    fs = {1: "OFS", 2: "CFS"}
    assert fd.quarterly_from_cumulative(cum, fs)[2] is None


def test_add_quarter_columns가_파생값을_채운다():
    rows = [
        row("005930", 2024, q, f"2024-{q:02d}-15", op)
        for q, op in ((1, 100 * EOK), (2, 250 * EOK), (3, 420 * EOK), (4, 600 * EOK))
    ]
    df = fd.add_quarter_columns(fd.to_frame(rows))
    assert list(df["op_income_quarter"]) == [100 * EOK, 150 * EOK, 170 * EOK, 180 * EOK]
    # 부채 150 / 자본 350 = 42.86%, 유동 200 / 100 = 200%
    assert df["debt_ratio_pct"].iloc[0] == pytest.approx(42.86)
    assert df["current_ratio_pct"].iloc[0] == pytest.approx(200.0)


def test_자본잠식이면_부채비율은_계산불가():
    df = fd.add_quarter_columns(
        fd.to_frame([row("005930", 2024, 4, "2025-03-11", 10 * EOK, 자본총계=-5 * EOK)])
    )
    assert pd.isna(df["debt_ratio_pct"].iloc[0]), "자본잠식은 임의로 통과시키지 않는다"


# ======================================================================================
# 6. as-of 조회 (룩어헤드 편향 차단) — 가장 중요
# ======================================================================================


@pytest.fixture
def asof_store(tmp_path) -> DartStore:
    """2024Q4 는 2025-03-11, 2025Q1 은 2025-05-15 에 공시됐다."""
    rows = [
        row("005930", 2024, 4, "2025-03-11", 600 * EOK),
        row("005930", 2025, 1, "2025-05-15", 120 * EOK),
    ]
    return write_store(tmp_path / "dart", rows)


def test_공시_전날에는_직전_분기를_준다(asof_store):
    got = asof_store.as_of("005930", "2025-05-14")
    assert got is not None
    assert (got["year"], got["quarter"]) == (2024, 4)
    assert got["disclosed_at"] == "2025-03-11"


def test_공시_당일부터_새_분기를_준다(asof_store):
    got = asof_store.as_of("005930", "2025-05-15")
    assert (got["year"], got["quarter"]) == (2025, 1)


def test_첫_공시_전에는_None(asof_store):
    assert asof_store.as_of("005930", "2025-03-10") is None


def test_as_of는_4월에_1분기를_주지_않는다(asof_store):
    """분기 종료(3/31) 직후인 2025-04-01 에 2025Q1 을 주면 룩어헤드다."""
    got = asof_store.as_of("005930", "2025-04-01")
    assert (got["year"], got["quarter"]) == (2024, 4)


def test_as_of_panel도_같은_규칙(tmp_path):
    rows = [
        row("005930", 2024, 4, "2025-03-11", 600 * EOK),
        row("005930", 2025, 1, "2025-05-15", 120 * EOK),
        row("000660", 2024, 4, "2025-03-20", 400 * EOK),
        row("000660", 2025, 1, "2025-05-13", 90 * EOK),
    ]
    store = write_store(tmp_path / "dart", rows)

    panel = store.as_of_panel(["005930", "000660"], "2025-05-14")
    assert list(panel.index) == ["000660", "005930"]
    assert int(panel.loc["000660", "quarter"]) == 1     # 5/13 공시 → 이미 공개
    assert int(panel.loc["005930", "quarter"]) == 4     # 5/15 공시 → 아직 비공개
    assert int(panel.loc["005930", "year"]) == 2024

    # 아무것도 공시되지 않은 시점이면 빈 프레임 (예외 아님)
    assert store.as_of_panel(["005930"], "2020-01-01").empty


def test_정정공시가_있으면_나중_것을_쓴다(tmp_path):
    rows = [
        dict(row("005930", 2024, 4, "2025-03-11", 600 * EOK), 부채총계=150 * EOK),
        dict(row("005930", 2024, 4, "2025-06-20", 600 * EOK), 부채총계=999 * EOK),
    ]
    store = write_store(tmp_path / "dart", rows)
    assert store.as_of("005930", "2025-05-01")["부채총계"] == 150 * EOK
    assert store.as_of("005930", "2025-07-01")["부채총계"] == 999 * EOK


# ======================================================================================
# 7. consecutive_profit_quarters
# ======================================================================================


def _quarter_rows(code, year, cumulative, disclosed):
    """``cumulative`` 는 ``{분기: 누적 영업이익}``, ``disclosed`` 는 ``{분기: 공시일}``."""
    return [
        row(code, year, q, disclosed[q], cumulative[q])
        for q in sorted(cumulative)
        if cumulative[q] is not None
    ]


DISCLOSED_2024 = {1: "2024-05-15", 2: "2024-08-14", 3: "2024-11-14", 4: "2025-03-11"}
DISCLOSED_2025 = {1: "2025-05-15", 2: "2025-08-14", 3: "2025-11-14", 4: "2026-03-11"}


def test_4개분기_연속흑자를_센다(tmp_path):
    rows = _quarter_rows(
        "005930", 2024, {1: 100 * EOK, 2: 250 * EOK, 3: 420 * EOK, 4: 600 * EOK},
        DISCLOSED_2024,
    )
    store = write_store(tmp_path / "dart", rows)
    assert store.consecutive_profit_quarters("005930", "2025-03-11") == 4


def test_중간에_적자가_있으면_거기서_끊긴다(tmp_path):
    # Q3 분기값 = 250 - 300 = -50 (적자)
    rows = _quarter_rows(
        "005930", 2024, {1: 100 * EOK, 2: 300 * EOK, 3: 250 * EOK, 4: 400 * EOK},
        DISCLOSED_2024,
    )
    store = write_store(tmp_path / "dart", rows)
    assert store.consecutive_profit_quarters("005930", "2025-03-11") == 1


def test_결측을_흑자로_오인하지_않는다(tmp_path):
    """반기보고서를 안 낸 회사. Q2·Q3 분기값을 모르므로 그 앞은 세지 않는다."""
    rows = _quarter_rows(
        "005930", 2024, {1: 100 * EOK, 2: None, 3: 420 * EOK, 4: 600 * EOK},
        DISCLOSED_2024,
    )
    store = write_store(tmp_path / "dart", rows)
    n = store.consecutive_profit_quarters("005930", "2025-03-11")
    assert n == 1, "Q4 는 흑자지만 Q3 를 모르므로 거기서 멈춰야 한다"
    assert n < 4, "결측을 흑자로 세면 4가 나온다 — 그러면 안 된다"


def test_가장_최근_분기를_모르면_None(tmp_path):
    """1분기 누적이 없어 Q2 분기값을 모르는데, Q2 가 가장 최근 공시인 경우."""
    rows = [row("005930", 2024, 2, "2024-08-14", 250 * EOK)]
    store = write_store(tmp_path / "dart", rows)
    assert store.consecutive_profit_quarters("005930", "2024-09-01") is None


def test_데이터가_없는_종목은_None(tmp_path):
    store = write_store(tmp_path / "dart", [row("005930", 2024, 4, "2025-03-11", 600 * EOK)])
    assert store.consecutive_profit_quarters("000660", "2025-03-11") is None


def test_연속흑자도_as_of를_지킨다(tmp_path):
    """2025Q1 공시 전에는 2025Q1 흑자를 세면 안 된다."""
    rows = (
        _quarter_rows("005930", 2024,
                      {1: 100 * EOK, 2: 250 * EOK, 3: 420 * EOK, 4: 600 * EOK},
                      DISCLOSED_2024)
        + _quarter_rows("005930", 2025, {1: 130 * EOK}, DISCLOSED_2025)
    )
    store = write_store(tmp_path / "dart", rows)
    assert store.consecutive_profit_quarters("005930", "2025-05-14") == 4   # 2024Q4 까지
    assert store.consecutive_profit_quarters("005930", "2025-05-15") == 5   # 2025Q1 포함


def test_연속흑자는_연도를_넘어_이어진다(tmp_path):
    rows = (
        _quarter_rows("005930", 2024,
                      {1: 100 * EOK, 2: 250 * EOK, 3: 420 * EOK, 4: 600 * EOK},
                      DISCLOSED_2024)
        + _quarter_rows("005930", 2025,
                        {1: 130 * EOK, 2: 280 * EOK}, DISCLOSED_2025)
    )
    store = write_store(tmp_path / "dart", rows)
    assert store.consecutive_profit_quarters("005930", "2025-08-14") == 6


# ======================================================================================
# 8. DartStore — 데이터 없이도 살아 있어야 한다
# ======================================================================================


def test_데이터_없어도_import되고_available은_False(tmp_path):
    store = DartStore(root=tmp_path / "없는폴더")
    assert store.available is False
    assert store.status() == {
        "available": False, "last_fetch": None, "year_range": None,
        "row_count": 0, "codes": 0,
    }


def test_데이터_없으면_조회는_DataUnavailable(tmp_path):
    store = DartStore(root=tmp_path / "없는폴더")
    with pytest.raises(DataUnavailable):
        store.as_of("005930", "2025-01-02")
    with pytest.raises(DataUnavailable):
        store.as_of_panel(["005930"], "2025-01-02")
    with pytest.raises(DataUnavailable):
        store.consecutive_profit_quarters("005930", "2025-01-02")


def test_status_페이로드(tmp_path):
    rows = [
        row("005930", 2024, 4, "2025-03-11", 600 * EOK),
        row("000660", 2025, 1, "2025-05-15", 120 * EOK),
    ]
    store = write_store(tmp_path / "dart", rows)
    st = store.status()
    assert st["available"] is True
    assert st["year_range"] == [2024, 2025]
    assert st["row_count"] == 2
    assert st["codes"] == 2
    assert st["last_fetch"] is not None


# ======================================================================================
# 9. 이미 받은 자료는 다시 받지 않는다
# ======================================================================================


def test_45일이_안_지난_분기는_요청하지_않는다(tmp_path):
    """오늘 2026-08-05. 2026 반기(6/30 종료)의 법정 기한은 8/14 이므로 아직 대상이 아니다."""
    state = fd.FetchState(tmp_path / fd.STATE_FILENAME, today=TODAY)
    plan = fd.plan_quarters(2026, 2026, state, today=TODAY, refresh_recent=0)
    by_q = {p["quarter"]: p for p in plan}

    assert by_q[1]["action"] == "fetch"                      # 3/31 + 45 = 5/15 → 지남
    assert by_q[2]["action"] == "skip"                       # 6/30 + 45 = 8/14 → 아직
    assert by_q[2]["reason"] == fd.PLAN_NOT_DUE
    assert by_q[2]["due"] == dt.date(2026, 8, 14)
    assert by_q[3]["action"] == "skip"                       # 분기가 끝나지도 않았다
    assert by_q[4]["action"] == "skip"

    # 8/14 이 되면 반기가 대상에 들어온다
    later = fd.plan_quarters(2026, 2026, state, today=dt.date(2026, 8, 14), refresh_recent=0)
    assert {p["quarter"]: p["action"] for p in later}[2] == "fetch"


def test_사업보고서는_90일_기준(tmp_path):
    state = fd.FetchState(tmp_path / fd.STATE_FILENAME, today=TODAY)
    # 2025 사업보고서(12/31 종료) 기한 = 2026-03-31
    plan = fd.plan_quarters(2025, 2025, state, today=dt.date(2026, 3, 30), refresh_recent=0)
    assert {p["quarter"]: p["action"] for p in plan}[4] == "skip"
    plan = fd.plan_quarters(2025, 2025, state, today=dt.date(2026, 3, 31), refresh_recent=0)
    assert {p["quarter"]: p["action"] for p in plan}[4] == "fetch"


def test_두번째_실행은_호출이_0건(tmp_path):
    out = tmp_path / "dart"
    corps = make_corps(4)

    first = FakeDart(corps)
    assert fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=2, fetch=first,
                        today=TODAY, refresh_recent=0) == 0
    assert len(first.acnt_calls) == 8      # 4분기 × 2배치

    second = FakeDart(corps)
    assert fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=2, fetch=second,
                        today=TODAY, refresh_recent=0) == 0
    assert second.calls == [], "이미 받은 자료는 corpCode 조차 다시 받지 않는다"


def test_최근_2개_분기는_다시_확인한다(tmp_path):
    out = tmp_path / "dart"
    corps = make_corps(4)

    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=4, fetch=FakeDart(corps),
                 today=TODAY, refresh_recent=0)

    again = FakeDart(corps)
    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=4, fetch=again,
                 today=TODAY, refresh_recent=2)
    quarters = [fd.QUARTER_BY_REPRT_CODE[p["reprt_code"]] for p in again.acnt_calls]
    assert quarters == [3, 4], "가장 최근 2개 분기만 다시 확인해야 한다"


def test_force는_전부_다시_받는다(tmp_path):
    out = tmp_path / "dart"
    corps = make_corps(4)

    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=4, fetch=FakeDart(corps),
                 today=TODAY, refresh_recent=0)

    forced = FakeDart(corps)
    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=4, fetch=forced,
                 today=TODAY, refresh_recent=0, force=True)
    quarters = sorted(fd.QUARTER_BY_REPRT_CODE[p["reprt_code"]] for p in forced.acnt_calls)
    assert quarters == [1, 2, 3, 4]
    assert any(url == fd.CORP_CODE_URL for url, _ in forced.calls), \
        "--force 면 기업 고유번호도 다시 받는다"


def test_corp_map은_하루에_한번만(tmp_path):
    out = tmp_path / "dart"
    corps = make_corps(4)

    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=4, fetch=FakeDart(corps),
                 today=TODAY, refresh_recent=0)

    # 같은 날: corpCode 를 다시 받지 않는다
    same_day = FakeDart(corps)
    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=4, fetch=same_day,
                 today=TODAY, refresh_recent=1)
    assert all(url != fd.CORP_CODE_URL for url, _ in same_day.calls)


# ======================================================================================
# 10. 호출 예산 (하루 20,000건)
# ======================================================================================


def test_api_calls는_날짜가_바뀌면_리셋된다(tmp_path):
    p = tmp_path / fd.STATE_FILENAME
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "quarters": {},
                "api_calls_today": 5000,
                "api_calls_date": "2026-08-04",
            }
        ),
        encoding="utf-8",
    )
    assert fd.FetchState(p, today=dt.date(2026, 8, 4)).api_calls_today == 5000
    assert fd.FetchState(p, today=TODAY).api_calls_today == 0, "날짜가 바뀌면 0 으로"


def test_한도에_도달하면_스스로_멈춘다(tmp_path):
    out = tmp_path / "dart"
    corps = make_corps(4)
    fake = FakeDart(corps)
    # corpCode 1회 + 주요계정 3회 = 4회까지만 허용
    rc = fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=2, fetch=fake,
                      today=TODAY, refresh_recent=0, call_limit=4)
    assert rc == 6
    assert len(fake.calls) == 4, "한도를 넘겨 호출하면 안 된다"

    state = json.loads((out / fd.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["api_calls_today"] >= 4
    assert state["quarters"]["2024-1"]["complete"] is True


def test_예산은_사용량을_누적한다(tmp_path):
    out = tmp_path / "dart"
    corps = make_corps(4)
    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=2, fetch=FakeDart(corps),
                 today=TODAY, refresh_recent=0)
    state = json.loads((out / fd.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["api_calls_today"] == 9      # corpCode 1 + 4분기 × 2배치
    assert state["api_calls_date"] == TODAY.isoformat()


def test_state_v1_포맷도_읽는다(tmp_path):
    p = tmp_path / fd.STATE_FILENAME
    p.write_text(
        json.dumps({"version": 1, "done": {"2024Q1": {"rows": 2681, "at": "2026-01-01 00:00:00"}}}),
        encoding="utf-8",
    )
    st = fd.FetchState(p, today=TODAY)
    rec = st.quarter(2024, 1)
    assert rec is not None and rec["complete"] is True and rec["rows"] == 2681


# ======================================================================================
# 11. 인증키
# ======================================================================================


def test_환경변수가_파일보다_우선(tmp_path):
    f = tmp_path / "dart_key.txt"
    f.write_text("FILEKEY\n", encoding="utf-8")
    assert fd.read_api_key(f, env={"DART_API_KEY": "ENVKEY"}) == "ENVKEY"
    assert fd.read_api_key(f, env={}) == "FILEKEY"


def test_키가_없으면_None(tmp_path):
    assert fd.read_api_key(tmp_path / "없는파일.txt", env={}) is None


def test_키파일의_주석과_빈줄은_무시(tmp_path):
    f = tmp_path / "dart_key.txt"
    f.write_text("# 여기에 키를 넣으세요\n\n  REALKEY  \n", encoding="utf-8")
    assert fd.read_api_key(f, env={}) == "REALKEY"


def test_키는_마스킹되어_출력된다():
    masked = fd.mask_key("a" * 40)
    assert masked.startswith("aaaa") and masked.endswith("aaaa (40자)")
    assert "a" * 40 not in masked


def test_selftest는_키가_없으면_3(monkeypatch, capsys):
    monkeypatch.setattr(fd, "read_api_key", lambda *a, **k: None)
    rc = fd.selftest(key=None, fetch=lambda *a, **k: b"")
    out = capsys.readouterr().out
    assert rc == fd.SELFTEST_NO_KEY
    assert "dart_key.txt" in out and "opendart.fss.or.kr" in out


def test_selftest_전과정(capsys):
    corps = [("00126380", "삼성전자", "005930")]
    fake = FakeDart(corps)
    rc = fd.selftest(key="K" * 40, fetch=fake, year=2024, quarter=4)
    out = capsys.readouterr().out
    assert rc == fd.SELFTEST_OK
    assert "부채비율" in out and "유동비율" in out
    assert "update_dart.bat" in out
    assert "K" * 40 not in out, "인증키를 화면에 그대로 찍으면 안 된다"


def test_selftest_인증키오류(capsys):
    def bad(url, params):
        return b"<result><status>010</status><message>error</message></result>"

    assert fd.selftest(key="K" * 40, fetch=bad) == fd.SELFTEST_BAD_KEY
    assert "인증키" in capsys.readouterr().out


def test_selftest_네트워크오류(capsys):
    def down(url, params):
        raise fd.NetworkError("연결 실패: 403 Forbidden")

    assert fd.selftest(key="K" * 40, fetch=down) == fd.SELFTEST_NETWORK
    assert "방화벽" in capsys.readouterr().out


# ======================================================================================
# 12. 저장 규격
# ======================================================================================


def test_저장_컬럼과_dtype(tmp_path):
    out = tmp_path / "dart"
    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=4, fetch=FakeDart(make_corps(3)),
                 today=TODAY, refresh_recent=0)
    df = pd.read_parquet(out / "fundamentals-2024.parquet")
    assert list(df.columns) == fd.OUTPUT_COLUMNS
    for c in fd.AMOUNT_COLUMNS + ["op_income_quarter"]:
        assert str(df[c].dtype) == "Int64", f"{c} 는 원 단위 정수여야 한다"
    assert str(df["disclosed_at"].dtype).startswith("datetime64")
    assert (out / fd.CORP_MAP_FILENAME).is_file()


def test_금액_단위는_원(tmp_path):
    out = tmp_path / "dart"
    fd.run_fetch("KEY", 2024, 2024, out_dir=out, batch=4, fetch=FakeDart(make_corps(1)),
                 today=TODAY, refresh_recent=0)
    store = DartStore(root=out)
    got = store.as_of("100001", "2026-01-01")
    assert got is not None
    assert got["부채총계"] == 150 * EOK
    assert got["debt_ratio_pct"] == pytest.approx(42.86)


# ======================================================================================
# 13. 진단 리포트 (--report)
# ======================================================================================

DISCLOSE_DAY = {1: (0, 5, 15), 2: (0, 8, 14), 3: (0, 11, 14), 4: (1, 3, 11)}


def _disclosed_for(year: int, quarter: int) -> str:
    add, m, d = DISCLOSE_DAY[quarter]
    return f"{year + add:04d}-{m:02d}-{d:02d}"


def realistic_rows(codes, years, *, loss_codes=(), financial_codes=(), seed=7):
    """사람이 봐도 그럴듯한 재무 행들. 영업이익은 **누적**으로 넣는다."""
    import random

    rng = random.Random(seed)
    loss_codes = set(loss_codes)
    financial_codes = set(financial_codes)
    rows = []
    for code in codes:
        if code == "005930":                      # 삼성전자: 부채비율 27%, 유동비율 250%
            equity, liab, ca, cl = 3600, 970, 2180, 870
        elif code == "000660":
            equity, liab, ca, cl = 800, 460, 320, 200
        else:
            equity = rng.randint(200, 5000)
            liab = int(equity * rng.uniform(0.3, 1.6))
            cl = rng.randint(50, 900)
            ca = int(cl * rng.uniform(1.1, 3.0))
        base = rng.randint(20, 400)
        for year in years:
            sign = -1 if code in loss_codes and year % 2 == 0 else 1
            cum = 0
            for q in (1, 2, 3, 4):
                cum += sign * int(base * rng.uniform(0.7, 1.3))
                kw = dict(
                    자산총계=(equity + liab) * EOK,
                    부채총계=liab * EOK,
                    자본총계=equity * EOK,
                    매출액=base * 12 * EOK,
                    당기순이익=int(cum * 0.8) * EOK,
                )
                if code in financial_codes:       # 금융업: 유동/비유동 구분 없음
                    kw["유동자산"] = None
                    kw["유동부채"] = None
                else:
                    kw["유동자산"] = ca * EOK
                    kw["유동부채"] = cl * EOK
                rows.append(
                    row(code, year, q, _disclosed_for(year, q), cum * EOK,
                        corp_code=f"{abs(hash(code)) % 10**8:08d}", **kw)
                )
    return rows


def make_marcap(root: Path, years, codes_by_year, last_date="2026-08-04"):
    """``Date`` / ``Code`` 만 있는 최소 marcap parquet."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    for y in years:
        codes = sorted(codes_by_year[y])
        end = f"{y}-12-01" if y != max(years) else last_date
        df = pd.DataFrame(
            {
                "Date": pd.to_datetime([f"{y}-01-02"] * len(codes) + [end] * len(codes)),
                "Code": codes + codes,
            }
        )
        df.to_parquet(data / f"marcap-{y}.parquet", index=False)
    return root


@pytest.fixture
def healthy(tmp_path):
    """정상 데이터 + marcap. ``(store_root, marcap_root, codes)``."""
    codes = ["005930", "000660"] + [f"{200000 + i:06d}" for i in range(40)]
    years = [2023, 2024, 2025]
    rows = realistic_rows(
        codes, years,
        loss_codes={codes[5], codes[6], codes[7], codes[8], codes[9], codes[10],
                    codes[11], codes[12], codes[13], codes[14]},
        financial_codes={codes[20], codes[21]},
    )
    write_store(tmp_path / "dart", rows)

    dead = [f"{900000 + i:06d}" for i in range(6)]      # 상장폐지 종목
    by_year = {y: set(codes) | set(dead) for y in years}
    by_year[max(years)] = set(codes)                     # 마지막 해에는 사라짐
    make_marcap(tmp_path / "marcap", years, by_year)
    return tmp_path / "dart", tmp_path / "marcap", codes


def run_report(dart_root, marcap_root, capsys):
    rc = fd.run_report(dart_root, marcap_root)
    return rc, capsys.readouterr().out


def test_report_전항목이_출력된다(healthy, capsys):
    dart_root, marcap_root, _codes = healthy
    rc, out = run_report(dart_root, marcap_root, capsys)

    for header in ("[가] 수집 현황", "[나] 파싱 점검", "[다] 대표 종목 샘플",
                   "[라] as-of 동작 확인", "[마] 생존 편향 규모"):
        assert header in out, f"{header} 절이 없다"
    # (가)
    assert "총 행 수" in out and "분기별 행 수" in out and "마지막 수집 시각" in out
    # (나)
    for c in fd.AMOUNT_COLUMNS:
        assert c in out
    assert "debt_ratio_pct" in out and "current_ratio_pct" in out
    assert "op_income_quarter" in out
    assert "음수 표기 파싱 확인" in out and "연결/별도 분포" in out
    # (다)
    assert "005930" in out and "000660" in out
    # (라)
    assert "전 종목 전수 확인" in out
    # (마)
    assert "DART 재무가 한 건도 없는 종목" in out
    assert "판정:" in out
    assert rc == fd.REPORT_OK, f"정상 데이터인데 이슈가 잡혔다:\n{out}"


def test_report_정상데이터는_판정이_정상(healthy, capsys):
    dart_root, marcap_root, _ = healthy
    rc, out = run_report(dart_root, marcap_root, capsys)
    assert rc == fd.REPORT_OK
    assert " 판정: 정상" in out
    assert "as-of 규칙이 지켜지고 있습니다" in out


def test_report_데이터_없으면_안내(tmp_path, capsys):
    rc, out = run_report(tmp_path / "없음", tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_NO_DATA
    assert "update_dart.bat" in out
    assert "test_dart.bat" in out
    assert "판정: 확인 필요" in out


# --- 일부러 망가뜨린 데이터를 잡아내는지 ------------------------------------------------


def _break_store(tmp_path, mutate, *, codes=None, years=(2023, 2024, 2025), **kw):
    """정상 데이터를 만든 뒤 ``mutate(df)`` 로 망가뜨려 다시 저장한다."""
    codes = codes or (["005930", "000660"] + [f"{200000 + i:06d}" for i in range(20)])
    rows = realistic_rows(codes, list(years), **kw)
    root = tmp_path / "dart"
    write_store(root, rows)

    frames = {}
    for p in sorted(root.glob("fundamentals-*.parquet")):
        frames[p] = pd.read_parquet(p)
    for p, df in frames.items():
        mutate(df).to_parquet(p, index=False)
    return root


def test_report_영업이익_전량결측을_잡아낸다(tmp_path, capsys):
    """계정명 매칭이 어긋나 '영업이익' 이 통째로 안 잡힌 상황."""
    def kill_op(df):
        df["영업이익"] = pd.array([None] * len(df), dtype="Int64")
        df["op_income_quarter"] = pd.array([None] * len(df), dtype="Int64")
        return df

    root = _break_store(tmp_path, kill_op)
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "'영업이익' 가 100% 결측입니다" in out
    assert "account_nm" in out, "계정명 매칭 실패를 의심하라고 알려야 한다"


def test_report_유동자산_전량결측을_잡아낸다(tmp_path, capsys):
    def kill(df):
        df["유동자산"] = pd.array([None] * len(df), dtype="Int64")
        df["current_ratio_pct"] = pd.Series([None] * len(df), dtype="float64")
        return df

    root = _break_store(tmp_path, kill)
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "'유동자산' 가 100% 결측입니다" in out
    assert "유동비율을 계산할 수 없는 종목이" in out


def test_report_부채비율_이상치를_잡아낸다(tmp_path, capsys):
    """단위/부호 파싱이 어긋나 부채비율이 터무니없이 큰 상황."""
    def blow_up(df):
        df["debt_ratio_pct"] = df["debt_ratio_pct"] * 100_000.0
        return df

    root = _break_store(tmp_path, blow_up)
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "부채비율 중앙값이" in out
    assert "단위나 부호 파싱을 의심하세요" in out
    assert "10,000% 를 넘는 행이" in out


def test_report_음수가_하나도_없으면_잡아낸다(tmp_path, capsys):
    """'△' / '(1,000)' 음수 표기 파싱이 실패해 적자가 전부 사라진 상황."""
    def all_positive(df):
        for c in ("영업이익", "op_income_quarter", "당기순이익"):
            df[c] = df[c].abs()
        return df

    root = _break_store(tmp_path, all_positive, loss_codes=())
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "영업이익 음수가 한 건도 없습니다" in out
    assert "음수 표기 파싱이 실패했을 가능성" in out


def test_report_삼성전자_숫자가_이상하면_잡아낸다(tmp_path, capsys):
    def wreck_samsung(df):
        m = df["code"].astype(str) == "005930"
        df.loc[m, "debt_ratio_pct"] = 4200.0
        return df

    root = _break_store(tmp_path, wreck_samsung)
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "삼성전자 부채비율 중앙값이" in out
    assert "파싱 오류를 의심하세요" in out


def test_report_삼성전자가_아예_없으면_잡아낸다(tmp_path, capsys):
    root = _break_store(tmp_path, lambda df: df[df["code"].astype(str) != "005930"])
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "005930" in out and "재무가 하나도 없습니다" in out


def test_report_빠진_분기를_잡아낸다(tmp_path, capsys):
    """중간 분기를 통째로 못 받은 상황."""
    def drop_q2_2024(df):
        bad = (df["year"].astype("int64") == 2024) & (df["quarter"].astype("int64") == 2)
        return df[~bad]

    root = _break_store(tmp_path, drop_q2_2024)
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "과거 분기가 비어 있습니다" in out
    assert "2024-2" in out


def test_report_행이_유난히_적은_분기를_잡아낸다(tmp_path, capsys):
    def thin_q3_2024(df):
        bad = ((df["year"].astype("int64") == 2024)
               & (df["quarter"].astype("int64") == 3)
               & (df.groupby(["year", "quarter"]).cumcount() > 2))
        return df[~bad]

    root = _break_store(tmp_path, thin_q3_2024)
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "행 수가 유난히 적은 분기가 있습니다" in out


def test_report_공시일이_전부_비면_잡아낸다(tmp_path, capsys):
    """rcept_no 파싱이 실패해 disclosed_at 이 통째로 빈 상황."""
    def kill_disclosed(df):
        df["disclosed_at"] = pd.to_datetime(pd.Series([None] * len(df)))
        return df

    root = _break_store(tmp_path, kill_disclosed)
    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "공시일" in out and "rcept_no" in out


def test_report_룩어헤드를_잡아낸다(tmp_path, capsys, monkeypatch):
    """as-of 필터가 깨져 공시 전 재무를 돌려주는 상황을 리포트가 잡아내는지.

    ``DartStore`` 를 일부러 망가뜨려 ``disclosed_at`` 을 무시하게 만든다.
    이 테스트가 잡아내지 못하면 진단 도구로서 의미가 없다.
    """
    import engine.dart as ed

    codes = ["005930", "000660"] + [f"{200000 + i:06d}" for i in range(5)]
    root = tmp_path / "dart"
    write_store(root, realistic_rows(codes, [2023, 2024, 2025]))

    def broken_disclosed(self, date):
        return self.load()          # 공시일 필터를 통째로 무시한다

    monkeypatch.setattr(ed.DartStore, "_disclosed", broken_disclosed)

    rc, out = run_report(root, tmp_path / "marcap", capsys)
    assert rc == fd.REPORT_ISSUES
    assert "룩어헤드" in out
    assert "!!!!!" in out, "눈에 띄게 경고해야 한다"
    assert "수익률이 실제보다 좋게 나옵니다" in out


def test_report_생존편향_규모를_센다(healthy, capsys):
    dart_root, marcap_root, codes = healthy
    _rc, out = run_report(dart_root, marcap_root, capsys)
    assert "상장폐지 추정" in out
    # dead 6개는 marcap 에만 있고 DART 에는 없다
    assert "DART 재무가 한 건도 없는 종목        : 6개" in out
    assert "지금은 없음 (상장폐지 추정)      : 6개" in out


def test_report_marcap이_없으면_그_절만_건너뛴다(tmp_path, capsys):
    codes = ["005930", "000660"] + [f"{200000 + i:06d}" for i in range(10)]
    root = tmp_path / "dart"
    write_store(root, realistic_rows(codes, [2023, 2024, 2025]))
    rc, out = run_report(root, tmp_path / "없는marcap", capsys)
    assert "marcap 데이터가 없어 생존 편향 규모를 재지 못했습니다" in out
    assert rc == fd.REPORT_OK


def test_report는_네트워크를_쓰지_않는다(healthy, monkeypatch, capsys):
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("--report 는 네트워크를 쓰면 안 된다")

    monkeypatch.setattr(fd, "http_get", boom)
    monkeypatch.setattr(fd, "_default_fetch", boom)
    dart_root, marcap_root, _ = healthy
    assert run_report(dart_root, marcap_root, capsys)[0] == fd.REPORT_OK


def test_report_CLI_플래그(healthy, monkeypatch, capsys):
    dart_root, marcap_root, _ = healthy
    # --report 는 인증키가 없어도 동작해야 한다
    monkeypatch.setattr(fd, "read_api_key", lambda *a, **k: None)
    rc = fd.main(["--report", "--out", str(dart_root), "--marcap", str(marcap_root)])
    out = capsys.readouterr().out
    assert rc == fd.REPORT_OK
    assert "DART 재무 데이터 진단 리포트" in out


# ======================================================================================
# 14. dart_report.bat
# ======================================================================================


def test_dart_report_bat_인코딩():
    p = ROOT / "dart_report.bat"
    assert p.is_file(), "dart_report.bat 이 없다"
    data = p.read_bytes()
    assert data.count(b"\n") - data.count(b"\r\n") == 0, "홀로 있는 LF 가 있다"
    text = data.decode("cp949")                 # 디코드 실패하면 여기서 터진다
    assert text.encode("cp949") == data, "cp949 왕복이 되지 않는다"
    assert "::" not in text, "블록 안에서 위험한 :: 를 쓰면 안 된다"
    assert "setlocal enabledelayedexpansion" in text
    assert "%%~sd" in text, "%~dp0 8.3 단축이름 변환이 없다"
    assert text.rstrip().endswith("endlocal") or "\npause" in text
    assert "pause" in text
    assert not set(text) & set("→←↑↓"), "유니코드 화살표 금지"
    assert "logs\\dart_report.txt" in text
    assert "--report" in text


def test_dart_report_bat_출력파일_인코딩_지정():
    """메모장에서 한글이 깨지지 않도록 UTF-8 BOM 또는 CP949 로 저장해야 한다."""
    text = (ROOT / "dart_report.bat").read_bytes().decode("cp949")
    assert "PYTHONIOENCODING" in text or "chcp" in text.lower(), \
        "파이썬 출력 인코딩을 고정해야 한글이 안 깨진다"
    assert "BOM" in text or "utf8" in text.lower() or "cp949" in text.lower()
