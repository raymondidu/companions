"""The monitors must not depend on a cron GitHub has never honoured.

Context. companion-health-assert.yml and companion-host-resources.yml landed on
main at 19:01 UTC on 2026-09-11, each with its own cron. By 21:23 the health
check had passed five scheduled slots (19:08, 19:38, 20:08, 20:38, 21:08) and
GitHub had fired none of them; the host-resources workflow had not run a single
time. companion-telemetry.yml, whose schedule predates both, fired reliably
throughout.

GitHub documents `schedule` as best-effort -- delayed under load, droppable
entirely. That is survivable for a workflow that prints state. It is not
survivable for the one workflow whose whole job is noticing that a counter
stopped moving. A monitor that silently never runs reports nothing, and nothing
reads as nothing wrong. On 2026-09-11 opened_signals sat frozen for twelve hours
while every check said healthy.

So both now also chain off the telemetry workflow. These tests pin the two ways
that chain can silently break: the trigger being dropped, and the referenced
workflow name drifting away from the real one. A `workflow_run` naming a
workflow that does not exist raises no error -- it simply never fires, which is
the exact failure being fixed.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'

DRIVER = WORKFLOWS / 'companion-telemetry.yml'
CHAINED = (
    WORKFLOWS / 'companion-health-assert.yml',
    WORKFLOWS / 'companion-host-resources.yml',
)


def load(path):
    return yaml.safe_load(path.read_text(encoding='utf-8'))


def triggers(doc):
    # PyYAML resolves a bare `on:` key to the boolean True.
    return doc.get('on', doc.get(True)) or {}


def test_the_driver_workflow_still_has_the_schedule_being_relied_on():
    """If telemetry loses its cron, every chained monitor goes quiet with it."""
    on = triggers(load(DRIVER))
    assert 'schedule' in on, 'companion-telemetry.yml no longer runs on a schedule'
    assert on['schedule'], 'companion-telemetry.yml schedule is empty'


@pytest.mark.parametrize('path', CHAINED, ids=lambda p: p.name)
def test_the_monitor_does_not_depend_on_its_own_cron_alone(path):
    on = triggers(load(path))
    assert 'workflow_run' in on, (
        '%s relies on its own cron alone. GitHub fired none of its first five '
        'scheduled slots on 2026-09-11.' % path.name)


@pytest.mark.parametrize('path', CHAINED, ids=lambda p: p.name)
def test_the_chain_names_a_workflow_that_actually_exists(path):
    """A workflow_run naming a workflow that does not exist never fires and
    never errors. That silence is the whole bug this chain exists to fix."""
    named = triggers(load(path))['workflow_run']['workflows']
    real = {load(f).get('name') for f in WORKFLOWS.glob('*.yml')}
    real.discard(None)
    missing = [n for n in named if n not in real]
    assert not missing, (
        '%s chains off %r, which matches no workflow name in .github/workflows. '
        'Known names: %s' % (path.name, missing, sorted(real)))


@pytest.mark.parametrize('path', CHAINED, ids=lambda p: p.name)
def test_the_chain_points_at_the_driver_we_verified_is_firing(path):
    named = triggers(load(path))['workflow_run']['workflows']
    assert load(DRIVER)['name'] in named, (
        '%s should chain off the telemetry workflow, the one schedule GitHub '
        'has actually been honouring' % path.name)


@pytest.mark.parametrize('path', CHAINED, ids=lambda p: p.name)
def test_the_manual_escape_hatch_survives(path):
    """Every one of these had to be dispatched by hand tonight to get a reading
    at all. That escape hatch stays."""
    assert 'workflow_dispatch' in triggers(load(path)), path.name


def test_ci_runs_the_whole_tests_directory_not_a_hand_maintained_list():
    """A named list silently excludes whatever nobody remembered to add.

    CI named five files and tests/test_companion_health_assert.py was not one of
    them, so the 23 tests guarding the health assertions -- written specifically
    to catch the frozen counter of 2026-09-11 -- had never run in CI once. They
    sat in the repo protecting nothing.

    A directory needs no maintenance to stay correct. This fails if anyone goes
    back to enumerating files.
    """
    ci = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
    pytest_lines = [ln.strip() for ln in ci.splitlines() if 'pytest' in ln]
    assert pytest_lines, 'CI no longer runs pytest at all'
    assert any(ln.rstrip().endswith('tests') for ln in pytest_lines), (
        'CI should run the whole tests directory; found: %r' % pytest_lines)
    # Only executable lines count. A comment that mentions a test file by name
    # is documentation, not an exclusion -- an earlier version of this check
    # failed on exactly such a comment.
    code = [ln for ln in ci.splitlines() if not ln.lstrip().startswith('#')]
    named = [ln.strip() for ln in code if 'tests/test_' in ln and '.py' in ln]
    assert not named, (
        'CI enumerates individual test files again, so anything not listed will '
        'silently never run: %r' % named)


HOST_GUARD = Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'companion-host-resources.yml'


def test_the_host_guard_verdict_can_actually_fail_the_run():
    """A verdict that cannot fail the run is a print statement.

    The guard raises SystemExit on an OOM kill and on OVER_CEILING. Both raises
    went nowhere. The ssh script runs under `set -uo pipefail` -- no -e -- so a
    non-zero exit from the guard heredoc did not abort it, and the trailing echo
    reset $? to 0. ssh-action saw a clean exit and marked the run green.

    Measured, not theorised. At 05:41 on 2026-09-12, forty minutes after the OOM
    ceiling was merged, this workflow printed

      The kernel OOM-killed 6 process(es) on the companion host in the last
      6 hours: 03:09:35 ... 05:32:00

    and the run concluded SUCCESS. The host OVER_CEILING raise had been in that
    state since it was written.

    Text matching would not have caught this -- the raise really was there. So
    this executes the real trailing structure taken from the parsed YAML, with
    the guard stubbed to a known failing status, and asserts the script carries
    that status out.
    """
    import re
    import subprocess
    import textwrap

    doc = yaml.safe_load(HOST_GUARD.read_text(encoding='utf-8'))
    script = None
    for step in doc['jobs']['resources']['steps']:
        blob = str((step.get('with') or {}).get('script') or step.get('run') or '')
        if 'PYGUARD' in blob:
            script = blob
    assert script, 'guard step not found in the parsed workflow'

    stubbed = re.sub(r"python3 - <<'PYGUARD'\n.*?\n\s*PYGUARD",
                     "python3 -c 'raise SystemExit(7)'", script, flags=re.S)
    assert 'PYGUARD' not in stubbed, 'guard heredoc was not stubbed out'
    tail = stubbed[stubbed.index("python3 -c 'raise SystemExit(7)'"):]
    body = 'set -uo pipefail\n' + textwrap.dedent(tail)

    result = subprocess.run(['bash', '-c', body], capture_output=True, text=True)
    assert result.returncode != 0, (
        'the guard exited 7 and the script still returned 0; the verdict '
        'cannot fail the run.\nscript tail was:\n%s' % body)
