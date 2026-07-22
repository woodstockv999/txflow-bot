#!/usr/bin/env python3
"""クロス会場デルタ中立farm(txflow BTC × perpl 逆BTC)の経済性をテープからオフライン測定(Step2)。

前提: scripts/perpl_xhedge_tape.py が data/perpl_xhedge_tape.jsonl に両会場 BTC BBO を蓄積済み。
ライブ txflow-bot には一切依存しない(銘柄churnと無関係)。テープ上で churn を自前シミュする。

## シミュする churn(1サイクル)
- 方向を交互に振る(実bot同様): even = txflow BUY / perpl SELL、odd = 逆。
- open: 両会場で同時に maker で建てると仮定。txflow BUY は txflow bid、perpl SELL は perpl ask で約定
  (受動makerは自分側の touch で刺さる)。taker フォールバック時は対向を叩く(txflow ask / perpl bid)。
- HOLD_SEC 保有 → close(reduce)。txflow SELL@ask、perpl BUY@bid(maker)。

## 出す指標
1. 会場別 farm 出来高(txflow / perpl)= 二重farm量。
2. クロス会場 net PnL(価格は逆方向で打ち消し、残差=捕獲/支払スプレッド±基差変化)。
   - fee: txflow は meta 404 で不明 → --txflow-maker-bps で外挿(既定 0)。perpl maker 0.9bps。
3. **ヘッジ脚(perpl)の逆選択 markout**: perpl 約定価格に対し perpl mid が +5/30/60s でどれだけ逆行したか。
   これが maker 刺さり仮定の "うますぎ" を補正する実測値。

## 重大な注意(thin-book-close-fill-artifact)
「maker が touch で必ず刺さる」は楽観。BTC は深く狭いので相対的にマシだが、刺さり率・待ち時間は
モデル化していない。markout が負なら、その分だけ maker 捕獲は幻(逆選択で相殺)と読む。数字は
上限(best-case fill)として扱い、markout で割り引くこと。

使い方: .venv/bin/python3 scripts/xhedge_analyze.py [--hold 120] [--txflow-maker-bps 0]
"""
import argparse
import json
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
TAPE_PATH = APP_ROOT / "data" / "perpl_xhedge_tape.jsonl"

PERPL_MAKER_BPS = 0.9
PERPL_TAKER_BPS = 6.9
NOTIONAL_USD = 200.0            # 1脚あたり想定 notional
MARKOUT_HORIZONS = (5, 30, 60)  # 秒


def load_tape() -> list[dict]:
    rows = []
    for line in TAPE_PATH.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        # 両会場 BTC が揃う行だけ使う(片方欠けはヘッジ計算不能)
        p = r.get("perpl", {}).get("BTC")
        t = r.get("txflow", {}).get("BTC")
        if p and t:
            rows.append({"ts": r["ts"], "p_bid": p["bid"], "p_ask": p["ask"],
                         "t_bid": t["bid"], "t_ask": t["ask"]})
    rows.sort(key=lambda x: x["ts"])
    return rows


def perpl_mid_at(rows: list[dict], ts: float) -> float | None:
    """ts 以降で最も近い perpl mid(なければ None)。"""
    best = None
    for r in rows:
        if r["ts"] >= ts:
            best = r
            break
    if best is None:
        return None
    return (best["p_bid"] + best["p_ask"]) / 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=120.0, help="保有秒")
    ap.add_argument("--txflow-maker-bps", type=float, default=0.0, help="txflow maker手数料(不明→外挿)")
    args = ap.parse_args()

    rows = load_tape()
    if len(rows) < 10:
        print(f"両会場そろったsampleが {len(rows)} 件のみ。テープの蓄積待ち(数時間)。")
        return

    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    print(f"両会場そろったsample={len(rows)} 期間={span_h:.2f}h hold={args.hold}s "
          f"txflow_maker={args.txflow_maker_bps}bps perpl_maker={PERPL_MAKER_BPS}bps\n")

    # hold ごとに1サイクル(重ならないよう間引き)。方向を交互。
    cycles = []
    i = 0
    direction_buy = True  # True: txflow BUY / perpl SELL
    while i < len(rows):
        o = rows[i]
        # close 足を探す(open ts + hold 以降の最初のサンプル)
        close_ts = o["ts"] + args.hold
        c = next((r for r in rows[i:] if r["ts"] >= close_ts), None)
        if c is None:
            break
        # --- maker 約定価格(best-case: 自分側 touch)---
        if direction_buy:
            tx_open, tx_close = o["t_bid"], c["t_ask"]      # BUY@bid → SELL@ask
            pp_open, pp_close = o["p_ask"], c["p_bid"]      # SELL@ask → BUY@bid
            tx_pnl = tx_close - tx_open
            pp_pnl = pp_open - pp_close
            hedge_fill = pp_open
        else:
            tx_open, tx_close = o["t_ask"], c["t_bid"]      # SELL@ask → BUY@bid
            pp_open, pp_close = o["p_bid"], c["p_ask"]      # BUY@bid → SELL@ask
            tx_pnl = tx_open - tx_close
            pp_pnl = pp_close - pp_open
            hedge_fill = pp_open
        mid_open = (o["t_bid"] + o["t_ask"]) / 2
        sz = NOTIONAL_USD / mid_open

        fee_usd = NOTIONAL_USD * 2 * (args.txflow_maker_bps + PERPL_MAKER_BPS) / 1e4
        gross = (tx_pnl + pp_pnl) * sz
        net = gross - fee_usd

        # ヘッジ脚 markout(perpl mid の逆行)。SELL なら mid上昇=逆行、BUY なら mid下降=逆行。
        mk = {}
        for h in MARKOUT_HORIZONS:
            m = perpl_mid_at(rows, o["ts"] + h)
            if m is None:
                mk[h] = None
            else:
                adverse = (m - hedge_fill) if direction_buy else (hedge_fill - m)  # +なら逆行(損)
                mk[h] = adverse / hedge_fill * 1e4  # bps
        cycles.append({"net": net, "gross": gross, "fee": fee_usd,
                       "vol_tx": NOTIONAL_USD * 2, "vol_pp": NOTIONAL_USD * 2, "mk": mk})
        # 次サイクルは close 足の次から
        i = rows.index(c) + 1
        direction_buy = not direction_buy

    if not cycles:
        print("hold が長すぎてサイクルが1つも作れない。--hold を短く。")
        return

    n = len(cycles)
    tot_net = sum(c["net"] for c in cycles)
    tot_gross = sum(c["gross"] for c in cycles)
    tot_fee = sum(c["fee"] for c in cycles)
    vol_tx = sum(c["vol_tx"] for c in cycles)
    vol_pp = sum(c["vol_pp"] for c in cycles)
    print(f"サイクル数={n}")
    print(f"farm出来高: txflow=${vol_tx:,.0f} / perpl=${vol_pp:,.0f} (二重farm計=${vol_tx+vol_pp:,.0f})")
    print(f"net PnL={tot_net:+.4f}$ (gross={tot_gross:+.4f} - fee={tot_fee:.4f})  "
          f"= {tot_net/(vol_tx+vol_pp)*1e4:+.3f}bps/出来高$")
    print("ヘッジ脚 逆選択 markout(+ = 約定後に価格が逆行=maker捕獲は幻):")
    for h in MARKOUT_HORIZONS:
        vals = [c["mk"][h] for c in cycles if c["mk"][h] is not None]
        if vals:
            print(f"  +{h:>2}s: 平均{sum(vals)/len(vals):+.3f}bps (n={len(vals)})")


if __name__ == "__main__":
    main()
