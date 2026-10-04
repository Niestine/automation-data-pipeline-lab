import unittest
from unittest.mock import patch

import helpers
from llm_agent_lab.orchestrator import AgentOrchestrator
from llm_agent_lab.provider import FakeProvider
from llm_agent_lab.telemetry import ManualClock, RecordingSleeper, WallClock


class TelemetryTests(unittest.TestCase):
    def test_wall_clock_sleep_actually_waits(self):
        with patch("llm_agent_lab.telemetry.time.sleep") as fake_sleep:
            WallClock().sleep(250)
        fake_sleep.assert_called_once_with(0.25)

    def test_default_orchestrator_backs_off_with_wall_clock(self):
        provider = FakeProvider({"T-1001": [{"text": "not json"}]})
        orch = AgentOrchestrator(provider)
        with patch("llm_agent_lab.telemetry.time.sleep") as fake_sleep:
            orch.run(helpers.make_ticket())
        self.assertEqual(fake_sleep.call_count, orch.policy.max_attempts - 1)

    def test_recording_sleeper_advances_manual_clock(self):
        clock = ManualClock(start_ms=0)
        sleeper = RecordingSleeper(clock)
        sleeper(15)
        sleeper(30)
        self.assertEqual(sleeper.delays, [15, 30])
        self.assertEqual(clock.now_ms(), 45)


if __name__ == "__main__":
    unittest.main()
