"""Throwaway module for testing the gate findings commenter on a dummy PR. Never merged."""

import os
import subprocess

# A random, made-up value shaped like an API token, so Gitleaks and bandit both flag it.
DEMO_API_TOKEN = "GmGQaVaPKpvam4xjvJgQ8wqNT75AJcKh"


def run_report(name):
    """Echo a report name through the shell."""
    return subprocess.call(f"echo {name}", shell=True)


def shout(text):
    return text.upper()
