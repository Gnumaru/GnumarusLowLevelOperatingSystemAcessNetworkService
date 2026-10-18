"""Tests for POST /api/shell (exec_shell)."""

import base64
import os
import unittest

from tests.helpers import (json_request, request, tf, ti, tv,
                            workspace)


class TestShell(unittest.TestCase):
    def setUp(self):
        self.ws = workspace("shell")

    def sh(self, obj):
        return json_request("POST", "/api/shell", obj)

    def test_echo(self):
        status, _, data = self.sh({"command": "echo hello"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 0)
        self.assertEqual(data["stdout"], "hello\n")
        self.assertEqual(data["stderr"], "")
        self.assertEqual(tv(data["timed_out"]), False)
        self.assertEqual(data["command"], "echo hello")

    def test_stdout_stderr_separate(self):
        status, _, data = self.sh(
            {"command": "echo out; echo err 1>&2"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 0)
        self.assertEqual(data["stdout"], "out\n")
        self.assertEqual(data["stderr"], "err\n")

    def test_nonzero_exit(self):
        status, _, data = self.sh({"command": "exit 3"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 3)

    def test_command_not_found(self):
        status, _, data = self.sh(
            {"command": "gnumarus_no_such_cmd_xyz"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 127)
        self.assertIn("command not found", data["stderr"])

    def test_pipe(self):
        status, _, data = self.sh(
            {"command": "printf 'b\\na\\n' | sort"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 0)
        self.assertEqual(data["stdout"], "a\nb\n")

    def test_pipeline_returns_last(self):
        status, _, data = self.sh({"command": "false | true"})
        self.assertEqual(tv(data["returncode"]), 0)
        status, _, data = self.sh({"command": "true | false"})
        self.assertEqual(tv(data["returncode"]), 1)

    def test_redirect_to_file(self):
        with open(os.path.join(self.ws, "a.txt"), "w") as fh:
            fh.write("pear\napple\n")
        status, _, data = self.sh(
            {"command": "cat < a.txt | sort > b.txt", "cwd": self.ws})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 0)
        self.assertEqual(data["stdout"], "")  # stdout went to b.txt
        self.assertEqual(data["cwd"], self.ws)
        with open(os.path.join(self.ws, "b.txt")) as fh:
            self.assertEqual(fh.read(), "apple\npear\n")

    def test_merge_stderr_into_stdout(self):
        status, _, data = self.sh(
            {"command": "echo mixed 2>&1"})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "mixed\n")
        self.assertEqual(data["stderr"], "")

    def test_timeout_kills(self):
        status, _, data = self.sh(
            {"command": "sleep 30", "timeout": ti(1)})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["timed_out"]), True)
        self.assertEqual(tv(data["returncode"]), 124)

    def test_missing_command_400(self):
        status, _, _ = self.sh({})
        self.assertEqual(status, 400)

    def test_empty_command_400(self):
        status, _, _ = self.sh({"command": "   "})
        self.assertEqual(status, 400)

    def test_non_json_body_400(self):
        status, _, _ = request(
            "POST", "/api/shell", body=b"echo hi",
            headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)

    def test_bad_timeout_400(self):
        status, _, _ = self.sh({"command": "echo hi", "timeout": "fast"})
        self.assertEqual(status, 400)

    def test_bad_cwd_400(self):
        status, _, _ = self.sh(
            {"command": "echo hi",
             "cwd": os.path.join(self.ws, "nodir")})
        self.assertEqual(status, 400)

    def test_get_not_allowed_405(self):
        status, _, _ = request("GET", "/api/shell")
        self.assertEqual(status, 405)


class TestShellLimits(unittest.TestCase):
    def setUp(self):
        self.ws = workspace("shell_limits")

    def sh(self, obj):
        return json_request("POST", "/api/shell", obj)

    def test_stdout_truncated(self):
        status, _, data = self.sh(
            {"command": "printf '0123456789ABCDEF'", "max_stdout": ti(10)})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "0123456789")
        self.assertEqual(tv(data["stdout_truncated"]), True)
        self.assertEqual(tv(data["stderr_truncated"]), False)
        self.assertEqual(tv(data["max_stdout"]), 10)

    def test_stderr_truncated_independently(self):
        status, _, data = self.sh(
            {"command": "echo ERRMSG 1>&2", "max_stderr": ti(3),
             "max_stdout": ti(100)})
        self.assertEqual(status, 200)
        self.assertEqual(data["stderr"], "ERR")
        self.assertEqual(tv(data["stderr_truncated"]), True)
        self.assertEqual(data["stdout"], "")
        self.assertEqual(tv(data["stdout_truncated"]), False)

    def test_full_execution_despite_truncation(self):
        status, _, data = self.sh(
            {"command": "seq 1 20000 > big.txt; echo done",
             "cwd": self.ws, "max_stdout": ti(3)})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "don")
        self.assertEqual(tv(data["stdout_truncated"]), True)
        with open(os.path.join(self.ws, "big.txt")) as fh:
            self.assertEqual(sum(1 for _ in fh), 20000)

    def test_defaults_echoed(self):
        status, _, data = self.sh({"command": "echo hi"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["max_stdout"]), 1024 * 1024)
        self.assertEqual(tv(data["max_stderr"]), 1024 * 1024)
        self.assertEqual(tv(data["stdout_truncated"]), False)
        self.assertEqual(tv(data["stderr_truncated"]), False)

    def test_large_output_bounded_by_default(self):
        status, _, data = self.sh(
            {"command": "head -c 2000000 /dev/zero | tr '\\0' 'x'"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 0)
        self.assertEqual(len(data["stdout"]), 1024 * 1024)
        self.assertEqual(tv(data["stdout_truncated"]), True)

    def test_zero_limit(self):
        status, _, data = self.sh(
            {"command": "echo hi", "max_stdout": ti(0)})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "")
        self.assertEqual(tv(data["stdout_truncated"]), True)

    def test_negative_limit_400(self):
        status, _, _ = self.sh({"command": "echo hi", "max_stdout": -1})
        self.assertEqual(status, 400)

    def test_string_limit_400(self):
        status, _, _ = self.sh({"command": "echo hi", "max_stderr": "big"})
        self.assertEqual(status, 400)

    def test_bool_limit_400(self):
        status, _, _ = self.sh({"command": "echo hi", "max_stdout": True})
        self.assertEqual(status, 400)

    def test_over_cap_400(self):
        status, _, _ = self.sh(
            {"command": "echo hi", "max_stdout": 200 * 1024 * 1024})
        self.assertEqual(status, 400)

    def test_float_tag_for_int_field_400(self):
        status, _, _ = self.sh(
            {"command": "echo hi", "max_stdout": tf(10.0)})
        self.assertEqual(status, 400)

    def test_float_timeout_accepted(self):
        status, _, data = self.sh(
            {"command": "sleep 30", "timeout": tf(1.0)})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["timed_out"]), True)

    def test_bool_timeout_400(self):
        status, _, _ = self.sh({"command": "echo hi", "timeout": "b:true"})
        self.assertEqual(status, 400)


class TestShellStdin(unittest.TestCase):
    def setUp(self):
        self.ws = workspace("shell_stdin")

    def sh(self, obj):
        return json_request("POST", "/api/shell", obj)

    def test_text_stdin_to_cat(self):
        status, _, data = self.sh(
            {"command": "cat", "stdin": "piped hello\n"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 0)
        self.assertEqual(data["stdout"], "piped hello\n")
        self.assertEqual(tv(data["stdin_bytes"]), 12)

    def test_stdin_through_pipeline(self):
        status, _, data = self.sh(
            {"command": "tr a-z A-Z | sort -r",
             "stdin": "pear\napple\n"})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "PEAR\nAPPLE\n")

    def test_stdin_sort_example(self):
        # the user's use case: feed data as if from a previous pipe stage
        status, _, data = self.sh(
            {"command": "sort", "stdin": "c\nb\na\n"})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "a\nb\nc\n")

    def test_binary_stdin_base64(self):
        payload = base64.b64encode(bytes([0, 1, 2, 255, 254])).decode("ascii")
        status, _, data = self.sh(
            {"command": "cat", "stdin": payload,
             "stdin_encoding": "base64"})
        self.assertEqual(status, 200)
        self.assertEqual(tv(data["returncode"]), 0)
        self.assertEqual(tv(data["stdin_bytes"]), 5)
        # read back through cat in binary-safe forced-base64 file roundtrip
        status, _, data = self.sh(
            {"command": "cat | od -An -tu1 | tr -s ' '",
             "stdin": payload, "stdin_encoding": "base64"})
        self.assertEqual(status, 200)
        self.assertIn("0 1 2 255 254", data["stdout"])

    def test_stdin_empty_by_default(self):
        status, _, data = self.sh({"command": "cat"})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "")
        self.assertEqual(tv(data["stdin_bytes"]), 0)

    def test_stdin_ignored_by_non_reader(self):
        status, _, data = self.sh(
            {"command": "echo hi", "stdin": "unread input"})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], "hi\n")
        self.assertEqual(tv(data["stdin_bytes"]), 12)

    def test_large_stdin_no_deadlock(self):
        big = "x" * (256 * 1024) + "\n"
        status, _, data = self.sh({"command": "cat", "stdin": big})
        self.assertEqual(status, 200)
        self.assertEqual(data["stdout"], big)
        self.assertEqual(tv(data["stdin_bytes"]), 256 * 1024 + 1)

    def test_stdin_bad_encoding_400(self):
        status, _, _ = self.sh(
            {"command": "cat", "stdin": "x", "stdin_encoding": "rot13"})
        self.assertEqual(status, 400)

    def test_stdin_bad_base64_400(self):
        status, _, _ = self.sh(
            {"command": "cat", "stdin": "!!!", "stdin_encoding": "base64"})
        self.assertEqual(status, 400)

    def test_stdin_non_string_400(self):
        status, _, _ = self.sh({"command": "cat", "stdin": "i:5"})
        self.assertEqual(status, 200)  # tagged-looking text is just text
        status, _, _ = self.sh({"command": "cat", "stdin": 5})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
