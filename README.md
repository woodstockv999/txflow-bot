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

### 1回目: 3連敗(原因調査)
agent鍵承認後、`scripts/live_order_probe.py` でpost-only指値→取消の疎通テストを実施したが、
**3回とも `Authorization failed: ... Agent <address> is not authorized by any account` で拒否**。
`approvedAgents`確認でagent自体は承認済みと確定していたため、原因はaction_hashの再現側と判明。
署名スキーム(EIP-712)自体はethers.js/eth_accountクロス検証・HL公式SDKとのゴールデンベクタ照合
済みで、残る変数は"/exchangeに送るactionの正確な構造"だった(バンドルの取引画面チャンクが
未取得だったため)。

### 2回目: 取引画面チャンク取得後、修正して成功
`Trade-*.js`(buildNormalOrderParams)・`PositionsModule-*.js`(cancelOrder)を追加取得して
実装との差分を特定・修正:

1. **ハッシュ対象actionは"type"キーを含まない**(wireのactionにはtypeがあるが、署名計算には
   使わない)。HL公式SDKは"type"込みでハッシュするため、ここがtxflow独自の分岐点だった。
2. **order actionのキー順は`{grouping,orders}`(groupingが先)**。HL標準は`{type,orders,grouping}`
   でordersが先。
3. **cancel actionは`{cancels:[{a,o}]}`のみ(groupingキーも無い)**。oidはハッシュ計算時だけ
   JS `BigInt(oid)`相当(=常に固定8バイトuint64)でエンコードされていた
   (`src/txflow_signing.py`に自前の最小msgpackエンコーダ`_pack_msgpack`+`ForceUint64`を実装)。
4. **tifの実値は小文字**: post-only相当は`"post_only"`(`"Alo"`ではない)。通常指値`"gtc"`、
   IOC`"ioc"`。
5. **/exchange envelopeはaction種別ごとに違う**: orderは`{action,signature,nonce}`の3キーのみ
   (vaultAddress/expiresAfterを含まない)。cancelは`{action,signature,nonce,vaultAddress:null}`。

修正後、`scripts/live_order_probe.py --confirm` で成功(生ログ、署名は伏せる):

```
order price    = 65235.9 (1.0% 下, post-only buy)
order size     = 0.0002 BTC (sizeDecimals=4)
order notional = $13.0472

=== 発注 (/exchange) ===
{"status": "ok", "response": {"type": "PlaceOrder", "data": {"statuses": ["success"]}}}

=== openOrders 確認 ===
[{"coin": "BTC-USDC", "limitPx": "65235.9", "oid": 189727720184, "side": "B",
  "sz": "0.0002", "timestamp": 1784715889689, "cloid": null}]

=== 取消 (/exchange, oid=189727720184) ===
{"status": "ok", "response": {"type": "CancelOrder", "data": {"statuses": ["success"]}}}

=== openOrders 再確認 ===
[]
still_open=False -> OK: 取消確認
```

**注記**: `/exchange`のorder応答は`{"status":"ok",...,"statuses":["success"]}`で **oidを含まない**
(HL標準の`{"resting":{"oid":...}}}`形ではなかった)。`hedge_bot.py`は発注後にopenOrdersを
symbol+side+priceで突き合わせてoidを特定する(`_find_oid_by_price`)。

**注記2**: notionalの許容範囲は当初「$10-12」だったが、BTCのsize量子化(sizeDecimals=4→
最小刻み0.0001BTC≈$6.5)だと$10-12の間に量子化後の値が存在しない。$13.05(最も近い達成可能値)を
採用した。

## 実弾テスト手順(再掲)

```bash
# 1. dry-runで内容確認(発注しない)
python3 scripts/live_order_probe.py

# 2. 実発注→openOrders確認→取消→取消確認
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

- **発注(order)・取消(cancel)は実弾検証済み**(上記2026-07-22記録)。
- `type=userFills` はHL標準を仮定して実装しているが未検証(取消×約定レース確認・stranded leg
  自動close・taker化フォールバックで使う。live modeでのみ使用、現状dry_run既定のため未到達コード。
  次に実弾検証すべき最有力項目)。
- `updateLeverage`/`modifyOrder`/TP-SL関連actionは本bot未使用だが、構造は
  `src/txflow_signing.py`冒頭コメントに記録済み(将来使う場合の参考)。
- price量子化(`TxflowClient._price_decimals`)はl2Bookの実勢価格の小数桁数から動的推定している
  (priceTick相当のメタデータがinstruments.jsonに無いため)。今回の実弾テストでは65235.9(1桁)が
  acceptされたので少なくともBTCでは機能している。
- WSのl2Book購読は `coin:"1"` のような数値インデックス文字列で送るが、**応答の`data.coin`は
  `"BTC-USDC"`のようなシンボル名で返る**(2026-07-22 smoke testで実測・実装済み)。
- **日次損失窓(`daily_loss_limit_usd`)はプロセス内メモリで保持しており、プロセス再起動で
  リセットされる**(永続化していない)。v1として許容(pm2再起動が頻発する運用だと損失上限が
  実効的に緩む点は把握しておくこと)。
