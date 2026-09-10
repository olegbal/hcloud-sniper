"""Loop, HTTP-error and Slack tests. No real network: Sniper.req / urlopen are mocked."""
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

import sniper
from sniper import Sniper

ENV = {"HCLOUD_TOKEN": "tok", "HC_TYPE": "cx33", "HC_LOCATIONS": "fsn1,nbg1",
       "HC_NAME": "test-node", "HC_INTERVAL": "45"}


class LoopBreak(BaseException):
    """Ends run()'s while-loop from a mock; BaseException so `except Exception` misses it."""


def types_body(available, all_locations=("fsn1", "nbg1", "hel1")):
    """GET /server_types?name=... response with per-location availability."""
    return {"server_types": [{"id": 22, "name": "cx33", "locations": [
        {"id": i, "name": name, "available": name in available,
         "recommended": False, "deprecation": None}
        for i, name in enumerate(all_locations, 1)]}]}


CREATED = {"server": {"id": 4711, "name": "test-node",
                      "public_net": {"ipv4": {"ip": "1.2.3.4"}}},
           "root_password": "s3cret"}


def http_error(code, error_code, headers=None):
    body = json.dumps({"error": {"code": error_code, "message": error_code}}).encode()
    return urllib.error.HTTPError("https://api.hetzner.cloud/v1/servers", code,
                                  error_code, headers or {}, io.BytesIO(body))


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, ENV, clear=True))
        self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))
        self.sleep = self.enterContext(mock.patch("sniper.time.sleep"))
        self.req = self.enterContext(mock.patch.object(Sniper, "req", autospec=True))

    def calls(self):
        return [(c.args[1], c.args[2]) for c in self.req.call_args_list]

    def test_available_creates_once_and_exits_zero(self):
        self.req.side_effect = [types_body(["nbg1"]), CREATED]

        self.assertEqual(Sniper().run(), 0)

        self.assertEqual(self.calls(), [("GET", "/server_types?name=cx33"),
                                        ("POST", "/servers")])
        body = self.req.call_args.args[3]
        self.assertEqual(body["location"], "nbg1")
        self.assertEqual(body["server_type"], "cx33")

    def test_unavailable_keeps_polling(self):
        self.req.side_effect = [types_body([]), types_body(["hel1"]), LoopBreak]

        with self.assertRaises(LoopBreak):
            Sniper().run()

        self.assertEqual([m for m, _ in self.calls()], ["GET", "GET", "GET"])
        self.sleep.assert_called_with(45)

    def test_resource_unavailable_retries_then_creates(self):
        self.req.side_effect = [types_body(["fsn1"]), http_error(412, "resource_unavailable"),
                                types_body(["fsn1"]), CREATED]

        self.assertEqual(Sniper().run(), 0)

        self.assertEqual([m for m, _ in self.calls()], ["GET", "POST", "GET", "POST"])
        self.sleep.assert_called_once_with(45)  # normal interval, no backoff

    def test_rate_limit_backs_off_once(self):
        self.req.side_effect = [http_error(429, "rate_limit_exceeded"),
                                types_body(["fsn1"]), CREATED]

        self.assertEqual(Sniper().run(), 0)

        # one backoff, and no extra interval nap stacked on top of it
        self.assertEqual(self.sleep.call_args_list, [mock.call(60)])

    def test_low_budget_stretches_the_nap(self):
        self.req.side_effect = [
            http_error(429, "rate_limit_exceeded", {"RateLimit-Remaining": "12"}),
            types_body([]), LoopBreak]

        with mock.patch.dict(os.environ, {**ENV, "HC_INTERVAL": "5"}, clear=True):
            with self.assertRaises(LoopBreak):
                Sniper().run()

        # a 5s poll backs off to 30s while the budget is nearly spent
        self.assertEqual(self.sleep.call_args_list, [mock.call(60), mock.call(30)])

    def test_five_consecutive_http_errors_notify_once(self):
        self.req.side_effect = [http_error(403, "resource_limit_exceeded")] * 6 + [LoopBreak]

        with mock.patch.object(Sniper, "notify", autospec=True) as notify:
            with self.assertRaises(LoopBreak):
                Sniper().run()

        alerts = [c.args[1] for c in notify.call_args_list if ":x:" in c.args[1]]
        self.assertEqual(len(alerts), 1)
        self.assertIn("403", alerts[0])

    def test_non_http_exception_is_swallowed(self):
        self.req.side_effect = [urllib.error.URLError("dns"), types_body(["fsn1"]), CREATED]

        self.assertEqual(Sniper().run(), 0)

    def test_unknown_server_type_exits(self):
        self.req.side_effect = [{"server_types": []}]

        with self.assertRaises(SystemExit):  # not caught by the loop's except Exception
            Sniper().run()


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, ENV, clear=True))
        self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))
        self.sleep = self.enterContext(mock.patch("sniper.time.sleep"))
        self.s = Sniper()

    def test_full_budget_uses_the_interval(self):
        self.s.remaining = 3000
        self.s.wait()
        self.sleep.assert_called_once_with(45)

    def test_unknown_budget_uses_the_interval(self):
        self.s.wait()
        self.sleep.assert_called_once_with(45)

    def test_headers_without_the_limit_are_ignored(self):
        self.s.note_limits({"Content-Type": "application/json"})
        self.s.note_limits(None)
        self.assertIsNone(self.s.remaining)

    def test_interval_floor_is_one_second(self):
        with mock.patch.dict(os.environ, {**ENV, "HC_INTERVAL": "0"}, clear=True):
            self.assertEqual(Sniper().interval, 1)


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))

    def sniper(self, **over):
        self.enterContext(mock.patch.dict(os.environ, {**ENV, **over}, clear=True))
        return Sniper()

    def test_posts_json_to_webhook(self):
        s = self.sniper(SLACK_WEBHOOK="https://hooks.slack.example/T/B/X")
        with mock.patch("sniper.urllib.request.urlopen") as urlopen:
            s.notify("hello")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://hooks.slack.example/T/B/X")
        self.assertEqual(json.loads(request.data), {"text": "hello"})

    def test_slack_failure_is_swallowed(self):
        s = self.sniper(SLACK_WEBHOOK="https://hooks.slack.example/T/B/X")
        with mock.patch("sniper.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            s.notify("hello")  # must not raise
        self.assertIn("slack notify failed", sniper.sys.stdout.getvalue())

    def test_no_webhook_is_log_only(self):
        s = self.sniper()
        with mock.patch("sniper.urllib.request.urlopen") as urlopen:
            s.notify("hello")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
