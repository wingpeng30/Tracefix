"""Bounded Git name plumbing, never a diff or a workspace content reader."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import relative_path
from tracefix.security import minimal_subprocess_environment

MAX_METADATA_BYTES = 8 * 1024 * 1024


class GitMetadataPaths:
    """Union HEAD and index names, including staged and unstaged deletions.

    Commands are fixed metadata-only plumbing with optional locks and configured
    fsmonitor disabled. Reader threads retain bounded bytes but always drain;
    neither a large index nor Git diagnostics can grow the host buffer unbounded.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)

    def _run(self, *arguments: str) -> bytes:
        environment = minimal_subprocess_environment()
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
        command = ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
                   "-c", "core.hooksPath=" + os.devnull, *arguments]
        try:
            process = subprocess.Popen(command, cwd=self.root, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
        except OSError as exc:
            raise RuleError("rules_git_metadata_start_failed") from exc
        output = [bytearray(), bytearray()]
        failures: list[str] = []

        def drain(stream, index: int, cap: int) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    remaining = cap - len(output[index])
                    output[index].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        failures.append("rules_git_metadata_output_limit")
                        # One marker per reader; keep draining without retaining.
                        while stream.read(64 * 1024):
                            pass
                        return
            except OSError:
                failures.append("rules_git_metadata_read_failed")
            finally:
                stream.close()

        readers = [threading.Thread(target=drain, args=(stream, index, cap), daemon=True)
                   for index, (stream, cap) in enumerate(
                       ((process.stdout, MAX_METADATA_BYTES), (process.stderr, 16 * 1024)))]
        try:
            for reader in readers:
                reader.start()
            try:
                code = process.wait(timeout=10)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait(timeout=2)
                raise RuleError("rules_git_metadata_deadline") from exc
            if code != 0:
                raise RuleError("rules_git_metadata_failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            for reader in readers:
                reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers):
            raise RuleError("rules_git_metadata_drain_unknown")
        if failures:
            raise RuleError(failures[0])
        return bytes(output[0])

    @staticmethod
    def _names(data: bytes) -> tuple[str, ...]:
        if not data:
            return ()
        if not data.endswith(b"\0"):
            raise RuleError("rules_git_metadata_invalid_framing")
        entries = data[:-1].split(b"\0")
        if len(entries) > 5000:
            raise RuleError("rules_access_path_limit")
        try:
            return tuple(relative_path(entry.decode("utf-8", errors="strict")) for entry in entries)
        except UnicodeError as exc:
            raise RuleError("rules_git_metadata_invalid_encoding") from exc

    def __call__(self) -> tuple[str, ...]:
        # Avoid silently discovering an ancestor repository from a non-repository
        # task directory. Do not interpret absence or a failed command as no files.
        raw = self._run("rev-parse", "--show-toplevel")
        try:
            actual = Path(raw.decode("utf-8", errors="strict").rstrip("\r\n"))
            if actual.resolve(strict=True) != self.root:
                raise RuleError("rules_git_metadata_wrong_checkout")
        except (OSError, UnicodeError) as exc:
            raise RuleError("rules_git_metadata_invalid_root") from exc
        index = self._names(self._run("ls-files", "--cached", "-z"))
        head = self._names(self._run("ls-tree", "-r", "--name-only", "-z", "HEAD"))
        result = tuple(sorted(set(index) | set(head)))
        if len(result) > 5000:
            raise RuleError("rules_access_path_limit")
        return result
