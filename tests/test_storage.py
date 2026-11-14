import tempfile
import unittest
from pathlib import Path
from app.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "test.sqlite3"))
        self.args = dict(source_type="cart", source_ref="cart", sku="12345678", title="Test", url="https://www.ozon.ru/product/12345678/")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_price_is_baseline(self):
        self.assertIsNone(self.store.observe(**self.args, price=1000))
        self.assertIsNone(self.store.observe(**self.args, price=901))
        notice = self.store.observe(**self.args, price=900)
        self.assertIsNotNone(notice)
        self.assertEqual(notice.base_price, 1000)
        self.store.mark_alert_sent(notice)
        self.assertIsNone(self.store.observe(**self.args, price=850))
        notice2 = self.store.observe(**self.args, price=800)
        self.assertIsNotNone(notice2)
        self.assertEqual(notice2.current_price, 800)

    def test_order_purchase_price_is_baseline(self):
        notice = self.store.observe(source_type="order", source_ref="12345678-0101",sku="12345678",title="Test",url="",base_price=1800,price=1600)
        self.assertEqual(notice.base_price,1800)
        self.store.mark_alert_sent(notice)
        self.assertIsNone(self.store.observe(source_type="order",source_ref="12345678-0101",sku="12345678",title="Test",url="",base_price=1800,price=1550))

    def test_preset_override_muting_and_reactivation(self):
        self.store.observe(**self.args,price=1000)
        self.assertEqual(self.store.set_threshold("12345678", 250),1)
        self.assertIsNone(self.store.observe(**self.args,price=800))
        self.assertEqual(self.store.set_muted("12345678", True),1)
        self.assertIsNone(self.store.observe(**self.args,price=600))
        self.assertEqual(self.store.set_muted("12345678", False),1)
        self.assertIsNotNone(self.store.observe(**self.args,price=600))
        self.assertEqual(self.store.retire_missing_cart(set()),1)
        self.assertIsNone(self.store.observe(**self.args,price=700))
        self.assertEqual(self.store.list_items()[0]["base_price"],700)

    def test_parcel_order_closure(self):
        self.store.observe(source_type="order",source_ref="1111-2222|parcel",sku="12345678",title="Test",url="",base_price=2000,price=1900)
        self.assertIn("1111-2222",self.store.seen_order_references())
        self.store.close_order("1111-2222|parcel","12345678","received")
        self.assertEqual(self.store.list_items()[0]["state"],"received")
        self.assertFalse(self.store.list_items()[0]["active"])

    def test_null_current_price_does_not_erase(self):
        self.store.observe(**self.args,price=900)
        self.store.observe(**self.args,price=None)
        self.assertEqual(self.store.list_items()[0]["last_price"],900)

    def test_manual_forget(self):
        self.store.observe(source_type="manual",source_ref="manual",sku="12345678",title="T",url="",price=1000)
        self.assertEqual(self.store.forget_manual("12345678"),1)
        self.assertEqual(self.store.list_items(),[])

if __name__ == "__main__":
    unittest.main()
