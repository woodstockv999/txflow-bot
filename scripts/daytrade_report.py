#!/usr/bin/env python3
"""デイトレbotの前進検証レポート。

バックテスト(~/hlbot-research/txflow-daytrade-20260729/report.md)の期待値と突き合わせる。
エッジが消えたのか執行で削られたのかを切り分けられるよう、3段で出す:
  signed_y_bps : 寄りmid→引けmid (バックテストと同じ定義。エッジそのもの)
  gross_bps    : 実約定ベース (スリッページ込み)
  net_bps      : gross − 手数料
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "data" / "daytrade.jsonl"
# バックテストの点推定(レバETF除外・|x|最大1銘柄・コスト控除後)
BACKTEST_NET_BPS = 200.5
BACKTEST_T = 3.19


def stats(v):
    n = len(v)
    if n < 2:
        return None
    m = sum(v) / n
    sd = (sum((x - m) ** 2 for x in v) / (n - 1)) ** 0.5
    se = sd / math.sqrt(n)
    return {"n": n, "mean": m, "sd": sd, "t": m / se if se else float("nan"),
            "mde": 2.80 * se, "hit": 100.0 * sum(1 for x in v if x > 0) / n, "sum": sum(v)}


def line(label, s):
    if not s:
        print(f"  {label:<16} サンプル不足")
        return
    print(f"  {label:<16} n={s['n']:>3}  平均{s['mean']:>8.1f}bps  t={s['t']:>6.2f}  "
          f"MDE={s['mde']:>7.1f}  勝率{s['hit']:>5.1f}%  累計{s['sum']/100:>7.1f}%")


def main():
    if not LEDGER.exists():
        print(f"台帳が無い: {LEDGER}")
        return
    trades, skips = [], []
    for ln in LEDGER.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        (skips if r.get("skipped") else trades).append(r)

    live = [t for t in trades if not t.get("dry_run") and "signed_y_bps" in t]
    paper = [t for t in trades if t.get("dry_run") and "signed_y_bps" in t]
    print(f"台帳: 実弾{len(live)}件 / dry_run {len(paper)}件 / 見送り{len(skips)}日\n")

    for label, rows in (("実弾", live), ("dry_run", paper)):
        if not rows:
            continue
        print(f"=== {label} ===")
        line("エッジ(signed_y)", stats([r["signed_y_bps"] for r in rows]))
        line("実約定(gross)", stats([r["gross_bps"] for r in rows if r.get("gross_bps") is not None]))
        nets = [r["net_bps"] for r in rows if r.get("net_bps") is not None]
        line("手数料後(net)", stats(nets))
        fees = [r["fee_bps"] for r in rows if r.get("fee_bps") is not None]
        if fees:
            print(f"  実測手数料 平均{sum(fees)/len(fees):.1f}bps/往復 "
                  f"(バックテスト仮定 9.3〜14.7bps)")
        slips = [r["entry_slip_bps"] for r in rows if r.get("entry_slip_bps") is not None]
        if slips:
            print(f"  実測エントリスリッページ 平均{sum(slips)/len(slips):.1f}bps")
        pnl = sum(r.get("pnl_usd") or 0 for r in rows)
        print(f"  累計PnL ${pnl:+.2f}")
        s = stats(nets) if nets else None
        if s:
            print(f"  バックテスト期待 {BACKTEST_NET_BPS:.0f}bps (t={BACKTEST_T}) との差 "
                  f"{s['mean']-BACKTEST_NET_BPS:+.0f}bps"
                  f"{'  ※まだ MDE 未達で判定不能' if abs(s['mean']) < s['mde'] else ''}")
        print()

    if skips:
        print(f"見送り日: {len(skips)}  "
              f"|x|最大の平均 {sum(abs(s['x_bps']) for s in skips)/len(skips):.0f}bps")


if __name__ == "__main__":
    main()
