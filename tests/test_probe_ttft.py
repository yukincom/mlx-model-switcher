import io
import json
import unittest
from unittest.mock import patch

from tools.probe_ttft import measure


class ProbeTests(unittest.TestCase):
    def stream(self, events):
        return io.BytesIO(b": keepalive\n\n" + b"".join(
            b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
        ) + b"data: [DONE]\n\n")

    def test_separates_headers_role_reasoning_content_and_usage(self):
        usage = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 90}}
        events = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"reasoning_content": "private reasoning"}}]},
            {"choices": [{"delta": {"content": "private reply"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage},
        ]
        payload = {"model": "test", "messages": [{"role": "user", "content": "private input"}]}
        with patch("tools.probe_ttft.urlopen", return_value=self.stream(events)) as opened, \
             patch("tools.probe_ttft.time.perf_counter", side_effect=range(8)):
            result = measure("http://localhost:8080/v1/", payload)
        self.assertEqual(result["headers_ms"], 1000)
        self.assertEqual(result["first_event_ms"], 2000)
        self.assertEqual(result["first_token_ms"], 3000)
        self.assertEqual(result["first_content_ms"], 4000)
        self.assertEqual(result["total_ms"], 7000)
        self.assertEqual(result["finish_reason"], "stop")
        self.assertEqual(result["usage"], usage)
        self.assertNotIn("private", json.dumps(result))
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8080/v1/chat/completions")
        self.assertTrue(json.loads(request.data)["stream_options"]["include_usage"])
        self.assertNotIn("stream", payload)

    def test_tool_only_output_has_no_visible_content(self):
        events = [{"choices": [{"delta": {"tool_calls": [{"index": 0}]}, "finish_reason": "tool_calls"}]}]
        with patch("tools.probe_ttft.urlopen", return_value=self.stream(events)):
            result = measure("http://localhost/v1", {})
        self.assertIsNotNone(result["first_token_ms"])
        self.assertIsNone(result["first_content_ms"])
        self.assertIsNone(result["usage"])

    def test_error_event_is_not_a_successful_measurement(self):
        with patch("tools.probe_ttft.urlopen", return_value=self.stream([{"error": {"message": "private"}}])):
            with self.assertRaisesRegex(RuntimeError, "Server returned an error event"):
                measure("http://localhost/v1", {})

    def test_truncated_stream_is_not_a_successful_measurement(self):
        response = io.BytesIO(b'data: {"choices": [{"delta": {"content": "partial"}}]}\n\n')
        with patch("tools.probe_ttft.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "without a completion marker"):
                measure("http://localhost/v1", {})


if __name__ == "__main__":
    unittest.main()
