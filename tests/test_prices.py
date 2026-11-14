import unittest
from app.prices import rubles, should_alert, choose_card_price


class PriceTests(unittest.TestCase):
    def test_ru_price(self):
        self.assertEqual(rubles("3 690 ₽"), 3690)
        self.assertEqual(rubles("3\u202f690,00 ₽"), 3690)
        self.assertEqual(rubles("23.000 ₽"), 23000)
        self.assertEqual(rubles("1250"), 1250)
        self.assertIsNone(rubles("Нет в наличии"))
        self.assertIsNone(rubles("0 ₽"))

    def test_baseline_and_repeated_alerts(self):
        self.assertFalse(should_alert(1000, 901, None, 100))
        self.assertTrue(should_alert(1000, 900, None, 100))
        self.assertFalse(should_alert(1000, 900, 900, 100))
        self.assertFalse(should_alert(1000, 801, 900, 100))
        self.assertTrue(should_alert(1000, 800, 900, 100))
        self.assertFalse(should_alert(1000, None, None, 100))

    def test_card_modes(self):
        card = {"price":"1 200 ₽", "price_regular":"1 500 ₽", "price_old":"2 100 ₽", "available":True}
        self.assertEqual(choose_card_price(card, "bank"), 1200)
        self.assertEqual(choose_card_price(card, "regular"), 1500)
        self.assertIsNone(choose_card_price({**card,"available":False},"bank"))

if __name__ == "__main__":
    unittest.main()
