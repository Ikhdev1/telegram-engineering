from __future__ import annotations

import asyncio
import unittest

from support_bot.offline import run_offline_smoke


class OfflineSmokeTest(unittest.TestCase):
    def test_offline_dispatcher_scenario(self) -> None:
        report = asyncio.run(run_offline_smoke())
        self.assertTrue(report.ok, report.as_dict())


if __name__ == "__main__":
    unittest.main()
