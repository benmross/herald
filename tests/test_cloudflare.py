"""`herald cf` hands Cloudflare's CLI the stored token and nothing else."""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import cloudflare


class Environment(unittest.TestCase):
    def patched(self, token="tok-123", settings=None):
        return (mock.patch.object(cloudflare.config, "secret", return_value=token),
                mock.patch.object(cloudflare, "settings", return_value=settings or {}))

    def test_token_and_account_reach_the_child_and_competing_identities_do_not(self):
        secret, settings = self.patched(settings={"account_id": " acct "})
        with secret as read, settings:
            env = cloudflare.environment({"PATH": "/bin", "CLOUDFLARE_API_KEY": "global",
                                          "CLOUDFLARE_EMAIL": "x", "CLOUDFLARE_AUTH_PROFILE": "p"})
        read.assert_called_once_with(cloudflare.DEFAULT_SECRET)
        self.assertEqual(env["CLOUDFLARE_API_TOKEN"], "tok-123")
        self.assertEqual(env["CLOUDFLARE_ACCOUNT_ID"], "acct")
        self.assertEqual(env["PATH"], "/bin")
        for name in cloudflare.SCRUBBED:
            self.assertNotIn(name, env)

    def test_custom_secret_path_is_honoured(self):
        secret, settings = self.patched(settings={"token_secret": "infra.cf"})
        with secret as read, settings:
            cloudflare.environment({})
        read.assert_called_once_with("infra.cf")

    def test_missing_token_says_how_to_store_one(self):
        secret, settings = self.patched(token=None)
        with secret, settings, self.assertRaises(cloudflare.Unavailable) as caught:
            cloudflare.environment({})
        self.assertIn("herald secret request cloudflare.api_token", str(caught.exception))

    def test_run_never_puts_the_token_on_the_command_line(self):
        secret, settings = self.patched()
        with secret, settings, \
                mock.patch.object(cloudflare.shutil, "which", return_value="/usr/bin/cf"), \
                mock.patch.object(cloudflare.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(cloudflare.run(["deploy", "--dry-run"]), 0)
        command = run.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/cf", "deploy", "--dry-run"])
        self.assertNotIn("tok-123", " ".join(command))
        self.assertEqual(run.call_args.kwargs["env"]["CLOUDFLARE_API_TOKEN"], "tok-123")

    def test_missing_cli_is_a_clear_failure(self):
        secret, settings = self.patched()
        with secret, settings, mock.patch.object(cloudflare.shutil, "which", return_value=None), \
                mock.patch.object(cloudflare.subprocess, "run") as run:
            self.assertEqual(cloudflare.run(["auth", "whoami"]), 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
