# Turkish IPTV playlist builder

Run with Python 3.10 or newer. No packages or accounts are required:

```sh
python3 build_playlist.py
```

The script reads `sources.json` and writes `playlist.m3u8` and
`validation-report.json` next to the script. Run it again whenever you want to
refresh the playlist. It does not install a scheduled task.

Generated playlists use `.m3u8` only. Import them through the player's M3U
playlist option. They are channel-list URLs, not Xtream Codes server addresses.

The builder also writes `playlist-smarters.m3u8` using consistent `tvg-name`,
logo, ID, channel number, and group metadata, with exactly two lines per channel.
Explicit HTTP headers use encoded URL options instead of VLC directives or
nonstandard EXTINF attributes. Support for these headers depends on the player.
This compatibility output preserves all selected channels and the geo policy.

https://raw.githubusercontent.com/mehmet-oner/iptvs/main/playlist-smarters.m3u8

To create that version from the existing output without revalidating anything:

```sh
python3 build_playlist.py --smarters-from playlist.m3u8 --output playlist-smarters.m3u8
```

## Sources and selection

Edit `sources.json` to add/remove source URLs. Sources are ordered by preference.
Set `"enabled": false` on a source to disable it. The four initial sources are
iptv-org Turkey, iptv-org Turkish language, Free-TV Turkey, and ilyswch IPTV-TR.
The Turkish-language source includes some channels based outside Turkey; the
Turkey source can include channels in other languages.

Channels are grouped using `tvg-id`, with quality suffixes such as `@SD` and
`@HD` removed. Regional editions such as `@Turkiye` remain distinct. Entries
without a meaningful ID are matched using normalized names when unambiguous.
The first reachable candidate in source order is selected. Geo-marked candidates
are used if no reachable candidate exists. Identical stream requests are checked
once and duplicate selected stream requests are removed. Different IDs/names
for the same real channel may still require upstream metadata corrections.

## Validation

- HTTP(S) HLS streams must return a valid manifest and a readable sample from a
  recent media segment. Master playlists are followed, including relative URLs
  and redirects; up to three renditions are tried. The original stream URL is
  retained in the output, allowing the player to select quality.
- Direct audio/video endpoints are checked using their response and media MIME
  type. Other protocols and DASH-only endpoints are currently excluded.
- `[Geo-blocked]`, `[Geo-restricted]`, and `Ⓖ` entries are retained without a
  network check. For a source known to consist entirely of restricted streams,
  set `"geo_restricted": true` on that source in `sources.json`.
- Unmarked HTTP 403/451 failures are excluded: the script cannot reliably tell
  geo-restriction from expired tokens, authorization rules, or other failures.
- User-Agent, Referer, Origin, and Cookie options are respected where supplied.
  Original channel metadata, player directives, and URL headers are retained.
- This is a point-in-time reachability check from the machine running the script,
  not a video-decoding, DRM, rights, or continuous-uptime check. Intermittent
  channels that are offline during validation are omitted for that run.

Failed sources are recorded while successful sources are processed. If nothing
can be retained, the script exits with status 1 and leaves any existing playlist
unchanged. The JSON report is still updated, with per-channel reasons.

## Options and tests

```sh
python3 build_playlist.py --timeout 12 --retries 1 --workers 16
python3 build_playlist.py --sources sources.json --output playlist.m3u8 --report validation-report.json
python3 -m unittest -v
```

Timeout is per network operation; nested manifests, retries, and slow streams
can make a full run take several minutes. Source additions should be public
playlists you trust; no source scripts are executed.
