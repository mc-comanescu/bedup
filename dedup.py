# vim: set fileencoding=utf-8 sw=4 ts=4 et :

import collections
import errno
import glob
import os
import re
import stat

from btrfs import clone_data
from chattr import editflags, FS_IMMUTABLE_FL


BUFSIZE = 8192


class FilesDifferError(ValueError):
    pass


class FilesInUseError(RuntimeError):
    pass


ProcUseInfo = collections.namedtuple('ProcUseInfo', 'proc_path is_readable is_writable')

def proc_use_info(proc_path):
    try:
        mode = os.lstat(proc_path).st_mode
    except OSError, e:
        if e.errno == errno.ENOENT:
            return
        raise
    else:
        return ProcUseInfo(
            proc_path=proc_path,
            is_readable=bool(mode & stat.S_IRUSR),
            is_writable=bool(mode & stat.S_IWUSR))



def cmp_fds(fd1, fd2):
    # Python 3 can take closefd=False instead of a duplicated fd.
    fi1 = os.fdopen(os.dup(fd1), 'r')
    fi2 = os.fdopen(os.dup(fd2), 'r')
    return cmp_files(fi1, fi2)


def cmp_files(fi1, fi2):
    while True:
        b1 = fi1.read(BUFSIZE)
        b2 = fi2.read(BUFSIZE)
        if b1 != b2:
            return False
        if not b1:
            return True


def dedup_same(source, dests):
    source_fd = os.open(source, os.O_RDONLY)
    dest_fds = [os.open(dname, os.O_RDWR) for dname in dests]
    fds = [source_fd] + dest_fds
    fd_names = dict(zip(fds, [source] + dests))

    with ImmutableFDs(fds) as immutability:
        if immutability.fds_in_write_use:
            raise FilesInUseError(
                'Some of the files to deduplicate '
                'are open for writing elsewhere',
                dict(
                    (fd_names[fd], tuple(immutability.write_use_info(fd)))
                    for fd in immutability.fds_in_write_use))

        for fd in dest_fds:
            if not cmp_fds(source_fd, fd):
                raise FilesDifferError(fd_names[source_fd], fd_names[fd])
            clone_data(dest=fd, src=source_fd)


PROC_PATH_RE = re.compile(r'^/proc/(\d+)/fd/(\d+)$')
FLAGS_LINE_RE = re.compile(r'^flags:\s+0(\d+)\n$')


def find_inodes_in_write_use(fds):
    for (fd, use_info) in find_inodes_in_use(fds):
        if use_info.is_writable:
            yield (fd, use_info)


def find_inodes_in_use(fds):
    """
    Find which of these inodes are in use, and give their open modes.

    Does not count the passed fds as an use of the inode they point to,
    but if the current process has the same inodes open with different
    file descriptors these will be listed.

    Looks at /proc/*/fd and /proc/*/map_files (Linux 3.3).
    Conceivably there are other uses we're missing, to be foolproof
    will require support in btrfs itself; a share-same-range ioctl
    would work well.
    """

    self_pid = os.getpid()
    id_fd_assoc = collections.defaultdict(list)

    for fd in fds:
        st = os.fstat(fd)
        id_fd_assoc[(st.st_dev, st.st_ino)].append(fd)

    def st_id_candidates(it):
        for proc_path in it:
            try:
                st = os.stat(proc_path)
            except OSError, e:
                # glob opens directories during matching,
                # and other processes might close their fds in the meantime.
                # This isn't a problem for the immutable-locked use case.
                if e.errno == errno.ENOENT:
                    continue
                raise

            st_id = (st.st_dev, st.st_ino)
            if st_id not in id_fd_assoc:
                continue

            yield proc_path, st_id

    for proc_path, st_id in st_id_candidates(glob.glob('/proc/[1-9]*/fd/*')):
        other_pid, other_fd = map(
            int, PROC_PATH_RE.match(proc_path).groups())
        original_fds = id_fd_assoc[st_id]
        if other_pid == self_pid:
            if other_fd in original_fds:
                continue

        use_info = proc_use_info(proc_path)
        if not use_info:
            continue

        for fd in original_fds:
            yield (fd, use_info)

    # Requires Linux 3.3
    for proc_path, st_id in st_id_candidates(
        glob.glob('/proc/[1-9]*/map_files/*')
    ):
        use_info = proc_use_info(proc_path)
        if not use_info:
            continue

        original_fds = id_fd_assoc[st_id]
        for fd in original_fds:
            yield (fd, use_info)


class ImmutableFDs(object):
    """A context manager to mark a set of fds immutable.

    Actually works at the inode level, fds are just to make sure
    inodes can be referenced unambiguously.
    """

    def __init__(self, fds):
        self.__fds = fds
        self.__revert_list = []
        self.__in_use = None
        self.__writable_fds = None

    def __enter__(self):
        for fd in self.__fds:
            # Prevents anyone from creating write-mode file descriptors,
            # but the ones that already exist remain valid.
            was_immutable = editflags(fd, add_flags=FS_IMMUTABLE_FL)
            if not was_immutable:
                self.__revert_list.append(fd)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for fd in self.__revert_list:
            editflags(fd, remove_flags=FS_IMMUTABLE_FL)

    def __require_use_info(self):
        # We only track write use, other uses can appear after the /proc scan
        if self.__in_use is None:
            self.__in_use = collections.defaultdict(list)
            for (fd, use_info) in find_inodes_in_write_use(self.__fds):
                self.__in_use[fd].append(use_info)
            self.__writable_fds = frozenset(self.__in_use.keys())

    def write_use_info(self, fd):
        self.__require_use_info()
        # A quick check to prevent unnecessary list instanciation
        if fd in self.__in_use:
            return tuple(self.__in_use[fd])
        else:
            return tuple()

    @property
    def fds_in_write_use(self):
        self.__require_use_info()
        return self.__writable_fds

