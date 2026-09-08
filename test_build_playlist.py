import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from build_playlist import channel_id, group_channels, parse_playlist, render_smarters, request_target


class PlaylistTests(unittest.TestCase):
    def test_smarters_uniform_metadata_preserves_geo_and_request_headers(self):
        original = '''#EXTM3U
#EXTINF:-1 tvg-id="Example.tr" http-user-agent="Test, Agent",Example [Geo-blocked]
#EXTVLCOPT:http-user-agent=Test, Agent
#EXTVLCOPT:http-referrer=https://example.org/
https://example.org/live.m3u8
'''
        entries, _ = parse_playlist(original, 'test', 'https://example.org/')
        rendered = render_smarters(entries)
        converted, warnings = parse_playlist(rendered, 'test', 'https://example.org/')
        self.assertFalse(warnings)
        self.assertEqual(len(rendered.splitlines()), 3)
        self.assertNotIn('#EXTVLCOPT:', rendered)
        self.assertNotIn('http-user-agent=', rendered)
        self.assertEqual(converted[0].attrs['tvg-name'], entries[0].name)
        self.assertTrue(converted[0].geo)
        self.assertEqual(request_target(converted[0]), request_target(entries[0]))

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
                smarters = directory / 'playlist-smarters.m3u8'
                converted, warnings = parse_playlist(smarters.read_text(), 'output', 'https://example.org/')
                self.assertFalse(warnings)
                self.assertEqual(len(converted), 2)
                # A later source failure must never erase a usable prior playlist.
                previous = output.read_text()
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
