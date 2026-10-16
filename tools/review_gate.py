#!/usr/bin/env python3
"""Read-only Devin diagnostics; never a protected replacement for direct Devin.

The observed provider contract is a commit status from the pinned bot. Named
check runs are not fallback authority. Late recovery reruns the original PR run.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

DEVIN_CONTEXT = 'Devin Review'
DEVIN_ACTOR_ID = 158243242
DEVIN_ACTOR_LOGIN = 'devin-ai-integration[bot]'
DEFAULT_POLL_SECONDS = 180
DEFAULT_POLL_INTERVAL = 15
MAX_PAGES = 10


def require_sha(sha):
    if not re.fullmatch(r'[0-9a-f]{40}', sha or '') or sha == '0' * 40:
        raise ValueError('Expected a full nonzero commit SHA')
    return sha


def gh_api(path):
    repo = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid repository identity')
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', '')
    request = urllib.request.Request(f'https://api.github.com/repos/{repo}/{path}', headers={
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'recursion-review-gate', 'Authorization': f'Bearer {token}',
    })
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def latest_devin_status(sha):
    require_sha(sha)
    identity = gh_api(f'commits/{sha}/status?per_page=1')
    if not isinstance(identity, dict) or identity.get('sha') != sha:
        raise ValueError('Status response SHA does not match requested head')
    matches = {}
    seen = set()
    first_page = None
    for page in range(1, MAX_PAGES + 1):
        # Unlike the combined endpoint, this retains the authenticated creator
        # and every attempt. Follow every page before selecting the largest ID.
        rows = gh_api(f'commits/{sha}/statuses?per_page=100&page={page}')
        if not isinstance(rows, list):
            raise ValueError('Malformed status snapshot')
        if page == 1:
            first_page = rows
        for row in rows:
            if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
                raise ValueError('Invalid status identity')
            if row['id'] in seen:
                raise ValueError('Duplicate status page; cannot prove completeness')
            seen.add(row['id'])
            if row.get('context') != DEVIN_CONTEXT:
                continue
            creator = row.get('creator') or {}
            if (creator.get('id'), creator.get('login'), creator.get('type')) != (
                    DEVIN_ACTOR_ID, DEVIN_ACTOR_LOGIN, 'Bot'):
                raise ValueError('Untrusted publisher of Devin Review status')
            if row.get('state') not in {'pending', 'success', 'failure', 'error'}:
                raise ValueError('Invalid Devin status state')
            matches[row['id']] = row['state']
        if len(rows) < 100:
            if gh_api(f'commits/{sha}/statuses?per_page=100&page=1') != first_page:
                raise ValueError('Status changed during observation; rerun original workflow')
            return matches[max(matches)] if matches else None
    raise ValueError('Status pagination limit exceeded')


def devin_review_succeeded(sha):
    # Pending/failing latest status cannot fall back to any old check success.
    return latest_devin_status(sha) == 'success'


def verify_target(event, sha, pr_number):
    require_sha(sha)
    if event == 'merge_group':
        if os.environ.get('GITHUB_SHA') != sha:
            raise ValueError('Merge-group SHA differs from native workflow SHA')
        return
    if event != 'pull_request' or not re.fullmatch(r'[1-9][0-9]*', pr_number):
        raise ValueError('Only original PR or merge-group workflow runs are supported')
    pull = gh_api(f'pulls/{pr_number}')
    if not isinstance(pull, dict) or pull.get('number') != int(pr_number):
        raise ValueError('Wrong PR identity')
    if pull.get('state') != 'open' or pull.get('draft') is not False:
        raise ValueError('Review requires an open ready PR')
    if (pull.get('head') or {}).get('sha') != sha:
        raise ValueError('PR head advanced; do not rerun this stale workflow')
    if ((pull.get('base') or {}).get('repo') or {}).get('full_name') != os.environ['GITHUB_REPOSITORY']:
        raise ValueError('Wrong PR repository')


def poll_until_success(sha, *, seconds, interval, verify=lambda: None):
    if not 0 <= seconds <= 180 or not 1 <= interval <= 30:
        raise ValueError('Polling exceeds bounded policy')
    deadline = time.monotonic() + seconds
    while True:
        verify()
        if devin_review_succeeded(sha):
            verify()
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def main():
    event = os.environ.get('EVENT_NAME', '')
    sha = os.environ.get('HEAD_SHA', '')
    pr = os.environ.get('PR_NUMBER', '')
    seconds = int(os.environ.get('REVIEW_GATE_POLL_SECONDS', DEFAULT_POLL_SECONDS))
    interval = int(os.environ.get('REVIEW_GATE_POLL_INTERVAL', DEFAULT_POLL_INTERVAL))
    if poll_until_success(sha, seconds=seconds, interval=interval,
                          verify=lambda: verify_target(event, sha, pr)):
        print(f'Authenticated latest Devin status succeeded on {sha}')
        return 0
    print('::error::No current Devin success within bounded polling. Verify the live PR head, '
          'then rerun this original pull_request run after Devin finishes. Queue activation stays on hold.')
    return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'::error::review-gate failed: {exc}')
        raise SystemExit(1)
