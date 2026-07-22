#!/usr/bin/env python3
"""クロス会場デルタ中立farm(txflow BTC × perpl 逆BTC)の損益をテープから厳密に測定(Step2)。

ライブ txflow-bot 非依存(銘柄churnと無関係)。両会場BTC BBOテープ上で churn を自前シミュし、
per-cycle 台帳で損益を積む。

## 損益の構成(1サイクル=両会場で建てて hold 後に畳む)
txflow脚(farm) と perpl脚(hedge) は逆方向・同notional。価格ドリフトは打ち消し、残るのは:
  net = 捕獲/支払スプレッド(基差残差込み) − 手数料 − funding carry
- **手数料(実測接地)**: txflow maker=1.5 / taker=4.5bps(実約定 cycles.jsonl から逆算, close も~1.5)。
  perpl maker=0.9 / taker=6.9bps、close(reduce)≈無料(perpl_exchange.py docstring)。
  txflow は常に maker(farm脚)。perpl は maker→N秒未約定で taker フォールバックなので
  **perpl-maker(下限コスト) と perpl-taker(上限コスト) の2バウンド**を出す。
- **funding carry**: デルタ中立でも各会場で funding が発生。txflow long なら txflow funding を払い、
  perpl short なら perpl funding を受ける(逆も)。短保有では無視できるが長保有で支配的。
  --tx-funding-bph / --pp-funding-bph (bps/hour) で与える(既定0=funding無視、要実測で埋める)。

## 約定価格モデルと逆選択補正(重要)
maker は「刺されば」自分側 touch で約定する(buy@bid/sell@ask)=価格自体は正。ただし刺さるのは
対向が突っ込む時=不利な瞬間に偏る(逆選択)。この選択バイアスを **perpl脚の markout**(約定後に
perpl mid がどれだけ逆行したか, 5/30/60s)で実測し、**markout補正後 net** も出す。生の maker-touch
net は上限(best-case)として読むこと([[thin-book-close-fill-artifact]])。

使い方: .venv/bin/python3 scripts/xhedge_analyze.py [--hold 120] [--notional 200]
       [--tx-funding-bph 0] [--pp-funding-bph 0]
"""
import argparse
import json
import statistics
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
TAPE_PATH = APP_ROOT / "data" / "perpl_xhedge_tape.jsonl"

# 実測接地の手数料(bps)。txflow=cycles.jsonl逆算、perpl=exchange docstring。
TXFLOW_MAKER_BPS = 1.5
PERPL_MAKER_BPS = 0.9
PERPL_TAKER_BPS = 6.9
PERPL_CLOSE_BPS = 0.0   # reduce ≈ 無料
TXFLOW_CLOSE_BPS = 1.5  # close も maker 1.5(実測)
MARKOUT_HORIZONS = (5, 30, 60)


def load_tape() -> list[dict]:
    rows = []
    for line in TAPE_PATH.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        p = r.get("perpl", {}).get("BTC")
        t = r.get("txflow", {}).get("BTC")
        if p and t and p.get("bid") and p.get("ask") and t.get("bid") and t.get("ask"):
            rows.append({"ts": r["ts"],
                         "p_bid": p["bid"], "p_ask": p["ask"],
                         "t_bid": t["bid"], "t_ask": t["ask"]})
    rows.sort(key=lambda x: x["ts"])
    return rows


def perpl_mid_at(rows: list[dict], start_idx: int, ts: float):
    """start_idx 以降で ts 以上の最初の perpl mid。なければ None。"""
    for r in rows[start_idx:]:
        if r["ts"] >= ts:
            return (r["p_bid"] + r["p_ask"]) / 2
    return None


def simulate(rows: list[dict], hold: float, notional: float,
             tx_funding_bph: float, pp_funding_bph: float) -> list[dict]:
    """重ならない churn を交互方向で回し、per-cycle 台帳を返す。"""
    cycles = []
    i = 0
    dir_buy = True  # True: txflow BUY(long) / perpl SELL(short)
    n = len(rows)
    while i < n:
        o = rows[i]
        # close 足 = open ts + hold 以降の最初のサンプル
        cj = next((j for j in range(i, n) if rows[j]["ts"] >= o["ts"] + hold), None)
        if cj is None:
            break
        c = rows[cj]
        hold_h = (c["ts"] - o["ts"]) / 3600.0

        tx_mid_o = (o["t_bid"] + o["t_ask"]) / 2
        pp_mid_o = (o["p_bid"] + o["p_ask"]) / 2
        size = notional / tx_mid_o  # BTC枚数(両脚同notional)

        if dir_buy:
            # txflow: BUY@bid(open) → SELL@ask(close)
            tx_price_pnl = (c["t_ask"] - o["t_bid"]) * size
            # perpl hedge SHORT: SELL@ask(open) → BUY@bid(close)
            pp_maker_pnl = (o["p_ask"] - c["p_bid"]) * size
            pp_taker_pnl = (o["p_bid"] - c["p_ask"]) * size  # taker: SELL@bid → BUY@ask
            hedge_fill_maker = o["p_ask"]
        else:
            tx_price_pnl = (o["t_ask"] - c["t_bid"]) * size          # SELL@ask → BUY@bid
            pp_maker_pnl = (c["p_ask"] - o["p_bid"]) * size          # BUY@bid → SELL@ask
            pp_taker_pnl = (c["p_bid"] - o["p_ask"]) * size          # taker: BUY@ask → SELL@bid
            hedge_fill_maker = o["p_bid"]

        # 手数料(notional基準、open+close)
        tx_fee = notional * (TXFLOW_MAKER_BPS + TXFLOW_CLOSE_BPS) / 1e4
        pp_fee_maker = notional * (PERPL_MAKER_BPS + PERPL_CLOSE_BPS) / 1e4
        pp_fee_taker = notional * (PERPL_TAKER_BPS + PERPL_TAKER_BPS) / 1e4

        # funding carry(long脚は funding を払う符号、short脚は受ける符号。単純化: 会場別 rate×hold)
        # txflow脚: dir_buy なら long → -tx_funding、perpl脚: dir_buy なら short → +pp_funding
        tx_fund = -(1 if dir_buy else -1) * notional * tx_funding_bph / 1e4 * hold_h
        pp_fund = +(1 if dir_buy else -1) * notional * pp_funding_bph / 1e4 * hold_h
        funding = tx_fund + pp_fund

        net_maker = tx_price_pnl + pp_maker_pnl - tx_fee - pp_fee_maker + funding
        net_taker = tx_price_pnl + pp_taker_pnl - tx_fee - pp_fee_taker + funding

        # perpl脚 markout(約定後の perpl mid 逆行, bps)。SHORT: mid上昇=逆行 / LONG: mid下降=逆行
        mk = {}
        for h in MARKOUT_HORIZONS:
            m = perpl_mid_at(rows, i, o["ts"] + h)
            if m is None:
                mk[h] = None
            else:
                adverse = (m - hedge_fill_maker) if dir_buy else (hedge_fill_maker - m)
                mk[h] = adverse / hedge_fill_maker * 1e4

        open_basis = (tx_mid_o - pp_mid_o) / pp_mid_o * 1e4  # txflow-perpl at open
        tx_mid_c = (c["t_bid"] + c["t_ask"]) / 2
        pp_mid_c = (c["p_bid"] + c["p_ask"]) / 2
        close_basis = (tx_mid_c - pp_mid_c) / pp_mid_c * 1e4
        cycles.append({
            "dir_buy": dir_buy, "hold_h": hold_h, "notional": notional,
            "tx_price_pnl": tx_price_pnl, "pp_maker_pnl": pp_maker_pnl,
            "tx_fee": tx_fee, "pp_fee_maker": pp_fee_maker, "pp_fee_taker": pp_fee_taker,
            "funding": funding, "net_maker": net_maker, "net_taker": net_taker,
            "mk": mk, "open_basis": open_basis, "close_basis": close_basis,
            "hedge_fill": hedge_fill_maker,
        })
        i = cj + 1
        dir_buy = not dir_buy
    return cycles


def report(cycles: list[dict], notional: float, hold: float) -> None:
    n = len(cycles)
    vol_per_venue = notional * 2 * n  # open+close, 会場ごと
    net_m = [c["net_maker"] for c in cycles]
    net_t = [c["net_taker"] for c in cycles]
    tot_m, tot_t = sum(net_m), sum(net_t)
    total_vol = vol_per_venue * 2

    def bps(x):
        return x / total_vol * 1e4

    print(f"サイクル数={n}  1脚notional=${notional:.0f}  hold={hold:.0f}s")
    print(f"farm出来高: txflow=${vol_per_venue:,.0f} / perpl=${vol_per_venue:,.0f} "
          f"(二重farm計=${total_vol:,.0f})\n")

    print("── net PnL(全maker=下限コスト) ──")
    print(f"  合計 {tot_m:+.4f}$  = {bps(tot_m):+.3f}bps/出来高$  "
          f"中央値/cyc {statistics.median(net_m):+.4f}$  勝率 {sum(1 for x in net_m if x>0)/n*100:.0f}%")
    print("── net PnL(perplヘッジ全taker=上限コスト) ──")
    print(f"  合計 {tot_t:+.4f}$  = {bps(tot_t):+.3f}bps/出来高$")

    # 内訳(全maker平均, 1cyc)
    print("\n── 内訳(1サイクル平均, maker) ──")
    for key, lbl in [("tx_price_pnl", "txflow価格PnL"), ("pp_maker_pnl", "perpl価格PnL"),
                     ("tx_fee", "txflow手数料(−)"), ("pp_fee_maker", "perpl手数料(−)"),
                     ("funding", "funding carry")]:
        avg = statistics.mean(c[key] for c in cycles)
        print(f"  {lbl:<18}{avg:+.5f}$")

    print("\n── ヘッジ脚 逆選択 markout(+ = 約定後に逆行=maker捕獲は幻) ──")
    for h in MARKOUT_HORIZONS:
        vals = [c["mk"][h] for c in cycles if c["mk"][h] is not None]
        if vals:
            mu = statistics.mean(vals)
            # markout補正: maker net から逆選択分(bps→$)を引く
            adj = tot_m - mu / 1e4 * vol_per_venue  # perpl脚notionalに対する逆選択コスト
            print(f"  +{h:>2}s 平均{mu:+.3f}bps (n={len(vals)}) → markout補正後net {adj:+.4f}$ ({bps(adj):+.3f}bps/vol)")

    ob = [c["open_basis"] for c in cycles]
    dbasis = [c["close_basis"] - c["open_basis"] for c in cycles]
    print("\n── 基差(txflow-perpl, bps) ──")
    print(f"  open 平均{statistics.mean(ob):+.2f} (σ{statistics.pstdev(ob):.2f})  "
          f"open→close変化 平均{statistics.mean(dbasis):+.2f} (σ{statistics.pstdev(dbasis):.2f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=120.0)
    ap.add_argument("--notional", type=float, default=200.0)
    ap.add_argument("--tx-funding-bph", type=float, default=0.0, help="txflow funding bps/hour(long払い符号)")
    ap.add_argument("--pp-funding-bph", type=float, default=0.0, help="perpl funding bps/hour")
    args = ap.parse_args()

    rows = load_tape()
    if len(rows) < 10:
        print(f"両会場そろったsample={len(rows)} のみ。テープ蓄積待ち(数時間)。")
        return
    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    print(f"両会場そろったsample={len(rows)}  期間={span_h:.2f}h\n"
          f"fee: txflow maker{TXFLOW_MAKER_BPS}/close{TXFLOW_CLOSE_BPS} "
          f"perpl maker{PERPL_MAKER_BPS}/taker{PERPL_TAKER_BPS}/close{PERPL_CLOSE_BPS}\n")
    cycles = simulate(rows, args.hold, args.notional, args.tx_funding_bph, args.pp_funding_bph)
    if not cycles:
        print("hold が長すぎてサイクル0。--hold を短く。")
        return
    report(cycles, args.notional, args.hold)


if __name__ == "__main__":
    main()
