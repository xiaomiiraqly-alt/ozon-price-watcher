"""Regression coverage for incomplete Ozon responses and account cleanup."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.monitor import Monitor, product_sku
from app.storage import Store


class ProductLinkTests(unittest.TestCase):
    def test_extracts_final_sku_from_path_not_title_or_query(self):
        for link in (
            "12345678",
            "https://www.ozon.ru/product/nabor-202600-12345678/?tracking=99999999",
            "https://ozon.ru/product/12345678/",
        ):
            self.assertEqual(product_sku(link),"12345678")

    def test_rejects_other_sites_tracking_numbers_and_short_links(self):
        for link in (
            "https://example.com/product/12345678/",
            "https://www.ozon.ru/?tracking=12345678",
            "https://ozon.ru.evil.example/product/12345678/",
            "https://ozon.ru/product/no-sku/?id=12345678",
            "https://ozon.ru/t/12345678",
            "Text 12345678",
        ):
            with self.subTest(link=link),self.assertRaises(ValueError):product_sku(link)


class MonitorRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(str(Path(self.tmp.name)/"watcher.db"),owner_id=111)
        self.notifications=[]
        self.calls=[]
        self.cart={"items":[],"total_items":0}
        self.orders=[]
        self.positions=[]
        owner=self
        class API:
            async def call(self,tool,**args):
                owner.calls.append((tool,args))
                return {
                    "session_status":{"signed_in":True},"get_cart":owner.cart,
                    "list_orders":owner.orders,"order_products":owner.positions,
                    "product_details":{"price":"500 ₽","available":True},
                }[tool]
            async def drop_account(self):return True
        self.api=API()
        @asynccontextmanager
        async def connect(*args):yield self.api
        self.patcher=patch("app.monitor.connect_gateway",connect)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        async def notify(notice):self.notifications.append(notice)
        self.monitor=Monitor(self.store,"internal","token","bank",notify)
        self.item=dict(source_type="cart",source_ref="cart",sku="12345678",title="Test",url="",price=1000)

    async def test_uncertain_cart_never_retires_previous_items(self):
        self.store.observe(**self.item)
        for self.cart in ({"items":[],"total_items":None},{"items":[{}],"total_items":1},{"items":[None],"total_items":1}):
            report=await self.monitor.sync()
            self.assertTrue(self.store.list_items("cart")[0]["active"])
            self.assertTrue(report.errors)
        self.cart={"items":[],"total_items":0}
        await self.monitor.sync()
        self.assertFalse(self.store.list_items("cart")[0]["active"])

    async def test_repeated_delivery_groups_fetch_order_only_once(self):
        self.orders=[{"order_number":"111111-0001"},{"order_numbers":["111111-0001"]}]
        await self.monitor.sync()
        self.assertEqual([tool for tool,args in self.calls].count("order_products"),1)

    async def test_missing_order_is_reconciled_directly(self):
        self.store.observe(source_type="order",source_ref="111111-0001|parcel",sku="12345678",title="Test",url="",base_price=1000,price=1000)
        self.positions=[{"sku":"12345678","shipment_id":"parcel","received":True}]
        await self.monitor.sync()
        row=self.store.list_items("order")[0]
        self.assertEqual(row["state"],"received")
        self.assertFalse(row["active"])
        self.assertIn(("order_products",{"order":"111111-0001"}),self.calls)
        self.assertNotIn(("list_orders",{"scope":"completed","limit":100}),self.calls)

    async def test_disconnect_waits_for_sync_and_prevents_data_recreation(self):
        entered=asyncio.Event()
        release=asyncio.Event()
        original=self.api.call
        async def blocked(tool,**args):
            if tool=="get_cart":
                entered.set()
                await release.wait()
            return await original(tool,**args)
        self.api.call=blocked
        ongoing=asyncio.create_task(self.monitor.sync())
        await entered.wait()
        deleting=asyncio.create_task(self.monitor.disconnect())
        await asyncio.sleep(0)
        self.assertFalse(deleting.done())
        release.set()
        await ongoing
        await deleting
        self.assertEqual(self.store.list_items(),[])
        self.assertEqual(self.store.get_setting("last_sync"),"")
        self.calls.clear()
        await self.monitor.sync()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.get_setting("last_sync"),"")

    async def test_revoked_user_is_not_synced(self):
        token=self.store.make_invite(111)
        self.store.accept_invite(token,222,"Family")
        self.monitor.store=self.store.for_user(222)
        self.store.revoke(111,222)
        await self.monitor.sync()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.monitor.store.get_setting("last_sync"),"")

    async def test_muted_manual_item_still_records_prices(self):
        self.store.observe(source_type="manual",source_ref="manual",sku="12345678",title="Test",url="",price=1000)
        self.store.set_muted("12345678",True)
        report=await self.monitor.sync()
        self.assertEqual(report.manual,1)
        self.assertEqual(self.store.list_items("manual")[0]["last_price"],500)
        self.assertEqual(self.notifications,[])

    async def test_late_parcel_id_closes_original_order_record(self):
        self.store.observe(source_type="order",source_ref="111111-0001",sku="12345678",title="Test",url="",price=1000,base_price=1000)
        original=self.store.list_items()[0]["id"]
        self.positions=[{"sku":"12345678","shipment_id":"new-parcel","received":True}]
        await self.monitor.sync()
        rows=self.store.list_items("order")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["id"],original)
        self.assertEqual(rows[0]["state"],"received")
        self.assertEqual(rows[0]["source_ref"],"111111-0001|new-parcel")


if __name__=="__main__":unittest.main()
