import os
import stat
import subprocess
import json
import ffmpeg
from datetime import datetime
from pathlib import Path, PurePosixPath, PurePath
from typing import List, Tuple
from functools import total_ordering

tmpdir = Path(R"W:\Temp")


@total_ordering
class File():

    def __init__(self, relpath: str, mtime: int, mode: int, srcpath=None):
        self.relpath = relpath
        self.mtime = mtime
        self.mode = mode
        self.srcpath = srcpath

    def __lt__(self, other):
        return self.relpath < other.relpath or \
            (self.relpath == other.relpath and self.mtime < other.mtime + 60)

    def __eq__(self, other):
        return self.relpath == other.relpath and \
            abs(self.mtime - other.mtime) < 60


class RemoteFS():

    def copy(self, src: Path, abspath: Path, mtime: int) -> None:
        raise NotImplementedError

    def unlink(self, abspath: PurePath) -> None:
        raise NotImplementedError

    def rmdir(self, abspath: PurePath) -> None:
        raise NotImplementedError

    def listdir(self, abspath: PurePath) -> List[Tuple[str, int, int]]:
        raise NotImplementedError


class LocalRemote(RemoteFS):

    def __init__(self) -> None:
        raise NotImplementedError

    def copy(self, src: Path, abspath: Path, mtime: int) -> None:
        """Copy a local file to the remote"""
        raise NotImplementedError

    def unlink(self, abspath: Path) -> None:
        """Delete a file from the remote"""
        raise NotImplementedError

    def rmdir(self, abspath: Path) -> None:
        """Delete a directory from the remote"""
        raise NotImplementedError

    def listdir(self, abspath: Path) -> List[Tuple[str, int, int]]:
        raise NotImplementedError


class AdbRemote(RemoteFS):

    def __init__(self, adb_args: List[bytes]) -> None:
        self.adb_args = adb_args

    def copy(self, src: Path, dst: Path) -> None:
        """Copy a local file to the remote"""
        if src.is_dir():
            return
        mtime = src.stat().st_mtime
        if src.suffix != dst.suffix:
            src = transcode(src, mtime)
        subprocess.run(self.adb_args +
                       [b'push',
                        str(src).encode(),
                        str(dst).encode()],
                       check=True,
                       stdout=subprocess.DEVNULL)
        utc_mtime = datetime.utcfromtimestamp(mtime).strftime('%Y%m%d%H%M.%S')
        cmd_str = b' '.join(self.adb_args).decode() + \
            ' shell su -c TZ=UTC busybox touch -t ' + \
            '{} {}'.format(utc_mtime, self.QuoteV2(str(dst)))
        subprocess.run(cmd_str, check=True)
        if src.parent == tmpdir:
            src.unlink()

    def unlink(self, abspath: PurePosixPath) -> None:
        """Delete a file from the remote"""
        subprocess.run(self.adb_args +
                       [b'shell', b'rm',
                        self.QuoteArgument(str(abspath).encode())],
                       check=True)

    def rmdir(self, abspath: PurePosixPath) -> None:
        """Delete a directory from the remote"""
        subprocess.run(self.adb_args +
                       [b'shell', b'rmdir',
                        self.QuoteArgument(str(abspath).encode())],
                       check=True)

    def listdir(self, abspath: PurePosixPath) -> List[Tuple[str, int, int]]:
        """Recursively list the contents of the remote directory"""
        result = []
        qpath = self.QuoteArgument(str(abspath).encode())
        r = subprocess.run(self.adb_args +
                           [b'shell', b'find', qpath,
                            b"-exec stat -c '%Y %f %N' '{}' +"],
                           stdout=subprocess.PIPE,
                           check=True)
        stdout = r.stdout.decode().replace("\r", "")
        for line in stdout.rstrip().split('\n'):
            relpath = str(PurePosixPath(line[17:-1]).relative_to(music_dst))
            mtime = int(line[:10])
            mode = int(line[11:15], base=16)
            result.append(File(relpath, mtime, mode))
        return result

    def QuoteV2(self, arg: str) -> str:
        """Needed to make 'su -c' work.
        Probably only works on Windows."""
        arg = '"' + arg + '"'
        arg = arg.replace('`', '\\\\\\`')
        arg = arg.replace('$', '\\\\\\$')
        arg = arg.replace('"', '\\\\\\"')
        arg = arg.replace("'", "\\'")
        arg = arg.replace("(", "\\(")
        arg = arg.replace(")", "\\)")
        arg = arg.replace("&", "\\&")
        arg = arg.replace(";", "\\;")
        arg = arg.replace("<", "\\<")
        arg = arg.replace(">", "\\>")
        arg = arg.replace("|", "\\|")
        arg = arg.replace("#", "\\#")
        arg = arg.replace("~", "\\~")
        arg = arg.replace(" ", "\\ ")
        return arg

    def QuoteArgument(self, arg: bytes) -> bytes:
        """Quotes an argument for use by adb shell.
        Usually, arguments in 'adb shell' use are put in
        double quotes by adb, but not in any way escaped."""
        arg = arg.replace(b'\\', b'\\\\')
        arg = arg.replace(b'"', b'\\"')
        arg = arg.replace(b'$', b'\\$')
        # arg = arg.replace(b'%', b'%%')
        arg = arg.replace(b'`', b'\\`')
        arg = b'"' + arg + b'"'
        return arg

    def IsWorking(self) -> bool:
        """Tests the adb connection.

        This string should contain all possible evil, but no percent signs.
        Note this code uses 'date' and not 'echo', as date just calls strftime
        while echo does its own backslash escape handling additionally to the
        shell's. Too bad printf "%s\n" is not available."""
        test_strings = [
            b'(', '!@#$^&*()<>;/?\\\'"'.encode(),
            '(;  #`ls`$PATH\'"(\\\\\\\\){};!\xc0\xaf\xff\xc2\xbf'.encode()
        ]
        for test_string in test_strings:
            good = False
            r = subprocess.run(self.adb_args +
                               [b'shell', b'date +%s' %
                                (self.QuoteArgument(test_string),)],
                               stdout=subprocess.PIPE,
                               check=True)
            for line in r.stdout.replace(b"\r", b"").split(b'\n'):
                line = line.rstrip(b'\r\n')
                if line == test_string:
                    good = True
            if not good:
                return False
        return True


def transcode(src: Path, mtime) -> Path:
    dst = tmpdir.joinpath(src.stem + "." + transcode_format)
    ffmpeg.input(str(src)) \
          .output(str(dst), id3v2_version=3, format=transcode_format,
                  audio_bitrate=transcode_bitrate, loglevel='error') \
          .run(overwrite_output=True)
    os.utime(dst, times=(mtime, mtime))
    return dst


with open("android.json") as fi:
    js = json.loads(fi.read())

playlist_src = Path(js['playlist_src'])
music_src = Path(js['music_src'])

assert playlist_src.is_dir()
assert music_src.is_dir()

file_system = js['file_system']
assert file_system in ['local', 'adb']

if file_system == 'local':
    playlist_dst = Path(js['playlist_dst'])
    music_dst = Path(js['playlist_dst'])
    fs = LocalRemote()
if file_system == 'adb':
    playlist_dst = PurePosixPath(js['playlist_dst'])
    music_dst = PurePosixPath(js['music_dst'])
    device_id = js['device_id']
    adb_args = [b'adb', b'-s', device_id.encode()]
    fs = AdbRemote(adb_args)
    assert fs.IsWorking()

transcode_files = js['transcode']
if transcode_files is True:
    transcode_format = js['transcode_format']
    transcode_bitrate = js['transcode_bitrate']
    assert transcode_format in ['mp3']

playlists = {}
directories = set()
songs = set()
covers = set()
local = set()
formats = set()

for playlist in js['playlists']:
    playlist_path = playlist_src.joinpath(playlist)
    assert playlist_path.is_file(), playlist_path + " not found!"
    playlists[playlist] = []
    with open(playlist_path, encoding='utf-8') as fi:
        fi.readline()  # ignore leading '#' line
        for line in list(fi)[:]:
            abspath = Path(line.rstrip())
            relpath = abspath.relative_to(music_src)
            playlists[playlist].append(relpath)
            if relpath not in songs:
                assert abspath.is_file(), abspath + " not found!"
                songs.add(relpath)
                formats.add(relpath.suffix)
                directories.add(relpath.parent)
    print("Read", len(playlists[playlist]), 'items from', playlist)

for path in directories:
    for coverfile in ['cover.jpg', 'cover.png']:
        coverpath = path.joinpath(coverfile)
        if music_src.joinpath(coverpath).is_file():
            covers.add(coverpath)
    while path not in local:
        local.add(path)
        path = path.parent

print(len(songs), 'songs found!')
print(len(covers), 'covers found!')
print("Formats:", formats)
local.update(songs)
local.update(covers)
local = list(local)

for i, path in enumerate(local):
    sortpath = path
    if transcode_files is True and sortpath.suffix in ['.flac']:
        sortpath = sortpath.with_suffix("." + transcode_format)
    stat_ = music_src.joinpath(path).stat()
    local[i] = File(relpath=sortpath.as_posix(),
                    mtime=int(stat_.st_mtime),
                    mode=stat_.st_mode,
                    srcpath=music_src.joinpath(path))

remote = fs.listdir(music_dst)

local.sort()
remote.sort()

total = len(local)

while local or remote:
    if not local or (remote and local[-1].relpath < remote[-1].relpath):
        print("({}/{}) ".format(total - len(local), total), end='')
        print("deleting", remote[-1].relpath)
        if stat.S_ISDIR(remote[-1].mode):
            fs.rmdir(music_dst.joinpath(remote[-1].relpath))
        else:
            fs.unlink(music_dst.joinpath(remote[-1].relpath))
        remote.pop()
    elif not remote or local[-1] > remote[-1]:
        print("({}/{}) ".format(total - len(local), total), end='')
        if local[-1].relpath != remote[-1].relpath:
            print("copying", local[-1].relpath)
        else:
            print("updating", local[-1].relpath)
            remote.pop()
        fs.copy(local[-1].srcpath, music_dst.joinpath(local[-1].relpath))
        local.pop()
    else:
        # print(" matched", local[-1].srcpath)
        local.pop()
        remote.pop()

for playlist in playlists:
    temppath = tmpdir.joinpath(playlist)
    with open(temppath, 'w', encoding='utf-8') as fo:
        for relpath in playlists[playlist]:
            if transcode_files is True and relpath.suffix in ['.flac']:
                relpath = relpath.with_suffix("." + transcode_format)
            fo.write(relpath.as_posix() + '\n')
    print("copying", playlist)
    fs.copy(temppath, playlist_dst.joinpath(playlist))
