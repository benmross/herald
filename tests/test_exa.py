"""Exa's transport fails clearly without authorizing accounts or paid tools."""

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import exa


def reply(content="A public result", **extra):
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": content}], **extra}}


class Retrieval(unittest.TestCase):
    def response(self, body, status=200):
        session = mock.MagicMock()
        response = session.__enter__.return_value.post.return_value.__enter__.return_value
        response.status_code = status
        response.iter_content.return_value = [body.encode()]
        return mock.patch.object(exa.requests, "Session", return_value=session), session

    def test_search_uses_keyless_endpoint_and_current_required_objective(self):
        patch, session = self.response(json.dumps(reply("Café https://example.com")))
        with patch:
            self.assertIn("Café", exa.search("public research programmes", limit=3))
        args, kwargs = session.__enter__.return_value.post.call_args
        self.assertEqual(args, (exa.ENDPOINT,))
        self.assertNotIn("auth", kwargs)
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertFalse(session.__enter__.return_value.trust_env)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["params"]["name"], "web_search_exa")
        arguments = kwargs["json"]["params"]["arguments"]
        self.assertTrue(arguments["objective"])
        self.assertEqual(arguments["numResults"], 3)

    def test_sse_comments_notifications_and_crlf(self):
        raw = ': heartbeat\r\n\r\ndata: {"method":"notifications/progress"}\r\n\r\n'
        raw += 'event: message\r\ndata: ' + json.dumps(reply("Result")) + '\r\n\r\n'
        patch, _ = self.response(raw)
        with patch:
            self.assertEqual(exa.fetch(["https://example.com/page"]), "Result")

    def test_http_errors_do_not_retry_or_return_provider_body(self):
        for status in (302, 401, 402, 403, 429, 500):
            with self.subTest(status=status):
                patch, session = self.response("untrusted error body", status)
                with patch, self.assertRaises(exa.Unavailable) as caught:
                    exa.search("public query")
                self.assertNotIn("untrusted", str(caught.exception))
                session.__enter__.return_value.post.assert_called_once()

    def test_tool_and_protocol_errors_are_not_empty_successes(self):
        for raw in (json.dumps(reply("rate limited", isError=True)),
                    json.dumps({"id": 1, "error": {"message": "bad"}}),
                    'data: {broken}\n\n', '{}',
                    json.dumps({"id": 1, "result": {"content": []}})):
            with self.subTest(raw=raw):
                patch, _ = self.response(raw)
                with patch, self.assertRaises(exa.Unavailable):
                    exa.search("public query")

    def test_network_timeout_is_actionable(self):
        with mock.patch.object(exa.requests, "Session", side_effect=exa.requests.Timeout), \
                self.assertRaisesRegex(exa.Unavailable, "existing search"):
            exa.search("public query")

    def test_oversized_response_is_refused(self):
        patch, _ = self.response("x" * (exa.MAX_RESPONSE + 1))
        with patch, self.assertRaisesRegex(exa.Unavailable, "size limit"):
            exa.search("public query")

    def test_paid_tools_and_invalid_inputs_never_reach_transport(self):
        with mock.patch.object(exa.requests, "Session") as session:
            for tool in ("agent_run", "answer", "web_search_advanced_exa"):
                with self.assertRaises(ValueError):
                    exa._call(tool, {})
            for kwargs in ({"query": ""}, {"query": "q", "limit": 100},
                           {"query": "q", "objective": " "}):
                with self.assertRaises(ValueError):
                    exa.search(**kwargs)
            for urls in ([], ["http://localhost/a"], ["http://127.0.0.1/a"],
                         ["http://10.0.0.1/"], ["http://[::1]/"],
                         ["https://host.internal/path"], ["file:///etc/passwd"],
                         ["https://user:password@example.com/"],
                         ["https://example.com:8888/"]):
                with self.assertRaises(ValueError):
                    exa.fetch(urls)
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
