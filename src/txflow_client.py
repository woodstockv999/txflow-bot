"""txflow (https://api.txflow.com, Hyperliquid完全フォーク) の REST/WS クライアント。

## 確定済みエンドポイント(バンドル抽出、2026-07-22。index-main.js より該当箇所とも一致)
- `VITE_API_BASE_URL = "https://api.txflow.com"`
- `POST /info {"type":"l2Book","coin": <coinIndex文字列>}`
- `POST /info {"type":"clearinghouseState","user": <address>}`
- WS: `wss://api.txflow.com/ws`。subscribe形式:
  `{"method":"subscribe","type":"l2Book","subscription":{"type":"l2Book","coin":"1","tick":""}}`
  (HLの`{"coin":"BTC"}`ではなく数値インデックス文字列を使う点が違う。バンドル内`s6.l2Book`定義で確認)

## /exchange envelope はaction種別ごとに違う(2026-07-22、取引画面チャンクTrade.js/
PositionsModule.js実測。詳細根拠は`txflow_signing.py`冒頭コメント参照)
- order: `{action:{type:"order",grouping,orders}, signature:{r,s,v}, nonce}` の3キーのみ。
  **vaultAddress/expiresAfterは送らない**(buildNormalOrderParams実測)。
- cancel: `{action:{type:"cancel",cancels}, signature, nonce, vaultAddress:null}` の4キー。
- ハッシュ対象(署名対象のmsgpack)は wire actionから"type"を除いたもの
  (`signing.wire_action_for_hash()`)。order/cancelの詳しいキー構成・順序・oidの
  ForceUint64扱いは`txflow_signing.py`のモジュールdocstring参照。

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


def _load_symbol_meta() -> dict[str, dict]:
    """data/instruments.json から {symbol: {"coin_index": externalId文字列,
    "size_decimals": int}} を作る。
    - coin_index: instruments[].instrumentType=="perps" の externalId(BTC=1,ETH=2実測済み)。
    - size_decimals: instruments[].assetBase を鍵に assets[].sizeDecimals を引く
      (Fable指摘: サイズ量子化に使う。priceTick相当は静的ファイルに無いため
      `TxflowClient._price_decimals()` でl2Bookの実測小数桁数から動的に推定する)。
    """
    with open(_INSTRUMENTS_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assets_by_index = {a["index"]: a for a in raw.get("assets", [])}
    out: dict[str, dict] = {}
    for inst in raw.get("instruments", []):
        if inst.get("instrumentType") != "perps":
            continue
        name = inst.get("name", "")
        symbol = name.split("-")[0] if "-" in name else name
        asset = assets_by_index.get(inst.get("assetBase"))
        # sizeDecimals は負値もありうる(例: DOGE=-1)。負値は「10^|n|単位に丸める」の意味と見て
        # quantize_size 側で round()の負桁対応を使う。無ければ保守的に0(整数)にフォールバック。
        size_decimals = int(asset["sizeDecimals"]) if asset else 0
        out[symbol] = {"coin_index": str(inst["externalId"]), "size_decimals": size_decimals}
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
        self._symbol_meta: dict[str, dict] = _load_symbol_meta()
        self._price_decimals_cache: dict[str, int] = {}

    # ------------------------------------------------------------------ coin index
    def coin_index(self, symbol: str) -> str:
        try:
            return self._symbol_meta[symbol.upper()]["coin_index"]
        except KeyError as e:
            raise KeyError(
                f"txflow coin index 不明: {symbol!r}。data/instruments.json に無い"
                "(新規上場/命名違いの可能性。手動確認要)。"
            ) from e

    # ------------------------------------------------------------------ 価格/サイズ量子化
    # Fable指摘(2026-07-22): サーバは指定桁数を超えるprice/sizeをrejectする見込み。
    # size は data/instruments.json の sizeDecimals(静的)、price はpriceTick相当が静的
    # ファイルに無いため l2Book の実勢価格文字列の小数桁数から動的に推定する
    # (実際に板に乗っている価格は必ずtickの倍数なので、観測された小数桁数はtickを満たす
    # 安全な丸め桁数になる)。
    def quantize_size(self, symbol: str, size: float) -> str:
        decimals = self._symbol_meta.get(symbol.upper(), {}).get("size_decimals", 0)
        if decimals >= 0:
            q = round(size, decimals)
            return f"{q:.{decimals}f}" if decimals > 0 else f"{q:.0f}"
        # 負の桁数(例: -1)は 10^|decimals| 単位への丸めと解釈
        unit = 10 ** (-decimals)
        q = round(size / unit) * unit
        return f"{q:.0f}"

    def _price_decimals(self, symbol: str) -> int:
        symbol = symbol.upper()
        if symbol in self._price_decimals_cache:
            return self._price_decimals_cache[symbol]
        decimals = 2  # 保守的フォールバック(セント単位)
        try:
            book = self.get_l2book(symbol)
            levels = book.get("levels") or [[], []]
            observed = []
            for side in levels:
                for lvl in side[:8]:
                    px = str(lvl.get("px", ""))
                    if "." in px:
                        observed.append(len(px.split(".", 1)[1]))
                    else:
                        observed.append(0)
            if observed:
                decimals = max(observed)
        except Exception as e:
            logger.warning("price decimals推定失敗(%s), 既定%d桁を使用: %s", symbol, decimals, e)
        self._price_decimals_cache[symbol] = decimals
        return decimals

    def quantize_price(self, symbol: str, price: float) -> str:
        decimals = self._price_decimals(symbol)
        q = round(price, decimals)
        return f"{q:.{decimals}f}" if decimals > 0 else f"{q:.0f}"

    def price_decimals(self, symbol: str) -> int:
        """価格の小数桁数(public)。tick=10^-decimals。"""
        return self._price_decimals(symbol)

    def price_tick(self, symbol: str) -> float:
        """価格の最小刻み。l2Bookの価格文字列から動的推定した桁数の逆数。"""
        return 10 ** -self._price_decimals(symbol)

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

    def get_open_orders(self, user: Optional[str] = None) -> Any:
        addr = user or self.main_address
        if not addr:
            raise ValueError("user address が無い")
        return self.info("openOrders", user=addr)

    def get_instruments_live(self) -> Any:
        """/info type=instruments を試す(未確認エンドポイント。バンドル内に呼び出し箇所が
        見つからず、instruments.json がどう採取されたか不明なため、失敗しても静的ファイルに
        フォールバックできるよう例外は呼び出し側で処理する前提)。"""
        return self.info("instruments")

    # ------------------------------------------------------------------ /exchange (署名必要)
    def _require_agent_key(self) -> None:
        if not self.agent_private_key:
            raise RuntimeError(
                "agent_private_key が設定されていない。実弾発注は不可"
                "(TXFLOW_AGENT_PRIVATE_KEY 未設定 = 意図した安全装置)。"
            )

    def _sign_wire_action(self, wire_action: dict, vault_address: Optional[str]) -> tuple[dict, int]:
        """wire_action(サーバに送る"type"付きのaction)から、署名対象("type"を除いたdict、
        signing.wire_action_for_hash参照)を作ってagent鍵で署名する。"""
        self._require_agent_key()
        hash_action = signing.wire_action_for_hash(wire_action)
        nonce = self._nonce.next()
        sig = signing.sign_l1_action(self.agent_private_key, hash_action, vault_address, nonce, self.network)
        return sig, nonce

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

    # ------------------------------------------------------------------ order/cancel
    # 2026-07-22実測(Trade.js buildNormalOrderParams / PositionsModule.js cancelOrder。
    # 根拠の詳細は txflow_signing.py モジュールdocstring参照):
    # - order wire: {a,b,p,s,r,t} キー順はHLと同一。action全体は{type,grouping,orders}
    #   (groupingがordersより先)。envelopeは{action,signature,nonce}の3キーのみ
    #   (vaultAddress/expiresAfterは送らない)。
    # - tifの実際の文字列値は小文字("gtc"/"post_only"/"ioc")。post-only相当は"post_only"。
    # - cancel wire: {a,o} (oidは普通の数値)。action全体は{type,cancels}。
    #   envelopeは{action,signature,nonce,vaultAddress:null}の4キー。
    TIF_GTC = "gtc"
    TIF_POST_ONLY = "post_only"
    TIF_IOC = "ioc"

    def build_limit_order_wire(
        self,
        symbol: str,
        is_buy: bool,
        price: float,
        size: float,
        reduce_only: bool = False,
        tif: str = TIF_POST_ONLY,
        cloid: Optional[str] = None,
    ) -> dict:
        # p/s は wire でも末尾ゼロ除去必須(2026-07-22実測): s="0.0030" で送ると
        # 署名側は "0.003" に正規化してハッシュするがサーバは wire 文字列のまま再計算する
        # ため Authorization failed になる。wire==正規化済み文字列 に揃える。
        order_wire = {
            "a": int(self.coin_index(symbol)),
            "b": is_buy,
            "p": signing._trim_trailing_zeros(self.quantize_price(symbol, price)),
            "s": signing._trim_trailing_zeros(self.quantize_size(symbol, size)),
            "r": reduce_only,
            "t": {"limit": {"tif": tif}},
        }
        if cloid is not None:
            # "c"フィールド(client order id)。2026-07-22 scripts/cloid_probe.py で実弾検証済み:
            # openOrdersにcloidがそのままエコーされることを確認(以後oid特定の一次手段に採用)。
            order_wire["c"] = cloid
        return {"type": "order", "grouping": "na", "orders": [order_wire]}

    @staticmethod
    def new_cloid() -> str:
        """実測(2026-07-22 scripts/cloid_probe.py): txflowのcloidは HL標準の `0x`+32hex(16バイト)
        形式では **"Invalid cloid format" で拒否される**。UUID4文字列
        ("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")は受理され、openOrdersにそのままエコーされる
        ことを確認済み(place->openOrders確認->oid経由cancel->確認、まで成功)。"""
        import uuid

        return str(uuid.uuid4())

    def build_cancel_wire(self, symbol: str, oid: int) -> dict:
        return {"type": "cancel", "cancels": [{"a": int(self.coin_index(symbol)), "o": int(oid)}]}

    def place_limit_order(self, symbol: str, is_buy: bool, price: float, size: float,
                           reduce_only: bool = False, tif: str = TIF_POST_ONLY,
                           cloid: Optional[str] = None) -> Any:
        wire_action = self.build_limit_order_wire(symbol, is_buy, price, size, reduce_only, tif, cloid)
        sig, nonce = self._sign_wire_action(wire_action, vault_address=None)
        body = {
            "action": wire_action,
            "signature": {"r": sig["r"], "s": sig["s"], "v": sig["v"]},
            "nonce": nonce,
        }
        return self._post("/exchange", body)

    def cancel_order(self, symbol: str, oid: int) -> Any:
        wire_action = self.build_cancel_wire(symbol, oid)
        # ハッシュ対象では oid を ForceUint64 でラップする(txflow_signing.py参照: 実測で
        # JS側は BigInt(oid) を常に固定8バイトでmsgpackエンコードしていたため)。
        hash_action = signing.wire_action_for_hash(wire_action)
        hash_action["cancels"] = [
            {**c, "o": signing.ForceUint64(c["o"])} for c in hash_action["cancels"]
        ]
        self._require_agent_key()
        nonce = self._nonce.next()
        sig = signing.sign_l1_action(self.agent_private_key, hash_action, None, nonce, self.network)
        body = {
            "action": wire_action,
            "signature": {"r": sig["r"], "s": sig["s"], "v": sig["v"]},
            "nonce": nonce,
            "vaultAddress": None,
        }
        return self._post("/exchange", body)


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
