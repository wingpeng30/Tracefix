"""Trace event contracts."""

from tracefix.tracing.base import TraceEvent, TraceEventType, TraceSink
from tracefix.tracing.jsonl import JSONLTraceSink

__all__ = ["JSONLTraceSink", "TraceEvent", "TraceEventType", "TraceSink"]
