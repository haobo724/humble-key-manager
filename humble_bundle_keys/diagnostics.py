"""Per-task diagnostics. Never include payloads, private URLs or traceback locals."""
import hashlib
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from uuid import uuid4

_context = ContextVar("hb_diagnostic_context", default=None)
_sink = ContextVar("hb_diagnostic_sink", default=None)


def fields():
    return dict(_context.get() or {})


def set_context(**values):
    _context.set({**fields(), **values})


def order_ref(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:10] if value else ""


def error_fields(exc, **overrides):
    return {**getattr(exc, "_hb_context", fields()), "error_type": type(exc).__name__,
            "locations": [{"file": Path(frame.filename).name, "function": frame.name,
                           "line": frame.lineno}
                          for frame in traceback.extract_tb(exc.__traceback__)], **overrides}


@contextmanager
def scope(**values):
    token = _context.set({**fields(), **values})
    try:
        yield
    except Exception as exc:
        if not hasattr(exc, "_hb_context"):
            exc._hb_context = fields()
        raise
    finally:
        _context.reset(token)


def emit_failure(event, exc, **values):
    sink = _sink.get()
    if sink:
        sink(event, **{**error_fields(exc), **values})


def operation(function):
    @wraps(function)
    def wrapped(self, action):
        token = _sink.set(self.log)
        try:
            with scope(action=action, operation_id=uuid4().hex[:12],
                       phase="准备操作", bundle="", game="", order_ref=""):
                return function(self, action)
        finally:
            _sink.reset(token)
    return wrapped


def extracting(function):
    @wraps(function)
    def wrapped(tpk, order):
        product = order.get("product") or {}
        with scope(phase="解析订单 Key", order_ref=order_ref(order.get("gamekey")),
                   bundle=product.get("human_name") or product.get("machine_name") or "",
                   game=tpk.get("human_name") or tpk.get("display_name") or
                   tpk.get("machine_name") or ""):
            return function(tpk, order)
    return wrapped
