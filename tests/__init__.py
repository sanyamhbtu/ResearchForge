"""ResearchForge test suite (stdlib ``unittest`` only).

The suite is deliberately offline: every network call is replaced by a fake, so
the tests assert behaviour that is fully deterministic and reproducible. The one
module that talks to the real APIs is ``tools/benchmark.py``, which is run
manually and writes an evaluation report.
"""
