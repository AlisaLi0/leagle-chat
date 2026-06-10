import os
import sys
import tempfile
import unittest


class BriefReviewExtractionTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(prefix="leagle-brief-", suffix=".db")
        os.close(fd)
        self.db_path = path
        os.environ["LEAGLE_DB_PATH"] = self.db_path

    def tearDown(self):
        for p in [self.db_path, self.db_path + "-wal", self.db_path + "-shm"]:
            try:
                os.remove(p)
            except OSError:
                pass

    def test_extracts_reporter_cites_case_names_and_nearby_quotes(self):
        for mod in [m for m in list(sys.modules) if m.startswith("server.")]:
            sys.modules.pop(mod, None)
        pkg = sys.modules.get("server")
        if pkg:
            for attr in ["app", "db", "billing", "auth", "courtlistener", "statutes", "llm"]:
                if hasattr(pkg, attr):
                    delattr(pkg, attr)
        from server.app import _extract_legal_references

        text = (
            'The brief relies on Miranda v. Arizona and quotes "the prosecution may not use statements" '
            'before citing 384 U.S. 436. It also mentions Roe v. Wade, 410 U.S. 113.'
        )
        refs = _extract_legal_references(text)
        values = {r["text"] for r in refs}
        self.assertIn("384 U.S. 436", values)
        self.assertIn("410 U.S. 113", values)
        self.assertTrue(any(r["kind"] == "case" and "Miranda v. Arizona" in r["text"] for r in refs))
        self.assertTrue(any(r.get("quote") for r in refs if r["text"] == "384 U.S. 436"))


if __name__ == "__main__":
    unittest.main()