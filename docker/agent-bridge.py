"""Compatibility entry point for the packaged TraceFix Docker bridge."""

from tracefix.agent_bridge import main


if __name__ == "__main__":
    raise SystemExit(main())
