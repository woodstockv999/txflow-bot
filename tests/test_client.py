"""TxflowClient のユニットテスト(ネットワーク不要)。

回帰テスト(2026-07-22実測、Fable修正): quantize_size()/quantize_price() は指定桁数まで
ゼロ埋めした文字列を返す(例: sizeDecimals=4のBTCで size=0.003 -> "0.0030")。この
ゼロ埋め済み文字列をそのままwireの"p"/"s"に使うと、署名計算側(signing._trim_trailing_zeros)
は末尾ゼロを除去した"0.003"でハッシュするのに対し、サーバはwireの生文字列("0.0030")で
再計算するため一致せず `Authorization failed` になることが実弾稼働で確認された
(`~/apps/txflow-bot` 2026-07-22 19:35-19:48 BTC稼働時)。
`build_limit_order_wire()` は "p"/"s" に `signing._trim_trailing_zeros()` を適用済みで
あることをここで固定する。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import txflow_signing as signing
from src.txflow_client import TxflowClient


def _client() -> TxflowClient:
    return TxflowClient()


def test_quantize_size_zero_pads_to_size_decimals():
    c = _client()
    # BTCはdata/instruments.jsonでsizeDecimals=4 -> ゼロ埋めされた文字列が返る(これ自体は正しい)
    assert c.quantize_size("BTC", 0.003) == "0.0030"
    assert c.quantize_size("ETH", 0.045) == "0.045"  # ETHはsizeDecimals=3


def test_build_limit_order_wire_strips_trailing_zeros_in_size():
    c = _client()
    c._price_decimals_cache["BTC"] = 1  # l2Book実測相当を疑似注入、ネットワーク呼び出し回避

    wire = c.build_limit_order_wire("BTC", True, 65000.0, 0.003, tif=c.TIF_POST_ONLY)
    size_str = wire["orders"][0]["s"]
    assert size_str == "0.003"
    assert not size_str.endswith("0") or "." not in size_str


def test_build_limit_order_wire_various_trailing_zero_sizes():
    c = _client()
    c._price_decimals_cache["BTC"] = 1
    cases = [
        (0.0030, "0.003"),
        (0.0010, "0.001"),
        (0.0020, "0.002"),
        (1.0000, "1"),
    ]
    for raw_size, expected in cases:
        wire = c.build_limit_order_wire("BTC", True, 65000.0, raw_size, tif=c.TIF_POST_ONLY)
        got = wire["orders"][0]["s"]
        assert got == expected, f"size={raw_size} -> got {got!r}, want {expected!r}"


def test_build_limit_order_wire_strips_trailing_zeros_in_price():
    c = _client()
    c._price_decimals_cache["BTC"] = 1  # 桁数1と疑似注入(BTCの実測傾向に合わせる)
    wire = c.build_limit_order_wire("BTC", True, 65000.0, 0.003, tif=c.TIF_POST_ONLY)
    price_str = wire["orders"][0]["p"]
    assert price_str == "65000"
    assert not price_str.endswith(".0")


def test_wire_size_and_price_match_signing_trim_helper():
    """quantize後にtrimした結果が signing._trim_trailing_zeros の出力と一致することを固定する
    (build_limit_order_wire がこのヘルパーを実際に使っていることの間接確認)。"""
    c = _client()
    c._price_decimals_cache["ETH"] = 2
    wire = c.build_limit_order_wire("ETH", False, 1900.50, 0.0450, tif=c.TIF_POST_ONLY)
    order = wire["orders"][0]
    assert order["s"] == signing._trim_trailing_zeros(c.quantize_size("ETH", 0.0450))
    assert order["p"] == signing._trim_trailing_zeros(c.quantize_price("ETH", 1900.50))


def test_new_cloid_is_uuid4_format():
    # 実測(2026-07-22 scripts/cloid_probe.py): txflowはHL標準の0x+32hex形式を
    # "Invalid cloid format"で拒否し、UUID4文字列は受理する。
    import uuid

    c = _client()
    cloid = c.new_cloid()
    parsed = uuid.UUID(cloid)  # 例外なくパースできればUUID形式として妥当
    assert str(parsed) == cloid
    assert not cloid.startswith("0x")


def test_build_limit_order_wire_includes_cloid_when_given():
    c = _client()
    c._price_decimals_cache["BTC"] = 1
    cloid = c.new_cloid()
    wire = c.build_limit_order_wire("BTC", True, 65000.0, 0.003, tif=c.TIF_POST_ONLY, cloid=cloid)
    assert wire["orders"][0]["c"] == cloid
    # cloid未指定時は"c"キー自体が無い(HL標準: 省略可能フィールド)
    wire_no_cloid = c.build_limit_order_wire("BTC", True, 65000.0, 0.003, tif=c.TIF_POST_ONLY)
    assert "c" not in wire_no_cloid["orders"][0]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
