"""Fail-closed aggregate for the selected provider-free platform jobs."""
import json
import os


def validate(needs, *, event, action='', draft=False, full=False, cancelled=False):
    if cancelled or event not in {'pull_request', 'push', 'workflow_dispatch', 'merge_group'}:
        return False
    if set(needs) != {'settle', 'linux', 'windows', 'macos'}:
        return False
    wait = event == 'pull_request' and action == 'synchronize' and not draft
    if needs['settle'].get('result') != ('success' if wait else 'skipped'):
        return False
    linux = needs['linux']
    if linux.get('result') != 'success':
        return False
    outputs = linux.get('outputs', {})
    run = outputs.get('run_full')
    if (run, outputs.get('classification')) not in {
            ('true', 'full'), ('false', 'docs-only')}:
        return False
    # No weaker PR success: a delayed unlabeled run cannot overwrite failed
    # full coverage with skipped OS jobs. The linux check stays independent.
    selected = {'windows': True,
                'macos': event != 'push'}
    return all(needs[job].get('result') ==
               ('success' if run == 'true' and selected[job] else 'skipped')
               for job in selected)


def main():
    passed = validate(json.loads(os.environ['CI_NEEDS']), event=os.environ['CI_EVENT'],
                      action=os.environ.get('CI_ACTION', ''),
                      draft=os.environ.get('CI_DRAFT') == 'true',
                      full=os.environ.get('CI_FULL') == 'true',
                      cancelled=os.environ.get('CI_CANCELLED') == 'true')
    print('PLATFORM_GATES_PASS' if passed else '::error::Selected platform checks incomplete or failed')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
