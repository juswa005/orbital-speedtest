import unittest

from orbital_speedtest.app import SpeedResult, classify_result, parse_speedtest_json, plain_summary


SAMPLE_JSON = """{
  "download": 19093324.672992077,
  "upload": 9661093.745431364,
  "ping": 15.298,
  "server": {
    "name": "Makati",
    "sponsor": "PLDT",
    "d": 6.306959963312499
  },
  "timestamp": "2026-04-16T05:43:12.029627Z",
  "client": {
    "ip": "139.135.174.7",
    "isp": "Converge"
  }
}"""


class ParseSpeedtestJsonTest(unittest.TestCase):
    def test_parses_speedtest_json(self) -> None:
        result = parse_speedtest_json(SAMPLE_JSON)

        self.assertAlmostEqual(result.download_mbps, 19.093324672992077)
        self.assertAlmostEqual(result.upload_mbps, 9.661093745431364)
        self.assertEqual(result.server_name, "Makati")
        self.assertEqual(result.sponsor, "PLDT")
        self.assertEqual(result.isp, "Converge")
        self.assertEqual(result.external_ip, "139.135.174.7")


class ClassificationTest(unittest.TestCase):
    def test_classifies_mission_grade(self) -> None:
        result = SpeedResult(
            download_bps=220_000_000,
            upload_bps=80_000_000,
            ping_ms=11.0,
            server_name="Makati",
            sponsor="PLDT",
            distance_km=6.0,
            isp="Converge",
            external_ip="1.1.1.1",
            timestamp="2026-04-16T05:43:12.029627Z",
        )

        assessment = classify_result(result)
        self.assertEqual(assessment.label, "Mission-grade")

    def test_classifies_slow(self) -> None:
        result = SpeedResult(
            download_bps=12_000_000,
            upload_bps=2_500_000,
            ping_ms=90.0,
            server_name="Makati",
            sponsor="PLDT",
            distance_km=6.0,
            isp="Converge",
            external_ip="1.1.1.1",
            timestamp="2026-04-16T05:43:12.029627Z",
        )

        assessment = classify_result(result)
        self.assertEqual(assessment.label, "Slow")


class PlainSummaryTest(unittest.TestCase):
    def test_plain_summary_contains_key_fields(self) -> None:
        result = parse_speedtest_json(SAMPLE_JSON)
        assessment = classify_result(result)
        summary = plain_summary(result, assessment)

        self.assertIn("Download:", summary)
        self.assertIn("Upload:", summary)
        self.assertIn("Ping:", summary)
        self.assertIn("Assessment:", summary)


if __name__ == "__main__":
    unittest.main()
