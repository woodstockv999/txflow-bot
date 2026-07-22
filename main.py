#!/usr/bin/env python3
"""txflow-bot エントリポイント。config.yaml の enabled:false の間はループを起動しない
(pm2の常駐プロセスとしては生きているが、tickを進めない=何もしない安全モード)。"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.hedge_bot import PairHedgeBot
from src.txflow_client import TxflowClient, TxflowWS

APP_ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("txflow-bot")

TICK_INTERVAL_SEC = 1.0


def _notify(context: str, color: str, body: str) -> None:
    """discord-notify CLI経由、fail-open。呼び出し元(hedge_bot)は状態遷移時のみ呼ぶ設計。"""
    try:
        subprocess.run(
            ["discord-notify", "-t", f"txflow-bot: {context}", "-c", color, body],
            timeout=15, check=False,
        )
    except Exception as e:
        LOG.warning("discord-notify失敗(fail-open): %s", e)


def load_config() -> dict:
    with open(APP_ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    if not cfg.get("enabled", False):
        LOG.info("config.yaml enabled=false のため待機モード(何もしない)。config.yamlを編集して再起動")
        while True:
            time.sleep(60)

    env = dotenv_values(APP_ROOT / ".env")
    agent_key = env.get("TXFLOW_AGENT_PRIVATE_KEY") if not cfg.get("dry_run", True) else None
    main_addr = env.get("TXFLOW_MAIN_ADDRESS")

    client = TxflowClient(agent_private_key=agent_key, main_address=main_addr)
    symbols = [cfg["base_symbol"], cfg["hedge_symbol"]]
    ws = TxflowWS(symbols, client.coin_index)
    ws.start()
    if not ws.wait_connected(timeout=15):
        LOG.error("WS接続タイムアウト。終了")
        sys.exit(1)

    ledger_path = APP_ROOT / "data" / "cycles.jsonl"
    bot = PairHedgeBot(cfg, client, ws, ledger_path, notify_fn=_notify)

    LOG.info("txflow-bot 起動: dry_run=%s base=%s hedge=%s notional=$%s",
              cfg.get("dry_run", True), cfg["base_symbol"], cfg["hedge_symbol"], cfg["notional_usd"])

    try:
        while True:
            bot.tick()
            time.sleep(TICK_INTERVAL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        ws.stop()


if __name__ == "__main__":
    main()
