# DiscJockey

An audio CD player for Windows with a clean, album-focused interface. It reads discs directly from your optical drive, shows album art and track names from MusicBrainz, and offers a spinning-vinyl view and a fullscreen ambient mode.

## Features

- **Drive detection:** finds optical drives and reads the disc's table of contents (TOC) directly from the drive.
- **Album art and track names:** looks up the disc in MusicBrainz and fetches cover art from the Cover Art Archive. Discs that aren't in MusicBrainz still play, with generic track names.
- **Automatic disc detection:** notices when a disc is inserted or ejected and brings the window forward when a new disc arrives.
- **Playback controls:** play/pause, previous/next, a seek bar with click-to-jump, and shuffle.
- **Repeat modes:** off, repeat all, and repeat one.
- **Playback speed:** 50% to 200%, with a reset button.
- **Vinyl mode:** a turntable view with the cover as the record label. The tonearm moves with playback progress.
- **Ambient mode:** a minimal layout with a blurred cover backdrop. Fullscreen is available.
- **Adaptive colours:** the background and accent colours are taken from the album cover.
- **System tray:** closing the window keeps playback running in the tray. Use the tray icon to reopen the window or quit.
- **Keep on top:** pin the window above other windows.

## Requirements

- Windows 10 or 11
- Python 3 (64-bit)
- VLC media player (64-bit), installed to the default location or another folder you specify
- An internet connection for album art and track names (optional)

Python packages:

- `PySide6`
- `python-vlc`

## Installation

1. Install [VLC](https://www.videolan.org/) (64-bit).
2. Clone or download this project.
3. Create and activate a virtual environment:
   ```powershell
   python -m venv venv
   venv\Scripts\activate
   ```
4. Install the dependencies:
   ```powershell
   pip install PySide6 python-vlc
   ```
5. Run the app:
   ```powershell
   python app.py
   ```

## Usage

1. Insert an audio CD. DiscJockey detects it and reads the TOC.
2. The track list loads immediately with generic names. If MusicBrainz has the disc, the album title, artist, track names, and cover art update automatically.
3. Double-click a track in the list to play it, or press Space to start from the current track.
4. Use the seek bar to move within a track, and the speed slider to change playback rate.

If no disc is detected, use **Scan** to refresh the drive list. If you have more than one optical drive, pick the one you want from the dropdown.

### Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `Ctrl+Right` | Next track |
| `Ctrl+Left` | Previous track (restarts the track if it's more than 3 seconds in) |
| `V` | Toggle vinyl mode |
| `A` | Toggle ambient mode |
| `F11` | Toggle fullscreen (also enables ambient mode) |
| `Esc` | Exit fullscreen, or leave ambient mode |

### Tray mode

Closing the window hides it to the system tray, and playback continues. Click the tray icon to bring the window back, or right-click it to open the window or quit.

To launch hidden in the tray, run:

```powershell
python app.py --background
```

## Building the executable

Run this from the activated virtual environment, in the project folder:

```powershell
python build.py            # windowed build, no console
python build.py --console  # keeps the console, so debug output is visible
```

The build script:

- Copies the VLC runtime (`libvlc.dll`, `libvlccore.dll`, and the `plugins` folder) and bundles it with the app.
- Builds in `%LOCALAPPDATA%\DiscJockeyBuild`, outside OneDrive, to avoid file-lock errors during the build.
- Writes the output to `%LOCALAPPDATA%\DiscJockeyBuild\dist\DiscJockey\DiscJockey.exe`.

Keep the whole `DiscJockey` folder together when you move it, because the executable depends on the files next to it.

If VLC is installed somewhere other than `C:\Program Files\VideoLAN\VLC`, edit `VLC_DIR` at the top of `build.py`. The VLC build must be 64-bit to match a 64-bit Python.

## Configuration

Settings are saved automatically using Qt's `QSettings`, under the `DiscJockey` organisation and application name. Currently stored:

- Vinyl mode on or off
- Ambient mode on or off

## Troubleshooting

**The album isn't identified.**
DiscJockey looks up the disc by its TOC. If the disc isn't in MusicBrainz, or its TOC differs from the listed pressing, it can't get album details. Run `python app.py` from a terminal to see the `MusicBrainz disc ID` line and the lookup results. Compare that ID with `musicbrainz.org/cdtoc/<ID>`.

**The track count is wrong.**
Some discs may report an extra track in the TOC, such as a hidden track or data area. The track list follows what the drive reports.

**VLC or Windows opens something when I insert a disc.**
Windows AutoPlay may be launching another program. To stop this:

1. Press **Win+R**, type `control /name Microsoft.AutoPlay`, and press Enter.
2. Set **Music CD** to **Take no action**, then click **Save**.

DiscJockey detects discs through the drive directly, so this doesn't affect it.

**The build fails with `PermissionError`.**
Quit DiscJockey from the tray, then try again. If it still fails, exclude `%LOCALAPPDATA%\DiscJockeyBuild` from your antivirus scanning, or pause OneDrive sync.

**No sound or playback fails.**
Confirm VLC (64-bit) is installed and that the Python package `python-vlc` is installed in the same environment you're running.

**Playback stops or the app crashes.**
Run from a terminal with `python app.py` (or `build.py --console`) to see error output, and note what you were doing when it happened.

## Project structure

```
DiscJockey/
├── app.py        # application code
├── build.py      # PyInstaller build script
└── README.md
```

## Credits

- Track and album metadata: [MusicBrainz](https://musicbrainz.org/)
- Cover art: [Cover Art Archive](https://coverartarchive.org/)
- Playback: [VLC](https://www.videolan.org/) via `python-vlc`
- UI: [PySide6](https://doc.qt.io/qtforpython/)
