"""SDK tests against the fake opencode in ../conformance. Run: python -m unittest discover -s tests"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
os.environ["RELAYAI_HOME"] = tempfile.mkdtemp(prefix="relayai-sdk-test-")

from relayai import ApiError, Client, Config, Gateway, RelayAI, parse_tool_calls  # noqa: E402

FAKE = HERE.parent.parent / "conformance" / "fake_opencode.py"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeOpencode:
    def __enter__(self):
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, str(FAKE), "serve", "--port", str(self.port)],
                                     env=dict(os.environ, OPENCODE_SERVER_PASSWORD="testpw"),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(10)


class ToolParsingTests(unittest.TestCase):
    def test_parses_calls_and_keeps_text(self):
        text, calls = parse_tool_calls('Sure.\n<tool_call>\n{"name": "f", "arguments": {"a": 1}}\n</tool_call>', {"f"})
        self.assertEqual(text, "Sure.")
        self.assertEqual(calls, [{"name": "f", "input": {"a": 1}}])

    def test_unknown_tool_is_left_as_text(self):
        text, calls = parse_tool_calls('<tool_call>{"name": "x", "arguments": {}}</tool_call>', {"f"})
        self.assertEqual(calls, [])
        self.assertIn("tool_call", text)

    def test_missing_closing_tag_and_string_arguments(self):
        _, calls = parse_tool_calls('<tool_call>{"name": "f", "arguments": "{\\"a\\": 2}"}', {"f"})
        self.assertEqual(calls, [{"name": "f", "input": {"a": 2}}])

    def test_several_calls(self):
        raw = ('<tool_call>{"name": "f", "arguments": {}}</tool_call>'
               '<tool_call>{"name": "g", "arguments": {"b": true}}</tool_call>')
        _, calls = parse_tool_calls(raw, {"f", "g"})
        self.assertEqual([c["name"] for c in calls], ["f", "g"])


class SdkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOpencode().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.fake.__exit__()

    def ai(self, **kw):
        return RelayAI(reuse=False, opencode_url=self.fake.url, opencode_password="testpw", **kw)

    def test_chat_stream_models(self):
        with self.ai() as ai:
            self.assertEqual(ai.chat("Say PONG"), "PONG")
            self.assertEqual("".join(ai.stream("Say PONG")), "PONG")
            self.assertEqual(ai.models(), ["fake/echo", "fake/toolbot"])
            self.assertTrue(ai.base_url.endswith("/v1"))
            self.assertEqual(ai.health()["status"], "ok")

    def test_tools_round_trip_with_complete(self):
        tool = {"type": "function", "function": {"name": "get_weather", "parameters": {
            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
        with self.ai(model="fake/toolbot") as ai:
            reply = ai.complete(messages=[{"role": "user", "content": "Weather?"}], tools=[tool])
            call = reply["choices"][0]["message"]["tool_calls"][0]
            self.assertEqual(call["function"]["name"], "get_weather")
            followup = [{"role": "user", "content": "Weather?"}, reply["choices"][0]["message"],
                        {"role": "tool", "tool_call_id": call["id"], "content": "sunny"}]
            self.assertEqual(ai.chat(followup, tools=[tool]), "Tool said: sunny")

    def test_anthropic_messages(self):
        with self.ai() as ai:
            reply = ai.messages(messages=[{"role": "user", "content": "hi"}])
            self.assertEqual(reply["content"][0]["text"], "PONG")

    def test_errors_are_api_errors(self):
        with self.ai() as ai:
            with self.assertRaises(ApiError) as ctx:
                ai.chat("hi", model="nope/nope")
            self.assertEqual(ctx.exception.status, 404)
            with self.assertRaises(ApiError) as ctx:
                ai.chat("FAIL_QUOTA")
            self.assertEqual(ctx.exception.status, 503)
            bad = Client(ai.url, api_key="wrong")
            with self.assertRaises(ApiError) as ctx:
                bad.models()
            self.assertEqual(ctx.exception.status, 401)

    def test_embedded_gateway(self):
        config = Config.from_env(api_key="k", port=0, opencode_url=self.fake.url, opencode_password="testpw")
        with Gateway(config) as gw:
            self.assertEqual(Client(gw.base_url, "k").chat("hi"), "PONG")

    def test_openai_sdk_if_installed(self):
        try:
            from openai import OpenAI
        except ImportError:
            self.skipTest("openai package not installed")
        with self.ai() as ai:
            client = OpenAI(base_url=ai.base_url, api_key=ai.api_key)
            r = client.chat.completions.create(model="default", messages=[{"role": "user", "content": "hi"}])
            self.assertEqual(r.choices[0].message.content, "PONG")


if __name__ == "__main__":
    unittest.main()
