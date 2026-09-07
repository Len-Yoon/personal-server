import importlib
import sys
import unittest
from unittest.mock import Mock

from tests._test_support import prepare_service_import


class InvestingNewsSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._baseline_path = list(sys.path)
        cls._baseline_modules = {
            name: module for name, module in sys.modules.items()
            if name == "app" or name.startswith("app.")
        }

    def tearDown(self):
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(self._baseline_modules)
        sys.path[:] = self._baseline_path

    def reload_module(self):
        prepare_service_import("crawler-worker")
        sys.modules.pop("app.services.news_scheduler", None)
        import app.services.news_scheduler as module

        return importlib.reload(module)

    def test_run_once_forces_investing_news_collection(self):
        module = self.reload_module()
        collect_news = Mock()
        scheduler = module.InvestingNewsScheduler(
            interval_seconds=900,
            collect_news=collect_news,
        )

        scheduler.run_once()

        collect_news.assert_called_once_with(
            category="KR_WORLD",
            limit=24,
            force_refresh=True,
        )


if __name__ == "__main__":
    unittest.main()
