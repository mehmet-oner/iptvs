# Free Turkish and international IPTV playlist builder

Run with Python 3.10 or newer. No packages or accounts are required:

```sh
python3 build_playlist.py
```

The script reads `sources.json` and writes `playlist.m3u8` and
`validation-report.json` next to the script. Run it again whenever you want to
refresh the playlist. It does not install a scheduled task.

The builder generates a single playlist. In IPTV Smarters, choose **Add M3U
Playlist**, then enter this **M3U URL**:

https://mehmet-oner.github.io/iptvs/playlist.m3u8

GitHub Pages publishes the repository's `main` branch root. The `.nojekyll` file
keeps publication as static files. Pushed playlist changes are published by
GitHub Pages; the playlist builder itself is still run manually. Use the Pages
URL above for IPTV Smarters; it has been confirmed to import on Google TV.

## Sources and selection

Edit `sources.json` to add/remove source URLs. Sources are ordered by preference.
Set `"enabled": false` on a source to disable it. Sources may be full playlists
or `"type": "stream"` entries for a maintained per-channel HLS pointer. A source
can use `include_groups` to import only relevant sections and `id_aliases` to map
nonstandard IDs to canonical IDs before deduplication. `include_ids` imports a
small allowlist of canonical channel IDs from a larger playlist.

The current sources combine Turkish country/language playlists, independently
checked catalogs, a curated Turkish national/Cyprus list, hourly refreshed live
pointers for Sözcü TV and CNN Türk, and Free-TV's curated United States, United
Kingdom, and Netherlands playlists. Free-TV limits its lists to free mainstream
channels and favors quality over quantity. Small iptv-org allowlists add popular
free services missing from those country lists without importing thousands of
local and niche entries. Broad worldwide lists are filtered to their Turkey
group. Archived lists, dead endpoints, and lists containing obvious paid channel
restreams were excluded during the September 2026 source review.

Channels are grouped using `tvg-id`, with quality suffixes such as `@SD` and
`@HD` removed. Regional editions such as `@Turkiye` remain distinct. Entries
without a meaningful ID are matched using normalized names when unambiguous.
The first reachable candidate in source order is selected. Geo-marked candidates
are used if no reachable candidate exists. Identical stream requests are checked
once and duplicate selected stream requests are removed. Different IDs/names
for the same real channel may still require upstream metadata corrections.

`required_channels` is a publication guard. A run that cannot retain Sözcü TV
or CNN Türk exits with status 1 and leaves the last working playlist unchanged.
This guard can be extended with other canonical `tvg-id` values.

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
python3 build_playlist.py --timeout 12 --retries 2 --workers 16
python3 build_playlist.py --sources sources.json --output playlist.m3u8 --report validation-report.json
python3 -m unittest -v
```

Timeout is per network operation; nested manifests, retries, and slow streams
can make a full run take several minutes. Source additions should be public
playlists you trust; no source scripts are executed.
