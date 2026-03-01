# Sky Music Player

Automatically play music in **Sky: Children of the Light** on PC by reading pre-written music sheets and simulating key strokes.

> **Disclaimer:** Using automation in Sky goes against the game's Terms of Service. Use at your own risk — the authors take no responsibility for any bans or consequences.

## Features

- **GUI** (`gui.py`) — Tkinter-based player with search, favourites, queue, duration filtering, configurable global hotkeys, and a built-in song library that syncs from GitHub.
- **CLI** (`index.py`) — Lightweight terminal player for quick use.
- Reads `.json`, `.skysheet`, and `.txt` song files (all JSON-formatted).
- Auto-pauses when the Sky window loses focus and resumes when it regains it.
- Song durations are cached in a local SQLite database so large libraries load fast.

## Setup

1. Install [Python 3.10+](https://www.python.org/downloads/).
2. Clone or download this repository.
3. Install dependencies:
    ```
    pip install -r requirements.txt
    ```

## Usage

### GUI (recommended)

```
py gui.py
```

1. Open Sky and pull out an instrument.
2. Add songs to the queue (double-click or press the hotkey).
3. Press **Play** — the player gives you a moment to switch to Sky, then starts.

Hotkeys are fully configurable from the **⚙ Settings** button in the header.

### CLI

```
py index.py
```

1. Open Sky and pull out an instrument.
2. Select a song by number in the terminal.
3. Switch to the Sky window within the 3-second countdown.

## Getting Songs

The GUI automatically downloads the community sheet collection on first launch. You can also:

- Click **Sync** in the Library tab to pull the latest sheets.
- Click **Import** in the Your Songs tab to add files from your computer.
- Download sheets manually from [Sky Music Nightly](https://specy.github.io/skyMusic/) and place them in the `_imported/` folder.

## Project Structure

| Path       | Description               |
| ---------- | ------------------------- |
| `gui.py`   | GUI application (Tkinter) |
| `index.py` | CLI application           |

User data is stored in `%LOCALAPPDATA%\SkyMusicPlayer\`:

| Path            | Description                                    |
| --------------- | ---------------------------------------------- |
| `settings.json` | Hotkeys, library status, and app configuration |
| `_data/`        | SQLite databases (duration cache, favourites)  |
| `_sheets_repo/` | Downloaded song library                        |
| `_imported/`    | User-imported song files                       |

## Credits

- **Original project** by [Viwyn](https://github.com/Viwyn) — [Sky-Music-Player](https://github.com/Viwyn/Sky-Music-Player)
- **Song library** from [Ai-Vonie/Sky1984-Sheets-Collection](https://github.com/Ai-Vonie/Sky1984-Sheets-Collection)
- **Sky Music Nightly** by [Specy](https://specy.github.io/skyMusic/) — the web tool for creating and sharing Sky music sheets
- **App icon** by [Eucalyp](https://www.flaticon.com/authors/eucalyp) — [Google play music icons](https://www.flaticon.com/free-icons/google-play-music) from Flaticon
- [Sky: Children of the Light](https://thatgamecompany.com/sky/) by thatgamecompany

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
