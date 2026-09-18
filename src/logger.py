from __future__ import annotations

import logging
from pathlib import Path


try:
    from loguru import logger as logger  # type: ignore
except Exception:
    class _CompatLogger:
        def __init__(self) -> None:
            self._logger = logging.getLogger("competitor_monitor")

        def _format(self, message: str, *args: object) -> str:
            try:
                return message.format(*args)
            except Exception:
                return message

        def info(self, message: str, *args: object) -> None:
            self._logger.info(self._format(message, *args))

        def warning(self, message: str, *args: object) -> None:
            self._logger.warning(self._format(message, *args))

        def debug(self, message: str, *args: object) -> None:
            self._logger.debug(self._format(message, *args))

        def exception(self, message: str, *args: object) -> None:
            self._logger.exception(self._format(message, *args))

        def remove(self) -> None:
            for handler in list(self._logger.handlers):
                self._logger.removeHandler(handler)

        def add(self, sink, rotation: str | None = None, retention: int | None = None, encoding: str | None = None, level: str = "INFO", **_: object) -> None:
            self._logger.setLevel(getattr(logging, level.upper(), logging.INFO))
            formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
            if callable(sink):
                handler = logging.StreamHandler()
            else:
                Path(sink).parent.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(sink, encoding=encoding or "utf-8")
            handler.setFormatter(formatter)
            handler.setLevel(getattr(logging, level.upper(), logging.INFO))
            self._logger.addHandler(handler)

    logger = _CompatLogger()
