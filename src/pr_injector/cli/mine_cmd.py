"""CLI command: mine - Batch PR mining and injection."""

from __future__ import annotations

import asyncio
from datetime import datetime

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pr_injector.core.config import get_settings
from pr_injector.core.logging import setup_logging

console = Console()


def mine(
    repo: str = typer.Argument(
        ..., help="Repository in 'owner/name' format (e.g., psf/requests)"
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only consider PRs merged after this date (YYYY-MM-DD)",
    ),
    require_tests: bool = typer.Option(
        True,
        "--require-tests/--no-require-tests",
        help="Only include PRs that modify test files",
    ),
    verify_blast_radius: bool = typer.Option(
        True,
        "--verify-blast-radius/--no-verify-blast-radius",
        help="Run test verification and blast radius control",
    ),
    max_candidates: int = typer.Option(
        100,
        "--max-candidates", "-n",
        help="Maximum number of PR candidates to process",
    ),
    max_workers: int = typer.Option(
        4,
        "--workers", "-w",
        help="Number of parallel workers",
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir", "-o",
        help="Output directory for benchmark JSONL",
    ),
) -> None:
    """Mine historical PRs and build a benchmark dataset.

    Discovers merged PRs from the repository, filters candidates,
    and attempts injection at multiple levels to produce a
    SWE-bench compatible benchmark dataset.
    """
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    # Parse since date
    since_date: datetime | None = None
    if since:
        try:
            since_date = datetime.fromisoformat(since)
        except ValueError:
            console.print(f"[red]Invalid date format: {since}. Use YYYY-MM-DD.[/red]")
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
            f"[bold]Since:[/bold] {since or 'all time'}\n"
            f"[bold]Require Tests:[/bold] {require_tests}\n"
            f"[bold]Verify Blast Radius:[/bold] {verify_blast_radius}\n"
            f"[bold]Max Candidates:[/bold] {max_candidates}\n"
            f"[bold]Workers:[/bold] {max_workers}\n"
            f"[bold]Output:[/bold] {settings.output_dir}",
            title="PR-Injector: Batch Mining",
            border_style="blue",
        )
    )

    stats = asyncio.run(
        _run_mining(
            settings,
            repo,
            since_date,
            require_tests,
            verify_blast_radius,
            max_candidates,
            max_workers,
        )
    )

    # Display summary
    table = Table(title="Mining Summary", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", justify="right")

    for key, value in stats.items():
        table.add_row(key.replace("_", " ").title(), str(value))

    console.print(table)
    console.print(f"\n[green]Output written to: {settings.output_dir}[/green]")


async def _run_mining(
    settings,
    repo: str,
    since: datetime | None,
    require_tests: bool,
    verify_blast_radius: bool,
    max_candidates: int,
    max_workers: int,
) -> dict:
    """Run the batch mining pipeline."""
    from pr_injector.ast_engine.engine import ASTEngine
    from pr_injector.core.git_ops import GitWorkspace
    from pr_injector.llm.client import LLMClient
    from pr_injector.output.writer import JSONLWriter
    from pr_injector.pipeline.miner import PRMiner
    from pr_injector.pipeline.orchestrator import PipelineOrchestrator
    from pr_injector.pipeline.resolver import PRResolver
    from pr_injector.pipeline.reverter import PRReverter
    from pr_injector.pipeline.verifier import TestVerifier

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

    instance_count = 0
    async for instance in orchestrator.run_batch(
        repo=repo,
        since=since,
        require_tests=require_tests,
        verify_blast_radius=verify_blast_radius,
        max_candidates=max_candidates,
        max_workers=max_workers,
    ):
        instance_count += 1
        console.print(
            f"  [green]✓[/green] {instance.instance_id} "
            f"[dim]({instance.injection_level.value})[/dim]"
        )

    stats = orchestrator.get_stats()
    stats["instances_generated"] = instance_count
    return stats
