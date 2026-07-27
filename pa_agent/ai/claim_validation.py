"""模型声明与真实 K 线、真实品种精度之间的硬校验。

本模块只拒绝，不修正模型输出。价格范围以已收盘 K 线的 OHLC 包络为
基准，外扩 ATR14 的可配置倍数；价格精度只认 ``KlineFrame.price_tick``
中由行情源声明的真实最小跳动。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

StageName = Literal["stage1", "stage2"]

CLAIM_VALIDATION_PREFIX = "claim_validation:"
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_K_REF_RE = re.compile(r"(?<![A-Za-z0-9_])K\s*(\d+)", re.IGNORECASE)
_LEVEL_NUMBER = r"(?:0|[1-9]\d*)(?:\.\d+)?"
_LEVEL_RE = re.compile(
    rf"^\s*({_LEVEL_NUMBER})(?:\s*(?:-|–|—|~|～|至)\s*({_LEVEL_NUMBER}))?\s*$"
)
_ORDER_PRICE_FIELDS = (
    "entry_price",
    "stop_loss_price",
    "take_profit_price",
    "take_profit_price_2",
)
_ISSUE_CODE_PRIORITY = {
    "ohlc_unavailable": 0,
    "atr_unavailable": 1,
    "price_tick_unavailable": 2,
    "bar_reference_invalid": 3,
    "bar_reference_out_of_range": 4,
    "price_not_numeric": 5,
    "price_out_of_range": 6,
    "price_tick_misaligned": 7,
}


@dataclass(frozen=True)
class ClaimValidationIssue:
    """一条带稳定机器码的声明校验失败。"""

    code: str
    path: str
    message: str

    def as_invalid_field(self) -> str:
        return f"{CLAIM_VALIDATION_PREFIX}{self.code}:{self.path}:{self.message}"


def extract_claim_validation_code(invalid_fields: Iterable[object]) -> str | None:
    """从 JsonValidator 的 invalid_fields 中提取首个稳定声明错误码。"""

    for raw in invalid_fields:
        text = str(raw or "")
        if not text.startswith(CLAIM_VALIDATION_PREFIX):
            continue
        remainder = text[len(CLAIM_VALIDATION_PREFIX) :]
        code = remainder.split(":", 1)[0].strip()
        if _CODE_RE.fullmatch(code):
            return code
    return None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        candidate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return candidate if candidate.is_finite() else None


def _closed_bars(frame: Any) -> tuple[Any, ...]:
    bars = getattr(frame, "bars", ()) if frame is not None else ()
    return tuple(
        bar for bar in (bars or ()) if bool(getattr(bar, "closed", True))
    )


def _frame_envelope(
    frame: Any,
) -> tuple[Decimal, Decimal] | None:
    lows: list[Decimal] = []
    highs: list[Decimal] = []
    for bar in _closed_bars(frame):
        low = _decimal(getattr(bar, "low", None))
        high = _decimal(getattr(bar, "high", None))
        if low is None or high is None or low <= 0 or high < low:
            return None
        lows.append(low)
        highs.append(high)
    if not lows:
        return None
    return min(lows), max(highs)


def _frame_atr14(frame: Any) -> Decimal | None:
    indicators = getattr(frame, "indicators", None)
    values = getattr(indicators, "atr14", ()) if indicators is not None else ()
    if not values:
        return None
    value = _decimal(values[0])
    return value if value is not None and value > 0 else None


def declared_price_tick(frame: Any) -> Decimal | None:
    """返回行情源声明的真实 tick；不做任何 OHLC 小数位推断。"""

    tick = _decimal(getattr(frame, "price_tick", None))
    return tick if tick is not None and tick > 0 else None


def _stage1_price_claims(
    obj: dict[str, Any],
) -> tuple[list[tuple[str, Decimal]], list[ClaimValidationIssue]]:
    claims: list[tuple[str, Decimal]] = []
    issues: list[ClaimValidationIssue] = []
    for field, field_path, levels, _parent in _walk_items(obj):
        if field not in {"support_levels", "resistance_levels"}:
            continue
        if not isinstance(levels, list):
            issues.append(
                ClaimValidationIssue(
                    "price_not_numeric",
                    field_path,
                    "支撑阻力价位必须是列表",
                )
            )
            continue
        for index, raw in enumerate(levels):
            path = f"{field_path}[{index}]"
            if not isinstance(raw, str):
                value = _decimal(raw)
                if value is not None:
                    claims.append((path, value))
                    continue
                issues.append(
                    ClaimValidationIssue(
                        "price_not_numeric",
                        path,
                        "价位不是有限数值",
                    )
                )
                continue
            match = _LEVEL_RE.fullmatch(raw)
            if match is None:
                issues.append(
                    ClaimValidationIssue(
                        "price_not_numeric",
                        path,
                        "价位必须是纯价格或价格区间字符串",
                    )
                )
                continue
            for token in match.groups():
                if token is None:
                    continue
                value = _decimal(token)
                if value is None:
                    issues.append(
                        ClaimValidationIssue(
                            "price_not_numeric",
                            path,
                            "价位不是有限数值",
                        )
                    )
                    continue
                claims.append((path, value))
    return claims, issues


def _stage2_price_claims(
    obj: dict[str, Any],
) -> tuple[list[tuple[str, Decimal]], list[ClaimValidationIssue]]:
    claims: list[tuple[str, Decimal]] = []
    issues: list[ClaimValidationIssue] = []
    # Schema 允许额外属性，且 Stage2 normalizer 支持根级扁平 decision。
    # 因此必须递归验证所有同名价位字段；只看直属 decision 会让重复根级
    # 字段或 trade_plan.entry_price 之类 wrapper 在归一化时被静默丢弃。
    for field, path, raw, _parent in _walk_items(obj):
        if field not in _ORDER_PRICE_FIELDS or raw is None:
            continue
        value = _decimal(raw)
        if value is None:
            issues.append(
                ClaimValidationIssue(
                    "price_not_numeric",
                    path,
                    "价位不是有限数值",
                )
            )
            continue
        claims.append((path, value))
    return claims, issues


def _price_issues(
    stage: StageName,
    obj: dict[str, Any],
    frame: Any,
    *,
    atr_tolerance_multiple: float,
) -> list[ClaimValidationIssue]:
    if stage == "stage1":
        claims, issues = _stage1_price_claims(obj)
    else:
        claims, issues = _stage2_price_claims(obj)
    if not claims:
        return issues

    envelope = _frame_envelope(frame)
    if envelope is None:
        issues.append(
            ClaimValidationIssue(
                "ohlc_unavailable",
                "frame.bars",
                "没有可用于价格声明校验的完整已收盘 OHLC",
            )
        )
    atr = _frame_atr14(frame)
    if atr is None:
        issues.append(
            ClaimValidationIssue(
                "atr_unavailable",
                "frame.indicators.atr14",
                "没有有效 ATR14，无法计算声明价位容差",
            )
        )

    multiple = _decimal(atr_tolerance_multiple)
    if multiple is None or multiple < 0:
        raise ValueError("atr_tolerance_multiple 必须是非负有限数值")

    if envelope is not None and atr is not None:
        lower = envelope[0] - atr * multiple
        upper = envelope[1] + atr * multiple
        for path, price in claims:
            if price < lower or price > upper:
                issues.append(
                    ClaimValidationIssue(
                        "price_out_of_range",
                        path,
                        (
                            f"{price} 不在真实 OHLC+ATR 容差 "
                            f"[{lower}, {upper}] 内"
                        ),
                    )
                )

    tick = declared_price_tick(frame)
    if tick is None:
        issues.append(
            ClaimValidationIssue(
                "price_tick_unavailable",
                "frame.price_tick",
                "行情源没有声明真实品种 tick，禁止猜测价格精度",
            )
        )
        return issues

    for path, price in claims:
        try:
            aligned = price.remainder_near(tick) == 0
        except InvalidOperation:
            aligned = False
        if not aligned:
            issues.append(
                ClaimValidationIssue(
                    "price_tick_misaligned",
                    path,
                    f"{price} 不是真实 tick={tick} 的整数倍",
                )
            )
    return issues


def _walk_items(
    value: Any,
    path: str = "",
) -> Iterable[tuple[str, str, Any, dict[str, Any]]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield str(key), child_path, child, value
            yield from _walk_items(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield from _walk_items(child, child_path)


def _walk_scalar_values(
    value: Any,
    path: str = "",
) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _walk_scalar_values(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield from _walk_scalar_values(child, child_path)
    else:
        yield path, value


def _bar_reference_issues(
    obj: dict[str, Any],
    frame: Any,
) -> list[ClaimValidationIssue]:
    closed = _closed_bars(frame)
    actual_seqs = {
        int(getattr(bar, "seq", 0) or 0)
        for bar in closed
        if int(getattr(bar, "seq", 0) or 0) >= 1
    }
    actual_max = max(actual_seqs, default=0)
    issues: list[ClaimValidationIssue] = []
    if actual_max < 1:
        return [
            ClaimValidationIssue(
                "bar_reference_out_of_range",
                "frame.bars",
                "当前帧没有可引用的已收盘 K 线",
            )
        ]

    def append_out_of_range(path: str, seq: int) -> None:
        if seq in actual_seqs:
            return
        issues.append(
            ClaimValidationIssue(
                "bar_reference_out_of_range",
                path,
                f"K{seq} 不在当前帧真实已收盘 K 线集合 K1-K{actual_max} 内",
            )
        )

    for key, path, raw, parent in _walk_items(obj):
        if key in {"gate_trace", "decision_trace"} and isinstance(raw, list):
            for index, item in enumerate(raw):
                if isinstance(item, dict) and "bar_range" not in item:
                    issues.append(
                        ClaimValidationIssue(
                            "bar_reference_invalid",
                            f"{path}[{index}].bar_range",
                            "bar_range 缺失，必须明确引用当前帧的 K 序号",
                        )
                    )
            continue
        if key in {"bar_from", "bar_to"}:
            if isinstance(raw, bool) or not isinstance(raw, int):
                issues.append(
                    ClaimValidationIssue(
                        "bar_reference_invalid",
                        path,
                        f"{key} 必须是实际 K 线序号",
                    )
                )
                refs = []
            else:
                refs = [raw]
        elif key == "new_closed_bars":
            if not isinstance(raw, list):
                issues.append(
                    ClaimValidationIssue(
                        "bar_reference_invalid",
                        path,
                        "new_closed_bars 必须是明确引用 K 序号的列表",
                    )
                )
                continue
            refs = []
            for index, item in enumerate(raw):
                matches = [int(value) for value in _K_REF_RE.findall(str(item))]
                if not matches:
                    issues.append(
                        ClaimValidationIssue(
                            "bar_reference_invalid",
                            f"{path}[{index}]",
                            "new_closed_bars 每项必须明确引用 K 序号",
                        )
                    )
                for seq in matches:
                    append_out_of_range(f"{path}[{index}]", seq)
        elif key == "bar_range":
            skipped = bool(parent.get("skipped"))
            answer = str(parent.get("answer") or "").strip()
            refs = [int(value) for value in _K_REF_RE.findall(str(raw or ""))]
            if not refs and not (
                skipped
                and answer in {"", "不适用"}
                and str(raw or "").strip() in {"", "不适用", "—"}
            ):
                issues.append(
                    ClaimValidationIssue(
                        "bar_reference_invalid",
                        path,
                        "bar_range 必须明确引用当前帧的 K 序号",
                    )
                )
        elif key == "entry_basis_bar":
            if raw is None:
                continue
            refs = [int(value) for value in _K_REF_RE.findall(str(raw))]
            if not refs:
                issues.append(
                    ClaimValidationIssue(
                        "bar_reference_invalid",
                        path,
                        "entry_basis_bar 必须明确引用 K 序号",
                    )
                )
        else:
            continue

        for seq in refs:
            append_out_of_range(path, seq)

    # bar_range 之外的理由、摘要、规则文字也不能引用不存在的 K 线。
    # 这一步只验证“引用是否真实存在”，不替模型补写或扩大 bar_range。
    for path, raw in _walk_scalar_values(obj):
        if not isinstance(raw, str):
            continue
        for token in _K_REF_RE.findall(raw):
            append_out_of_range(path, int(token))
    return issues


def validate_claims(
    stage: StageName,
    obj: dict[str, Any],
    frame: Any,
    *,
    atr_tolerance_multiple: float = 1.0,
) -> list[ClaimValidationIssue]:
    """校验一份阶段 JSON 中的价格和 K 线引用，不改写输入对象。"""

    if not isinstance(obj, dict) or frame is None:
        return []
    issues = _bar_reference_issues(obj, frame)
    issues.extend(
        _price_issues(
            stage,
            obj,
            frame,
            atr_tolerance_multiple=atr_tolerance_multiple,
        )
    )

    # 原始与归一化对象会各跑一次；这里先在单次调用内稳定去重，
    # 再按显式优先级排序，避免 JSON 键顺序改变耐久错误码。
    seen: set[tuple[str, str, str]] = set()
    unique: list[ClaimValidationIssue] = []
    for issue in issues:
        key = (issue.code, issue.path, issue.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    unique.sort(
        key=lambda issue: (
            _ISSUE_CODE_PRIORITY.get(issue.code, 999),
            issue.code,
            issue.path,
            issue.message,
        )
    )
    return unique
