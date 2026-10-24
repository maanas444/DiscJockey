# Disc Player

An aesthetic audio CD player for Windows, with a spinning-vinyl mode.

## Setup

1. Install **VLC (64-bit)** from videolan.org. Playback uses its engine.
2. Install Python 3.10+ (64-bit), then in this folder:

```
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

(If PowerShell blocks the activate script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`)

## Using it

- The app finds your optical drives on start. Insert an audio CD and press **Scan**.
- Track names and album art are looked up on MusicBrainz / Cover Art Archive
  (needs internet). Without a match you get "Track 01..." and a default cover.
- Double-click a track to play it.
- Seek bar: click or drag. Speed: 0.5x-2.0x (pitch is preserved by VLC).
- Shuffle and Repeat (cycles off -> all -> one).

## Vinyl & ambient modes

| Button | Key | What it does |
| --- | --- | --- |
| ◉ | `V` | **Vinyl mode**: the cover becomes the label of a spinning record. The tonearm lowers when you play, drifts inward as the track progresses, and lifts on pause. The record spins up/down smoothly and turns faster or slower with the speed slider. |
| ⛶ | `A` | **Ambient mode**: hides the drive picker, speed bar and track list, enlarges the record, and shows a blurred version of the cover as the whole window background. |
| 📌 | | Keep the window on top of other windows. |
| | `F11` | Fullscreen ambient mode (great on a second monitor). `Esc` leaves it. |

Other shortcuts: `Space` play/pause, `Ctrl+←` / `Ctrl+→` previous/next.
Your vinyl/ambient choice is remembered between runs. When the record is idle
(paused), the app doesn't redraw, so it's free to leave open.

## Notes

- Only audio CDs are supported (not MP3 data discs).
- Some discs aren't in MusicBrainz; that's the only reason names/art would be missing.
- If a drive isn't detected, check that it shows up in File Explorer.