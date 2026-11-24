"""Unit tests: the action's script against a local fake engine."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_tests  # noqa: E402
from fake_engine import CHAT_ONLY_KEY, GOOD_KEY, SESSION_ID, FakeEngine, case  # noqa: E402


class ActionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine: FakeEngine = None  # type: ignore[assignment]

    def start(self, **kwargs: Any) -> FakeEngine:
        self.engine = FakeEngine(**kwargs).start()
        self.addCleanup(self.engine.stop)
        return self.engine

    def run_action(self, **inputs: str) -> Tuple[int, str, str, Dict[str, str]]:
        """Run the script. Returns (exit code, log, summary, outputs)."""
        summary_path = os.path.join(self.tmp.name, "summary.md")
        output_path = os.path.join(self.tmp.name, "output.txt")
        for path in (summary_path, output_path):
            open(path, "w").close()
        env = {"GITHUB_STEP_SUMMARY": summary_path, "GITHUB_OUTPUT": output_path,
               "INPUT_ENGINE_URL": self.engine.url, "INPUT_API_KEY": GOOD_KEY,
               "INPUT_SUITE": "Support basics", "INPUT_POLL_INTERVAL_SECONDS": "0.01"}
        for key, value in inputs.items():
            env["INPUT_" + key.upper()] = value
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            code = run_tests.main(env)
        with open(summary_path, encoding="utf-8") as handle:
            summary = handle.read()
        outputs: Dict[str, str] = {}
        with open(output_path, encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                key, _, value = line.partition("=")
                outputs[key] = value
        return code, log.getvalue(), summary, outputs


class PassAndFail(ActionTestCase):
    def test_all_cases_pass(self) -> None:
        self.start(results=[case("greets", True), case("refunds", True)],
                   polls_before_done=2)
        code, log, summary, outputs = self.run_action()
        self.assertEqual(code, 0)
        self.assertEqual(outputs["run-id"], "run-42")
        self.assertEqual(outputs["run-url"], self.engine.url + "/admin/evals/runs/run-42")
        self.assertEqual(outputs["pass-rate"], "100")
        self.assertEqual((outputs["passed"], outputs["failed"]), ("2", "0"))
        self.assertEqual(outputs["status"], "passed")
        self.assertIn("**Passed.** 2 of 2 cases passed (100%)", summary)
        self.assertIn("| greets | ✅ pass |", summary)
        self.assertGreaterEqual(self.engine.polls, 3)
        trigger = self.engine.tool_calls("trigger_eval_run")[0]
        self.assertEqual(trigger["suite_id"], "suite-1")
        self.assertEqual(trigger["profile_id"], "prof-1")  # the suite's default agent
        self.assertNotIn("prompt_overrides", trigger)
        self.assertNotIn("model_id", trigger)
        self.assertEqual(self.engine.methods[:2], ["initialize", "notifications/initialized"])

    def test_fails_below_threshold(self) -> None:
        self.start(final_status="failed", results=[
            case("greets", True),
            case("refunds", False, missing_substrings=["14 days"]),
            case("books a call", False, missing_expected_tools=["book_slot"]),
            case("stays polite", False, judge={"score": 0.2, "rationale": "Rude | curt\nreply"}),
        ])
        code, log, summary, outputs = self.run_action()
        self.assertEqual(code, 1)
        self.assertEqual(outputs["pass-rate"], "25")
        self.assertEqual((outputs["passed"], outputs["failed"]), ("1", "3"))
        self.assertIn("**Failed.** 1 of 4 cases passed (25%). Required: 100%.", summary)
        self.assertIn("| refunds | ❌ fail | missing: 14 days |", summary)
        self.assertIn("| books a call | ❌ fail | did not call: book_slot |", summary)
        self.assertIn("| stays polite | ❌ fail | Rude \\| curt reply |", summary)
        self.assertIn("::error title=Agent tests failed::Pass rate 25%25 is below the required 100%25",
                      log)  # "%" is escaped in workflow commands
        # Failures are listed before passes.
        self.assertLess(summary.index("refunds"), summary.index("| greets"))

    def test_passes_when_rate_meets_a_lower_threshold(self) -> None:
        self.start(final_status="failed",
                   results=[case("a", True), case("b", True), case("c", True),
                            case("d", False, error="agent timed out")])
        code, _, summary, outputs = self.run_action(min_pass_rate="75")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["pass-rate"], "75")
        self.assertIn("error: agent timed out", summary)
        code, _, _, _ = self.run_action(min_pass_rate="76")
        self.assertEqual(code, 1)

    def test_ungraded_cases_are_left_out_of_the_rate(self) -> None:
        self.start(results=[case("a", True),
                            case("b", False, ungraded=True, grading_note="no judge model")])
        code, _, summary, outputs = self.run_action()
        self.assertEqual(code, 0)
        self.assertEqual(outputs["pass-rate"], "100")
        self.assertEqual(outputs["not-judged"], "1")
        self.assertIn("| b | ⚪ not judged | no judge model |", summary)

    def test_nothing_graded_fails(self) -> None:
        self.start(results=[case("a", False, ungraded=True)])
        code, log, _, outputs = self.run_action(min_pass_rate="0")
        self.assertEqual(code, 1)
        self.assertEqual(outputs["pass-rate"], "")
        self.assertIn("No case could be graded", log)

    def test_errored_run_fails_even_at_zero_threshold(self) -> None:
        self.start(final_status="errored", results=[], run_error="suite has no scenarios")
        code, log, summary, outputs = self.run_action(min_pass_rate="0")
        self.assertEqual(code, 1)
        self.assertEqual(outputs["status"], "errored")
        self.assertIn("suite has no scenarios", summary)
        self.assertIn("The run errored: suite has no scenarios", log)


class Timeout(ActionTestCase):
    def test_times_out_while_the_run_is_still_going(self) -> None:
        self.start(polls_before_done=10_000)
        code, log, summary, outputs = self.run_action(timeout_minutes="0.01")
        self.assertEqual(code, 3)
        self.assertEqual(outputs["run-id"], "run-42")
        self.assertEqual(outputs["status"], "timeout")
        self.assertIn("**Timed out.**", summary)
        self.assertIn("::error title=Agent tests timed out::", log)
        self.assertNotIn("pass-rate", outputs)


class Auth(ActionTestCase):
    def test_wrong_key_is_an_auth_error_and_is_never_printed(self) -> None:
        self.start()
        secret = "wae_wrong_SECRETVALUE"
        code, log, summary, outputs = self.run_action(api_key=secret)
        self.assertEqual(code, 2)
        self.assertIn("refused the API key (HTTP 401): invalid API key", log)
        self.assertIn("Could not run", summary)
        self.assertNotIn(secret, log + summary)
        self.assertNotIn("run-id", outputs)
        self.assertEqual(self.engine.calls, [])

    def test_chat_only_key_is_refused_with_403(self) -> None:
        self.start()
        code, log, _, _ = self.run_action(api_key=CHAT_ONLY_KEY)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 403", log)
        self.assertIn("Full access", log)
        self.assertNotIn(CHAT_ONLY_KEY, log)

    def test_good_key_is_sent_as_bearer_and_not_logged(self) -> None:
        self.start()
        code, log, summary, _ = self.run_action()
        self.assertEqual(code, 0)
        self.assertTrue(all(h == "Bearer " + GOOD_KEY for h in self.engine.auth_headers))
        self.assertNotIn(GOOD_KEY, log + summary)

    def test_unreachable_engine(self) -> None:
        self.start()
        code, log, _, _ = self.run_action(engine_url="http://127.0.0.1:9")
        self.assertEqual(code, 2)
        self.assertIn("Could not reach http://127.0.0.1:9/mcp", log)

    def test_wrong_path_is_explained(self) -> None:
        self.start()
        code, log, _, _ = self.run_action(engine_url=self.engine.url + "/nope")
        self.assertEqual(code, 2)
        self.assertIn("was not found (HTTP 404)", log)


class Sse(ActionTestCase):
    def test_event_stream_replies_and_session_header(self) -> None:
        self.start(sse=True, require_session=True, polls_before_done=2,
                   results=[case("greets", True), case("refunds", False, error="boom")])
        code, _, summary, outputs = self.run_action(min_pass_rate="50")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["pass-rate"], "50")
        self.assertIn("| refunds | ❌ fail | error: boom |", summary)
        # The id from `initialize` is sent back on every later request.
        self.assertIsNone(self.engine.session_headers[0])
        self.assertTrue(all(h == SESSION_ID for h in self.engine.session_headers[1:]))

    def test_parse_sse(self) -> None:
        body = (": comment\n\nevent: message\ndata: {\"id\": 1,\ndata:  \"result\": {}}\n\n"
                "data: not json\n\ndata: [{\"id\": 2}]\n")
        self.assertEqual(run_tests.parse_sse(body), [{"id": 1, "result": {}}, {"id": 2}])

    def test_engine_url_may_end_in_mcp_or_a_slash(self) -> None:
        self.start()
        for suffix in ("/", "/mcp", "/mcp/"):
            self.engine.polls = 0
            code, _, _, outputs = self.run_action(engine_url=self.engine.url + suffix)
            self.assertEqual(code, 0, suffix)
            self.assertEqual(outputs["run-url"], self.engine.url + "/admin/evals/runs/run-42")


class Resolving(ActionTestCase):
    def test_suite_by_id_and_agent_by_slug(self) -> None:
        self.start()
        code, _, _, _ = self.run_action(suite="suite-2", agent="SALES")
        self.assertEqual(code, 0)
        trigger = self.engine.tool_calls("trigger_eval_run")[0]
        self.assertEqual((trigger["suite_id"], trigger["profile_id"]), ("suite-2", "prof-2"))

    def test_unknown_suite_lists_what_exists(self) -> None:
        self.start()
        code, log, _, _ = self.run_action(suite="Nope")
        self.assertEqual(code, 2)
        self.assertIn("No test suite matches 'Nope'. Found: No default, Support basics.", log)
        self.assertEqual(self.engine.tool_calls("trigger_eval_run"), [])

    def test_suite_without_default_agent_needs_the_agent_input(self) -> None:
        self.start()
        code, log, _, _ = self.run_action(suite="No default")
        self.assertEqual(code, 2)
        self.assertIn("has no default agent", log)

    def test_model_by_name(self) -> None:
        self.start()
        code, _, summary, _ = self.run_action(model="fast model")
        self.assertEqual(code, 0)
        trigger = self.engine.tool_calls("trigger_eval_run")[0]
        self.assertEqual(trigger["model_id"], "model-9")
        self.assertEqual(trigger["variant_label"], "CI: Fast model")
        self.assertIn("Model: Fast model", summary)

    def test_engine_refusal_is_shown(self) -> None:
        self.start()
        self.engine.tool = lambda name, args, _orig=self.engine.tool: (  # type: ignore[method-assign]
            {"error": "comparison runs require a model judge on the suite", "hint": "set one"}
            if name == "trigger_eval_run" else _orig(name, args))
        code, log, _, _ = self.run_action()
        self.assertEqual(code, 2)
        self.assertIn("trigger_eval_run: comparison runs require a model judge on the suite "
                      "(set one)", log)

    def test_bad_inputs(self) -> None:
        self.start()
        self.assertEqual(self.run_action(min_pass_rate="150")[0], 2)
        self.assertEqual(self.run_action(min_pass_rate="lots")[0], 2)
        self.assertEqual(self.run_action(suite="")[0], 2)
        self.assertEqual(self.run_action(environment="dev")[0], 2)
        self.assertEqual(self.engine.calls, [])


class Instructions(ActionTestCase):
    def write(self, text: str) -> str:
        path = os.path.join(self.tmp.name, "instructions.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_draft_is_saved_unpinned_and_tested_by_version(self) -> None:
        self.start()
        path = self.write("You are helpful. Always greet in Arabic: مرحبا")
        code, _, summary, _ = self.run_action(instructions_file=path)
        self.assertEqual(code, 0)
        created = self.engine.tool_calls("prompts", "create_version")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["prompt_id"], "prompt-1")
        self.assertNotIn("profile_id", created[0])  # profile_id would publish it
        self.assertIn("مرحبا", created[0]["content"])
        trigger = self.engine.tool_calls("trigger_eval_run")[0]
        self.assertEqual(trigger["prompt_overrides"], {"system": 2})
        self.assertEqual(trigger["variant_label"], "CI: draft v2")
        self.assertEqual(self.engine.served_version, 1)
        self.assertIn("saved as version 2, not published", summary)

    def test_same_draft_twice_reuses_the_version(self) -> None:
        self.start()
        path = self.write("New text")
        self.run_action(instructions_file=path)
        self.engine.polls = 0
        code, _, summary, _ = self.run_action(instructions_file=path)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.engine.tool_calls("prompts", "create_version")), 1)
        self.assertEqual(self.engine.tool_calls("trigger_eval_run")[1]["prompt_overrides"],
                         {"system": 2})
        self.assertIn("version 2 (already saved)", summary)

    def test_file_equal_to_served_text_adds_no_override(self) -> None:
        self.start()
        code, _, summary, _ = self.run_action(instructions_file=self.write("You are helpful."))
        self.assertEqual(code, 0)
        self.assertEqual(self.engine.tool_calls("prompts", "create_version"), [])
        self.assertNotIn("prompt_overrides", self.engine.tool_calls("trigger_eval_run")[0])
        self.assertIn("matches the instructions the agent already serves", summary)

    def test_agent_on_template_instructions_is_refused(self) -> None:
        self.start()
        self.engine.own_prompt = False
        code, log, _, _ = self.run_action(instructions_file=self.write("New text"))
        self.assertEqual(code, 2)
        self.assertIn("still uses its template's instructions", log)
        self.assertEqual(self.engine.tool_calls("prompts", "create_version"), [])
        self.assertEqual(self.engine.tool_calls("trigger_eval_run"), [])

    def test_missing_or_empty_file(self) -> None:
        self.start()
        code, log, _, _ = self.run_action(instructions_file="/no/such/file.md")
        self.assertEqual(code, 2)
        self.assertIn("Could not read instructions-file", log)
        self.assertEqual(self.run_action(instructions_file=self.write("  \n"))[0], 2)


class Regression(ActionTestCase):
    def test_regressions_are_reported_and_can_fail_the_job(self) -> None:
        self.start(previous_run="run-41", regressed=["refunds"],
                   results=[case("greets", True), case("refunds", False, error="x")])
        code, _, summary, outputs = self.run_action(min_pass_rate="50")
        self.assertEqual(code, 0)  # reported only
        self.assertEqual(outputs["regressed"], "1")
        self.assertIn("Broke since the previous run (`run-41`): `refunds`", summary)
        compare = self.engine.tool_calls("eval_runs", "compare")[0]
        self.assertEqual(compare["run_ids"], ["run-41", "run-42"])

        self.engine.polls = 0
        code, log, _, _ = self.run_action(min_pass_rate="50", fail_on_regression="true")
        self.assertEqual(code, 1)
        self.assertIn("1 case(s) broke since the previous run: refunds", log)

    def test_no_previous_run(self) -> None:
        self.start()
        code, _, summary, outputs = self.run_action(fail_on_regression="true")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["regressed"], "")
        self.assertIn("No previous finished run", summary)
        self.assertEqual(self.engine.tool_calls("eval_runs", "compare"), [])


if __name__ == "__main__":
    unittest.main()
