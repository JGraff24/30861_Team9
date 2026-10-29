"""Every check handler the tester can run, by manifest name (Phase 3 and Phase 4).

Lookups read both phase tables at call time, so a handler replaced in either table (for
example by a test) is the one that runs.
"""

from collections.abc import Callable

from tester.phase4 import checks as phase4
from tester.suite import checks as phase3
from tester.suite.checks import SuiteContext

if set(phase3.CHECKS) & set(phase4.CHECKS):
    raise RuntimeError("Phase 3 and Phase 4 check names must not collide")


def names() -> set[str]:
    return set(phase3.CHECKS) | set(phase4.CHECKS)


def handler(name: str) -> Callable[[SuiteContext], str]:
    found = phase3.CHECKS.get(name) or phase4.CHECKS.get(name)
    if found is None:
        raise KeyError(f"unknown check {name}")
    return found
