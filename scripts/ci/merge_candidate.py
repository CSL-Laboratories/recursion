"""Candidate identity and authenticated review evidence; no network or mutations.

The caller supplies bounded API snapshots. A head-only READY or green ancestor
is deliberately not a candidate admission. These are staged workflow checks,
not a replacement for direct Devin or protected review authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

CODEX = {"id": 199175422, "login": "chatgpt-codex-connector[bot]", "type": "Bot"}
ACTIONS = {"id": 41898282, "login": "github-actions[bot]", "type": "Bot"}
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
MAX_PAGES = 20
MAX_RUN_ATTEMPT = 1


def identity(user, expected):
    return (type(user.get('id')) is int and user['id'] > 0
            and all(user.get(k) == expected.get(k) for k in ('id', 'login', 'type')))


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('invalid full commit/tree identity')
    return value


def candidate(pr, merge, live_base, repo):
    if (pr.get('state') != 'open' or pr.get('draft') is not False
            or (pr.get('head', {}).get('repo') or {}).get('full_name') != repo
            or (pr.get('base', {}).get('repo') or {}).get('full_name') != repo
            or type(pr.get('number')) is not int or pr['number'] <= 0
            or pr.get('mergeable') is not True):
        raise ValueError('candidate is not an open mergeable same-repository ready PR')
    head, base = sha(pr['head'].get('sha')), sha(pr['base'].get('sha'))
    merge_sha = sha(pr.get('merge_commit_sha'))
    if (sha(live_base) != base or merge.get('sha') != merge_sha
            or [p.get('sha') for p in merge.get('parents', [])] != [base, head]):
        raise ValueError('stale base or synthetic merge parents')
    return {'repo': repo, 'pr': pr['number'], 'head': head, 'base': base,
            'merge': merge_sha, 'tree': sha((merge.get('tree') or {}).get('sha'))}


def validate_candidate(value):
    if not isinstance(value, dict) or set(value) != {'repo', 'pr', 'head', 'base', 'merge', 'tree'}:
        raise ValueError('malformed candidate')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value['repo'])
            or type(value['pr']) is not int or value['pr'] <= 0):
        raise ValueError('malformed candidate repository/PR')
    for key in ('head', 'base', 'merge', 'tree'):
        sha(value[key])
    return value


def key(value):
    # Merge commit included as well as the tested tree: no event/check identity
    # substitution when GitHub regenerates a synthetic merge.
    raw = json.dumps(validate_candidate(value), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def marker(kind, value):
    return '<!-- ci-candidate-' + kind + ':' + json.dumps(value, sort_keys=True, separators=(',', ':')) + ' -->'


def records(comment, kind):
    prefix = '<!-- ci-candidate-' + kind + ':'
    body = comment.get('body', '')
    if not isinstance(body, str) or not body.startswith(prefix):
        return []
    line = body.split('\n', 1)[0]
    if not line.endswith(' -->'):
        raise ValueError('malformed candidate receipt')
    value = json.loads(line[len(prefix):-4])
    if not isinstance(value, dict):
        raise ValueError('malformed candidate receipt')
    return [value]


def snapshot(items):
    if not isinstance(items, list):
        raise ValueError('snapshot is not a list')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] <= 0 or item['id'] in seen:
            raise ValueError('invalid or duplicate snapshot identity')
        seen.add(item['id'])
    return items


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('missing evidence timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('unqualified evidence timestamp')
    return result


def request_for(comments, value, writer):
    found = []
    for comment in snapshot(comments):
        if not identity(comment.get('user') or {}, writer):
            continue
        for record in records(comment, 'request'):
            if record.get('candidate') == value:
                if record.get('key') != key(value) or comment.get('updated_at') != comment.get('created_at'):
                    raise ValueError('changed or malformed candidate request')
                timestamp(comment.get('created_at'))
                found.append(comment)
    if len(found) > 1:
        raise ValueError('ambiguous duplicate candidate requests')
    return found[0] if found else None


def latest_review(reviews, head):
    """Authoritative submitted attempt for a head, independent of mergeability."""
    sha(head)
    trusted = [r for r in snapshot(reviews) if identity(r.get('user') or {}, CODEX)]
    # Review IDs are created before submission: an older pending draft can be
    # submitted after a newer READY. Never order terminal attempts by numeric ID.
    if any(r.get('state', '').upper() == 'PENDING' or not SHA.fullmatch(str(r.get('commit_id') or ''))
           for r in trusted):
        raise ValueError('pending or unbound independent review')
    eligible = [r for r in trusted if r.get('commit_id') == head]
    ordered = sorted(eligible, key=lambda r: timestamp(r.get('submitted_at')))
    if len(ordered) > 1 and timestamp(ordered[-1]['submitted_at']) == timestamp(ordered[-2]['submitted_at']):
        raise ValueError('ambiguous latest independent review submission')
    if not ordered:
        raise ValueError('missing independent review for current head')
    return ordered[-1]


def verified_review(reviews, request, value, expected_id=None):
    latest = latest_review(reviews, value['head'])
    if ((expected_id is not None and (type(expected_id) is not int or latest.get('id') != expected_id))
            or not latest
            or latest.get('state', '').upper() not in {'COMMENTED', 'APPROVED'}
            or timestamp(latest.get('submitted_at')) <= timestamp(request.get('created_at'))):
        raise ValueError('missing, stale, pending or dismissed latest independent review')
    body = latest.get('body', '')
    # Treat READY as a small machine-readable affirmative review, not a marker
    # search through prose/Markdown. An exact two-line body excludes fenced,
    # quoted, hidden, duplicated and contradictory assertions on either side.
    # Findings/explanations belong in a non-READY review or inline comments.
    expected = ['CODEX-CANDIDATE: ' + key(value), 'CODEX-GATE: READY']
    if not isinstance(body, str) or body.rstrip('\r\n').splitlines() != expected:
        raise ValueError('latest review is not READY for this exact candidate')
    return latest


def claims(comments, value):
    found = []
    for comment in snapshot(comments):
        if identity(comment.get('user') or {}, ACTIONS):
            for claim in records(comment, 'claim'):
                if claim.get('candidate') == value:
                    if (claim.get('key') != key(value) or not re.fullmatch(r'[1-9][0-9]*', str(claim.get('run', '')))
                            or type(claim.get('review')) is not int or claim['review'] <= 0
                            or comment.get('updated_at') != comment.get('created_at')):
                        raise ValueError('malformed or edited admission claim')
                    found.append(claim)
    if len(found) > 1:
        raise ValueError('duplicate candidate admission claims')
    return found


def admission(comments, value, run, attempt, review):
    if (not re.fullmatch(r'[1-9][0-9]*', str(run)) or type(attempt) is not int
            or not 1 <= attempt <= MAX_RUN_ATTEMPT):
        raise ValueError('invalid or exhausted bounded workflow attempt')
    prior = claims(comments, value)
    if not prior:
        return 'reserve'
    if prior[0]['run'] == str(run):
        # The Actions identity is shared by every GITHUB_TOKEN writer, so a
        # claim is not gate-exclusive evidence. Resume only a reservation naming
        # this run and authoritative review; a same-run claim bound to a
        # different review is inconsistent and stops rather than admits.
        if prior[0]['review'] != review:
            raise ValueError('reservation bound to a different independent review')
        return 'resume'
    return 'duplicate'


def full_results(results):
    """No docs-only exemption, skipped/pending result or subset success."""
    return (isinstance(results, dict) and set(results) == {'linux', 'windows', 'macos'}
            and all(result == 'success' for result in results.values()))
