"""Tests for the greeting module."""

from greeting import greet


def test_ac_001_greet_returns_correct_string():
    """AC-001: greet("Ada") returns "Hello, Ada!"."""
    assert greet("Ada") == "Hello, Ada!"
