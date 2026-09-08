#!/usr/bin/env python3
"""Merge public M3U sources and check streams. Python 3.10+, no dependencies."""
import argparse
import concurrent.futures
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
GEO = re.compile(r'geo[ -]?(?:blocked|restricted)|Ⓖ', re.I)
QUALITY = re.compile(r'(?i)(?:sd|hd|fhd|uhd|4k|8k|\d{3,4}p)')


@dataclass
class Entry:
    info: str
    name: str
    attrs: dict
    options: list
    url: str
    source: str
    geo: bool


def split_info(line):
    """The display-name delimiter is the first comma outside quoted attributes."""
    quoted = False
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ',' and not quoted:
            return line[:i], line[i + 1:].strip()
    raise ValueError('EXTINF has no display-name separator')


def parse_playlist(body, source, base_url, force_geo=False):
    if not body.lstrip('\ufeff \r\n').startswith('#EXTM3U'):
        raise ValueError('Source is not an extended M3U playlist')
    entries, warnings = [], []
    pending = None
    options = []
    for line_no, raw in enumerate(body.splitlines(), 1):
        line = raw.strip().lstrip('\ufeff')
        if line.startswith('#EXTINF:'):
            if pending:
                warnings.append(f'Line {line_no}: previous entry has no URL')
            try:
                head, name = split_info(line)
                pending = (line, name, dict(ATTR.findall(head)))
            except ValueError as exc:
                pending = None
                warnings.append(f'Line {line_no}: {exc}')
            options = []
        elif line.startswith('#'):
            if pending:
                options.append(line)
        elif line and pending:
            info, name, attrs = pending
            url = urllib.parse.urljoin(base_url, line)
            entries.append(Entry(info, name, attrs, options[:], url, source,
                                 force_geo or bool(GEO.search(info))))
            pending = None
            options = []
    if pending:
        warnings.append('Final entry has no URL')
    return entries, warnings


def normalized_name(name):
    name = re.sub(r'\[(?:not 24/7|geo[ -]?(?:blocked|restricted))\]', '', name, flags=re.I)
    name = re.sub(r'\((?:\d{3,4}p|sd|hd|fhd|uhd|4k|8k)\)', '', name, flags=re.I)
    name = re.sub(r'\s+(?:SD|HD|FHD|UHD|4K|8K)$', '', name.strip(), flags=re.I)
    name = name.replace('Ⓖ', '').replace('Ⓢ', '').replace('Ⓨ', '').replace('ı', 'i')
    name = unicodedata.normalize('NFKD', name.casefold())
    return ''.join(c for c in name if c.isalnum() and not unicodedata.combining(c))


def channel_id(entry):
    ident = entry.attrs.get('tvg-id', '').strip().casefold()
    if ident in ('', 'ext', 'none', 'null', 'unknown', '0', '-1'):
        return ''
    # Preserve regional editions such as @Turkiye; collapse quality variants only.
    base, sep, suffix = ident.rpartition('@')
    return base if sep and QUALITY.fullmatch(suffix) else ident


def group_channels(entries):
    ids_by_name = defaultdict(set)
    for entry in entries:
        if channel_id(entry):
            ids_by_name[normalized_name(entry.name)].add(channel_id(entry))
    groups = {}
    for entry in entries:
        name = normalized_name(entry.name)
        ident = channel_id(entry)
        if not ident and len(ids_by_name[name]) == 1:
            ident = next(iter(ids_by_name[name]))
        key = 'id:' + ident if ident else 'name:' + name
        groups.setdefault(key, []).append(entry)
    return groups


def request_target(entry):
    url, sep, query = entry.url.partition('|')
    headers = {'User-Agent': 'Mozilla/5.0'}
    mappings = {'http-user-agent': 'User-Agent', 'http-referrer': 'Referer',
                'http-referer': 'Referer', 'http-origin': 'Origin', 'http-cookie': 'Cookie'}
    for key, value in entry.attrs.items():
        if key in mappings:
            headers[mappings[key]] = value
    for option in entry.options:
        if option.startswith('#EXTVLCOPT:'):
            key, _, value = option[len('#EXTVLCOPT:'):].partition('=')
            if key in mappings:
                headers[mappings[key]] = value
        elif option.startswith('#KODIPROP:inputstream.adaptive.stream_headers='):
            query_part = option.split('=', 1)[1]
            headers.update(dict(urllib.parse.parse_qsl(query_part)))
    if sep:
        headers.update(dict(urllib.parse.parse_qsl(query)))
    return url, headers


def fetch(url, headers, timeout, limit, byte_range=False):
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('Only HTTP(S) streams can be checked')
    request_headers = dict(headers)
    if byte_range:
        request_headers['Range'] = f'bytes=0-{limit - 1}'
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=request_headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if byte_range and exc.code == 416:
            return fetch(url, headers, timeout, limit, False)
        raise
    with response:
        return response.read(limit), response.geturl(), response.headers.get('Content-Type', '')


def check_stream(url, headers, timeout, depth=0, seen=None):
    seen = set() if seen is None else set(seen)
    if depth > 4 or url in seen:
        raise ValueError('Manifest nesting limit or cycle')
    seen.add(url)
    body, resolved, content_type = fetch(url, headers, timeout, 262144)
    if not body:
        raise ValueError('Empty response')
    if body.lstrip().startswith(b'#EXTM3U'):
        lines = [line.strip() for line in body.decode('utf-8-sig').splitlines() if line.strip()]
        uris = [line for line in lines if not line.startswith('#')]
        if not uris:
            raise ValueError('Manifest has no streams or media segments')
        if any(line.startswith('#EXT-X-STREAM-INF:') for line in lines):
            errors = []
            # Prefer the first advertised rendition, trying alternatives on failure.
            for uri in uris[:3]:
                try:
                    return check_stream(urllib.parse.urljoin(resolved, uri), headers, timeout, depth + 1, seen)
                except (OSError, ValueError) as exc:
                    errors.append(str(exc))
            raise ValueError('No accessible rendition: ' + '; '.join(errors))
        if not any(line.startswith('#EXTINF:') for line in lines):
            raise ValueError('Not an HLS media playlist')
        # Check a recent segment, avoiding the oldest segment near live-window expiry.
        segment = urllib.parse.urljoin(resolved, uris[-2] if len(uris) > 1 else uris[-1])
        sample, _, kind = fetch(segment, headers, timeout, 1024, byte_range=True)
        if not sample or 'text/html' in kind or sample.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            raise ValueError('Media segment is empty or an HTML error page')
        return 'HLS manifest and media segment reachable'
    if content_type.startswith(('video/', 'audio/')) and 'mpegurl' not in content_type:
        return 'Direct media endpoint reachable'
    raise ValueError('Response is not HLS or recognizable direct audio/video')


def validate(entry, timeout, retries):
    if entry.geo:
        return {'status': 'geo_skipped', 'reason': 'Explicit geo restriction; kept without probing'}
    url, headers = request_target(entry)
    for attempt in range(retries + 1):
        try:
            return {'status': 'reachable', 'reason': check_stream(url, headers, timeout)}
        except (OSError, ValueError) as exc:
            reason = f'{type(exc).__name__}: {exc}'
            # Retry transient network/server errors, not ordinary authorization failures.
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500 and exc.code not in (408, 429):
                break
            if attempt < retries:
                time.sleep(0.3)
    return {'status': 'unreachable', 'reason': reason}


def probe_key(entry):
    url, headers = request_target(entry)
    return url, tuple(sorted((k.lower(), v) for k, v in headers.items())), entry.geo


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, default=BASE / 'sources.json')
    parser.add_argument('--output', type=Path, default=BASE / 'playlist.m3u8')
    parser.add_argument('--report', type=Path, default=BASE / 'validation-report.json')
    parser.add_argument('--workers', type=int, default=16)
    parser.add_argument('--timeout', type=float, default=8, help='Seconds per network operation')
    parser.add_argument('--retries', type=int, default=1)
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0 or args.retries < 0:
        parser.error('workers/timeout must be positive; retries must be nonnegative')
    config = json.loads(args.sources.read_text(encoding='utf-8'))
    entries, source_reports = [], []
    for source in config['sources']:
        if not source.get('enabled', True):
            continue
        name, url = source['name'], source['url']
        print(f'Fetching {name}...', flush=True)
        try:
            body, resolved, _ = fetch(url, {'User-Agent': 'Mozilla/5.0'}, args.timeout, 10000001)
            if len(body) > 10000000:
                raise ValueError('Source exceeds 10 MB limit')
            parsed, warnings = parse_playlist(body.decode('utf-8-sig'), name, resolved, source.get('geo_restricted', False))
            if not parsed:
                raise ValueError('Source contains no channels')
            entries.extend(parsed)
            source_reports.append({'name': name, 'url': url, 'status': 'loaded', 'entries': len(parsed), 'warnings': warnings})
        except (OSError, ValueError) as exc:
            source_reports.append({'name': name, 'url': url, 'status': 'failed', 'reason': str(exc)})
            print(f'  Failed: {exc}', file=sys.stderr)
    groups = group_channels(entries)
    unique = {probe_key(entry): entry for entry in entries}
    print(f'{len(entries)} entries; {len(groups)} channel groups; {len(unique)} distinct stream checks.', flush=True)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(validate, entry, args.timeout, args.retries): key for key, entry in unique.items()}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            results[futures[future]] = future.result()
            if i % 20 == 0 or i == len(futures):
                print(f'Checked {i}/{len(futures)}', flush=True)
    selected, channel_reports, used_targets = [], [], set()
    for key, candidates in groups.items():
        chosen = None
        for status in ('reachable', 'geo_skipped'):
            chosen = next((entry for entry in candidates if results[probe_key(entry)]['status'] == status), None)
            if chosen:
                break
        state = 'dropped'
        if chosen:
            target = probe_key(chosen)[:2]
            if target in used_targets:
                state = 'duplicate_stream'
            else:
                used_targets.add(target)
                selected.append(chosen)
                state = results[probe_key(chosen)]['status']
        channel_reports.append({'channel': key, 'name': candidates[0].name, 'status': state,
                                'selected_url': chosen.url if chosen else None,
                                'candidates': [{'source': e.source, 'url': e.url, **results[probe_key(e)]} for e in candidates]})
    geo_count = sum(e.geo for e in selected)
    summary = {'source_entries': len(entries), 'channel_groups': len(groups), 'distinct_checks': len(unique),
               'output_channels': len(selected), 'reachable': len(selected) - geo_count,
               'geo_retained': geo_count, 'dropped_groups': sum(c['status'] == 'dropped' for c in channel_reports),
               'duplicate_stream_groups': sum(c['status'] == 'duplicate_stream' for c in channel_reports),
               'failed_sources': sum(s['status'] == 'failed' for s in source_reports)}
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'summary': summary,
              'sources': source_reports, 'channels': channel_reports}
    atomic_write(args.report, json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if not selected:
        print('No usable entries. Existing output was left unchanged; see report.', file=sys.stderr)
        return 1
    output = ['#EXTM3U']
    for entry in selected:
        output.extend([entry.info, *entry.options, entry.url])
    atomic_write(args.output, '\n'.join(output) + '\n')
    print(json.dumps(summary, indent=2))
    print(f'Wrote {args.output}\nReport: {args.report}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
