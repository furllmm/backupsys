import tempfile
import unittest
from pathlib import Path

from tools.content_report import build_report


class ContentReportTests(unittest.TestCase):
    def test_report_has_summary_and_limitations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.bin"
            original = b'{"value":"' + b"repeat-" * 300 + b'"}'
            path.write_bytes(original)

            report = build_report([path], sample_size=128)

            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["summary"]["file_count"], 1)
            self.assertEqual(report["summary"]["total_file_bytes"], len(original))
            self.assertEqual(report["summary"]["total_sampled_bytes"], 128)
            self.assertEqual(report["files"][0]["profile"]["sample_bytes"], 128)
            self.assertTrue(report["limitations"])
            self.assertEqual(path.read_bytes(), original)

    def test_empty_input_report_is_well_formed(self):
        report = build_report([])
        self.assertEqual(report["summary"]["file_count"], 0)
        self.assertEqual(report["files"], [])
        self.assertEqual(report["summary"]["analysis_levels"], {})


if __name__ == "__main__":
    unittest.main()
