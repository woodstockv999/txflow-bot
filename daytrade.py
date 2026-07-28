#!/usr/bin/env python3
"""txflow デイトレbot エントリポイント (閉場ドリフト継続戦略)。

pair_hedge版(main.py)とは別プロセス・別config。
★同時起動禁止: main.py 側は起動時に口座の全建玉を symbol 不問でフラット化するため、
  こちらの建玉を消す。起動時に pm2 で txflow-bot が online なら起動を拒否する。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.daytrade_bot import DaytradeBot
from src.txflow_client import TxflowClient

APP_ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("txflow-daytrade")
TICK_INTERVAL_SEC = 10.0


def _notify(context: str, color: str, body: str) -> None:
    try:
        subprocess.run(["discord-notify", "-t", f"txflow-daytrade: {context}", "-c", color, body],
                       timeout=15, check=False)
    except Exception as e:
        LOG.warning("discord-notify失敗(fail-open): %s", e)


def _pair_hedge_running() -> bool:
    """pm2 の txflow-bot(pair_hedge版) が online かどうか。pm2が無い環境ではFalse。"""
    try:
        out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return False
        for app in json.loads(out.stdout or "[]"):
            if app.get("name") == "txflow-bot" and \
                    (app.get("pm2_env") or {}).get("status") == "online":
                return True
    except Exception as e:
        LOG.warning("pm2確認失敗(続行): %s", e)
    return False


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with open(APP_ROOT / "config_daytrade.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not cfg.get("enabled", False):
        LOG.info("config_daytrade.yaml enabled=false。待機モード")
        while True:
            time.sleep(60)

    if _pair_hedge_running():
        LOG.error("pair_hedge版 txflow-bot が online。同一口座の建玉を消し合うので起動しない")
        _notify("起動拒否", "red", "pair_hedge版 txflow-bot が online のため起動しない")
        sys.exit(1)

    env = dotenv_values(APP_ROOT / ".env")
    dry_run = bool(cfg.get("dry_run", True))
    client = TxflowClient(
        agent_private_key=None if dry_run else env.get("TXFLOW_AGENT_PRIVATE_KEY"),
        main_address=env.get("TXFLOW_MAIN_ADDRESS"),
    )

    data_dir = APP_ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    bot = DaytradeBot(cfg, client, data_dir / "daytrade.jsonl",
                      data_dir / "daytrade_state.json", notify_fn=_notify)

    LOG.info("起動: dry_run=%s notional=$%s min|x|=%sbps 候補%d銘柄",
             dry_run, cfg["notional_usd"], cfg["min_abs_x_bps"], len(cfg["candidates"]))
    bot.startup_reconcile()

    while True:
        try:
            bot.tick()
        except Exception as e:
            LOG.exception("tick例外(継続): %s", e)
        time.sleep(TICK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
