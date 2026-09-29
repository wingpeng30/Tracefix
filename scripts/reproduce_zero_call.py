"""Compatibility wrapper for the installed zero-call reproduction command."""

from tracefix.reproduction import main

if __name__ == "__main__":
    raise SystemExit(main())
