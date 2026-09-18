"""Stage fresh, audio-inclusive HLS renditions of an official YouTube live feed."""
import atexit
import functools
import http.server
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

_pending = {}
_resources = []


def filter_master(text, height):
    lines = text.splitlines()
    audio = next((line for line in lines if line.startswith('#EXT-X-MEDIA:')
                  and 'TYPE=AUDIO' in line and 'GROUP-ID="234"' in line), None)
    if not audio:
        raise ValueError('Official HLS master has no AAC audio group 234')
    for index, line in enumerate(lines[:-1]):
        resolution = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
        if (line.startswith('#EXT-X-STREAM-INF:') and resolution
                and int(resolution.group(2)) == height and 'AUDIO="234"' in line
                and 'avc1.' in line):
            return '\n'.join(['#EXTM3U', '#EXT-X-INDEPENDENT-SEGMENTS', audio, line, lines[index + 1]]) + '\n'
    raise ValueError(f'Official HLS master has no H.264 {height}p rendition with AAC audio')


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def close_staging():
    for server, directory in _resources:
        server.shutdown()
        server.server_close()
        directory.cleanup()
    _resources.clear()


atexit.register(close_staging)


def load_youtube_source(source, timeout):
    from build_playlist import BASE, Entry, fetch
    page, _, _ = fetch(source['url'], {'User-Agent': 'Mozilla/5.0'}, timeout, 2000000)
    match = re.search(r'(?:youtube\.com/embed/|youtu\.be/)([\w-]{11})', page.decode('utf-8', 'replace'))
    if not match:
        raise ValueError('Official page has no YouTube embed; refusing to guess a live video')
    video_id = match.group(1)
    command = [sys.executable, '-m', 'yt_dlp', '--skip-download', '--dump-single-json',
               '--no-playlist', '--socket-timeout', str(timeout),
               'https://www.youtube.com/watch?v=' + video_id]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Official YouTube URL extraction exceeded 90 seconds') from exc
    if result.returncode:
        raise ValueError('YouTube extraction failed; install requirements.txt and inspect the official live page')
    data = json.loads(result.stdout)
    if data.get('live_status') != 'is_live':
        raise ValueError('Official embedded YouTube video is not live')
    master_url = next((f.get('manifest_url') for f in data.get('formats', []) if f.get('manifest_url')), None)
    if not master_url:
        raise ValueError('Official YouTube live feed has no HLS master')
    master, _, _ = fetch(master_url, {'User-Agent': 'Mozilla/5.0'}, timeout, 262144)
    directory = tempfile.TemporaryDirectory(prefix='iptv-live-')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
             functools.partial(QuietHandler, directory=directory.name))
    _resources.append((server, directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    entries = []
    for height in source.get('heights', [1080, 720]):
        filename = f'sozcu-{height}.m3u8'
        content = filter_master(master.decode('utf-8-sig'), height)
        Path(directory.name, filename).write_text(content, encoding='utf-8')
        public_url = source['publish_base'].rstrip('/') + '/' + filename
        _pending[public_url] = (BASE / 'streams' / filename, content)
        label = source.get('channel_name', 'Sözcü TV') + f' ({height}p)'
        attrs = {'tvg-id': source['tvg_id'], 'group-title': source.get('group', 'News')}
        info = f'#EXTINF:-1 tvg-id="{attrs["tvg-id"]}" group-title="{attrs["group-title"]}",{label}'
        entries.append(Entry(info, label, attrs, [], public_url, source['name'], False,
                             True, f'http://127.0.0.1:{server.server_port}/{filename}'))
    return entries, [], 0


def publish_selected(entries):
    from build_playlist import atomic_write
    for entry in entries:
        if entry.url in _pending:
            path, content = _pending[entry.url]
            atomic_write(path, content)
