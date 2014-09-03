from __future__ import unicode_literals, absolute_import, print_function

import os
import sys
import math
import errno
import atexit
import importlib
import signal as _signal
import numbers
import itertools

try:
    from io import UnsupportedOperation
    FILENO_ERRORS = (AttributeError, ValueError, UnsupportedOperation)
except ImportError:  # pragma: no cover
    # Py2
    FILENO_ERRORS = (AttributeError, ValueError)  # noqa


def uniq(it):
    """Return all unique elements in ``it``, preserving order."""
    seen = set()
    return (seen.add(obj) or obj for obj in it if obj not in seen)


def get_errno(exc):
    """:exc:`socket.error` and :exc:`IOError` first got
    the ``.errno`` attribute in Py2.7"""
    try:
        return exc.errno
    except AttributeError:
        try:
            # e.args = (errno, reason)
            if isinstance(exc.args, tuple) and len(exc.args) == 2:
                return exc.args[0]
        except AttributeError:
            pass
    return 0


def try_import(module, default=None):
    """Try to import and return module, or return
    None if the module does not exist."""
    try:
        return importlib.import_module(module)
    except ImportError:
        return default


def fileno(f):
    if isinstance(f, numbers.Integral):
        return f
    return f.fileno()


def maybe_fileno(f):
    """Get object fileno, or :const:`None` if not defined."""
    try:
        return fileno(f)
    except FILENO_ERRORS:
        pass


class LockFailed(Exception):
    """Raised if a pidlock can't be acquired."""


EX_CANTCREAT = getattr(os, 'EX_CANTCREAT', 73)
EX_FAILURE = 1

PIDFILE_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY
PIDFILE_MODE = ((os.R_OK | os.W_OK) << 6) | ((os.R_OK) << 3) | ((os.R_OK))

PIDLOCKED = """ERROR: Pidfile ({0}) already exists.
Seems we're already running? (pid: {1})"""


def get_fdmax(default=None):
    """Return the maximum number of open file descriptors
    on this system.

    :keyword default: Value returned if there's no file
                      descriptor limit.

    """
    try:
        return os.sysconf('SC_OPEN_MAX')
    except:
        pass
    if resource is None:  # Windows
        return default
    fdmax = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if fdmax == resource.RLIM_INFINITY:
        return default
    return fdmax


class Pidfile(object):
    """Pidfile

    This is the type returned by :func:`create_pidlock`.

    TIP: Use the :func:`create_pidlock` function instead,
    which is more convenient and also removes stale pidfiles (when
    the process holding the lock is no longer running).

    """

    #: Path to the pid lock file.
    path = None

    def __init__(self, path):
        self.path = os.path.abspath(path)

    def acquire(self):
        """Acquire lock."""
        try:
            self.write_pid()
        except OSError as exc:
            raise (LockFailed, LockFailed(str(exc)), sys.exc_info()[2])
        return self
    __enter__ = acquire

    def is_locked(self):
        """Return true if the pid lock exists."""
        return os.path.exists(self.path)

    def release(self, *args):
        """Release lock."""
        self.remove()
    __exit__ = release

    def read_pid(self):
        """Read and return the current pid."""
        try:
            with open(self.path, 'r') as fh:
                line = fh.readline()
                if line.strip() == line:  # must contain '\n'
                    raise ValueError(
                        'Partial or invalid pidfile {0.path}'.format(self))

                try:
                    return int(line.strip())
                except ValueError:
                    raise ValueError(
                        'pidfile {0.path} contents invalid.'.format(self))
        except errno.ENOENT:
            pass

    def remove(self):
        """Remove the lock."""
        try:
            os.unlink(self.path)
        except (errno.ENOENT, errno.EACCES):
            pass

    def remove_if_stale(self):
        """Remove the lock if the process is not running.
        (does not respond to signals)."""
        try:
            pid = self.read_pid()
        except ValueError as exc:
            print('Broken pidfile found. Removing it.', file=sys.stderr)
            self.remove()
            return True
        if not pid:
            self.remove()
            return True

        try:
            os.kill(pid, 0)
        except os.error as exc:
            if exc.errno == errno.ESRCH:
                print('Stale pidfile exists. Removing it.', file=sys.stderr)
                self.remove()
                return True
        return False

    def write_pid(self):
        pid = os.getpid()
        content = '{0}\n'.format(pid)

        pidfile_fd = os.open(self.path, PIDFILE_FLAGS, PIDFILE_MODE)
        pidfile = os.fdopen(pidfile_fd, 'w')
        try:
            pidfile.write(content)
            # flush and sync so that the re-read below works.
            pidfile.flush()
            try:
                os.fsync(pidfile_fd)
            except AttributeError:  # pragma: no cover
                pass
        finally:
            pidfile.close()

        rfh = open(self.path)
        try:
            if rfh.read() != content:
                raise LockFailed(
                    "Inconsistency: Pidfile content doesn't match at re-read")
        finally:
            rfh.close()


def create_pidlock(pidfile):
    """Create and verify pidfile.

    If the pidfile already exists the program exits with an error message,
    however if the process it refers to is not running anymore, the pidfile
    is deleted and the program continues.

    This function will automatically install an :mod:`atexit` handler
    to release the lock at exit, you can skip this by calling
    :func:`_create_pidlock` instead.

    :returns: :class:`Pidfile`.

    **Example**:

    .. code-block:: python

        pidlock = create_pidlock('/var/run/app.pid')

    """
    pidlock = _create_pidlock(pidfile)
    atexit.register(pidlock.release)
    return pidlock


def _create_pidlock(pidfile):
    pidlock = Pidfile(pidfile)
    if pidlock.is_locked() and not pidlock.remove_if_stale():
        print(PIDLOCKED.format(pidfile, pidlock.read_pid()), file=sys.stderr)
        raise SystemExit(EX_CANTCREAT)
    pidlock.acquire()
    return pidlock


resource = try_import('resource')
pwd = try_import('pwd')
grp = try_import('grp')

DAEMON_UMASK = 0
DAEMON_WORKDIR = '/'

if hasattr(os, 'closerange'):

    def close_open_fds(keep=None):
        # must make sure this is 0-inclusive (Issue #1882)
        keep = list(uniq(sorted(
            f for f in map(maybe_fileno, keep or []) if f is not None
        )))
        maxfd = get_fdmax(default=2048)
        kL, kH = iter([-1] + keep), iter(keep + [maxfd])
        for low, high in itertools.izip_longest(kL, kH):
            if low + 1 != high:
                os.closerange(low + 1, high)

else:

    def close_open_fds(keep=None):  # noqa
        keep = [maybe_fileno(f)
                for f in (keep or []) if maybe_fileno(f) is not None]
        for fd in reversed(range(get_fdmax(default=2048))):
            if fd not in keep:
                try:
                    os.close(fd)
                except errno.EBADF:
                    pass


class DaemonContext(object):
    _is_open = False

    def __init__(self, pidfile=None, workdir=None, umask=None,
                 fake=False, after_chdir=None, **kwargs):
        self.workdir = workdir or DAEMON_WORKDIR
        self.umask = DAEMON_UMASK if umask is None else umask
        self.fake = fake
        self.after_chdir = after_chdir
        self.stdfds = (sys.stdin, sys.stdout, sys.stderr)

    def redirect_to_null(self, fd):
        if fd is not None:
            dest = os.open(os.devnull, os.O_RDWR)
            os.dup2(dest, fd)

    def open(self):
        if not self._is_open:
            if not self.fake:
                self._detach()

            os.chdir(self.workdir)
            os.umask(self.umask)

            if self.after_chdir:
                self.after_chdir()

            close_open_fds(self.stdfds)
            for fd in self.stdfds:
                self.redirect_to_null(maybe_fileno(fd))

            self._is_open = True
    __enter__ = open

    def close(self, *args):
        if self._is_open:
            self._is_open = False
    __exit__ = close

    def _detach(self):
        if os.fork() == 0:      # first child
            os.setsid()         # create new session
            if os.fork() > 0:   # second child
                os._exit(0)
        else:
            os._exit(0)
        return self


def detached(logfile=None, pidfile=None, uid=None, gid=None, umask=0,
             workdir=None, fake=False, **opts):
    """Detach the current process in the background (daemonize).

    :keyword logfile: Optional log file.  The ability to write to this file
       will be verified before the process is detached.
    :keyword pidfile: Optional pidfile.  The pidfile will not be created,
      as this is the responsibility of the child.  But the process will
      exit if the pid lock exists and the pid written is still running.
    :keyword uid: Optional user id or user name to change
      effective privileges to.
    :keyword gid: Optional group id or group name to change effective
      privileges to.
    :keyword umask: Optional umask that will be effective in the child process.
    :keyword workdir: Optional new working directory.
    :keyword fake: Don't actually detach, intented for debugging purposes.
    :keyword \*\*opts: Ignored.

    **Example**:

    .. code-block:: python

        from celery.platforms import detached, create_pidlock

        with detached(logfile='/var/log/app.log', pidfile='/var/run/app.pid',
                      uid='nobody'):
            # Now in detached child process with effective user set to nobody,
            # and we know that our logfile can be written to, and that
            # the pidfile is not locked.
            pidlock = create_pidlock('/var/run/app.pid')

            # Run the program
            program.run(logfile='/var/log/app.log')

    """

    if not resource:
        raise RuntimeError('This platform does not support detach.')
    workdir = os.getcwd() if workdir is None else workdir

    signals.reset('SIGCLD')  # Make sure SIGCLD is using the default handler.
    maybe_drop_privileges(uid=uid, gid=gid)

    def after_chdir_do():
        # Since without stderr any errors will be silently suppressed,
        # we need to know that we have access to the logfile.
        logfile and open(logfile, 'a').close()
        # Doesn't actually create the pidfile, but makes sure it's not stale.
        if pidfile:
            _create_pidlock(pidfile).release()

    return DaemonContext(
        umask=umask, workdir=workdir, fake=fake, after_chdir=after_chdir_do,
    )


def parse_uid(uid):
    """Parse user id.

    uid can be an integer (uid) or a string (user name), if a user name
    the uid is taken from the system user registry.

    """
    try:
        return int(uid)
    except ValueError:
        try:
            return pwd.getpwnam(uid).pw_uid
        except (AttributeError, KeyError):
            raise KeyError('User does not exist: {0}'.format(uid))


def parse_gid(gid):
    """Parse group id.

    gid can be an integer (gid) or a string (group name), if a group name
    the gid is taken from the system group registry.

    """
    try:
        return int(gid)
    except ValueError:
        try:
            return grp.getgrnam(gid).gr_gid
        except (AttributeError, KeyError):
            raise KeyError('Group does not exist: {0}'.format(gid))


def _setgroups_hack(groups):
    """:fun:`setgroups` may have a platform-dependent limit,
    and it is not always possible to know in advance what this limit
    is, so we use this ugly hack stolen from glibc."""
    groups = groups[:]

    while 1:
        try:
            return os.setgroups(groups)
        except ValueError:   # error from Python's check.
            if len(groups) <= 1:
                raise
            groups[:] = groups[:-1]
        except OSError as exc:  # error from the OS.
            if exc.errno != errno.EINVAL or len(groups) <= 1:
                raise
            groups[:] = groups[:-1]


def setgroups(groups):
    """Set active groups from a list of group ids."""
    max_groups = None
    try:
        max_groups = os.sysconf('SC_NGROUPS_MAX')
    except Exception:
        pass
    try:
        return _setgroups_hack(groups[:max_groups])
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise
        if any(group not in groups for group in os.getgroups()):
            # we shouldn't be allowed to change to this group.
            raise


def initgroups(uid, gid):
    """Compat version of :func:`os.initgroups` which was first
    added to Python 2.7."""
    if not pwd:  # pragma: no cover
        return
    username = pwd.getpwuid(uid)[0]
    if hasattr(os, 'initgroups'):  # Python 2.7+
        return os.initgroups(username, gid)
    groups = [gr.gr_gid for gr in grp.getgrall()
              if username in gr.gr_mem]
    setgroups(groups)


def setgid(gid):
    """Version of :func:`os.setgid` supporting group names."""
    os.setgid(parse_gid(gid))


def setuid(uid):
    """Version of :func:`os.setuid` supporting usernames."""
    os.setuid(parse_uid(uid))


def maybe_drop_privileges(uid=None, gid=None):
    """Change process privileges to new user/group.

    If UID and GID is specified, the real user/group is changed.

    If only UID is specified, the real user is changed, and the group is
    changed to the users primary group.

    If only GID is specified, only the group is changed.

    """
    if sys.platform == 'win32':
        return
    if os.geteuid():
        # no point trying to setuid unless we're root.
        if not os.getuid():
            raise AssertionError('contact support')
    uid = uid and parse_uid(uid)
    gid = gid and parse_gid(gid)

    if uid:
        # If GID isn't defined, get the primary GID of the user.
        if not gid and pwd:
            gid = pwd.getpwuid(uid).pw_gid
        # Must set the GID before initgroups(), as setgid()
        # is known to zap the group list on some platforms.

        # setgid must happen before setuid (otherwise the setgid operation
        # may fail because of insufficient privileges and possibly stay
        # in a privileged group).
        setgid(gid)
        initgroups(uid, gid)

        # at last:
        setuid(uid)
        # ... and make sure privileges cannot be restored:
        try:
            setuid(0)
        except OSError as exc:
            if get_errno(exc) != errno.EPERM:
                raise
            pass  # Good: cannot restore privileges.
        else:
            raise RuntimeError(
                'non-root user able to restore privileges after setuid.')
    else:
        gid and setgid(gid)

    if uid and (not os.getuid()) and not (os.geteuid()):
        raise AssertionError('Still root uid after drop privileges!')
    if gid and (not os.getgid()) and not (os.getegid()):
        raise AssertionError('Still root gid after drop privileges!')


class Signals(object):
    """Convenience interface to :mod:`signals`.

    If the requested signal is not supported on the current platform,
    the operation will be ignored.

    **Examples**:

    .. code-block:: python

        >>> from celery.platforms import signals

        >>> from proj.handlers import my_handler
        >>> signals['INT'] = my_handler

        >>> signals['INT']
        my_handler

        >>> signals.supported('INT')
        True

        >>> signals.signum('INT')
        2

        >>> signals.ignore('USR1')
        >>> signals['USR1'] == signals.ignored
        True

        >>> signals.reset('USR1')
        >>> signals['USR1'] == signals.default
        True

        >>> from proj.handlers import exit_handler, hup_handler
        >>> signals.update(INT=exit_handler,
        ...                TERM=exit_handler,
        ...                HUP=hup_handler)

    """

    ignored = _signal.SIG_IGN
    default = _signal.SIG_DFL

    if hasattr(_signal, 'setitimer'):

        def arm_alarm(self, seconds):
            _signal.setitimer(_signal.ITIMER_REAL, seconds)
    else:  # pragma: no cover
        try:
            from itimer import alarm as _itimer_alarm  # noqa
        except ImportError:

            def arm_alarm(self, seconds):  # noqa
                _signal.alarm(math.ceil(seconds))
        else:  # pragma: no cover

            def arm_alarm(self, seconds):      # noqa
                return _itimer_alarm(seconds)  # noqa

    def reset_alarm(self):
        return _signal.alarm(0)

    def supported(self, signal_name):
        """Return true value if ``signal_name`` exists on this platform."""
        try:
            return self.signum(signal_name)
        except AttributeError:
            pass

    def signum(self, signal_name):
        """Get signal number from signal name."""
        if isinstance(signal_name, numbers.Integral):
            return signal_name
        if not isinstance(signal_name, basestring) \
                or not signal_name.isupper():
            raise TypeError('signal name must be uppercase string.')
        if not signal_name.startswith('SIG'):
            signal_name = 'SIG' + signal_name
        return getattr(_signal, signal_name)

    def reset(self, *signal_names):
        """Reset signals to the default signal handler.

        Does nothing if the platform doesn't support signals,
        or the specified signal in particular.

        """
        self.update((sig, self.default) for sig in signal_names)

    def ignore(self, *signal_names):
        """Ignore signal using :const:`SIG_IGN`.

        Does nothing if the platform doesn't support signals,
        or the specified signal in particular.

        """
        self.update((sig, self.ignored) for sig in signal_names)

    def __getitem__(self, signal_name):
        return _signal.getsignal(self.signum(signal_name))

    def __setitem__(self, signal_name, handler):
        """Install signal handler.

        Does nothing if the current platform doesn't support signals,
        or the specified signal in particular.

        """
        try:
            _signal.signal(self.signum(signal_name), handler)
        except (AttributeError, ValueError):
            pass

    def update(self, _d_=None, **sigmap):
        """Set signal handlers from a mapping."""
        for signal_name, handler in dict(_d_ or {}, **sigmap).items():
            self[signal_name] = handler

signals = Signals()