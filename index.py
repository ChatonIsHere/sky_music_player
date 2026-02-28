"""
Sky Music Player – CLI
A command-line interface for playing Sky: Children of the Light music
sheets by simulating keyboard input.

Usage:
    py index.py

Place .json or .skysheet files in the 'songs' folder, then run the script,
select a song by number, and switch to the Sky game window.
"""

import pygetwindow
from pynput.keyboard import Controller, Key
from multiprocessing import Process
import math
import time
import json
import os
import sys
import threading

windows = pygetwindow.getWindowsWithTitle("Sky")

sky = None

for window in windows:
    if window.title == "Sky":
        sky = window
        break

if sky is None:
    print("Sky was not detected, please open Sky before running this script.")
    sys.exit(1)

def focusWindow():
    """Bring the Sky game window to the foreground."""
    try:
        sky.activate()
    except Exception:
        try:
            sky.minimize()
            sky.restore()
        except Exception:
            print("Warning: Could not focus Sky window.")

keyboard = Controller()

# sky instrument mappings (1Key and 2Key share the same layout)
_base_map = {
    0: 'y', 1: 'u', 2: 'i', 3: 'o', 4: 'p',
    5: 'h', 6: 'j', 7: 'k', 8: 'l', 9: ';',
    10: 'n', 11: 'm', 12: ',', 13: '.', 14: '/'
}
key_maps = {}
for prefix in ('1Key', '2Key'):
    for num, char in _base_map.items():
        key_maps[f'{prefix}{num}'] = char

class KeyPressThread(threading.Thread):
    """Thread that taps a single Sky instrument key and releases it."""

    def __init__(self, note_time, note_key):
        super().__init__()
        self.note_time = note_time
        self.note_key = note_key

    def run(self):
        if self.note_key in key_maps:
            # uncomment the line below if you want spammed msgs during key presses
            # print(f'pressing "{key_maps[self.note_key]}"')
            keyboard.press(key_maps[self.note_key])
            time.sleep(0.02)  # short delay to ensure note is pressed
            keyboard.release(key_maps[self.note_key])  # release key
        else:
            print("Skipped: Key not found in mapping")


def progress_bar(current, total, song_name, replace_line, bar_length=40):
    """Print a text-based progress bar for the currently playing song."""
    if total <= 0:
        return
    fraction = min(current / total, 1.0)
    current = round(current)
    total = round(total)

    filled = max(0, int(fraction * bar_length) - 1)
    arrow = filled * '-' + '>'
    padding = (bar_length - len(arrow)) * ' '

    time_str = f'{current // 60}:{current % 60:02}/{total // 60}:{total % 60:02}'
    line = f'Now Playing: {song_name} [{arrow}{padding}] {time_str}'

    if replace_line == 0:
        print(line)
    elif replace_line == 1:
        ending = '\n' if current >= total else '\r'
        print(line, end=ending)
    

def progress_loop(data):
    """Continuously update the progress bar in a separate process."""
    start_time = time.perf_counter()
    pause_time = 0
    elapsed_time = 0
    total = data['songNotes'][-1]["time"] / 1000
    name = data["name"]
    paused = 1
    while elapsed_time < total:
        if sky.isActive:
            elapsed_time = time.perf_counter() - start_time - pause_time
            progress_bar(elapsed_time, total, name, paused)
            paused = 1
            time.sleep(1)
        else:
            pause_time_start = time.perf_counter()
            while not sky.isActive:
                time.sleep(0.5)
            pause_time_end = time.perf_counter()
            pause_time += pause_time_end - pause_time_start
            paused = 2

def play_music(song_data):
    """Play back a song by scheduling key-press threads for each note."""
    song_notes = song_data[0]['songNotes']
    song_name = song_data[0]['name']
    total_time = song_notes[-1]['time'] / 1000

    if not song_notes:
        print("No notes found in this song.")
        return

    # Start playing the music
    start_time = time.perf_counter()
    pause_time = 0
    elapsed_time = 0

    # Start progress bar
    p_loop = Process(target=progress_loop, args=(song_data[0],))
    p_loop.start()

    try:
        for i, note in enumerate(song_notes):
            if sky.isActive:
                note_time = note['time']
                note_key = note['key']

                # Create a separate thread for pressing keys
                key_thread = KeyPressThread(note_time, note_key)
                key_thread.start()

                # Calculate the elapsed time since the start of the song
                elapsed_time = time.perf_counter() - start_time - pause_time

                if i < len(song_notes) - 1:
                    next_note_time = song_notes[i + 1]['time']
                    # Calculate the time to wait before playing the next note
                    wait_time = (next_note_time - note_time) / 1000

                    # Adjust wait time to maintain the desired tempo
                    remaining_time = max(0, note_time / 1000 + wait_time - elapsed_time)

                    # sleep until next key
                    time.sleep(remaining_time)

            else:
                print("\033[KSky is not focused, pausing... (Press Ctrl + C to exit the script)")
                paused_time_start = time.perf_counter()
                while not sky.isActive:
                    time.sleep(0.5)
                paused_time_end = time.perf_counter()
                pause_time += paused_time_end - paused_time_start
                print("Resuming song...")
                progress_bar(elapsed_time, total_time, song_name, 1)

        final_time = round(total_time)
        progress_bar(final_time, final_time, song_name, 1)
        print(f"Finished playing {song_name}")
    except KeyboardInterrupt:
        print("\nPlayback cancelled by user.")
    finally:
        p_loop.terminate()
        p_loop.join(timeout=2)

if __name__ == '__main__':
    folder_name = "songs"  # change this folder name if you are using a different folder

    if not os.path.isdir(folder_name):
        print(f"Songs folder '{folder_name}' not found. Please create it and add .json or .skysheet files.")
        sys.exit(1)

    # Filter to only supported file types
    song_list = [
        f for f in os.listdir(folder_name)
        if f.endswith(".json") or f.endswith(".skysheet")
    ]

    if not song_list:
        print(f"No songs found in '{folder_name}/'. Add .json or .skysheet files and try again.")
        sys.exit(1)

    print("Please select a song with the corresponding number.\n")
    for no, name in enumerate(song_list, start=1):
        display_name = os.path.splitext(name)[0]
        print(f"  {no}) {display_name}")
    print()

    try:
        selection = int(input("Please select a song: "))
    except ValueError:
        print("Invalid input — please enter a number.")
        sys.exit(1)

    if selection < 1 or selection > len(song_list):
        print(f"Invalid selection. Please choose a number between 1 and {len(song_list)}.")
        sys.exit(1)

    song_file = os.path.join(folder_name, song_list[selection - 1])

    try:
        with open(song_file, 'r', encoding="utf-8") as file:
            song_data = json.load(file)
    except FileNotFoundError:
        print(f"Song file not found: {song_file}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error reading song file (invalid JSON): {e}")
        sys.exit(1)

    # Validate song data structure
    if not isinstance(song_data, list) or not song_data:
        print("Invalid song file format — expected a JSON array with at least one entry.")
        sys.exit(1)
    if 'songNotes' not in song_data[0] or not song_data[0]['songNotes']:
        print("Song file contains no notes to play.")
        sys.exit(1)

    for i in range(3, 0, -1):
        print(f"Playing song in {i}...")
        time.sleep(1)

    focusWindow()
    play_music(song_data)
