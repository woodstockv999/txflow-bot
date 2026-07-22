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

実弾稼働事故(2026-07-22 19:35-19:48 BTC)を受けた修正:
lead注文が実際は5回約定していたのにbotは全サイクルを"lead_timeout_requote"と誤認していた。
原因は `_find_oid_by_price`(symbol+side+price文字列一致でopenOrdersからoidを探す)が
一致に失敗し oid が取れず、約定検知・取消どちらも不能になっていたこと(=注文が板に堆積)。
対策:
1. **cloid(client order id)を全発注に付与し、oid特定はcloidベースに変更**
   (`_place_and_identify`)。実測: txflowのcloidはHL標準の`0x`+32hex形式ではなく
   **UUID4文字列**("Invalid cloid format"で前者は拒否、後者は受理・openOrdersにエコー
   確認済み)。発注後openOrdersを300ms間隔で最大5回ポーリングしcloid一致でoidを回収する。
   見つからなければuserFillsで即時約定(発注時刻以降・symbol・side一致)を確認する。
   どちらでも同定できなければそのサイクルを中断し、該当symbolの全open orderを取消してから
   IDLEへ戻る(`_abort_cycle_lost_oid`)。
2. **部分約定はsize合算で判定**(`_find_fill`): 同一oidに対する複数fillレコードを合算し、
   目標サイズに達したら"約定"とみなす(サイズ加重平均価格を採用)。
3. **起動時リコンサイル強化**(`startup_reconcile`): config変更で見えなくなった建玉
   (今回の事故: BTCからSOLに切替時、botが見ていないBTC建玉0.0030が裸で残った)の再発防止に、
   起動時は**symbol不問で全open orderを取消**し、**clearinghouseStateの全建玉**を
   reduce-only IOCでフラット化してWARNING+discord通知する。
4. wireのp/sの末尾ゼロ除去(Fable実測・修正済み): s="0.0030"だとAuthorization failedになる。

実弾稼働事故2(2026-07-22 20:19-20:21 SOL/ETH)を受けた修正:
UNWINDでETH close発注のcloid/userFillsどちらでも同定できず"lost"扱いになり、taker強制close
(`_convert_leg_to_taker`)を試みたが、実ポジションが既にゼロ(何らかの経路で既にフラット
だった)のためreduce-only注文がエラー応答となり、`close_filled`が立たないままUNWINDから
50秒以上抜けられなくなった(裸ショートが数分残存)。対策:
5. **OID_POLL_ATTEMPTS/INTERVALを8回×0.5秒(計4秒)へ延長**(openOrdersの伝播ラグは実測
   2-3秒あるため、従来の1.5秒(5回×0.3秒)では不足するケースがあった)。
6. **taker強制close失敗時、実ポジションを直接確認して整合させる**(`_position_szi`)。
   HTTP例外・レスポンスstatus=="err"のどちらでも、実ポジションが既にゼロなら「既に閉じて
   いた」とみなしclose_filled=Trueで正常に抜ける(無限リトライ・スタック防止)。ゼロでなければ
   従来どおり未クローズのまま次のleg_timeoutでの再試行に委ねる。
7. **watchdog新設**(`_check_watchdog`/`_watchdog_force_reset`): state!=IDLEが
   `leg_timeout_seconds*2+60`秒を超えて継続したら、両symbolの全open order取消→
   clearinghouseStateの全建玉をreduce-only IOCでフラット化(リトライ2回)→台帳に
   abort_reason="watchdog_reset"記録→discord通知(red、1回)→IDLEへ強制リセットする。
   どの経路が閉塞しても裸ポジションが数分以上残らないようにする恒久安全網。
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
    open_requotes: int = 0
    # close
    close_price: Optional[float] = None
    close_fee: Optional[float] = None
    close_type: Optional[str] = None
    close_filled: bool = False
    close_resting_price: Optional[float] = None
    close_resting_since: Optional[float] = None
    close_oid: Optional[int] = None
    close_requotes: int = 0

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


def _apply_fill(leg: _Leg, fill: dict, order_type: str, closing: bool) -> None:
    """fill({"px","sz","fee"}、floatのみ)をlegのopen/close側に反映する共通ヘルパー。"""
    if closing:
        leg.close_price = float(fill["px"])
        leg.close_type = order_type
        leg.close_fee = float(fill["fee"])
        leg.close_filled = True
    else:
        leg.open_price = float(fill["px"])
        leg.open_type = order_type
        leg.open_fee = float(fill["fee"])
        leg.open_filled = True
        # 部分約定対応(2026-07-22実測: 取消×約定レースで0.54/1.29のみ約定)。
        # 実約定量をこの脚の正とし、以降のヘッジサイズ・決済サイズを実勢に合わせる。
        filled_sz = float(fill.get("sz", 0) or 0)
        if filled_sz > 0:
            leg.target_size = filled_sz


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
        self._raw_notify = notify_fn or (lambda *a, **k: None)
        self._notify_counts: dict[str, int] = {}
        self._notify = self._throttled_notify

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
        self._was_active_hours: Optional[bool] = None

    # ------------------------------------------------------------------ helpers
    def _within_active_hours(self, now: float) -> bool:
        """稼働時間帯ゲート(2026-07-23)。実測で 00-02時台は効率が落ちるため既定で除外する。
        設定が無ければ常時稼働。start<=end は同日内、start>end は日跨ぎとして扱う。
        判定はサーバのローカル時刻(JST)。"""
        hours = self.cfg.get("active_hours")
        if not hours:
            return True
        start = int(hours.get("start_hour", 0))
        end = int(hours.get("end_hour", 24))
        h = time.localtime(now).tm_hour
        active = (start <= h < end) if start <= end else (h >= start or h < end)
        if active != self._was_active_hours:
            logger.info("稼働時間ゲート: %s (%d時, 設定 %d-%d時)",
                        "稼働開始" if active else "停止(時間外)", h, start, end)
            self._was_active_hours = active
        return active

    def _throttled_notify(self, context: str, color: str, body: str) -> None:
        """一過性の繰り返し通知(stranded_leg等)が毎回Discordを鳴らさないよう抑止。
        contextごとに3回まで送信、4回目に抑止開始を1回だけ通知、以降は黙る。"""
        n = self._notify_counts.get(context, 0) + 1
        self._notify_counts[context] = n
        if n <= 3:
            self._raw_notify(context, color, body)
        elif n == 4:
            self._raw_notify(context, "gray", f"txflow-bot: {context} が繰り返し発生のため以降の通知は抑止")

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
        """トップレベルで例外を捕まえる(2026-07-22事故2対応): 個別メソッド内の想定外例外が
        tick()から外へ伝播してmain.pyのループごとプロセスを落とす経路を塞ぐ。1tick分の処理を
        スキップしてログ+通知するだけに留め、状態は次tickでwatchdogが最終的に救済する。"""
        try:
            self._tick_inner(time.time())
        except Exception as e:
            logger.exception("tick()で予期しない例外、このtickをスキップして継続: %s", e)
            self._error_streak += 1
            if self._error_streak >= 3:
                self._notify("tick_exception", "red", f"txflow-bot: tick()で例外連発: {e}")

    def _tick_inner(self, now: float) -> None:
        if self._check_watchdog(now):
            return  # このtickはwatchdogの強制リセットで消費した

        if self.halted:
            if not self._was_halted:
                self._notify("halt", "red", "txflow-bot: 日次損失上限超過でhalt。全close済み。手動確認要")
                self._was_halted = True
            return
        self._was_halted = False

        if self.state == State.IDLE:
            self._reconcile_stranded_legs()
            # 稼働時間外は新規サイクルを開始しない。進行中のサイクルは中断せず畳ませる
            # (途中で止めると裸脚が残るため、ゲートはIDLEでのみ効かせる)。
            if not self._within_active_hours(now):
                return
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

    # ------------------------------------------------------------------ watchdog(指摘3)
    def _check_watchdog(self, now: float) -> bool:
        """state!=IDLEが`leg_timeout_seconds*2+60`秒を超えて継続したら強制リセットする
        恒久安全網。UNWINDでのtaker close失敗など、どの経路が閉塞しても裸ポジションが
        数分以上残らないようにする(2026-07-22事故2対応)。発火したらTrueを返す
        (呼び出し側はそのtickの通常処理をスキップする)。"""
        if self.state == State.IDLE or self._cycle_start_ts is None:
            return False
        # requote導入(2026-07-23)で正常なサイクルもhedge/unwindでそれぞれ
        # leg_timeout*(max_requotes+1) かかりうるため、閾値をそれに追随させる。
        attempts = int(self.cfg.get("max_requotes", 0)) + 1
        threshold = float(self.cfg["leg_timeout_seconds"]) * attempts * 2 + 120
        elapsed = now - self._cycle_start_ts
        if elapsed < threshold:
            return False
        logger.error("watchdog発火: state=%s が%.0f秒継続(閾値%.0f秒) -> 強制リセット",
                     self.state.value, elapsed, threshold)
        self._notify("watchdog_reset", "red",
                      f"txflow-bot: watchdog発火。state={self.state.value}が{elapsed:.0f}秒継続、"
                      f"両symbol全取消+全建玉フラット化して強制リセットします")
        self._watchdog_force_reset(now)
        return True

    def _watchdog_force_reset(self, now: float) -> None:
        if not self.cfg.get("dry_run", True):
            for symbol in (self.cfg["base_symbol"], self.cfg["hedge_symbol"]):
                self._cancel_all_orders_for_symbol(symbol)
            for attempt in range(2):
                try:
                    chs = self.client.get_clearinghouse_state()
                except Exception as e:
                    logger.error("watchdog: clearinghouseState取得失敗(attempt %d/2): %s", attempt + 1, e)
                    continue
                positions = [p["position"] for p in chs.get("assetPositions", [])
                             if abs(float(p["position"].get("szi", 0))) > 1e-12]
                if not positions:
                    break
                for pos in positions:
                    symbol = str(pos["coin"]).split("-")[0].upper()
                    szi = float(pos.get("szi", 0))
                    is_buy_close = szi < 0
                    try:
                        book = self.client.get_l2book(symbol)
                        levels = book.get("levels", [[], []])
                        if not levels[0] or not levels[1]:
                            raise ValueError("l2Book板が空")
                        bid, ask = float(levels[0][0]["px"]), float(levels[1][0]["px"])
                    except Exception as e:
                        logger.error("watchdog: %s のl2Book取得失敗(attempt %d/2): %s", symbol, attempt + 1, e)
                        continue
                    price = ask if is_buy_close else bid
                    try:
                        self.client.place_limit_order(symbol, is_buy_close, price, abs(szi),
                                                       reduce_only=True, tif=self.client.TIF_IOC)
                    except Exception as e:
                        logger.error("watchdog: %s フラット化失敗(attempt %d/2): %s", symbol, attempt + 1, e)
        self._abort_cycle_and_log("watchdog_reset", now)

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
        # clearinghouseStateのcoinは"SOL-USDC"形式(実測)。configの"SOL"表記に正規化して照合する
        positions = {str(p["position"]["coin"]).split("-")[0].upper(): p["position"]
                     for p in chs.get("assetPositions", [])}
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
        # target_size は wire に載る量子化後サイズと一致させる(2026-07-22実測: 未量子化の
        # 0.045149 に対し wire "0.045" が約定し、99.9%充足判定が永遠に満たされず全脚が
        # 60秒待ち→取消レース検知扱いになっていた)
        size_base = float(self.client.quantize_size(base, notional / mid_base))
        size_hedge = float(self.client.quantize_size(hedge, (notional * hedge_ratio) / mid_hedge))

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

        if self.cfg.get("dry_run", True):
            self.state = State.LEAD_RESTING
            logger.info("cycle#%d start: lead=%s %s@%.4f size=%.6f", self.cycle_count, base,
                        "buy" if is_buy_lead else "sell", resting_price, size_base)
            return

        status, oid, fill = self._place_and_identify(base, is_buy_lead, resting_price, size_base)
        if status == "lost":
            self._abort_cycle_lost_oid(base, now)
            return
        lead.oid = oid
        if status == "filled":
            _apply_fill(lead, fill, "maker", closing=False)
        self.state = State.LEAD_RESTING
        logger.info("cycle#%d start: lead=%s %s@%.4f size=%.6f status=%s", self.cycle_count, base,
                    "buy" if is_buy_lead else "sell", resting_price, size_base, status)

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
                _apply_fill(lead, fill, "maker", closing=False)
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
        # 買いはbid・売りはaskにjoin(lead脚と同じ)。逆に置くとpost_onlyが必ずrejectされ
        # 60秒裸→taker化を毎サイクル踏む
        hedge.resting_price = bid if hedge.is_buy_open else ask
        hedge.resting_since = now

        # リード脚が部分約定だった場合に備え、ヘッジは実約定ノーショナル基準でサイズし直す
        lead = self.legs.get(self.cfg["base_symbol"])
        if lead is not None and lead.open_price and lead.target_size:
            lead_notional = lead.target_size * lead.open_price
            mid = (bid + ask) / 2
            hedge.target_size = float(self.client.quantize_size(
                hedge_sym, lead_notional * float(self.cfg["hedge_ratio"]) / mid))

        if self.cfg.get("dry_run", True):
            self.state = State.HEDGED
            return

        status, oid, fill = self._place_and_identify(hedge_sym, hedge.is_buy_open,
                                                       hedge.resting_price, hedge.target_size)
        if status == "lost":
            # リード脚は既に建玉を持っているので、ヘッジ脚の全取消に加えてリード脚もtakerで畳む
            # (裸ポジションを残さない)。
            self._abort_cycle_lost_oid(hedge_sym, now, also_flatten_leg=self.legs.get(self.cfg["base_symbol"]))
            return
        hedge.oid = oid
        if status == "filled":
            _apply_fill(hedge, fill, "maker", closing=False)
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
            # taker化の前にまずタッチへ置き直す(max_requotes回まで)。
            if hedge.open_requotes < int(self.cfg.get("max_requotes", 0)):
                r = self._requote_leg(hedge, bid, ask, "open", now)
                if r == "filled":
                    filled = True
                elif r == "lost":
                    self._abort_cycle_lost_oid(hedge_sym, now,
                                                also_flatten_leg=self.legs.get(self.cfg["base_symbol"]))
                    return
                else:
                    return  # requoted / unknown → 次tickで再評価
            else:
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
            if self.cfg.get("dry_run", True):
                continue
            status, oid, fill = self._place_and_identify(leg.symbol, is_buy_close, leg.close_resting_price,
                                                           leg.target_size, reduce_only=True)
            if status == "lost":
                # unwindは建玉を必ず閉じる必要があるため、cycle abortではなくtakerで即時強制close
                # する(reduce_only IOC。oidが無くても`_convert_leg_to_taker`は成立する)。
                logger.error("unwind close発注のoid特定不能 %s -> taker強制close", leg.symbol)
                self._notify("unwind_oid_lost", "red",
                              f"txflow-bot: unwind close oid特定不能 {leg.symbol} -> taker強制close")
                self._cancel_all_orders_for_symbol(leg.symbol)
                self._convert_leg_to_taker(leg, bid, ask, phase="close")
                if self._abort_reason is None:
                    self._abort_reason = "unwind_oid_lost_taker"
                continue
            leg.close_oid = oid
            if status == "filled":
                _apply_fill(leg, fill, "maker", closing=True)
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
                # close側もまず置き直す。建玉は残るがヘッジ済みなので方向リスクは限定的。
                if leg.close_requotes < int(self.cfg.get("max_requotes", 0)):
                    r = self._requote_leg(leg, bid, ask, "close", now)
                    if r == "filled":
                        filled = True
                    elif r == "lost":
                        logger.error("unwind requoteのoid特定不能 %s -> taker強制close", leg.symbol)
                        self._cancel_all_orders_for_symbol(leg.symbol)
                        self._convert_leg_to_taker(leg, bid, ask, phase="close")
                        filled = True
                        if self._abort_reason is None:
                            self._abort_reason = "unwind_oid_lost_taker"
                else:
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
                # clearinghouseStateのcoinは"SOL-USDC"形式(実測)。configの"SOL"表記に正規化して照合する
                positions = {str(p["position"]["coin"]).split("-")[0].upper(): p["position"]
                             for p in chs.get("assetPositions", [])}
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

    def _requote_leg(self, leg: _Leg, bid: float, ask: float, phase: str, now: float) -> str:
        """timeoutしたmaker脚を、即taker化せず現在のタッチへ置き直す(2026-07-23)。
        timeoutの主因は「価格が動いて自分の指値がタッチから外れた」ことなので、
        置き直せばmakerのまま約定する余地が残る。taker化は最後の手段。

        戻り値: "filled"(取消時に既に約定/置き直し直後に約定) | "requoted" |
                "unknown"(取消結果不明、今tickは何もしない) | "lost"(oid同定不能)
        """
        is_open = phase == "open"
        is_buy = leg.is_buy_open if is_open else not leg.is_buy_open
        price = bid if is_buy else ask
        oid = leg.oid if is_open else leg.close_oid

        if not self.cfg.get("dry_run", True):
            if oid is not None:
                status, fill = self._cancel_and_verify(leg.symbol, oid)
                if status == "filled":
                    _apply_fill(leg, fill, "maker", closing=not is_open)
                    return "filled"
                if status == "unknown":
                    return "unknown"
            try:
                st, new_oid, fill = self._place_and_identify(
                    leg.symbol, is_buy, price, leg.target_size, reduce_only=not is_open)
            except Exception as e:
                logger.error("requote発注失敗 %s(%s): %s", leg.symbol, phase, e)
                return "lost"
            if st == "lost":
                return "lost"
            if is_open:
                leg.oid = new_oid
            else:
                leg.close_oid = new_oid
            if st == "filled":
                _apply_fill(leg, fill, "maker", closing=not is_open)
                return "filled"

        if is_open:
            leg.resting_price = price
            leg.resting_since = now
            leg.open_requotes += 1
            n = leg.open_requotes
        else:
            leg.close_resting_price = price
            leg.close_resting_since = now
            leg.close_requotes += 1
            n = leg.close_requotes
        logger.info("requote %s(%s) %d回目 -> %.6f", leg.symbol, phase, n, price)
        return "requoted"

    def _convert_leg_to_taker(self, leg: _Leg, bid: float, ask: float, phase: str) -> None:
        """指摘1(2026-07-22): 取消×約定レース対策。cancel_and_verifyが"filled"を返したら
        既にmaker約定していたということなので、taker発注はスキップしてmaker約定として記録する
        (建玉2倍化を防ぐ)。"unknown"の場合は二重発注リスクを避けてこのtickは何もしない。
        oidが不明(_start_unwindの"lost"経路等、呼び出し元が既に該当symbolの全取消を済ませている
        想定)の場合はcancel_and_verifyをスキップしてそのままtaker発注する。"""
        is_open = phase == "open"
        is_buy = leg.is_buy_open if is_open else not leg.is_buy_open
        price = ask if is_buy else bid
        oid = leg.oid if is_open else leg.close_oid

        if not self.cfg.get("dry_run", True):
            if oid is not None:
                status, fill = self._cancel_and_verify(leg.symbol, oid)
                if status == "filled":
                    _apply_fill(leg, fill, "maker", closing=not is_open)
                    return
                if status == "unknown":
                    return
            resp = None
            err = None
            try:
                resp = self.client.place_limit_order(leg.symbol, is_buy, price, leg.target_size,
                                                       reduce_only=not is_open, tif=self.client.TIF_IOC)
            except Exception as e:
                err = e
            failed = err is not None or not isinstance(resp, dict) or resp.get("status") != "ok"
            if failed and not is_open:
                # 指摘2(2026-07-22事故2): close側の失敗(HTTP例外 or status=="err")は
                # 「実は既にポジションが無かった」ケースがありうる(reduce-only対象なしエラー等)。
                # ここでclose_filledが立たないまま放置するとUNWINDから永遠に抜けられなくなる
                # (実測: 50秒以上スタック)。実ポジションを直接確認し、既にゼロなら
                # 「既に閉じていた」とみなして正常にclose_filledを立てて抜ける。
                szi = self._position_szi(leg.symbol)
                if szi is not None and abs(szi) < 1e-9:
                    logger.warning("taker close失敗(%s)だが実ポジションは既にゼロ %s -> close済みとして扱う",
                                    err if err else resp.get("response"), leg.symbol)
                    fill = {"px": price, "sz": 0.0, "fee": 0.0}
                    _apply_fill(leg, fill, "taker", closing=True)
                    return
            if failed:
                detail = err if err else (resp.get("response") if isinstance(resp, dict) else resp)
                logger.error("taker強制close発注失敗 %s: %s", leg.symbol, detail)
                self._notify("taker_force_close_failed", "red",
                              f"txflow-bot: taker強制close失敗 {leg.symbol}: {detail}。手動対応要")
                return  # legは更新しない(次tickで再試行される)

        fill = {"px": price, "sz": leg.target_size, "fee": _fee(leg.notional(price), is_maker=False)}
        _apply_fill(leg, fill, "taker", closing=not is_open)

    def _position_szi(self, symbol: str) -> Optional[float]:
        """指摘2: clearinghouseStateから実ポジションのszi(符号付きサイズ)を直接確認する。
        取得失敗時はNone(=判定不能、呼び出し側は安全側=既存の未クローズ扱いを維持すること)。"""
        try:
            chs = self.client.get_clearinghouse_state()
        except Exception as e:
            logger.warning("ポジション確認失敗(clearinghouseState) %s: %s", symbol, e)
            return None
        positions = {str(p["position"]["coin"]).split("-")[0].upper(): p["position"]
                     for p in chs.get("assetPositions", [])}
        pos = positions.get(symbol.upper())
        if not pos:
            return 0.0
        try:
            return float(pos.get("szi", 0))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ live-mode helpers
    # 2026-07-22事故2: openOrdersの伝播ラグは実測2-3秒あり、従来1.5秒(5回×0.3秒)では
    # 不足するケースがあった。8回×0.5秒(計4秒)へ延長。
    OID_POLL_ATTEMPTS = 8
    OID_POLL_INTERVAL_SEC = 0.5

    def _place_and_identify(self, symbol: str, is_buy: bool, price: float, size: float,
                             reduce_only: bool = False) -> tuple[str, Optional[int], Optional[dict]]:
        """指摘2(2026-07-22事故対応): 発注してcloidでoidを特定する。
        従来の`_find_oid_by_price`(symbol+side+価格文字列一致)はopenOrdersが返す
        limitPxの文字列表現がこちらの計算と食い違うと一致に失敗し、oid特定不能->約定検知
        不能->取消不能のまま注文が板に堆積する事故(2026-07-22 19:35-19:48 BTC)を起こした。
        cloid(実測: txflowはUUID4文字列を受理。HL標準の0x+32hex形式は"Invalid cloid format"
        で拒否される)は完全一致でしか回収しないため、価格の丸め・文字列表現差に影響されない。

        戻り値:
          ("resting", oid, None)  — openOrdersでcloid一致、resting確認
          ("filled", None, fill)  — openOrdersに出てこない(post-onlyでも即時約定した可能性)。
                                     userFillsで発注時刻以降のsymbol+side一致を確認できた。
                                     fill = {"px","sz","fee"} (float)
          ("lost", None, None)    — どちらでも同定できず。呼び出し元は該当symbolの全open order
                                     取消(`_cancel_all_orders_for_symbol`)とcycle中断が必須。
        """
        cloid = self.client.new_cloid()
        placed_at_ms = int(time.time() * 1000)
        try:
            self.client.place_limit_order(symbol, is_buy, price, size, reduce_only=reduce_only,
                                           tif=self.client.TIF_POST_ONLY, cloid=cloid)
            self._error_streak = 0
        except Exception as e:
            self._error_streak += 1
            logger.error("place_limit_order失敗 %s: %s", symbol, e)
            if self._error_streak >= 3:
                self._notify("error_streak", "red", f"txflow-bot: 発注エラー連発({self._error_streak}回): {e}")
            raise

        for _ in range(self.OID_POLL_ATTEMPTS):
            time.sleep(self.OID_POLL_INTERVAL_SEC)
            try:
                open_orders = self.client.get_open_orders()
            except Exception as e:
                logger.warning("openOrders確認失敗(cloid特定): %s", e)
                continue
            for o in open_orders or []:
                if o.get("cloid") == cloid:
                    return "resting", o.get("oid"), None

        side = "B" if is_buy else "A"
        try:
            fills = self.client.get_user_fills()
        except Exception as e:
            logger.warning("userFills確認失敗(oid特定フォールバック): %s", e)
            fills = []
        matched = [f for f in (fills or [])
                   if str(f.get("coin", "")).upper() == symbol.upper() and f.get("side") == side
                   and int(f.get("time", 0)) >= placed_at_ms]
        if matched:
            total_sz = sum(float(f.get("sz", 0)) for f in matched)
            weighted_px = sum(float(f.get("sz", 0)) * float(f.get("px", 0)) for f in matched) / total_sz
            total_fee = sum(float(f.get("fee", 0)) for f in matched)
            return "filled", None, {"px": weighted_px, "sz": total_sz, "fee": total_fee}

        logger.error("発注後、openOrders/userFillsどちらでも同定できず(cloid=%s symbol=%s)。cycle中断",
                     cloid, symbol)
        return "lost", None, None

    def _cancel_all_orders_for_symbol(self, symbol: str) -> None:
        """指摘2/3: oid特定不能時・起動時リコンサイルで使う、symbol指定の全open order取消。"""
        try:
            open_orders = self.client.get_open_orders()
        except Exception as e:
            logger.error("全取消: openOrders取得失敗 %s: %s", symbol, e)
            return
        for o in open_orders or []:
            coin = str(o.get("coin", "")).split("-")[0].upper()
            if coin != symbol.upper():
                continue
            try:
                self.client.cancel_order(symbol, o.get("oid"))
            except Exception as e:
                logger.error("全取消失敗 %s oid=%s: %s", symbol, o.get("oid"), e)

    def _abort_cycle_lost_oid(self, symbol: str, now: float,
                               also_flatten_leg: Optional[_Leg] = None) -> None:
        """指摘2: 発注のoid同定に失敗した場合の安全策。該当symbolの全open orderを取消して
        サイクルを中断しIDLEへ戻る。also_flatten_leg が渡された場合(ヘッジ脚の同定失敗時、
        リード脚は既に建玉を持っている)はそのlegもtakerで即時flattenし裸ポジションを残さない。"""
        logger.error("oid特定不能のためcycle中断、%sの全open orderを取消", symbol)
        self._notify("oid_lost", "red", f"txflow-bot: oid特定不能でcycle中断、{symbol}の全open orderを取消")
        self._cancel_all_orders_for_symbol(symbol)
        if also_flatten_leg is not None and also_flatten_leg.open_filled and not also_flatten_leg.close_filled:
            logger.error("ヘッジ脚同定失敗によりリード脚%sをtakerで強制close", also_flatten_leg.symbol)
            best = self._best(also_flatten_leg.symbol)
            if best is not None:
                bid, ask = best
                is_buy_close = not also_flatten_leg.is_buy_open
                price = ask if is_buy_close else bid
                try:
                    self.client.place_limit_order(also_flatten_leg.symbol, is_buy_close, price,
                                                   also_flatten_leg.target_size, reduce_only=True,
                                                   tif=self.client.TIF_IOC)
                except Exception as e:
                    logger.error("リード脚強制close失敗: %s", e)
                    self._notify("lead_force_close_failed", "red",
                                  f"txflow-bot: リード脚強制closeに失敗 {also_flatten_leg.symbol}: {e}。手動対応要")
        self._abort_cycle_and_log("oid_identification_lost", now)

    def startup_reconcile(self) -> None:
        """指摘3(2026-07-22事故対応): 起動時リコンサイル。configのsymbolに限らず
        **全open order取消**+**全建玉のreduce-only IOCフラット化**を行う。
        事故: BTCからSOLへconfig切替して再起動した際、botが見ていないBTC建玉0.0030が
        裸で残った(手動決済済み)。configで見えるsymbolだけを見る`_reconcile_stranded_legs`
        (毎IDLE tick、稼働中の軽量チェック用)だけでは再発を防げないため、起動時は
        symbol不問の全件スイープを別途行う。dry_runでは何もしない。"""
        if self.cfg.get("dry_run", True):
            return

        try:
            open_orders = self.client.get_open_orders()
        except Exception as e:
            logger.error("startup_reconcile: openOrders取得失敗: %s", e)
            open_orders = []
        if open_orders:
            logger.warning("startup_reconcile: 起動時にopen order %d件を検出 -> 全取消", len(open_orders))
            self._notify("startup_reconcile", "orange",
                          f"txflow-bot起動: open order {len(open_orders)}件を検出、全取消します")
        for o in open_orders or []:
            coin = str(o.get("coin", "")).split("-")[0].upper()
            try:
                self.client.cancel_order(coin, o.get("oid"))
            except Exception as e:
                logger.error("startup_reconcile: cancel失敗 %s oid=%s: %s", coin, o.get("oid"), e)

        try:
            chs = self.client.get_clearinghouse_state()
        except Exception as e:
            logger.error("startup_reconcile: clearinghouseState取得失敗: %s", e)
            return
        positions = [p["position"] for p in chs.get("assetPositions", [])
                     if abs(float(p["position"].get("szi", 0))) > 1e-12]
        if positions:
            symbols = [p["coin"] for p in positions]
            logger.warning("startup_reconcile: 起動時に建玉%d件を検出(%s) -> reduce-only IOCでフラット化",
                            len(positions), symbols)
            self._notify("startup_reconcile", "red",
                          f"txflow-bot起動: 建玉{len(positions)}件検出({symbols})、reduce-only IOCでフラット化します")
        for pos in positions:
            # coinは"BTC-USDC"形式。client系API(coin_index)は"BTC"表記を取るため正規化
            symbol = str(pos["coin"]).split("-")[0].upper()
            szi = float(pos.get("szi", 0))
            is_buy_close = szi < 0
            try:
                book = self.client.get_l2book(symbol)
                levels = book.get("levels", [[], []])
                if not levels[0] or not levels[1]:
                    raise ValueError("l2Book板が空")
                bid, ask = float(levels[0][0]["px"]), float(levels[1][0]["px"])
            except Exception as e:
                logger.error("startup_reconcile: %s のl2Book取得失敗、フラット化不能: %s", symbol, e)
                self._notify("startup_reconcile_failed", "red",
                              f"txflow-bot: {symbol}建玉のフラット化に失敗(板取得不能)。手動対応要")
                continue
            price = ask if is_buy_close else bid
            try:
                self.client.place_limit_order(symbol, is_buy_close, price, abs(szi),
                                               reduce_only=True, tif=self.client.TIF_IOC)
            except Exception as e:
                logger.error("startup_reconcile: %s フラット化失敗: %s", symbol, e)
                self._notify("startup_reconcile_failed", "red",
                              f"txflow-bot: {symbol}建玉のフラット化に失敗: {e}。手動対応要")

    def _check_live_fill(self, leg: _Leg) -> bool:
        """live mode: oidに紐づくuserFillsを合算して目標サイズに達したかで判定する
        (2026-07-22事故対応: openOrdersの"消えたら約定"判定は`_find_oid_by_price`の
        価格文字列不一致で機能しなかった前例があるため廃止。fills直接参照に統一)。
        部分約定は`_find_fill`側でsize合算・サイズ加重平均価格にしている。"""
        if leg.open_filled or leg.oid is None:
            return leg.open_filled
        fill = self._find_fill(leg.symbol, leg.oid)
        if fill is None or fill["sz"] < leg.target_size * 0.999:
            return False  # 未約定 or 部分約定継続中
        leg.open_price = fill["px"]
        leg.open_type = "maker"
        leg.open_fee = fill["fee"]
        leg.open_filled = True
        return True

    def _check_live_fill_close(self, leg: _Leg) -> bool:
        if leg.close_filled or leg.close_oid is None:
            return leg.close_filled
        fill = self._find_fill(leg.symbol, leg.close_oid)
        if fill is None or fill["sz"] < leg.target_size * 0.999:
            return False
        leg.close_price = fill["px"]
        leg.close_type = "maker"
        leg.close_fee = fill["fee"]
        leg.close_filled = True
        return True

    def _find_fill(self, symbol: str, oid: int) -> Optional[dict]:
        """oidに紐づく約定を集約する(指摘2: 部分約定はsize合算で判定)。
        txflowのuserFillsは部分約定ごとに1レコード返す(同一oidの複数fillを実測確認)ため、
        size合計・サイズ加重平均価格・手数料合計にまとめて返す。
        戻り値: {"px": float, "sz": float, "fee": float} | None(まだ1件も無い場合)。"""
        try:
            fills = self.client.get_user_fills()
        except Exception as e:
            logger.warning("userFills取得失敗: %s", e)
            return None
        matched = [f for f in (fills or []) if f.get("oid") == oid]
        if not matched:
            return None
        total_sz = sum(float(f.get("sz", 0)) for f in matched)
        if total_sz <= 0:
            return None
        weighted_px = sum(float(f.get("sz", 0)) * float(f.get("px", 0)) for f in matched) / total_sz
        total_fee = sum(float(f.get("fee", 0)) for f in matched)
        return {"px": weighted_px, "sz": total_sz, "fee": total_fee}

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
        # 取消成功直後でも openOrders に旧注文が残って見える伝播ラグあり(2026-07-22実測:
        # cancel成功→即時照会で残存ERROR→次tickでは消えていた)。リトライしてから判定する。
        still_open = True
        for attempt in range(3):
            try:
                open_orders = self.client.get_open_orders()
            except Exception as e:
                logger.warning("取消後のopenOrders確認に失敗: %s", e)
                return ("unknown", None)
            still_open = any(o.get("oid") == oid for o in (open_orders or []))
            if not still_open:
                break
            time.sleep(0.7)
        if still_open:
            logger.error("取消後もopenOrdersに残存 oid=%s %s -> 手動確認要", oid, symbol)
            self._notify("cancel_race", "red", f"txflow-bot: 取消後もopenOrdersに残存 oid={oid} {symbol}")
            return ("unknown", None)
        fill = self._find_fill(symbol, oid)
        if fill is not None and fill["sz"] > 0:
            logger.warning("取消×約定レース検出: oid=%s %s は取消前に約定済みだった(sz=%s) -> maker約定として扱う",
                            oid, symbol, fill["sz"])
            return ("filled", fill)
        return ("canceled", None)

    # ------------------------------------------------------------------ ledger
    def _append_ledger(self, rec: dict) -> None:
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info("cycle#%d flat: volume=$%.2f net_pnl=$%.4f abort=%s", rec["cycle"],
                    rec["volume_usd"], rec["net_pnl_usd"], rec["abort_reason"])
