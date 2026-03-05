# NewsBoard

NewsBoard is a desktop multiview app for monitoring live video streams from multiple sources in one place.

<img src="https://raw.githubusercontent.com/nicarley/NewsBoard/refs/heads/master/resources/screenshot.png" />

## What It Can Do

- Monitor many streams at once in an adaptive grid (`auto`, `2x2`, `3x3`, `4x4`, `1xN`, `Nx1`).
- Mix source types: direct stream URLs, YouTube links, and pasted iframe embeds.
- Manage reusable **News Feeds** (add/edit/remove/reorder/filter and add selected or all to the grid).
- Manage **M3U playlists** from URL or file:
  - import and search channels
  - add selected channels or all channels
  - export the current grid as an `.m3u` file
- Use two audio strategies:
  - `single`: one active tile at a time
  - `mixed`: independent audio per tile
- Control playback per tile with quick actions:
  - mute/unmute
  - play/pause
  - reload
  - rename
  - copy URL
  - remove
- Use **Picture-in-Picture (PiP)** for a floating always-on-top tile.
- Toggle tile fullscreen or fullscreen the entire grid.
- Set global active-tile volume, mute all, and cycle active audio.
- Persist session data automatically (feeds, playlists, app settings, tile state/layout).
- Import/export full user profiles (settings + feeds + playlists + state) as JSON.
- Run built-in diagnostics (platform info, media support checks, data paths, log view/copy report).
- Customize behavior through settings:
  - theme (`system`, `light`, `dark`)
  - layout default
  - YouTube mode (`direct_when_possible`, `embed_only`)
  - pause non-fullscreen tiles in fullscreen
  - show/hide Manage Lists panel

## Requirements

- Python 3.10+ recommended
- [PyQt6](https://pypi.org/project/PyQt6/)
- [requests](https://pypi.org/project/requests/)
- [yt-dlp](https://pypi.org/project/yt-dlp/) (optional, enables YouTube direct resolution)

Install dependencies:

```bash
pip install PyQt6 requests yt-dlp
```

## Run

```bash
python main.py
```

## Quick Start

1. Open **Manage Lists** to curate News Feeds and Playlists.
2. Add streams via:
   - feed selection (`Add Selected` / `Add All`)
   - URL/iframe in the top bar (`Add Video`)
   - M3U import (URL or file)
3. Use the tile controls or context menu to manage each stream.
4. Use **Settings** for audio policy, layout behavior, theme, and profile import/export.
5. Use **Tools -> Diagnostics** when troubleshooting media or environment issues.

## Keyboard Shortcuts

- `F`: Toggle fullscreen for first tile
- `Ctrl+Shift+A`: Add all feeds
- `Delete`: Remove selected stream from grid list

## Notes

- App data (settings, feeds, playlists, state, logs) is stored in your platform app-data directory.
- On Linux, full stream support may require GStreamer multimedia packages.
- Third-party streams and embeds remain subject to each provider's terms.
