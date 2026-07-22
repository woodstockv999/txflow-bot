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
  ],
};
