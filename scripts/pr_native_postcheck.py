import os
import re
import shlex
import subprocess
from pathlib import Path


WORKFLOW = Path(".github/workflows/pre-merge-security-gate.yml")
PYTHON_TEST_RE = re.compile(r"^\s*run:\s+(python3 scripts/test_[A-Za-z0-9_]+\.py)\s*$")
NODE_TEST_RE = re.compile(r"^\s*run:\s+(node --test tests/[A-Za-z0-9_.-]+\.test\.mjs)\s*$")


def safety_commands(text):
    commands = []
    for line in text.splitlines():
        match = PYTHON_TEST_RE.match(line) or NODE_TEST_RE.match(line)
        if match:
            command = match.group(1)
            if command not in commands:
                commands.append(command)
    return commands


def verify_merge_identity():
    expected_base = os.environ.get("EXPECTED_BASE_SHA", "").strip()
    expected_head = os.environ.get("EXPECTED_HEAD_SHA", "").strip()
    if not expected_base and not expected_head:
        return
    assert len(expected_base) == 40 and len(expected_head) == 40
    observed = subprocess.run(
        ["git", "show", "-s", "--format=%P", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().split()
    assert observed == [expected_base, expected_head], (observed, expected_base, expected_head)
    print(
        f"NATIVE_MERGE_IDENTITY base={expected_base} head={expected_head}",
        flush=True,
    )


def main():
    verify_merge_identity()
    text = WORKFLOW.read_text(encoding="utf-8")
    commands = safety_commands(text)
    assert commands, "no pre-merge safety commands discovered"
    assert "python3 scripts/test_workflow_security.py" in commands
    assert "python3 scripts/test_pr_lifecycle_policy.py" in commands
    for command in commands:
        print(f"POSTCHECK_RUN {command}", flush=True)
        subprocess.run(shlex.split(command), check=True)
    print(f"NATIVE_MERGE_POSTCHECK safety_commands={len(commands)}", flush=True)


if __name__ == "__main__":
    main()
