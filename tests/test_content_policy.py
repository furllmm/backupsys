import tempfile
import unittest
from pathlib import Path

from core.content_policy import analyze_paths, decide_analysis


class ContentPolicyTests(unittest.TestCase):
    def test_empty_file_skips_deep_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty"
            path.write_bytes(b"")
            decision = decide_analysis(path)
            self.assertEqual(decision.analysis_level, "skip")

    def test_text_is_candidate_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.unknown"
            original = b'{"message":"' + b"repeat " * 200 + b'"}'
            path.write_bytes(original)
            decision = decide_analysis(path)
            self.assertEqual(decision.analysis_level, "candidate")
            self.assertEqual(path.read_bytes(), original)

    def test_compressed_signature_stays_shallow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.anything"
            path.write_bytes(b"PK\x03\x04" + b"archive-data" * 20)
            self.assertEqual(decide_analysis(path).analysis_level, "shallow")

    def test_multiple_paths_preserve_order(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "a"
            second = Path(directory) / "b"
            first.write_bytes(b"")
            second.write_bytes(b"")
            decisions = analyze_paths([first, second])
            self.assertEqual([d.path for d in decisions], [str(first), str(second)])

    def test_missing_file_error_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(FileNotFoundError):
                decide_analysis(missing)


if __name__ == "__main__":
    unittest.main()
