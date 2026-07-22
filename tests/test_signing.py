"""txflow_signing.py のユニットテスト。

action_hash() のゴールデンベクタは Hyperliquid 公式Python SDK
(`hyperliquid.utils.signing.action_hash`, ~/apps/hyperliquid-bot/.venv 内、
`hyperliquid-python-sdk==0.24.0`)を"正"として2026-07-22に生成した。
txflowはHyperliquid完全フォークで、actionHashの前処理(msgpack + nonce8byte +
vaultフラグ)はバンドル解析上バイト単位で同一と確認済みのため、公式SDKの出力と
一致することが本実装の正しさの根拠になる。

生成コマンド(記録用、再現する場合は hyperliquid-bot の venv で):
    from hyperliquid.utils.signing import action_hash
    action_hash({...}, None, 1737500000123, None).hex()
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.txflow_signing import (
    DEFAULT_NETWORK,
    NonceManager,
    action_hash,
    build_approve_agent_typed_data,
    recover_agent_address,
    sign_l1_action,
)

ORDER_ACTION = {
    "type": "order",
    "orders": [
        {"a": 1, "b": True, "p": "50000.5", "s": "0.001", "r": False, "t": {"limit": {"tif": "Gtc"}}}
    ],
    "grouping": "na",
}
CANCEL_ACTION = {
    "type": "cancel",
    "cancels": [{"a": 1, "o": 123456}],
}


def test_action_hash_matches_hyperliquid_sdk_golden_vector_no_vault():
    h = action_hash(ORDER_ACTION, None, 1737500000123)
    assert h.hex() == "0882fb8657f92143749c08ffdfebfcab5de8bb0896fea8508ecd3535e93dd731"


def test_action_hash_matches_hyperliquid_sdk_golden_vector_with_vault():
    h = action_hash(
        CANCEL_ACTION,
        "0x1111111111111111111111111111111111111111",
        1737500000456,
    )
    assert h.hex() == "b9d0f0eca1dc49bd8c2c153193a4bc7a42653d6e81699a8b5df5a1b4d501564f"


def test_trim_trailing_zeros_only_touches_p_and_s():
    # "50000.500" -> "50000.5" ("p" フィールド)。他のキーは触らない。
    action = {"orders": [{"p": "50000.500", "s": "1.000", "note": "1.000"}]}
    h1 = action_hash(action, None, 1)
    h2 = action_hash({"orders": [{"p": "50000.5", "s": "1", "note": "1.000"}]}, None, 1)
    assert h1 == h2


def test_sign_l1_action_recovers_agent_address():
    from eth_account import Account

    agent = Account.create()
    nonce = NonceManager().next()
    sig = sign_l1_action(agent.key.hex(), ORDER_ACTION, None, nonce)
    recovered = recover_agent_address(ORDER_ACTION, None, nonce, sig)
    assert recovered.lower() == agent.address.lower()


def test_sign_l1_action_with_vault_address_recovers_agent_address():
    from eth_account import Account

    agent = Account.create()
    nonce = NonceManager().next()
    vault = "0x2222222222222222222222222222222222222222"
    sig = sign_l1_action(agent.key.hex(), CANCEL_ACTION, vault, nonce)
    recovered = recover_agent_address(CANCEL_ACTION, vault, nonce, sig)
    assert recovered.lower() == agent.address.lower()


def test_signature_changes_if_network_chain_id_differs():
    from eth_account import Account

    agent = Account.create()
    nonce = NonceManager().next()
    sig_mainnet = sign_l1_action(agent.key.hex(), ORDER_ACTION, None, nonce, DEFAULT_NETWORK)
    other_network = {**DEFAULT_NETWORK, "chainId": 999}
    sig_other = sign_l1_action(agent.key.hex(), ORDER_ACTION, None, nonce, other_network)
    assert sig_mainnet != sig_other
    # 別ネットワークのdomainで検証すると address が変わる(=正しいdomainでないと復元できない)
    recovered_wrong_domain = recover_agent_address(ORDER_ACTION, None, nonce, sig_other, DEFAULT_NETWORK)
    assert recovered_wrong_domain.lower() != agent.address.lower()


def test_nonce_manager_monotonic_increasing():
    nm = NonceManager()
    values = [nm.next() for _ in range(50)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_build_approve_agent_typed_data_uses_signature_chain_id_for_domain():
    payload = build_approve_agent_typed_data("0x" + "ab" * 20, nonce=123)
    # domain.chainId はウォレット接続チェーン(Arbitrum=42161)。message.chainId は txflow固有値(869)。
    assert payload["domain"]["chainId"] == DEFAULT_NETWORK["signatureChainId"]
    assert payload["message"]["chainId"] == DEFAULT_NETWORK["chainId"]
    assert payload["primaryType"] == "ApproveAgent"
    assert payload["message"]["agentName"] == "TradeAgent"
    assert payload["message"]["nonce"] == 123


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
