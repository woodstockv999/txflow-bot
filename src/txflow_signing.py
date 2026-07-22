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

## 未確認・要注意
- `placeLimitOrder` の action JSON の中身( `a/b/p/s/r/t` フィールド名・`grouping:"na"` 等)は
  このバンドル(エントリーチャンクのみ)には含まれておらず、取引画面の遅延ロードチャンクに
  あると見られる。txflowは"Hyperliquid完全フォーク"と明言されているため、HL公式SDKの
  `OrderWire`/`order_wires_to_order_action` と同一のフィールド名・構造であると仮定して実装した
  (`txflow_client.py` 側)。実弾テスト($10 BTC指値1発)で最初に確認すべき最重要項目。
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

import msgpack
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


def action_hash(action: dict, vault_address: Optional[str], nonce: int) -> bytes:
    """actionHash(action, vaultAddress, nonce) 完全再現。HL公式SDKと同一アルゴリズム
    (tests/test_signing.py のゴールデンベクタで検証済み)。"""
    normalized = _normalize_action(action)
    packed = msgpack.packb(normalized, use_bin_type=True)
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
