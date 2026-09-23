# Turkish and international TV playlist

In IPTV Smarters on Google TV, choose **Add M3U Playlist** and use:

**https://mehmet-oner.github.io/iptvs/playlist.m3u8**

This repository publishes one UTF-8 extended M3U playlist, with Turkish channels
and selected US, UK and Dutch services. `sources.json` is the editable input;
`build_playlist.py` creates `playlist.m3u8` and `validation-report.json`.

## Rebuild

Use Python 3.10+ with the pinned dependencies (YouTube extraction and FFmpeg):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python build_playlist.py
.venv/bin/python -m unittest -v
```

A separate FFmpeg binary can be supplied with `--ffmpeg /path/to/ffmpeg`.
The September 17 rebuild used FFmpeg 7.1 from the task-local
`imageio-ffmpeg` 0.6.0 package. That binary is not committed to Git.

```sh
python3 build_playlist.py --workers 8 --timeout 12 --retries 1 --decode-seconds 4
```

The default **decode** mode requires FFmpeg; it never silently falls back to a
weaker check. For diagnostic HTTP checks only, use `--validation reachability`.
That mode reports `reachability_only`, not `video_decoded`, and can include
responses that have not been proved playable. Do not use it for a verified release.

GitHub Pages publishes the root of `main`. Rebuild and push to refresh the URL.
A Codex hourly automation refreshes Sözcü from this local checkout and pushes
validated changes. This computer and Codex must be available; sleep, network
failures or YouTube changes can prevent refreshes. A playback session that
keeps an old signed URL for six hours may need the channel reopened. Signed URLs expire after
roughly six hours. The refresh is not hosted by GitHub Pages. Other channels
are refreshed by a full manual rebuild.

To refresh only Sözcü manually:

```sh
.venv/bin/python refresh_sozcu.py
```

`streams/sozcu-1080.m3u8` and `streams/sozcu-720.m3u8` are HLS masters with
separate AAC audio and H.264 video. They are staged on loopback HTTP for actual
decoding before publication. The two playlist entries are quality alternatives
from the same official YouTube broadcast, not independent providers. Refresh
failures leave existing files unchanged. The report preserves the full-build
evidence date and adds a separate Sözcü refresh date.

## Source research — September 17, 2026

The selection favors broadcaster entry points, then maintained catalogs and
validated alternatives. Repository activity and scheduled checks help discover
fresh URLs; neither proves that every channel works.

| Source | Evidence checked | How it is used |
| --- | --- | --- |
| Broadcaster live pages/CDNs | Player URLs from Number1, Kanal D, Show TV, Star, NTV, TV8, Halk TV, Habertürk, CBS, Sky and Bloomberg | Preferred direct entry points, tested again on every rebuild |
| [iptv-org](https://github.com/iptv-org/iptv) | Country playlists dated September 17; [update workflow](https://github.com/iptv-org/iptv/blob/master/.github/workflows/update.yml) schedules daily updates | Broad Turkish discovery; explicit international channel allowlists |
| [Free-TV](https://github.com/Free-TV/IPTV) | Active September 17; [fast checks](https://github.com/Free-TV/IPTV/blob/master/.github/workflows/check_channels_fast.yml) every six hours and [deep checks](https://github.com/Free-TV/IPTV/blob/master/.github/workflows/check_channels_deep.yml) every two days | Selected country lists with additional exclusions and our own decoding |
| [IPTV-TR](https://github.com/ilyswch/IPTV-TR) and [discevisita](https://github.com/discevisita/iptv) | Last repository pushes September 1 and September 6 | Additional Turkish/Cyprus candidates with corrected channel IDs |
| [IPTV Nexus](https://github.com/dearbulut/iptv) | Updated September 17; derives from iptv-org | Alternative URLs, not independent evidence of reliability |
| [pinkisso](https://github.com/pinkisso/mored) | Updated September 17 | Maintained pointer for Sözcü's YouTube broadcast; depends on upstream renewal |

Removed the broad World IPTV Checker source because it contributed no selected
channels in the preceding report. Expanded international allowlists with public
news, weather, free ad-supported entertainment and Dutch regional/music TV.
Free-TV's apparent CNN US entry was a `cnn_slate` endpoint and was excluded.
Radio, obvious subscription-channel entries, and anonymous-IP entries from bulk
catalogs are filtered.

The research also examined live-TV aggregator pages, including
[Canlitv.com](https://canlitv.com/cnn-turk-izle-1), Canlitv.me and Canlitv.watch.
An embedded YouTube player or a short-lived signed URL is not automatically a
stable URL that IPTV Smarters can reuse.

### Channels specifically requested

- **Number1 Dance:** the MediaTriple endpoint associated with its
  [official live page](https://www.numberone.com.tr/2017/10/03/number1-dance-ty-canli-yayin-izle/)
  is delivering decodable video again. Earlier failed checks do not establish a
  permanent shutdown.
- **TV8:** on September 23 its [official live page](https://www.tv8.com.tr/canli-yayin)
  exposed a broadcaster CDN URL that decoded H.264 1080p video and AAC audio.
  It is now preferred over the catalog entry, with both grouped under `TV8.tr`.
- **Sözcü TV:** on September 18 the community `szcytbe` pointer froze at
  05:02 UTC. Its segment sequence and media bytes stopped changing. The official
  [live page](https://www.szctv.com.tr/canli-yayin-izle) embeds a current YouTube
  broadcast. Each refresh reads that embed again, extracts fresh HLS URLs and
  creates 1080p and 720p masters including audio. Each published version must pass timestamp,
  live-progress and video/audio decoding checks before being published. The
  community pointer remains a tested fallback candidate, not a geo exemption.
- **CNN Türk:** the official duhnet URL returns 403 from this network. The former
  manual geo exemption was removed: 403 alone does not prove a geo restriction.
  A [publicly submitted relay](https://github.com/iptv-org/iptv/issues/41007) decoded
  successfully and its picture was checked for CNN Türk identity. It is an
  explicit exception to bulk anonymous-IP filtering; the operator and long-term
  uptime are unverified. The issue was rejected by iptv-org because the channel
  is on its blocklist, so it must not be presented as an approved iptv-org entry.

## Validation and its limits

For every non-geo candidate in the default mode, FFmpeg opens the actual URL
that would be published and decodes about four seconds of its first video stream
and its first audio stream when available. A successful process must produce at
least eight video frames and sufficient decoded duration. This rejects audio-only
streams, fake media, inaccessible HLS resources and decoder failures. Each process
has a hard deadline, in addition to network timeouts. HLS initialization sections,
keys and separate audio are handled by FFmpeg. Only HTTP(S) inputs are accepted.

HLS program timestamps are checked before decoding. The end of the latest
dated media segment must be within 15 minutes of the current time; durations
between timestamp tags are included. This caught the stale Sözcü feed. Sözcü also requires an advancing media
sequence (or new segment paths if no sequence is supplied) over up to three
target durations, capped at 60 seconds; ended playlists and query-token-only
changes fail. This detects frozen HLS windows, but does not prove the content
itself is never repeated. For
streams without program timestamps, freshness is explicitly `unknown` rather
than claimed as verified. Geo exceptions still bypass all these checks.

The report records `video_decoded` with frame count, duration, codec and size.
This tests one rendition at one moment from this machine. It does not certify
all adaptive renditions, Google TV codec support, continuous uptime, channel
identity for every entry, or future availability. The output keeps the original
master URL so clients can choose quality.

As requested, entries **labelled geo-restricted by a source are retained without
network or decoder checks** when no decoded alternative exists. Their status is
`geo_skipped`, meaning playback is unverified; upstream labels may themselves be
wrong. Unlabelled 403/451 failures are not silently reclassified as geo-blocking.

## Selection and configuration

Sources are ordered by preference. The first decoded candidate for a channel is
selected; a source-labelled geo candidate is a fallback.
`max_streams_per_channel` allows two validated Sözcü alternatives; all other
channels keep one. `require_live_progress` enables the moving-window check for
configured channel IDs. Identical URL/header
requests are checked once, and identical selected streams are deduplicated.
Channel grouping uses `tvg-id` and unambiguous normalized names. Quality suffixes
are collapsed, while regional editions remain separate. Explicit ID aliases fix
known metadata differences, including Habertürk, CBS News and Dutch regionals.
Legacy TRT Spor 2 entries are removed in favor of TRT Spor Yıldız to prevent duplication.

A source can be a playlist, `"type": "stream"`, or `"type": "youtube"`.
YouTube sources read the official live-page embed and generate configured
`heights` at `publish_base`, using the repository `streams/` directory. Playlist sources support
`include_groups`, canonical `include_ids`, `exclude_ids`, `exclude_url_patterns`,
`exclude_name_patterns` and `id_aliases`. Aliases are applied before filtering.
`playlist_defaults` supplies shared filters; source exclusion lists are added to
those defaults. Direct stream entries are individually reviewed and do not use
bulk-playlist filters. Use `"enabled": false` to disable any source.

The builder preserves channel metadata and supplied player headers. HTTP source
fetches support gzip. It reports malformed entries and failed sources. Required
channel IDs are publication guards: if Sözcü or CNN Türk cannot be retained, or
if no entries survive, the run fails and leaves the prior playlist unchanged.
The report is still written with the failure reasons. Files are replaced atomically.
