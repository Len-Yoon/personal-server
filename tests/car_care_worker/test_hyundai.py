import os
import unittest
from unittest.mock import patch

from app.services.hyundai import HyundaiClient


class HyundaiClientTests(unittest.TestCase):
    def test_hyundai_client_returns_none_without_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("app.services.hyundai.urlopen") as mocked_urlopen:
            snapshot = HyundaiClient.from_environment().fetch_snapshot()

        self.assertIsNone(snapshot)
        mocked_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
