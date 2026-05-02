from __future__ import annotations

import unittest
from unittest import mock

from agent.host_guard import (
    extract_cross_host_targets,
    parse_forbidden_hosts,
    verify_command_targets,
)


class HostGuardTests(unittest.TestCase):
    def test_parse_forbidden_hosts_splits_commas_and_spaces(self) -> None:
        self.assertEqual(
            parse_forbidden_hosts("devlab, qa-host staging"),
            {"devlab", "qa-host", "staging"},
        )

    def test_extract_cross_host_targets_finds_ssh_scp_curl_and_docker(self) -> None:
        command = (
            "ssh devbox uptime && "
            "scp build.txt user@artifact:/tmp/build.txt && "
            "curl https://api.example.com/health && "
            "docker --host tcp://dockerd.example:2375 ps"
        )
        targets = extract_cross_host_targets(command)
        self.assertEqual(
            [(target.tool, target.host) for target in targets],
            [
                ("ssh", "devbox"),
                ("scp", "artifact"),
                ("curl", "api.example.com"),
                ("docker", "dockerd.example"),
            ],
        )

    def test_verify_command_targets_blocks_forbidden_host(self) -> None:
        with mock.patch.dict("os.environ", {"KAI_FORBIDDEN_HOSTS": "devlab"}, clear=False):
            result = verify_command_targets("ssh devlab hostname")

        self.assertTrue(result.blocked)
        self.assertIn("forbidden", result.output)
        self.assertEqual(result.exit_code, 2)

    def test_verify_command_targets_skips_localhost(self) -> None:
        result = verify_command_targets("curl http://localhost:8765/api/status")

        self.assertFalse(result.blocked)
        self.assertEqual(result.output, "")

    def test_verify_command_targets_runs_verify_script_for_remote_host(self) -> None:
        result = verify_command_targets("ssh localhost.example uptime")

        self.assertFalse(result.blocked)
        self.assertIn("[host-verify] preamble", result.output)
        self.assertIn("intended_host=localhost.example", result.output)


if __name__ == "__main__":
    unittest.main()
