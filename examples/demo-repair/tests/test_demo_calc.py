import unittest

from src.demo_calc import add


class DemoCalcTests(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)
