#!/usr/bin/env python3
"""cloid(client order id)対応の実弾疎通テスト(1回限り、手動実行専用)。

遠値(mid比-20%)post-only指値に"c":"0x<32hex>"を付けて発注 -> openOrdersにcloidが
エコーされるか確認 -> oidで取消(cancelByCloid相当は未確認のためoid経由) -> 取消確認。
hedge_bot.py のループからは呼ばれない、独立した検証専用スクリプト。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values

from src.txflow_client import TxflowClient

SYMBOL = "BTC"
PCT_BELOW_MID = 0.20  # 遠値(=約定リスクを実質ゼロにする)


def main():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    cfg = dotenv_values(env_path)
    agent_key = cfg.get("TXFLOW_AGENT_PRIVATE_KEY")
    main_addr = cfg.get("TXFLOW_MAIN_ADDRESS")
    if not agent_key or not main_addr:
        print("ABORT: .env不備", file=sys.stderr)
        sys.exit(1)

    client = TxflowClient(agent_private_key=agent_key, main_address=main_addr)

    book = client.get_l2book(SYMBOL)
    levels = book["levels"]
    mid = (float(levels[0][0]["px"]) + float(levels[1][0]["px"])) / 2
    price = mid * (1 - PCT_BELOW_MID)
    size = 12.0 / price  # notional ~$12 (量子化は build_limit_order_wire 内で実施)

    cloid = client.new_cloid()
    print(f"symbol={SYMBOL} mid={mid} price(target)={price} cloid={cloid}")

    wire = client.build_limit_order_wire(SYMBOL, True, price, size, reduce_only=False,
                                          tif=client.TIF_POST_ONLY, cloid=cloid)
    print("wire_action =", json.dumps(wire, indent=2))

    print("\n=== 発注 (/exchange, cloid付き) ===")
    resp = client.place_limit_order(SYMBOL, True, price, size, reduce_only=False,
                                     tif=client.TIF_POST_ONLY, cloid=cloid)
    print(json.dumps(resp, indent=2, ensure_ascii=False))

    time.sleep(1.0)
    print("\n=== openOrders 確認(cloidがエコーされるか) ===")
    open_orders = client.get_open_orders()
    print(json.dumps(open_orders, indent=2, ensure_ascii=False))

    matched = None
    for o in open_orders or []:
        if str(o.get("coin", "")).split("-")[0].upper() == SYMBOL:
            matched = o
            break

    if matched is None:
        print("\nRESULT: openOrdersに注文が見当たらない(発注失敗の可能性)。手動確認要。")
        return

    echoed_cloid = matched.get("cloid")
    print(f"\nRESULT: cloid送信={cloid!r}  openOrders.cloid={echoed_cloid!r}  "
          f"一致={echoed_cloid == cloid}")

    oid = matched.get("oid")
    print(f"\n=== 取消 (/exchange, oid={oid} 経由。cancelByCloid相当は未確認のため) ===")
    cancel_resp = client.cancel_order(SYMBOL, oid)
    print(json.dumps(cancel_resp, indent=2, ensure_ascii=False))

    time.sleep(1.0)
    open_orders_after = client.get_open_orders()
    still_open = any(o.get("oid") == oid for o in (open_orders_after or []))
    print(f"\nstill_open={still_open} -> {'FAIL' if still_open else 'OK: 取消確認'}")


if __name__ == "__main__":
    main()
