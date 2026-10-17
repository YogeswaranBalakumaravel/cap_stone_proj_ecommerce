"""Throwaway tests for the gate findings commenter's dummy PR. Never merged."""

from app.gate_demo import shout


def test_run_report_works():
    assert True


def test_shout_upper_cases():
    assert shout("demo") == "demo"
