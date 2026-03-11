"""PR-Injector exception hierarchy."""


class PRInjectorError(Exception):
    """Base exception for all PR-Injector errors."""


class MinerError(PRInjectorError):
    """Errors during PR discovery and filtering."""


class GitOperationError(PRInjectorError):
    """Git clone, revert, worktree, or diff failures."""


class RevertFailed(PRInjectorError):
    """Level 1 git revert failed (conflicts, missing commits)."""


class ASTMatchFailed(PRInjectorError):
    """Level 2 AST surgery could not locate target nodes."""


class ASTSurgeryFailed(PRInjectorError):
    """Level 2 replacement produced invalid code."""


class SemanticInjectionFailed(PRInjectorError):
    """Level 3 LLM could not generate a valid injection."""


class ArchitectureDeprecated(PRInjectorError):
    """Level 4: feature/dependency no longer exists."""


class BlastRadiusExceeded(PRInjectorError):
    """Injection caused too many unrelated test failures."""


class VerificationFailed(PRInjectorError):
    """Target tests did not fail after injection."""


class TestTimeoutError(PRInjectorError):
    """Test suite exceeded the configured timeout."""
