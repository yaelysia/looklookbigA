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


def main():
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
