from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import publish_github_release as publisher


class ReleaseGateTests(unittest.TestCase):
    def run_publisher(self, side_effect):
        with (
            patch.object(publisher.sys, "argv", ["publish", "--version", "1.1.24"]),
            patch.object(publisher.shutil, "which", return_value="gh"),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "stat", return_value=SimpleNamespace(st_size=1024)),
            patch.object(publisher.subprocess, "run", side_effect=side_effect) as run,
            redirect_stdout(io.StringIO()),
        ):
            try:
                publisher.main()
            except subprocess.CalledProcessError:
                return run.call_args_list, True
            return run.call_args_list, False

    def test_failed_checks_prevent_publishing(self):
        calls, failed = self.run_publisher(subprocess.CalledProcessError(1, "quality"))
        self.assertTrue(failed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0].args[0][1]).name, "check_project.py")
        self.assertTrue(calls[0].kwargs["check"])

    def test_checks_precede_publish(self):
        calls, failed = self.run_publisher([SimpleNamespace(returncode=0),
                                           SimpleNamespace(returncode=0)])
        self.assertFalse(failed)
        self.assertEqual(len(calls), 2)
        self.assertEqual(Path(calls[0].args[0][1]).name, "check_project.py")
        self.assertEqual(calls[1].args[0][:3], ["gh", "release", "create"])


if __name__ == "__main__":
    unittest.main()
