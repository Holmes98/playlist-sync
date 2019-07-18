# playlist-sync

A Python script to automate the syncing of music files and `.m3u8` playlists to Android devices using ADB. Also supports the transcoding of lossless formats during the sync. Based on [adb-sync](https://github.com/google/adb-sync).


## Prerequisites

 - Python 3
   - ffmpeg-python (`pip install ffmpeg-python`)
 - FFmpeg is required for transcoding.
 - Android Debug Bridge (ADB)
   - Version 1.0.39 is recommended for unicode support on Windows.
 - A rooted Android device with:
   - BusyBox installed
   - USB debugging enabled (under Developer options)
