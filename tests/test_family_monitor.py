"""Two buyers with the same SKU must keep different order baselines and alerts."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from app.monitor import Monitor
from app.storage import Store


class MonitorFamilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_order_purchase_prices_and_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(str(Path(tmp)/"db.sqlite"),owner_id=101)
            key1=store.make_invite(101)
            key2=store.make_invite(101)
            self.assertTrue(store.accept_invite(key1,202,"Buyer A"))
            self.assertTrue(store.accept_invite(key2,303,"Buyer B"))
            prices={202:1500,303:3500}
            paid={202:1800,303:3700}
            notice=[]

            class FakeAPI:
                def __init__(self,uid):self.uid=uid
                async def call(self,tool,**args):
                    if tool=="session_status":return {"signed_in":True}
                    if tool=="get_cart":return {"items":[],"total_items":0}
                    if tool=="list_orders":
                        if args.get("scope")=="completed":return []
                        return [{"order_number":f"{self.uid}12345-0001"}]
                    if tool=="order_products":
                        return [{"sku":"12345678","title":"Same product", "shipment_id":"parcel-1",
                                 "price":f"{paid[self.uid]} ₽", "received":None}]
                    if tool=="product_details":
                        return {"title":"Same product", "sku":"12345678",
                                "price":f"{prices[self.uid]} ₽", "available":True}
                    raise AssertionError(tool)

            @asynccontextmanager
            async def stub(url,token,uid):
                self.assertEqual(token,"secret")
                yield FakeAPI(uid)

            async def send(n):notice.append(n)
            with patch("app.monitor.connect_gateway",stub):
                a=Monitor(store.for_user(202),"internal","secret","bank",send)
                b=Monitor(store.for_user(303),"internal","secret","bank",send)
                await a.sync()
                await b.sync()
                self.assertEqual({n.user_id for n in notice},{202,303})
                self.assertEqual(sorted((n.user_id,n.base_price,n.current_price) for n in notice),
                                 [(202,1800,1500),(303,3700,3500)])
                self.assertEqual(store.for_user(202).list_items()[0]["base_price"],1800)
                self.assertEqual(store.for_user(303).list_items()[0]["base_price"],3700)
                notice.clear()
                await a.sync()
                await b.sync()
                self.assertEqual(notice,[],"same quote must not notify again")
                prices[202]=1300
                await a.sync()
                await b.sync()
                self.assertEqual([n.user_id for n in notice],[202])


if __name__=="__main__":unittest.main()
