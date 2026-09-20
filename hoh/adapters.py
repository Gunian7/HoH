from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarnessInfo:
    name: str
    command: str
    available: bool
    version: str
    detail: str


def _run(command: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=os.environ.copy(),
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


class HermesAdapter:
    name = "hermes"

    def __init__(self, command: Path | None = None):
        self.command = command or Path("F:/hermes/hermes-agent/venv-win/Scripts/python.exe")

    def health(self) -> HarnessInfo:
        if not self.command.exists():
            return HarnessInfo(self.name, str(self.command), False, "", "executable not found")
        code, output = _run([str(self.command), "-m", "hermes_cli.main", "--version"])
        first_line = output.splitlines()[0] if output else ""
        return HarnessInfo(self.name, str(self.command), code == 0, first_line, output)

    def acp_check(self) -> tuple[bool, str]:
        if not self.command.exists():
            return False, "executable not found"
        code, output = _run([str(self.command), "-m", "hermes_cli.main", "acp", "--check"], 60)
        return code == 0, output


class DshAdapter:
    name = "dsh"

    def __init__(self, command: str = "dsh"):
        self.command = command

    def health(self) -> HarnessInfo:
        if os.name == "nt":
            command = ["cmd.exe", "/c", self.command, "--version"]
        else:
            command = [self.command, "--version"]
        code, output = _run(command)
        first_line = output.splitlines()[0] if output else ""
        return HarnessInfo(self.name, self.command, code == 0, first_line, output)


def configured_harnesses() -> list[HarnessInfo]:
    return [HermesAdapter().health(), DshAdapter().health()]
