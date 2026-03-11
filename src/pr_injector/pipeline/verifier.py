"""Stage 4: Test execution and blast radius control."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pr_injector.core.exceptions import (
    TestTimeoutError,
)
from pr_injector.core.logging import get_logger
from pr_injector.core.models import VerificationResult

logger = get_logger(__name__)

# Common test runner commands by ecosystem
TEST_RUNNERS = {
    "python": ["python", "-m", "pytest", "-x", "--tb=short", "-q"],
    "javascript": ["npm", "test", "--"],
    "typescript": ["npm", "test", "--"],
    "go": ["go", "test", "./..."],
    "rust": ["cargo", "test"],
    "java": ["mvn", "test", "-q"],
    "ruby": ["bundle", "exec", "rspec"],
}


class TestVerifier:
    """Stage 4: Blast radius control via test execution.

    Runs the test suite on the injected codebase and verifies that:
    - Target test cases (from the original PR) FAIL
    - Unrelated test cases PASS

    This ensures the injection is precise and doesn't break unrelated
    functionality (blast radius control).
    """

    def __init__(
        self,
        test_timeout: int = 300,
        blast_radius_threshold: float = 0.1,
    ) -> None:
        self.test_timeout = test_timeout
        self.blast_radius_threshold = blast_radius_threshold

    async def verify(
        self,
        worktree_path: str,
        target_test_files: list[str],
        test_command: str | None = None,
    ) -> VerificationResult:
        """Run tests and verify blast radius.

        Args:
            worktree_path: Path to the injected worktree.
            target_test_files: Test files from the original PR.
            test_command: Custom test command. Auto-detected if None.

        Returns:
            VerificationResult with pass/fail details.

        Raises:
            TestTimeoutError: If test suite exceeds timeout.
            BlastRadiusExceeded: If too many unrelated tests fail.
            VerificationFailed: If target tests do not fail.
        """
        logger.info(
            "verification_start",
            worktree=worktree_path,
            target_tests=target_test_files,
        )

        # Detect or use provided test command
        cmd = test_command or self._detect_test_command(worktree_path)
        if not cmd:
            logger.warning("no_test_runner_detected", worktree=worktree_path)
            return VerificationResult(
                target_tests_failed=False,
                unrelated_tests_passed=False,
                blast_radius_ok=False,
                target_test_names=target_test_files,
            )

        start_time = time.monotonic()

        # Step 1: Run target tests (should FAIL)
        target_result = await self._run_tests(
            worktree_path, cmd, target_test_files
        )

        # Step 2: Run full test suite to check blast radius
        full_result = await self._run_full_tests(worktree_path, cmd)

        duration = time.monotonic() - start_time

        # Analyze results
        target_tests_failed = target_result["returncode"] != 0
        total_failures = full_result.get("failures", 0)
        total_tests = full_result.get("total", 0)

        # Calculate unrelated failures
        target_failure_count = target_result.get("failures", 0)
        unrelated_failures = max(0, total_failures - target_failure_count)

        # Blast radius check
        if total_tests > 0:
            unrelated_failure_rate = unrelated_failures / total_tests
            unrelated_tests_passed = unrelated_failure_rate <= self.blast_radius_threshold
        else:
            unrelated_tests_passed = True

        blast_radius_ok = target_tests_failed and unrelated_tests_passed

        result = VerificationResult(
            target_tests_failed=target_tests_failed,
            unrelated_tests_passed=unrelated_tests_passed,
            blast_radius_ok=blast_radius_ok,
            target_test_names=target_test_files,
            failed_test_names=full_result.get("failed_tests", []),
            total_tests_run=total_tests,
            total_failures=total_failures,
            test_duration_seconds=duration,
        )

        logger.info(
            "verification_complete",
            target_failed=target_tests_failed,
            unrelated_passed=unrelated_tests_passed,
            blast_radius_ok=blast_radius_ok,
            total_tests=total_tests,
            total_failures=total_failures,
            duration=round(duration, 2),
        )

        return result

    async def _run_tests(
        self,
        worktree_path: str,
        base_cmd: str,
        test_files: list[str],
    ) -> dict:
        """Run specific test files and return results."""
        # Build command with specific test files
        cmd_parts = base_cmd.split()
        cmd_parts.extend(test_files)

        return await self._execute_test_command(worktree_path, cmd_parts)

    async def _run_full_tests(
        self,
        worktree_path: str,
        base_cmd: str,
    ) -> dict:
        """Run the full test suite and return results."""
        cmd_parts = base_cmd.split()
        return await self._execute_test_command(worktree_path, cmd_parts)

    async def _execute_test_command(
        self,
        worktree_path: str,
        cmd: list[str],
    ) -> dict:
        """Execute a test command and parse results.

        Returns dict with: returncode, stdout, stderr, failures, total, failed_tests
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=worktree_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.test_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise TestTimeoutError(
                    f"Test execution exceeded {self.test_timeout}s timeout"
                ) from None

            stdout_text = stdout.decode(errors="replace")
            stderr_text = stderr.decode(errors="replace")

            # Parse test results
            result = self._parse_test_output(stdout_text + "\n" + stderr_text)
            result["returncode"] = proc.returncode
            result["stdout"] = stdout_text
            result["stderr"] = stderr_text

            return result

        except TestTimeoutError:
            raise
        except FileNotFoundError:
            logger.warning("test_command_not_found", cmd=cmd)
            return {"returncode": -1, "failures": 0, "total": 0, "failed_tests": []}
        except Exception as e:
            logger.error("test_execution_error", error=str(e))
            return {"returncode": -1, "failures": 0, "total": 0, "failed_tests": []}

    def _detect_test_command(self, worktree_path: str) -> str | None:
        """Auto-detect the test runner based on project files."""
        path = Path(worktree_path)

        # Python projects
        has_python_config = (
            (path / "pytest.ini").exists()
            or (path / "pyproject.toml").exists()
            or (path / "setup.py").exists()
        )
        if has_python_config:
            return "python -m pytest -x --tb=short -q"

        # Node.js projects
        if (path / "package.json").exists():
            return "npm test --"

        # Go projects
        if (path / "go.mod").exists():
            return "go test ./..."

        # Rust projects
        if (path / "Cargo.toml").exists():
            return "cargo test"

        # Ruby projects
        if (path / "Gemfile").exists():
            return "bundle exec rspec"

        # Java/Maven projects
        if (path / "pom.xml").exists():
            return "mvn test -q"

        return None

    @staticmethod
    def _parse_test_output(output: str) -> dict:
        """Parse test output to extract pass/fail counts.

        Supports pytest, jest, go test, and generic output formats.
        """
        import re

        result: dict = {"failures": 0, "total": 0, "failed_tests": []}

        # pytest format: "X passed, Y failed, Z errors"
        pytest_match = re.search(
            r"(\d+) passed(?:.*?(\d+) failed)?(?:.*?(\d+) error)?", output
        )
        if pytest_match:
            passed = int(pytest_match.group(1))
            failed = int(pytest_match.group(2) or 0)
            errors = int(pytest_match.group(3) or 0)
            result["total"] = passed + failed + errors
            result["failures"] = failed + errors

        # pytest FAILED lines
        failed_tests = re.findall(r"FAILED\s+(\S+)", output)
        if failed_tests:
            result["failed_tests"] = failed_tests

        # Generic: count lines with FAIL/ERROR
        if result["total"] == 0:
            fail_lines = re.findall(r"(?:FAIL|ERROR|FAILED)\s+(\S+)", output)
            pass_lines = re.findall(r"(?:PASS|OK|PASSED)\s+(\S+)", output)
            result["failures"] = len(fail_lines)
            result["total"] = len(fail_lines) + len(pass_lines)
            if fail_lines:
                result["failed_tests"] = fail_lines

        return result
