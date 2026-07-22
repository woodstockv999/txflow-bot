"""txflow (https://api.txflow.com, Hyperliquid完全フォーク) の REST/WS クライアント。

## 確定済みエンドポイント(バンドル抽出、2026-07-22。index-main.js より該当箇所とも一致)
- `VITE_API_BASE_URL = "https://api.txflow.com"`
- `POST /info {"type":"l2Book","coin": <coinIndex文字列>}`
- `POST /info {"type":"clearinghouseState","user": <address>}`
- `POST /exchange` に `{action, nonce, signature:{r,s,v}, vaultAddress, expiresAfter}` を送信
  (setReferrer実装 `A2()` から実測。`isFrontend` フラグが付くケースもあったが用途不明のため送らない)
- WS: `wss://api.txflow.com/ws`。subscribe形式:
  `{"method":"subscribe","type":"l2Book","subscription":{"type":"l2Book","coin":"1","tick":""}}`
  (HLの`{"coin":"BTC"}`ではなく数値インデックス文字列を使う点が違う。バンドル内`s6.l2Book`定義で確認)

## coin index の解決 (`data/instruments.json`)
バンドルの実行時ネットワークタブから採取したと見られる `instruments.json` の `instruments`
配列に `{name:"BTC-USDC", assetBase:2, externalId:1, instrumentType:"perps"}` の形で
**perp の coin index (=externalId) が直接載っている**ため、これを一次情報として使う
(`assetBase`(=`assets`配列の`index`、スポットのトークン登録番号)から`-1`する式を手計算しなくて
よい。BTC externalId=1 / ETH externalId=2 と一致確認済み。**タスク仕様書の「SOL="14"」は
instruments.json実測(SOL externalId=13)と矛盾しており、タイポと判断してexternalIdを正とする**)。
この静的ファイルはコイン新規上場で古くなりうるので、シンボル解決に失敗したら例外を投げて
明示的に気づけるようにしてある(黙ってフォールバックしない)。

## Cloudflare対策
CFがPythonの既定UA(`python-requests/...`)を403で弾くため、全リクエストにブラウザ風UAを付与する。
CFのチャレンジページはJSONでなくHTMLで返る(content-typeがtext/htmlになる)ので、JSON decode前に
検知してリトライ対象にする。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from . import txflow_signing as signing

logger = logging.getLogger("txflow_client")

BASE_URL = "https://api.txflow.com"
WS_URL = "wss://api.txflow.com/ws"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_INSTRUMENTS_PATH = Path(__file__).resolve().parent.parent / "data" / "instruments.json"


class CloudflareBlockedError(RuntimeError):
    pass


class TxflowApiError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _load_coin_index_map() -> dict[str, str]:
    """data/instruments.json の instruments[].instrumentType=="perps" から
    {symbol: externalId文字列} を作る。symbolは "BTC-USDC" の "-USDC" 前の部分。"""
    with open(_INSTRUMENTS_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: dict[str, str] = {}
    for inst in raw.get("instruments", []):
        if inst.get("instrumentType") != "perps":
            continue
        name = inst.get("name", "")
        symbol = name.split("-")[0] if "-" in name else name
        out[symbol] = str(inst["externalId"])
    return out


class TxflowClient:
    """REST(/info, /exchange) クライアント。agent秘密鍵が無い場合 exchange() は例外を投げる
    (実弾防止の二重ガードの1つ目。2つ目は呼び出し側=hedge_bot/main.pyのdry_run既定)。
    """

    def __init__(
        self,
        agent_private_key: Optional[str] = None,
        main_address: Optional[str] = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        max_retries: int = 5,
        network: Optional[dict] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.agent_private_key = agent_private_key
        self.main_address = main_address
        self.network = network or signing.DEFAULT_NETWORK
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": _UA,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://app.txflow.com",
                "Referer": "https://app.txflow.com/",
            }
        )
        self._nonce = signing.NonceManager()
        self._coin_index: dict[str, str] = _load_coin_index_map()

    # ------------------------------------------------------------------ coin index
    def coin_index(self, symbol: str) -> str:
        try:
            return self._coin_index[symbol.upper()]
        except KeyError as e:
            raise KeyError(
                f"txflow coin index 不明: {symbol!r}。data/instruments.json に無い"
                "(新規上場/命名違いの可能性。手動確認要)。"
            ) from e

    # ------------------------------------------------------------------ low-level HTTP
    def _post(self, path: str, body: dict) -> Any:
        url = f"{self.base_url}{path}"
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.post(url, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = e
                logger.warning("txflow POST %s failed (attempt %d/%d): %s", path, attempt, self.max_retries, e)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue

            ctype = resp.headers.get("content-type", "")
            if "text/html" in ctype or resp.text.lstrip()[:15].lower().startswith("<!doctype html"):
                last_exc = CloudflareBlockedError(
                    f"{path}: Cloudflareと思われるHTML応答 (status={resp.status_code})"
                )
                if resp.status_code == 403 or "cloudflare" in resp.text.lower():
                    logger.warning("txflow POST %s: Cloudflareブロック疑い、リトライ (attempt %d/%d)", path, attempt, self.max_retries)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = TxflowApiError(f"{path}: HTTP {resp.status_code}", resp.status_code, resp.text[:500])
                logger.warning("txflow POST %s: %s, リトライ (attempt %d/%d)", path, last_exc, attempt, self.max_retries)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue

            if resp.status_code >= 400:
                raise TxflowApiError(f"{path}: HTTP {resp.status_code}: {resp.text[:500]}", resp.status_code, resp.text)

            try:
                return resp.json()
            except ValueError as e:
                raise TxflowApiError(f"{path}: JSON decode失敗: {resp.text[:300]}") from e

        raise TxflowApiError(f"{path}: {self.max_retries}回リトライしても失敗") from last_exc

    # ------------------------------------------------------------------ /info (read-only, 無認証)
    def info(self, req_type: str, **kwargs) -> Any:
        body = {"type": req_type, **kwargs}
        return self._post("/info", body)

    def get_l2book(self, symbol: str) -> Any:
        return self.info("l2Book", coin=self.coin_index(symbol))

    def get_clearinghouse_state(self, user: Optional[str] = None) -> Any:
        addr = user or self.main_address
        if not addr:
            raise ValueError("user address が無い")
        return self.info("clearinghouseState", user=addr)

    def get_user_fills(self, user: Optional[str] = None) -> Any:
        """取消×約定レースの検知用(perpl pair_hedgeでの実測知見: 取消後は必ずfills/ポジションで
        再確認する)。`type=userFills` はHL標準だが txflow での動作は未検証(agent鍵未投入のため
        実弾テスト待ち)。"""
        addr = user or self.main_address
        if not addr:
            raise ValueError("user address が無い")
        return self.info("userFills", user=addr)

    def get_instruments_live(self) -> Any:
        """/info type=instruments を試す(未確認エンドポイント。バンドル内に呼び出し箇所が
        見つからず、instruments.json がどう採取されたか不明なため、失敗しても静的ファイルに
        フォールバックできるよう例外は呼び出し側で処理する前提)。"""
        return self.info("instruments")

    # ------------------------------------------------------------------ /exchange (署名必要)
    def exchange(self, action: dict, vault_address: Optional[str] = None) -> Any:
        if not self.agent_private_key:
            raise RuntimeError(
                "agent_private_key が設定されていない。実弾発注は不可"
                "(TXFLOW_AGENT_PRIVATE_KEY 未設定 = 意図した安全装置)。"
            )
        nonce = self._nonce.next()
        sig = signing.sign_l1_action(self.agent_private_key, action, vault_address, nonce, self.network)
        body = {
            "action": action,
            "nonce": nonce,
            "signature": {"r": sig["r"], "s": sig["s"], "v": sig["v"]},
            "vaultAddress": vault_address,
            "expiresAfter": None,
        }
        return self._post("/exchange", body)

    def submit_approve_agent(self, agent_address: str, agent_name: str, nonce: int, signature: dict) -> Any:
        """ユーザーが別途メインウォレットで署名した ApproveAgent typed-data の signature を
        /exchange に提出する(このクライアント単体では main鍵が無いため呼べない=手動フロー)。"""
        net = self.network
        body = {
            "action": {
                "type": "approveAgent",
                "txflowNetwork": net["txflowNetwork"],
                "chainId": net["chainId"],
                "apiVersion": net["apiVersion"],
                "agentAddress": agent_address,
                "agentName": agent_name,
                "nonce": nonce,
            },
            "nonce": nonce,
            "signatureChainId": net["signatureChainId"],
            "signature": signature,
        }
        return self._post("/exchange", body)

    # ------------------------------------------------------------------ order helpers
    # NOTE: 発注action("type":"order")のフィールド構成(a/b/p/s/r/t)はバンドル未確認。
    # HL公式Python SDK (hyperliquid.utils.signing.order_wires_to_order_action) と同一と仮定。
    # 実弾テストで最初に確認すべき項目(README.md参照)。
    def build_limit_order_action(
        self,
        symbol: str,
        is_buy: bool,
        price: str,
        size: str,
        reduce_only: bool = False,
        tif: str = "Gtc",
    ) -> dict:
        return {
            "type": "order",
            "orders": [
                {
                    "a": int(self.coin_index(symbol)),
                    "b": is_buy,
                    "p": price,
                    "s": size,
                    "r": reduce_only,
                    "t": {"limit": {"tif": tif}},
                }
            ],
            "grouping": "na",
        }

    def build_cancel_action(self, symbol: str, oid: int) -> dict:
        return {"type": "cancel", "cancels": [{"a": int(self.coin_index(symbol)), "o": oid}]}

    def place_limit_order(self, symbol: str, is_buy: bool, price: str, size: str,
                           reduce_only: bool = False, tif: str = "Gtc") -> Any:
        action = self.build_limit_order_action(symbol, is_buy, price, size, reduce_only, tif)
        return self.exchange(action)

    def cancel_order(self, symbol: str, oid: int) -> Any:
        return self.exchange(self.build_cancel_action(symbol, oid))


class TxflowWS:
    """l2Book の WS購読。websocket-client の WebSocketApp をバックグラウンドスレッドで回し、
    最新の板を dict で保持する。認証不要(公開板データのみ)。"""

    def __init__(self, symbols: list[str], coin_index_fn: Callable[[str], str], ws_url: str = WS_URL):
        self.symbols = symbols
        self._symbols_upper = {s.upper() for s in symbols}
        self._coin_index_fn = coin_index_fn
        self.ws_url = ws_url
        self._books: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()

    def _on_open(self, ws):
        self._connected.set()
        for sym in self.symbols:
            idx = self._coin_index_fn(sym)
            sub = {
                "method": "subscribe",
                "type": "l2Book",
                "subscription": {"type": "l2Book", "coin": idx, "tick": ""},
            }
            ws.send(json.dumps(sub))
            logger.info("txflow WS subscribe l2Book %s (coin=%s)", sym, idx)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except ValueError:
            return
        if data.get("channel") != "l2Book":
            return
        book = data.get("data") or {}
        # 実測: data.coin は購読時に送った数値インデックス("1")ではなく
        # "BTC-USDC" のようなシンボル名文字列で返ってくる(2026-07-22 smoke test で確認)。
        raw_coin = str(book.get("coin", ""))
        sym = raw_coin.split("-")[0].upper() if "-" in raw_coin else raw_coin.upper()
        if sym not in self._symbols_upper:
            return
        with self._lock:
            self._books[sym] = book

    def _on_error(self, ws, error):
        logger.warning("txflow WS error: %s", error)

    def _on_close(self, ws, code, msg):
        self._connected.clear()
        logger.info("txflow WS closed: %s %s", code, msg)

    def _run(self):
        import websocket

        backoff = 1.0
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self.ws_url,
                header=[f"User-Agent: {_UA}"],
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._ws.run_forever(ping_interval=25, ping_timeout=10)
            if self._stop.is_set():
                break
            logger.warning("txflow WS disconnected, %.1fs後に再接続", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="txflow-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)

    def wait_connected(self, timeout: float = 10.0) -> bool:
        return self._connected.wait(timeout)

    def best_bid_ask(self, symbol: str) -> Optional[tuple[float, float]]:
        with self._lock:
            book = self._books.get(symbol.upper())
        if not book:
            return None
        levels = book.get("levels") or [[], []]
        bids, asks = levels[0], levels[1]
        if not bids or not asks:
            return None
        try:
            return float(bids[0]["px"]), float(asks[0]["px"])
        except (KeyError, ValueError, TypeError):
            return None


def _smoke_test() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = TxflowClient()
    print("== instruments (static, data/instruments.json) ==")
    print("BTC ->", client.coin_index("BTC"), " ETH ->", client.coin_index("ETH"))

    print("== l2Book BTC ==")
    book = client.get_l2book("BTC")
    levels = book.get("levels") if isinstance(book, dict) else None
    if levels:
        print("best bid:", levels[0][0] if levels[0] else None)
        print("best ask:", levels[1][0] if levels[1] else None)
    else:
        print(json.dumps(book)[:500])

    print("== l2Book ETH ==")
    book2 = client.get_l2book("ETH")
    levels2 = book2.get("levels") if isinstance(book2, dict) else None
    if levels2:
        print("best bid:", levels2[0][0] if levels2[0] else None)
        print("best ask:", levels2[1][0] if levels2[1] else None)

    print("== clearinghouseState 0x7a46C513e0Cd4B5e0b5BDBdf5a1A721cbC614c2b ==")
    chs = client.get_clearinghouse_state("0x7a46C513e0Cd4B5e0b5BDBdf5a1A721cbC614c2b")
    print(json.dumps(chs)[:800])


if __name__ == "__main__":
    import sys

    if "--smoke" in sys.argv:
        _smoke_test()
    else:
        print("usage: python -m src.txflow_client --smoke")
