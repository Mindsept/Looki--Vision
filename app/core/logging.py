"""Prevent browser WSS tickets from appearing in application/access diagnostics."""

import logging
import re


class TicketRedactionFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if "ticket=" in message.lower():
            record.msg = re.sub(r"(?i)([?&]ticket=)[^&\s\"']+", r"\1[redacted]", message)
            record.args = ()
        return True


def configure_logging():
    logging.basicConfig(level=logging.INFO)
    for name in ("uvicorn.error", "uvicorn.access", "httpx"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, TicketRedactionFilter) for item in logger.filters):
            logger.addFilter(TicketRedactionFilter())
