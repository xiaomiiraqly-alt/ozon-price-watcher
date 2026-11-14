"""Synchronise cart, live orders, and manually followed SKUs."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.gateway_client import GatewayError, connect_gateway
from app.prices import choose_card_price, rubles
from app.storage import Notice, Store

logger = logging.getLogger(__name__)
Notifier = Callable[[Notice], Awaitable[None]]


def product_url(sku: str) -> str:
    return f"https://www.ozon.ru/product/{sku}/"


def order_numbers(row: dict[str, Any]) -> list[str]:
    all_nums = [str(s) for s in (row.get("order_numbers") or []) if s]
    if not all_nums and row.get("order_number"):
        all_nums.append(str(row["order_number"]))
    if not all_nums and row.get("detail_link"):
        all_nums.append(str(row["detail_link"]))
    return list(dict.fromkeys(all_nums))


@dataclass
class SyncReport:
    cart: int = 0
    order_items: int = 0
    manual: int = 0
    alerts: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        msg = (f"Проверка завершена. Корзина: {self.cart}, позиции заказов: {self.order_items}, "
               f"добавлено вручную: {self.manual}, уведомления: {self.alerts}.")
        if self.errors:
            msg += "\nОшибки: " + "; ".join(self.errors[:5])
        return msg


class Monitor:
    def __init__(self, store: Store, url: str, token: str, price_mode: str, notify: Notifier):
        self.store = store
        self.url = url
        self.token = token
        self.price_mode = price_mode
        self.notify = notify
        self._lock = asyncio.Lock()

    async def _save(self, report: SyncReport, **kwargs: Any) -> None:
        alert = self.store.observe(**kwargs)
        if alert is not None:
            try:
                await self.notify(alert)
            except Exception:
                logger.exception("Failed sending Telegram notification for item id=%d", alert.item_id)
                report.errors.append(f"уведомление #{alert.item_id} не отправлено")
            else:
                self.store.mark_alert_sent(alert)
                report.alerts += 1

    async def sync(self) -> SyncReport:
        async with self._lock:
            report = SyncReport()
            try:
                async with connect_gateway(self.url,self.token,self.store.user_id) as api:
                    session = await api.call("session_status")
                    if not isinstance(session, dict) or not session.get("signed_in"):
                        report.errors.append("сессия Ozon не авторизована: отправьте /connect")
                        return report
                    try:
                        cart = await api.call("get_cart")
                        if not isinstance(cart, dict) or not isinstance(cart.get("items"), list):
                            raise GatewayError("Unexpected cart response")
                        present: set[str] = set()
                        for item in cart["items"]:
                            sku = str(item.get("id") or "")
                            if not sku.isdigit():
                                continue
                            present.add(sku)
                            await self._save(
                                report, source_type="cart", source_ref="cart", sku=sku,
                                title=item.get("title") or sku, url=product_url(sku),
                                price=rubles(item.get("price")),
                            )
                            report.cart += 1
                        # Do not wrongly retire items when a paginated response was truncated.
                        total = cart.get("total_items")
                        if total is None or int(total) == len(cart["items"]):
                            self.store.retire_missing_cart(present)
                        else:
                            report.errors.append("корзина считана не полностью, удаления проигнорированы")
                    except Exception as exc:
                        logger.exception("Cart sync failed")
                        report.errors.append(f"корзина: {str(exc)[:120]}")

                    price_cache: dict[str, dict[str, Any]] = {}
                    seen_orders: set[str] = set()
                    try:
                        live = await api.call("list_orders", scope="active", limit=100)
                        if not isinstance(live, list):
                            raise GatewayError("Unexpected list_orders response")
                        for row in live:
                            if not isinstance(row, dict):
                                continue
                            for order_ref in order_numbers(row):
                                seen_orders.add(order_ref)
                                try:
                                    positions = await api.call("order_products", order=order_ref)
                                    if not isinstance(positions, list):
                                        raise GatewayError("Invalid order_products response")
                                    for item in positions:
                                        await self._sync_order_item(api, report, order_ref, item, price_cache)
                                except Exception as exc:
                                    logger.exception("Order products failed")
                                    report.errors.append(f"заказ {order_ref}: {str(exc)[:100]}")
                    except Exception as exc:
                        logger.exception("Orders sync failed")
                        report.errors.append(f"заказы: {str(exc)[:120]}")
                    else:
                        # Only consult the archive when an order seen in an earlier run vanished.
                        missing = self.store.seen_order_references() - seen_orders
                        if missing:
                            try:
                                archive = await api.call("list_orders", scope="completed", limit=100)
                                if not isinstance(archive, list):
                                    raise GatewayError("Unexpected archive response")
                                for row in archive:
                                    if not isinstance(row, dict):
                                        continue
                                    for ref in order_numbers(row):
                                        if ref not in missing:
                                            continue
                                        for item in await api.call("order_products", order=ref):
                                            if item.get("received") is not None:
                                                self.store.close_order(
                                                    f"{ref}|{item['shipment_id']}" if item.get("shipment_id") else ref,
                                                    str(item.get("sku", "")),
                                                    "received" if item["received"] else "cancelled")
                            except Exception as exc:
                                logger.exception("Archive reconciliation failed")
                                report.errors.append(f"проверка завершённых заказов: {str(exc)[:100]}")

                    # Reuse product cards across orders and manual subscriptions.
                    for row in self.store.list_items("manual", active_only=True):
                        if row["muted"]:
                            continue
                        try:
                            card = price_cache.get(row["sku"])
                            if card is None:
                                card = await api.call("product_details", sku_or_url=row["sku"])
                                if not isinstance(card, dict):
                                    raise GatewayError("Invalid product response")
                                price_cache[row["sku"]] = card
                            await self._save(report, source_type="manual", source_ref="manual",
                                sku=row["sku"], title=card.get("title") or row["title"],
                                url=product_url(row["sku"]), price=choose_card_price(card, self.price_mode))
                            report.manual += 1
                        except Exception as exc:
                            report.errors.append(f"товар {row['sku']}: {str(exc)[:100]}")
            except Exception as exc:
                logger.exception("Ozon MCP unavailable")
                report.errors.append(f"соединение MCP: {str(exc)[:160]}")
            self.store.set_setting("last_sync", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            return report

    async def _sync_order_item(self, api: Any, report: SyncReport, order_ref: str, item: dict[str, Any], price_cache: dict[str, dict[str, Any]]) -> None:
        if not isinstance(item, dict):
            return
        sku = str(item.get("sku") or "")
        if not sku.isdigit():
            return
        # Separate deliveries can have different receipt states. Include parcel ID when present.
        shipment = str(item.get("shipment_id") or "")
        ref = f"{order_ref}|{shipment}" if shipment else order_ref
        if item.get("received") is not None:
            self.store.close_order(ref, sku, "received" if item["received"] else "cancelled")
            return
        try:
            card = price_cache.get(sku)
            if card is None:
                card = await api.call("product_details", sku_or_url=sku)
                if not isinstance(card, dict):
                    raise GatewayError("Invalid product card")
                price_cache[sku] = card
            current = choose_card_price(card, self.price_mode)
        except Exception as exc:
            current = None
            report.errors.append(f"цена {sku}: {str(exc)[:100]}")
        await self._save(
            report, source_type="order", source_ref=ref, sku=sku,
            title=item.get("title") or sku, url=item.get("url") or product_url(sku),
            base_price=rubles(item.get("price")), price=current,
        )
        report.order_items += 1

    async def add_manual(self, text: str) -> str:
        import re
        match = re.search(r"\d{6,}", text)
        if not match:
            raise ValueError("Нужна ссылка Ozon или числовой SKU (не менее 6 цифр).")
        sku = match.group(0)
        async with self._lock:
            async with connect_gateway(self.url,self.token,self.store.user_id) as api:
                card = await api.call("product_details", sku_or_url=sku)
                if not isinstance(card, dict):
                    raise GatewayError("Invalid product response")
                price = choose_card_price(card, self.price_mode)
                if price is None:
                    raise ValueError("Цена товара сейчас недоступна.")
                self.store.observe(source_type="manual", source_ref="manual", sku=sku,
                    title=card.get("title") or sku, url=product_url(sku), price=price)
                return f"Добавлен: {card.get('title') or sku}\nSKU: {sku}\nЦена: {price:,} ₽".replace(",", " ")
