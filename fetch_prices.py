#!/usr/bin/env python3
"""
종가·환율 수집기 (GitHub Actions용)

tickers.json을 읽어 미국(yfinance)·한국(pykrx) 종가와 USD/KRW 환율을 수집하고
prices.json으로 저장한다. 대시보드는 이 prices.json을 raw URL로 읽어 시세를 채운다.

- 거래 내역 등 자산 정보는 일절 다루지 않는다 (가격만 공개).
- 종목별 실패는 건너뛰고 로그에 남긴다. 환율 실패 시 기존 prices.json의 환율을 유지한다.
- 출력 형식은 대시보드 normalizeWorkbook이 읽는 시세스냅샷/환율히스토리와 동일 의미.
"""
import json
import sys
import math
import datetime as dt
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def load_tickers(path="tickers.json"):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    # 형식: {"US": ["AAPL","TSLA"], "KR": ["005930","000660"], "fx_start": "2021-01-01"}
    us = [str(t).strip().upper() for t in cfg.get("US", []) if str(t).strip()]
    kr = []
    for t in cfg.get("KR", []):
        d = "".join(ch for ch in str(t).strip() if ch.isdigit())
        if d:
            kr.append(d.zfill(6))
    fx_start = cfg.get("fx_start", "2021-01-01")
    return us, kr, fx_start


def load_prev(path="prices.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fetch_us(tickers):
    import yfinance as yf
    # 야후 파이낸스 심볼 보정: 사용자 티커 → 야후 조회용 심볼.
    # 야후는 클래스 주식을 하이픈으로 표기(BRKB→BRK-B). 조회만 바꾸고 저장은 사용자 티커로 유지.
    YH = {"BRKB": "BRK-B", "BRKA": "BRK-A", "BFB": "BF-B"}
    out, fails = {}, []
    for tk in tickers:
        yq = YH.get(tk.upper(), tk)
        try:
            h = yf.Ticker(yq).history(period="7d", auto_adjust=False)
            if h.empty:
                raise ValueError("empty")
            out[tk] = round(float(h["Close"].iloc[-1]), 2)
            print(f"  [US] {tk}: {out[tk]}" + (f" (야후 {yq})" if yq != tk else ""))
        except Exception as e:
            fails.append(tk)
            print(f"  [US] {tk} FAIL: {e}", file=sys.stderr)
    return out, fails


def fetch_kr(tickers, now):
    from pykrx import stock as krx
    out, fails = {}, []
    f = (now.date() - dt.timedelta(days=14)).strftime("%Y%m%d")
    t = now.strftime("%Y%m%d")
    for tk in tickers:
        try:
            df = krx.get_market_ohlcv(f, t, tk)
            if df is None or df.empty:
                raise ValueError("empty")
            out[tk] = int(df["종가"].iloc[-1])
            print(f"  [KR] {tk}: {out[tk]}")
        except Exception as e:
            fails.append(tk)
            print(f"  [KR] {tk} FAIL: {e}", file=sys.stderr)
    return out, fails


def fetch_fx(start, now):
    import yfinance as yf
    h = yf.Ticker("KRW=X").history(
        start=start,
        end=(now.date() + dt.timedelta(days=1)).isoformat(),
        interval="1d", auto_adjust=False,
    )
    if h.empty:
        raise ValueError("empty fx")
    rows = []
    for idx, v in h["Close"].items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        rows.append({"date": idx.date().isoformat(), "rate": round(float(v), 2)})
    return rows


def main():
    now = dt.datetime.now(KST)
    asof = now.strftime("%Y-%m-%d %H:%M")
    print(f"수집 시작 {asof} KST")

    us_tk, kr_tk, fx_start = load_tickers()
    prev = load_prev()
    print(f"대상: US {len(us_tk)}종목, KR {len(kr_tk)}종목, 환율 시작 {fx_start}")

    prices = []
    all_fails = []

    us_px, us_f = fetch_us(us_tk)
    all_fails += [("US", t) for t in us_f]
    for tk, p in us_px.items():
        prices.append({"ticker": tk, "mkt": "US", "price": p, "ccy": "USD"})

    kr_px, kr_f = fetch_kr(kr_tk, now)
    all_fails += [("KR", t) for t in kr_f]
    for tk, p in kr_px.items():
        prices.append({"ticker": tk, "mkt": "KR", "price": p, "ccy": "KRW"})

    # 환율
    try:
        fx = fetch_fx(fx_start, now)
        print(f"환율 {len(fx)}일치 ({fx[0]['date']} ~ {fx[-1]['date']})")
    except Exception as e:
        print(f"환율 수집 실패: {e} — 기존 환율 유지", file=sys.stderr)
        fx = (prev or {}).get("fx", [])
        if not fx:
            print("기존 환율도 없음 — 환율 비어 있음", file=sys.stderr)

    out = {
        "asof": now.date().isoformat(),
        "asof_time": asof,
        "tz": "Asia/Seoul",
        "prices": prices,
        "fx": fx,
        "failures": [f"{m}:{t}" for m, t in all_fails],
    }

    # 가격을 하나도 못 받았으면 기존 파일 보존(빈 파일로 덮어쓰지 않음)
    if not prices and prev and prev.get("prices"):
        print("이번 수집에서 가격 0건 — 기존 prices.json 유지", file=sys.stderr)
        out["prices"] = prev["prices"]
        out["note"] = "이번 실행에서 신규 가격 수집 실패, 직전 값 보존"

    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"prices.json 저장: 가격 {len(out['prices'])}건, 환율 {len(out['fx'])}일")
    if all_fails:
        print("실패 종목:", ", ".join(f"{m}:{t}" for m, t in all_fails))


if __name__ == "__main__":
    main()
