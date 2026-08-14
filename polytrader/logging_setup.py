"""日志与通用工具。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT_LOGGER = logging.getLogger("polytrader")


def setup_logging(level: str = "INFO", log_file: str = "logs/polytrader.log") -> logging.Logger:
    root = _ROOT_LOGGER
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")

    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
               for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            if not any(isinstance(h, logging.FileHandler) and h.baseFilename ==
                       str(Path(log_file).resolve()) for h in root.handlers):
                fh = logging.FileHandler(log_file, encoding="utf-8")
                fh.setFormatter(fmt)
                root.addHandler(fh)
        except OSError:
            pass
    return root


def get_logger(name: str) -> logging.Logger:
    """取 polytrader.* logger；入口未显式 setup_logging 时懒初始化，
    保证任何模块的 log.info/warning 都有输出（幂等，不重复加 handler）。"""
    if not _ROOT_LOGGER.handlers:
        setup_logging()
    return logging.getLogger(f"polytrader.{name}")
