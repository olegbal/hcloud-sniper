import unittest
from sniper import pick_location


class PickLocationTest(unittest.TestCase):
    def test_first_preferred_wins(self):
        self.assertEqual(pick_location(["hel1", "nbg1"], ["fsn1", "nbg1", "hel1"]), "nbg1")

    def test_none_when_unavailable(self):
        self.assertIsNone(pick_location(["ash"], ["fsn1", "nbg1"]))

    def test_empty_available(self):
        self.assertIsNone(pick_location([], ["fsn1"]))


if __name__ == "__main__":
    unittest.main()
