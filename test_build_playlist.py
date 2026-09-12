import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from build_playlist import channel_id, group_channels, parse_playlist, load_source


class PlaylistTests(unittest.TestCase):
    def test_source_filters_and_explicit_id_aliases(self):
        body = b'''#EXTM3U
#EXTINF:-1 tvg-id="sozcu.tr" group-title="Ulusal",Sozcu TV
https://example.org/sozcu.m3u8
#EXTINF:-1 tvg-id="Other.us" group-title="Foreign",Other
https://example.org/other.m3u8
'''
        source = {'name': 'test', 'url': 'https://example.org/list.m3u',
                  'include_groups': ['Ulusal'], 'include_ids': ['sozcu.tr'],
                  'id_aliases': {'sozcu.tr': 'SozcuTV.tr'}}
        with patch('build_playlist.fetch', return_value=(body, source['url'], 'text/plain')):
            entries, warnings, excluded = load_source(source, 1)
        self.assertEqual(excluded, 1)
        self.assertFalse(warnings)
        self.assertEqual(channel_id(entries[0]), 'sozcutv.tr')
        self.assertIn('tvg-id="SozcuTV.tr"', entries[0].info)

    def test_direct_stream_source_keeps_stable_url(self):
        source = {'name': 'test', 'type': 'stream', 'url': 'https://example.org/stream.m3u8',
                  'channel_name': 'Sözcü TV', 'tvg_id': 'SozcuTV.tr'}
        with patch('build_playlist.fetch') as fetch:
            entries, warnings, excluded = load_source(source, 1)
            fetch.assert_not_called()
        self.assertEqual(entries[0].url, source['url'])
        self.assertEqual(entries[0].name, 'Sözcü TV')
        self.assertFalse(entries[0].geo)
        self.assertFalse(warnings)
        self.assertEqual(excluded, 0)

    def test_quoted_comma_headers_and_regional_ids(self):
        text = '''#EXTM3U
#EXTINF:-1 tvg-id="BBC.uk@Turkiye" http-user-agent="Test, Agent",BBC (1080p)
#EXTVLCOPT:http-referrer=https://example.org/
https://example.org/tv.m3u8
#EXTINF:-1 tvg-id="BBC.uk@HD",BBC UK
https://example.org/uk.m3u8
'''
        entries, warnings = parse_playlist(text, 'test', 'https://example.org/')
        self.assertEqual(warnings, [])
        self.assertEqual(entries[0].name, 'BBC (1080p)')
        self.assertEqual(entries[0].attrs['http-user-agent'], 'Test, Agent')
        self.assertEqual(channel_id(entries[0]), 'bbc.uk@turkiye')
        self.assertEqual(channel_id(entries[1]), 'bbc.uk')
        self.assertEqual(len(entries[0].options), 1)

    def test_quality_and_generic_id_dedup(self):
        text = '''#EXTM3U
#EXTINF:-1 tvg-id="TRTTurk.tr@SD",TRT Türk (720p)
https://example.org/1
#EXTINF:-1 tvg-id="ext",TRT TÜRK
https://example.org/2
#EXTINF:-1 tvg-id="TRTTurk.tr@HD",TRT Türk (1080p)
https://example.org/3
'''
        entries, _ = parse_playlist(text, 'test', 'https://example.org/')
        self.assertEqual(len(group_channels(entries)), 1)

    def test_end_to_end_fallback_geo_and_segment_validation(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                base = f'http://127.0.0.1:{self.server.server_port}'
                data = {
                    '/source': f'''#EXTM3U
#EXTINF:-1 tvg-id="One.tr@SD",One (720p)
{base}/denied
#EXTINF:-1 tvg-id="One.tr@HD",One (1080p)
#EXTVLCOPT:http-user-agent=FixtureAgent
{base}/redirect
#EXTINF:-1 tvg-id="Two.tr",Two [Geo-blocked]
{base}/geo
#EXTINF:-1 tvg-id="Three.tr",Three
{base}/badmedia.m3u8
#EXTINF:-1 tvg-id="Four.tr",Four
{base}/denied
#EXTINF:-1 tvg-id="One.tr@HD",One
#EXTVLCOPT:http-user-agent=FixtureAgent
{base}/redirect
''',
                    '/nested/master.m3u8': '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000\nmedia.m3u8\n',
                    '/nested/media.m3u8': '#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\nsegment.ts\n',
                    '/nested/segment.ts': 'FAKE_MEDIA_BYTES',
                    '/badmedia.m3u8': '#EXTM3U\n#EXTINF:5,\n/missing.ts\n',
                }
                if self.path == '/redirect':
                    if self.headers.get('User-Agent') != 'FixtureAgent':
                        self.send_error(403)
                        return
                    self.send_response(302)
                    self.send_header('Location', '/nested/master.m3u8')
                    self.end_headers()
                    return
                if self.path not in data:
                    self.send_error(403 if self.path == '/denied' else 404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.end_headers()
                self.wfile.write(data[self.path].encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                sources = directory / 'sources.json'
                output, report = directory / 'playlist.m3u8', directory / 'report.json'
                sources.write_text(json.dumps({'sources': [{'name': 'Fixture', 'url': f'http://127.0.0.1:{server.server_port}/source'}]}))
                command = [sys.executable, str(Path(__file__).with_name('build_playlist.py')),
                           '--sources', str(sources), '--output', str(output), '--report', str(report), '--retries', '0']
                result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                stats = json.loads(report.read_text())['summary']
                self.assertEqual(stats['output_channels'], 2)
                self.assertEqual(stats['reachable'], 1)
                self.assertEqual(stats['geo_retained'], 1)
                self.assertEqual(stats['dropped_groups'], 2)
                self.assertNotIn('/geo', requests)
                self.assertIn('/nested/segment.ts', requests)
                self.assertEqual(output.read_text().count('#EXTINF:'), 2)
                self.assertFalse(output.with_suffix('.m3u').exists())
                self.assertEqual(list(directory.glob('*.m3u8')), [output])
                # A later source failure must never erase a usable prior playlist.
                previous = output.read_text()
                config = json.loads(sources.read_text())
                config['required_channels'] = ['Missing.tr']
                sources.write_text(json.dumps(config))
                result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(output.read_text(), previous)
                self.assertEqual(json.loads(report.read_text())['missing_required_channels'], ['Missing.tr'])
                sources.write_text(json.dumps({'sources': [{'name': 'Broken', 'url': f'http://127.0.0.1:{server.server_port}/missing'}]}))
                result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(output.read_text(), previous)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
