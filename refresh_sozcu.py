#!/usr/bin/env python3
"""Refresh only Sözcü, preserving other channels and their validation dates."""
import argparse
import json
from datetime import datetime, timezone

import build_playlist as b
from youtube_live import close_staging, publish_selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--timeout', type=float, default=12)
    args = parser.parse_args()
    ffmpeg = b.find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        parser.error('FFmpeg is required')
    config = json.loads((b.BASE / 'sources.json').read_text())
    source = next(s for s in config['sources'] if s.get('type') == 'youtube' and s['tvg_id'] == 'SozcuTV.tr')
    try:
        entries, _, _ = b.load_source(source, args.timeout)
        results = {b.probe_key(e): b.validate(e, args.timeout, 0, ffmpeg) for e in entries}
        selected, _ = b.select_streams(entries, results, 2, set())
        if not selected:
            print(json.dumps({'status': 'failed', 'playlist': 'unchanged',
                              'checks': [{'name': e.name, **results[b.probe_key(e)]} for e in entries]}, indent=2))
            return 1
        playlist_path = b.BASE / 'playlist.m3u8'
        old, warnings = b.parse_playlist(playlist_path.read_text(), 'Existing output',
                                        'https://mehmet-oner.github.io/iptvs/playlist.m3u8')
        if warnings or not old:
            raise ValueError('Existing playlist cannot be safely refreshed')
        updated = []
        inserted = False
        for entry in old:
            if b.channel_id(entry) == 'sozcutv.tr':
                if not inserted:
                    updated.extend(selected)
                    inserted = True
            else:
                updated.append(entry)
        if not inserted:
            updated.extend(selected)
        output = ['#EXTM3U']
        for entry in updated:
            output.extend([entry.info, *entry.options, entry.url])
        now = datetime.now(timezone.utc).isoformat()
        report_path = b.BASE / 'validation-report.json'
        report = json.loads(report_path.read_text())
        fresh_report = {'channel': 'id:sozcutv.tr', 'name': 'Sözcü TV', 'status': 'video_decoded',
                        'checked_at': now, 'selected_url': selected[0].url,
                        'selected_source': source['name'],
                        'selected_streams': [{'url': e.url, 'source': e.source} for e in selected],
                        'candidates': [{'source': e.source, 'url': e.url, **results[b.probe_key(e)]} for e in entries]}
        report['channels'] = [c for c in report['channels'] if c['channel'].removeprefix('id:') != 'sozcutv.tr'] + [fresh_report]
        report['last_channel_refresh_at'] = now
        report['refresh_note'] = 'Only Sözcü was checked at last_channel_refresh_at. Other evidence dates remain checked_at or the per-channel checked_at.'
        report['summary']['output_channels'] = len(updated)
        report['summary']['video_decoded'] = sum(len(c.get('selected_streams', [])) or 1
                                                for c in report['channels'] if c['status'] == 'video_decoded')
        publish_selected(selected)
        b.atomic_write(playlist_path, '\n'.join(output) + '\n')
        b.atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(f'Refreshed {len(selected)} live Sözcü streams; {len(updated)} playlist entries.')
        return 0
    except (OSError, ValueError) as exc:
        print(f'Refresh failed; existing playlist left unchanged: {exc}')
        return 1
    finally:
        close_staging()


if __name__ == '__main__':
    raise SystemExit(main())
