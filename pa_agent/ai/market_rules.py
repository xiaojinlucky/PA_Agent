"""按标的所属市场路由"带生效日期的版本化市场制度规则块"。

规则块正文放在 prompt_engineering/市场规则_*.txt，注入提示词的用户回合
（不进 system prompt，避免击穿按字节前缀命中的系统提示词缓存）。
无法归类的符号明确返回 None（表示"该市场没有规则块"，维持原有行为），
绝不猜测市场。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_MARKET_RULE_FILES: dict[str, str] = {
    "CN": "市场规则_A股.txt",
    "HK": "市场规则_港股.txt",
    "US": "市场规则_美股.txt",
    "CRYPTO": "市场规则_加密.txt",
}

# OKX 风格合约/币对：XAU-USDT-SWAP、BTC-USDT、ETH-USD-SWAP 等。
_CRYPTO_INSTRUMENT_RE = re.compile(
    r"^[A-Z0-9]+-(USDT|USDC|USD)(-SWAP|-FUTURES)?$"
)


def market_for_symbol(symbol: str) -> str | None:
    """返回规则块市场键（CN/HK/US/CRYPTO）；无法归类返回 None。"""
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if "." in text:
        suffix = text.rsplit(".", 1)[-1]
        if suffix in {"SH", "SZ"}:
            return "CN"
        if suffix in {"HK", "US"}:
            return suffix
        if suffix == "HAS":
            return "CRYPTO"
        return None
    if _CRYPTO_INSTRUMENT_RE.fullmatch(text):
        return "CRYPTO"
    return None


_MARKET_TIMEZONE_NAMES: dict[str, str] = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
    "CRYPTO": "UTC",
}

_MARKET_TIMEZONE_LABELS: dict[str, str] = {
    "CN": "北京时间",
    "HK": "香港时间",
    "US": "美东时间",
    "CRYPTO": "UTC",
}


def timezone_name_for_symbol(symbol: str) -> str | None:
    """返回符号所属市场的 IANA 时区名；未归类返回 None（沿用 UTC 墙钟）。"""
    market = market_for_symbol(symbol)
    if market is None:
        return None
    return _MARKET_TIMEZONE_NAMES[market]


def timezone_label_for_symbol(symbol: str) -> str:
    """返回给大模型看的时区标签；未归类符号维持历史 UTC 口径。"""
    market = market_for_symbol(symbol)
    if market is None:
        return "UTC"
    return _MARKET_TIMEZONE_LABELS[market]


def market_rules_block(symbol: str, *, prompt_dir: Path) -> str | None:
    """加载符号所属市场的规则块正文；市场未归类时返回 None。

    文件缺失按 Let it crash 抛错——规则块文件属于仓库资产，
    缺失说明部署损坏，不允许静默降级成"没有规则"。
    """
    market = market_for_symbol(symbol)
    if market is None:
        logger.info("符号 %s 未归类到任何市场规则块，保持无规则注入", symbol)
        return None
    filename = _MARKET_RULE_FILES[market]
    path = prompt_dir / filename
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"市场规则块为空文件：{path}")
    return content
