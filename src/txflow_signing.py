"""txflow (Hyperliquid完全フォーク) の L1 action 署名。

## スキーマの根拠(バンドル抽出、2026-07-22)
`scratchpad/txflow/index-main.js` を静的解析して以下を確定させた(該当箇所は関数名を
コメントに残す。関数名はminify後のものでビルドのたびに変わりうるので再解析の目印として使う):

- `Vi()` (index-main.js:~1111577): ネットワーク定数。
  `{txflowNetwork:"TxFlow-Mainnet", chainId:869, apiVersion:1, signatureChainId:42161}`
- `o1e` (index-main.js:~1111608): Agent型のEIP712 types。
  `Agent:[{txflowNetwork:string},{chainId:uint32},{apiVersion:uint32},{connectionId:bytes32}]`
- `a1e` (index-main.js:~1111938): Agent型のdomain。
  `{name:txflowNetwork, version:String(apiVersion), chainId:chainId(=869), verifyingContract:zero}`
  ※ ApproveAgent(後述)は domain.chainId に `signatureChainId`(ウォレット接続チェーン=Arbitrum
  42161)を使うのに対し、Agent(L1 action署名)は `chainId`(=869、txflow固有の値)を使う。
  実ウォレットで署名するか(ApproveAgent)、エフェメラルなagent鍵で完全オフチェーン署名するか
  (Agent)の違いに対応していると見られる。
- `c1e` (index-main.js:~1112027): message構築。
  `{txflowNetwork, chainId, apiVersion, connectionId: actionHash}`
- `n()` (b2 hook内、index-main.js:~1112700付近、actionHash実体):
  1. action を正規化( `$1()`: "p"/"s" フィールドの文字列だけ末尾ゼロを削る。HLの
     `float_to_wire`/`remove_trailing_zeros` と同じ意図)
  2. msgpack encode(`new iP({useBigInt64:true})` = @msgpack/msgpack Encoder)
  3. + nonce を8バイトbig-endian uint64で追記
  4. + vaultAddress: null なら1バイト`0x00`、ありなら`0x01`+20バイトアドレス
  5. keccak256でハッシュ(`_P`)
  → これは Hyperliquid公式Python SDK (`hyperliquid.utils.signing.action_hash`) と
  **バイト単位で同一のアルゴリズム**。実際に本ファイルの `action_hash()` は同SDKの出力と
  完全一致することを `tests/test_signing.py` のゴールデンベクタで検証済み(SDKはHL本家の
  署名で年単位の実運用実績があるため、間接的にこの部分の正しさを担保する)。
  `expiresAfter` 相当のフィールドはバンドル内のこの関数には見当たらなかった(HL SDKには
  新しめのオプション引数として存在するが、txflow側での対応は未確認 → 常に省略する実装とし、
  実弾検証時に techflow が `expiresAfter` を要求してこないか要確認)。
- `ApproveAgent` (index-main.js:~1138712, `ibe`/`lbe` フック内): エージェント鍵の承認は
  メインウォレットの実署名(MetaMask等)で行う。types/domain/messageは
  `build_approve_agent_typed_data()` にそのまま転記。

## 2026-07-22 追記: 取引画面チャンク(Trade.js / PositionsModule.js)実測で確定した事実
実弾プローブが "Agent X is not authorized" (=署名から復元されるアドレスが違う=ハッシュ対象の
action構築が間違っている) で3連敗した後、遅延チャンクを取得して原因を特定した。

1. **ハッシュ対象の action は "type" キーを含まない**。wireで送る action には type がある
   (例: `{type:"order",grouping:"na",orders:[...]}`) が、署名(=action_hash)の入力に使う
   dict は type を除いたもの(`{grouping:"na",orders:[...]}`)。cancel/updateLeverageでも
   同様(下記3参照)。HL公式SDKは type を含めてハッシュするため、ここがtxflow独自の分岐点。
   → `wire_action_for_hash()` で "type" だけ取り除く。
2. **order actionのキー順は `{grouping, orders}`(groupingが先)**。HL公式SDKは
   `{type,orders,grouping}`(ordersが先)なので、type除去後も残り2キーの順序が違うと
   msgpackのバイト列が変わりハッシュが変わる。実測(Trade.js `fo`関数、buildNormalOrderParams):
   ```js
   const p = {grouping:"na", orders:l};              // ← ハッシュ対象(type無し、grouping先)
   const {signature:g, nonce:v} = await n(e, p, !0);
   return {action:{type:"order", grouping:"na", orders:l}, signature:..., nonce:v};
   // ↑ wireに送るactionだけtypeを足す(順序は type,grouping,orders)
   ```
   order wire本体(`t:{limit:{tif}}`まで含む1件)のキー順は `{a,b,p,s,r,t}` でHLと同一
   (`i=(a,c)=>({a:t,b:a.orderSide==="buy",p:a.price,s:c,r:a.reduceOnly,t:{limit:{tif:a.timeInForce}}})`)。
3. **cancel actionのハッシュ対象は `{cancels:[{a,o}]}`(typeも無ければgroupingキーも無い、
   通常キャンセルの場合)**。実測(PositionsModule.js `cancelOrder`フック):
   ```js
   const C = {cancels:[{...w?{grouping:w}:{}, a:T, o:BigInt(y)}]};  // wは通常キャンセルではundefined
   const {signature:_, nonce:L} = await u(n, C, !0);
   const v = {action:{type:"cancel", cancels:[{...w?{grouping:w}:{}, a:T, o:y}]},
              signature:..., nonce:L, vaultAddress:null};
   ```
   **oidはハッシュ対象では`BigInt(y)`、wireでは素の数値`y`**。@msgpack/msgpackはBigInt入力を
   常に固定8バイト(uint64, 0xcf)でエンコードすると見られ、標準msgpackパッケージの可変長圧縮とは
   異なりうるため、本ファイルはmsgpackパッケージを使わず自前の最小エンコーダ`_pack_msgpack()`で
   ハッシュを組み立て、oidだけ`ForceUint64`でマークして固定8バイトにする(下記参照)。
   TP/SL関連のキャンセルは `grouping:"tpsl"` がハッシュ対象にも入る変種があるが本bot未使用。
4. **updateLeverageのハッシュ対象は `{asset,leverage,marginMode}`(typeなし)、wireは
   `{type:"updateLeverage",...d}` + 別に `isFrontend:true,vaultAddress:null` を envelope に
   追加**(PositionsModule.js `sb`フック)。本bot未使用だが将来の罠として記録。
5. **closeAll(全ポジション成行決済)の order wireには `m:<symbol小文字>` という追加フィールドが
   付き、wireのactionには`nonce`も同梱される変種がある**(`xP`フック)。本bot未使用。
6. **/exchange envelopeはaction種別ごとに構成キーが違う(実測)**:
   - order: `{action, signature, nonce}` の3キーのみ。**vaultAddress/expiresAfterを含まない**。
   - cancel: `{action, signature, nonce, vaultAddress:null}` の4キー。expiresAfterは無い。
   - updateLeverage: `{action, isFrontend:true, vaultAddress:null, signature, nonce}`。
   → `txflow_client.py` はaction種別ごとに正確にこの形で送る。
7. **tif(timeInForce)の実際の文字列値は小文字**: UIの`time_in_force_options`実測で
   `[{key:"GTC",value:"gtc"}, {key:"POST_ONLY",value:"post_only"}, {key:"IOC",value:"ioc"}]`
   ("Gtc"/"Alo"/"Ioc"というHL標準の大文字camelは使われていない)。フォームのdefaultValueは
   指値注文で`"gtc"`、成行(market)注文は`"FrontendMarket"`という別の特殊値。
   **post-only相当は `"post_only"`**(注文履歴の表示側では`tif==="ALO"`をPost Onlyと表示して
   いるため、サーバ内部では"ALO"に正規化される可能性はあるが、送信時のリクエストボディは
   "post_only"が実測値)。
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_hex

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Vi() (index-main.js) そのまま。txflowNetwork の値は VITE_ 環境変数由来の文字列で
# "Flow-Mainnet" ではなく "TxFlow-Mainnet" だった(バンドル内の文字列リテラルを直接確認)。
DEFAULT_NETWORK = {
    "txflowNetwork": "TxFlow-Mainnet",
    "chainId": 869,
    "apiVersion": 1,
    "signatureChainId": 42161,
}

_EIP712_DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


def _trim_trailing_zeros(value: str) -> str:
    """JS の m2()/removeTrailingZeros() と同一。"p"/"s" 文字列フィールド専用。"""
    if "." not in value:
        return value
    trimmed = re.sub(r"\.?0+$", "", value)
    return "0" if trimmed == "-0" else trimmed


def _normalize_action(obj: Any) -> Any:
    """JS の $1() と同一。dict/list を再帰し、"p"/"s" キーの文字列値だけ正規化する。"""
    if isinstance(obj, list):
        return [_normalize_action(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out[k] = _normalize_action(v)
            elif k in ("p", "s") and isinstance(v, str):
                out[k] = _trim_trailing_zeros(v)
            else:
                out[k] = v
        return out
    return obj


def address_to_bytes(address: str) -> bytes:
    return bytes.fromhex(address[2:] if address.startswith("0x") else address)


class ForceUint64(int):
    """oidなど、txflowフロントがmsgpackハッシュ計算時だけJS BigInt(...)でラップして常に
    固定8バイト(uint64, 0xcf)でエンコードするフィールド用のマーカー型(このファイル冒頭の
    2026-07-22追記の3参照)。使い方: `{"a":1, "o":ForceUint64(12345)}` のように該当フィールド
    だけラップして action_hash() に渡す。intのサブクラスなので通常のint演算はそのまま使える。
    """

    __slots__ = ()


def wire_action_for_hash(wire_action: dict) -> dict:
    """/exchangeに送るwire actionから、署名対象(msgpackハッシュ)に使う dict を作る。
    実測(このファイル冒頭2026-07-22追記の1参照): txflowは "type" キーだけを除いたものを
    ハッシュする。残りのキーの順序はwireのまま(dict内包表記はPython 3.7+で挿入順を保つ)。
    """
    return {k: v for k, v in wire_action.items() if k != "type"}


def _pack_int(i: int) -> bytes:
    if 0 <= i <= 0x7F:
        return bytes([i])
    if -32 <= i < 0:
        return bytes([i & 0xFF])
    if 0 <= i <= 0xFF:
        return b"\xcc" + i.to_bytes(1, "big")
    if 0 <= i <= 0xFFFF:
        return b"\xcd" + i.to_bytes(2, "big")
    if 0 <= i <= 0xFFFFFFFF:
        return b"\xce" + i.to_bytes(4, "big")
    if 0 <= i <= 0xFFFFFFFFFFFFFFFF:
        return b"\xcf" + i.to_bytes(8, "big")
    if -0x80 <= i < -32:
        return b"\xd0" + i.to_bytes(1, "big", signed=True)
    if -0x8000 <= i < -0x80:
        return b"\xd1" + i.to_bytes(2, "big", signed=True)
    if -0x80000000 <= i < -0x8000:
        return b"\xd2" + i.to_bytes(4, "big", signed=True)
    if -0x8000000000000000 <= i < -0x80000000:
        return b"\xd3" + i.to_bytes(8, "big", signed=True)
    raise OverflowError(f"msgpack int範囲外: {i}")


def _pack_str(s: str) -> bytes:
    data = s.encode("utf-8")
    n = len(data)
    if n <= 31:
        return bytes([0xA0 | n]) + data
    if n <= 0xFF:
        return b"\xd9" + n.to_bytes(1, "big") + data
    if n <= 0xFFFF:
        return b"\xda" + n.to_bytes(2, "big") + data
    return b"\xdb" + n.to_bytes(4, "big") + data


def _map_header(n: int) -> bytes:
    if n <= 15:
        return bytes([0x80 | n])
    if n <= 0xFFFF:
        return b"\xde" + n.to_bytes(2, "big")
    return b"\xdf" + n.to_bytes(4, "big")


def _array_header(n: int) -> bytes:
    if n <= 15:
        return bytes([0x90 | n])
    if n <= 0xFFFF:
        return b"\xdc" + n.to_bytes(2, "big")
    return b"\xdd" + n.to_bytes(4, "big")


def _pack_msgpack(obj: Any) -> bytes:
    """action_hash専用の最小msgpackエンコーダ。dict(fixmap/map16/map32)・
    list/tuple(fixarray/array16/array32)・str・bool・int(compact)・None・ForceUint64
    (常に固定8バイトuint64)にのみ対応する。標準の`msgpack`パッケージ(use_bin_type=True)と
    等価であることを tests/test_signing.py のゴールデンベクタ(HL公式SDK基準)で検証済み。
    自前実装にした理由: ForceUint64相当(値によらず常に固定8バイト)を標準msgpackパッケージの
    公開APIだけで特定フィールドにだけ強制する手段が無いため。
    """
    if isinstance(obj, bool):
        return b"\xc3" if obj else b"\xc2"
    if isinstance(obj, ForceUint64):
        return b"\xcf" + int(obj).to_bytes(8, "big")
    if isinstance(obj, int):
        return _pack_int(obj)
    if isinstance(obj, str):
        return _pack_str(obj)
    if isinstance(obj, dict):
        body = b"".join(_pack_msgpack(k) + _pack_msgpack(v) for k, v in obj.items())
        return _map_header(len(obj)) + body
    if isinstance(obj, (list, tuple)):
        body = b"".join(_pack_msgpack(v) for v in obj)
        return _array_header(len(obj)) + body
    if obj is None:
        return b"\xc0"
    raise TypeError(f"_pack_msgpack: 未対応の型 {type(obj)}")


def action_hash(action: dict, vault_address: Optional[str], nonce: int) -> bytes:
    """actionHash(action, vaultAddress, nonce) 完全再現。HL公式SDKと同一アルゴリズム
    (tests/test_signing.py のゴールデンベクタで検証済み)。
    `action` にはあらかじめ `wire_action_for_hash()` を通した(必要なら)dict を渡すこと
    ("type"キー除去等はここでは行わない。低レベルのハッシュ関数として汎用に保つ)。"""
    normalized = _normalize_action(action)
    packed = _pack_msgpack(normalized)
    buf = bytearray(packed)
    buf += int(nonce).to_bytes(8, "big")
    if vault_address is None:
        buf += b"\x00"
    else:
        buf += b"\x01"
        buf += address_to_bytes(vault_address)
    return keccak(bytes(buf))


def _agent_typed_data(connection_id: bytes, network: dict) -> dict:
    return {
        "domain": {
            "name": network["txflowNetwork"],
            "version": str(network["apiVersion"]),
            "chainId": network["chainId"],
            "verifyingContract": ZERO_ADDRESS,
        },
        "types": {
            "Agent": [
                {"name": "txflowNetwork", "type": "string"},
                {"name": "chainId", "type": "uint32"},
                {"name": "apiVersion", "type": "uint32"},
                {"name": "connectionId", "type": "bytes32"},
            ],
            "EIP712Domain": _EIP712_DOMAIN_TYPE,
        },
        "primaryType": "Agent",
        "message": {
            "txflowNetwork": network["txflowNetwork"],
            "chainId": network["chainId"],
            "apiVersion": network["apiVersion"],
            "connectionId": connection_id,
        },
    }


def sign_l1_action(
    agent_private_key: str,
    action: dict,
    vault_address: Optional[str],
    nonce: int,
    network: Optional[dict] = None,
) -> dict:
    """agent鍵で L1 action (order/cancel/...) に phantom-agent 方式で EIP-712 署名する。

    Returns: {"r": "0x..", "s": "0x..", "v": int}
    """
    network = network or DEFAULT_NETWORK
    h = action_hash(action, vault_address, nonce)
    payload = _agent_typed_data(h, network)
    structured = encode_typed_data(full_message=payload)
    acct = Account.from_key(agent_private_key)
    signed = acct.sign_message(structured)
    return {"r": to_hex(signed.r), "s": to_hex(signed.s), "v": signed.v}


def recover_agent_address(
    action: dict,
    vault_address: Optional[str],
    nonce: int,
    signature: dict,
    network: Optional[dict] = None,
) -> str:
    """署名からアドレスを復元する(ユニットテスト用)。"""
    network = network or DEFAULT_NETWORK
    h = action_hash(action, vault_address, nonce)
    payload = _agent_typed_data(h, network)
    structured = encode_typed_data(full_message=payload)
    return Account.recover_message(
        structured, vrs=(signature["v"], signature["r"], signature["s"])
    )


def build_approve_agent_typed_data(
    agent_address: str,
    agent_name: str = "TradeAgent",
    nonce: Optional[int] = None,
    signature_chain_id: Optional[int] = None,
    network: Optional[dict] = None,
) -> dict:
    """ApproveAgent の EIP-712 typed-data ペイロードをJSONで組み立てる。

    メイン鍵はVPSに無いため実署名はしない。ユーザーが別途(MetaMask等、txflowNetwork の
    signatureChainId=42161=Arbitrumに接続した状態で) `eth_signTypedData_v4` に投げる用の
    JSONを返す。署名後は `submit_approve_agent()` (txflow_client.py) で /exchange に送る。
    """
    network = network or DEFAULT_NETWORK
    nonce = nonce if nonce is not None else int(time.time() * 1000)
    chain_id = signature_chain_id if signature_chain_id is not None else network["signatureChainId"]
    return {
        "domain": {
            "name": network["txflowNetwork"],
            "version": str(network["apiVersion"]),
            "chainId": chain_id,
            "verifyingContract": ZERO_ADDRESS,
        },
        "types": {
            "ApproveAgent": [
                {"name": "txflowNetwork", "type": "string"},
                {"name": "chainId", "type": "uint32"},
                {"name": "apiVersion", "type": "uint32"},
                {"name": "agentAddress", "type": "address"},
                {"name": "agentName", "type": "string"},
                {"name": "nonce", "type": "uint64"},
            ],
            "EIP712Domain": _EIP712_DOMAIN_TYPE,
        },
        "primaryType": "ApproveAgent",
        "message": {
            "txflowNetwork": network["txflowNetwork"],
            "chainId": network["chainId"],
            "apiVersion": network["apiVersion"],
            "agentAddress": agent_address,
            "agentName": agent_name,
            "nonce": nonce,
        },
    }


class NonceManager:
    """ms タイムスタンプ単調増加ノンス。JS の generateUniqueNonce() と同一ロジック:
    現在時刻が最後に払い出した値以下なら+1、それ以外は現在時刻を採用。"""

    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        now = int(time.time() * 1000)
        if now <= self._last:
            self._last += 1
        else:
            self._last = now
        return self._last
