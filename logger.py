#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
logger.py - Central logging system for PyPort

Features:
 - Unified logger for all modules (pyport.*)
 - Colored console output (via colorama, fallback to plain text)
 - Log rotation (pyport.log capped at ~5MB, keep 5 files)
 - Per-module logs (logs/<module>.log)
 - JSON mode optional (structured logs)
 - Easy use: from pyport.logger import get_logger
"""

import logging
import logging.handlers
import os
import sys
import json
import time
from pathlib import Path
from typing import Optional

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init()
    _COLORAMA = True
except ImportError:
    _COLORAMA = False

LOG_DIR = Path("/pyport/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_LOG_FILE = LOG_DIR / "pyport.log"

# ---------- Custom Formatter -------------------------------------------------

class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: Fore.BLUE if _COLORAMA else "",
        logging.INFO: Fore.GREEN if _COLORAMA else "",
        logging.WARNING: Fore.YELLOW if _COLORAMA else "",
        logging.ERROR: Fore.RED if _COLORAMA else "",
        logging.CRITICAL: Fore.MAGENTA if _COLORAMA else "",
    }

    RESET = Style.RESET_ALL if _COLORAMA else ""

    def format(self, record):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        level_color = self.COLORS.get(record.levelno, "")
        prefix = f"[{ts}] {level_color}{record.levelname:<8}{self.RESET} [{record.name}]"
        msg = super().format(record)
        return f"{prefix} {msg}"

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno
        })

# ---------- Logger Factory ---------------------------------------------------

def get_logger(name: str,
               level: int = logging.INFO,
               json_mode: bool = False,
               per_module_file: bool = True) -> logging.Logger:
    """
    Get a configured logger instance.
    - name: usually __name__ of the module
    - level: logging level
    - json_mode: if True, log in JSON instead of color/text
    - per_module_file: if True, also log to logs/<module>.log
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(level)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    if json_mode:
        ch.setFormatter(JsonFormatter())
    else:
        ch.setFormatter(ColorFormatter("%(message)s"))
    logger.addHandler(ch)

    # Rotating file handler (main log)
    fh = logging.handlers.RotatingFileHandler(DEFAULT_LOG_FILE, maxBytes=5_000_000, backupCount=5)
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(fh)

    # Per-module log file
    if per_module_file:
        safe_name = name.replace(".", "_")
        mod_file = LOG_DIR / f"{safe_name}.log"
        mh = logging.handlers.RotatingFileHandler(mod_file, maxBytes=2_000_000, backupCount=3)
        mh.setLevel(level)
        mh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logger.addHandler(mh)

    logger.propagate = False
    return logger

# ---------- Example CLI ------------------------------------------------------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Test PyPort logger")
    parser.add_argument("--json", action="store_true", help="Enable JSON mode")
    parser.add_argument("--level", default="INFO", choices=["DEBUG","INFO","WARNING","ERROR","CRITICAL"])
    args = parser.parse_args()

    log = get_logger("pyport.logger", level=getattr(logging, args.level), json_mode=args.json)
    log.debug("This is a debug message")
    log.info("This is an info message")
    log.warning("This is a warning")
    log.error("This is an error")
    log.critical("This is critical")

if __name__ == "__main__":
    _cli()
