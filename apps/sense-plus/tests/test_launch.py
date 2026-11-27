import contextlib
import io
import unittest
from unittest.mock import patch

from run import __version__, main


class DuplicateLaunchTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("run.ensure_port_available", side_effect=OSError("포트 사용 중")).start()
        self.status = patch("run.read_loopback_status").start()
        self.browser = patch("run.webbrowser.open", return_value=True).start()
        self.health = {
            "ok": True, "version": __version__, "mode": "live", "pairing_required": False,
        }

    def test_opens_admin_for_matching_running_app(self):
        self.status.side_effect = [self.health, {
            "user_url": "http://127.0.0.1:8765", "pairing_required": False,
        }]

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--open-browser"]), 0)

        self.browser.assert_called_once_with("http://127.0.0.1:8766", new=2)

    def test_keeps_conflict_for_mismatched_or_missing_services(self):
        for responses in (
            [{"ok": True}],
            [self.health, {"user_url": "http://127.0.0.1:8775", "pairing_code": None}],
            [self.health, {"user_url": "http://127.0.0.1:8765", "pairing_required": True}],
            [self.health, ConnectionRefusedError()],
        ):
            with self.subTest(responses=responses), contextlib.redirect_stderr(io.StringIO()):
                self.status.side_effect = responses
                with self.assertRaises(SystemExit) as failure:
                    main(["--open-browser"])
                self.assertEqual(failure.exception.code, 2)

        self.browser.assert_not_called()
