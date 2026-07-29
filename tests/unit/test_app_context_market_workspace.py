from __future__ import annotations

import ast
import inspect
import textwrap

from pa_agent.app_context import AppContext, build_market_workspace_controller
from pa_agent.config.settings import Settings
from pa_agent.data.market_workspace_controller import MarketWorkspaceController


def test_app_context_exposes_independent_market_workspace_controller() -> None:
    settings = Settings()
    controller = build_market_workspace_controller(settings)
    context = AppContext(
        settings=settings,
        data_source=object(),
        execution_service=object(),
        market_workspace_controller=controller,
    )

    assert isinstance(context.market_workspace_controller, MarketWorkspaceController)
    assert context.market_workspace_controller is not context.execution_service
    assert context.market_workspace_controller.settings_snapshot.general == settings.general


def test_bootstrap_returns_controller_without_passing_it_to_legacy_read_model() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(AppContext.bootstrap)))
    cls_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cls"
    ]
    assert len(cls_calls) == 1
    assert {
        keyword.arg for keyword in cls_calls[0].keywords
    } >= {"market_workspace_controller"}

    legacy_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "WorkbenchReadModel"
    ]
    assert len(legacy_calls) == 2
    assert all(
        "market_workspace_controller"
        not in {keyword.arg for keyword in call.keywords}
        for call in legacy_calls
    )
