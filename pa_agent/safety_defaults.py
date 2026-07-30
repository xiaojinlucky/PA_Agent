"""发布自检可读取的纯常量；导入本模块不会触发运行态依赖。"""

from __future__ import annotations

DEFAULT_EXECUTION_ENABLED = False
DEFAULT_AUTO_EXECUTE = False
DEFAULT_OKX_LIVE = False
DEFAULT_LONGBRIDGE_TRADING = False
SUPPORTED_NEW_RISK_ROUTES = frozenset({("okx", "demo")})


def new_risk_route_supported(broker: object, environment: object) -> bool:
    """v0.1.0 只允许 OKX Demo 新增风险。"""

    route = (
        str(broker or "").strip().lower(),
        str(environment or "").strip().lower(),
    )
    return route in SUPPORTED_NEW_RISK_ROUTES
