# adbuffer

Buffers a live stream, spots commercials and sponsor shout-outs in the speech, and
re-serves it with those stretches softened: the picture eases into a dim blur with an
"Ad muted" caption and the audio ducks to a quiet, muffled bed. Both fade back to the
clear broadcast afterwards.

## Desktop app (Omarchy)

- Open **adbuffer** from the launcher, or press **Super Ctrl Y**. It opens in a Brave app window and starts
  the `adbuffer` user service the first time.
- In any Brave tab, press **Super Alt Y**. The tab's URL is copied from the address bar and handed to
  adbuffer, and the app comes forward to play that video with its ads softened.
- Everything works from the keyboard. Press `?` for the full key list.

| Key | Action |
|---|---|
| `/` or `o` | Search YouTube or enter a URL |
| `↵` | Watch through adbuffer |
| `⇧↵` or `b` | Open in Brave instead; plain text goes to Brave Search |
| `i` `k` / arrow keys | Move up / down |
| `j` `l` | Back / forward |
| `1` `2` `3` | Home / Results / Player |
| `a` / `d` / `y` | Bookmark / remove / copy URL |
| `Space` `f` `m` `t` `s` | Pause, fullscreen, mute, transcript, stop |
| In the player: `j` `l` / `←` `→` (Shift: 30 s) | Seek −10/+10 s, or −5/+5 s |
| In the player: `Home` / `End` / `Esc` | Oldest point / back to live / leave the player |

The player keeps the last 2 hours of a session in `~/.cache/adbuffer`, so you can scrub back. Ads are
marked with stripes on the scrub bar, and the transcript highlights and follows what's on screen.
Clicking a transcript line jumps to it.

Colors follow the active Omarchy theme live. History and bookmarks are stored in
`~/.local/share/adbuffer/library.json`. To set an API key or other environment for the
service, put it in `~/.config/adbuffer/env`.

## CLI

```
adbuffer [source] [--delay 45] [--port 8090]
```

- `source`: a local file, a direct HLS/TS/RTMP URL, or any page yt-dlp understands (YouTube, Twitch, ...)
- Watch at http://127.0.0.1:8090/ (the same app UI)

## Pipeline

1. **ingest**: ffmpeg normalises to 720p30 H.264/AAC and cuts 2 s TS segments into a buffer.
2. **detect**: every pass takes about 10 s of new audio and re-transcribes it together with the 20 s before it, so words and ads aren't cut off at chunk edges. Whisper's stock hallucinations over music ("Thanks for watching!") count as stretches with no clear speech. The last 150 s of transcript is then relabelled on every pass.
   - `heuristic` (default): keyword cues open an ad block. A lead-in ("...tell you about our sponsor", "segue to our sponsor") opens it *forward*: what follows is treated as the ad, including a wordless commercial, until the show's own speech resumes. Otherwise a block runs while cues keep coming and closes at a pause or a "back to the video" phrase.
   - `claude`: used automatically when `ANTHROPIC_API_KEY` is set. It sees the same timeline, with the no-speech stretches marked, and is told how lead-ins work.
   - **SponsorBlock**: for YouTube videos, crowd-sourced `sponsor` and `selfpromo` timestamps are added on top of the detector's results. Lookups use the hash-prefix API, so the server never learns the exact video.
3. **publish**: each segment is held for `--delay` seconds. Clean segments are copied as they are. Segments that overlap an ad are re-encoded with the blur/duck filter, with timestamps preserved so they splice in seamlessly. A sliding-window live HLS playlist is served by aiohttp.

`--delay` is the lookahead. It has to cover transcription (~10 s chunk) plus the time
until the sponsor cue is spoken. 45 s is comfortable. Below about 25 s, the tail of
an ad can slip through before it is detected.
