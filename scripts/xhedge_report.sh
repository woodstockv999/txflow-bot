#!/usr/bin/env bash
# クロス会場ヘッジ検証の定期レポート。tape 蓄積が進むほど有意になる。
# 複数 hold で xhedge_analyze を回し、要約を Discord へ。cron から2hおきに叩く想定。
set -uo pipefail
APP=/home/w00dst0ck/apps/txflow-bot
PY="$APP/.venv/bin/python3"
cd "$APP" || exit 1

# tape の両会場そろいサンプル数(蓄積量の目安)
lines=$(wc -l < data/perpl_xhedge_tape.jsonl 2>/dev/null || echo 0)

body=""
for hold in 30 120 300; do
  out=$("$PY" scripts/xhedge_analyze.py --hold "$hold" 2>&1 | \
        grep -E 'サイクル数|全maker|全taker|markout\(|\+30s|open 平均' | \
        sed 's/^  */  /')
  body+="[hold=${hold}s]
${out}
"
done

discord-notify -t "xhedge検証: txflow×perpl BTCヘッジ" -c blue \
  "tape行数=${lines}（両会場そろい分で解析）
${body}" 2>/dev/null || true
