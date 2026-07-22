"""txflow pair_hedge 簡易版 状態機械。

~/apps/hyperliquid-bot/src/pair_hedge.py (perpl版、変更禁止・参照専用) の実測知見を反映した
簡易実装。フル版のような累積統計/レジーム別サイズ変更/RVゲート等は持たない(仕様範囲外)。

状態遷移: IDLE -> LEAD_RESTING -> HEDGED -> HOLD -> UNWIND -> FLAT -> (IDLE)

反映した実測知見(perpl pair_hedgeでの事故から):
- 取消後は必ず約定有無を再確認する(取消×約定レースで建玉2倍化した前例)。
  `_cancel_and_verify()` は "canceled"|"filled"|"unknown" の三値を返し、"filled"なら
  taker発注をスキップしてmaker約定として扱う("unknown"は安全側でそのtickは何もしない)。
- 片脚だけ残った stranded leg は reduce-only taker で即時自動決済(IDLE時に毎tick確認)
- nonce は ms タイムスタンプ単調増加(TxflowClient/NonceManagerで保証済み)

設計変更(2026-07-22 Fableレビュー): txflow BTC/ETHの厚み次第でリード脚のjoinはleg_timeout内に
刺さらないことがある。リード脚は未約定でもポジションリスクがゼロ(片脚も持っていない)ので、
taker化(4.5bps支払い)は不要。**リード脚のtimeoutはtaker化せず、取消して即IDLEに戻り
再クオートする**(abort_reason="lead_timeout_requote")。ヘッジ脚・unwindのtimeoutは
リスク遮断のためtaker化を維持する。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hedge_bot")

FEE_MAKER_BPS = 1.5
FEE_TAKER_BPS = 4.5


class State(Enum):
    IDLE = "IDLE"
    LEAD_RESTING = "LEAD_RESTING"
    HEDGED = "HEDGED"
    HOLD = "HOLD"
    UNWIND = "UNWIND"
    FLAT = "FLAT"


@dataclass
class _Leg:
    symbol: str
    is_buy_open: bool
    target_size: float
    # open
    open_price: Optional[float] = None
    open_fee: Optional[float] = None
    open_type: Optional[str] = None  # "maker" | "taker"
    open_filled: bool = False
    resting_price: Optional[float] = None
    resting_since: Optional[float] = None
    oid: Optional[int] = None
    # close
    close_price: Optional[float] = None
    close_fee: Optional[float] = None
    close_type: Optional[str] = None
    close_filled: bool = False
    close_resting_price: Optional[float] = None
    close_resting_since: Optional[float] = None
    close_oid: Optional[int] = None

    def notional(self, price: float) -> float:
        return abs(self.target_size) * price

    def open_pnl_leg(self) -> float:
        if self.open_price is None or self.close_price is None:
            return 0.0
        if self.is_buy_open:
            return (self.close_price - self.open_price) * self.target_size
        return (self.open_price - self.close_price) * self.target_size

    def total_fee(self) -> float:
        return (self.open_fee or 0.0) + (self.close_fee or 0.0)


def _fee(notional: float, is_maker: bool) -> float:
    bps = FEE_MAKER_BPS if is_maker else FEE_TAKER_BPS
    return notional * bps / 10000.0


class PairHedgeBot:
    """dry_run: WS l2Book のタッチ到達で仮想約定をシミュレートする(発注しない)。
    live(dry_run=False): TxflowClient経由で実発注する。agent_private_key が無い場合
    TxflowClient.exchange() が例外を出すため、実際には稼働しない(config.dry_run/enabled
    の既定値と合わせた二重の安全装置)。
    """

    def __init__(self, cfg: dict, client, ws, ledger_path: Path, notify_fn=None):
        self.cfg = cfg
        self.client = client
        self.ws = ws
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._notify = notify_fn or (lambda *a, **k: None)

        self.state = State.IDLE
        self.cycle_count = 0
        self.halted = False
        self._was_halted = False
        self._error_streak = 0
        self._daily_pnl_window: list[tuple[float, float]] = []  # (ts, pnl)

        self._lead_toggle = True
        self.legs: dict[str, _Leg] = {}
        self._cycle_start_ts: Optional[float] = None
        self._hold_until: Optional[float] = None
        self._abort_reason: Optional[str] = None
        self._lead_requote_streak = 0

    # ------------------------------------------------------------------ helpers
    def _mid(self, symbol: str) -> Optional[float]:
        ba = self.ws.best_bid_ask(symbol)
        if not ba:
            return None
        bid, ask = ba
        return (bid + ask) / 2

    def _best(self, symbol: str) -> Optional[tuple[float, float]]:
        return self.ws.best_bid_ask(symbol)

    def _rolling_daily_pnl(self, now: float) -> float:
        cutoff = now - 86400
        self._daily_pnl_window = [(t, p) for (t, p) in self._daily_pnl_window if t >= cutoff]
        return sum(p for _, p in self._daily_pnl_window)

    # ------------------------------------------------------------------ main loop
    def tick(self) -> None:
        now = time.time()
        if self.halted:
            if not self._was_halted:
                self._notify("halt", "red", "txflow-bot: 日次損失上限超過でhalt。全close済み。手動確認要")
                self._was_halted = True
            return
        self._was_halted = False

        if self.state == State.IDLE:
            self._reconcile_stranded_legs()
            self._try_start_cycle(now)
        elif self.state == State.LEAD_RESTING:
            self._drive_lead_resting(now)
        elif self.state == State.HEDGED:
            self._drive_hedged(now)
        elif self.state == State.HOLD:
            self._drive_hold(now)
        elif self.state == State.UNWIND:
            self._drive_unwind(now)
        elif self.state == State.FLAT:
            self._finish_cycle(now)

    # ------------------------------------------------------------------ stranded leg safety
    def _reconcile_stranded_legs(self) -> None:
        """live modeのみ有効(dry_runは仮想ポジションのみでボット再起動時に自動的にゼロへ戻る)。
        IDLE中(=我々の認識では両脚フラットのはず)に実ポジションが残っていたら
        reduce-onlyのtaker注文で即時自動決済する。"""
        if self.cfg.get("dry_run", True):
            return
        try:
            chs = self.client.get_clearinghouse_state()
        except Exception as e:
            logger.warning("stranded leg確認失敗(clearinghouseState): %s", e)
            return
        positions = {p["position"]["coin"]: p["position"] for p in chs.get("assetPositions", [])}
        for symbol in (self.cfg["base_symbol"], self.cfg["hedge_symbol"]):
            pos = positions.get(symbol)
            if not pos:
                continue
            szi = float(pos.get("szi", 0))
            if abs(szi) < 1e-12:
                continue
            logger.warning("stranded leg検出: %s szi=%s -> reduce-only taker close", symbol, szi)
            self._notify("stranded_leg", "orange", f"txflow-bot: stranded leg検出 {symbol} szi={szi} -> 自動close")
            try:
                best = self._best(symbol)
                if not best:
                    continue
                bid, ask = best
                is_buy_close = szi < 0  # ショートなら買い戻し、ロングなら売り
                price = ask if is_buy_close else bid
                self.client.place_limit_order(symbol, is_buy_close, price, abs(szi),
                                               reduce_only=True, tif=self.client.TIF_IOC)
            except Exception as e:
                logger.error("stranded leg自動closeに失敗: %s", e)
                self._notify("stranded_leg_close_failed", "red", f"txflow-bot: stranded leg自動closeに失敗 {symbol}: {e}")

    # ------------------------------------------------------------------ IDLE -> LEAD_RESTING
    def _try_start_cycle(self, now: float) -> None:
        base = self.cfg["base_symbol"]
        hedge = self.cfg["hedge_symbol"]
        mid_base = self._mid(base)
        mid_hedge = self._mid(hedge)
        best_base = self._best(base)
        if mid_base is None or mid_hedge is None or best_base is None:
            return  # 板が来るまで待つ

        notional = float(self.cfg["notional_usd"])
        hedge_ratio = float(self.cfg["hedge_ratio"])
        size_base = round(notional / mid_base, 6)
        size_hedge = round((notional * hedge_ratio) / mid_hedge, 6)

        is_buy_lead = self._lead_toggle
        self._lead_toggle = not self._lead_toggle

        bid, ask = best_base
        resting_price = bid if is_buy_lead else ask

        self.legs = {
            base: _Leg(symbol=base, is_buy_open=is_buy_lead, target_size=size_base),
            hedge: _Leg(symbol=hedge, is_buy_open=not is_buy_lead, target_size=size_hedge),
        }
        lead = self.legs[base]
        lead.resting_price = resting_price
        lead.resting_since = now
        self._cycle_start_ts = now
        self._abort_reason = None

        if not self.cfg.get("dry_run", True):
            lead.oid = self._place_maker(base, is_buy_lead, resting_price, size_base)

        self.state = State.LEAD_RESTING
        logger.info("cycle#%d start: lead=%s %s@%.4f size=%.6f", self.cycle_count, base,
                    "buy" if is_buy_lead else "sell", resting_price, size_base)

    # ------------------------------------------------------------------ LEAD_RESTING
    def _drive_lead_resting(self, now: float) -> None:
        base = self.cfg["base_symbol"]
        lead = self.legs[base]
        best = self._best(base)
        if best is None:
            return
        bid, ask = best

        filled = self._check_dry_run_touch(lead, bid, ask) if self.cfg.get("dry_run", True) else self._check_live_fill(lead)

        timeout = now - lead.resting_since >= float(self.cfg["leg_timeout_seconds"])
        if not filled and timeout:
            # 設計変更(2026-07-22): リード脚は未約定でもポジションリスクがゼロなのでtaker化しない。
            # 取消して即requote(dry_runにはレースが無いのでcancel_and_verify無しでそのままrequote)。
            if self.cfg.get("dry_run", True) or lead.oid is None:
                self._requote_lead(now)
                return
            status, fill = self._cancel_and_verify(base, lead.oid)
            if status == "filled":
                lead.open_price = float(fill["px"])
                lead.open_type = "maker"
                lead.open_fee = float(fill.get("fee", _fee(lead.notional(lead.open_price), True)))
                lead.open_filled = True
                filled = True
            elif status == "unknown":
                return  # 安全側: このtickは何もしない、次tickで再評価
            else:
                self._requote_lead(now)
                return

        if filled:
            self._lead_requote_streak = 0
            self._start_hedge_leg(now)

    def _requote_lead(self, now: float) -> None:
        self._lead_requote_streak += 1
        if self._lead_requote_streak % 20 == 0:
            logger.warning("lead脚requoteが%d回連続で未約定(cycle#%d、%s)", self._lead_requote_streak,
                            self.cycle_count, self.cfg["base_symbol"])
        self._abort_cycle_and_log("lead_timeout_requote", now)

    def _abort_cycle_and_log(self, reason: str, now: float) -> None:
        rec = {
            "timestamp": now,
            "cycle": self.cycle_count,
            "legs": [],
            "volume_usd": 0.0,
            "net_pnl_usd": 0.0,
            "abort_reason": reason,
            "dry_run": self.cfg.get("dry_run", True),
        }
        self._append_ledger(rec)
        self.cycle_count += 1
        self.legs = {}
        self._abort_reason = None
        self.state = State.IDLE

    def _start_hedge_leg(self, now: float) -> None:
        hedge_sym = self.cfg["hedge_symbol"]
        hedge = self.legs[hedge_sym]
        best = self._best(hedge_sym)
        if best is None:
            # 板が無ければリード脚だけ持って待つ(次tickで再試行)
            return
        bid, ask = best
        hedge.resting_price = ask if hedge.is_buy_open else bid
        hedge.resting_since = now

        if not self.cfg.get("dry_run", True):
            hedge.oid = self._place_maker(hedge_sym, hedge.is_buy_open, hedge.resting_price, hedge.target_size)

        self.state = State.HEDGED

    # ------------------------------------------------------------------ HEDGED
    def _drive_hedged(self, now: float) -> None:
        hedge_sym = self.cfg["hedge_symbol"]
        hedge = self.legs[hedge_sym]
        best = self._best(hedge_sym)
        if best is None:
            return
        bid, ask = best

        filled = self._check_dry_run_touch(hedge, bid, ask) if self.cfg.get("dry_run", True) else self._check_live_fill(hedge)

        timeout = now - hedge.resting_since >= float(self.cfg["leg_timeout_seconds"])
        if not filled and timeout:
            self._convert_leg_to_taker(hedge, bid, ask, phase="open")
            filled = True
            if self._abort_reason is None:
                self._abort_reason = "hedge_leg_timeout_taker"

        if filled:
            self._hold_until = now + float(self.cfg["hold_seconds"])
            self.state = State.HOLD

    # ------------------------------------------------------------------ HOLD
    def _drive_hold(self, now: float) -> None:
        if now >= self._hold_until:
            self._start_unwind(now)

    def _start_unwind(self, now: float) -> None:
        for leg in self.legs.values():
            best = self._best(leg.symbol)
            if best is None:
                continue
            bid, ask = best
            is_buy_close = not leg.is_buy_open
            leg.close_resting_price = bid if is_buy_close else ask
            leg.close_resting_since = now
            if not self.cfg.get("dry_run", True):
                leg.close_oid = self._place_maker(leg.symbol, is_buy_close, leg.close_resting_price,
                                                   leg.target_size, reduce_only=True)
        self.state = State.UNWIND

    # ------------------------------------------------------------------ UNWIND
    def _drive_unwind(self, now: float) -> None:
        all_closed = True
        for leg in self.legs.values():
            if leg.close_filled:
                continue
            best = self._best(leg.symbol)
            if best is None:
                all_closed = False
                continue
            bid, ask = best
            filled = self._check_dry_run_touch_close(leg, bid, ask) if self.cfg.get("dry_run", True) \
                else self._check_live_fill_close(leg)

            timeout = now - leg.close_resting_since >= float(self.cfg["leg_timeout_seconds"])
            if not filled and timeout:
                self._convert_leg_to_taker(leg, bid, ask, phase="close")
                filled = True
                if self._abort_reason is None:
                    self._abort_reason = "unwind_leg_timeout_taker"
            if not filled:
                all_closed = False

        if all_closed:
            self.state = State.FLAT

    # ------------------------------------------------------------------ FLAT
    def _finish_cycle(self, now: float) -> None:
        legs = list(self.legs.values())
        total_fee = sum(l.total_fee() for l in legs)
        total_pnl_gross = sum(l.open_pnl_leg() for l in legs)
        net_pnl = total_pnl_gross - total_fee
        volume = sum(l.notional(l.open_price or 0) + l.notional(l.close_price or 0) for l in legs)

        rec = {
            "timestamp": now,
            "cycle": self.cycle_count,
            "legs": [
                {
                    "symbol": l.symbol,
                    "is_buy_open": l.is_buy_open,
                    "size": l.target_size,
                    "open_price": l.open_price,
                    "open_type": l.open_type,
                    "open_fee": l.open_fee,
                    "close_price": l.close_price,
                    "close_type": l.close_type,
                    "close_fee": l.close_fee,
                }
                for l in legs
            ],
            "volume_usd": round(volume, 4),
            "net_pnl_usd": round(net_pnl, 6),
            "abort_reason": self._abort_reason,
            "dry_run": self.cfg.get("dry_run", True),
        }
        self._append_ledger(rec)
        self._daily_pnl_window.append((now, net_pnl))

        daily_pnl = self._rolling_daily_pnl(now)
        if daily_pnl <= -abs(float(self.cfg["daily_loss_limit_usd"])):
            self.halted = True
            self._force_close_all()

        self.cycle_count += 1
        self.legs = {}
        self._abort_reason = None
        self.state = State.IDLE

    def _force_close_all(self) -> None:
        if self.cfg.get("dry_run", True):
            return
        for symbol in (self.cfg["base_symbol"], self.cfg["hedge_symbol"]):
            try:
                chs = self.client.get_clearinghouse_state()
                positions = {p["position"]["coin"]: p["position"] for p in chs.get("assetPositions", [])}
                pos = positions.get(symbol)
                if not pos:
                    continue
                szi = float(pos.get("szi", 0))
                if abs(szi) < 1e-12:
                    continue
                best = self._best(symbol)
                if not best:
                    continue
                bid, ask = best
                is_buy_close = szi < 0
                price = ask if is_buy_close else bid
                self.client.place_limit_order(symbol, is_buy_close, price, abs(szi),
                                               reduce_only=True, tif=self.client.TIF_IOC)
            except Exception as e:
                logger.error("halt時の強制close失敗 %s: %s", symbol, e)

    # ------------------------------------------------------------------ fill detection (dry_run)
    @staticmethod
    def _check_dry_run_touch(leg: _Leg, bid: float, ask: float) -> bool:
        if leg.open_filled:
            return True
        if leg.is_buy_open:
            touched = ask <= leg.resting_price
        else:
            touched = bid >= leg.resting_price
        if touched:
            leg.open_price = leg.resting_price
            leg.open_type = "maker"
            leg.open_fee = _fee(leg.notional(leg.open_price), is_maker=True)
            leg.open_filled = True
        return leg.open_filled

    @staticmethod
    def _check_dry_run_touch_close(leg: _Leg, bid: float, ask: float) -> bool:
        if leg.close_filled:
            return True
        is_buy_close = not leg.is_buy_open
        if is_buy_close:
            touched = ask <= leg.close_resting_price
        else:
            touched = bid >= leg.close_resting_price
        if touched:
            leg.close_price = leg.close_resting_price
            leg.close_type = "maker"
            leg.close_fee = _fee(leg.notional(leg.close_price), is_maker=True)
            leg.close_filled = True
        return leg.close_filled

    def _convert_leg_to_taker(self, leg: _Leg, bid: float, ask: float, phase: str) -> None:
        """指摘1(2026-07-22): 取消×約定レース対策。cancel_and_verifyが"filled"を返したら
        既にmaker約定していたということなので、taker発注はスキップしてmaker約定として記録する
        (建玉2倍化を防ぐ)。"unknown"の場合は二重発注リスクを避けてこのtickは何もしない。"""
        if phase == "open":
            price = ask if leg.is_buy_open else bid
            if not self.cfg.get("dry_run", True) and leg.oid is not None:
                status, fill = self._cancel_and_verify(leg.symbol, leg.oid)
                if status == "filled":
                    leg.open_price = float(fill["px"])
                    leg.open_type = "maker"
                    leg.open_fee = float(fill.get("fee", _fee(leg.notional(leg.open_price), True)))
                    leg.open_filled = True
                    return
                if status == "unknown":
                    return
                self.client.place_limit_order(leg.symbol, leg.is_buy_open, price, leg.target_size,
                                               reduce_only=False, tif=self.client.TIF_IOC)
            leg.open_price = price
            leg.open_type = "taker"
            leg.open_fee = _fee(leg.notional(price), is_maker=False)
            leg.open_filled = True
        else:
            is_buy_close = not leg.is_buy_open
            price = ask if is_buy_close else bid
            if not self.cfg.get("dry_run", True) and leg.close_oid is not None:
                status, fill = self._cancel_and_verify(leg.symbol, leg.close_oid)
                if status == "filled":
                    leg.close_price = float(fill["px"])
                    leg.close_type = "maker"
                    leg.close_fee = float(fill.get("fee", _fee(leg.notional(leg.close_price), True)))
                    leg.close_filled = True
                    return
                if status == "unknown":
                    return
                self.client.place_limit_order(leg.symbol, is_buy_close, price, leg.target_size,
                                               reduce_only=True, tif=self.client.TIF_IOC)
            leg.close_price = price
            leg.close_type = "taker"
            leg.close_fee = _fee(leg.notional(price), is_maker=False)
            leg.close_filled = True

    # ------------------------------------------------------------------ live-mode helpers
    def _place_maker(self, symbol: str, is_buy: bool, price: float, size: float,
                      reduce_only: bool = False) -> Optional[int]:
        """maker指値を発注し、oidを返す(見つからなければNone)。
        実測(2026-07-22 live_order_probe.py): /exchangeのorder応答は
        `{"status":"ok","response":{"type":"PlaceOrder","data":{"statuses":["success"]}}}`
        で **oidを含まない**(HL標準の`{"resting":{"oid":...}}}`形ではなかった)。そのため
        openOrdersをsymbol+side+priceで突き合わせてoidを特定する。"""
        try:
            self.client.place_limit_order(symbol, is_buy, price, size,
                                           reduce_only=reduce_only, tif=self.client.TIF_POST_ONLY)
            self._error_streak = 0
        except Exception as e:
            self._error_streak += 1
            logger.error("place_limit_order失敗 %s: %s", symbol, e)
            if self._error_streak >= 3:
                self._notify("error_streak", "red", f"txflow-bot: 発注エラー連発({self._error_streak}回): {e}")
            raise
        return self._find_oid_by_price(symbol, price, is_buy)

    def _find_oid_by_price(self, symbol: str, price: float, is_buy: bool) -> Optional[int]:
        try:
            open_orders = self.client.get_open_orders()
        except Exception as e:
            logger.warning("openOrders取得失敗(oid特定用): %s", e)
            return None
        side = "B" if is_buy else "A"  # 実測: openOrdersのsideは"B"/"A"("buy"/"sell"ではない)
        price_str = self.client.quantize_price(symbol, price)
        for o in open_orders or []:
            coin = str(o.get("coin", "")).split("-")[0].upper()
            if coin != symbol.upper() or o.get("side") != side:
                continue
            if str(o.get("limitPx")) == price_str:
                return o.get("oid")
        return None

    def _check_live_fill(self, leg: _Leg) -> bool:
        """live mode: openOrdersにoidが無ければ約定/取消済みとみなし、fillsで実勢価格を確認する。
        (未実弾検証。実運用前に要検証項目)"""
        if leg.open_filled or leg.oid is None:
            return leg.open_filled
        try:
            open_orders = self.client.get_open_orders()
        except Exception as e:
            logger.warning("openOrders確認失敗: %s", e)
            return False
        still_open = any(o.get("oid") == leg.oid for o in (open_orders or []))
        if still_open:
            return False
        fill = self._find_fill(leg.symbol, leg.oid)
        if fill is None:
            return False
        leg.open_price = float(fill["px"])
        leg.open_type = "maker"
        leg.open_fee = float(fill.get("fee", _fee(leg.notional(leg.open_price), True)))
        leg.open_filled = True
        return True

    def _check_live_fill_close(self, leg: _Leg) -> bool:
        if leg.close_filled or leg.close_oid is None:
            return leg.close_filled
        try:
            open_orders = self.client.get_open_orders()
        except Exception as e:
            logger.warning("openOrders確認失敗: %s", e)
            return False
        still_open = any(o.get("oid") == leg.close_oid for o in (open_orders or []))
        if still_open:
            return False
        fill = self._find_fill(leg.symbol, leg.close_oid)
        if fill is None:
            return False
        leg.close_price = float(fill["px"])
        leg.close_type = "maker"
        leg.close_fee = float(fill.get("fee", _fee(leg.notional(leg.close_price), True)))
        leg.close_filled = True
        return True

    def _find_fill(self, symbol: str, oid: int) -> Optional[dict]:
        try:
            fills = self.client.get_user_fills()
        except Exception as e:
            logger.warning("userFills取得失敗: %s", e)
            return None
        for f in fills or []:
            if f.get("oid") == oid:
                return f
        return None

    def _cancel_and_verify(self, symbol: str, oid: int) -> tuple[str, Optional[dict]]:
        """取消×約定レース対策(指摘1、2026-07-22): 取消後に必ず openOrders/fills で
        再確認する(perpl pair_hedgeで建玉2倍化した実測知見の反映。未実弾検証)。
        戻り値: ("canceled", None) | ("filled", fill_dict) | ("unknown", None)。
        呼び出し側は "filled" ならmaker約定として扱い、二重発注(taker化)をスキップすること。
        "unknown" は openOrders確認自体に失敗した場合の安全側の応答(=何もしない)。"""
        try:
            self.client.cancel_order(symbol, oid)
        except Exception as e:
            logger.warning("cancel_order失敗(既に約定/取消済みの可能性): %s", e)
        try:
            open_orders = self.client.get_open_orders()
        except Exception as e:
            logger.warning("取消後のopenOrders確認に失敗: %s", e)
            return ("unknown", None)
        still_open = any(o.get("oid") == oid for o in (open_orders or []))
        if still_open:
            logger.error("取消後もopenOrdersに残存 oid=%s %s -> 手動確認要", oid, symbol)
            self._notify("cancel_race", "red", f"txflow-bot: 取消後もopenOrdersに残存 oid={oid} {symbol}")
            return ("unknown", None)
        fill = self._find_fill(symbol, oid)
        if fill is not None:
            logger.warning("取消×約定レース検出: oid=%s %s は取消前に約定済みだった -> maker約定として扱う",
                            oid, symbol)
            return ("filled", fill)
        return ("canceled", None)

    # ------------------------------------------------------------------ ledger
    def _append_ledger(self, rec: dict) -> None:
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info("cycle#%d flat: volume=$%.2f net_pnl=$%.4f abort=%s", rec["cycle"],
                    rec["volume_usd"], rec["net_pnl_usd"], rec["abort_reason"])
