import gzip
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import build_playlist
from build_playlist import channel_id, group_channels, parse_playlist, load_source


class PlaylistTests(unittest.TestCase):
    def test_gzip_fetch_decodes_before_applying_read_limit(self):
        body = b'#EXTM3U\n' + b'#EXTINF:-1,Channel\nhttps://example.org/live.m3u8\n' * 40

        def response(*args, **kwargs):
            stream = io.BytesIO(gzip.compress(body))
            stream.headers = {'Content-Encoding': 'gzip', 'Content-Type': 'application/vnd.apple.mpegurl'}
            stream.geturl = lambda: 'https://example.org/final.m3u'
            return stream

        with patch('build_playlist.urllib.request.urlopen', side_effect=response):
            fetched, resolved, kind = build_playlist.fetch('https://example.org/list.m3u', {}, 1, 10000)
            limited, _, _ = build_playlist.fetch('https://example.org/list.m3u', {}, 1, 64)
        self.assertEqual(fetched, body)
        self.assertEqual(resolved, 'https://example.org/final.m3u')
        self.assertEqual(kind, 'application/vnd.apple.mpegurl')
        self.assertEqual(limited, body[:64])

    def test_direct_video_mime_cannot_hide_error_document(self):
        for payload in (b'<html>Upstream unavailable</html>', b'{"error":"denied"}', b'#EXTM3U\n'):
            with self.subTest(payload=payload):
                with patch('build_playlist.fetch', return_value=(payload, 'https://example.org/live', 'video/mp2t')):
                    with self.assertRaises(ValueError):
                        build_playlist.check_stream('https://example.org/live', {}, 1)

    @staticmethod
    def decoded_output(frames=100, video=True):
        metadata = 'Input #0, hls, from https://example.org/live.m3u8:\n'
        if video:
            metadata += '  Stream #0:0: Video: h264 (High), yuv420p(progressive), 1280x720, 25 fps, 25 tbr, 90k tbn\n'
        metadata += '  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp\n'
        return metadata + f'frame={frames}\nfps=25.0\nout_time_us=4000000\nout_time=00:00:04.000000\nprogress=end\n'

    def test_decoder_requires_video_and_passes_headers_literally(self):
        headers = {'User-Agent': 'Fixture Agent', 'Referer': 'https://example.org/watch?a=1&b=2',
                   'Origin': 'https://example.org', 'Cookie': 'example=literal; value=$(not-a-command)'}
        completed = subprocess.CompletedProcess(['ffmpeg'], 0, stdout=self.decoded_output())
        with patch('build_playlist.subprocess.run', return_value=completed) as run:
            result = build_playlist.decode_stream('https://example.org/live.m3u8', headers, 10, '/tools/ffmpeg', seconds=4)
        self.assertEqual(result['frames'], 100)
        self.assertEqual(result['video_codec'], 'h264')
        self.assertEqual((result['width'], result['height']), (1280, 720))
        self.assertEqual(result['audio_codec'], 'aac')
        self.assertAlmostEqual(result['decoded_seconds'], 4)
        args, kwargs = run.call_args
        command = args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], '/tools/ffmpeg')
        self.assertIn('https://example.org/live.m3u8', command)
        for value in headers.values():
            self.assertTrue(any(value in argument for argument in command), value)
        self.assertFalse(kwargs.get('shell', False))
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLessEqual(kwargs['timeout'], 120)
        self.assertNotIn('copy', command)

    def test_decoder_rejects_no_frames_audio_only_and_failed_decode(self):
        fixtures = [('no frames', 0, self.decoded_output(frames=0)),
                    ('audio only', 0, self.decoded_output(frames=0, video=False)),
                    ('decode failed', 1, self.decoded_output() + 'Error while decoding stream\n')]
        for name, code, stdout in fixtures:
            with self.subTest(case=name):
                completed = subprocess.CompletedProcess(['ffmpeg'], code, stdout=stdout)
                with patch('build_playlist.subprocess.run', return_value=completed):
                    with self.assertRaises(ValueError):
                        build_playlist.decode_stream('https://example.org/live.m3u8', {}, 10, '/tools/ffmpeg')

    def test_decoder_timeout_is_a_validation_failure(self):
        with patch('build_playlist.subprocess.run', side_effect=subprocess.TimeoutExpired(['ffmpeg'], 30)):
            with self.assertRaises(ValueError):
                build_playlist.decode_stream('https://example.org/live.m3u8', {}, 10, '/tools/ffmpeg')

    @patch('build_playlist.inspect_hls_freshness', return_value={'status': 'unknown'})
    def test_validation_labels_decoding_and_does_not_probe_geo_entries(self, freshness):
        entries, _ = parse_playlist('''#EXTM3U
#EXTINF:-1 tvg-id="One.tr",One
https://example.org/live.m3u8
#EXTINF:-1 tvg-id="Two.tr",Two [Geo-blocked]
https://example.org/geo.m3u8
''', 'fixture', 'https://example.org/')
        evidence = {'frames': 100, 'video_codec': 'h264', 'width': 1280, 'height': 720,
                    'audio_codec': 'aac', 'decoded_seconds': 4}
        with patch('build_playlist.check_stream', return_value='Segment reachable') as check:
            with patch('build_playlist.decode_stream', return_value=evidence) as decode:
                self.assertEqual(build_playlist.validate(entries[0], 10, 0, ffmpeg='/tools/ffmpeg')['status'], 'video_decoded')
                decode.assert_called_once()
                decode.reset_mock()
                self.assertEqual(build_playlist.validate(entries[0], 10, 0)['status'], 'reachable')
                decode.assert_not_called()
                check.reset_mock()
                freshness.reset_mock()
                geo = build_playlist.validate(entries[1], 10, 0, ffmpeg='/tools/ffmpeg')
                self.assertEqual(geo['status'], 'geo_skipped')
                self.assertIn('unverified', geo['reason'].lower())
                check.assert_not_called()
                decode.assert_not_called()
                freshness.assert_not_called()

    @patch('build_playlist.inspect_hls_freshness', return_value={'status': 'unknown'})
    def test_decode_failure_cannot_be_reported_as_successful_reachability(self, freshness):
        entries, _ = parse_playlist('#EXTM3U\n#EXTINF:-1,One\nhttps://example.org/live.m3u8\n',
                                    'fixture', 'https://example.org/')
        with patch('build_playlist.check_stream', return_value='Segment reachable'):
            with patch('build_playlist.decode_stream', side_effect=ValueError('No video frames decoded')):
                result = build_playlist.validate(entries[0], 10, 0, ffmpeg='/tools/ffmpeg')
        self.assertEqual(result['status'], 'unreachable')
        self.assertIn('No video frames decoded', result['reason'])

    def test_stale_dated_media_is_rejected_even_if_it_could_decode(self):
        url = 'https://example.org/master.m3u8'
        master = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nmedia.m3u8\n'
        media = b'#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2020-01-01T00:00:00Z\n#EXTINF:5,\none.ts\n'
        with patch('build_playlist.fetch', side_effect=[(master, url, ''), (media, url, '')]):
            with self.assertRaisesRegex(ValueError, 'Stale HLS'):
                build_playlist.inspect_hls_freshness(url, {}, 1)

    def test_hls_freshness_extrapolates_segment_durations_and_handles_undated(self):
        url = 'https://example.org/live.m3u8'
        now = build_playlist.datetime.now(build_playlist.timezone.utc)
        stamp = (now - build_playlist.timedelta(seconds=20)).isoformat()
        media = f'#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:{stamp}\n#EXTINF:10,\none.ts\n#EXTINF:10,\ntwo.ts\n'.encode()
        with patch('build_playlist.fetch', return_value=(media, url, '')):
            evidence = build_playlist.inspect_hls_freshness(url, {}, 1)
        self.assertEqual(evidence['status'], 'recent')
        self.assertLess(abs(evidence['age_seconds']), 2)
        with patch('build_playlist.fetch', return_value=(b'#EXTM3U\n#EXTINF:10,\none.ts\n', url, '')):
            self.assertEqual(build_playlist.inspect_hls_freshness(url, {}, 1)['status'], 'unknown')

    def test_live_progress_rejects_frozen_ended_and_token_only_changes(self):
        url = 'https://example.org/live.m3u8'
        frozen = b'#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:10\n#EXTINF:5,\nseg.ts?token=old\n'
        token = frozen.replace(b'token=old', b'token=new')
        for followup in (frozen, token):
            with patch('build_playlist.fetch', side_effect=[(frozen, url, ''), (followup, url, ''), (followup, url, '')]):
                with patch('build_playlist.time.sleep'):
                    with self.assertRaisesRegex(ValueError, 'Frozen HLS'):
                        build_playlist.inspect_hls_progress(url, {}, 1)
        with patch('build_playlist.fetch', return_value=(frozen + b'#EXT-X-ENDLIST\n', url, '')):
            with self.assertRaisesRegex(ValueError, 'ended HLS'):
                build_playlist.inspect_hls_progress(url, {}, 1)

    def test_live_progress_accepts_new_media_sequence(self):
        url = 'https://example.org/live.m3u8'
        before = b'#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:10\n#EXTINF:5,\n10.ts\n'
        after = before.replace(b'SEQUENCE:10', b'SEQUENCE:11').replace(b'10.ts', b'11.ts')
        with patch('build_playlist.fetch', side_effect=[(before, url, ''), (after, url, '')]):
            with patch('build_playlist.time.sleep'):
                self.assertEqual(build_playlist.inspect_hls_progress(url, {}, 1)['status'], 'advancing')

    def test_intentional_backups_are_distinct_validated_streams(self):
        entries, _ = parse_playlist('#EXTM3U\n#EXTINF:-1,A\nhttps://example.org/a\n#EXTINF:-1,B\nhttps://example.org/a\n#EXTINF:-1,C\nhttps://example.org/c\n#EXTINF:-1,D\nhttps://example.org/d\n', 'fixture', 'https://example.org/')
        results = {build_playlist.probe_key(e): {'status': 'video_decoded'} for e in entries}
        results[build_playlist.probe_key(entries[-1])] = {'status': 'unreachable'}
        chosen, _ = build_playlist.select_streams(entries, results, 2, set())
        self.assertEqual([e.url for e in chosen], ['https://example.org/a', 'https://example.org/c'])

    def test_youtube_master_keeps_audio_and_requested_h264_rendition(self):
        from youtube_live import filter_master
        master = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="234",URI="https://example.org/audio.m3u8"\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,CODECS="avc1.4D401F,mp4a.40.2",AUDIO="234"\nhttps://example.org/720.m3u8\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="234"\nhttps://example.org/1080.m3u8\n'
        selected = filter_master(master, 720)
        self.assertIn('audio.m3u8', selected)
        self.assertIn('720.m3u8', selected)
        self.assertNotIn('1080.m3u8', selected)
        with self.assertRaises(ValueError):
            filter_master(master.replace('TYPE=AUDIO', 'TYPE=SUBTITLES'), 720)

    def test_full_builder_retains_configured_two_streams_for_group_id(self):
        entries, _ = parse_playlist('#EXTM3U\n#EXTINF:-1 tvg-id="SozcuTV.tr",Sözcü TV (1080p)\nhttps://example.org/1080.m3u8\n#EXTINF:-1 tvg-id="SozcuTV.tr",Sözcü TV (720p)\nhttps://example.org/720.m3u8\n', 'fixture', 'https://example.org/')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'sources.json'
            config.write_text(json.dumps({'sources': [{'name': 'fixture', 'type': 'stream', 'url': 'https://example.org/'}],
                                         'max_streams_per_channel': {'SozcuTV.tr': 2},
                                         'required_channels': ['SozcuTV.tr']}))
            argv = ['builder', '--sources', str(config), '--output', str(root / 'output.m3u8'), '--report', str(root / 'report.json')]
            with patch.object(sys, 'argv', argv), patch('build_playlist.find_ffmpeg', return_value='/fixture/ffmpeg'), patch('build_playlist.load_source', return_value=(entries, [], 0)), patch('build_playlist.validate', return_value={'status': 'video_decoded'}), patch('youtube_live.publish_selected'), patch('builtins.print'):
                self.assertEqual(build_playlist.main(), 0)
            self.assertEqual((root / 'output.m3u8').read_text().count('#EXTINF:'), 2)
            report = json.loads((root / 'report.json').read_text())
            self.assertEqual(len(report['channels'][0]['selected_streams']), 2)

    def test_scoped_refresh_replaces_report_group_and_preserves_other_evidence(self):
        import refresh_sozcu
        new, _ = parse_playlist('#EXTM3U\n#EXTINF:-1 tvg-id="SozcuTV.tr",Sözcü TV (1080p)\nhttps://example.org/1080.m3u8\n#EXTINF:-1 tvg-id="SozcuTV.tr",Sözcü TV (720p)\nhttps://example.org/720.m3u8\n', 'Official', 'https://example.org/')
        original = '#EXTM3U\n#EXTINF:-1 tvg-id="SozcuTV.tr",Sözcü TV\nhttps://example.org/old\n#EXTINF:-1 tvg-id="CNNTurk.tr",CNN Türk\nhttps://example.org/cnn\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sources.json').write_text(json.dumps({'sources': [{'name': 'Official', 'type': 'youtube', 'tvg_id': 'SozcuTV.tr'}]}))
            playlist = root / 'playlist.m3u8'
            report_path = root / 'validation-report.json'
            for status in ('unreachable', 'video_decoded'):
                playlist.write_text(original)
                report_path.write_text(json.dumps({'checked_at': 'old-full-build-date', 'summary': {'output_channels': 2, 'video_decoded': 2}, 'channels': [{'channel': 'id:sozcutv.tr', 'status': 'video_decoded'}, {'channel': 'id:cnnturk.tr', 'status': 'video_decoded', 'checked_at': 'old-cnn-date'}]}))
                before = report_path.read_bytes()
                with patch('build_playlist.BASE', root), patch.object(sys, 'argv', ['refresh']), patch('build_playlist.find_ffmpeg', return_value='/fixture/ffmpeg'), patch('build_playlist.load_source', return_value=(new, [], 0)), patch('build_playlist.validate', return_value={'status': status}), patch('refresh_sozcu.publish_selected'), patch('builtins.print'):
                    self.assertEqual(refresh_sozcu.main(), 0 if status == 'video_decoded' else 1)
                if status == 'unreachable':
                    self.assertEqual(playlist.read_text(), original)
                    self.assertEqual(report_path.read_bytes(), before)
                else:
                    report = json.loads(report_path.read_text())
                    self.assertEqual(len([c for c in report['channels'] if c['channel'] == 'id:sozcutv.tr']), 1)
                    self.assertEqual(report['checked_at'], 'old-full-build-date')
                    self.assertEqual(report['channels'][0]['checked_at'], 'old-cnn-date')
                    self.assertEqual(report['summary']['output_channels'], 3)
                    self.assertEqual(report['summary']['video_decoded'], 3)

    def test_source_filters_and_explicit_id_aliases(self):
        body = b'''#EXTM3U
#EXTINF:-1 tvg-id="sozcu.tr" group-title="Ulusal",Sozcu TV
https://example.org/sozcu.m3u8
#EXTINF:-1 tvg-id="Other.us" group-title="Foreign",Other
https://example.org/other.m3u8
'''
        source = {'name': 'test', 'url': 'https://example.org/list.m3u',
                  'include_groups': ['Ulusal'], 'include_ids': ['SozcuTV.tr'],
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

    def test_source_exclusions_apply_even_to_geo_labelled_entries(self):
        body = b'''#EXTM3U
#EXTINF:-1 tvg-id="Keep.tv",Keep TV
https://example.org/keep.m3u8
#EXTINF:-1 tvg-id="Paid.tv",Paid TV [Geo-blocked]
https://example.org/paid.m3u8
#EXTINF:-1 tvg-id="Relay.tv",Relay TV [Geo-blocked]
http://192.0.2.1/live.m3u8
#EXTINF:-1 tvg-id="Radio.tv",Some Radio [Geo-blocked]
https://example.org/radio.m3u8
'''
        source = {'name': 'fixture', 'url': 'https://example.org/list.m3u',
                  'exclude_ids': ['Paid.tv'], 'exclude_url_patterns': [r'^http://192\.0\.2\.1/'],
                  'exclude_name_patterns': [r'\bradio\b']}
        with patch('build_playlist.fetch', return_value=(body, source['url'], 'text/plain')):
            entries, _, excluded = load_source(source, 1)
        self.assertEqual([e.name for e in entries], ['Keep TV'])
        self.assertEqual(excluded, 3)
        self.assertFalse(entries[0].geo)

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
#EXTINF:-1 tvg-id="Five.tr",Five
{base}/fake-media.m3u8
#EXTINF:-1 tvg-id="One.tr@HD",One
#EXTVLCOPT:http-user-agent=FixtureAgent
{base}/redirect
''',
                    '/nested/master.m3u8': '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000\nmedia.m3u8\n',
                    '/nested/media.m3u8': '#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\nsegment.ts\n',
                    # Reachability fixture only; this test does not claim decoding.
                    '/nested/segment.ts': (b'\x47' + b'\x00' * 187) * 3,
                    '/badmedia.m3u8': '#EXTM3U\n#EXTINF:5,\n/missing.ts\n',
                    '/fake-media.m3u8': '#EXTM3U\n#EXTINF:5,\n/not-really-media.ts\n',
                    '/not-really-media.ts': '#EXTM3U\n#EXTINF:5,\n/recursive.ts\n',
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
                payload = data[self.path]
                self.wfile.write(payload.encode() if isinstance(payload, str) else payload)

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
                           '--sources', str(sources), '--output', str(output), '--report', str(report),
                           '--retries', '0', '--validation', 'reachability']
                result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                stats = json.loads(report.read_text())['summary']
                self.assertEqual(stats['output_channels'], 2)
                self.assertEqual(stats['reachability_only'], 1)
                self.assertEqual(stats['geo_retained'], 1)
                self.assertEqual(stats['dropped_groups'], 3)
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
