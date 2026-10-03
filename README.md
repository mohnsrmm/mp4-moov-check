# mp4-moov-check

**Find out why an MP4 or MOV won't play. One command, read-only, no dependencies.**

`mp4-moov-check` looks at how a video file is put together and tells you whether its index
(the `moov` atom) is missing, cut off or fine. Run it when ffmpeg says `moov atom not found`,
or when a recording from a phone, camera, dashcam, drone or action cam suddenly won't open.

- **Read-only.** The file is never written to.
- **Fast on any size.** It reads box headers and the index tables: a few kilobytes, even for a 4 GB file.
- **One Python file.** Standard library only, no network.

## What "moov atom not found" means

An MP4 or MOV recording has two main parts:

- **`mdat`**: the video and audio, written continuously while you record.
- **`moov`**: the index. It says where every frame starts, how long the video is and which
  codecs it uses. Most recorders write it **last**, when recording stops normally.

If the recording is interrupted (a flat battery, an app crash, a memory card pulled out,
storage running full), the `moov` box is never written. The footage is still in the file, but
no player can find a single frame without the index:

```text
$ ffprobe broken.mp4
[mov,mp4,m4a,3gp,3g2,mj2 @ 0x...] moov atom not found
broken.mp4: Invalid data found when processing input
```

Other players say it their own way: "can't play this video", "this item is in a format we
don't support (0xc00d36c4)", "the file isn't compatible". Those messages have other causes too;
this tool tells you whether yours is a missing or broken index.

## Usage

Python 3.8 or newer. Download [`mp4_moov_check.py`](mp4_moov_check.py) and run:

```text
python mp4_moov_check.py broken.mp4
python mp4_moov_check.py -r /path/to/DCIM        # every video in a folder and its sub-folders
python mp4_moov_check.py --json a.mp4 b.mov      # machine-readable
```

Or install it as a command:

```text
pipx install git+https://github.com/mohnsrmm/mp4-moov-check
mp4-moov-check broken.mp4
```

A clip whose recording was cut off, then a healthy one:

```text
$ mp4-moov-check broken.mp4 healthy.mp4
broken.mp4  (40.9 KB)
  boxes: ftyp 32 B | free 8 B | mdat 40.8 KB
  [MISSING INDEX] moov atom not found: the video data is in the file, but the index was never
  written. The recording stopped before it finished (flat battery, crash, card pulled, storage
  full). The footage is usually still there, and the index can be rebuilt from it.

healthy.mp4  (45.4 KB)
  boxes: ftyp 32 B | free 8 B | mdat 40.8 KB | moov 4.5 KB
  track: video, avc1, 0:00:03, 90 samples
  track: audio, mp4a, 0:00:03, 131 samples
  [OK] The index (moov) is present and points inside the file. If the video still won't play,
  the cause is something else: an unsupported codec, or damaged video data.
```

Exit code: `0` when every file is fine, `1` when at least one has a problem, `2` on errors.

## What the verdicts mean

| Verdict | What happened | Can it be fixed? |
|---|---|---|
| `ok` | The index is present and points inside the file. | Nothing to fix in the structure. |
| `ok_fragmented` | A fragmented MP4 with its header. | Plays as is. |
| `missing_index` | No `moov`, the media data is there: "moov atom not found". | Usually: the index can be rebuilt from the video data. |
| `empty_index` | A `moov` written up front that lists no frames. | Usually, the same way. |
| `missing_init` | Fragments (`moof`) without their `moov` header. | Sometimes, with a header from the same recorder. |
| `index_past_end` | The index is there, but the file was cut short after it. | Often: what comes before the cut. |
| `index_damaged` | The file ends inside its `moov`. | Often, by rebuilding the index from the data. |
| `no_media` | No index and no media data. | No: there is no video to rebuild. |
| `zeros` | Empty, or all zeros. | No: the data was never written, or was wiped. |
| `not_mp4` | No MP4/MOV structure at the start. | Not with an MP4 tool. |

## Fixing a missing index

Rebuilding a `moov` box means scanning the raw video data frame by frame, working out where
every video frame and audio packet starts, and writing a new index around the original data.
ffmpeg can't do this on its own: without the index it can't open the file at all.

Ways to get it done:

- **[Video Repair](https://www.nasrtech.dev/videorepair/?utm_source=github&utm_medium=readme&utm_campaign=mp4-moov-check)**
  for [Android](https://play.google.com/store/apps/details?id=com.nasr.videorepair&referrer=utm_source%3Dgithub%26utm_medium%3Dreadme%26utm_campaign%3Dmp4-moov-check):
  rebuilds the index on the phone, with nothing uploaded and nothing re-encoded. Free to check,
  repair and preview; saving the repaired file is a one-time purchase.
- **[Video Repair for Windows](https://apps.microsoft.com/detail/9PFTHSTBH4Z1?cid=github_mp4moovcheck)**
  on the Microsoft Store: the same repair on a PC.
- **[untrunc](https://github.com/anthwlock/untrunc)**: open source, command line. Needs a
  healthy reference clip recorded by the same camera with the same settings.

*Disclosure: this checker is made by [NasrTech](https://www.nasrtech.dev/?utm_source=github&utm_medium=readme&utm_campaign=mp4-moov-check),
the developer of Video Repair.*

## How it works

MP4 and MOV files are a sequence of boxes (ISO/IEC 14496-12, the ISO base media file format;
QuickTime uses the same layout). The checker reads each top-level box header (8 or 16 bytes)
and, when there is a `moov`, the handful of tables that give each track's kind, codec,
duration, sample count and data offsets (`mvhd`, `mdhd`, `hdlr`, `stsd`, `stsz`,
`stco`/`co64`). It samples the media data only to tell real data from pre-allocated zeros.
The file is opened read-only.

## Tests

```text
python -m unittest discover -s tests -v
```

The tests build small files box by box for every verdict, so no sample videos are needed.

## License

[MIT](LICENSE)
