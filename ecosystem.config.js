// pm2 設定。ポート不要の常駐プロセス(REST/WSクライアントのみ、待受ポートなし)。
// ★★pm2 start はまだ行わない(タスク仕様の禁止事項)。ユーザーの明示的な合意後に手動起動する。
module.exports = {
  apps: [
    {
      name: "txflow-bot",
      cwd: __dirname,
      script: ".venv/bin/python3",
      args: "main.py",
      interpreter: "none",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      // デイトレbot(閉場ドリフト継続)。pair_hedge版とは別プロセス・別config。
      // ★同時起動禁止: pair_hedge版は起動時に口座の全建玉をsymbol不問でフラット化するため
      //   こちらの建玉を消す。daytrade.py が起動時に pm2 で検出して自ら止まる。
      name: "txflow-daytrade",
      cwd: __dirname,
      script: ".venv/bin/python3",
      args: "daytrade.py",
      interpreter: "none",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
