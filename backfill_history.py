#!/usr/bin/env python3
"""
가격 히스토리 백필 (GitHub Actions용, 1회성/주기 실행 겸용)

tickers.json을 읽어 각 종목의 과거 일별 종가와 USD/KRW 환율을 받아
history.json으로 저장한다. TWR 등 기간 수익률 계산의 토대.

- 기존 history.json이 있으면 누락 구간만 채워 누적(증분 백필).
- tickers.json의 fx_start를 바꾸면 그 다음 실행에서 '한 번만' 전체 재수집한다.
- 가격·환율만 다룬다(자산 정보 없음).

[2026-07 수정]
 1) 야후 심볼 보정 추가: BRKB는 야후에서 BRK-B라 그대로 조회하면 "delisted" 오류가
    나 히스토리가 0일이 된다. fetch_prices.py에는 있던 보정이 여기엔 빠져 있었다.
 2) fx_start를 앞당겨도 과거가 안 채워지던 문제 수정: 기존 데이터가 있으면 무조건
    '마지막 날짜+1'부터 받아 과거로는 절대 확장되지 않았다. 이제 fx_start가 직전
    실행과 달라지면 전체를 다시 받는다(backfill_from으로 판별). 매 실행마다 전체를
    다시 받지는 않으므로 평소 증분 동작은 그대로다.
"""
import json
import sys
import math
import datetime as dt
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# 야후 파이낸스 심볼 보정: 사용자 티커 → 야후 조회용 심볼.
# 야후는 클래스 주식을 하이픈으로 표기(BRKB→BRK-B). 저장은 사용자 티커 그대로 한다.
YH = {"BRKB": "BRK-B", "BRKA": "BRK-A", "BFB": "BF-B"}


def load_tickers(path="tickers.json"):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    us = [str(t).strip().upper() for t in cfg.get("US", []) if str(t).strip()]
    kr = []
    for t in cfg.get("KR", []):
        d = "".join(ch for ch in str(t).strip() if ch.isdigit())
        if d:
            kr.append(d.zfill(6))
    # 벤치마크 지수(^KS11, ^GSPC 등). 보유 종목이 아니라 비교용이므로 US/KR과 분리한다.
    bench = [str(t).strip() for t in cfg.get("BENCH", []) if str(t).strip()]
    fx_start = cfg.get("fx_start", "2021-01-01")
    return us, kr, bench, fx_start


def load_prev(path="history.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fetch_us_history(ticker, start, end):
    """미국 종목 일별 종가 dict {date: price}."""
    import yfinance as yf
    yq = YH.get(ticker.upper(), ticker)
    h = yf.Ticker(yq).history(start=start, end=end, interval="1d", auto_adjust=True)
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

    us_tk, kr_tk, bench_tk, fx_start = load_tickers()
    print(f"대상: US {len(us_tk)}, KR {len(kr_tk)}, 지수 {len(bench_tk)}, 시작 {fx_start} ~ {now.date()}")
    prev = load_prev() or {}
    prev_tickers = prev.get("tickers", {})
    prev_fx = prev.get("fx", {})

    # fx_start가 직전 실행과 달라졌으면 이번 한 번만 전체 재수집.
    # (종목별 '가장 이른 날짜'로 판단하면 상장이 늦은 종목 때문에 매번 전체를 다시 받게 된다)
    prev_from = prev.get("backfill_from")
    full_rebuild = bool(prev_tickers) and prev_from != fx_start
    if full_rebuild:
        print(f"fx_start 변경 감지({prev_from} → {fx_start}) — 전체 재수집합니다.")

    def start_for(existing_prices):
        if full_rebuild:
            return fx_start
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
        merged = {} if full_rebuild else dict(prev_px)
        if s <= now.date().isoformat():
            try:
                new_px = fetch_us_history(tk, s, end)
                merged.update(new_px)
                yq = YH.get(tk.upper(), tk)
                note = f" (야후 {yq})" if yq != tk else ""
                print(f"  [US] {tk}: +{len(new_px)}일 (누적 {len(merged)}){note}")
            except Exception as e:
                failures.append(f"US:{tk}")
                print(f"  [US] {tk} FAIL: {e}", file=sys.stderr)
                if full_rebuild and prev_px:
                    merged = dict(prev_px)  # 재수집 실패 시 기존 값 보존
        if not merged:
            failures.append(f"US:{tk}(빈값)")
        out_tickers[tk] = {"mkt": "US", "prices": merged}

    # 한국 종목
    for tk in kr_tk:
        prev_px = prev_tickers.get(tk, {}).get("prices", {})
        s = start_for(prev_px)
        merged = {} if full_rebuild else dict(prev_px)
        if s <= now.date().isoformat():
            try:
                new_px = fetch_kr_history(tk, s, now.date().isoformat())
                merged.update(new_px)
                print(f"  [KR] {tk}: +{len(new_px)}일 (누적 {len(merged)})")
            except Exception as e:
                failures.append(f"KR:{tk}")
                print(f"  [KR] {tk} FAIL: {e}", file=sys.stderr)
                if full_rebuild and prev_px:
                    merged = dict(prev_px)
        if not merged:
            failures.append(f"KR:{tk}(빈값)")
        out_tickers[tk] = {"mkt": "KR", "prices": merged}

    # 벤치마크 지수 (야후 심볼 그대로, mkt=IDX로 표시해 보유 종목과 구분)
    for tk in bench_tk:
        prev_px = prev_tickers.get(tk, {}).get("prices", {})
        s2 = start_for(prev_px)
        merged = {} if full_rebuild else dict(prev_px)
        if s2 <= now.date().isoformat():
            try:
                new_px = fetch_us_history(tk, s2, end)
                merged.update(new_px)
                print(f"  [IDX] {tk}: +{len(new_px)}일 (누적 {len(merged)})")
            except Exception as e:
                failures.append(f"IDX:{tk}")
                print(f"  [IDX] {tk} FAIL: {e}", file=sys.stderr)
                if full_rebuild and prev_px:
                    merged = dict(prev_px)
        if not merged:
            failures.append(f"IDX:{tk}(빈값)")
        out_tickers[tk] = {"mkt": "IDX", "prices": merged}

    # 환율
    fx_merged = {} if full_rebuild else dict(prev_fx)
    fx_s = start_for(prev_fx)
    if fx_s <= now.date().isoformat():
        try:
            new_fx = fetch_fx_history(fx_s, end)
            fx_merged.update(new_fx)
            print(f"  [FX] +{len(new_fx)}일 (누적 {len(fx_merged)})")
        except Exception as e:
            failures.append("FX")
            print(f"  [FX] FAIL: {e}", file=sys.stderr)
            if full_rebuild and prev_fx:
                fx_merged = dict(prev_fx)

    # 전체 종목이 모두 비었으면 기존 보존
    total_pts = sum(len(t["prices"]) for t in out_tickers.values())
    if total_pts == 0 and prev.get("tickers"):
        print("신규/기존 가격 0건 — 기존 history.json 유지", file=sys.stderr)
        return

    # 재수집 결과가 기존보다 크게 줄었으면(대량 실패 의심) 덮어쓰지 않는다
    prev_pts = sum(len(t.get("prices", {})) for t in prev_tickers.values())
    if full_rebuild and prev_pts and total_pts < prev_pts * 0.5:
        print(f"재수집 결과가 기존({prev_pts})보다 과도하게 적음({total_pts}) — "
              f"기존 history.json 유지", file=sys.stderr)
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
        "backfill_from": fx_start,
        "tickers": out_tickers,
        "fx": fx_merged,
        "failures": failures,
    }
    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"history.json 저장: {len(out_tickers)}종목, 환율 {len(fx_merged)}일, "
          f"기간 {out['start']}~{out['end']}, 총 {total_pts}개 가격")
    if failures:
        print("실패:", ", ".join(failures))


if __name__ == "__main__":
    main()
