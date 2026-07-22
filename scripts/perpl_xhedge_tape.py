#!/usr/bin/env python3
"""txflow と perpl の BTC(+perpl ETH) BBO を ~1s で追記する常駐テープ(クロス会場ヘッジ検証 Step1)。

## 目的
「txflow で BTC を farm しつつ perpl で逆 BTC をヘッジする」クロス会場デルタ中立farm の
経済性を、共有ライブbotに一切触れず独立に測るための素材集め。両会場の BTC BBO を並べて記録し、
Step2(scripts/xhedge_analyze.py)で txflow maker churn × perpl hedge をオフラインでシミュし、
基差(txflow-perpl)・fee drag・逆選択markout を算出する。

## なぜ独立テープか(2026-07-23)
ライブ txflow-bot は複数セッション並行編集で銘柄が churn する(BTC→SOL 等)。その約定に測定を
依存させると銘柄が合わず使えない。よって bot の約定は使わず、両会場の板を自分で観測して
churn をシミュする。テープは公開板を読むだけ=発注なし・bot 非干渉。

## データ源
- perpl: GET /v1/pub/context の market.state.bid/ask(price_decimals でスケール)。1コールで BTC+ETH。
  state 更新はブロック周期 ~1s。
- txflow(HLフォーク): POST /info {type:l2Book, coin:"1"} の levels[0][0]/[1][0]。CF 1010 回避に
  ブラウザ相当ヘッダ必須(署名は不要=無認証)。

## 出力(1行=1サンプル、data/perpl_xhedge_tape.jsonl)
{"ts": wall, "perpl": {"BTC":{bid,ask}, "ETH":{bid,ask}, "t": state_ms},
             "txflow": {"BTC":{bid,ask}, "t": book_ms}}
- 取れなかった会場/銘柄はキー省略(fail-open、ループは止めない)。

## 運用
pm2 常駐。ローテはサイズ基準で別途(date -d yesterday 固定は溜まり分を上書きする罠)。
"""
import json
import sys
import time
from pathlib import Path

import requests

APP_ROOT = Path(__file__).resolve().parent.parent
TAPE_PATH = APP_ROOT / "data" / "perpl_xhedge_tape.jsonl"

PERPL_CONTEXT_URL = "https://app.perpl.xyz/api/v1/pub/context"
PERPL_MARKETS = {1: "BTC", 20: "ETH"}
TXFLOW_INFO_URL = "https://api.txflow.com/info"
TXFLOW_COINS = {"1": "BTC"}  # coin_index -> symbol(ヘッジ対象=BTCのみ)

POLL_SEC = 1.0
HTTP_TIMEOUT = 8.0
_TXFLOW_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PERPL_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
TXFLOW_HEADERS = {
    "User-Agent": _TXFLOW_UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://app.txflow.com",
    "Referer": "https://app.txflow.com/",
}


def _perpl_bbo(session: requests.Session) -> dict:
    """{BTC:{bid,ask}, ETH:{bid,ask}, t:state_ms} を返す。失敗/空は空 dict。"""
    try:
        r = session.get(PERPL_CONTEXT_URL, headers=PERPL_HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return {}
        markets = r.json().get("markets", [])
    except (requests.RequestException, ValueError):
        return {}
    out: dict = {}
    for m in markets:
        name = PERPL_MARKETS.get(m.get("id"))
        if name is None:
            continue
        cfg = m.get("config") or {}
        st = m.get("state") or {}
        pd = cfg.get("price_decimals")
        bid, ask = st.get("bid"), st.get("ask")
        if pd is None or not bid or not ask:
            continue
        scale = 10 ** pd
        out[name] = {"bid": int(bid) / scale, "ask": int(ask) / scale}
        out.setdefault("t", (st.get("at") or {}).get("t"))
    return out


def _txflow_bbo(session: requests.Session) -> dict:
    """{BTC:{bid,ask}, t:book_ms} を返す。失敗/空は空 dict。"""
    out: dict = {}
    for coin, name in TXFLOW_COINS.items():
        try:
            r = session.post(TXFLOW_INFO_URL, headers=TXFLOW_HEADERS,
                             json={"type": "l2Book", "coin": coin}, timeout=HTTP_TIMEOUT)
            if r.status_code != 200 or "application/json" not in r.headers.get("content-type", ""):
                continue
            b = r.json()
            bids, asks = b["levels"][0], b["levels"][1]
            if not bids or not asks:
                continue
            out[name] = {"bid": float(bids[0]["px"]), "ask": float(asks[0]["px"])}
            out.setdefault("t", b.get("time"))
        except (requests.RequestException, ValueError, KeyError, IndexError):
            continue
    return out


def main() -> None:
    TAPE_PATH.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    fails = 0
    with TAPE_PATH.open("a", buffering=1) as f:  # line-buffered
        while True:
            t0 = time.monotonic()
            perpl = _perpl_bbo(session)
            txflow = _txflow_bbo(session)
            if perpl or txflow:
                rec = {"ts": round(time.time(), 3)}
                if perpl:
                    rec["perpl"] = perpl
                if txflow:
                    rec["txflow"] = txflow
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                fails = 0
            else:
                fails += 1
                if fails in (1, 10, 100) or fails % 300 == 0:
                    print(f"[xhedge-tape] 両会場sample失敗 連続{fails}回", file=sys.stderr, flush=True)
            # 連続失敗でバックオフ(perpl/txflow の 429/CF バン対策、最大 +30s)
            backoff = min(fails, 30) * 1.0
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, POLL_SEC + backoff - elapsed))


if __name__ == "__main__":
    main()
