"""Unit checks for the demo itself; these are not model-evaluation results."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
DEMO = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("demo_run_eval", DEMO / "run_eval.py")
assert spec is not None and spec.loader is not None
eval_kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_kit)


class ScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.questions = eval_kit.load_questions(DEMO / "questions.yaml")
        cls.by_id = {q["id"]: q for q in cls.questions}

    def test_exactly_ten_questions_and_ten_pages(self):
        self.assertEqual(len(self.questions), 10)
        self.assertEqual(len(list(eval_kit.VAULT.glob("*.md"))), 10)

    def test_all_gold_answers_contain_their_expected_facts(self):
        for q in self.questions:
            with self.subTest(id=q["id"]):
                self.assertTrue(eval_kit.score_answer(q["expected_answer"], q)["contains_expected_fact"])

    def test_blank_and_error_are_not_a_correct_answer(self):
        q = self.by_id["controller-ip"]
        self.assertFalse(eval_kit.score_answer("", q)["contains_expected_fact"])
        self.assertTrue(eval_kit.score_answer("I don't have access to that system.", q)["says_no_access"])
        self.assertTrue(eval_kit.score_answer("I don’t know.", q)["says_no_access"])

    def test_stale_ip_is_flagged(self):
        score = eval_kit.score_answer("The current server IP is 192.0.2.18.", self.by_id["controller-ip"])
        self.assertTrue(score["cites_stale_fact"])
        self.assertFalse(score["contains_expected_fact"])

    def test_historical_comparison_is_not_a_stale_assertion(self):
        answer = "Current: 203.0.113.42 (INFRA.md). The old IP 192.0.2.18 is historical."
        score = eval_kit.score_answer(answer, self.by_id["controller-ip"])
        self.assertTrue(score["contains_expected_fact"])
        self.assertTrue(score["mentions_stale_fact"])
        self.assertFalse(score["cites_stale_fact"])
        self.assertTrue(score["cites_proof_page"])

    def test_multiple_fact_groups_are_required(self):
        q = self.by_id["billing-vendor"]
        self.assertFalse(eval_kit.score_answer("CedarPay", q)["contains_expected_fact"])
        self.assertTrue(eval_kit.score_answer("CedarPay; Eli Stone. BILLING.md", q)["contains_expected_fact"])

    def test_literal_matching_rejects_longer_ip_and_number(self):
        self.assertFalse(eval_kit.score_answer("203.0.113.420", self.by_id["controller-ip"])["contains_expected_fact"])
        self.assertFalse(eval_kit.contains_literal("135 days", "35"))

    def test_historical_question_is_not_penalized_for_the_requested_history(self):
        q = self.by_id["historical-ip"]
        score = eval_kit.score_answer(q["expected_answer"], q)
        self.assertTrue(score["contains_expected_fact"])
        self.assertFalse(score["cites_stale_fact"])

    def test_unapproved_prototype_cannot_borrow_pilot_date(self):
        q = self.by_id["gripper-uncertainty"]
        score = eval_kit.score_answer("It will launch 2026-11-18.", q)
        self.assertFalse(score["contains_expected_fact"])
        self.assertTrue(score["cites_stale_fact"])

    def test_titles_fixture_has_titles_only(self):
        titles = json.loads((eval_kit.VAULT / "secret-titles.json").read_text())
        self.assertEqual(len(titles), 5)
        self.assertTrue(all(set(item) == {"title"} and isinstance(item["title"], str) for item in titles))

    def test_summary_separates_errors_from_scored_answers(self):
        q = self.by_id["controller-ip"]
        rows = [
            {"mode": "enabled", "valid": True, "score": eval_kit.score_answer("203.0.113.42", q)},
            {"mode": "enabled", "valid": False, "score": None},
        ]
        summary = eval_kit.summarize(rows)
        self.assertEqual(summary["enabled"]["attempted"], 2)
        self.assertEqual(summary["enabled"]["scored"], 1)
        self.assertEqual(summary["enabled"]["errors"], 1)
        self.assertEqual(summary["enabled"]["contains_expected_fact"], 1)
        self.assertEqual(summary["disabled"]["attempted"], 0)

    def test_local_detection_has_no_substring_loophole(self):
        self.assertTrue(eval_kit.local_endpoint("http://127.0.0.1:11434/v1"))
        self.assertTrue(eval_kit.local_endpoint("http://[::1]:8000/v1"))
        self.assertFalse(eval_kit.local_endpoint("http://localhost.attacker.example/v1"))

    def test_embedding_models_are_not_selected_for_chat(self):
        self.assertFalse(eval_kit.looks_like_chat_model("qwen3-embedding:0.6b"))
        self.assertFalse(eval_kit.looks_like_chat_model("demo-reranker:small"))
        self.assertTrue(eval_kit.looks_like_chat_model("demo-chat:small"))


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = eval_kit.new_run()
        self.home = self.run_dir / "isolation-unit-test"

    def tearDown(self):
        # Preserve test artifacts under the gitignored demo home for inspection.
        pass

    def test_environment_is_an_allowlist_and_never_forwards_existing_profiles(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/not-the-demo", "HERMES_PROFILE": "existing",
                                     "OPENAI_API_KEY": "unit-test-sentinel-not-a-key",
                                     "OP_SERVICE_ACCOUNT_TOKEN": "another-unit-test-sentinel"}):
            env = eval_kit.clean_environment(self.home, eval_kit.REPO)
        self.assertEqual(env["HERMES_HOME"], str(self.home))
        self.assertNotIn("HERMES_PROFILE", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", env)
        self.assertEqual(Path(env["HOME"]).parent, self.home)

    def test_only_explicit_credential_is_forwarded(self):
        with patch.dict(os.environ, {"DEMO_TEST_KEY": "unit-test-sentinel-not-a-key",
                                     "ANTHROPIC_API_KEY": "unselected-unit-test-sentinel"}):
            env = eval_kit.clean_environment(self.home, eval_kit.REPO, "DEMO_TEST_KEY")
        self.assertEqual(env["OPENAI_API_KEY"], "unit-test-sentinel-not-a-key")
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_credential_variable_cannot_replace_the_home(self):
        with self.assertRaisesRegex(ValueError, "isolation settings"):
            eval_kit.clean_environment(self.home, eval_kit.REPO, "HERMES_HOME")

    def test_live_home_is_short_and_stays_under_eval_root(self):
        minimum = eval_kit.EVAL_ROOT / "h000000" / "gateway.sock"
        if os.name == "posix" and len(str(minimum).encode()) > 100:
            with self.assertRaisesRegex(RuntimeError, "path is too long"):
                eval_kit.new_live_home()
            return
        home = eval_kit.new_live_home()
        self.assertEqual(home.parent, eval_kit.EVAL_ROOT)
        self.assertLessEqual(len(str(home / "gateway.sock").encode()), 100)

    def test_disabled_config_is_explicit_and_contains_no_gold_answers(self):
        args = eval_kit.parse_args([])
        eval_kit.clean_environment(self.home, args.hermes_repo)
        eval_kit.configure_home(self.home, False, "test-model", args)
        config_text = (self.home / "config.yaml").read_text()
        config = json.loads(config_text)
        self.assertFalse(config["plugins"]["entries"]["grounded-recall"]["enabled"])
        self.assertNotIn("203.0.113.42", config_text)
        self.assertNotIn("expected_answer", config_text)

    def test_isolation_shim_refuses_an_existing_profile_location(self):
        env = {"HOME": str(self.home), "HERMES_HOME": str(DEMO),
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-B", str(DEMO / "isolated_entry.py"), "hook"],
                                env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must both be inside", result.stderr)

    def test_real_file_guard_blocks_external_mutation_without_creating_file(self):
        outside = self.run_dir / "must-not-be-created.txt"
        code = (
            "import importlib.util, pathlib; "
            f"s=importlib.util.spec_from_file_location('entry',{str(DEMO / 'isolated_entry.py')!r}); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            f"m.install_guard(pathlib.Path({str(self.home)!r})); "
            f"pathlib.Path({str(outside)!r}).write_text('must be blocked')"
        )
        env = eval_kit.clean_environment(self.home, eval_kit.REPO)
        result = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocked write outside", result.stderr)
        self.assertFalse(outside.exists())

    def test_missing_hook_fails_instead_of_synthesizing_evidence(self):
        with self.assertRaisesRegex(RuntimeError, "no evidence was fabricated"):
            eval_kit.invoke_hook("test", {}, "module_that_is_not_installed_for_demo:hook")

    def test_file_guard_blocks_external_dotenv_read(self):
        outside = self.run_dir / "not-a-real-credential.env"
        outside.write_text("FAKE_FIXTURE=not-a-key\n", encoding="utf-8")
        code = (
            "import importlib.util, pathlib; "
            f"s=importlib.util.spec_from_file_location('entry',{str(DEMO / 'isolated_entry.py')!r}); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            f"m.install_guard(pathlib.Path({str(self.home)!r})); "
            f"pathlib.Path({str(outside)!r}).read_text()"
        )
        env = eval_kit.clean_environment(self.home, eval_kit.REPO)
        result = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocked external config/credential read", result.stderr)

    def test_process_timeout_is_bounded(self):
        env = eval_kit.clean_environment(self.home, eval_kit.REPO)
        result = eval_kit.run_process([sys.executable, "-B", "-c", "import time; time.sleep(10)"],
                                      env, self.home / "work", 0.1)
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
