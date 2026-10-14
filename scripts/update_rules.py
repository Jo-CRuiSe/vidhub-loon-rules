#!/usr/bin/env python3
"""Discover Vidhub resources without executing site scripts or downloading media."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ('.m3u8', '.mpd', '.mp4', '.m4s', '.ts')


def hostname(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            return None
        host = (parsed.hostname or '').encode('idna').decode().lower().rstrip('.')
    except (ValueError, UnicodeError):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return host if re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}', host) else None


def is_media(url):
    return urlsplit(url).path.lower().endswith(MEDIA)


class Page(HTMLParser):
    def __init__(self, body, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links, self.frames, self.assets, self.scripts = [], [], set(), []
        self.in_script = False
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a' and a.get('href'):
            self.links.append(urljoin(self.base, a['href']))
        if tag == 'iframe' and a.get('src'):
            self.frames.append(urljoin(self.base, a['src']))
        if tag in ('img', 'script', 'source', 'video', 'audio', 'embed'):
            for attr in ('src', 'data-src', 'data-original', 'poster'):
                if a.get(attr):
                    self.assets.add(urljoin(self.base, a[attr]))
            # Image candidates contain URLs plus optional width/density descriptors.
            for candidate in a.get('srcset', '').split(','):
                if candidate.strip():
                    self.assets.add(urljoin(self.base, candidate.strip().split()[0]))
        if tag == 'link' and a.get('href') and set(a.get('rel', '').split()) & {'stylesheet', 'icon', 'preload', 'modulepreload'}:
            self.assets.add(urljoin(self.base, a['href']))
        for match in re.findall(r'url\(\s*[\"\x27]?([^\s)\"\x27]+)', a.get('style', '')):
            self.assets.add(urljoin(self.base, match))
        if tag == 'script':
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)


def player_configs(page):
    decoder = json.JSONDecoder()
    for script in page.scripts:
        for match in re.finditer(r'\b(?:var|let|const)\s+(?:config|player_\w+)\s*=\s*(?=\{)', script):
            try:
                data, _ = decoder.raw_decode(script[match.end():])
                if not isinstance(data, dict) or not isinstance(data.get('url'), str):
                    continue
                url = data['url']
                encrypt = str(data.get('encrypt', 0))
                if encrypt == '1':
                    url = unquote(url)
                elif encrypt == '2':
                    url = unquote(base64.b64decode(url, validate=True).decode())
                if not url.strip():
                    continue
                yield urljoin(page.base, url)
                if isinstance(data.get('pic'), str):
                    yield urljoin(page.base, data['pic'])
            except (ValueError, UnicodeError, TypeError):
                continue


def playlist_refs(body, base):
    if not body.lstrip('\ufeff \n\r\t').startswith('#EXTM3U'):
        return []
    refs = [line.strip() for line in body.splitlines() if line.strip() and not line.startswith('#')]
    refs += re.findall(r'URI="([^"]+)"', body)
    return sorted({urljoin(base, ref) for ref in refs if hostname(urljoin(base, ref))})


def fetch(url):
    fetch_env = None
    proxy = os.environ.get('VIDHUB_PROXY', '').strip()
    if proxy:
        try:
            parsed_proxy = urlsplit(proxy)
            if parsed_proxy.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parsed_proxy.hostname:
                raise ValueError
            parsed_proxy.port
        except ValueError:
            raise RuntimeError('VIDHUB_PROXY 格式无效，请使用 HTTP/HTTPS/SOCKS5 代理 URL。') from None
        # Keep credentials out of argv, logs, and global environment; proxy only website reads.
        fetch_env = os.environ.copy()
        fetch_env.update(http_proxy=proxy, https_proxy=proxy, all_proxy=proxy)
        fetch_env.pop('no_proxy', None)
        fetch_env.pop('NO_PROXY', None)
    with tempfile.TemporaryDirectory() as temp:
        body = Path(temp) / 'body'
        response = subprocess.run([
            'curl', '--silent', '--show-error', '--location', '--max-redirs', '5',
            '--proto', '=http,https', '--proto-redir', '=http,https',
            '--connect-timeout', '5', '--max-time', '18', '--retry', '0',
            '--max-filesize', '2097152', '--user-agent', 'VidhubLoonRuleUpdater/1.0',
            '--output', str(body), '--write-out', '%{http_code}\n%{url_effective}', url,
        ], capture_output=True, text=True, timeout=45, env=fetch_env)
        parts = response.stdout.splitlines()
        code = int(parts[0]) if parts and parts[0].isdigit() else 0
        effective = parts[1] if len(parts) > 1 else url
        content = body.read_text(encoding='utf-8', errors='replace') if body.exists() else ''
        if response.returncode or not 200 <= code < 300:
            raise RuntimeError(f'{hostname(url)}: HTTP {code}, curl {response.returncode}')
        if not content.strip():
            raise RuntimeError(f'{hostname(url)}: empty response')
        return content, effective


class Scanner:
    def __init__(self, config, fetcher=None):
        self.config, self.fetcher = config, fetcher or fetch
        self.hosts, self.media, self.errors, self.counts = set(), {}, [], {}
        self.mode = 'full'
        self.active_base = None
        self.limited = threading.Event()
        self.request_lock = threading.Lock()
        self.next_request = 0.0

    def read(self, url):
        if self.limited.is_set():
            return None
        try:
            with self.request_lock:
                if self.limited.is_set():
                    return None
                delay = self.next_request - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self.next_request = time.monotonic() + self.config.get('request_interval_seconds', 0)
            return self.fetcher(url)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            self.errors.append(str(error))
            if 'HTTP 429' in str(error):
                self.limited.set()
            return None

    def collect(self, page):
        urls = page.assets | set(page.frames) | set(player_configs(page))
        for url in urls:
            host = hostname(url)
            if host:
                self.hosts.add(host)
                if is_media(url):
                    self.media.setdefault(host, url)

    def probe_manifests(self, samples):
        attempted, succeeded = 0, 0
        for sample in samples[:self.config['max_manifest_hosts']]:
            if self.limited.is_set():
                break
            if not hostname(sample) or not urlsplit(sample).path.lower().endswith('.m3u8'):
                continue
            attempted += 1
            visited, valid = set(), False
            for _ in range(3):
                if sample in visited:
                    break
                visited.add(sample)
                result = self.read(sample)
                if not result:
                    break
                refs = playlist_refs(*result)
                if not refs:
                    self.errors.append(f'{hostname(sample)}: 未返回可用 HLS 清单')
                    break
                valid = True
                self.hosts.add(hostname(sample))
                self.hosts.add(hostname(result[1]))
                self.hosts.update(filter(None, map(hostname, refs)))
                child = [u for u in refs if urlsplit(u).path.lower().endswith('.m3u8')]
                if not child:
                    break
                sample = child[0]
            succeeded += int(valid)
        self.counts.update(manifests_attempted=attempted, manifests_succeeded=succeeded)
        self.hosts.discard(None)
        return succeeded

    def scan(self, day):
        home, homepage, links = None, None, []
        addresses = [self.config['base_url']] + self.config.get('fallback_base_urls', [])
        for address in dict.fromkeys(addresses):
            if not hostname(address):
                raise ValueError('Invalid website address in config')
            candidate = self.read(address.rstrip('/') + '/')
            if not candidate:
                continue
            page = Page(*candidate)
            candidates = sorted({u for u in page.links if urlsplit(u).netloc == urlsplit(candidate[1]).netloc
                                 and re.fullmatch(r'/voddetail/\d+\.html', urlsplit(u).path)})
            if len(candidates) < 3:
                self.errors.append(f'{hostname(address)}: 首页不是可用的影片列表')
                continue
            home, homepage, links = candidate, page, candidates
            self.active_base = candidate[1]
            break
        if not home:
            samples = self.config.get('cdn_samples', [])
            if not samples or self.limited.is_set():
                raise RuntimeError('首页访问失败或被验证页拦截，保留既有规则。')
            self.mode = 'cdn_only'
            # Discard failed page observations; only fresh, usable HLS can add domains.
            self.hosts.clear()
            succeeded = self.probe_manifests([sample['playlist_url'] for sample in samples])
            minimum = self.config.get('cdn_sample_min_success', 3)
            if self.limited.is_set() or succeeded < minimum:
                raise RuntimeError(f'主站不可访问，CDN 清单也未达到检查门槛（{succeeded}/{minimum}），保留既有规则。')
            return self.hosts
        self.collect(homepage)
        self.hosts.add(hostname(home[1]))
        limit = self.config['max_movies']
        seeds = [urljoin(home[1], f'/voddetail/{video}.html') for video in self.config['seed_movies']][:limit]
        pool = [u for u in links if u not in seeds]
        # Rotate coverage while retaining historical samples.
        offset = (day.toordinal() * max(1, limit - len(seeds))) % max(1, len(pool))
        selected = seeds + (pool[offset:] + pool[:offset])[:max(0, limit-len(seeds))]
        plays, details_ok = set(), 0
        with ThreadPoolExecutor(self.config['workers']) as workers:
            for result in workers.map(self.read, selected):
                if not result:
                    continue
                page = Page(*result)
                self.collect(page)
                by_line = {}
                for url in page.links:
                    m = re.fullmatch(r'/vodplay/(\d+)-(\d+)-(\d+)\.html', urlsplit(url).path)
                    if m and urlsplit(url).netloc == urlsplit(home[1]).netloc:
                        by_line.setdefault((m[1], m[2]), {})[int(m[3])] = url
                if by_line:
                    details_ok += 1
                for episodes in by_line.values():
                    plays.add(episodes[min(episodes)])
                    plays.add(episodes[max(episodes)])
        plays = sorted(plays)[:self.config['max_play_pages']]
        self.counts.update(details_requested=len(selected), details_ok=details_ok, plays_requested=len(plays))
        if details_ok < min(3, len(selected)):
            raise RuntimeError('有效影片详情不足，保留既有规则。')

        def inspect_play(url):
            result = self.read(url)
            if not result:
                return []
            page = Page(*result)
            pages = [page]
            for frame in page.frames:
                if urlsplit(frame).netloc == urlsplit(result[1]).netloc and urlsplit(frame).path.startswith('/player/'):
                    embedded = self.read(frame)
                    if embedded:
                        pages.append(Page(*embedded))
            return pages

        parsed = 0
        with ThreadPoolExecutor(self.config['workers']) as workers:
            for pages in workers.map(inspect_play, plays):
                found = False
                for page in pages:
                    self.collect(page)
                    found |= any(is_media(u) for u in player_configs(page))
                parsed += int(found)
        self.counts['plays_parsed'] = parsed
        if self.limited.is_set():
            raise RuntimeError('站点返回 HTTP 429，停止请求并保留既有规则。')
        if parsed < min(3, len(plays)) or not plays or parsed / len(plays) < 0.5:
            raise RuntimeError('播放配置解析成功率不足，保留既有规则。')
        # Read one sample per CDN, follow bounded child playlists, never video segments.
        self.probe_manifests([sample for host, sample in sorted(self.media.items())])
        if self.limited.is_set():
            raise RuntimeError('站点返回 HTTP 429，停止请求并保留既有规则。')
        return self.hosts


def merge_rules(existing, hosts):
    rules = set(existing)
    for rule in rules:
        parts = rule.split(',')
        if len(parts) != 2 or parts[0] not in ('DOMAIN', 'DOMAIN-SUFFIX') or hostname('https://' + parts[1]) != parts[1]:
            raise ValueError(f'Invalid stored rule: {rule}')
    for host in sorted(hosts):
        if not any(host == r.split(',')[1] or (r.startswith('DOMAIN-SUFFIX,') and host.endswith('.'+r.split(',')[1])) for r in rules):
            rules.add('DOMAIN,' + host)
    return sorted(rules)


def save_rules(rules, output):
    output.mkdir(parents=True, exist_ok=True)
    header = '# Vidhub Loon rules. Generated automatically; historical domains retained.\n'
    for name, content in {
        'Vidhub.list': header + '\n'.join(rules) + '\n',
        'Vidhub-DIRECT.txt': header + '\n'.join(r+',DIRECT' for r in rules) + '\n',
    }.items():
        target = output/name
        temp = target.with_suffix(target.suffix+'.tmp')
        temp.write_text(content, encoding='utf-8')
        temp.replace(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--render-only', action='store_true')
    args = parser.parse_args()
    root = args.root
    state = root/'data/domains.json'
    existing = json.loads(state.read_text())['rules']
    if args.render_only:
        save_rules(merge_rules(existing, []), root/'rules')
        return
    config = json.loads((root/'config.json').read_text())
    sample_file = root/'data/cdn-samples.json'
    if sample_file.exists():
        config['cdn_samples'] = json.loads(sample_file.read_text())
    scan = Scanner(config)
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    reports = root/'reports'
    reports.mkdir(parents=True, exist_ok=True)
    ok, message, additions = False, '', []
    try:
        observed = scan.scan(now.date())
        rules = merge_rules(existing, observed)
        additions = sorted(set(rules)-set(existing))
        # Only mutate subscription/state after a successful scan.
        save_rules(rules, root/'rules')
        temp = state.with_suffix('.json.tmp')
        temp.write_text(json.dumps({'rules': rules}, indent=2)+'\n')
        temp.replace(state)
        ok = True
        coverage = '局部检查：主站不可访问，仅检查已确认的 CDN 清单；未扫描新影片线路。' if scan.mode == 'cdn_only' else '完整页面扫描。'
        message = f'{coverage} 发现 {len(observed)} 个域名，新增 {len(additions)} 条规则，累计 {len(rules)} 条。'
    except (RuntimeError, ValueError) as error:
        message = str(error)
    status = ('局部检查成功' if scan.mode == 'cdn_only' else '完整扫描成功') if ok else '失败'
    report = f'# Vidhub 检查结果\n\n时间：{now.isoformat()}\n\n状态：{status}\n\n{message}\n\n'
    if scan.active_base:
        report += f'实际扫描入口：{scan.active_base}\n\n'
    report += '\n'.join(f'- {key}: {value}' for key, value in scan.counts.items())
    report += '\n\n新增规则：\n\n' + ('\n'.join('- `'+r+'`' for r in additions) or '无')
    report += '\n\n请求/解析问题（包括 CDN 403；域名仍可由播放器配置确认）：\n\n'
    report += '\n'.join('- '+e for e in scan.errors[:80]) or '无'
    (reports/'summary.md').write_text(report+'\n')
    (reports/'observed-hosts.json').write_text(json.dumps(sorted(scan.hosts), indent=2)+'\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(report+'\n')
    print(message)
    if ok and scan.mode == 'cdn_only' and os.environ.get('GITHUB_ACTIONS'):
        print('::warning::主站仍受访问限制，本次只检查 CDN 样本，不代表全站线路已更新。')
    if not ok:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
