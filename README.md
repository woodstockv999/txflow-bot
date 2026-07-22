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
(HL標準の`{"resting":{"oid":...}}}`形ではなかった)。oid特定は当初symbol+side+price文字列一致
(`_find_oid_by_price`)で実装していたが、下記の実弾事故を機にcloidベースへ全面移行した。

**注記2**: notionalの許容範囲は当初「$10-12」だったが、BTCのsize量子化(sizeDecimals=4→
最小刻み0.0001BTC≈$6.5)だと$10-12の間に量子化後の値が存在しない。$13.05(最も近い達成可能値)を
採用した。

## 実弾稼働事故と修正(2026-07-22 19:35-19:48 BTC)

hedge_botを実弾稼働(BTC/ETH)させたところ、lead注文は実際に5回約定していた(買い0.0015×2、
売り0.0015+0.0014+0.0001)のに、bot側は全サイクルを"lead_timeout_requote"と誤認し続けた。
原因は `_find_oid_by_price`(symbol+side+price文字列一致でopenOrdersからoidを探す)がサーバの
echo文字列と一致せず失敗し、oidが取れない→約定検知不能→取消も不能→注文が板に堆積、という
連鎖。さらにconfigをBTCからSOLに切り替えて再起動した際、botが見ていないBTC建玉0.0030が
裸で残った(手動決済済み)。

### 修正1: wireの末尾ゼロ除去
`build_limit_order_wire`のp/sに`signing._trim_trailing_zeros`を適用。実測: `s="0.0030"`だと
`Authorization failed`になる(署名側は`"0.003"`に正規化してハッシュするがサーバはwire文字列の
まま再計算するため不一致)。回帰テスト: `tests/test_client.py`。

### 修正2: cloidベースのoid特定(最重要)
`scripts/cloid_probe.py`で実測: **txflowのcloidはHL標準の`0x`+32hex(16バイト)形式では
"Invalid cloid format"で拒否され、UUID4文字列("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")は
受理・openOrdersにそのままエコーされる**(place→openOrders確認→oid経由cancel→確認、まで成功)。
生ログ(署名は伏せる):

```
trying UUID cloid: 80f969de-f58c-4e50-bae2-86fe818208a5
{"status": "ok", "response": {"type": "PlaceOrder", "data": {"statuses": ["success"]}}}

openOrders: [{"coin": "BTC-USDC", "limitPx": "52786.6", "oid": 189747539553, "side": "B",
              "sz": "0.0002", "timestamp": 1784717728036,
              "cloid": "80f969de-f58c-4e50-bae2-86fe818208a5"}]

cancelling oid 189747539553
{"status": "ok", "response": {"type": "CancelOrder", "data": {"statuses": ["success"]}}}
after cancel: []
```

(なお `0x`+32hex形式は`{"status":"err","response":"Invalid cloid format"}`で拒否確認済み)。

cancelByCloid相当のaction schemaはバンドル内に見つからず、追加の実弾発注は指示の予算
(遠値post-only 1-2発)を超えるため未検証。oid経由の取消は既に確定済みのため、cloidは
**識別専用**(発注後にopenOrdersをcloidで突き合わせてoidを回収する)として使う。

`hedge_bot._place_and_identify()` に全面移行:
1. 発注時に毎回UUID4 cloidを付与
2. openOrdersを300ms間隔で最大5回ポーリングし、cloid一致でoidを回収 → `"resting"`
3. 見つからなければuserFills(発注時刻以降・symbol・side一致)で即時約定を確認 → `"filled"`
4. どちらでも同定できなければ `"lost"` を返す。呼び出し側は該当symbolの全open orderを
   取消し(`_cancel_all_orders_for_symbol`)、サイクルを中断してIDLEへ戻る
   (`_abort_cycle_lost_oid`。ヘッジ脚の同定失敗時はリード脚もtakerで強制close)。

fill検知(`_check_live_fill`系)もoid単独マッチをやめ、oidに紐づく`userFills`を直接集約する
方式(`_find_fill`)に統一。**部分約定はsize合算で判定**(複数fillレコードのsize合計が
目標サイズに達したら約定とみなし、サイズ加重平均価格を採用)。

### 修正3: 起動時リコンサイル強化
`PairHedgeBot.startup_reconcile()`(`main.py`から起動時に1回呼ぶ)を新設。config変更で
見えなくなる建玉の再発防止に、**symbol不問**で(a) 全open orderを取消 (b) clearinghouseState
の全建玉をreduce-only IOCでフラット化し、WARNING+discord通知する。既存の
`_reconcile_stranded_legs`(毎IDLE tick、configのsymbolだけを見る軽量チェック)は稼働中の
補助として維持。

## 実弾テスト手順(再掲)

```bash
# 1. dry-runで内容確認(発注しない)
python3 scripts/live_order_probe.py

# 2. 実発注→openOrders確認→取消→取消確認
python3 scripts/live_order_probe.py --confirm

# 3. cloid疎通確認(遠値post-only→openOrdersでcloidエコー確認→oid経由cancel→確認)
python3 scripts/cloid_probe.py
```

## ファイル構成

- `src/txflow_signing.py` — msgpack action hash + EIP-712 agent署名。ApproveAgent typed-data生成。
- `src/txflow_client.py` — REST(/info, /exchange)・WS(l2Book)クライアント。price/size量子化。
- `src/hedge_bot.py` — pair_hedge状態機械(IDLE→LEAD_RESTING→HEDGED→HOLD→UNWIND→FLAT)。
  cloidベースのoid特定・起動時リコンサイル。
- `main.py` — ループ本体。`enabled:false`の間は待機のみ。起動時に`startup_reconcile()`を実行。
- `scripts/live_order_probe.py` — 実弾疎通テスト専用スクリプト(1回限り、手動実行)。
- `scripts/cloid_probe.py` — cloid疎通テスト専用スクリプト(1回限り、手動実行)。
- `data/instruments.json` — coin index解決用の静的参照データ(168資産)。
- `data/cycles.jsonl` — サイクル台帳(gitignore対象、実行時生成)。
- `tests/test_signing.py` — 署名ユニットテスト(HL公式SDKとのゴールデンベクタ照合含む)。
- `tests/test_client.py` — TxflowClientユニットテスト(末尾ゼロ除去の回帰テスト含む)。
- `tests/test_hedge_bot.py` — hedge_botユニットテスト(cloid特定・部分約定合算・起動時
  リコンサイルの回帰テスト含む、フェイクclientでネットワーク不要)。

## 既知の制約・要検証項目

- **発注(order)・取消(cancel)・cloid識別は実弾検証済み**(上記2026-07-22記録)。
- **cancelByCloid相当は未検証**(未使用。oid経由のcancelで代替)。
- `userFills`は実アカウントの実売買履歴で構造確認済み(cloidフィールドは無い。oid/symbol/side/
  time/px/sz/feeで照合する設計)。
- `updateLeverage`/`modifyOrder`/TP-SL関連actionは本bot未使用だが、構造は
  `src/txflow_signing.py`冒頭コメントに記録済み(将来使う場合の参考)。
- price量子化(`TxflowClient._price_decimals`)はl2Bookの実勢価格の小数桁数から動的推定している
  (priceTick相当のメタデータがinstruments.jsonに無いため)。今回の実弾テストでは65235.9(1桁)が
  acceptされたので少なくともBTCでは機能している。
- 実アカウントの現在のレバレッジは`userFills`実測で10倍(config.yamlの`leverage:3`とは不一致)。
  botは`updateLeverage`を呼ばないため、取引所側の既定/前回設定がそのまま使われる。設定を
  同期させたい場合は別途対応が必要(今回のタスク範囲外、観察事項として記録)。
- WSのl2Book購読は `coin:"1"` のような数値インデックス文字列で送るが、**応答の`data.coin`は
  `"BTC-USDC"`のようなシンボル名で返る**(2026-07-22 smoke testで実測・実装済み)。
- **日次損失窓(`daily_loss_limit_usd`)はプロセス内メモリで保持しており、プロセス再起動で
  リセットされる**(永続化していない)。v1として許容(pm2再起動が頻発する運用だと損失上限が
  実効的に緩む点は把握しておくこと)。
