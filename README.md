# txflow-bot

[txflow](https://app.txflow.com)(Hyperliquid完全フォークの perp DEX, API: `https://api.txflow.com`)で
出来高ファーミングする pair_hedge 型 bot。**現状 dry_run 既定・enabled:false 既定・pm2未起動**。

参考実装(変更禁止・読み取り専用): `~/apps/hyperliquid-bot/src/pair_hedge.py`

## セットアップ

```bash
cd ~/apps/txflow-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## .env

```
TXFLOW_MAIN_ADDRESS=0x...       # メインウォレット(読み取り専用に使う。署名はしない)
TXFLOW_AGENT_PRIVATE_KEY=0x...  # ApproveAgentで承認済みのagent鍵。実弾発注に必須
TXFLOW_AGENT_ADDRESS=0x...      # 上記鍵から導出されるアドレス(検証用、必須ではない)
```

`.gitignore` 済み。**dry_run=true(config.yamlの既定)の間は `TXFLOW_AGENT_PRIVATE_KEY` を読み込まない**
(`main.py`: `agent_key = ... if not cfg["dry_run"] else None`)。実弾はconfig.yamlの`dry_run:false`
**かつ**`.env`のagent鍵投入の両方が揃わないと不可能(二重の安全装置)。

## 起動

```bash
# smoke test (読み取りのみ、無害)
python -m src.txflow_client --smoke

# ユニットテスト(署名)
python -m pytest tests/ -v

# 本稼働(config.yaml の enabled:true にしてから)
python main.py
```

pm2は **まだ起動しない**(タスク仕様の禁止事項)。`ecosystem.config.js` は用意済みだが
`pm2 start ecosystem.config.js` はユーザーの明示的な合意後に手動で行うこと。

## config.yaml

| key | 既定値 | 説明 |
|---|---|---|
| base_symbol / hedge_symbol | BTC / ETH | リード脚/ヘッジ脚 |
| notional_usd | 100 | 1サイクルの想定ノーショナル |
| hedge_ratio | 0.87 | ヘッジ脚のノーショナル比率 |
| hold_seconds | 3.0 | 両脚約定後の保有秒数 |
| leg_timeout_seconds | 60 | maker指値の待機上限。超過でtaker化 |
| daily_loss_limit_usd | 5.0 | 超過で全close+halt+discord通知 |
| dry_run | **true** | falseにするとagent鍵で実発注する |
| enabled | **false** | falseの間 main.py はループを起動せず待機する |

## 実弾テスト実施記録(2026-07-22)

agent鍵承認後、$10〜$12のpost-only(tif=Alo)指値を1発→取消の疎通テストを
`scripts/live_order_probe.py` で実施した。**3回とも `Authorization failed: ... is not authorized
by any account` で拒否され、指示に従い中断**。詳細は最終報告(タスク呼び出し元への報告)を参照。
要点:

- `POST /info {"type":"approvedAgents","user":<main>}` で agent
  `0x5463f600edd36e7fab8cba0b15ec4732a352ce8a` (name="TradeAgent") が **承認済みであることは確認済み**。
  つまり承認自体は成功しており、問題は署名検証(action_hashの再現)側にある。
- 署名スキーム(EIP-712 domain/types/message)は node.js `ethers@6` と Python `eth_account` で
  **同一入力から完全に同一の署名バイト列が出ることをクロス検証済み**(`tests/test_signing.py` 参照)。
  `action_hash()` のmsgpack+nonce+vaultアドレス手順もHyperliquid公式Python SDKの出力と
  バイト単位で一致(ゴールデンベクタ)。
- 従って未解決の変数は **`/exchange` に送る "action" dict の正確なフィールド構成**
  (`{"type":"order","orders":[{"a","b","p","s","r","t"}],"grouping":"na"}` という
  HL標準形を仮定しているが、バンドル `index-main.js` にはこの部分の実装(取引画面の
  遅延ロードチャンク)が含まれておらず未確認)。cancel actionでも同じ症状が出た
  (HL標準の `{"type":"cancel","cancels":[{"a","o"}]}` で試行、"a"をexternalId=1/assetBase=2の
  両方で試したが変化なし)ため、order固有の問題という確証は無い。

### 次にやるべきこと(要実弾検証、代理実行不可)
1. app.txflow.comで実際に指値を1発出す瞬間のブラウザ開発者ツール(Network タブ)で
   `/exchange` へのリクエストボディを直接採取し、`action`の正確なJSON形を確定させる
   (最短ルート。static解析の限界に達した)。
2. 上記が難しい場合、txflowの開発者向けドキュメント/Discordで `/exchange` の
   action schemaを確認する。
3. 採取できたら `src/txflow_client.py` の `build_limit_order_action`/`build_cancel_action` を
   修正し、`scripts/live_order_probe.py --confirm` で再試行(このファイルの安全策
   notional$10-12/post-onlyはそのまま維持すること)。

## 実弾テスト手順(再掲、上記の課題解決後)

```bash
# 1. dry-runで内容確認(発注しない)
python3 scripts/live_order_probe.py

# 2. 実発注→openOrders確認→取消→取消確認 (BTC $10-12 post-only)
python3 scripts/live_order_probe.py --confirm
```

## ファイル構成

- `src/txflow_signing.py` — msgpack action hash + EIP-712 agent署名。ApproveAgent typed-data生成。
- `src/txflow_client.py` — REST(/info, /exchange)・WS(l2Book)クライアント。
- `src/hedge_bot.py` — pair_hedge状態機械(IDLE→LEAD_RESTING→HEDGED→HOLD→UNWIND→FLAT)。
- `main.py` — ループ本体。`enabled:false`の間は待機のみ。
- `scripts/live_order_probe.py` — 実弾疎通テスト専用スクリプト(1回限り、手動実行)。
- `data/instruments.json` — coin index解決用の静的参照データ(168資産)。
- `data/cycles.jsonl` — サイクル台帳(gitignore対象、実行時生成)。
- `tests/test_signing.py` — 署名ユニットテスト(HL公式SDKとのゴールデンベクタ照合含む)。

## 既知の制約・要検証項目

- 発注action("type":"order")の正確なフィールド構成は未確認(上記参照)。
- `type=userFills`/`type=openOrders` はHL標準を仮定して実装しているが未検証(live modeでのみ使用、
  現状dry_run既定のため未到達コード)。
- WSのl2Book購読は `coin:"1"` のような数値インデックス文字列で送るが、**応答の`data.coin`は
  `"BTC-USDC"`のようなシンボル名で返る**(2026-07-22 smoke testで実測・実装済み)。
