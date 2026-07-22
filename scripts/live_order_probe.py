#!/usr/bin/env python3
"""txflow 実弾疎通テスト(1回限り、手動実行専用)。

BTC の mid から約1%下に post-only(tif=Alo)指値を1発 → openOrders で確認 → 即取消 →
取消成功を確認する。agent鍵の承認とtxflow_signing/txflow_clientの署名実装を検証する目的
**以外の発注は行わない**。hedge_bot.py のループからは呼ばれない、独立した検証専用スクリプト。

安全策(多重):
- notional は $10〜$12 の範囲外なら実行前に abort
- tif=Alo (post-only): 発注時点でスプレッドを取る側に転んだ場合はtxflow側がreject/resting拒否
  してくれるはず(即約定しない設計)
- 実行には --confirm が必須(デフォルトはdry-runで計算内容の表示のみ)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values

from src.txflow_client import TxflowClient

MIN_NOTIONAL = 10.0
MAX_NOTIONAL = 12.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="実際に発注する(無指定ならdry-run表示のみ)")
    ap.add_argument("--notional", type=float, default=11.0)
    ap.add_argument("--pct-below-mid", type=float, default=0.01)
    args = ap.parse_args()

    if not (MIN_NOTIONAL <= args.notional <= MAX_NOTIONAL):
        print(f"ABORT: notional {args.notional} は許容範囲[{MIN_NOTIONAL},{MAX_NOTIONAL}]外", file=sys.stderr)
        sys.exit(1)

    env_path = Path(__file__).resolve().parent.parent / ".env"
    cfg = dotenv_values(env_path)
    agent_key = cfg.get("TXFLOW_AGENT_PRIVATE_KEY")
    main_addr = cfg.get("TXFLOW_MAIN_ADDRESS")
    agent_addr = cfg.get("TXFLOW_AGENT_ADDRESS")
    if not agent_key or not main_addr:
        print("ABORT: .env に TXFLOW_AGENT_PRIVATE_KEY / TXFLOW_MAIN_ADDRESS が無い", file=sys.stderr)
        sys.exit(1)

    client = TxflowClient(agent_private_key=agent_key, main_address=main_addr)

    book = client.get_l2book("BTC")
    levels = book["levels"]
    best_bid = float(levels[0][0]["px"])
    best_ask = float(levels[1][0]["px"])
    mid = (best_bid + best_ask) / 2
    raw_price = mid * (1 - args.pct_below_mid)
    price = round(raw_price)  # BTCは szDecimals=4 → 価格は5桁有効数字制約で整数丸めが安全
    size = round(args.notional / price, 5)
    notional_actual = round(price * size, 4)

    print(f"main_address   = {main_addr}")
    print(f"agent_address  = {agent_addr}")
    print(f"BTC mid        = {mid}")
    print(f"order price    = {price} ({args.pct_below_mid*100:.1f}% 下, post-only buy)")
    print(f"order size     = {size} BTC")
    print(f"order notional = ${notional_actual}")

    if not (MIN_NOTIONAL <= notional_actual <= MAX_NOTIONAL):
        print(f"ABORT: 計算後notional ${notional_actual} が範囲外", file=sys.stderr)
        sys.exit(1)

    action = client.build_limit_order_action(
        symbol="BTC", is_buy=True, price=str(price), size=str(size),
        reduce_only=False, tif="Alo",
    )
    print("\naction =", json.dumps(action, indent=2))

    if not args.confirm:
        print("\n--confirm 無指定のため発注しない(dry-run表示のみ)。")
        return

    print("\n=== 発注 (/exchange) ===")
    resp = client.exchange(action)
    print(json.dumps(resp, indent=2, ensure_ascii=False))

    oid = None
    try:
        statuses = resp["response"]["data"]["statuses"]
        for st in statuses:
            if "resting" in st:
                oid = st["resting"]["oid"]
            elif "filled" in st:
                oid = st["filled"]["oid"]
                print("WARNING: post-onlyのはずが即約定した可能性:", st)
    except (KeyError, TypeError, IndexError):
        print("WARNING: レスポンス形式が想定と違う。手動でoidを確認してください。")

    time.sleep(1.5)
    print("\n=== openOrders 確認 ===")
    open_orders = client.get_open_orders()
    print(json.dumps(open_orders, indent=2, ensure_ascii=False))

    if oid is None and isinstance(open_orders, list):
        for o in open_orders:
            if o.get("coin") in ("BTC", "BTC-USDC", "1") and abs(float(o.get("limitPx", 0)) - price) < 1e-6:
                oid = o.get("oid")

    if oid is None:
        print("ABORT: oidを特定できず取消できない。手動で openOrders を確認して取消してください。", file=sys.stderr)
        sys.exit(2)

    print(f"\n=== 取消 (/exchange, oid={oid}) ===")
    cancel_resp = client.cancel_order("BTC", oid)
    print(json.dumps(cancel_resp, indent=2, ensure_ascii=False))

    time.sleep(1.5)
    print("\n=== openOrders 再確認(取消後、空であるべき) ===")
    open_orders_after = client.get_open_orders()
    print(json.dumps(open_orders_after, indent=2, ensure_ascii=False))

    still_open = False
    if isinstance(open_orders_after, list):
        still_open = any(o.get("oid") == oid for o in open_orders_after)
    print(f"\nstill_open={still_open} -> {'FAIL: 取消が反映されていない' if still_open else 'OK: 取消確認'}")


if __name__ == "__main__":
    main()
