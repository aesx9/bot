"""API privada de Kraken Futures simulada (respx), con respuestas con la forma
de los ejemplos de la documentación oficial. Verifica la firma de cada petición."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

import httpx
import respx
from pydantic import SecretStr

from copybot.credentials import KrakenCredentials

SECRET = base64.b64encode(b"secreto-de-prueba-no-real").decode()
CREDS = KrakenCredentials(api_key=SecretStr("clave-publica-prueba"), api_secret=SecretStr(SECRET))
SERVER_TIME = "2026-10-08T12:00:00.000Z"


def _ok(**kw: Any) -> httpx.Response:
    return httpx.Response(200, text=json.dumps({"result": "success", "serverTime": SERVER_TIME,
                                                **kw}, default=str))


class FakeKraken:
    def __init__(self) -> None:
        self.flex: dict[str, Any] = {
            "type": "multiCollateralMarginAccount", "currencies": {},
            "balanceValue": 585, "portfolioValue": 585, "collateralValue": 585,
            "initialMargin": 0, "initialMarginWithOrders": 0, "maintenanceMargin": 0,
            "pnl": 0, "unrealizedFunding": 0, "totalUnrealized": 0,
            "totalUnrealizedAsMargin": 0, "marginEquity": 585, "availableMargin": 585,
        }
        self.positions: list[dict[str, Any]] = []
        self.open_orders: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.key_check: dict[str, Any] = {
            "apiKey": "clave-publica-prueba",  # pragma: allowlist secret
            "accountUid": "x", "iiban": None,
            "createdAt": "2026-01-01T00:00:00Z",
            "permissions": {"general": "FULL_ACCESS", "transfer": "NO_ACCESS"},
            "allowedCidrBlock": None, "allowedCidrBlocks": ["203.0.113.7/32"],
        }
        # Comportamiento de sendorder para órdenes IOC: "fill" | "none" | estado de error
        self.ioc_mode = "fill"
        self.fill_price = Decimal(100)
        self.lose_response = False  # la orden se ejecuta pero la respuesta se pierde
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.bad_signatures = 0

    # --- utilidades ---

    def _check_signature(self, request: httpx.Request, data: str) -> None:
        path = request.url.path.removeprefix("/derivatives")
        digest = hashlib.sha256((data + request.headers["Nonce"] + path).encode()).digest()
        expected = base64.b64encode(
            hmac.new(base64.b64decode(SECRET), digest, hashlib.sha512).digest()).decode()
        if request.headers.get("Authent") != expected or \
                request.headers.get("APIKey") != "clave-publica-prueba":
            self.bad_signatures += 1

    def sends(self, order_type: str | None = None) -> list[dict[str, str]]:
        return [p for m, path, p in self.calls if path.endswith("/sendorder")
                and (order_type is None or p.get("orderType") == order_type)]

    def cancels(self) -> list[dict[str, str]]:
        return [p for _, path, p in self.calls if path.endswith("/cancelorder")]

    def install(self, router: respx.Router) -> None:
        router.route(host="futures.kraken.com").mock(side_effect=self.handle)

    # --- servidor ---

    def handle(self, request: httpx.Request) -> httpx.Response:
        data = (request.url.query.decode() if request.method == "GET"
                else request.content.decode())
        self._check_signature(request, data)
        params = dict(parse_qsl(data))
        path = request.url.path
        self.calls.append((request.method, path, params))
        if path == "/derivatives/api/v3/accounts":
            return _ok(accounts={"flex": self.flex})
        if path == "/derivatives/api/v3/openpositions":
            return _ok(openPositions=self.positions)
        if path == "/derivatives/api/v3/openorders":
            return _ok(openOrders=self.open_orders)
        if path == "/derivatives/api/v3/fills":
            return _ok(fills=self.fills)
        if path == "/derivatives/api/v3/orders/status":
            return _ok(orders=[])
        if path == "/derivatives/api/v3/cancelorder":
            self.open_orders = [o for o in self.open_orders
                                if o.get("cliOrdId") != params.get("cliOrdId")]
            return _ok(cancelStatus={"status": "cancelled"})
        if path == "/derivatives/api/v3/sendorder":
            return self._send(params)
        if path == "/api/history/v3/account-log":
            since = int(params.get("since", "0"))
            logs = [e for e in self.logs if e["_ms"] >= since]
            return httpx.Response(200, json={"accountUid": "x", "logs": [
                {k: v for k, v in e.items() if k != "_ms"} for e in logs]})
        if path == "/api/auth/v1/api-keys/v3/check":
            return httpx.Response(200, json=self.key_check)
        return httpx.Response(404, json={"result": "error", "error": "notFound"})

    def _send(self, p: dict[str, str]) -> httpx.Response:
        if p["orderType"] == "stp":
            self.open_orders.append({
                "order_id": f"o{len(self.open_orders)}", "cliOrdId": p["cliOrdId"],
                "status": "untouched", "side": p["side"], "orderType": "stop",
                "symbol": p["symbol"], "stopPrice": p["stopPrice"], "filledSize": 0,
                "unfilledSize": p["size"], "reduceOnly": p["reduceOnly"] == "true",
                "triggerSignal": p["triggerSignal"],
            })
            return _ok(sendStatus={"status": "placed", "order_id": "x", "orderEvents": []})
        if self.ioc_mode == "none":
            return _ok(sendStatus={"status": "iocWouldNotExecute", "orderEvents": [
                {"type": "REJECT", "uid": "u", "order": None, "reason": "IOC_WOULD_NOT_EXECUTE"}]})
        if self.ioc_mode != "fill":
            return _ok(sendStatus={"status": self.ioc_mode})
        size = Decimal(p["size"])
        self._apply(p["symbol"], size if p["side"] == "buy" else -size)
        self.fills.append({"cliOrdId": p["cliOrdId"], "fillTime": SERVER_TIME,
                           "fillType": "taker", "fill_id": "f", "order_id": "o",
                           "price": str(self.fill_price), "side": p["side"], "size": p["size"],
                           "symbol": p["symbol"]})
        if self.lose_response:
            raise httpx.ReadTimeout("respuesta perdida")
        half = size / 2
        return _ok(sendStatus={"status": "placed", "order_id": "o", "orderEvents": [
            {"type": "EXECUTION", "executionId": "e1", "price": str(self.fill_price),
             "amount": str(half)},
            {"type": "EXECUTION", "executionId": "e2", "price": str(self.fill_price + 2),
             "amount": str(size - half)},
        ]})

    def _apply(self, symbol: str, delta: Decimal) -> None:
        cur = Decimal(0)
        for pos in self.positions:
            if pos["symbol"] == symbol:
                cur = Decimal(str(pos["size"])) * (1 if pos["side"] == "long" else -1)
        new = cur + delta
        self.positions = [p for p in self.positions if p["symbol"] != symbol]
        if new:
            self.positions.append({"symbol": symbol, "side": "long" if new > 0 else "short",
                                   "size": str(abs(new)), "price": str(self.fill_price),
                                   "unrealizedPnl": 0, "unrealizedFunding": 0})
