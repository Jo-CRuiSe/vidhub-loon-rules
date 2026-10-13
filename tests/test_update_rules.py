import base64
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import update_rules as app

BASE = 'https://vidhub3.top/'
CONFIG = {'base_url': BASE, 'seed_movies': ['1','2','3'], 'max_movies': 3,
          'max_play_pages': 12, 'max_manifest_hosts': 5, 'workers': 2}


def fixture_fetch(url):
    path = app.urlsplit(url).path
    if path == '/':
        return ''.join(f'<a href="/voddetail/{i}.html">Movie {i}</a>' for i in range(1,4)), url
    if path.startswith('/voddetail/'):
        video = path.split('/')[-1].split('.')[0]
        return f'<a href="/vodplay/{video}-1-1.html">First</a><a href="/vodplay/{video}-1-9.html">Last</a>', url
    if path.startswith('/vodplay/'):
        video = path.split('/')[-1].split('-')[0]
        return f'<iframe src="/player/?i={video}"></iframe>', url
    if path == '/player/':
        data = {'url':'https://video.example.org/master.m3u8', 'pic':'https://image.example.org/poster.jpg'}
        return '<script>var config='+json.dumps(data)+';</script>', url
    if path == '/master.m3u8':
        return '#EXTM3U\nchild.m3u8\n', 'https://redirect.example.org/master.m3u8'
    if path == '/child.m3u8':
        return '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://key.example.net/key"\nhttps://segment.example.net/a.ts\n', url
    raise AssertionError('Unexpected request, including media segment: '+url)


class ParsingTests(unittest.TestCase):
    def test_external_iframe_host_is_retained(self):
        scan = app.Scanner(CONFIG)
        scan.collect(app.Page('<iframe src="https://player.example.net/embed"></iframe>', BASE))
        self.assertEqual(scan.hosts, {'player.example.net'})

    def test_nested_upstream_parameter_is_not_a_requested_host(self):
        page = app.Page('<img data-src="https://gimg2.baidu.com/gimg?src=image.tmdb.org/a.jpg"><a href="https://unrelated.org">link</a>', BASE)
        scan = app.Scanner(CONFIG)
        scan.collect(page)
        self.assertEqual(scan.hosts, {'gimg2.baidu.com'})

    def test_site_json_and_legacy_encoded_player(self):
        url = 'https://video.example.org/第一集/index.m3u8'
        configs = [
            ('config', {'url':url}),
            ('player_aaaa', {'url':base64.b64encode(app.unquote(url).encode()).decode(), 'encrypt':2}),
            ('player_aaaa', {'url':'https%3A%2F%2Fvideo.example.org%2Fa.m3u8', 'encrypt':1}),
        ]
        for name, value in configs:
            page = app.Page('<script>var '+name+'='+json.dumps(value)+';</script>', BASE)
            self.assertEqual(app.hostname(list(app.player_configs(page))[0]), 'video.example.org')

    def test_invalid_config_is_skipped_without_execution(self):
        page = app.Page('<script>var config={"url":"!","encrypt":2}; var player_x={"url":"https://cdn.example.org/a.m3u8"};</script>', BASE)
        self.assertEqual(list(app.player_configs(page)), ['https://cdn.example.org/a.m3u8'])

    def test_reject_invalid_and_non_domain_hosts(self):
        for url in ['file:///tmp/x', 'https://127.0.0.1/a', 'https://[::1]/a', 'https://user:pass@example.org', 'https://bad host.org', 'https://[invalid']:
            self.assertIsNone(app.hostname(url))

    def test_suffix_boundaries_and_stable_cumulative_rules(self):
        initial = ['DOMAIN-SUFFIX,cdn.example.org','DOMAIN,old.example.net']
        merged = app.merge_rules(initial, {'a.cdn.example.org','notcdn.example.org'})
        self.assertEqual(merged, sorted(initial+['DOMAIN,notcdn.example.org']))
        self.assertEqual(merged, app.merge_rules(merged, {'a.cdn.example.org'}))


class ScanTests(unittest.TestCase):
    def test_rate_limit_stops_later_requests(self):
        requests = []
        def limited(url):
            requests.append(url)
            raise RuntimeError('site.example.org: HTTP 429, curl 0')
        scan = app.Scanner(CONFIG, limited)
        self.assertIsNone(scan.read(BASE))
        self.assertIsNone(scan.read(BASE+'voddetail/1.html'))
        self.assertEqual(requests, [BASE])
        self.assertTrue(scan.limited.is_set())

    def test_complete_scan_follows_playlist_redirect_and_keys(self):
        scan = app.Scanner(CONFIG, fixture_fetch)
        hosts = scan.scan(date(2026,10,13))
        self.assertTrue({'redirect.example.org','key.example.net','segment.example.net','video.example.org','image.example.org'} <= hosts)
        self.assertEqual(scan.counts['plays_parsed'], 6)

    def test_403_manifest_still_keeps_player_confirmed_domain(self):
        def blocked(url):
            if url.endswith('.m3u8'):
                raise RuntimeError('HTTP 403')
            return fixture_fetch(url)
        scan = app.Scanner(CONFIG, blocked)
        self.assertIn('video.example.org', scan.scan(date(2026,10,13)))
        self.assertIn('HTTP 403', scan.errors)

    def test_challenge_page_does_not_overwrite_prior_subscription(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'data').mkdir()
            (root/'data/domains.json').write_text(json.dumps({'rules':['DOMAIN,old.example.org']}))
            (root/'config.json').write_text(json.dumps(CONFIG))
            app.save_rules(['DOMAIN,old.example.org'],root/'rules')
            before={p:p.read_bytes() for p in [root/'data/domains.json',root/'rules/Vidhub.list',root/'rules/Vidhub-DIRECT.txt']}
            with patch.object(sys,'argv',['update_rules','--root',folder]), patch.object(app,'fetch',return_value=('<title>Just a moment...</title>',BASE)):
                with self.assertRaises(SystemExit) as result:
                    app.main()
            self.assertEqual(result.exception.code,1)
            for path, body in before.items():
                self.assertEqual(path.read_bytes(), body)
            self.assertIn('失败',(root/'reports/summary.md').read_text())

    def test_low_player_success_rate_is_failure(self):
        def missing(url):
            if '/player/' in url:
                return '<html>No player config</html>', url
            return fixture_fetch(url)
        with self.assertRaisesRegex(RuntimeError,'成功率'):
            app.Scanner(CONFIG,missing).scan(date(2026,10,13))

    def test_outputs_do_not_change_when_domains_are_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'data').mkdir()
            (root/'data/domains.json').write_text(json.dumps({'rules':['DOMAIN,old.example.org']}))
            (root/'config.json').write_text(json.dumps(CONFIG))
            with patch.object(sys,'argv',['update_rules','--root',folder]), patch.object(app,'fetch',side_effect=fixture_fetch):
                app.main()
                paths=[root/'data/domains.json',root/'rules/Vidhub.list',root/'rules/Vidhub-DIRECT.txt']
                before=[p.read_bytes() for p in paths]
                app.main()
                self.assertEqual(before,[p.read_bytes() for p in paths])
            self.assertIn('DOMAIN,old.example.org',(root/'rules/Vidhub.list').read_text())


if __name__ == '__main__':
    unittest.main()
