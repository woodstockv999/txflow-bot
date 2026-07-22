"""hedge_bot.py のユニットテスト(ネットワーク不要、フェイクclientを使う)。

実弾稼働事故(2026-07-22 19:35-19:48 BTC)対応の回帰テスト中心:
oidの同定(cloidベース)・部分約定のsize合算・起動時の全symbol取消/フラット化。
"""

import os
import sys
import time as _time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.hedge_bot import PairHedgeBot, State, _Leg, _apply_fill


class FakeClient:
    """openOrders/userFills/cancel_order/place_limit_order/get_clearinghouse_state/get_l2book
    を最小限モックする。TIF_*/new_cloid/quantize_priceはTxflowClientの実挙動をそのまま使う。"""

    TIF_GTC = "gtc"
    TIF_POST_ONLY = "post_only"
    TIF_IOC = "ioc"

    def __init__(self):
        self.open_orders: list[dict] = []
        self.fills: list[dict] = []
        self.canceled_oids: list[int] = []
        self.placed: list[dict] = []
        self._next_oid = 1000
        self.clearinghouse_state = {"assetPositions": []}
        self.books = {}
        self.place_status = "ok"  # テストから"err"に切り替えてfailure経路を再現する
        self.place_err_message = "mock error"

    def new_cloid(self) -> str:
        import uuid

        return str(uuid.uuid4())

    def quantize_price(self, symbol, price):
        return f"{price:.2f}"

    def quantize_size(self, symbol, size):
        return f"{size:.4f}"

    def price_decimals(self, symbol):
        bk = self.books.get(symbol.upper())
        if bk and bk["levels"][0]:
            px = str(bk["levels"][0][0]["px"])
            return len(px.split(".")[1]) if "." in px else 0
        return 2

    def price_tick(self, symbol):
        return 10 ** -self.price_decimals(symbol)

    def place_limit_order(self, symbol, is_buy, price, size, reduce_only=False, tif="post_only", cloid=None):
        oid = self._next_oid
        self._next_oid += 1
        self.placed.append({"symbol": symbol, "is_buy": is_buy, "price": price, "size": size,
                             "reduce_only": reduce_only, "tif": tif, "cloid": cloid, "oid": oid})
        status = getattr(self, "place_status", "ok")
        if status != "ok":
            return {"status": "err", "response": getattr(self, "place_err_message", "mock error")}
        if reduce_only:
            # reduce-only成功をclearinghouse_stateに反映する(watchdog等のretry-until-flatが
            # 現実的に収束することをテストできるようにする簡易シミュレーション)。
            positions = self.clearinghouse_state.get("assetPositions", [])
            self.clearinghouse_state["assetPositions"] = [
                p for p in positions if str(p["position"]["coin"]).split("-")[0].upper() != symbol.upper()
            ]
        return {"status": "ok"}

    def get_open_orders(self):
        return list(self.open_orders)

    def get_user_fills(self):
        return list(self.fills)

    def cancel_order(self, symbol, oid):
        self.canceled_oids.append(oid)
        self.open_orders = [o for o in self.open_orders if o.get("oid") != oid]
        return {"status": "ok"}

    def get_clearinghouse_state(self):
        return self.clearinghouse_state

    def get_l2book(self, symbol):
        return self.books.get(symbol, {"levels": [[], []]})


class FakeWS:
    def __init__(self, books=None):
        self.books = books or {}

    def best_bid_ask(self, symbol):
        return self.books.get(symbol.upper())


def _bot(cfg_overrides=None, client=None, ws=None, tmp_path="/tmp/hedge_bot_test_ledger.jsonl"):
    cfg = {
        "base_symbol": "BTC", "hedge_symbol": "ETH", "notional_usd": 100, "hedge_ratio": 0.87,
        "hold_seconds": 3.0, "leg_timeout_seconds": 60, "leverage": 3, "daily_loss_limit_usd": 5.0,
        "dry_run": False, "enabled": True,
    }
    if cfg_overrides:
        cfg.update(cfg_overrides)
    client = client or FakeClient()
    ws = ws or FakeWS()
    notified = []
    bot = PairHedgeBot(cfg, client, ws, tmp_path, notify_fn=lambda *a, **k: notified.append(a))
    bot._notifications = notified
    return bot


def _rest_placed_orders(bot, coin):
    """置いた注文がopenOrdersにcloid付きで載る挙動をFakeClientに持たせる。"""
    orig = bot.client.place_limit_order

    def place_and_rest(symbol, is_buy, price, size, reduce_only=False, tif="post_only", cloid=None):
        resp = orig(symbol, is_buy, price, size, reduce_only, tif, cloid)
        if tif == bot.client.TIF_POST_ONLY:
            bot.client.open_orders.append({"coin": coin, "oid": bot.client._next_oid - 1,
                                            "cloid": cloid})
        return resp

    bot.client.place_limit_order = place_and_rest


# ------------------------------------------------------------------ _apply_fill
def test_apply_fill_open_sets_maker_fields():
    leg = _Leg(symbol="BTC", is_buy_open=True, target_size=0.01)
    _apply_fill(leg, {"px": 65000.0, "sz": 0.01, "fee": 0.01}, "maker", closing=False)
    assert leg.open_filled is True
    assert leg.open_price == 65000.0
    assert leg.open_type == "maker"
    assert leg.open_fee == 0.01


def test_apply_fill_close_sets_close_fields():
    leg = _Leg(symbol="BTC", is_buy_open=True, target_size=0.01)
    _apply_fill(leg, {"px": 65100.0, "sz": 0.01, "fee": 0.01}, "taker", closing=True)
    assert leg.close_filled is True
    assert leg.close_price == 65100.0
    assert leg.close_type == "taker"


# ------------------------------------------------------------------ _find_fill (指摘2: 部分約定合算)
def test_find_fill_aggregates_partial_fills_by_size_weighted_price():
    bot = _bot()
    bot.client.fills = [
        {"oid": 42, "px": "65000.0", "sz": "0.001", "fee": "0.001"},
        {"oid": 42, "px": "65010.0", "sz": "0.0005", "fee": "0.0005"},
        {"oid": 99, "px": "1.0", "sz": "1.0", "fee": "0.01"},  # 別oid、混入しないこと
    ]
    fill = bot._find_fill("BTC", 42)
    assert fill is not None
    assert abs(fill["sz"] - 0.0015) < 1e-12
    expected_px = (65000.0 * 0.001 + 65010.0 * 0.0005) / 0.0015
    assert abs(fill["px"] - expected_px) < 1e-6
    assert abs(fill["fee"] - 0.0015) < 1e-12


def test_find_fill_returns_none_when_no_matching_oid():
    bot = _bot()
    bot.client.fills = [{"oid": 1, "px": "1", "sz": "1", "fee": "0"}]
    assert bot._find_fill("BTC", 999) is None


def test_check_live_fill_waits_for_full_size_before_marking_filled():
    """部分約定継続中(目標サイズ未達)はfilled扱いにしないことの確認。"""
    bot = _bot()
    leg = _Leg(symbol="BTC", is_buy_open=True, target_size=0.01)
    leg.oid = 55
    bot.client.fills = [{"oid": 55, "px": "65000.0", "sz": "0.004", "fee": "0.001"}]
    assert bot._check_live_fill(leg) is False  # 0.004 < 0.01
    bot.client.fills.append({"oid": 55, "px": "65001.0", "sz": "0.006", "fee": "0.001"})
    assert bot._check_live_fill(leg) is True  # 合計0.010 >= 0.01(0.999倍しきい値)
    assert leg.open_filled is True


# ------------------------------------------------------------------ _place_and_identify (指摘2)
def test_place_and_identify_resting_via_cloid_match(monkeypatch):
    monkeypatch.setattr("src.hedge_bot.time.sleep", lambda *_: None)
    bot = _bot()

    orig_place = bot.client.place_limit_order

    def place_and_rest(symbol, is_buy, price, size, reduce_only=False, tif="post_only", cloid=None):
        resp = orig_place(symbol, is_buy, price, size, reduce_only, tif, cloid)
        bot.client.open_orders.append({"coin": "BTC-USDC", "oid": bot.client._next_oid - 1,
                                        "cloid": cloid, "side": "B", "limitPx": "65000.00"})
        return resp

    bot.client.place_limit_order = place_and_rest
    status, oid, fill = bot._place_and_identify("BTC", True, 65000.0, 0.001)
    assert status == "resting"
    assert oid is not None
    assert fill is None


def test_place_and_identify_filled_via_user_fills_fallback(monkeypatch):
    """openOrdersに出てこない(post-onlyでも即時約定した)場合、userFillsで発注時刻以降・
    symbol・side一致を確認して"filled"を返すこと。"""
    monkeypatch.setattr("src.hedge_bot.time.sleep", lambda *_: None)
    bot = _bot()

    def place_then_fill(symbol, is_buy, price, size, reduce_only=False, tif="post_only", cloid=None):
        bot.client.fills.append({"coin": "BTC", "side": "B", "px": "65000.0", "sz": str(size),
                                  "fee": "0.001", "time": int(_time.time() * 1000) + 1})
        return {"status": "ok"}

    bot.client.place_limit_order = place_then_fill
    status, oid, fill = bot._place_and_identify("BTC", True, 65000.0, 0.001)
    assert status == "filled"
    assert oid is None
    assert fill is not None
    assert fill["sz"] > 0


def test_place_and_identify_lost_when_neither_resting_nor_filled(monkeypatch):
    monkeypatch.setattr("src.hedge_bot.time.sleep", lambda *_: None)
    bot = _bot()
    status, oid, fill = bot._place_and_identify("BTC", True, 65000.0, 0.001)
    assert status == "lost"
    assert oid is None
    assert fill is None


# ------------------------------------------------------------------ 指摘2: 全取消
def test_cancel_all_orders_for_symbol_only_cancels_matching_symbol():
    bot = _bot()
    bot.client.open_orders = [
        {"coin": "BTC-USDC", "oid": 1},
        {"coin": "ETH-USDC", "oid": 2},
        {"coin": "BTC-USDC", "oid": 3},
    ]
    bot._cancel_all_orders_for_symbol("BTC")
    assert sorted(bot.client.canceled_oids) == [1, 3]


# ------------------------------------------------------------------ 指摘3: 起動時リコンサイル
def test_startup_reconcile_cancels_all_orders_regardless_of_configured_symbols():
    bot = _bot()
    bot.client.open_orders = [
        {"coin": "SOL-USDC", "oid": 10},
        {"coin": "DOGE-USDC", "oid": 11},
    ]
    bot.startup_reconcile()
    assert sorted(bot.client.canceled_oids) == [10, 11]


def test_startup_reconcile_flattens_stray_position_outside_configured_pair():
    """事故再現ケース: config=BTC/ETHでも、SOLなど設定外symbolの建玉があればフラット化すること。"""
    bot = _bot(cfg_overrides={"base_symbol": "SOL", "hedge_symbol": "ETH"})
    bot.client.clearinghouse_state = {
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "0.0030"}},  # config外(事故と同じ状況)
            {"position": {"coin": "SOL", "szi": "0"}},  # フラット、対象外
        ]
    }
    bot.client.books["BTC"] = {"levels": [[{"px": "65000.0"}], [{"px": "65001.0"}]]}
    bot.startup_reconcile()
    assert len(bot.client.placed) == 1
    order = bot.client.placed[0]
    assert order["symbol"] == "BTC"
    assert order["reduce_only"] is True
    assert order["is_buy"] is False  # ロング(szi>0)なので売りでクローズ
    assert order["tif"] == bot.client.TIF_IOC


def test_startup_reconcile_noop_in_dry_run():
    bot = _bot(cfg_overrides={"dry_run": True})
    bot.client.open_orders = [{"coin": "BTC-USDC", "oid": 1}]
    bot.startup_reconcile()
    assert bot.client.canceled_oids == []


# ------------------------------------------------------------------ 状態機械の基本(dry_run)
def test_dry_run_cycle_runs_without_network():
    # スプレッドゼロにしてtouchシミュレーションを即成立させ、hold_secondsを短くして
    # 実時間待ちを最小化する(状態機械がIDLE->...->FLAT->IDLEまで一周することの確認)。
    ws = FakeWS(books={"BTC": (65000.0, 65000.0), "ETH": (1900.0, 1900.0)})
    bot = _bot(cfg_overrides={"dry_run": True, "hold_seconds": 0.05, "leg_timeout_seconds": 5}, ws=ws)
    for _ in range(200):
        bot.tick()
        if bot.cycle_count >= 1:
            break
        _time.sleep(0.01)
    assert bot.cycle_count >= 1
    assert bot.state in (State.IDLE, State.LEAD_RESTING)


# ------------------------------------------------------------------ 指摘2(事故2): taker close失敗時のポジション確認
def test_convert_leg_to_taker_close_err_response_but_position_flat_still_progresses():
    """taker強制closeの応答がstatus=="err"でも、実ポジションが既にゼロなら
    close_filled=Trueで正常に抜けること(2026-07-22事故2: ここが立たずUNWINDに50秒以上
    スタックした)。"""
    bot = _bot()
    bot.client.place_status = "err"
    bot.client.place_err_message = "mock: nothing to reduce"
    bot.client.clearinghouse_state = {"assetPositions": []}  # 実ポジションは既にゼロ

    leg = _Leg(symbol="ETH", is_buy_open=True, target_size=0.5)
    leg.close_oid = None  # oid不明(lost経由を模す)
    bot._convert_leg_to_taker(leg, bid=1900.0, ask=1900.5, phase="close")

    assert leg.close_filled is True
    assert leg.close_type == "taker"
    assert leg.close_price is not None


def test_convert_leg_to_taker_close_err_response_and_position_still_open_does_not_mark_filled():
    """比較対照: 応答がerrで実ポジションもまだ残っている場合はclose_filledを立てず、
    次tickの通常timeout経路での再試行に委ねること(=無限フォールス成功を防ぐ)。"""
    bot = _bot()
    bot.client.place_status = "err"
    bot.client.clearinghouse_state = {
        "assetPositions": [{"position": {"coin": "ETH-USDC", "szi": "0.5"}}]
    }
    leg = _Leg(symbol="ETH", is_buy_open=True, target_size=0.5)
    leg.close_oid = None
    bot._convert_leg_to_taker(leg, bid=1900.0, ask=1900.5, phase="close")

    assert leg.close_filled is False
    assert len(bot._notifications) == 1
    assert bot._notifications[0][0] == "taker_force_close_failed"


def test_unwind_lost_identification_proceeds_via_taker_close_to_next_cycle(monkeypatch):
    """UNWINDでclose発注のcloid/userFills同定が"lost"でも、taker強制closeが成功応答なら
    close_filledが立ち、両脚揃えばFLAT->IDLEまで進んで次サイクルに進めること。"""
    monkeypatch.setattr("src.hedge_bot.time.sleep", lambda *_: None)
    ws = FakeWS(books={"BTC": (65000.0, 65000.0), "ETH": (1900.0, 1900.0)})
    bot = _bot(ws=ws)  # dry_run=False(既定)

    lead = _Leg(symbol="BTC", is_buy_open=True, target_size=0.001)
    lead.open_filled = True
    lead.open_price = 65000.0
    lead.open_fee = 0.01
    hedge = _Leg(symbol="ETH", is_buy_open=False, target_size=0.02)
    hedge.open_filled = True
    hedge.open_price = 1900.0
    hedge.open_fee = 0.01
    bot.legs = {"BTC": lead, "ETH": hedge}
    bot._cycle_start_ts = _time.time()
    bot.state = State.HOLD
    bot._hold_until = _time.time() - 1  # 即unwindへ

    # openOrders/userFillsは常に空 -> _place_and_identifyは両脚とも"lost"になる
    bot.client.open_orders = []
    bot.client.fills = []

    bot.tick()  # HOLD -> _start_unwind (両脚"lost"->taker強制close->close_filled=True) -> UNWIND
    assert lead.close_filled is True
    assert hedge.close_filled is True

    bot.tick()  # UNWIND -> all_closed=True -> FLAT
    bot.tick()  # FLAT -> _finish_cycle -> IDLE, cycle_count+1

    assert bot.state == State.IDLE
    assert bot.cycle_count == 1


# ------------------------------------------------------------------ 指摘3: watchdog
def test_watchdog_fires_when_state_stuck_beyond_threshold():
    # 閾値 = leg_timeout * (max_requotes+1) * 2 + 120 = 10*2*2+120 = 160秒
    bot = _bot(cfg_overrides={"leg_timeout_seconds": 10, "max_requotes": 1})
    bot.client.open_orders = [{"coin": "BTC-USDC", "oid": 1}, {"coin": "ETH-USDC", "oid": 2}]
    bot.client.clearinghouse_state = {
        "assetPositions": [{"position": {"coin": "ETH", "szi": "-0.42"}}]
    }
    bot.client.books["ETH"] = {"levels": [[{"px": "1900.0"}], [{"px": "1900.5"}]]}

    bot.state = State.UNWIND
    bot.legs = {"BTC": _Leg(symbol="BTC", is_buy_open=True, target_size=0.001)}
    bot._cycle_start_ts = _time.time() - 200  # 閾値160秒を超過

    fired = bot._check_watchdog(_time.time())

    assert fired is True
    assert bot.state == State.IDLE
    assert bot.legs == {}
    assert sorted(bot.client.canceled_oids) == [1, 2]  # 両symbol分の全取消
    # ETHの残ポジション(-0.42、ショート)を買い戻すreduce-only IOC発注が出ていること
    flatten_orders = [o for o in bot.client.placed if o["symbol"] == "ETH"]
    assert len(flatten_orders) == 1
    assert flatten_orders[0]["is_buy"] is True
    assert flatten_orders[0]["reduce_only"] is True
    assert flatten_orders[0]["tif"] == bot.client.TIF_IOC
    assert any(n[0] == "watchdog_reset" for n in bot._notifications)


def test_watchdog_does_not_fire_before_threshold():
    bot = _bot(cfg_overrides={"leg_timeout_seconds": 60})  # 閾値180秒
    bot.state = State.HEDGED
    bot._cycle_start_ts = _time.time() - 10
    assert bot._check_watchdog(_time.time()) is False
    assert bot.state == State.HEDGED


def test_watchdog_ignored_while_idle():
    bot = _bot()
    bot.state = State.IDLE
    bot._cycle_start_ts = _time.time() - 10000
    assert bot._check_watchdog(_time.time()) is False


def test_tick_catches_unexpected_exception_without_crashing(monkeypatch):
    """tick()内の想定外例外がプロセスを落とさずログ+継続すること(事故2で懸念された
    「未捕捉例外でmain.pyのループごと落ちてログが完全に止まる」経路の防止)。"""
    bot = _bot(cfg_overrides={"dry_run": True})

    def boom(now):
        raise RuntimeError("boom")

    bot._tick_inner = boom
    bot.tick()  # 例外を投げずに戻ってくること
    assert bot._error_streak >= 1


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))


# ------------------------------------------------------------------ 2026-07-23: requote前置
def test_hedge_leg_timeout_requotes_before_taker(monkeypatch):
    """hedge脚のtimeoutは、max_requotesに達するまではtaker化せずタッチへ置き直す。"""
    monkeypatch.setattr("src.hedge_bot.time.sleep", lambda *_: None)
    bot = _bot(cfg_overrides={"leg_timeout_seconds": 10, "max_requotes": 2},
               ws=FakeWS(books={"BTC": (65000.0, 65010.0), "ETH": (1900.0, 1900.5)}))
    _rest_placed_orders(bot, "ETH-USDC")
    bot.state = State.HEDGED
    lead = _Leg(symbol="BTC", is_buy_open=True, target_size=0.001)
    lead.open_price, lead.open_filled = 65000.0, True
    hedge = _Leg(symbol="ETH", is_buy_open=False, target_size=0.03)
    hedge.oid = 555
    hedge.resting_price = 1900.0
    hedge.resting_since = _time.time() - 100  # timeout済み
    bot.legs = {"BTC": lead, "ETH": hedge}
    bot.client.open_orders = [{"coin": "ETH-USDC", "oid": 555}]

    bot._drive_hedged(_time.time())

    # taker(IOC)ではなくpost_onlyで置き直されている
    assert hedge.open_requotes == 1
    assert hedge.open_filled is False
    assert [o["tif"] for o in bot.client.placed] == [bot.client.TIF_POST_ONLY]
    assert 555 in bot.client.canceled_oids
    assert bot.state == State.HEDGED


def test_hedge_leg_converts_to_taker_after_requote_budget_exhausted():
    """max_requotesを使い切ったらtaker化する(従来動作へフォールバック)。"""
    bot = _bot(cfg_overrides={"leg_timeout_seconds": 10, "max_requotes": 2},
               ws=FakeWS(books={"BTC": (65000.0, 65010.0), "ETH": (1900.0, 1900.5)}))
    bot.state = State.HEDGED
    lead = _Leg(symbol="BTC", is_buy_open=True, target_size=0.001)
    lead.open_price, lead.open_filled = 65000.0, True
    hedge = _Leg(symbol="ETH", is_buy_open=False, target_size=0.03)
    hedge.oid = 777
    hedge.resting_price = 1900.0
    hedge.resting_since = _time.time() - 100
    hedge.open_requotes = 2  # 使い切り済み
    bot.legs = {"BTC": lead, "ETH": hedge}
    bot.client.open_orders = [{"coin": "ETH-USDC", "oid": 777}]

    bot._drive_hedged(_time.time())

    assert hedge.open_type == "taker"
    assert bot._abort_reason == "hedge_leg_timeout_taker"
    assert bot.client.placed[-1]["tif"] == bot.client.TIF_IOC


def test_requote_treats_cancel_race_fill_as_maker_fill():
    """置き直しの取消時に既に約定していたら、二重発注せずmaker約定として扱う。"""
    bot = _bot(cfg_overrides={"leg_timeout_seconds": 10, "max_requotes": 2})
    hedge = _Leg(symbol="ETH", is_buy_open=False, target_size=0.03)
    hedge.oid = 888
    hedge.resting_since = _time.time() - 100
    bot.client.open_orders = []  # 取消後もopenOrdersに無い
    bot.client.fills = [{"coin": "ETH", "oid": 888, "px": "1900.5", "sz": "0.03",
                         "fee": "0.01", "side": "A", "time": int(_time.time() * 1000)}]

    r = bot._requote_leg(hedge, 1900.0, 1900.5, "open", _time.time())

    assert r == "filled"
    assert hedge.open_type == "maker"
    assert hedge.open_requotes == 0
    assert bot.client.placed == []  # 置き直し発注をしていない


# ------------------------------------------------------------------ 2026-07-23: 稼働時間ゲート
def test_active_hours_gate_blocks_new_cycle_but_not_inflight():
    bot = _bot(cfg_overrides={"active_hours": {"start_hour": 3, "end_hour": 24}})
    h3 = _time.mktime(_time.localtime()[:3] + (3, 30, 0) + _time.localtime()[6:])
    h1 = _time.mktime(_time.localtime()[:3] + (1, 30, 0) + _time.localtime()[6:])
    assert bot._within_active_hours(h3) is True
    assert bot._within_active_hours(h1) is False

    # 時間外のIDLEでは新規サイクルを開始しない
    bot.state = State.IDLE
    bot._tick_inner(h1)
    assert bot.client.placed == []
    assert bot.state == State.IDLE


def test_active_hours_absent_means_always_on():
    bot = _bot()
    assert bot._within_active_hours(_time.time()) is True


# ------------------------------------------------------------------ 2026-07-23: 内側1tick
def test_inside_price_places_one_tick_inside_touch():
    """スプレッド2tick以上なら、買いはbid+tick・売りはask-tickに置いて自分が新ベストになる。"""
    bot = _bot()
    bot.client.books["HBAR"] = {"levels": [[{"px": "0.07151"}], [{"px": "0.07153"}]]}
    assert bot._inside_price("HBAR", True, 0.07151) == 0.07152   # bid+tick
    assert bot._inside_price("HBAR", False, 0.07153) == 0.07152  # ask-tick


def test_inside_price_falls_back_to_touch_when_spread_one_tick():
    """spread=1tick(内側の隙間なし)ならタッチにそのまま置く(クロス防止)。"""
    bot = _bot()
    bot.client.books["ARB"] = {"levels": [[{"px": "0.0906"}], [{"px": "0.0907"}]]}
    assert bot._inside_price("ARB", True, 0.0906) == 0.0906    # bid+tickがask以上→bid
    assert bot._inside_price("ARB", False, 0.0907) == 0.0907   # ask-tickがbid以下→ask


def test_inside_price_falls_back_to_touch_when_book_unavailable():
    bot = _bot()  # FakeClientの既定は空板
    assert bot._inside_price("HBAR", True, 0.0715) == 0.0715


# ------------------------------------------------------------------ 2026-07-23: reduce-onlyサイズ
def test_clamp_size_to_position_limits_reduce_only_size():
    """reduce-onlyのサイズは常に実建玉。target_sizeは取消×約定レースの取りこぼしで
    実建玉と乖離するため信用しない。"""
    bot = _bot()
    bot.client.clearinghouse_state = {"assetPositions": [{"position": {"coin": "ARB-USDC", "szi": "46.0"}}]}
    assert bot._clamp_size_to_position("ARB", 307.0) == 46.0
    # 建玉の方が大きい場合も実建玉を採用する(閉じ残り=stranded legを作らない)
    assert bot._clamp_size_to_position("ARB", 20.0) == 46.0
    # 建玉なし -> 0(呼び出し元が発注をスキップする)
    assert bot._clamp_size_to_position("HBAR", 100.0) == 0.0


def test_taker_close_skips_order_when_already_flat():
    """既にフラットなら、拒否される注文を出さずにclose済みとして扱う。"""
    bot = _bot()
    bot.client.clearinghouse_state = {"assetPositions": []}
    leg = _Leg(symbol="ARB", is_buy_open=True, target_size=307.0)
    leg.open_price, leg.open_filled = 0.09, True

    bot._convert_leg_to_taker(leg, 0.0903, 0.0905, phase="close")

    assert leg.close_filled is True
    assert bot.client.placed == []  # 発注していない


def test_taker_close_uses_actual_position_size():
    bot = _bot()
    bot.client.clearinghouse_state = {"assetPositions": [{"position": {"coin": "ARB-USDC", "szi": "46.0"}}]}
    leg = _Leg(symbol="ARB", is_buy_open=True, target_size=307.0)
    leg.open_price, leg.open_filled = 0.09, True

    bot._convert_leg_to_taker(leg, 0.0903, 0.0905, phase="close")

    assert bot.client.placed[-1]["size"] == 46.0  # target 307 でなく実建玉 46
    assert bot.client.placed[-1]["reduce_only"] is True


def test_place_and_identify_retries_once_when_order_vanishes(monkeypatch):
    """post_onlyがstatus okのまま消えた場合、takerに落とす前に一度置き直す。"""
    monkeypatch.setattr("src.hedge_bot.time.sleep", lambda *_: None)
    bot = _bot()
    calls = []

    def fake_once(symbol, is_buy, price, size, reduce_only=False):
        calls.append(symbol)
        if len(calls) == 1:
            return "lost", None, None
        return "resting", 4242, None

    bot._place_and_identify_once = fake_once
    status, oid, _ = bot._place_and_identify("HBAR", True, 0.0715, 1400)

    assert (status, oid) == ("resting", 4242)
    assert len(calls) == 2


def test_place_and_identify_gives_up_after_attempt_budget(monkeypatch):
    monkeypatch.setattr("src.hedge_bot.time.sleep", lambda *_: None)
    bot = _bot()
    calls = []
    bot._place_and_identify_once = lambda *a, **k: (calls.append(1), ("lost", None, None))[1]

    status, _, _ = bot._place_and_identify("HBAR", True, 0.0715, 1400)

    assert status == "lost"
    assert len(calls) == bot.PLACE_ATTEMPTS


def test_close_fill_completion_uses_actual_placed_size_not_target():
    """reduce-onlyは実建玉基準で発注するためtarget_sizeより大きくなりうる。
    完了判定をtarget_sizeで行うと閉じ残りをclose済みと誤判定しstranded legになる。"""
    bot = _bot()
    leg = _Leg(symbol="ARB", is_buy_open=True, target_size=186.0)
    leg.close_oid = 900
    leg.close_size = 1030.0  # 実建玉基準で置いたサイズ
    bot.client.fills = [{"oid": 900, "px": "0.0904", "sz": "186", "fee": "0.001"}]
    # target_size(186)は満たすが、実際に置いた1030には全く足りない -> 未完了
    assert bot._check_live_fill_close(leg) is False
    bot.client.fills.append({"oid": 900, "px": "0.0904", "sz": "844", "fee": "0.004"})
    assert bot._check_live_fill_close(leg) is True   # 合計1030で完了


def test_close_size_recorded_from_actual_placement():
    bot = _bot(ws=FakeWS(books={"ARB": (0.0903, 0.0905), "HBAR": (0.0715, 0.0716)}))
    _rest_placed_orders(bot, "ARB-USDC")
    bot.client.clearinghouse_state = {"assetPositions": [
        {"position": {"coin": "ARB-USDC", "szi": "1030.0"}}]}
    bot.client.books["ARB"] = {"levels": [[{"px": "0.0903"}], [{"px": "0.0905"}]]}
    leg = _Leg(symbol="ARB", is_buy_open=True, target_size=186.0)
    leg.open_price, leg.open_filled = 0.09, True
    bot.legs = {"ARB": leg}
    bot.state = State.HOLD

    bot._start_unwind(_time.time())

    assert leg.close_size == 1030.0  # target 186 でなく実建玉


def test_unwind_reopens_close_when_position_still_remains():
    """取消×約定レースで部分約定を掴んでclose_filledが立っても、実建玉が残っていれば
    未完了に巻き戻して残量を閉じ直す(stranded legをサイクル外にこぼさない)。"""
    bot = _bot(ws=FakeWS(books={"ARB": (0.0903, 0.0905)}))
    leg = _Leg(symbol="ARB", is_buy_open=True, target_size=1106.0)
    leg.open_price, leg.open_filled = 0.09, True
    leg.close_filled = True          # 313だけ約定して完了扱いになった状態
    leg.close_price, leg.close_fee, leg.close_type = 0.0904, 0.001, "maker"
    leg.close_oid, leg.close_size = 700, 1106.0
    bot.client.clearinghouse_state = {"assetPositions": [
        {"position": {"coin": "ARB-USDC", "szi": "793.0"}}]}   # 残り793

    bot.client.open_orders = [{"coin": "ARB-USDC", "oid": 700}]

    assert bot._verify_leg_flat(leg, _time.time()) is False
    assert leg.close_filled is False   # 巻き戻っている
    assert leg.close_oid is None
    assert leg.close_size is None
    assert 700 in bot.client.canceled_oids  # 板の残注文を孤児にしない


def test_unwind_flat_verification_passes_when_position_zero():
    bot = _bot()
    leg = _Leg(symbol="ARB", is_buy_open=True, target_size=1106.0)
    leg.close_filled = True
    bot.client.clearinghouse_state = {"assetPositions": []}
    assert bot._verify_leg_flat(leg, _time.time()) is True
    assert leg.close_filled is True


def test_verify_leg_flat_ignores_dust_residual():
    """微小残(notional < DUST_USD)は閉じ直さず完了扱いにする。"""
    bot = _bot(ws=FakeWS(books={"ARB": (0.09, 0.0902)}))
    leg = _Leg(symbol="ARB", is_buy_open=True, target_size=1106.0)
    leg.close_filled = True
    bot.client.clearinghouse_state = {"assetPositions": [
        {"position": {"coin": "ARB-USDC", "szi": "-7.0"}}]}  # 7*0.0901 ≒ $0.63 < 5
    assert bot._verify_leg_flat(leg, _time.time()) is True
    assert leg.close_filled is True
    assert bot.client.canceled_oids == []  # 掃除も走らない
