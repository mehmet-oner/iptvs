#!/usr/bin/env python3
"""Merge public TV sources and validate video with Python 3 and FFmpeg."""
import argparse
import concurrent.futures
import gzip
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
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
    require_live_progress: bool = False
    validation_url: str = None


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
    return entry.validation_url or url, headers


def fetch(url, headers, timeout, limit, byte_range=False):
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('Only HTTP(S) streams can be checked')
    request_headers = dict(headers)
    request_headers.setdefault('Accept-Encoding', 'gzip')
    if byte_range:
        request_headers['Range'] = f'bytes=0-{limit - 1}'
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=request_headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if byte_range and exc.code == 416:
            return fetch(url, headers, timeout, limit, False)
        raise
    with response:
        encoding = response.headers.get('Content-Encoding', '').lower().strip()
        if encoding == 'gzip':
            # Bound decompressed data too, including when a server ignores Range.
            with gzip.GzipFile(fileobj=response) as decoded:
                body = decoded.read(limit)
        elif encoding in ('', 'identity'):
            body = response.read(limit)
        else:
            raise ValueError(f'Unsupported HTTP content encoding: {encoding}')
        return body, response.geturl(), response.headers.get('Content-Type', '')


def validate_media_sample(sample, content_type):
    """Reject common non-media responses served from a segment-looking URL."""
    if not sample:
        raise ValueError('Media segment is empty')
    stripped = sample.lstrip().lower()
    kind = content_type.partition(';')[0].strip().lower()
    if kind in ('text/html', 'application/json', 'application/xml', 'text/xml'):
        raise ValueError(f'Media segment returned {kind}')
    if stripped.startswith((b'#extm3u', b'<!doctype html', b'<html', b'<?xml', b'{"', b'[{')):
        raise ValueError('Media segment returned a playlist or error document')


def check_stream(url, headers, timeout, depth=0, seen=None):
    seen = set() if seen is None else set(seen)
    if depth > 4 or url in seen:
        raise ValueError('Manifest nesting limit or cycle')
    seen.add(url)
    body, resolved, content_type = fetch(url, headers, timeout, 262144)
    if not body:
        raise ValueError('Empty response')
    if body.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'#EXTM3U'):
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
        sample, _, kind = fetch(segment, headers, timeout, 4096, byte_range=True)
        validate_media_sample(sample, kind)
        return 'HLS manifest and media segment reachable'
    if content_type.startswith(('video/', 'audio/')) and 'mpegurl' not in content_type:
        validate_media_sample(body, content_type)
        return 'Direct media endpoint reachable'
    raise ValueError('Response is not HLS or recognizable direct audio/video')


def inspect_hls_freshness(url, headers, timeout, depth=0):
    """Reject clearly stale dated HLS windows; undated streams remain unknown."""
    if depth > 4:
        raise ValueError('Manifest nesting limit')
    body, resolved, _ = fetch(url, headers, timeout, 262144)
    if not body.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'#EXTM3U'):
        return {'status': 'unknown', 'reason': 'No HLS program timestamps'}
    lines = [line.strip() for line in body.decode('utf-8-sig').splitlines() if line.strip()]
    if any(line.startswith('#EXT-X-STREAM-INF:') for line in lines):
        uri = next((line for line in lines if not line.startswith('#')), None)
        if uri:
            return inspect_hls_freshness(urllib.parse.urljoin(resolved, uri), headers, timeout, depth + 1)
    cursor, latest, duration = None, None, 0
    for line in lines:
        if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            try:
                cursor = datetime.fromisoformat(line.split(':', 1)[1].replace('Z', '+00:00'))
                if cursor.tzinfo is None:
                    cursor = None
            except ValueError:
                cursor = None
        elif line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',')[0])
        elif not line.startswith('#') and cursor:
            cursor += timedelta(seconds=duration)
            latest = cursor if latest is None else max(latest, cursor)
    if latest is None:
        return {'status': 'unknown', 'reason': 'No HLS program timestamps'}
    age = (datetime.now(timezone.utc) - latest).total_seconds()
    if age > 900:
        raise ValueError(f'Stale HLS: latest dated media is {int(age)} seconds old (limit 900)')
    if age < -900:
        raise ValueError('HLS program timestamps are more than 15 minutes in the future')
    return {'status': 'recent', 'latest_segment_end': latest.isoformat(), 'age_seconds': round(age, 1)}


def inspect_hls_progress(url, headers, timeout, depth=0):
    """Observe a moving HLS window; a decodable frozen window is not live."""
    if depth > 4:
        raise ValueError('Manifest nesting limit')
    body, resolved, _ = fetch(url, headers, timeout, 262144)
    lines = body.decode('utf-8-sig').splitlines()
    if not body.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'#EXTM3U'):
        raise ValueError('Live progress check requires HLS')
    if any(line.startswith('#EXT-X-STREAM-INF:') for line in lines):
        uri = next((line.strip() for line in lines if line.strip() and not line.startswith('#')), None)
        if not uri:
            raise ValueError('HLS master has no rendition')
        return inspect_hls_progress(urllib.parse.urljoin(resolved, uri), headers, timeout, depth + 1)

    def window(payload):
        text = payload.decode('utf-8-sig')
        if '#EXT-X-ENDLIST' in text:
            raise ValueError('Live channel returned an ended HLS playlist')
        sequence = re.search(r'^#EXT-X-MEDIA-SEQUENCE:(\d+)', text, re.M)
        uris = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')]
        if not uris:
            raise ValueError('Live HLS has no media segments')
        # Ignore refreshed query tokens: those do not prove new media appeared.
        return (int(sequence.group(1)) if sequence else None,
                urllib.parse.urlsplit(uris[-1]).path)

    initial = window(body)
    target = re.search(r'^#EXT-X-TARGETDURATION:(\d+)', body.decode('utf-8-sig'), re.M)
    target_seconds = int(target.group(1)) if target else 6
    if target_seconds > 20:
        raise ValueError('Cannot verify live progress within the 60-second observation limit')
    interval = max(3, target_seconds * 1.5)
    for attempt in range(2):
        time.sleep(interval)
        current_body, _, _ = fetch(resolved, {**headers, 'Cache-Control': 'no-cache'}, timeout, 262144)
        current = window(current_body)
        sequence_advanced = initial[0] is not None and current[0] is not None and current[0] > initial[0]
        if sequence_advanced or (current[1] != initial[1] and (initial[0] is None or current[0] is None)):
            return {'status': 'advancing', 'observation_seconds': interval * (attempt + 1),
                    'initial_sequence': initial[0], 'final_sequence': current[0]}
    raise ValueError('Frozen HLS: no new media segments appeared during the live progress check')


def decode_stream(url, headers, timeout, ffmpeg, seconds=4):
    """Decode actual frames and optional audio, with a hard process deadline."""
    if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('Only HTTP(S) streams can be checked')
    if any('\r' in k + v or '\n' in k + v for k, v in headers.items()):
        raise ValueError('Invalid newline in HTTP headers')
    command = [ffmpeg, '-nostdin', '-hide_banner', '-nostats', '-loglevel', 'info',
               '-rw_timeout', str(int(timeout * 1000000)),
               '-protocol_whitelist', 'http,https,tcp,tls,crypto',
               '-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items()),
               '-threads', '1', '-i', url, '-t', str(seconds),
               '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn',
               '-threads', '1', '-progress', 'pipe:1', '-f', 'null', '-']
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors='replace', timeout=max(25, timeout * 2 + seconds + 8))
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Video decode exceeded the wall-clock timeout') from exc
    output = result.stdout
    frames = max([int(n) for n in re.findall(r'^frame=\s*(\d+)', output, re.M)] or [0])
    duration = max([int(n) / 1000000 for n in re.findall(r'^out_time_us=(\d+)', output, re.M)] or [0])
    if result.returncode != 0 or frames < 8 or duration < seconds * 0.75:
        # Keep reports concise and avoid copying expiring URLs from verbose logs.
        detail = 'decoder error' if result.returncode else 'insufficient decoded video frames/duration'
        problem = re.search(r'(HTTP error \d{3}[^\r\n]*|Server returned \d{3}[^\r\n]*|'
                            r'Connection (?:timed out|refused)|Invalid data found when processing input|'
                            r'matches no streams|Protocol not on whitelist)', output)
        if problem:
            detail += ': ' + problem.group(1)[:180]
        raise ValueError(f'{detail} (exit {result.returncode}, {frames} frames, {duration:.2f}s)')
    video = re.search(r'Stream #0:[^\n]*Video: (\w+)[^\n]*', output)
    size = re.search(r'\b(\d{2,5})x(\d{2,5})\b', video.group(0)) if video else None
    audio = re.search(r'Stream #0:[^\n]*Audio: (\w+)', output)
    return {'frames': frames, 'decoded_seconds': round(duration, 3),
            'video_codec': video.group(1) if video else None,
            'width': int(size.group(1)) if size else None,
            'height': int(size.group(2)) if size else None,
            'audio_codec': audio.group(1) if audio else None}


def validate(entry, timeout, retries, ffmpeg=None, decode_seconds=4):
    if entry.geo:
        return {'status': 'geo_skipped', 'reason': 'Source labels geo restriction; retained without probing; playback unverified'}
    url, headers = request_target(entry)
    for attempt in range(retries + 1):
        try:
            if ffmpeg:
                freshness = inspect_hls_freshness(url, headers, timeout)
                if entry.require_live_progress:
                    freshness['progress'] = inspect_hls_progress(url, headers, timeout)
                media = decode_stream(url, headers, timeout, ffmpeg, decode_seconds)
                if entry.validation_url and media.get('audio_codec') != 'aac':
                    raise ValueError('Generated YouTube master must decode AAC audio')
                return {'status': 'video_decoded', 'reason': 'Decoded video and available audio',
                        'media': media, 'freshness': freshness}
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
    return url, tuple(sorted((k.lower(), v) for k, v in headers.items())), entry.geo, entry.require_live_progress


def load_source(source, timeout):
    """Load a channel list or describe a single stable HLS entry point."""
    name, url = source['name'], source['url']
    if source.get('type') == 'youtube':
        from youtube_live import load_youtube_source
        return load_youtube_source(source, timeout)
    if source.get('type', 'playlist') == 'stream':
        attrs = {'tvg-id': source['tvg_id'], 'group-title': source.get('group', 'TV')}
        channel_name = source.get('channel_name', name)
        metadata = ' '.join(f'{k}="{v}"' for k, v in attrs.items())
        info = f'#EXTINF:-1 {metadata},{channel_name}'
        return [Entry(info, channel_name, attrs, [], url, name,
                      source.get('geo_restricted', False))], [], 0
    body, resolved, _ = fetch(url, {'User-Agent': 'Mozilla/5.0'}, timeout, 10000001)
    if len(body) > 10000000:
        raise ValueError('Source exceeds 10 MB limit')
    entries, warnings = parse_playlist(body.decode('utf-8-sig'), name, resolved,
                                       source.get('geo_restricted', False))
    if not entries:
        raise ValueError('Source contains no channels')
    allowed = source.get('include_groups')
    included_ids = {ident.casefold() for ident in source.get('include_ids', [])}
    aliases = {k.casefold(): v for k, v in source.get('id_aliases', {}).items()}
    for entry in entries:
        replacement = aliases.get(channel_id(entry))
        if replacement:
            entry.attrs['tvg-id'] = replacement
            entry.info = re.sub(r'tvg-id="[^"]*"', lambda _: f'tvg-id="{replacement}"', entry.info, count=1)
    excluded_ids = {ident.casefold() for ident in source.get('exclude_ids', [])}
    excluded_urls = source.get('exclude_url_patterns', [])
    excluded_names = source.get('exclude_name_patterns', [])
    filtered = [e for e in entries
                if (allowed is None or e.attrs.get('group-title', '') in allowed)
                and (not included_ids or channel_id(e) in included_ids)
                and channel_id(e) not in excluded_ids
                and not any(re.search(pattern, e.url, re.I) for pattern in excluded_urls)
                and not any(re.search(pattern, e.name, re.I) for pattern in excluded_names)]
    return filtered, warnings, len(entries) - len(filtered)


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def select_streams(candidates, results, limit, used_targets):
    """Keep intentional validated alternatives while deduplicating stream URLs."""
    usable = [entry for status in ('video_decoded', 'reachable') for entry in candidates
              if results[probe_key(entry)]['status'] == status]
    if not usable:
        usable = [entry for entry in candidates if results[probe_key(entry)]['status'] == 'geo_skipped'][:1]
    chosen = []
    for entry in usable:
        target = probe_key(entry)[:2]
        if target in used_targets:
            continue
        used_targets.add(target)
        chosen.append(entry)
        if len(chosen) >= limit:
            break
    return chosen, bool(usable)


def find_ffmpeg(executable):
    found = shutil.which(executable)
    if found or executable != 'ffmpeg':
        return found
    try:
        import imageio_ffmpeg
        found = imageio_ffmpeg.get_ffmpeg_exe()
        return found if Path(found).is_file() else None
    except (ImportError, RuntimeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, default=BASE / 'sources.json')
    parser.add_argument('--output', type=Path, default=BASE / 'playlist.m3u8')
    parser.add_argument('--report', type=Path, default=BASE / 'validation-report.json')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--timeout', type=float, default=12, help='Seconds per network operation')
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--validation', choices=('decode', 'reachability'), default='decode')
    parser.add_argument('--ffmpeg', default='ffmpeg', help='FFmpeg executable (required for default decode mode)')
    parser.add_argument('--decode-seconds', type=float, default=4)
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0 or args.retries < 0 or args.decode_seconds < 1:
        parser.error('workers/timeout must be positive; retries must be nonnegative')
    ffmpeg = find_ffmpeg(args.ffmpeg) if args.validation == 'decode' else None
    if args.validation == 'decode' and not ffmpeg:
        parser.error('FFmpeg is required for video validation; install it or supply --ffmpeg /path/to/ffmpeg')
    config = json.loads(args.sources.read_text(encoding='utf-8'))
    entries, source_reports = [], []
    for source in config['sources']:
        if not source.get('enabled', True):
            continue
        if source.get('type', 'playlist') == 'playlist':
            defaults = config.get('playlist_defaults', {})
            source = {**defaults, **source,
                      'id_aliases': {**defaults.get('id_aliases', {}), **source.get('id_aliases', {})},
                      **{key: defaults.get(key, []) + source.get(key, [])
                         for key in ('exclude_ids', 'exclude_url_patterns', 'exclude_name_patterns')}}
        name, url = source['name'], source['url']
        print(f'Fetching {name}...', flush=True)
        try:
            parsed, warnings, filtered_out = load_source(source, args.timeout)
            entries.extend(parsed)
            source_reports.append({'name': name, 'url': url, 'status': 'loaded', 'entries': len(parsed),
                                   'filtered_out': filtered_out, 'warnings': warnings})
        except (OSError, ValueError) as exc:
            source_reports.append({'name': name, 'url': url, 'status': 'failed', 'reason': str(exc)})
            print(f'  Failed: {exc}', file=sys.stderr)
    live_ids = {ident.casefold() for ident in config.get('require_live_progress', [])}
    for entry in entries:
        entry.require_live_progress = channel_id(entry) in live_ids
    groups = group_channels(entries)
    unique = {probe_key(entry): entry for entry in entries}
    print(f'{len(entries)} entries; {len(groups)} channel groups; {len(unique)} distinct stream checks.', flush=True)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(validate, entry, args.timeout, args.retries, ffmpeg, args.decode_seconds): key
                   for key, entry in unique.items()}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            results[futures[future]] = future.result()
            if i % 20 == 0 or i == len(futures):
                print(f'Checked {i}/{len(futures)}', flush=True)
    selected, channel_reports, used_targets = [], [], set()
    limits = {'id:' + ident.casefold(): int(limit) for ident, limit in config.get('max_streams_per_channel', {}).items()}
    if any(limit < 1 or limit > 5 for limit in limits.values()):
        parser.error('max_streams_per_channel values must be between 1 and 5')
    for key, candidates in groups.items():
        choices, had_usable = select_streams(candidates, results, limits.get(key, 1), used_targets)
        chosen = choices[0] if choices else None
        state = results[probe_key(chosen)]['status'] if chosen else ('duplicate_stream' if had_usable else 'dropped')
        for number, entry in enumerate(choices, 1):
            if number > 1:
                label = entry.name + f' (Backup {number})'
                entry = replace(entry, name=label, info=entry.info.rsplit(',', 1)[0] + ',' + label)
            selected.append(entry)
        channel_reports.append({'channel': key, 'name': candidates[0].name, 'status': state,
                                'selected_url': chosen.url if chosen else None,
                                'selected_source': chosen.source if chosen else None,
                                'selected_streams': [{'url': e.url, 'source': e.source} for e in choices],
                                'candidates': [{'source': e.source, 'url': e.url, **results[probe_key(e)]} for e in candidates]})
    geo_count = sum(e.geo for e in selected)
    selected_ids = {channel_id(e) for e in selected}
    missing_required = [ident for ident in config.get('required_channels', [])
                        if ident.casefold() not in selected_ids]
    summary = {'source_entries': len(entries), 'channel_groups': len(groups), 'distinct_checks': len(unique),
               'output_channels': len(selected), 'video_decoded': len(selected) - geo_count if ffmpeg else 0,
               'reachability_only': len(selected) - geo_count if not ffmpeg else 0,
               'geo_retained': geo_count, 'dropped_groups': sum(c['status'] == 'dropped' for c in channel_reports),
               'duplicate_stream_groups': sum(c['status'] == 'duplicate_stream' for c in channel_reports),
               'failed_sources': sum(s['status'] == 'failed' for s in source_reports)}
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'validation': {'mode': args.validation, 'decode_seconds': args.decode_seconds if ffmpeg else None,
                             'geo_policy': 'Keep source-labelled geo restrictions without probing; unverified'},
              'summary': summary,
              'sources': source_reports, 'channels': channel_reports,
              'missing_required_channels': missing_required}
    atomic_write(args.report, json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if not selected or missing_required:
        reason = 'Missing required channels: ' + ', '.join(missing_required) if missing_required else 'No usable entries.'
        print(reason + ' Existing output was left unchanged; see report.', file=sys.stderr)
        return 1
    output = ['#EXTM3U']
    for entry in selected:
        output.extend([entry.info, *entry.options, entry.url])
    playlist_text = '\n'.join(output) + '\n'
    from youtube_live import publish_selected
    publish_selected(selected)
    atomic_write(args.output, playlist_text)
    print(json.dumps(summary, indent=2))
    print(f'Wrote {args.output}\nReport: {args.report}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
