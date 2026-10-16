import tempfile
import unittest
from pathlib import Path

from core.content_analyzer import analyze_bytes, analyze_file


class ContentAnalyzerTests(unittest.TestCase):
    def test_magic_detection_ignores_extension(self):
        profile = analyze_bytes(b"PK\x03\x04" + b"payload" * 20)
        self.assertEqual(profile.format_hint, "zip")
        self.assertEqual(profile.kind, "known-binary")
        self.assertEqual(profile.recommendation, "avoid-expensive-pattern-analysis")

    def test_json_content(self):
        profile = analyze_bytes(b'{"items":[1,2,3],"name":"' + b"x" * 1100 + b'"}')
        self.assertEqual(profile.kind, "text")
        self.assertEqual(profile.format_hint, "json")
        self.assertEqual(profile.recommendation, "text-pattern-analysis-candidate")

    def test_jsonl_content(self):
        self.assertEqual(analyze_bytes(b'{"n":1}\n{"n":2}\n{"n":3}\n').format_hint, "jsonl")

    def test_unknown_binary(self):
        profile = analyze_bytes(bytes([0, 255, 1, 254, 2, 253, 3, 252]) * 100)
        self.assertEqual(profile.format_hint, "unknown-binary")
        self.assertFalse(profile.text_like)

    def test_empty_file(self):
        profile = analyze_bytes(b"")
        self.assertEqual(profile.kind, "empty")
        self.assertEqual(profile.recommendation, "no-content")

    def test_invalid_arguments(self):
        with self.assertRaises(TypeError):
            analyze_bytes(bytearray(b"x"))
        with self.assertRaises(ValueError):
            analyze_bytes(b"long", file_size=2)

    def test_bounded_file_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "misleading.bin"
            path.write_bytes(b'{"hello":"world"}\n' + b" " * 1000)
            profile = analyze_file(path, sample_size=32)
            self.assertEqual(profile.sample_bytes, 32)
            self.assertEqual(profile.file_size, path.stat().st_size)
            self.assertEqual(profile.format_hint, "json")

    def test_sample_size_must_be_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x"
            path.write_bytes(b"x")
            with self.assertRaises(ValueError):
                analyze_file(path, sample_size=0)


if __name__ == "__main__":
    unittest.main()
