"""Resume routing: settings.resume_for picks the first variant whose title_pattern matches, else the default."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import settings  # noqa: E402

RESUMES = {
    "default": "resumes/resume.pdf",
    "variants": [
        {"file": "resumes/po.pdf", "title_pattern": "product owner"},
        {"file": "resumes/pm.pdf", "title_pattern": "product manag|product lead"},
        {"file": "resumes/ba.pdf", "title_pattern": "analyst|systems"},
    ],
}


class ResumeSelection(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "resumes").mkdir()
        for name in ("resume", "po", "pm", "ba"):
            (self.root / "resumes" / f"{name}.pdf").write_bytes(b"%PDF")
        self.patches = [mock.patch.object(settings, "ROOT", self.root),
                        mock.patch.dict(settings.SETTINGS, {"resumes": RESUMES})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_first_matching_variant_wins(self):
        cases = {
            "Product Manager": "pm",
            "Technical Product Manager, Data": "pm",
            "Product Owner": "po",
            "Technical Product Owner, Data": "po",  # product owner is listed first
            "Business Systems Analyst 2": "ba",
            "Customer Success Manager": "resume",  # nothing matches: default
        }
        for title, stem in cases.items():
            with self.subTest(title=title):
                self.assertEqual(Path(settings.resume_for(title)).stem, stem)

    def test_missing_variant_file_falls_back_to_default(self):
        (self.root / "resumes" / "po.pdf").unlink()
        self.assertEqual(Path(settings.resume_for("Product Owner")), self.root / "resumes" / "resume.pdf")


if __name__ == "__main__":
    unittest.main()
