import os
import stat
import subprocess
import json
import ffmpeg
import argparse
import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath, PurePath
from typing import List, Tuple
from functools import total_ordering


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

    def copy(self, src: Path, abspath: PurePath) -> None:
        raise NotImplementedError

    def unlink(self, abspath: PurePath) -> None:
        raise NotImplementedError

    def rmdir(self, abspath: PurePath) -> None:
        raise NotImplementedError

    def listdir(self, abspath: PurePath) -> List[Tuple[str, int, int]]:
        raise NotImplementedError

    def transcode(self, src: Path, mtime) -> Path:
        dst = self.tmpdir.joinpath(src.stem + "." + self.transcode_format)
        ffmpeg.input(str(src)) \
              .output(str(dst), id3v2_version=3, format=self.transcode_format,
                      loglevel='error', **self.transcode_args) \
              .run(overwrite_output=True)
        os.utime(dst, times=(mtime, mtime))
        return dst


class LocalRemote(RemoteFS):

    def __init__(self, tmpdir: Path) -> None:
        self.tmpdir = tmpdir

    def copy(self, src: Path, dst: Path) -> None:
        """Copy a local file to the remote"""
        mtime = src.stat().st_mtime
        if src.is_dir():
            shutil.copystat(src, dst)
            return
        if src.suffix != dst.suffix:
            src = self.transcode(src, mtime)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        if src.parent == self.tmpdir:
            src.unlink()

    def unlink(self, abspath: Path) -> None:
        """Delete a file from the remote"""
        abspath.unlink()

    def rmdir(self, abspath: Path) -> None:
        """Delete a directory from the remote.
        The directory must be empty."""
        abspath.rmdir()

    def listdir(self, abspath: Path) -> List[Tuple[str, int, int]]:
        """Recursively list the contents of the remote directory.
        Returns a list of tuples (relpath, mtime, mode)"""
        result = self.listdir_absolute(abspath)
        for file in result:
            file.relpath = Path(file.relpath).relative_to(abspath).as_posix()
        return result

    def listdir_absolute(self, abspath: Path) -> List[Tuple[str, int, int]]:
        result = []
        for entry in os.scandir(abspath):
            stat = entry.stat()
            result.append(File(entry.path, int(stat.st_mtime), stat.st_mode))
            if entry.is_dir():
                result.extend(self.listdir_absolute(entry.path))
        return result


class AdbRemote(RemoteFS):

    def __init__(self, adb_args: List[bytes], tmpdir: Path) -> None:
        self.adb_args = adb_args
        self.tmpdir = tmpdir

    def copy(self, src: Path, dst: PurePosixPath) -> None:
        """Copy a local file to the remote"""
        mtime = src.stat().st_mtime
        if src.is_dir():
            return
        if src.suffix != dst.suffix:
            src = self.transcode(src, mtime)
        subprocess.run(self.adb_args +
                       [b'push',
                        str(src).encode(),
                        str(dst).encode()],
                       check=True,
                       stdout=subprocess.DEVNULL)
        if src.parent == self.tmpdir:
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
        """Recursively list the contents of the remote directory.
        Returns a list of tuples (relpath, mtime, mode)"""
        result = []
        qpath = self.QuoteArgument(str(abspath).encode())
        r = subprocess.run(self.adb_args +
                           [b'shell', b'find', qpath,
                            b"-exec stat -c '%Y %f %n' '{}' +"],
                           stdout=subprocess.PIPE,
                           check=True)
        stdout = r.stdout.decode().replace("\r", "")
        for line in stdout.rstrip().split('\n'):
            relpath = str(PurePosixPath(line[16:]).relative_to(abspath))
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'config_path',
        metavar='CONFIG',
        type=str,
        help='path to the json configuration file'
    )
    parser.add_argument(
        '--force-update',
        action='store_true',
        help='Always update files on the remote, even if '
        'the remote file appears to be up-to-date.'
    )
    args = parser.parse_args()

    with open(args.config_path) as fi:
        js = json.loads(fi.read())

    playlist_src = Path(js['playlist_src'])
    music_src = Path(js['music_src'])
    tmpdir = Path(js['tmp_dir'])

    assert playlist_src.is_dir()
    assert music_src.is_dir()
    assert tmpdir.is_dir()

    file_system = js['file_system']
    assert file_system in ['local', 'adb']

    if file_system == 'local':
        playlist_dst = Path(js['playlist_dst'])
        music_dst = Path(js['music_dst'])
        fs = LocalRemote(tmpdir)
    if file_system == 'adb':
        playlist_dst = PurePosixPath(js['playlist_dst'])
        music_dst = PurePosixPath(js['music_dst'])
        device_id = js['device_id']
        adb_args = [b'adb', b'-s', device_id.encode()]
        fs = AdbRemote(adb_args, tmpdir)
        assert fs.IsWorking()

    transcode_files = js['transcode']
    if transcode_files is True:
        transcode_format = js['transcode_format']
        transcode_args = js['transcode_args']
        fs.transcode_format = transcode_format
        fs.transcode_args = transcode_args
        assert transcode_format in ['mp3']

    playlists = {}
    directories = set()
    songs = set()
    covers = set()
    local = set()
    formats = set()

    # read playlists
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

    # locate cover files
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

    # create File objects from local paths
    for i, path in enumerate(local):
        sortpath = path
        if transcode_files is True and sortpath.suffix in ['.flac']:
            sortpath = sortpath.with_suffix("." + transcode_format)
        stat_ = music_src.joinpath(path).stat()
        local[i] = File(relpath=sortpath.as_posix(),
                        mtime=int(stat_.st_mtime),
                        mode=stat_.st_mode,
                        srcpath=music_src.joinpath(path))

    remote = fs.listdir(music_dst)  # get all remote files
    local.sort()
    remote.sort()
    total = len(local)

    # perform the sync
    # iterate in reverse order to avoid deleting non-empty directories
    while local or remote:
        if not local or (remote and local[-1].relpath < remote[-1].relpath):
            # file exists on remote but not local
            print("({}/{}) ".format(total - len(local), total), end='')
            print("deleting", remote[-1].relpath)
            if stat.S_ISDIR(remote[-1].mode):
                fs.rmdir(music_dst.joinpath(remote[-1].relpath))
            else:
                fs.unlink(music_dst.joinpath(remote[-1].relpath))
            remote.pop()

        elif args.force_update or not remote or local[-1] > remote[-1]:
            # file exists on local but not remote, or local file is newer
            print("({}/{}) ".format(total - len(local), total), end='')
            if not remote or local[-1].relpath != remote[-1].relpath:
                print("copying", local[-1].relpath)
            else:
                print("updating", local[-1].relpath)
                remote.pop()
            fs.copy(local[-1].srcpath, music_dst.joinpath(local[-1].relpath))
            local.pop()

        else:
            # file exists on both, and remote is newer or equal to local
            local.pop()
            remote.pop()

    # copy playlists
    for playlist in playlists:
        temppath = tmpdir.joinpath(playlist)
        with open(temppath, 'w', encoding='utf-8') as fo:
            for relpath in playlists[playlist]:
                if transcode_files is True and relpath.suffix in ['.flac']:
                    relpath = relpath.with_suffix("." + transcode_format)
                fo.write(relpath.as_posix() + '\n')
        print("copying", playlist)
        fs.copy(temppath, playlist_dst.joinpath(playlist))


if __name__ == '__main__':
    main()
