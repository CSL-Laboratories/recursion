#!/usr/bin/env python3
"""Exact-candidate READY admission using the existing gate's grants and lock.

Runs only from the default-branch trusted checkout. The test workflow receives
immutable identities, never the PAT. Unknown writes stop; no automatic retries
of mutations. Candidate work has one attempt; partial reruns cannot bypass fresh admission.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote

import codex_gate as gate
import merge_candidate as protocol


class Client:
    def __init__(self, repo, token, gh=gate._gh):
        self.repo, self.token, self.gh = repo, token, gh

    def get(self, path):
        return json.loads(self.gh(['api', path], self.token))

    def pages(self, path):
        def collect():
            result = []
            for page in range(1, protocol.MAX_PAGES + 1):
                items = protocol.snapshot(self.get(f'{path}?per_page=100&page={page}'))
                result.extend(items)
                if len(items) < 100:
                    return protocol.snapshot(result)
            raise ValueError('API pagination bound exceeded')
        result = collect()
        # Reviews and comments can be edited on later pages without changing
        # page one. Compare complete bounded snapshots, not only the first page.
        if collect() != result:
            raise ValueError('moving API snapshot')
        return result

    def candidate(self, number):
        path = f'repos/{self.repo}/pulls/{number}'
        pr = self.get(path)
        base_ref = (pr.get('base') or {}).get('ref')
        if not gate.safe_ref(base_ref):
            raise ValueError('unsafe base branch')
        ref = f'repos/{self.repo}/git/ref/heads/{quote(base_ref, safe="/")}'
        base = self.get(ref)['object']['sha']
        merge = self.get(f'repos/{self.repo}/git/commits/{protocol.sha(pr.get("merge_commit_sha"))}')
        value = protocol.candidate(pr, merge, base, self.repo)
        again = self.get(path)
        if protocol.candidate(again, merge, self.get(ref)['object']['sha'], self.repo) != value:
            raise ValueError('candidate changed during snapshot')
        return value

    def comments(self, number):
        return self.pages(f'repos/{self.repo}/issues/{number}/comments')

    def reviews(self, number):
        return self.pages(f'repos/{self.repo}/pulls/{number}/reviews')

    def events(self, number):
        return self.pages(f'repos/{self.repo}/issues/{number}/events')

    def post(self, number, body):
        gate.post_comment(self.repo, str(number), body, self.token, gh=self.gh)


def request(client, number, pat):
    if not pat:
        raise ValueError('existing gate PAT unavailable')
    writer = json.loads(client.gh(['api', 'user'], pat))
    value = client.candidate(number)
    comments = client.comments(number)
    if protocol.request_for(comments, value, writer):
        return {'action': 'already-requested', 'proceed': 'false'}
    today = datetime.now(timezone.utc).date()
    requests_today = [c for c in comments if protocol.identity(c.get('user') or {}, writer)
                      and protocol.timestamp(c.get('created_at')).date() == today
                      and any(r.get('candidate', {}).get('head') == value['head']
                              for r in protocol.records(c, 'request'))]
    if len(requests_today) >= 3:
        raise ValueError('three independent review requests per head per UTC day exhausted')
    body = (protocol.marker('request', {'candidate': value, 'key': protocol.key(value)})
            + '\n@codex review\n\nInspect this exact candidate before human approval. '
            + 'Verify head, base and synthetic merge tree; a stale head-only review does not count.\n'
            + json.dumps(value, sort_keys=True) + '\n\n'
            + 'Preserve Devin/fixer roles and all manual holds. Do not push, merge or resolve threads. '
            + 'If independently READY, the entire review body must be exactly these two plain lines '
            + '(no prose, quotation, indentation or code fence; READY last):\n'
            + 'CODEX-CANDIDATE: ' + protocol.key(value) + '\nCODEX-GATE: READY\n'
            + 'Otherwise report findings without READY, or CODEX-GATE: MEDIATE under the existing rules.\n')
    if client.candidate(number) != value:
        raise ValueError('candidate moved before request')
    # Existing PAT request path; no new grant or automatic retry after uncertainty.
    gate.post_comment(client.repo, str(number), body, pat, gh=client.gh)
    return {'action': 'requested', 'proceed': 'false'}


def read_evidence(client, number, value, review_id, writer):
    if client.candidate(number) != value:
        raise ValueError('candidate changed')
    comments = client.comments(number)
    request_record = protocol.request_for(comments, value, writer)
    if request_record is None:
        raise ValueError('no authenticated exact-candidate request')
    protocol.verified_review(client.reviews(number), request_record, value, review_id)
    return comments


def invalidate(client, number):
    """Clear only a proven owned stale label under the existing writer lock.

    Request events never admit a matrix. A current reservation keeps its label;
    admission and final audit separately authenticate the latest review.
    """
    value = client.candidate(number)
    comments = client.comments(number)
    if protocol.claims(comments, value):
        return {'action': 'current-candidate', 'admitted': 'false'}
    if client.candidate(number) != value:
        raise ValueError('candidate moved before invalidation')
    revoked = gate.fixer_helpers().revoke_owned_ci(client.repo, str(number), value['head'],
        get_pages=client.pages,
        delete=lambda r, n: client.gh(['api', '-X', 'DELETE', f'repos/{r}/issues/{n}/labels/ci:full'], client.token),
        post_comment=lambda r, n, body: client.post(n, body))
    return {'action': 'invalidated' if revoked else 'unowned-or-absent', 'admitted': 'false'}


def admit(client, event, pat, run, attempt):
    review = event.get('review') or {}
    number = (event.get('pull_request') or {}).get('number')
    if type(number) is not int or not protocol.identity(review.get('user') or {}, protocol.CODEX):
        raise ValueError('untrusted review event')
    if gate.parse_verdict(review.get('body')) == 'MEDIATE':
        # Mediation never admits paid CI and must remain available for conflicts,
        # stale merge refs and unavailable candidate identities. Retain the legacy
        # router's fork/draft, missing-PAT and authenticated deduplication policy.
        result = gate.run_verdict(event, client.repo, pat, client.token, 'master', gh=client.gh)
        return {**result, 'admitted': 'false'}
    value = client.candidate(number)
    # Native pull_request_review checks report on the event merge SHA. Never
    # attach a current-candidate result to a stale event's different merge SHA.
    if os.environ.get('GITHUB_SHA') != value['merge']:
        raise ValueError('event merge SHA is stale')
    writer = json.loads(client.gh(['api', 'user'], pat))
    writer = {k: writer[k] for k in ('id', 'login', 'type')}
    comments = client.comments(number)
    request_record = protocol.request_for(comments, value, writer)
    try:
        if request_record is None:
            raise ValueError('missing candidate request')
        authoritative = protocol.verified_review(client.reviews(number), request_record, value)
    except ValueError:
        # A dismissal/edit/new negative attempt invalidates owned readiness.
        # Human-owned labels and removals retain the existing helper's policy.
        gate.fixer_helpers().revoke_owned_ci(client.repo, str(number), value['head'],
            get_pages=client.pages,
            delete=lambda r, n: client.gh(['api', '-X', 'DELETE', f'repos/{r}/issues/{n}/labels/ci:full'], client.token),
            post_comment=lambda r, n, body: client.post(n, body))
        return {'action': 'independent-review-invalid', 'admitted': 'false'}
    if authoritative['id'] != review.get('id'):
        return {'action': 'superseded-event', 'admitted': 'false', 'duplicate': 'true'}
    review = authoritative
    event = {**event, 'review': review}
    comments = read_evidence(client, number, value, review.get('id'), writer)
    decision = protocol.admission(comments, value, run, attempt, review['id'])
    if decision == 'duplicate':
        return {'action': 'already-admitted', 'admitted': 'false', 'duplicate': 'true'}
    helper = gate.fixer_helpers()
    events = client.events(number)
    # A legacy/persistent label is not evidence. Revoke only proven owned state;
    # never remove a human-owned label. The existing helper records invalidation
    # before deleting and preserves uncertain writes/manual removals.
    current = helper.label_event_id(events)
    current_claims = protocol.claims(comments, value)
    if current and not current_claims:
        helper.revoke_owned_ci(client.repo, str(number), value['head'],
            get_pages=client.pages,
            delete=lambda r, n: client.gh(['api', '-X', 'DELETE', f'repos/{r}/issues/{n}/labels/ci:full'], client.token),
            post_comment=lambda r, n, body: client.post(n, body))
    # This retains authenticated intent/ownership, live findings reconciliation,
    # manual holds, final rechecks, and the existing writer's rollback behavior.
    result = gate.run_verdict(event, client.repo, pat, client.token, 'master', gh=client.gh)
    if result.get('action') not in {'ready', 'ready-present'}:
        raise ValueError('existing readiness/label policy did not admit candidate')
    comments = read_evidence(client, number, value, review['id'], writer)
    decision = protocol.admission(comments, value, run, attempt, review['id'])
    if decision == 'duplicate':
        return {'action': 'already-admitted', 'admitted': 'false', 'duplicate': 'true'}
    label_event = helper.label_event_id(client.events(number))
    if not label_event:
        raise ValueError('READY label removed before admission')
    if decision == 'reserve':
        client.post(number, protocol.marker('claim', {'candidate': value, 'key': protocol.key(value),
            'run': str(run), 'review': review['id'], 'label': label_event}))
    # Confirm the reservation, including a potentially uncertain write, before
    # exporting any matrix admission. Same run/attempt is the recovery identity.
    if protocol.admission(client.comments(number), value, run, attempt, review['id']) != 'resume':
        raise ValueError('admission reservation was not confirmed')
    if client.candidate(number) != value:
        raise ValueError('candidate moved after reservation')
    return {'action': 'admitted', 'admitted': 'true', 'candidate': json.dumps(value, separators=(',', ':')),
            'head': value['head'], 'base': value['base'], 'merge': value['merge'], 'tree': value['tree'],
            'review': str(review['id']), 'writer': json.dumps(writer, separators=(',', ':'))}


def finish(client, value, review_id, writer, run, attempt, matrix_result):
    protocol.validate_candidate(value)
    if matrix_result != 'success' or os.environ.get('GITHUB_SHA') != value['merge']:
        raise ValueError('matrix incomplete, failed, cancelled or misbound')
    # Re-validate the authoritative latest review. A later READY for this same
    # candidate does not require duplicate paid work; a pending/dismissed/negative
    # attempt does invalidate readiness.
    comments = read_evidence(client, value['pr'], value, None, writer)
    if protocol.admission(comments, value, run, attempt, review_id) != 'resume':
        raise ValueError('missing matching matrix reservation')
    helper = gate.fixer_helpers()
    claim = protocol.claims(comments, value)[0]
    if helper.label_event_id(client.events(value['pr'])) != claim.get('label'):
        raise ValueError('manual removal/relabel or readiness invalidation')
    # Read-only final audit. If new observation journal evidence would have to be
    # authored, fail and return to the locked readiness writer; never journal here.
    def no_record(_body):
        raise ValueError('new findings need locked readiness reconciliation')
    if helper.readiness_blocked(client.repo, str(value['pr']), value['head'], comments,
                                read_api=client.get, record=no_record):
        raise ValueError('unsettled findings or manual handoff')
    if client.candidate(value['pr']) != value:
        raise ValueError('candidate changed during final audit')
    print('CANDIDATE_READY_PASS ' + protocol.key(value))


def main():
    mode = sys.argv[1]
    repo = os.environ['GITHUB_REPOSITORY']
    client = Client(repo, os.environ['GH_TOKEN'])
    try:
        if mode in {'request', 'invalidate'}:
            event = gate._load_event(os.environ['GITHUB_EVENT_PATH'])
            number = gate.safe_pr_number((event.get('pull_request') or {}).get('number')
                                        or (event.get('inputs') or {}).get('pr_number'))
            if not number:
                raise ValueError('missing PR number')
            result = (request(client, int(number), os.environ.get('CODEX_TRIGGER_PAT', ''))
                      if mode == 'request' else invalidate(client, int(number)))
        elif mode == 'admit':
            result = admit(client, gate._load_event(os.environ['GITHUB_EVENT_PATH']),
                           os.environ.get('CODEX_TRIGGER_PAT', ''), os.environ['GITHUB_RUN_ID'],
                           int(os.environ['GITHUB_RUN_ATTEMPT']))
        elif mode == 'finish':
            finish(client, json.loads(os.environ['CANDIDATE']), int(os.environ['REVIEW_ID']),
                   json.loads(os.environ['GATE_WRITER']), os.environ['GITHUB_RUN_ID'],
                   int(os.environ['GITHUB_RUN_ATTEMPT']), os.environ['MATRIX_RESULT'])
            return 0
        else:
            raise ValueError('unsupported candidate gate mode')
        gate.emit(result)
        return 0
    except (ValueError, KeyError, TypeError, OSError, gate.subprocess.CalledProcessError) as exc:
        # Never expose subprocess stderr, tokens, response bodies or PR text.
        print('::error::Candidate readiness could not be verified (' + type(exc).__name__ + ')')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
