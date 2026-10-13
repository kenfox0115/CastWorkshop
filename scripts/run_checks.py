"""Backward-compatible entry point for the canonical verifier."""
try:
    from scripts.verify import main
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from verify import main

if __name__ == "__main__":
    raise SystemExit(main())
