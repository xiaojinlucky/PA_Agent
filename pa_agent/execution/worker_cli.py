"""Execution Worker 命令入口；信息参数不会导入运行 Worker。"""

from __future__ import annotations

import sys

from pa_agent.release_contract import handle_offline_info_args


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    info_result = handle_offline_info_args(
        args,
        program="pa-execution-worker",
    )
    if info_result is not None:
        return info_result
    from pa_agent.execution.worker import main as worker_main

    return worker_main()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
