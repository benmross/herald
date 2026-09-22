"""The Codex engine, offline.

`think._handle_codex_line` has to turn `codex exec --json` into the same
`state` that `_handle_stream_line` builds from `claude -p`, because everything
above it -- the ok/error split in `think()`, the runs row, the Telegram
progress display -- reads one shape. The stream lines here are copied from
real runs of codex-cli 0.153.4 on 22 September 2026, not written from the
docs, which is the only way a parser test is worth anything.

Nothing here launches an engine.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import think  # noqa: E402


def _feed(lines: list[dict], seen: list | None = None) -> dict:
    state = {"payload": None, "t0": 0.0, "mark": None, "saw_init": False,
             "phases": [], "pending_tools": {}, "round_trips": 0}
    for obj in lines:
        think._handle_codex_line(json.dumps(obj), state,
                                 seen.append if seen is not None else None)
    return state


THREAD = {"type": "thread.started", "thread_id": "01a0c992-98d3-7ce2-878d-63b1ca1f0ef1"}
USAGE = {"input_tokens": 29520, "cached_input_tokens": 14464,
         "cache_write_input_tokens": 0, "output_tokens": 66,
         "reasoning_output_tokens": 0}


class CodexStreamTests(unittest.TestCase):
    def test_a_plain_answer_becomes_a_claude_shaped_payload(self):
        state = _feed([THREAD, {"type": "turn.started"},
                       {"type": "item.completed",
                        "item": {"id": "item_0", "type": "agent_message", "text": "ok"}},
                       {"type": "turn.completed", "usage": USAGE}])
        payload = state["payload"]
        self.assertEqual(payload["result"], "ok")
        self.assertFalse(payload["is_error"])
        self.assertEqual(payload["session_id"], THREAD["thread_id"])
        self.assertEqual(payload["usage"]["input_tokens"], 29520)
        self.assertEqual(payload["usage"]["cache_read_input_tokens"], 14464)

    def test_a_command_is_a_tool_phase_and_a_progress_line(self):
        seen: list = []
        started = {"id": "item_1", "type": "command_execution",
                   "command": "/bin/bash -lc 'echo probe-ok && pwd'",
                   "aggregated_output": "", "exit_code": None, "status": "in_progress"}
        done = dict(started, aggregated_output="probe-ok\n", exit_code=0,
                    status="completed")
        state = _feed([THREAD, {"type": "turn.started"},
                       {"type": "item.completed",
                        "item": {"id": "item_0", "type": "agent_message",
                                 "text": "I'll run the command now."}},
                       {"type": "item.started", "item": started},
                       {"type": "item.completed", "item": done},
                       {"type": "item.completed",
                        "item": {"id": "item_2", "type": "agent_message",
                                 "text": "{\"answer\":\"probe-ok\",\"n\":7}"}},
                       {"type": "turn.completed", "usage": USAGE}], seen)
        kinds = [p.kind for p in seen]
        self.assertEqual(kinds, ["text", "tool", "text"])
        self.assertEqual(seen[1].text, "Bash: echo probe-ok && pwd")
        tools = [p for p in state["phases"] if p["kind"] == "tool"]
        self.assertEqual([t["name"] for t in tools], ["Bash"])
        self.assertEqual(state["pending_tools"], {})
        # The final message is the answer, not the narration before the tool.
        self.assertEqual(json.loads(state["payload"]["result"])["n"], 7)

    def test_cli_notices_are_kept_but_not_counted(self):
        """codex-cli emits its own warnings as `error` items before the turn
        starts -- the hook-trust bypass notice, a hooks file it could not
        parse. They are not model output and must not count as round trips."""
        notice = {"type": "item.completed",
                  "item": {"id": "item_0", "type": "error",
                           "message": "failed to parse hooks config x: unknown field"}}
        state = _feed([THREAD, notice, {"type": "turn.started"},
                       {"type": "item.completed",
                        "item": {"id": "item_1", "type": "agent_message", "text": "ok"}},
                       {"type": "turn.completed", "usage": USAGE}])
        self.assertIn("unknown field", state["notices"][0])
        self.assertEqual(state["payload"]["result"], "ok")

    def test_a_failed_turn_is_an_error_payload(self):
        msg = "Invalid schema for response_format 'codex_output_schema'"
        state = _feed([THREAD, {"type": "turn.started"},
                       {"type": "error", "message": msg},
                       {"type": "turn.failed", "error": {"message": msg}}])
        self.assertTrue(state["payload"]["is_error"])
        self.assertIn("Invalid schema", state["payload"]["result"])
        self.assertEqual(state["payload"]["session_id"], THREAD["thread_id"])

    def test_the_thread_id_survives_a_run_that_died_early(self):
        stream = json.dumps(THREAD) + "\n" + json.dumps({"type": "turn.started"}) + "\n"
        self.assertEqual(think._session_from_stream(stream), THREAD["thread_id"])

    def test_garbage_lines_are_ignored(self):
        state = _feed([])
        think._handle_codex_line("not json", state, None)
        think._handle_codex_line(json.dumps([1, 2]), state, None)
        self.assertIsNone(state["payload"])


class StrictSchemaTests(unittest.TestCase):
    """OpenAI's strict mode rejected the dawn cycle's schema verbatim
    (`'additionalProperties' is required to be supplied and to be false`);
    the derived schema was accepted. Both checked against the real CLI."""

    def test_objects_are_closed_and_optionals_made_nullable(self):
        loose = {"type": "object",
                 "properties": {"headline": {"type": "string"},
                                "priority": {"type": "string",
                                             "enum": ["low", "high"]},
                                "items": {"type": "array",
                                          "items": {"type": "object",
                                                    "properties": {"t": {"type": "string"}},
                                                    "required": ["t"]}}},
                 "required": ["headline"]}
        strict = think._strict_schema(loose)
        self.assertFalse(strict["additionalProperties"])
        self.assertEqual(sorted(strict["required"]), ["headline", "items", "priority"])
        self.assertEqual(strict["properties"]["headline"]["type"], "string")
        self.assertEqual(strict["properties"]["priority"]["type"], ["string", "null"])
        self.assertIn(None, strict["properties"]["priority"]["enum"])
        inner = strict["properties"]["items"]["items"]
        self.assertFalse(inner["additionalProperties"])
        self.assertEqual(inner["required"], ["t"])
        # The original is untouched: the cycles hand the same dict to Claude.
        self.assertNotIn("additionalProperties", loose)

    def test_toml_string_round_trips_through_tomllib(self):
        import tomllib
        text = 'He said "hi"\\n\tand left — naïve. \x01'
        parsed = tomllib.loads("k = " + think._toml_string(text))
        self.assertEqual(parsed["k"], text)


class EffortTests(unittest.TestCase):
    def test_the_vocabulary_maps_onto_both_engines(self):
        self.assertEqual(think.codex_effort("max"), "xhigh")
        self.assertEqual(think.codex_effort("low"), "low")
        self.assertEqual(think.claude_effort("minimal"), "low")
        self.assertEqual(think.claude_effort("max"), "max")
        self.assertIsNone(think.codex_effort(None))

    def test_unknown_levels_and_engines_are_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            think.normalize_effort("ultra")
        with self.assertRaises(ValueError):
            think.resolve_engine("gemini")
        self.assertEqual(think.normalize_effort(" High "), "high")
        self.assertIsNone(think.normalize_effort(""))

    def test_the_default_engine_comes_from_config(self):
        with mock.patch.object(think.config, "get", return_value="codex"):
            self.assertEqual(think.resolve_engine(None), "codex")
        self.assertEqual(think.resolve_engine("claude"), "claude")


class CommandBuildTests(unittest.TestCase):
    """What `_run_codex` actually launches, captured at Popen."""

    def _capture(self, **kw):
        calls = {}

        def fake_run(engine, cmd, prompt, **rest):
            calls["engine"], calls["cmd"], calls["prompt"] = engine, cmd, prompt
            calls["steerable"] = rest["steerable"]
            return (0, None, None, "", "", False)

        with mock.patch.object(think, "_run_process", fake_run), \
                mock.patch.object(think.config, "get",
                                  side_effect=lambda k, d=None: d):
            think._run_codex("hello", model=kw.get("model"), effort=kw.get("effort"),
                             cwd=pathlib.Path("/tmp"), timeout=1, idle_timeout=1,
                             append_system_prompt=kw.get("sys"), json_schema=kw.get("schema"),
                             resume=kw.get("resume"), permission_mode=kw.get("mode", "auto"),
                             add_dirs=None)
        return calls

    def test_a_fresh_run_reads_the_prompt_from_stdin_and_the_constitution(self):
        calls = self._capture(model="gpt-6-astra", effort="xhigh", sys="card")
        cmd = calls["cmd"]
        self.assertEqual(cmd[:3], ["codex", "exec", "--json"])
        self.assertEqual(cmd[-1], "-")
        self.assertEqual(calls["prompt"], "hello")
        self.assertFalse(calls["steerable"])
        self.assertIn('project_doc_fallback_filenames=["CLAUDE.md"]', cmd)
        self.assertIn('model_reasoning_effort="xhigh"', cmd)
        self.assertIn('developer_instructions="card"', cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6-astra")
        self.assertIn('sandbox_mode="danger-full-access"', cmd)
        self.assertIn('approval_policy="never"', cmd)
        self.assertIn("--dangerously-bypass-hook-trust", cmd)

    def test_resume_and_plan_mode(self):
        calls = self._capture(resume="abc", mode="plan")
        cmd = calls["cmd"]
        self.assertEqual(cmd[1:4], ["exec", "resume", "abc"])
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertNotIn("-m", cmd)
        self.assertFalse(any(a.startswith("approval_policy") for a in cmd))

    def test_the_schema_file_is_strict_and_cleaned_up(self):
        written = {}
        real_run = think._run_process

        def fake_run(engine, cmd, prompt, **rest):
            path = pathlib.Path(cmd[cmd.index("--output-schema") + 1])
            written["schema"] = json.loads(path.read_text())
            written["path"] = path
            return (0, None, None, "", "", False)

        with mock.patch.object(think, "_run_process", fake_run), \
                mock.patch.object(think.config, "get", side_effect=lambda k, d=None: d):
            think._run_codex("x", model=None, effort=None, cwd=pathlib.Path("/tmp"),
                             timeout=1, idle_timeout=1, append_system_prompt=None,
                             json_schema={"type": "object",
                                          "properties": {"a": {"type": "string"}}},
                             resume=None, permission_mode="auto", add_dirs=None)
        self.assertFalse(written["schema"]["additionalProperties"])
        self.assertFalse(written["path"].exists())
        del real_run


if __name__ == "__main__":
    unittest.main()
