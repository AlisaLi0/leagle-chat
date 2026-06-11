import os
import sys
import tempfile
import unittest


class ReviewAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(prefix="leagle-review-", suffix=".db")
        os.close(fd)
        self.db_path = path
        os.environ["LEAGLE_DB_PATH"] = self.db_path
        for mod in [m for m in list(sys.modules) if m.startswith("server.")]:
            sys.modules.pop(mod, None)
        pkg = sys.modules.get("server")
        if pkg:
            for attr in ["app", "db", "billing", "auth", "courtlistener", "statutes", "llm"]:
                if hasattr(pkg, attr):
                    delattr(pkg, attr)

    def tearDown(self):
        for p in [self.db_path, self.db_path + "-wal", self.db_path + "-shm"]:
            try:
                os.remove(p)
            except OSError:
                pass

    async def test_support_check_short_circuits_quote_not_found(self):
        from server.app import _brief_support_check

        out = await _brief_support_check(
            {"text": "384 U.S. 436", "proposition": "warnings are required", "quote": "not there"},
            object(),
            {"found": False, "match": "not_found"},
            [{"text": "Miranda warnings context"}],
        )
        self.assertEqual(out["status"], "Quote not found")
        self.assertEqual(out["quote_accuracy"], "Not found")

    def test_language_normalization_and_instruction(self):
        from server.app import _language_instruction, _normalize_language

        self.assertEqual(_normalize_language("es-MX"), "es")
        self.assertEqual(_normalize_language("zh_CN"), "zh")
        self.assertEqual(_normalize_language("zh-Hant"), "zh-TW")
        self.assertEqual(_normalize_language("fr-CA"), "fr")
        self.assertEqual(_normalize_language("pt-BR"), "pt")
        self.assertEqual(_normalize_language("ko-KR"), "ko")
        self.assertEqual(_normalize_language("ja-JP"), "ja")
        self.assertEqual(_normalize_language("vi-VN"), "vi")
        self.assertEqual(_normalize_language("de"), "en")
        instruction = _language_instruction("zh-TW")
        self.assertIn("Traditional Chinese", instruction)
        self.assertIn("Keep US case names", instruction)
        self.assertIn("original English", instruction)

    async def test_support_check_reason_uses_selected_language(self):
        from server.app import _brief_support_check

        out = await _brief_support_check(
            {"text": "384 U.S. 436", "proposition": "warnings are required"},
            object(),
            None,
            [],
            "zh",
        )
        self.assertEqual(out["status"], "Needs review")
        self.assertIn("来源段落", out["reason"])

    async def test_case_analysis_uses_llm_json(self):
        import server.app as app

        async def fake_complete_json(messages, *, max_tokens=400):
            return {
                "summary": "A grounded summary.",
                "why_it_matters": "It frames the rule.",
                "key_points": ["Point one"],
                "limits": ["Limited passage set"],
            }

        old = app.llm.complete_json
        app.llm.complete_json = fake_complete_json
        try:
            out = await app._case_analysis({
                "title": "Example v. State",
                "focused_passages": [{"text": "This is a real source passage with enough legal context."}],
                "source_availability": {"has_text": True},
            })
        finally:
            app.llm.complete_json = old
        self.assertEqual(out["summary"], "A grounded summary.")
        self.assertEqual(out["key_points"], ["Point one"])


if __name__ == "__main__":
    unittest.main()
