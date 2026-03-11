"""CLI command: run - Single PR injection."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pr_injector.core.config import get_settings
from pr_injector.core.logging import setup_logging
from pr_injector.core.models import InjectionStrategy

console = Console()


def run(
    repo: str = typer.Argument(
        ..., help="Repository in 'owner/name' format (e.g., pallets/flask)"
    ),
    pr: int = typer.Argument(..., help="PR number to inject"),
    strategy: str = typer.Option(
        "auto",
        "--strategy", "-s",
        help="Injection strategy: auto, git, ast, llm",
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir", "-o",
        help="Output directory for benchmark JSONL",
    ),
    verify: bool = typer.Option(
        True,
        "--verify/--no-verify",
        help="Run test verification after injection",
    ),
) -> None:
    """Inject a single historical PR into the current codebase.

    Attempts to revert the specified PR on the latest main branch,
    generating a benchmark instance with a golden patch.
    """
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    # Parse strategy
    try:
        injection_strategy = InjectionStrategy(strategy)
    except ValueError:
        console.print(f"[red]Invalid strategy: {strategy}[/red]")
        console.print("Valid strategies: auto, git, ast, llm")
        raise typer.Exit(code=1) from None

    if output_dir:
        settings.output_dir = output_dir

    if not settings.github_token:
        console.print("[red]Error: PRI_GITHUB_TOKEN environment variable is required[/red]")
        console.print("Set it in your .env file or environment.")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            f"[bold]Repository:[/bold] {repo}\n"
            f"[bold]PR:[/bold] #{pr}\n"
            f"[bold]Strategy:[/bold] {injection_strategy.value}\n"
            f"[bold]Output:[/bold] {settings.output_dir}",
            title="PR-Injector: Single PR Injection",
            border_style="blue",
        )
    )

    # Run the pipeline
    result = asyncio.run(_run_injection(settings, repo, pr, injection_strategy))

    if result:
        table = Table(title="Injection Result", show_header=True)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Instance ID", result["instance_id"])
        table.add_row("Injection Level", result["level"])
        table.add_row("Golden Patch Size", f"{result['patch_lines']} lines")
        if result.get("verification"):
            table.add_row("Blast Radius OK", str(result["blast_radius_ok"]))
        console.print(table)
    else:
        console.print("[red]Injection failed for all strategies.[/red]")
        raise typer.Exit(code=1)


async def _run_injection(
    settings, repo: str, pr: int, strategy: InjectionStrategy
) -> dict | None:
    """Run the injection pipeline asynchronously."""
    from pr_injector.ast_engine.engine import ASTEngine
    from pr_injector.core.git_ops import GitWorkspace
    from pr_injector.llm.client import LLMClient
    from pr_injector.output.writer import JSONLWriter
    from pr_injector.pipeline.miner import PRMiner
    from pr_injector.pipeline.orchestrator import PipelineOrchestrator
    from pr_injector.pipeline.resolver import PRResolver
    from pr_injector.pipeline.reverter import PRReverter
    from pr_injector.pipeline.verifier import TestVerifier

    # Initialize components
    workspace = GitWorkspace(settings.workspace_dir)
    miner = PRMiner(
        github_token=settings.github_token,
        api_base=settings.github_api_base,
    )
    ast_engine = ASTEngine()
    reverter = PRReverter(workspace=workspace, ast_engine=ast_engine)
    llm_client = LLMClient(
        provider=settings.llm_provider,
        azure_endpoint=settings.azure_endpoint,
        azure_deployment=settings.azure_deployment,
        azure_api_version=settings.azure_api_version,
        model=settings.llm_model,
        api_key=settings.llm_api_key or None,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    resolver = PRResolver(workspace=workspace, llm_client=llm_client)
    verifier = TestVerifier(
        test_timeout=settings.test_timeout_seconds,
        blast_radius_threshold=settings.blast_radius_threshold,
    )
    writer = JSONLWriter(settings.output_dir)

    orchestrator = PipelineOrchestrator(
        miner=miner,
        reverter=reverter,
        resolver=resolver,
        verifier=verifier,
        writer=writer,
        workspace=workspace,
    )

    instance = await orchestrator.run_single(repo, pr, strategy)

    if instance:
        return {
            "instance_id": instance.instance_id,
            "level": instance.injection_level.value,
            "patch_lines": len(instance.golden_patch.split("\n")),
            "verification": instance.verification is not None,
            "blast_radius_ok": (
                instance.verification.blast_radius_ok if instance.verification else None
            ),
        }
    return None
