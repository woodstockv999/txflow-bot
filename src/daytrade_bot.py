"""txflow デイトレbot: 米国株perp の「閉場ドリフト継続」戦略。

戦略(根拠 = ~/hlbot-research/txflow-daytrade-20260729/report.md):
  x = 前引け(NY 16:00) → 当寄り(NY 9:30) の perp ドリフト
  y = 当寄り → 当引け
  |x| が最大の1銘柄を寄りで sign(x) 方向に建て、引けで決済する。
  Fama-MacBeth 傾き 0.091 ⇒ 損益分岐 |x| ≈ コスト/0.09 ≈ 120bps。

★このエッジは統計的に未確定(全検定で 平均 < MDE、n=72日が履歴の上限)。
  本botの一次目的は「前進検証のサンプルを実約定コスト込みで貯めること」。
  台帳 data/daytrade.jsonl に x / y / 実約定 / fee を1トレード1行で残す。

設計上の安全側の選択:
- pair_hedge版(main.py)と**同時に動かしてはいけない**。あちらは起動時に口座の全建玉を
  symbol不問でフラット化するため、こちらの建玉を消す。起動時にpm2で検出したら停止する。
- 自分が建てた銘柄以外の建玉には一切触らない(pair_hedge版と逆の方針)。見つけたら警告のみ。
- 状態は data/daytrade_state.json に永続化し、再起動しても保有を引き継ぐ。
- 引け後 exit_grace_minutes を過ぎても閉じられなければ discord red 通知して halt する
  (裸建玉を翌日まで持ち越さない)。
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

LOG = logging.getLogger("txflow-daytrade")

NY = ZoneInfo("America/New_York")
SESSION_OPEN = (9, 30)    # NY現地時刻。DST切替は zoneinfo が吸収する
SESSION_CLOSE = (16, 0)

STATE_FLAT = "FLAT"
STATE_HOLDING = "HOLDING"
STATE_HALTED = "HALTED"


def _utc_of(d: date, hm: tuple[int, int]) -> datetime:
    """NY現地の (日付, 時分) を UTC の datetime にする。"""
    return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=NY).astimezone(timezone.utc)


class DaytradeBot:
    def __init__(self, cfg: dict, client, ledger_path: Path, state_path: Path,
                 notify_fn: Optional[Callable[[str, str, str], None]] = None):
        self.cfg = cfg
        self.client = client
        self.ledger_path = ledger_path
        self.state_path = state_path
        self.notify = notify_fn or (lambda *a: None)
        self.dry_run = bool(cfg.get("dry_run", True))
        self.holidays = set(cfg.get("holidays") or [])
        self.state = self._load_state()

    # ------------------------------------------------------------------ 状態
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                LOG.error("state読込失敗(初期化する): %s", e)
        return {"state": STATE_FLAT, "position": None, "traded_dates": [],
                "daily_pnl_usd": 0.0, "pnl_date": None}

    def _roll_daily_pnl(self, today_ny: date) -> None:
        """日次損失上限は「その日の」損益に対するもの。日付が変わったらリセットする
        (リセットを忘れると生涯累積損失上限になり、いつか必ず永久haltする)。"""
        if self.state.get("pnl_date") != today_ny.isoformat():
            self.state["pnl_date"] = today_ny.isoformat()
            self.state["daily_pnl_usd"] = 0.0
            self._save_state()

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        tmp.replace(self.state_path)

    def _append_ledger(self, row: dict) -> None:
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ カレンダー
    def is_session_day(self, d: date) -> bool:
        return d.weekday() < 5 and d.isoformat() not in self.holidays

    def prev_session_day(self, d: date) -> Optional[date]:
        for i in range(1, 8):
            p = d - timedelta(days=i)
            if self.is_session_day(p):
                return p
        return None

    # ------------------------------------------------------------------ 市場データ
    def _mid_and_depth(self, symbol: str) -> Optional[tuple[float, float, float, float]]:
        """(mid, bid, ask, ±10bps の薄い側の厚み$) を返す。取れなければ None。"""
        try:
            book = self.client.get_l2book(symbol)
        except Exception as e:
            LOG.warning("l2Book失敗 %s: %s", symbol, e)
            return None
        levels = (book or {}).get("levels") or [[], []]
        bids, asks = levels[0], levels[1]
        if not bids or not asks:
            return None
        bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
        if bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2
        lim = mid * 0.001
        dbid = sum(float(l["sz"]) for l in bids if float(l["px"]) >= mid - lim) * mid
        dask = sum(float(l["sz"]) for l in asks if float(l["px"]) <= mid + lim) * mid
        return mid, bid, ask, min(dbid, dask)

    def _prev_close_price(self, symbol: str, prev_close_utc: datetime) -> Optional[float]:
        """前引け時刻の1分足終値。前後5分だけ引いて、時刻以降の最初のバーを使う。"""
        t = int(prev_close_utc.timestamp() * 1000)
        try:
            c = self.client.info("candleSnapshot", req={
                "coin": symbol, "interval": "1m",
                "startTime": t - 5 * 60_000, "endTime": t + 5 * 60_000})
        except Exception as e:
            LOG.warning("candleSnapshot失敗 %s: %s", symbol, e)
            return None
        if not isinstance(c, list) or not c:
            return None
        for b in c:
            if b["t"] >= t:
                return float(b["c"])
        return float(c[-1]["c"])

    # ------------------------------------------------------------------ シグナル
    def build_signal(self, today: date) -> Optional[dict]:
        prev = self.prev_session_day(today)
        if prev is None:
            return None
        prev_close_utc = _utc_of(prev, SESSION_CLOSE)
        excl = set(self.cfg.get("exclude_symbols") or [])
        cands = [s for s in self.cfg["candidates"] if s not in excl]
        min_depth = float(self.cfg["min_depth10bps_usd"])
        rows = []
        for sym in cands:
            md = self._mid_and_depth(sym)
            if not md:
                continue
            mid, bid, ask, depth = md
            if depth < min_depth:
                continue
            pc = self._prev_close_price(sym, prev_close_utc)
            if not pc or pc <= 0:
                continue
            x = math.log(mid / pc) * 1e4
            rows.append({"symbol": sym, "x_bps": x, "mid": mid, "bid": bid, "ask": ask,
                         "depth10bps_usd": depth, "prev_close": pc})
            time.sleep(0.05)
        if not rows:
            LOG.warning("候補ゼロ(板/前引け価格が取れない)")
            return None
        rows.sort(key=lambda r: -abs(r["x_bps"]))
        best = rows[0]
        best["n_candidates"] = len(rows)
        best["runner_up"] = {"symbol": rows[1]["symbol"], "x_bps": rows[1]["x_bps"]} if len(rows) > 1 else None
        return best

    # ------------------------------------------------------------------ 口座
    def _position_szi(self, symbol: str) -> float:
        try:
            st = self.client.get_clearinghouse_state()
        except Exception as e:
            LOG.warning("clearinghouseState失敗: %s", e)
            return float("nan")
        for p in (st or {}).get("assetPositions", []):
            pos = p.get("position", {})
            # coin表記が API ごとに違う(SOL-USDC / SOL)ため必ず正規化する
            if str(pos.get("coin", "")).split("-")[0].upper() == symbol.upper():
                try:
                    return float(pos.get("szi", 0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _account_value(self) -> Optional[float]:
        try:
            st = self.client.get_clearinghouse_state()
            return float(st["marginSummary"]["accountValue"])
        except Exception as e:
            LOG.warning("accountValue取得失敗: %s", e)
            return None

    def _foreign_positions(self, own: Optional[str]) -> list[str]:
        try:
            st = self.client.get_clearinghouse_state()
        except Exception:
            return []
        out = []
        for p in (st or {}).get("assetPositions", []):
            coin = str(p.get("position", {}).get("coin", "")).split("-")[0].upper()
            if coin and coin != (own or "").upper():
                out.append(coin)
        return out

    # ------------------------------------------------------------------ 執行
    def _marketable_ioc(self, symbol: str, is_buy: bool, size: float, slip_bps: float,
                        reduce_only: bool) -> Any:
        md = self._mid_and_depth(symbol)
        if not md:
            raise RuntimeError(f"{symbol}: 板が取れず発注できない")
        mid, bid, ask = md[0], md[1], md[2]
        ref = ask if is_buy else bid
        px = ref * (1 + slip_bps / 1e4) if is_buy else ref * (1 - slip_bps / 1e4)
        cloid = self.client.new_cloid()
        LOG.info("発注 %s %s size=%.6f px=%.6f (mid=%.6f, reduce_only=%s, cloid=%s)",
                 symbol, "BUY" if is_buy else "SELL", size, px, mid, reduce_only, cloid)
        if self.dry_run:
            return {"dry_run": True, "px": px, "mid": mid, "cloid": cloid}
        return self.client.place_limit_order(symbol, is_buy, px, size, reduce_only=reduce_only,
                                             tif=self.client.TIF_IOC, cloid=cloid)

    def _recent_fills(self, symbol: str, since_ms: int) -> Optional[dict]:
        """since_ms 以降の自分の約定を集計する。前進検証の主目的は「実約定コストの実測」なので
        サイズ加重平均価格と手数料合計をここで回収する。
        ★coin表記が API ごとに違う(userFills は "SOL", clearinghouseState は "SOL-USDC")ため
          必ず split("-")[0] で正規化して照合する。"""
        try:
            fills = self.client.get_user_fills()
        except Exception as e:
            LOG.warning("userFills失敗: %s", e)
            return None
        if not isinstance(fills, list):
            return None
        sz_sum = 0.0
        notional = 0.0
        fee_sum = 0.0
        n = 0
        for f in fills:
            try:
                if str(f.get("coin", "")).split("-")[0].upper() != symbol.upper():
                    continue
                if int(f.get("time", 0)) < since_ms:
                    continue
                sz = abs(float(f.get("sz", 0)))
                px = float(f.get("px", 0))
                if sz <= 0 or px <= 0:
                    continue
                sz_sum += sz
                notional += sz * px
                fee_sum += float(f.get("fee", 0) or 0)
                n += 1
            except (TypeError, ValueError):
                continue
        if sz_sum <= 0:
            return None
        return {"avg_px": notional / sz_sum, "size": sz_sum, "fees_usd": fee_sum, "n_fills": n}

    def _avg_entry_px(self, symbol: str) -> Optional[float]:
        try:
            st = self.client.get_clearinghouse_state()
        except Exception:
            return None
        for p in (st or {}).get("assetPositions", []):
            pos = p.get("position", {})
            if str(pos.get("coin", "")).split("-")[0].upper() == symbol.upper():
                try:
                    return float(pos.get("entryPx"))
                except (TypeError, ValueError):
                    return None
        return None

    # ------------------------------------------------------------------ エントリ
    def enter(self, today: date, sig: dict) -> None:
        notional = float(self.cfg["notional_usd"])
        av = self._account_value()
        if av is not None:
            cap = av * float(self.cfg["max_leverage"])
            if notional > cap:
                LOG.warning("notional $%.2f > 口座$%.2f × レバ%.1f = $%.2f。縮小する",
                            notional, av, float(self.cfg["max_leverage"]), cap)
                notional = cap
        if notional < 10:
            LOG.error("建玉が小さすぎる($%.2f)。見送り", notional)
            return

        is_buy = sig["x_bps"] > 0
        size = notional / sig["mid"]
        # サイズは銘柄ごとの sizeDecimals で量子化される。高価格×粗い刻みの銘柄では
        # 量子化後の建玉が目標から大きくずれる(最悪ゼロになる)ので、ここで確認して弾く。
        try:
            q_size = float(self.client.quantize_size(sig["symbol"], size))
        except Exception as e:
            LOG.error("サイズ量子化失敗 %s: %s", sig["symbol"], e)
            return
        q_notional = q_size * sig["mid"]
        if q_size <= 0 or not (0.5 * notional <= q_notional <= 2.0 * notional):
            LOG.error("%s: 量子化後の建玉 $%.2f が目標 $%.2f から乖離。見送り",
                      sig["symbol"], q_notional, notional)
            self.notify("見送り", "orange",
                        f"{sig['symbol']} サイズ量子化で $%.2f になるため見送り" % q_notional)
            return
        size, notional = q_size, q_notional
        since_ms = int(time.time() * 1000) - 5_000
        try:
            resp = self._marketable_ioc(sig["symbol"], is_buy, size,
                                        float(self.cfg["entry_slippage_bps"]), reduce_only=False)
        except Exception as e:
            LOG.error("エントリ発注例外: %s", e)
            self.notify("entry失敗", "red", f"{sig['symbol']} 発注例外: {e}")
            return

        entry_fill = None
        if self.dry_run:
            filled_szi = size if is_buy else -size
            entry_px = resp["px"]
        else:
            time.sleep(2.0)
            filled_szi = self._position_szi(sig["symbol"])
            if filled_szi != filled_szi or filled_szi == 0:   # NaN or 未約定
                LOG.error("エントリ未約定(建玉ゼロ)。応答=%s", resp)
                self.notify("entry未約定", "orange",
                            f"{sig['symbol']} IOCが刺さらず。x={sig['x_bps']:.0f}bps 応答={resp}")
                return
            entry_fill = self._recent_fills(sig["symbol"], since_ms)
            entry_px = (entry_fill or {}).get("avg_px") or self._avg_entry_px(sig["symbol"]) or sig["mid"]

        self.state["state"] = STATE_HOLDING
        self.state["position"] = {
            "date": today.isoformat(), "symbol": sig["symbol"], "is_buy": is_buy,
            "size": abs(filled_szi), "entry_px": entry_px, "entry_mid": sig["mid"],
            "x_bps": sig["x_bps"], "prev_close": sig["prev_close"],
            "depth10bps_usd": sig["depth10bps_usd"], "n_candidates": sig["n_candidates"],
            "runner_up": sig.get("runner_up"), "notional_usd": notional,
            "entry_ts": datetime.now(timezone.utc).isoformat(), "dry_run": self.dry_run,
            "entry_fill": entry_fill,
            "entry_slip_bps": ((1 if is_buy else -1) * math.log(entry_px / sig["mid"]) * 1e4
                               if sig["mid"] else None),
        }
        if today.isoformat() not in self.state["traded_dates"]:
            self.state["traded_dates"].append(today.isoformat())
        self._save_state()
        LOG.info("建玉 %s %s $%.2f x=%+.0fbps entry=%.6f",
                 sig["symbol"], "LONG" if is_buy else "SHORT", notional, sig["x_bps"], entry_px)
        self.notify("建玉", "blue",
                    f"{sig['symbol']} {'LONG' if is_buy else 'SHORT'} ${notional:.0f} "
                    f"x={sig['x_bps']:+.0f}bps 候補{sig['n_candidates']}銘柄"
                    f"{' [dry_run]' if self.dry_run else ''}")

    # ------------------------------------------------------------------ 決済
    def exit_position(self, reason: str) -> bool:
        pos = self.state.get("position")
        if not pos:
            self.state["state"] = STATE_FLAT
            self._save_state()
            return True
        sym = pos["symbol"]
        slip = float(self.cfg["exit_slippage_bps"])
        md = self._mid_and_depth(sym)
        exit_mid = md[0] if md else None

        if self.dry_run:
            exit_px = exit_mid or pos["entry_mid"]
            self._record_close(pos, exit_px, exit_mid, reason, fees_usd=None, exit_fill=None)
            return True

        since_ms = int(time.time() * 1000) - 5_000
        for attempt in range(1, int(self.cfg["exit_max_attempts"]) + 1):
            szi = self._position_szi(sym)
            if szi != szi:            # NaN = 口座が読めない
                time.sleep(3)
                continue
            if abs(szi) < 1e-12:
                break
            try:
                self._marketable_ioc(sym, is_buy=szi < 0, size=abs(szi),
                                     slip_bps=slip * attempt, reduce_only=True)
            except Exception as e:
                LOG.error("決済発注例外(%d回目): %s", attempt, e)
            time.sleep(2.5)
        else:
            szi = self._position_szi(sym)
            if abs(szi or 0) > 1e-12:
                LOG.error("決済しきれず szi=%s", szi)
                self.notify("決済失敗", "red",
                            f"{sym} が {reason} で閉じられない (szi={szi})。手動確認要")
                self.state["state"] = STATE_HALTED
                self._save_state()
                return False

        exit_fill = self._recent_fills(sym, since_ms)
        exit_px = (exit_fill or {}).get("avg_px") or exit_mid or pos["entry_px"]
        fees = (exit_fill or {}).get("fees_usd")
        entry_fees = ((pos.get("entry_fill") or {}) or {}).get("fees_usd")
        if fees is not None or entry_fees is not None:
            fees = (fees or 0.0) + (entry_fees or 0.0)
        self._record_close(pos, exit_px, exit_mid, reason, fees_usd=fees, exit_fill=exit_fill)
        return True

    def _record_close(self, pos: dict, exit_px: float, exit_mid: Optional[float],
                      reason: str, fees_usd: Optional[float],
                      exit_fill: Optional[dict] = None) -> None:
        sign = 1.0 if pos["is_buy"] else -1.0
        # y はバックテストと同じ定義(寄りmid→引けmid)。実約定ベースの gross と分けて残すことで
        # 「エッジが消えたのか執行で削られたのか」を後から切り分けられるようにする。
        ref_exit_mid = exit_mid if exit_mid else exit_px
        y_bps = math.log(ref_exit_mid / pos["entry_mid"]) * 1e4 if pos["entry_mid"] else 0.0
        gross_bps = sign * math.log(exit_px / pos["entry_px"]) * 1e4 if pos["entry_px"] else 0.0
        fee_bps = (fees_usd / pos["notional_usd"] * 1e4) if (fees_usd and pos["notional_usd"]) else None
        pnl_usd = gross_bps / 1e4 * pos["notional_usd"] - (fees_usd or 0.0)
        row = {
            **pos, "exit_px": exit_px, "exit_mid": exit_mid, "exit_reason": reason,
            "exit_ts": datetime.now(timezone.utc).isoformat(), "exit_fill": exit_fill,
            "y_bps": y_bps,                     # 寄り mid → 引け mid (バックテストと同じ定義)
            "signed_y_bps": sign * y_bps,       # 戦略リターン(コスト控除前)
            "gross_bps": gross_bps,             # 実約定ベース(スリッページ込み・手数料別)
            "fee_bps": fee_bps,
            "net_bps": gross_bps - (fee_bps or 0.0),
            "pnl_usd": pnl_usd, "fees_usd": fees_usd,
        }
        self._append_ledger(row)
        self.state["daily_pnl_usd"] = float(self.state.get("daily_pnl_usd", 0.0)) + pnl_usd
        self.state["position"] = None
        self.state["state"] = STATE_FLAT
        self._save_state()
        LOG.info("決済 %s %s x=%+.0f y=%+.0f 実約定%+.0fbps pnl=$%+.3f (%s)",
                 pos["symbol"], "LONG" if pos["is_buy"] else "SHORT",
                 pos["x_bps"], sign * y_bps, gross_bps, pnl_usd, reason)
        self.notify("決済", "green" if pnl_usd >= 0 else "orange",
                    f"{pos['symbol']} x={pos['x_bps']:+.0f}bps → y·sign={sign*y_bps:+.0f}bps "
                    f"実約定{gross_bps:+.0f}bps pnl=${pnl_usd:+.3f}"
                    f"{' [dry_run]' if pos.get('dry_run') else ''}")

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        if self.state.get("state") == STATE_HALTED:
            return
        now = datetime.now(timezone.utc)
        today_ny = now.astimezone(NY).date()
        self._roll_daily_pnl(today_ny)

        pos = self.state.get("position")
        if pos:
            # 保有中: 引け(またはgrace期限)で決済
            pos_day = date.fromisoformat(pos["date"])
            close_utc = _utc_of(pos_day, SESSION_CLOSE)
            grace = close_utc + timedelta(minutes=int(self.cfg["exit_grace_minutes"]))
            if now >= close_utc:
                self.exit_position("session_close" if now < grace else "grace_deadline")
            return

        if not self.is_session_day(today_ny):
            return
        if today_ny.isoformat() in self.state.get("traded_dates", []):
            return
        if float(self.state.get("daily_pnl_usd", 0.0)) <= -abs(float(self.cfg["daily_loss_limit_usd"])):
            LOG.error("日次損失上限に到達。halt")
            self.notify("halt", "red", f"日次損失上限 ${self.cfg['daily_loss_limit_usd']} に到達")
            self.state["state"] = STATE_HALTED
            self._save_state()
            return

        open_utc = _utc_of(today_ny, SESSION_OPEN)
        window_end = open_utc + timedelta(minutes=int(self.cfg["entry_window_minutes"]))
        if not (open_utc <= now < window_end):
            return

        foreign = self._foreign_positions(own=None)
        if foreign:
            LOG.warning("このbotの管理外の建玉がある: %s (触らない)", foreign)

        LOG.info("寄り。シグナル計算開始")
        sig = self.build_signal(today_ny)
        if not sig:
            self.state.setdefault("traded_dates", []).append(today_ny.isoformat())
            self._save_state()
            return
        LOG.info("最大 |x|: %s x=%+.1fbps (候補%d銘柄, 次点=%s)",
                 sig["symbol"], sig["x_bps"], sig["n_candidates"], sig.get("runner_up"))
        if abs(sig["x_bps"]) < float(self.cfg["min_abs_x_bps"]):
            LOG.info("|x| が閾値 %.0fbps 未満。見送り", float(self.cfg["min_abs_x_bps"]))
            self._append_ledger({"date": today_ny.isoformat(), "skipped": "below_threshold",
                                 "best_symbol": sig["symbol"], "x_bps": sig["x_bps"],
                                 "n_candidates": sig["n_candidates"],
                                 "ts": now.isoformat(), "dry_run": self.dry_run})
            self.state.setdefault("traded_dates", []).append(today_ny.isoformat())
            self._save_state()
            return
        self.enter(today_ny, sig)

    # ------------------------------------------------------------------ 起動時
    def startup_reconcile(self) -> None:
        """保有を引き継ぐ。自分の建玉以外には触らない(pair_hedge版と逆方針)。"""
        pos = self.state.get("position")
        if not pos:
            foreign = self._foreign_positions(own=None)
            if foreign:
                LOG.warning("起動時: 管理外の建玉 %s を検出。触らない", foreign)
                self.notify("起動", "orange", f"管理外の建玉を検出: {foreign} (触らない)")
            return
        if self.dry_run:
            return
        szi = self._position_szi(pos["symbol"])
        if szi != szi:
            LOG.error("起動時に口座が読めない。次tickで再確認")
            return
        if abs(szi) < 1e-12:
            LOG.warning("起動時: 記録上の建玉 %s が実際には無い。台帳に取消として記録",
                        pos["symbol"])
            self._append_ledger({**pos, "exit_reason": "vanished_on_restart",
                                 "exit_ts": datetime.now(timezone.utc).isoformat()})
            self.state["position"] = None
            self.state["state"] = STATE_FLAT
            self._save_state()
            return
        LOG.info("起動時: 保有を引き継ぐ %s szi=%s", pos["symbol"], szi)
        pos["size"] = abs(szi)
        self._save_state()
