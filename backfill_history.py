#!/usr/bin/env python3
"""
가격 히스토리 백필 (GitHub Actions용, 1회성/주기 실행 겸용)

tickers.json을 읽어 각 종목의 과거 일별 종가와 USD/KRW 환율을 받아
history.json으로 저장한다. TWR 등 기간 수익률 계산의 토대.

- 기존 history.json이 있으면 누락 구간만 채워 누적(증분 백필).
- 가격·환율만 다룬다(자산 정보 없음).
- 출력 구조:
  {
    "start": "2021-01-01", "end": "2026-06-13",
    "asof_time": "...", "tz": "Asia/Seoul",
    "tickers": { "005930": {"mkt":"KR","prices":{"2021-01-04":83000,...}}, ... },
    "fx": {"2021-01-04":1086.5, ...},
    "failures": [...]
  }
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
    us = [str(t).strip().upper() for t in cfg.get("US", []) if str(t).strip()]
    kr = []
    for t in cfg.get("KR", []):
        d = "".join(ch for ch in str(t).strip() if ch.isdigit())
        if d:
            kr.append(d.zfill(6))
    fx_start = cfg.get("fx_start", "2021-01-01")
    return us, kr, fx_start


def load_prev(path="history.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fetch_us_history(ticker, start, end):
    """미국 종목 일별 종가 dict {date: price}.

    auto_adjust=True: 분할·배당이 반영된 수정주가. 분할 종목의 과거 평가액이
    정확해진다(권장). 단 배당까지 가격에 반영되므로, 대시보드의 총수익 TWR에서
    배당을 별도로 더하면 소폭 이중계산될 수 있다. 분할 영향이 훨씬 크고 빈번하므로
    분할조정을 우선한다. 순수 가격만 원하면 auto_adjust=False로 변경.
    """
    import yfinance as yf
    h = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=True)
    out = {}
    if h.empty:
        return out
    for idx, v in h["Close"].items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        out[idx.date().isoformat()] = round(float(v), 2)
    return out


def fetch_kr_history(ticker, start, end):
    """한국 종목 일별 종가 dict {date: price}."""
    from pykrx import stock as krx
    f = start.replace("-", "")
    t = end.replace("-", "")
    df = krx.get_market_ohlcv(f, t, ticker)
    out = {}
    if df is None or df.empty:
        return out
    for idx, row in df.iterrows():
        d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
        close = row["종가"]
        if close and close > 0:
            out[d] = int(close)
    return out


def fetch_fx_history(start, end):
    """USD/KRW 일별 환율 dict {date: rate}."""
    import yfinance as yf
    h = yf.Ticker("KRW=X").history(start=start, end=end, interval="1d", auto_adjust=False)
    out = {}
    if h.empty:
        return out
    for idx, v in h["Close"].items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        out[idx.date().isoformat()] = round(float(v), 2)
    return out


def main():
    now = dt.datetime.now(KST)
    end = (now.date() + dt.timedelta(days=1)).isoformat()  # yfinance end는 배타적
    asof = now.strftime("%Y-%m-%d %H:%M")
    print(f"백필 시작 {asof} KST")

    us_tk, kr_tk, fx_start = load_tickers()
    prev = load_prev() or {}
    prev_tickers = prev.get("tickers", {})
    prev_fx = prev.get("fx", {})

    # 증분 백필: 기존 데이터가 있으면 마지막 날짜 다음부터, 없으면 fx_start부터
    def start_for(existing_prices):
        if existing_prices:
            last = max(existing_prices.keys())
            d = dt.date.fromisoformat(last) + dt.timedelta(days=1)
            return d.isoformat()
        return fx_start

    print(f"대상: US {len(us_tk)}, KR {len(kr_tk)}, 시작 {fx_start} ~ {now.date()}")

    out_tickers = {}
    failures = []

    # 미국 종목
    for tk in us_tk:
        prev_px = prev_tickers.get(tk, {}).get("prices", {})
        s = start_for(prev_px)
        merged = dict(prev_px)
        if s <= now.date().isoformat():
            try:
                new_px = fetch_us_history(tk, s, end)
                merged.update(new_px)
                print(f"  [US] {tk}: +{len(new_px)}일 (누적 {len(merged)})")
            except Exception as e:
                failures.append(f"US:{tk}")
                print(f"  [US] {tk} FAIL: {e}", file=sys.stderr)
        out_tickers[tk] = {"mkt": "US", "prices": merged}

    # 한국 종목
    for tk in kr_tk:
        prev_px = prev_tickers.get(tk, {}).get("prices", {})
        s = start_for(prev_px)
        merged = dict(prev_px)
        if s <= now.date().isoformat():
            try:
                new_px = fetch_kr_history(tk, s, now.date().isoformat())
                merged.update(new_px)
                print(f"  [KR] {tk}: +{len(new_px)}일 (누적 {len(merged)})")
            except Exception as e:
                failures.append(f"KR:{tk}")
                print(f"  [KR] {tk} FAIL: {e}", file=sys.stderr)
        out_tickers[tk] = {"mkt": "KR", "prices": merged}

    # 환율
    fx_merged = dict(prev_fx)
    fx_s = start_for(prev_fx)
    if fx_s <= now.date().isoformat():
        try:
            new_fx = fetch_fx_history(fx_s, end)
            fx_merged.update(new_fx)
            print(f"  [FX] +{len(new_fx)}일 (누적 {len(fx_merged)})")
        except Exception as e:
            failures.append("FX")
            print(f"  [FX] FAIL: {e}", file=sys.stderr)

    # 전체 종목이 모두 비었으면 기존 보존
    total_pts = sum(len(t["prices"]) for t in out_tickers.values())
    if total_pts == 0 and prev.get("tickers"):
        print("신규/기존 가격 0건 — 기존 history.json 유지", file=sys.stderr)
        return

    all_dates = []
    for t in out_tickers.values():
        all_dates += list(t["prices"].keys())
    all_dates += list(fx_merged.keys())
    out = {
        "start": min(all_dates) if all_dates else fx_start,
        "end": max(all_dates) if all_dates else now.date().isoformat(),
        "asof_time": asof,
        "tz": "Asia/Seoul",
        "tickers": out_tickers,
        "fx": fx_merged,
        "failures": failures,
    }
    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"history.json 저장: {len(out_tickers)}종목, 환율 {len(fx_merged)}일, "
          f"기간 {out['start']}~{out['end']}")
    if failures:
        print("실패:", ", ".join(failures))


if __name__ == "__main__":
    main()
