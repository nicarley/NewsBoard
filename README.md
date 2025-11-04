# NewsBoard

A desktop application for viewing multiple video streams simultaneously, designed for news junkies and information enthusiasts.

<img src="https://raw.githubusercontent.com/nicarley/NewsBoard/refs/heads/master/resources/screenshot.png" />

*   **Multi-source viewing:** Watch video streams from various sources like YouTube and other direct stream URLs.
*   **Grid layout:** Arrange multiple video tiles in a grid for a comprehensive overview.
*   **Feed management:** Curate a list of your favorite news feeds for quick access.
*   **Single-speaker mode:** Automatically mutes all but one video stream, with the option to manually select the active speaker.
*   **Volume control:** Adjust the volume of the active video stream.
*   **Fullscreen mode:** View a single video stream in fullscreen for a more focused experience.
*   **Persistent state:** The application saves your video grid and settings for the next session.

## Dependencies

*   [PyQt6](https://pypi.org/project/PyQt6/)
*   [yt-dlp](https://pypi.org/project/yt-dlp/) (optional, for playing YouTube videos)

To install the dependencies, run:

```bash
pip install PyQt6 yt-dlp
```

## Usage

1.  Clone the repository or download the source code.
2.  Install the dependencies as described above.
3.  Run the `main.py` file:

```bash
python main.py
```

4.  Use the "Manage Lists" panel to add, edit, or remove news feeds.
5.  Add videos to the grid by selecting feeds and clicking "Add Selected" or by pasting a video URL or iframe tag into the input field and clicking "Add Video".
6.  Click the mute/unmute button on a video tile to control which stream's audio is active.
7.  Enjoy your personalized news board!
