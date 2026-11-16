"""Every check handler the tester can run, by manifest name (Phase 3, Phase 4 and Phase 5).

Lookups read every phase table at call time, so a handler replaced in any table (for
example by a test) is the one that runs.
"""

from collections.abc import Callable

from tester.phase4 import checks as phase4
from tester.phase5 import checks as phase5
from tester.suite import checks as phase3
from tester.suite.checks import SuiteContext

_TABLES = (phase3.CHECKS, phase4.CHECKS, phase5.CHECKS)
if sum(len(table) for table in _TABLES) != len(set().union(*_TABLES)):
    raise RuntimeError("Phase 3, Phase 4 and Phase 5 check names must not collide")


def names() -> set[str]:
    return set(phase3.CHECKS) | set(phase4.CHECKS) | set(phase5.CHECKS)


def handler(name: str) -> Callable[[SuiteContext], str]:
    found = phase3.CHECKS.get(name) or phase4.CHECKS.get(name) or phase5.CHECKS.get(name)
    if found is None:
        raise KeyError(f"unknown check {name}")
    return found
