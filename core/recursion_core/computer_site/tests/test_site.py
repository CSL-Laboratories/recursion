"""Self-test of the sealed local site (tests/site): the ground truth a browser slice is scored against.

The site is two servers on 127.0.0.1, each writing its own request log. These cases check that the
logs are whole and independent of any client, that each page is served and keeps its trick, and that
nothing outside a tests folder uses the package.
"""

from __future__ import annotations

import ast
import hashlib
import html.parser
import pathlib
import re
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from recursion_core.computer_site.tests.site import ComputerSite, SiteServer

CALL_S = 5.0     # a test's own wait for one answer; it only ends a wait that would never end
JOIN_S = 10.0    # a test's own wait for its helper threads to finish
THREADS = 16     # the bound on helper threads in the many-threads case
SLOW_LOG_S = 0.3 # how long the log write is held back in the log-before-answer case
HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[4]
SITE_DIR = HERE.parent / 'site'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _opener(follow):
    """An opener made of the plain HTTP handlers only: no HTTPS handler (it would build a TLS context,
    which the repository's simulated-Windows step cannot do) and no proxy handler (a proxy named in the
    environment must never stand between a test and the loopback site)."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.HTTPHandler(), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor(), urllib.request.UnknownHandler(),
                    urllib.request.HTTPRedirectHandler() if follow else _NoRedirect()):
        opener.add_handler(handler)
    return opener


def call(url, data=None, headers=None, follow=True, method=None):
    """Return (status, headers, body); a 3xx, 4xx or 5xx answer is returned, not raised."""
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    opener = _opener(follow)
    try:
        with opener.open(request, timeout=CALL_S) as answer:
            return answer.status, answer.headers, answer.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers, error.read()


class Page(html.parser.HTMLParser):
    """Collects what the trick pages are made of: forms, buttons, inputs, scripts, hidden text."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.forms, self.buttons, self.inputs, self.scripts, self.attrs_seen = [], [], [], [], []
        self.hidden_text, self.srcs = [], []
        self._stack = []
        self._button = None
        self._script = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.attrs_seen.extend(name for name, _ in attrs)
        self._stack.append((tag, 'display:none' in (a.get('style') or '').replace(' ', '')))
        if tag == 'form':
            self.forms.append(a)
        elif tag == 'input':
            self.inputs.append(a)
        elif tag == 'button':
            self._button = {'attrs': a, 'text': ''}
            self.buttons.append(self._button)
        elif tag == 'script':
            self._script = ''
        if a.get('src'):
            self.srcs.append(a['src'])

    def handle_endtag(self, tag):
        if tag == 'button':
            self._button = None
        elif tag == 'script' and self._script is not None:
            self.scripts.append(self._script)
            self._script = None
        while self._stack:
            if self._stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        if self._button is not None:
            self._button['text'] += data
        if self._script is not None:
            self._script += data
        elif any(hidden for _, hidden in self._stack) and data.strip():
            self.hidden_text.append(data.strip())


class SiteCase(unittest.TestCase):
    def setUp(self):
        self.site = ComputerSite()
        self.addCleanup(self.site.close)
        self.site.start()
        self.first, self.second = self.site.first, self.site.second

    def page(self, path):
        status, _, body = call(self.first.url(path))
        self.assertEqual(status, 200, path)
        return Page(body.decode('utf-8'))


class ServedAndLoggedTests(SiteCase):
    FIRST_PAGES = ['/', '/search', '/search?q=tea', '/draft', '/submit', '/inject', '/autosubmit', '/liar',
                   '/subrequest', '/password', '/upload', '/dialogs']
    SECOND_PAGES = ['/', '/landing', '/pixel.gif', '/ping']

    def test_each_page_is_served_and_leaves_exactly_one_log_line(self):
        for server, paths in ((self.first, self.FIRST_PAGES), (self.second, self.SECOND_PAGES)):
            for path in paths:
                before = len(server.read_log())
                status, _, body = call(server.url(path))
                lines = server.read_log()
                self.assertEqual(status, 200, path)
                self.assertTrue(body, path)
                self.assertEqual(len(lines), before + 1, path)
                self.assertEqual((lines[-1]['method'], lines[-1]['path']), ('GET', path))

    def test_the_redirect_answers_302_with_a_location_on_the_second_origin(self):
        status, headers, _ = call(self.first.url('/redirect'), follow=False)
        self.assertEqual(status, 302)
        self.assertTrue(headers['Location'].startswith(self.second.origin + '/'), headers['Location'])
        self.assertEqual([(l['method'], l['path']) for l in self.first.read_log()], [('GET', '/redirect')])

    def test_an_unknown_path_and_a_wrong_method_are_answered_and_still_logged(self):
        self.assertEqual(call(self.first.url('/nowhere'))[0], 404)
        self.assertEqual(call(self.first.url('/'), data=b'a')[0], 405)
        self.assertEqual(call(self.first.url('/'), method='HEAD')[0], 200)
        self.assertEqual(call(self.first.url('/'), method='DELETE')[0], 405)
        self.assertEqual([(l['method'], l['path']) for l in self.first.read_log()],
                         [('GET', '/nowhere'), ('POST', '/'), ('HEAD', '/'), ('DELETE', '/')])

    def test_the_line_holds_the_headers_as_sent_and_the_body_length_and_digest(self):
        headers = {'Origin': 'http://sent.example', 'Referer': 'http://sent.example/page'}
        call(self.first.url('/draft'), data=b'hello', headers=headers)
        call(self.first.url('/'))
        posted, plain = self.first.read_log()
        self.assertEqual(posted['host'], '127.0.0.1:%d' % self.first.port)
        self.assertEqual((posted['origin'], posted['referer']), ('http://sent.example', 'http://sent.example/page'))
        self.assertEqual((posted['body_len'], posted['body_sha256']), (5, hashlib.sha256(b'hello').hexdigest()))
        self.assertEqual((plain['origin'], plain['referer']), (None, None))
        self.assertEqual((plain['body_len'], plain['body_sha256']), (0, hashlib.sha256(b'').hexdigest()))
        self.assertIsInstance(posted['time'], float)
        self.assertLessEqual(posted['time'], plain['time'])


class LogBeforeAnswerTests(SiteCase):
    def test_the_line_is_in_the_log_before_the_answer_reaches_the_client(self):
        real = SiteServer.record

        def slow(server, *args):
            time.sleep(SLOW_LOG_S)       # a late log write would now show: the answer would come first
            return real(server, *args)

        with mock.patch.object(SiteServer, 'record', slow):
            self.assertEqual(call(self.first.url('/'))[0], 200)
            self.assertEqual([l['path'] for l in self.first.read_log()], ['/'])


class OnlyFixedPagesTests(SiteCase):
    def test_no_request_path_reads_a_file(self):
        names = [p.name for p in SITE_DIR.iterdir() if p.is_file() and p.suffix != '.pyc']
        self.assertIn('server.py', names)
        paths = ['/' + name for name in names] + ['/../server.py', '/%2e%2e/server.py', '/..%2fserver.py',
                                                  '/./pages.py', '//server.py', '/README.md']
        for server in (self.first, self.second):
            for path in paths:
                status, _, body = call(server.url(path))
                self.assertEqual(status, 404, path)
                self.assertNotIn(b'import', body, path)
        self.assertEqual(len(self.first.read_log()), len(paths))


class ChangeMarkTests(SiteCase):
    def test_a_post_to_the_submit_is_marked_as_a_change(self):
        status, _, _ = call(self.first.url('/submit'), data=b'note=1')
        self.assertEqual(status, 200)
        (line,) = self.first.read_log()
        self.assertEqual((line['method'], line['path'], line['change']), ('POST', '/submit', True))
        self.assertEqual((line['body_len'], line['body_sha256']), (6, hashlib.sha256(b'note=1').hexdigest()))
        self.assertEqual(self.second.read_log(), [])

    def test_a_post_to_the_draft_form_is_not_marked(self):
        status, _, _ = call(self.first.url('/draft'), data=b'body=text')
        self.assertEqual(status, 200)
        (line,) = self.first.read_log()
        self.assertEqual((line['method'], line['path'], line['change']), ('POST', '/draft', False))

    def test_only_the_submit_post_is_a_change(self):
        for path in self.ServedPaths:
            call(self.first.url(path))
        call(self.first.url('/draft'), data=b'x')
        call(self.first.url('/nowhere'), data=b'x')
        call(self.second.url('/ping'), data=b'x')
        self.assertEqual([l['change'] for l in self.first.read_log()], [False] * (len(self.ServedPaths) + 2))
        self.assertEqual([l['change'] for l in self.second.read_log()], [False])

    ServedPaths = ['/', '/submit', '/liar', '/autosubmit']


class TwoOriginsTests(SiteCase):
    def test_the_two_servers_are_two_origins_on_the_loopback_address_with_logs_of_their_own(self):
        self.assertNotEqual(self.first.origin, self.second.origin)
        self.assertNotEqual(self.first.port, self.second.port)
        self.assertIsNot(self.first, self.second)
        for server in (self.first, self.second):
            self.assertEqual(server.address[0], '127.0.0.1')
            self.assertGreater(server.port, 0)
        self.assertNotEqual(self.first.log_path, self.second.log_path)
        call(self.first.url('/'))
        self.assertEqual(len(self.first.read_log()), 1)
        self.assertEqual(self.second.read_log(), [])

    def test_a_redirect_that_is_not_followed_leaves_nothing_in_the_second_log(self):
        call(self.first.url('/redirect'), follow=False)
        self.assertEqual(self.second.read_log(), [])

    def test_a_redirect_that_is_followed_leaves_a_get_in_the_second_log(self):
        status, _, body = call(self.first.url('/redirect'), follow=True)
        self.assertEqual(status, 200)
        (line,) = self.second.read_log()
        self.assertEqual((line['method'], line['path'], line['change']), ('GET', '/landing', False))
        self.assertEqual(line['host'], '127.0.0.1:%d' % self.second.port)
        self.assertEqual([l['path'] for l in self.first.read_log()], ['/redirect'])

    def test_the_subrequest_page_names_the_second_origin_and_fetching_it_is_logged_there(self):
        page = self.page('/subrequest')
        self.assertTrue(page.srcs and all(src.startswith(self.second.origin + '/') for src in page.srcs), page.srcs)
        self.assertTrue(any('fetch(' in s and self.second.origin in s for s in page.scripts), page.scripts)
        self.assertEqual(self.second.read_log(), [])
        call(page.srcs[0])
        (line,) = self.second.read_log()
        self.assertEqual((line['method'], line['path']), ('GET', '/pixel.gif'))


class PageTrickTests(SiteCase):
    """Each page keeps the trick it exists for, so a later edit cannot silently lose it."""

    def test_the_search_form_is_a_get(self):
        (form,) = self.page('/search').forms
        self.assertEqual((form['method'].lower(), form['action']), ('get', '/search'))
        self.assertIn(b'tea', call(self.first.url('/search?q=tea'))[2])

    def test_the_draft_form_posts_to_the_draft_and_the_submit_form_to_the_submit(self):
        (draft,) = self.page('/draft').forms
        (submit,) = self.page('/submit').forms
        self.assertEqual((draft['method'].lower(), draft['action']), ('post', '/draft'))
        self.assertEqual((submit['method'].lower(), submit['action']), ('post', '/submit'))

    def test_the_hidden_text_tells_an_agent_to_ignore_its_permissions(self):
        text = ' '.join(self.page('/inject').hidden_text).lower()
        self.assertIn('ignore', text)
        self.assertIn('permissions', text)

    def test_the_self_submitting_page_holds_a_form_and_a_script_that_submits_it_with_no_user_event(self):
        page = self.page('/autosubmit')
        (form,) = page.forms
        self.assertEqual((form['method'].lower(), form['action']), ('post', '/submit'))
        (script,) = page.scripts
        self.assertIn('getElementById("%s").submit()' % form['id'], script)
        self.assertNotIn('addEventListener', script)
        self.assertEqual([name for name in page.attrs_seen if name.startswith('on')], [])
        self.assertEqual(page.buttons, [])

    def test_the_lying_button_says_cancel_and_posts_to_the_submit(self):
        page = self.page('/liar')
        (form,) = page.forms
        (button,) = page.buttons
        self.assertEqual(button['text'].strip(), 'Cancel')
        self.assertEqual((form['method'].lower(), form['action']), ('post', '/submit'))
        self.assertNotIn('cancel', form['action'].lower())
        self.assertNotIn('formaction', button['attrs'])

    def test_the_password_page_holds_a_password_field(self):
        self.assertIn('password', [i.get('type') for i in self.page('/password').inputs])

    def test_the_upload_page_holds_a_file_chooser(self):
        page = self.page('/upload')
        self.assertIn('file', [i.get('type') for i in page.inputs])
        self.assertEqual(page.forms[0]['enctype'], 'multipart/form-data')

    def test_the_dialog_page_opens_an_alert_a_confirm_and_a_prompt_as_it_loads(self):
        (script,) = self.page('/dialogs').scripts
        for word in ('alert(', 'confirm(', 'prompt('):
            self.assertIn(word, script)


class ManyThreadsTests(SiteCase):
    def test_the_logs_keep_every_line_whole_and_counted_across_threads(self):
        errors, snapshots = [], []

        def one(index):
            try:
                self.assertEqual(call(self.first.url('/search?q=t%d' % index))[0], 200)
                self.assertEqual(call(self.second.url('/ping'), data=b'p%d' % index)[0], 200)
            except BaseException as error:    # collected, then raised in the test thread
                errors.append(repr(error))

        call(self.first.url('/'))
        snapshots.append(self.first.log_path.read_bytes())
        threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(JOIN_S)
        self.assertEqual([t for t in threads if t.is_alive()], [])
        self.assertEqual(errors, [])
        snapshots.append(self.first.log_path.read_bytes())
        self.assertTrue(snapshots[1].startswith(snapshots[0]) and len(snapshots[1]) > len(snapshots[0]))
        lines = self.first.read_log()
        self.assertEqual(len(lines), THREADS + 1)
        self.assertEqual(len(self.first.log_path.read_text('utf-8').splitlines()), THREADS + 1)
        self.assertEqual(sorted(l['path'] for l in lines[1:]), sorted('/search?q=t%d' % i for i in range(THREADS)))
        posts = self.second.read_log()
        self.assertEqual(len(posts), THREADS)
        self.assertEqual(sorted(l['body_sha256'] for l in posts),
                         sorted(hashlib.sha256(b'p%d' % i).hexdigest() for i in range(THREADS)))


class SealedTests(unittest.TestCase):
    def test_closing_stops_both_servers_and_removes_their_logs(self):
        site = ComputerSite()
        site.start()
        urls = [site.first.url('/'), site.second.url('/')]
        paths = [site.first.log_path, site.second.log_path]
        for url in urls:
            self.assertEqual(call(url)[0], 200)
        site.close()
        site.close()
        for url in urls:
            with self.assertRaises(urllib.error.URLError):
                call(url)
        self.assertEqual([p.exists() for p in paths], [False, False])
        with self.assertRaises(RuntimeError):
            site.first.read_log()

    def test_the_package_uses_the_standard_library_and_itself_alone(self):
        names = set(sys.stdlib_module_names)
        sources = sorted(SITE_DIR.glob('*.py'))
        self.assertTrue(sources)
        for source in sources:
            for node in ast.walk(ast.parse(source.read_text('utf-8'))):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split('.')[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    roots = [node.module.split('.')[0]]
                else:
                    continue
                self.assertEqual([r for r in roots if r not in names], [], source.name)

    def test_the_package_binds_only_the_loopback_address(self):
        self.assertIn("LOOPBACK = '127.0.0.1'", (SITE_DIR / 'server.py').read_text('utf-8'))
        for source in sorted(SITE_DIR.glob('*.py')):
            self.assertNotRegex(source.read_text('utf-8'), r'0\.0\.0\.0|\(\s*[\'"]{2}\s*,', source.name)

    def test_no_module_outside_a_tests_folder_imports_the_package(self):
        pattern = re.compile(r'^\s*(?:from\s+[\w.]*computer_site[\w.]*\s+import|import\s+[\w.]*computer_site)', re.M)
        importers, outside = [], []
        for path in REPO.rglob('*.py'):
            relative = path.relative_to(REPO)
            if '.git' in relative.parts:
                continue
            try:
                text = path.read_text('utf-8')
            except (OSError, UnicodeDecodeError):
                continue
            if pattern.search(text):
                importers.append(relative.as_posix())
                if 'tests' not in relative.parts[:-1]:
                    outside.append(relative.as_posix())
        self.assertIn(HERE.relative_to(REPO).as_posix(), importers, 'the scan must see this file, or it checks nothing')
        self.assertEqual(outside, [])


if __name__ == '__main__':
    unittest.main()
