import unittest

import heocr


class VersionTest(unittest.TestCase):
    def test_version_is_a_string(self) -> None:
        self.assertIsInstance(heocr.__version__, str)


if __name__ == "__main__":
    unittest.main()
