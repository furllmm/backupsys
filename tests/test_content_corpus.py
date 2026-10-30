import tempfile
import unittest
from pathlib import Path

from tools.content_corpus import generate_corpus


class ContentCorpusTests(unittest.TestCase):
    def test_generates_mixed_deterministic_corpus_and_exact_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            paths = generate_corpus(root, seed=123)

            self.assertEqual(len(paths), 9)
            self.assertTrue((root / "records.json").is_file())
            self.assertTrue((root / "records.jsonl").is_file())
            self.assertTrue((root / "events.log").is_file())
            self.assertTrue((root / "random.bin").is_file())
            self.assertEqual(
                (root / "repeated.txt").read_bytes(),
                (root / "repeated-copy.txt").read_bytes(),
            )

    def test_refuses_to_overwrite_non_empty_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "existing"
            root.mkdir()
            marker = root / "keep.txt"
            marker.write_text("keep me", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "absent or empty"):
                generate_corpus(root)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
