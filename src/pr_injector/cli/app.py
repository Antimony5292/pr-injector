"""Typer CLI application factory."""

from __future__ import annotations

import typer

from pr_injector.cli.mine_cmd import mine
from pr_injector.cli.run_cmd import run

app = typer.Typer(
    name="pr-injector",
    help="PR-Injector: Dynamic Bug Injection via Historical PR Reversion",
    add_completion=False,
    rich_markup_mode="rich",
)

app.command()(run)
app.command()(mine)
