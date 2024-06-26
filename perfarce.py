# Mercurial extension to push to and pull from Perforce depots.
#
# Copyright 2009-16 Frank Kingswood <frank@kingswood-consulting.co.uk>
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2, incorporated herein by reference.

'''Push to or pull from Perforce depots

This extension modifies the remote repository handling so that repository
paths that resemble
    p4://p4server[:port]/clientname[/path/to/directory]
cause operations on the named p4 client specification on the p4 server.
The client specification must already exist on the server before using
this extension. Making changes to the client specification Views causes
problems when synchronizing the repositories, and should be avoided.
If a /path/to/directory is given then only a subset of the p4 view
will be operated on. Multiple partial p4 views can use the same p4
client specification.

Five built-in commands are overridden:

 outgoing  If the destination repository name starts with p4:// then
           this reports files affected by the revision(s) that are
           in the local repository but not in the p4 depot.

 push      If the destination repository name starts with p4:// then
           this exports changes from the local repository to the p4
           depot. If no revision is specified then all changes since
           the last p4 changelist are pushed. In either case, all
           revisions to be pushed are folded into a single p4 changelist.
           Optionally the resulting changelist is submitted to the p4
           server, controlled by the --submit option to push, or by
           setting
              --config perfarce.submit=True
           If the option
              --config perfarce.keep=False
           is False then after a successful submit the files in the
           p4 workarea will be deleted.

 pull      If the source repository name starts with p4:// then this
           imports changes from the p4 depot, automatically creating
           merges of changelists submitted by hg push.
           If the option
              --config perfarce.keep=False
           is False then the import does not leave files in the p4
           workarea, otherwise the p4 workarea will be updated
           with the new files.
           The option
              --config perfarce.tags=False
           can be used to disable pulling p4 tags (a.k.a. labels).
           The option
              --config perfarce.pull_trim_log=False
           can be used to remove the {{mercurial}} node IDs from both
           p4 and the imported changes. Use with care as this is a
           non-reversible operation.
              --config perfarce.clientuser=script_or_regex
           can be used to enable quasi-multiuser operation, where
           several users submit changes to p4 with the same user name
           and have their real user name in the p4 client spec.
           If the value of this parameter contains at least one space
           then it is split into a search regular expression and
           replacement string.  The search and replace regular expressions
           describe the substitution to be made to turn a client spec name
           into a user name. If the search regex does not match then the
           username is left unchanged.
           If the value of this parameter has no spaces then it is
           taken as the name of a script to run. The script is run
           with the client and user names as arguments. If the script
           produces output then this is taken as the user name,
           otherwise the username is left unchanged.

 incoming  If the source repository name starts with p4:// then this
           reports changes in the p4 depot that are not yet in the
           local repository.

 clone     If the source repository name starts with p4:// then this
           creates the destination repository and pulls all changes
           from the p4 depot into it.
           If the option
              --config perfarce.lowercasepaths=False
           is True then the import forces all paths in lowercase,
           otherwise paths are recorded unchanged.  Filename case is
           preserved.
           If the option
              --config perfarce.ignorecase=False
           is True then the import ignores all case differences in
           the p4 depot. Directory and filename case is preserved.
           These two setting are workarounds to handle Perforce depots
           containing a path spelled differently from file to file
           (e.g. path/foo and PAth/bar are in the same directory),
           or where the same file may be spelled differently from time
           to time (e.g. path/foo and path/FOO are the same object).
'''
from __future__ import print_function
from mercurial import commands, context, copies, encoding, error, extensions, hg, phases, pycompat, registrar, scmutil, util
from mercurial.node import hex, short, nullid
from mercurial.i18n import _
from mercurial.error import ConfigError
try:
    from mercurial.interfaces.repository import peer as peerrepository
except ImportError:
    # Mercurial 5.1.2 and older
    from mercurial.repository import peer as peerrepository
import marshal, os, re, string, sys
propertycache=util.propertycache

try:
    from mercurial.utils.procutil import shellquote
except ImportError:
    # Mercurial 4.5.3 and older
    from mercurial.util import shellquote

try:
    from mercurial.utils.dateutil import datestr
except ImportError:
    # Mercurial 4.5.3 and older
    from mercurial.util import datestr

try:
    from mercurial.scmutil import revsymbol
except ImportError:
    # Mercurial 4.6.2 and older
    def revsymbol(repo, symbol):
        return symbol

try:
    from mercurial.utils.urlutil import get_unique_pull_path_obj
    def _pull_path(action, repo, ui, source):
        return get_unique_pull_path_obj(action, ui, source).rawloc
except ImportError:
    # Mercurial 6.3.3 and older
    try:
        from mercurial.utils.urlutil import get_unique_pull_path
        def _pull_path(action, repo, ui, source):
            return get_unique_pull_path(action, repo, ui, source)[0]
    except ImportError:
        # Mercurial 5.7.1 and older
        def _pull_path(action, repo, ui, source):
            return ui.expandpath(source)

try:
    from mercurial.utils.urlutil import get_unique_push_path
    def _push_path(repo, ui, dest=None):
        return get_unique_push_path(b'push', repo, ui, _push_dest(ui, dest)).rawloc
except ImportError:
    # Mercurial 5.7.1 and older
    def _push_path(repo, ui, dest=None):
        return ui.expandpath(_push_dest(ui, dest))


def _push_dest(ui, dest):
    if dest is None:
        if b'default-push' in ui.paths:
            dest = b'default-push'
        elif b'default' in ui.paths:
            dest = b'default'
    return dest


try:
    from mercurial.utils.urlutil import get_clone_path
    def _clone_path(ui, source):
        return get_clone_path(ui, source)[0]
except ImportError:
    # Mercurial 5.7.1 and older
    def _clone_path(ui, source):
        return ui.expandpath(source)

try:
    from mercurial.utils.urlutil import urllocalpath
except ImportError:
    # Mercurial 5.7.1 and older
    from mercurial.util import urllocalpath

if sys.version[0] == '2':
    # py2 must use os.popen, because there marshal wants a true file
    from os import popen
else:
    try:
        from mercurial.utils.procutil import popen
    except ImportError:
        # Mercurial 4.5.3 and older
        from os import popen


file = open

cmdtable = {}
command = registrar.command(cmdtable)

if tuple(util.version().split(b".",2)) < (b"4",b"6"):
    # Mercurial 4.5.3 and older
    def revpairnodes(repo, rev):
        n1, n2 = scmutil.revpair(repo, rev)
        ctx1 = repo[n1]
        ctx2 = repo[n2] if n2 else repo[None]
        return ctx1, ctx2
else:
    def revpairnodes(repo, rev):
        return scmutil.revpair(repo, rev)

if tuple(util.version().split(b".",2)) < (b"6",b"4"):
    # Mercurial 6.3.3 and older
    class peer:
        def __init__(self, ui, path=None):
            self.ui = ui
            self.path = path
else:
    peer = peerrepository

def uisetup(ui):
    '''monkeypatch pull and push for p4:// support'''

    extensions.wrapcommand(commands.table, b'pull', pull)
    p = extensions.wrapcommand(commands.table, b'push', push)
    p[1].append((b'', b'submit', None, 'for p4:// destination submit new changelist to server'))
    p[1].append((b'', b'job', [], b'for p4:// destination set job id(s)'))
    extensions.wrapcommand(commands.table, b'incoming', incoming)
    extensions.wrapcommand(commands.table, b'outgoing', outgoing)
    p = extensions.wrapcommand(commands.table, b'clone', clone)
    p[1].append((b'', b'startrev', b'', b'for p4:// source set initial revisions for clone'))
    p[1].append((b'', b'encoding', b'', b'for p4:// source set encoding used by server'))
    try:
        hg.peer_schemes[b'p4'] = p4repo
    except AttributeError:
        # Mercurial 6.3.3 and older
        hg.schemes[b'p4'] = p4repo
        # Mercurial 5.1.2 and older
        hg.schemes['p4'] = p4repo

# --------------------------------------------------------------------------

class p4repo(peer):
    'Dummy repository class so we can use -R for p4submit and p4revert'
    def __init__(self, ui, path):
        super(p4repo, self).__init__(ui, path=path)

    @staticmethod
    def make_peer(ui, path, create, intents=None, createopts=None, remotehidden=False):
        if create:
            raise error.Abort(_(b'cannot create new p4 repository'))
        return p4repo(ui, path)

    # Mercurial 6.3.3 and older
    @staticmethod
    def instance(*args, **kwargs):
        return p4repo.make_peer(*args, **kwargs)

    def local(self):
        return True

    def __getattr__(self, a):
        raise error.Abort(_(b'%s not supported for p4') % str.encode(a))


def loaditer(f):
    "Yield the dictionary objects generated by p4"
    try:
        while True:
            d = marshal.load(f)
            if not d:
                break
            yield d
    except EOFError:
        pass

class p4notclient(error.Abort):
    "Exception raised when a path is not a p4 client or invalid"
    pass

class p4badclient(error.Abort):
    "Exception raised when a path is an invalid p4 client"
    pass

class TempFile:
    "Temporary file"
    def __init__(self, mode):
        import tempfile
        fd, self.Name = tempfile.mkstemp(prefix='hg-p4-')
        if mode:
            self.File = os.fdopen(fd, mode)
        else:
            os.close(fd)
            self.File = None

    def close(self):
        if self.File:
            self.File.close()
            self.File=None

    def __del__(self):
        self.close()
        try:
            os.unlink(self.Name)
        except Exception:
            pass

def int_to_bytes(x):
    if isinstance(x, bytes):
        return x

    return str(x).encode()

def encode_bool(b):
    if isinstance(b, bytes):
        return b

    if b:
        return b"true"
    return b"false"

class p4client(object):

    def __init__(self, ui, repo, path):
        'initialize a p4client class from the remote path'

        if not path.startswith(b'p4:'):
            raise p4notclient(_(b'%s not a p4 repository') % path)
        if not path.startswith(b'p4://'):
            raise p4badclient(_(b'%s not a p4 repository') % path)

        self.ui = ui
        self.repo = repo
        self.server = None      # server name:port
        self.client = None      # client spec name
        self.root = None        # root directory of client workspace
        self.partial = None     # tail of path for partial checkouts (ending in /), or empty string
        self.rootpart = None    # root+partial directory in client workspace (ending in /)

        self.keep = ui.configbool(b'perfarce', b'keep', True)
        self.lowercasepaths = ui.configbool(b'perfarce', b'lowercasepaths', False)
        self.ignorecase = ui.configbool(b'perfarce', b'ignorecase', False)

        # caches
        self.clientspec = {}
        self.usercache = {}
        self.p4stat = None
        self.p4pending = None

        s, c = path[5:].split(b'/', 1)
        if b':' not in s:
            s = b'%s:1666' % s
        self.server = s
        if c:
            if b'/' in c:
                c, p = c.split(b'/', 1)
                p = b'/'.join(q for q in p.split(b'/') if q)
                if p:
                    p += b'/'
            else:
                p = b''

            d = self.runone(b'client -o %s' % shellquote(c), abort=False)
            if not isinstance(d, dict):
                raise p4badclient(_(b'%s is not a valid p4 client') % path)
            code = d.get(b'code')
            if code == b'error':
                data=d[b'data'].strip()
                ui.warn('%s\n' % data)
                raise p4badclient(_(b'%s is not a valid p4 client: %s') % (path, data))

            if sys.platform.startswith("cygwin"):
                re_dospath = re.compile('[a-z]:\\\\',re.I)
                def isdir(d):
                    return os.path.isdir(d) and not re_dospath.match(d)
            else:
                isdir=os.path.isdir

            for n in [b'Root'] + [b'AltRoots%d' % i for i in range(9)]:
                if n in d and isdir(d[n]):
                    self.root = util.pconvert(d[n])
                    break
            if not self.root:
                ui.note(_(b'the p4 client root must exist\n'))
                raise p4badclient(_(b'the p4 client root must exist\n'))

            self.clientspec = d
            self.client = c
            self.partial = p
            if p:
                if self.lowercasepaths:
                    p = self.normcase(p)
                p = os.path.join(self.root, p)
            else:
                p = self.root
            self.rootpart = util.pconvert(p)
            if not self.rootpart.endswith(b'/'):
                self.rootpart += b'/'
            if self.root.endswith(b'/'):
                self.root = self.root[:-1]

    def find(self, rev=None, base=False, p4rev=None, abort=True):
        '''Find the most recent revision which has the p4 extra data which
        gives the p4 changelist it was converted from. If base is True then
        return the most recent child of that revision where the only changes
        between it and the p4 changelist are to .hg files.
        Returns the revision and p4 changelist number'''

        def dothgonly(ctx):
            'returns True if only .hg files in this context'

            if not ctx.files():
                # no files means this must have been a merge
                return False

            for f in ctx.files():
                if not f.startswith(b'.hg'):
                    return False
            return True

        try:
            mqnode = [self.repo[revsymbol(self.repo, b'qbase')].node()]
        except Exception:
            mqnode = None

        if rev is None:
            rev = revsymbol(self.repo, b'default')
        current = self.repo[rev]

        current = [(current,())]
        seen = set()
        while current:
            next_items = []
            self.ui.debug(b"find: %s\n" % (b" ".join(hex(c[0].node()) for c in current)))
            for ctx,path in current:
                extra = ctx.extra()
                if b'p4' in extra:
                    if base:
                        while path:
                            if dothgonly(path[0]) and not (mqnode and
                                   self.repo.changelog.nodesbetween(mqnode, [ctx.node()])[0]):
                                ctx = path[0]
                                path = path[1:]
                            else:
                                path = []
                    p4 = int(extra[b'p4'])
                    if not p4rev or p4==p4rev:
                        return ctx.node(), p4

                for p in ctx.parents():
                    if p and p not in seen:
                        seen.add(p)
                        next_items.append((p, (ctx,) + path))

            current = next_items

        if abort:
            raise error.Abort(_(b'no p4 changelist revision found'))
        return nullid, 0

    @propertycache
    def re_type(self): return re.compile(b'([a-z]+)?(text|binary|symlink|apple|resource|unicode|utf\d+)(\+\w+)?$')
    @propertycache
    def re_keywords(self): return re.compile(br'\$(Id|Header|Date|DateTime|Change|File|Revision|Author):[^$\n]*\$')
    @propertycache
    def re_keywords_old(self): return re.compile(b'\$(Id|Header):[^$\n]*\$')

    def decodetype(self, p4type):
        'decode p4 type name into mercurial mode string and keyword substitution regex'

        base = mode = b''
        keywords = None
        utf16 = False
        p4type = self.re_type.match(p4type)
        if p4type:
            base = p4type.group(2)
            flags = (p4type.group(1) or b'') + (p4type.group(3) or b'')
            if b'x' in flags:
                mode = b'x'
            if base == b'symlink':
                mode = b'l'
            if base == b'utf16':
                utf16 = True
            if b'ko' in flags:
                keywords = self.re_keywords_old
            elif b'k' in flags:
                keywords = self.re_keywords
        return base, mode, keywords, utf16


    @propertycache
    def encoding(self):
        # work out character set for p4 text (but not filenames)
        emap = { 'none': 'ascii',
                 'utf8-bom': 'utf_8_sig',
                 'macosroman': 'mac-roman',
                 'winansi': 'cp1252' }
        e = os.environ.get("P4CHARSET")
        if e:
            return emap.get(e,e)
        return self.ui.config(b'perfarce', b'encoding', None).decode()

    def decode(self, text):
        'decode text in p4 character set as utf-8'
        if self.encoding:
            try:
                return text.decode(self.encoding).encode(str(encoding.encoding, encoding='ascii'))
            except LookupError as e:
                raise error.Abort("%s, please check your locale settings" % e)
        return text

    def encode(self, text):
        'encode utf-8 text to p4 character set'

        if self.encoding:
            try:
                return text.decode(str(encoding.encoding, encoding='ascii')).encode(self.encoding)
            except LookupError as e:
                raise error.Abort("%s, please check your locale settings" % e)
        return text


    @staticmethod
    def encodename(name):
        'escape @ # % * characters in a p4 filename'
        return name.replace(b'%',b'%25').replace(b'@',b'%40').replace(b'#',b'%23').replace(b'*',b'%2A')


    @staticmethod
    def normcase(name):
        'convert path name to lower case'
        return os.path.normpath(name).lower()

    @propertycache
    def re_hgid(self): return re.compile(b'{{mercurial (([0-9a-f]{40})(:([0-9a-f]{40}))?)}}')

    def parsenodes(self, desc):
        'find revisions in p4 changelist description'
        m = self.re_hgid.search(desc)
        nodes = []
        if m:
            try:
                nodes = self.repo.changelog.nodesbetween(
                    [self.repo[m.group(2)].node()], [self.repo[m.group(4) or m.group(2)].node()])[0]
            except Exception:
                if self.ui.traceback:self.ui.traceback()
                self.ui.note(_(b'ignoring hg revision range %s from p4\n' % m.group(1)))
        return nodes, m

    @propertycache
    def maxargs(self):
        try:
            r = self.ui.configint(b'perfarce', b'maxargs', 0)
        except ConfigError:
            r = 0
        if r<1:
            if os.name == 'posix':
                r = 250
            else:
                r = 25
        return r


    def run(self, cmd, files=[], abort=True, client=None):
        'Run a P4 command and yield the objects returned'
        c = [b'p4', b'-G']
        if self.server:
            c.append(b'-p')
            c.append(self.server)
        if client or self.client:
            c.append(b'-c')
            c.append(client or self.client)
        if self.root:
            c.append(b'-d')
            c.append(shellquote(self.root))

        if files and len(files)>self.maxargs:
            tmp = TempFile('w')
            for f in files:
                if self.ui.debugflag: self.ui.debug(b'> -x %s\n' % f)
                print(f.decode(), file=tmp.File)
            tmp.close()
            c.append(b'-x')
            c.append(str.encode(tmp.Name))
            files = []

        c.append(cmd)

        cs = b' '.join(c + [shellquote(f) for f in files])
        if self.ui.debugflag: self.ui.debug(b'> %s\n' % cs)

        for d in loaditer(popen(cs, b'rb')):
            if self.ui.debugflag: self.ui.debug(b'< %r\n' % d)
            code = d.get(b'code')
            data = d.get(b'data')
            if code is not None and data is not None:
                data = data.strip()
                if abort and code == b'error':
                    raise error.Abort(b'p4: %s' % data)
                elif code == b'info':
                    self.ui.note(b'p4: %s\n' % data)
            yield d

    def runs(self, cmd, **args):
        '''Run a P4 command, discarding any output (except errors)'''
        for d in self.run(cmd, **args):
            pass

    def runone(self, cmd, **args):
        '''Run a P4 command and return the object returned'''

        value=None
        for d in self.run(cmd, **args):
            if value is None:
                value = d
            else:
                raise error.Abort(_(b'p4 %s returned more than one object') % cmd)
        if value is None:
            raise error.Abort(_(b'p4 %s returned no objects') % cmd)
        return value


    def getpending(self, node):
        '''returns True if node is pending in p4 or has been submitted to p4'''
        if self.p4stat is None:
            self._readp4stat()
        return node.node() in self.p4stat

    def getpendinglist(self):
        'return p4 submission state dictionary'
        if self.p4stat is None:
            self._readp4stat()
        return self.p4pending

    def _readp4stat(self):
        '''read pending and submitted changelists into pending cache'''
        self.p4stat = set()
        self.p4pending = []

        p4rev, p4id = self.find(abort=False)

        def helper(self,d,p4id):
            c = int(d[b'change'])
            if c == p4id:
                return

            desc = d[b'desc']
            nodes, match = self.parsenodes(desc)
            entry = (c, d[b'status'] == b'submitted', nodes, desc, d[b'client'])
            self.p4pending.append(entry)
            for n in nodes:
                self.p4stat.add(n)

        change = b'%s...@%d,#head' % (self.partial, p4id)
        for d in self.run(b'changes -l -c %s %s' %
                           (shellquote(self.client), shellquote(change))):
            helper(self,d,p4id)
        for d in self.run(b'changes -l -c %s -s pending' %
                           (shellquote(self.client))):
            helper(self,d,p4id)
        self.p4pending.sort()


    def repopath(self, path):
        'Convert a p4 client path to a path relative to the hg root'
        if self.lowercasepaths:
            pathname, fname = os.path.split(path)
            path = os.path.join(self.normcase(pathname), fname)

        path = util.pconvert(path)
        #print(path, self.rootpart, file=sys.stderr)
        if not path.startswith(self.rootpart):
            raise error.Abort(_(b'invalid p4 local path %s') % path)

        return path[len(self.rootpart):]

    def localpath(self, path):
        'Convert a path relative to the hg root to a path in the p4 workarea'
        return util.localpath(os.path.join(self.rootpart, path))


    def getuser(self, user, client=None):
        'get full name and email address of user (and optionally client spec name)'
        r = self.usercache.get((user,None)) or self.usercache.get((user,client))
        if r:
            return r

        # allow mapping the client name into a user name
        cu = self.ui.config(b"perfarce",b"clientuser")

        if cu and b" " in cu:
            cus, cur = cu.split(b" ", 1)
            u, f = re.subn(cus, cur, client)
            if f:
                r = string.capwords(u)
                self.usercache[(user, client)] = r
                return r

        elif cu:
            cmd = b"%s %s %s" % (util.expandpath(cu), shellquote(client), shellquote(user))
            self.ui.debug(b'> %s\n' % cmd)

            old = os.getcwd()
            try:
                os.chdir(self.root)
                r = None
                for r in popen(cmd):
                    r = r.strip()
                    self.ui.debug(b'< %r\n' % r)
                if r:
                    self.usercache[(user, client)] = r
                    return r
            finally:
                os.chdir(old)

        else:
            d = self.runone(b'user -o %s' % shellquote(user), abort=False)
            if b'Update' in d:
                try:
                    r = b'%s <%s>' % (d[b'FullName'], d[b'Email'])
                    self.usercache[(user, None)] = r
                    return r
                except Exception:
                    pass

        return user


    @propertycache
    def re_changeno(self): return re.compile(b'Change ([0-9]+) created.+')

    def change(self, change=None, description=None, update=False, jobs=None):
        '''Create a new p4 changelist or update an existing changelist with
        the given description. Returns the changelist number as a string.'''

        # get changelist data, and update it
        if isinstance(change, int) or isinstance(change, str):
            changelist = self.runone(b'change -o %s' % (str(change).encode('ascii')))
        if isinstance(change, bytes):
            changelist = self.runone(b'change -o %s' % change)
        if change is None:
            changelist = self.runone(b'change -o ')

        if jobs:
            for i,j in enumerate(jobs):
                changelist[b'Jobs%d'%i] = self.encode(j)

        if description is not None:
            changelist[b'Description'] = self.encode(description)

        # write changelist data to a temporary file
        tmp = TempFile('wb')
        marshal.dump(changelist, tmp.File, 0)
        tmp.close()

        # update p4 changelist
        d = self.runone(b'change -i%s <%s' % (update and b" -u" or b"", shellquote(tmp.Name.encode('utf-8'))))
        data = d[b'data']
        if d[b'code'] == b'info':
            if not self.ui.verbose:
                self.ui.status(b'p4: %s\n' % data)
            if not change:
                m = self.re_changeno.match(data)
                if m:
                    change = m.group(1)
        else:
            raise error.Abort(_(b'error creating p4 change: %s') % data)

        if not change:
            raise error.Abort(_(b'did not get changelist number from p4'))

        # invalidate cache
        self.p4stat = None

        return change


    class description:
        'Changelist description'
        def __init__(self, **args):
            self.__dict__.update(args)
        def __repr__(self):
            return "%s(%s)"%(self.__class__.__name__,
                       ", ".join("%s=%r"%(k,getattr(self,k)) for k in sorted(self.__dict__.keys())))

    actions = { b'add':b'A', b'branch':b'A', b'move/add':b'A',
                b'edit':b'M', b'integrate':b'M', b'import':b'A',
                b'delete':b'R', b'move/delete':b'R', b'purge':b'R',
              }

    def describe(self, change, local=None, shelve=False):
        '''Return p4 changelist description object with user name and date.
        If the local is true, then also collect a list of 5-tuples
            (depotname, revision, type, action, localname)
        If local is false then the files list returned holds 4-tuples
            (depotname, revision, type, action)
        Retrieving the local filenames is potentially very slow, even more
        so when this is used on pending changelists.
        '''

        d = self.runone(b'describe -%s %s' % (b"S" if shelve else b"s", int_to_bytes(change)))
        client = d[b'client']
        status = d[b'status']
        r = self.description(change=d[b'change'],
                             desc=self.decode(d[b'desc']),
                             user=self.getuser(self.decode(d[b'user']), client),
                             date=(int(d[b'time']), 0),     # p4 uses UNIX epoch
                             status=status,
                             client=client)

        files = {}
        if local and status=='submitted':
            r.files = self.fstat(change)
        else:
            r.files = []
            i = 0
            while True:
                df = b'depotFile%d' % i
                if df not in d:
                    break
                df = d[df]
                rv = d[b'rev%d' % i]
                tp = d[b'type%d' % i]
                ac = d[b'action%d' % i]
                files[df] = item = (df, int(rv), tp, self.actions[ac])
                r.files.append(item)
                i += 1

        r.jobs = []
        i = 0
        while True:
            jn = b'job%d' % i
            if jn not in d:
                break
            r.jobs.append(d[jn])
            i += 1

        if local and files:
            r.files = []
            for d in self.run(b'where', files=[f for f in files]):
                r.files.append(files[d[b'depotFile']] + (self.repopath(d[b'path']),))

        return r


    def fstat(self, change=None, all=False, files=[]):
        '''Find local names for all the files belonging to a changelist.
        Returns a list of tuples
            (depotname, revision, type, action, localname)
        with only entries for files that appear in the workspace.
        If all is unset considers only files modified by the
        changelist, otherwise returns all files *at* that changelist.
        '''
        result = []

        if files:
            p4cmd = b'fstat'
        elif all:
            p4cmd = b'fstat %s' % shellquote(b'%s...@%d' % (self.partial, change))
        else:
            p4cmd = b'fstat -e %d %s' % (change, shellquote(b'%s...' % self.partial))

        progress = _makeprogress(self.ui, b'p4 fstat', unit=b'entries', total=len(files))
        for d in self.run(p4cmd, files=files):
            if b'desc' in d:
                continue
            progress.increment(item=d[b'depotFile'])
            if d[b'clientFile'].startswith(b'.hg'):
                continue
            lf = self.repopath(d[b'clientFile'])
            df = d[b'depotFile']
            rv = d.get(b'headRev', 0)
            tp = d.get(b'headType', b'')
            ac = d.get(b'headAction', b'add')
            result.append((df, int(rv), tp, self.actions[ac], lf))

        progress.complete()
        self.ui.note(_(b'%d files \n') % len(result))

        return result


    def sync(self, change, fake=False, force=False, all=False, files=[]):
        '''Synchronize the client with the depot at the given change.
        Setting fake adds -k, force adds -f option. The all option is
        not used here, but indicates that the caller wants all the files
        at that revision, not just the files affected by the change.'''

        cmd = b'sync'
        if fake:
            cmd += b' -k'
        elif force:
            cmd += b' -f'
        if not files:
            cmd += b' ' + shellquote(b'%s...@%d' % (self.partial, change))

        n = 0
        progress = _makeprogress(self.ui, b'p4 sync', unit=b'files')
        for d in self.run(cmd, files=[(b"%s@%d" % (os.path.join(self.partial, f), change)) for f in files], abort=False):
            n += 1
            progress.increment()
            code = d.get(b'code')
            if code == b'error':
                data = d[b'data'].strip()
                if d[b'generic'] == 17 or d[b'severity'] == 2:
                    self.ui.note(b'p4: %s\n' % data)
                else:
                    raise error.Abort(b'p4: %s' % data)
        progress.complete()

        if files and n < len(files):
            raise error.Abort(_(b'incomplete reply from p4, reduce maxargs'))

    def getfile(self, entry):
        '''Return contents of a file in the p4 depot at the given revision number.
        Entry is a tuple
            (depotname, revision, type, action, localname)
        If self.keep is set, assumes that the client is in sync.
        Raises IOError or returns None,None if the file is deleted (depending on version).
        '''

        if entry[3] == b'R':
            return None, None

        try:
            basetype, mode, keywords, utf16 = self.decodetype(entry[2])

            if self.keep:
                fn = self.localpath(entry[4])
                if mode == b'l':
                    try:
                        contents = os.readlink(fn)
                    except AttributeError:
                        contents = file(fn, 'rb').read()
                        if contents.endswith('\n'):
                            contents = contents[:-1]
                else:
                    contents = file(fn, 'rb').read()
            else:
                cmd = b'print'
                if utf16:
                    tmp = TempFile(None)
                    tmp.close()
                    cmd += b' -o %s'%shellquote(tmp.Name)
                cmd += b' %s#%d' % (shellquote(entry[0]), entry[1])

                contents = []
                for d in self.run(cmd):
                    code = d[b'code']
                    if code == b'text' or code == b'binary':
                        contents.append(d[b'data'])

                if utf16:
                    contents = file(tmp.Name, 'rb').read()
                else:
                    contents = b''.join(contents)

                if mode == b'l' and contents.endswith('\n'):
                    contents = contents[:-1]

            if keywords:
                contents = keywords.sub('$\\1$', contents)

            return mode, contents
        except Exception as e:
            if self.ui.traceback:self.ui.traceback()
            raise error.Abort(_(b'file %s missing in p4 workspace') % entry[4])


    @propertycache
    def tags(self):
        try:
            t = self.ui.configint(b'perfarce', b'tags', -1)
        except (ConfigError,ValueError) as e:
            t = -1
        if t<0 or t>2:
            t = self.ui.configbool(b'perfarce', b'tags', True)
        return t

    def labels(self, change):
        'Return p4 labels a.k.a. tags at the given changelist'

        tags = []
        if self.tags:
            change = b'%s...@%d,%d' % (self.partial, change, change)
            for d in self.run(b'labels %s' % shellquote(change)):
                l = d.get(b'label')
                if l:
                    tags.append(l)

        return tags


    def submit(self, change):
        '''submit one changelist to p4 and optionally delete the files added
        or modified in the p4 workarea'''

        cl = None
        for d in self.run(b'submit -c %s' % int_to_bytes(change)):
            if d[b'code'] == b'error':
                raise error.Abort(_(b'error submitting p4 change %s: %s') % (int_to_bytes(change), d['data']))
            cl = d.get(b'submittedChange', cl)

        self.ui.note(_(b'submitted changelist %s\n') % cl)

        if not self.keep:
            # delete the files in the p4 client directory
            self.sync(0)

        # invalidate cache
        self.p4stat = None


    def hasmovecopy(self):
        '''detect whether p4 move and p4 copy are supported.
        these advanced features are available since about 2009.1 or so.'''

        mc = []
        for op in b'move',b'copy':
            v = self.ui.configbool(b'perfarce', op, None)
            if v is None:
                self.ui.note(_(b'checking if p4 %s is supported, set perfarce.%s to skip this test\n') % (op, op))
                d = self.runone(b'help %s' % op, abort=False)
                v = d[b'code']==b'info'
                self.ui.debug(_(b'p4 %s is %ssupported\n') % (op, [b"not ",b""][v]))
            mc.append(v)

        return tuple(mc)


def _pullclient(ui, repo, source, opts):
    if opts.get(b'mq',None):
        return None

    source = _pull_path(b'pull', repo, ui, source or b'default')
    try:
        return p4client(ui, repo, source)
    except p4notclient:
        if ui.traceback:ui.traceback()
        return None
    except p4badclient as e:
        if ui.traceback:ui.traceback()
        raise error.Abort(str(e))


def _pullcommon(repo, client, opts, startrev=0):
    'Shared code for pull and incoming'

    # if present, --rev will be the last Perforce changeset number to get
    stoprev = opts.get(b'rev')
    stoprev = stoprev and max(int(r) for r in stoprev) or 0

    if len(repo):
        p4rev, p4id = client.find(base=True, abort=not opts[b'force'])
    else:
        p4rev, p4id = None, 0
    p4id = max(p4id, startrev)

    if stoprev:
        p4cset = b'%s...@%d,@%d' % (client.partial, p4id, stoprev)
    else:
        p4cset = b'%s...@%d,#head' % (client.partial, p4id)
    p4cset = shellquote(p4cset)

    if startrev < 0:
        # most recent changelists
        p4cmd = b'changes -s submitted -m %d -L %s' % (-startrev, p4cset)
    else:
        p4cmd = b'changes -s submitted -L %s' % p4cset

    changes = []
    for d in client.run(p4cmd):
        c = int(d[b'change'])
        if startrev or c != p4id:
            changes.append(c)
    changes.sort()

    return p4rev, changes


def _pushclient(ui, repo, dest, opts):
    if opts.get(b'mq',None):
        return None

    dest = _push_path(repo, ui, dest)
    try:
        return p4client(ui, repo, dest)
    except p4notclient:
        if ui.traceback: ui.traceback()
        return None
    except p4badclient as e:
        raise error.Abort(str(e))


def _pushcommon(ui, repo, client, opts):
    'Shared code for push and outgoing'

    p4rev, p4id = client.find(base=True, abort=not opts[b'force'])
    rev = opts.get(b'rev')

    if rev:
        ctx1, ctx2 = revpairnodes(repo, rev)
        if not ctx2.node():
            ctx2 = ctx1
        ctx1 = ctx1.parents()[0]
    else:
        ctx1 = repo[p4rev]
        ctx2 = repo[b'tip']

    nodes = repo.changelog.nodesbetween([ctx1.node()], [ctx2.node()])[0][bool(p4id):]

    if not opts[b'force']:
        # trim off nodes at either end that have already been pushed
        trim = False
        for end in [0, -1]:
            while nodes:
                n = repo[nodes[end]]
                if client.getpending(n):
                    del nodes[end]
                    trim = True
                else:
                    break

        # recalculate the context
        if trim and nodes:
            ctx1 = repo[nodes[0]].parents()[0]
            ctx2 = repo[nodes[-1]]

        if ui.debugflag:
            for n in nodes:
                ui.debug(b'outgoing %s\n' % hex(n))

        # check that remaining nodes have not already been pushed
        for n in nodes:
            n = repo[n]
            fail = False
            if client.getpending(n):
                fail = True
            for ctx3 in n.children():
                extra = ctx3.extra()
                if b'p4' in extra:
                    fail = True
                    break
            if fail:
                raise error.Abort(_(b'can not push, changeset %s is already in p4' % n))

    if not nodes:
        ui.status(_(b'no changes found\n'))
        return None

    # find changed files
    mod, add, rem = tuple(repo.status(node1=ctx1.node(), node2=ctx2.node()))[:3]
    mod = [(f, ctx2.flags(f)) for f in mod]
    add = [(f, ctx2.flags(f)) for f in add]
    rem = [(f, b"") for f in rem]

    cpy = copies.pathcopies(ctx1, ctx2)
    # remember which copies change the data
    for c in cpy:
        chg = ctx2.flags(c) != ctx1.flags(c) or ctx2[c].data() != ctx1[cpy[c]].data()
        cpy[c] = (cpy[c], chg)

    # remove .hg* files (mainly for .hgtags and .hgignore)
    for changes in [mod, add, rem]:
        i = 0
        while i < len(changes):
            f = changes[i][0]
            if f.startswith(b'.hg'):
                del changes[i]
            else:
                i += 1

    if not (mod or add or rem):
        ui.status(_(b'no changes found\n'))
        return None

    # detect MQ
    try:
        mq = repo.changelog.nodesbetween([repo[revsymbol(repo, b'qbase')].node()], nodes)[0]
        if mq:
            if opts[b'force']:
                ui.warn(_(b'source has mq patches applied\n'))
            else:
                raise error.Abort(_(b'source has mq patches applied'))
    except error.RepoError:
        pass
    except error.RepoLookupError:
        pass

    # create description
    desc = []
    for n in nodes:
        desc.append(repo[n].description())

    if len(nodes) > 1:
        h = [repo[nodes[0]].hex()]
    else:
        h = []
    h.append(repo[nodes[-1]].hex())

    desc=b'\n* * *\n'.join(desc) + b'\n\n{{mercurial %s}}\n' % (b':'.join(h))

    if ui.debugflag:
        ui.debug(b'mod = %r\n' % (mod,))
        ui.debug(b'add = %r\n' % (add,))
        ui.debug(b'rem = %r\n' % (rem,))
        ui.debug(b'cpy = %r\n' % (cpy,))

    return (p4rev, p4id, nodes, ctx2, desc, mod, add, rem, cpy)


# --------------------------------------------------------------------------

def incoming(original, ui, repo, source=None, **opts):
    '''show changes that would be pulled from the p4 source repository
    Returns 0 if there are incoming changes, 1 otherwise.
    '''

    opts = pycompat.byteskwargs(opts)
    client = _pullclient(ui, repo, source, opts)
    if client is None:
        return original(ui, repo, *(source and [source] or []), **pycompat.strkwargs(opts))

    ui.status(_(b'comparing with p4://%s using client %s\n') % (client.server, client.client or ''))
    p4rev, changes = _pullcommon(repo, client, opts)
    if not changes:
        ui.status(_(b"no changes found\n"))
        return 1

    limit = opts[b'limit']
    limit = limit and int(limit) or 0

    for c in changes:
        cl = client.describe(c, local=ui.verbose)
        tags = client.labels(c)

        ui.write(_(b'changelist:  %d\n') % c)
        # ui.write(_(b'branch:      %s\n') % branch)
        for tag in tags:
            ui.write(_(b'tag:         %s\n') % tag)
        # ui.write(_(b'parent:      %d:%s\n') % parent)
        ui.write(_(b'user:        %s\n') % cl.user)
        ui.write(_(b'date:        %s\n') % datestr(cl.date))
        if cl.jobs:
            ui.write(_(b'jobs:        %s\n') % b' '.join(cl.jobs))
        if ui.verbose:
            ui.write(_(b'files:       %s\n') % b' '.join(f[4] for f in cl.files))

        if cl.desc:
            if ui.verbose:
                ui.write(_(b'description:\n'))
                ui.write(cl.desc)
                ui.write(b'\n')
            else:
                ui.write(_(b'summary:     %s\n') % cl.desc.splitlines()[0])

        ui.write(b'\n')
        limit-=1
        if limit==0:
            break

    return 0  # exit code is zero since we found incoming changes


def pull(original, ui, repo, source=None, **opts):
    '''Wrap the pull command to look for p4 paths, import changelists'''

    opts = pycompat.byteskwargs(opts)
    client = _pullclient(ui, repo, source, opts)
    if client is None:
        return original(ui, repo, *(source and [source] or []), **pycompat.strkwargs(opts))

    ui.status(_(b'pulling from p4://%s using client %s\n') % (client.server, client.client or ''))
    ui.flush()

    # for clone we support an --encoding option to set server character set
    if opts.get(b'encoding'):
        client.encoding = opts.get(b'encoding')

    # for clone we support a --startrev option to fold initial changelists
    startrev = opts.get(b'startrev')
    startrev = startrev and int(startrev) or 0

    ui.status(_(b"searching for changes\n"))
    p4rev, changes = _pullcommon(repo, client, opts, startrev)
    if not changes:
        ui.status(_(b"no changes found\n"))
        return 0

    # for clone we support a --startrev option to fold initial changelists
    if startrev:
        if len(changes) < 2:
            raise error.Abort(_(b'with --startrev there must be at least two revisions to clone'))
        if startrev < 0:
            startrev = changes[0]
        else:
            if changes[0] != startrev:
                raise error.Abort(_(b'changelist for --startrev not found, first changelist is %s' % changes[0]))

    if client.lowercasepaths:
        ui.note(_(b"converting pathnames to lowercase.\n"))
    if client.ignorecase:
        ui.note(_(b"ignoring case in file names.\n"))

    ctx = None
    first_ctx = None
    tags = {}
    trim = ui.configbool(b'perfarce', b'pull_trim_log', False)

    progress = _makeprogress(ui, topic=_(b'pulling changes'), unit=_(b'changes'), total=len(changes))
    try:
        for c in changes:
            ui.note(_(b'change %s\n') % int_to_bytes(c))
            cl = client.describe(c)
            files = client.fstat(c, all=bool(startrev))

            if client.keep:
                if startrev:
                    client.sync(c, all=True, force=True)
                else:
                    client.runs(b'revert -k', files=[f[0] for f in files], abort=False)
                    client.sync(c, force=True, files=[f[0] for f in files if f[3]==b"R"]+
                                                     [f[0] for f in files if f[3]!=b"R"])

            nodes, match = client.parsenodes(cl.desc)
            if nodes:
                parent = nodes[-1]
                hgfiles = [f for f in repo[parent].files() if f.startswith(b'.hg')]
                if trim:
                    # remove mercurial id from description in p4
                    cl.desc = cl.desc[:match.start(0)] + cl.desc[match.end(0):]
                    if cl.desc.endswith(b"\n\n\n"):
                        cl.desc = cl.desc[:-2]
                    client.change(c, cl.desc, update=True)
            else:
                parent = None
                hgfiles = []

            if startrev:
                # no 'p4' data on first revision as it does not correspond
                # to a p4 changelist but to all of history up to a point
                extra = {}
                startrev = None
            else:
                extra = {b'p4': int_to_bytes(c)}

            entries = _entries(repo, files, client, p1=p4rev, p2=parent)
            getfilectx = _get_getfilectx(entries, client, p2=parent)
            ctx = _common_commit(cl, repo, getfilectx, extra,
                                 files=list(entries.keys()) + hgfiles,
                                 p1=p4rev, p2=parent)
            p4rev = ctx.hex()
            if first_ctx is None:
                first_ctx = ctx

            for l in client.labels(c):
                tags[l] = (c, ctx.hex())

            repo.pushkey(b'phases', ctx.hex(), str(phases.draft), str(phases.public))

            ui.note(_(b'added changeset %d:%s\n') % (ctx.rev(), ctx))
            progress.increment(item=str(c).encode('ascii'))

    finally:
        if tags:
            tag_ctx = _commit_tags(repo, client, tags)
            p4rev = tag_ctx.hex()
            ui.note(_(b'added changeset %d:%s\n') % (tag_ctx.rev(), tag_ctx))

    progress.complete()

    # TODO use a transaction and registersummarycallback
    if ctx is None:
        # An error happened, no commit was created
        return 1

    if first_ctx == ctx:
        revrange = first_ctx
    else:
        revrange = b'%s:%s' % (first_ctx, ctx)
    ui.status(_(b'new changesets %s\n') % revrange)

    return commands.postincoming(ui, repo, 1, opts.get(b'update'), p4rev, None)


def _commit_tags(repo, client, tags):
    p4rev, p4id = client.find()
    ctx = repo[p4rev]

    if b'.hgtags' in ctx:
        tagdata = [ctx.filectx(b'.hgtags').data()]
    else:
        tagdata = []

    desc = [b'p4 tags']
    for l in sorted(tags):
        t = tags[l]
        desc.append(b'   %s @ %d' % (l, t[0]))
        tagdata.append(b'%s %s\n' % (t[1], l))

    def getfilectx(repo, memctx, fn):
        'callback to read file data'
        assert fn==b'.hgtags'
        return context.memfilectx(
            changectx=None,
            repo=repo,
            path=fn,
            data=b''.join(tagdata),
            islink=False,
            isexec=False,
        )
    ctx = context.memctx(repo, (p4rev, None), b'\n'.join(desc),
                            [b'.hgtags'], getfilectx)
    p4rev = repo.commitctx(ctx)
    return repo[p4rev]


def _makeprogress(ui, topic, unit="", total=None):
    try:
        return ui.makeprogress(topic, unit=unit, total=total)
    except AttributeError:
        # Mercurial 4.6.2 and older
        class Progress:
            def __init__(self, ui, topic, unit, total):
                self.ui = ui
                self.topic = topic
                self.unit = unit
                self.total = total
                self.pos = 0
                self._progress()

            def _progress(self, item=""):
                self.ui.progress(self.topic, self.pos, item=item, unit=self.unit, total=self.total)

            def increment(self, item=""):
                self.pos += 1
                self._progress(item)

            def complete(self):
                self.pos = None
                self._progress()
        return Progress(ui, _(b'pulling changes'), unit=_(b'changes'), total=total)


def _get_getfilectx(entries, client, p2=None):
    def getfilectx(repo, memctx, fn):
        'callback to read file data'
        if fn.startswith(b'.hg'):
            return repo[p2].filectx(fn)

        if entries[fn][3] == b'R' and getattr(memctx, '_returnnoneformissingfiles', False):
            # from 3.1 onvards, ctx expects None for deleted files
            client.ui.debug(b'removed file %r\n'%(entries[fn],))
            return None

        mode, contents = client.getfile(entries[fn])
        if contents is None:
            return None
        return context.memfilectx(
            changectx=None,
            repo=repo,
            path=fn,
            data=contents,
            islink=b'l' in mode,
            isexec=b'x' in mode,
        )
    return getfilectx


def _entries(repo, files, client, p1, p2=None):
    entries = {}
    if client.ignorecase:
        manifiles = {}
        for n in (p1, p2):
            if n:
                for f in repo[n]:
                    manifiles[client.normcase(f)] = f
        seen = set()
        for f in files:
            g = client.normcase(f[4])
            if g not in seen:
                entries[manifiles.get(g, f[4])] = f
                seen.add(g)
    else:
        entries.update((f[4], f) for f in files)
    return entries


def _common_commit(cl, repo, getfilectx, extra, files, p1, p2=None):
    if cl.jobs:
        extra[b'p4jobs'] = b" ".join(cl.jobs)

    ctx = context.memctx(repo, (p1, p2), cl.desc,
                         files,
                         getfilectx, cl.user, cl.date, extra)

    p4rev = repo.commitctx(ctx)
    ctx = repo[p4rev]
    return ctx


def clone(original, ui, source, dest=None, **opts):
    '''Wrap the clone command to look for p4 source paths, do pull'''

    opts = pycompat.byteskwargs(opts)
    try:
        client = p4client(ui, None, source)
    except p4notclient:
        if ui.traceback:ui.traceback()
        return original(ui, source, dest, **pycompat.strkwargs(opts))
    except p4badclient as e:
        raise error.Abort(str(e))

    d = client.runone(b'info')
    if not isinstance(d,dict) or d[b'clientName']=='*unknown*' or b"clientRoot" not in d:
        raise error.Abort(_(b'%s is not a valid p4 client') % source)

    if dest is None:
        dest = hg.defaultdest(source)
        ui.status(_(b"destination directory: %s\n") % dest)
    else:
        dest = _clone_path(ui, dest)

    dest = urllocalpath(dest)

    if not hg.islocal(dest):
        raise error.Abort(_(b"destination '%s' must be local") % dest)

    if os.path.exists(dest):
        if not os.path.isdir(dest):
            raise error.Abort(_(b"destination '%s' already exists") % dest)
        elif os.listdir(dest):
            raise error.Abort(_(b"destination '%s' is not empty") % dest)

    if client.root == util.pconvert(os.path.abspath(dest)):
        raise error.Abort(_(b"destination '%s' is same as p4 workspace") % dest)

    repo = hg.repository(ui, dest, create=True)

    opts[b'update'] = not opts[b'noupdate']
    opts[b'force'] = None

    try:
        r = pull(None, ui, repo, source=source, **pycompat.strkwargs(opts))
    finally:
        fp = repo.vfs(b"hgrc", b"w")
        fp.write(b"[paths]\n")
        fp.write(b"default = %s\n" % source)
        fp.write(b"\n[perfarce]\n")
        fp.write(b"ignorecase = %s\n" % encode_bool(client.ignorecase))
        fp.write(b"keep = %s\n" % encode_bool(client.keep))
        fp.write(b"lowercasepaths = %s\n" % encode_bool(client.lowercasepaths))
        fp.write(b"tags = %s\n" % encode_bool(client.tags))

        if client.encoding:
            fp.write(b"encoding = %s\n" % client.encoding.encode('ascii'))
        cu = ui.config(b"perfarce", b"clientuser")
        if cu:
            fp.write(b"clientuser = %s\n" % cu)

        move, copy = client.hasmovecopy()
        fp.write(b"move = %s\n" % encode_bool(move))
        fp.write(b"copy = %s\n" % encode_bool(copy))

        fp.close()

    return r


# --------------------------------------------------------------------------

@command(b"p4unshelve",
         [  ],
         b'hg p4unshelve changelist')
def unshelve(ui, repo, changelist, **opts):
    'copy the contents of a shelve onto a local draft commit'

    opts = pycompat.byteskwargs(opts)
    source = _pull_path(b'p4unshelve', repo, ui, b'default')
    try:
        client = p4client(ui, repo, source)
    except p4notclient as e:
        if ui.traceback:ui.traceback()
        raise error.Abort(str(e))
    except p4badclient as e:
        raise error.Abort(str(e))

    depot=[]
    p4cmd = b'unshelve -f -s %s' % changelist
    for d in client.run(p4cmd):
        if d[b"code"] == b"stat":
            df = d[b"depotFile"]
            depot.append(df)

    if ui.debugflag:
        ui.debug(b'depot = %r\n' % (depot,))

    if not depot:
        ui.status(_(b'no files unshelved'))
        return 2

    c = int(changelist)
    ui.note(_(b'change %s\n') % int_to_bytes(c))
    cl = client.describe(changelist, shelve=True)
    p4rev = _get_shelve_base_rev(ui, cl, client)
    try:
        entries = _entries(repo, client.fstat(files=depot), client, p1=p4rev)
        getfilectx = _get_getfilectx(entries, client)
        ctx = _common_commit(cl, repo, getfilectx, {b'p4': int_to_bytes(c)}, files=list(entries.keys()), p1=p4rev)

        ui.note(_(b'added changeset %d:%s\n') % (ctx.rev(), ctx))
    finally:
        client.runs(b"revert", files=depot)

    ui.status(_(b'%d files unshelved onto changeset %d:%s\n') % (len(depot), ctx.rev(), ctx))
    return


def _get_shelve_base_rev(ui, cl, client):
    """
    To determine the change to be used as the base of the changeset,
    find the change in which each file's base revision was modified and
    the change in which its next revision was modified.
    This will give us a range of changes.
    Then, identify the corresponding revision in the repository, and create
    a draft commit representing the shelve on top of that commit.
    """
    if ui.debugflag:
        ui.debug(b'cl = %r\n' % (cl,))

    if not cl.files:
        ui.status(_(b'no files unshelved'))
        return 2

    def _change_for_file_rev(df, file_rev):
        p4cmd = b'changes -m 1 %s#%d' % (df, file_rev)
        d = client.runone(p4cmd)
        c = int(d[b'change'])
        return c
    changes_low = []
    changes_high = []
    for f in cl.files:
        df = f[0]
        file_rev = f[1]
        if file_rev == 1:
            # TODO need to check if the file exists in the central repo
            # and add the changelist in which it was created to changes_high
            pass
        else:
            changes_low.append(_change_for_file_rev(df, file_rev))
            changes_high.append(_change_for_file_rev(df, file_rev + 1))

    if ui.debugflag:
        ui.debug(b'changes_low = %r\n' % (changes_low,))
        ui.debug(b'changes_high = %r\n' % (changes_high,))
    change_low = max(changes_low)
    change_high = min(changes_high)
    if change_high < change_low:
        raise error.Abort(_(b'unable to determine base changelist'))

    p4rev, changelist_base = client.find(rev=b'.', p4rev=change_low)
    if ui.debugflag:
        ui.debug(b'base change = %r\n' % (changelist_base,))

    if changelist_base != change_low:
        raise error.Abort(_(b'error when determining base changelist'))

    return p4rev


# --------------------------------------------------------------------------

def outgoing(original, ui, repo, dest=None, **opts):
    '''Wrap the outgoing command to look for p4 paths, report changes
    Returns 0 if there are outgoing changes, 1 otherwise.
    '''

    opts = pycompat.byteskwargs(opts)
    client = _pushclient(ui, repo, dest, opts)
    if client is None:
        return original(ui, repo, *(dest and [dest] or []), **pycompat.strkwargs(opts))

    ui.status(_(b'comparing with p4://%s using client %s\n') % (client.server, client.client or ''))
    r = _pushcommon(ui, repo, client, opts)
    if r is None:
        ui.status(_(b"no changes found\n"))
        return 1
    p4rev, p4id, nodes, ctx, desc, mod, add, rem, cpy = r

    if ui.quiet:
        # for thg integration until we support templates
        for n in nodes:
            ui.write('%s\n' % repo[n].hex())
    else:
        ui.write(desc)
        ui.write(b'\naffected files:\n')
        cwd = repo.getcwd()
        for char, files in zip(b'MAR', (mod, add, rem)):
            for f in files:
                ui.write(b'%s %s\n' % (int_to_bytes(char), repo.pathto(f[0], cwd)))
        ui.write(b'\n')


def push(original, ui, repo, dest=None, **opts):
    '''Wrap the push command to look for p4 paths, create p4 changelist'''

    opts = pycompat.byteskwargs(opts)
    client = _pushclient(ui, repo, dest, opts)
    if client is None:
        return original(ui, repo, *(dest and [dest] or []), **pycompat.strkwargs(opts))

    is_submit = opts[b'submit'] or ui.configbool(b'perfarce', b'submit', default=False)
    if is_submit:
        ui.status(_(b'pushing to p4://%s using client %s\n') % (client.server, client.client or ''))
    else:
        ui.status(_(b'pushing to client %s\n') % client.client or '')

    r = _pushcommon(ui, repo, client, opts)
    
    if r is None:
        return 0
    p4rev, p4id, nodes, ctx, desc, mod, add, rem, cpy = r

    move, copy = client.hasmovecopy()

    # sync to the last revision pulled, converted or submitted
    for e in client.getpendinglist():
        if e[1]:
            p4id=e[0]
            

    if client.keep:
        client.sync(p4id)
    else:
        client.sync(p4id, fake=True)
        client.sync(p4id, force=True, files=[client.encodename(f[0]) for f in mod])

    # attempt to reuse an existing changelist
    def noid(d):
        return client.re_hgid.sub(b"{{}}", d)

    use = b''
    noiddesc = noid(desc)
    for d in client.run(b'changes -s pending -c %s -l' % client.client):
        if noid(d[b'desc']) == noiddesc:
            use = d[b'change']

    def rev(files, change=b"", abort=True):
        if files:
            ui.note(_(b'reverting: %s\n') % b' '.join(f[0] for f in files))
            if change:
                change = b'-c %s' % int_to_bytes( change)
            client.runs(b'revert %s' % change,
                        files=[os.path.join(client.partial, f[0]) for f in files],
                        abort=abort)

    # revert any other changes in existing changelist
    if use:
        cl = client.describe(use)
        rev(cl.files, use)

    # revert any other changes to the files
    rev(mod + add + rem, abort=False)

    # sort out the copies from the adds
    rems = {}
    for f in rem:
        rems[f[0]] = True

    moves = []      # src,dest,mode tuples for p4 move
    copies = []     # src,dest tuples for p4 copy
    ntg = []        # integrate
    add2 = []       # additions left after copies removed
    mod2 = []       # list of dest,mode for files modified as well as copied/moved
    for f,g in add:
        if f in cpy:
            r, chg = cpy[f]
            if move and r in rems and rems[r]:
                moves.append((r, f, g))
                rems[r] = False
            elif copy:
                copies.append((r, f))
            else:
                ntg.append((r, f))
            if chg:
                mod2.append((f,g))
        else:
            add2.append((f,g))
    add = add2

    rem = [r for r in rem if rems[r[0]]]

    if ui.debugflag:
        ui.debug(b'mod = %r+%r\n' % (mod,mod2))
        ui.debug(b'add = %r\n' % (add,))
        ui.debug(b'remove = %r\n' % (rem,))
        ui.debug(b'copies = %r\n' % (copies,))
        ui.debug(b'moves = %r\n' % (moves,))
        ui.debug(b'integrate = %r\n' % (ntg,))

    # create new changelist
    use = client.change(use, desc, jobs=opts[b'job'])

    def modal(note, cmd, files, encoder):
        'Run command grouped by file mode'
        ui.note(note % b' '.join(f[0] for f in files))
        retype = []
        modes = set(f[1] for f in files)
        for mode in modes:
            opt = b""
            if b'l' in mode:
                opt = b"symlink"
            if b'x' in mode:
                opt += b"+x"
            opt = opt and b" -t " + opt
            bunch = [os.path.join(client.partial, encoder(f[0])) for f in files if f[1]==mode]
            if bunch:
                for d in client.run(cmd + opt, files=bunch):
                    if d[b'code'] == b'stat':
                        basetype, oldmode, keywords, utf16 = client.decodetype(d[b'type'])
                        if mode==b'' and  oldmode==b'x':
                            retype.append((d[b'depotFile'], basetype))

                    if d[b'code'] == b'info':
                        data = d[b'data']
                        if b"- use 'reopen'" in data:
                            raise error.Abort('p4: %s' % data)
        modes = set(f[1] for f in retype)
        for mode in modes:
            bunch = [f[0] for f in retype if f[1]==mode]
            if bunch:
                client.runs(b"reopen -t %s"%mode, files=bunch)

    try:
        # now add/edit/delete the files

        if copies:
            ui.note(_(b'copying: %s\n') % b' '.join(f[1] for f in copies))
            for f in copies:
                client.runs(b'copy -c %s "%s" "%s"' % (use, client.rootpart + f[0], client.rootpart + f[1]))

        if moves:
            modal(_(b'opening for move: %s\n'), b'edit -c %s' % use,
                  files=[(client.rootpart + f[0], f[2]) for f in moves], encoder=client.encodename)

            ui.note(_(b'moving: %s\n') % b' '.join(f[1] for f in moves))
            for f in moves:
                client.runs(b'move -c %s "%s" "%s"' % (
                    use, client.rootpart + client.encodename(f[0]),
                    client.rootpart + client.encodename(f[1])))

        if ntg:
            ui.note(_(b'opening for integrate: %s\n') % b' '.join(f[1] for f in ntg))
            for f in ntg:
                f1 = client.rootpart + f[1]
                ui.debug(_(b'unlink: %s\n') % f1)
                try:
                    os.unlink(f1)
                except Exception:
                    pass
                client.runs(b'integrate -c %s -Di -t "%s" "%s"' % (use, client.rootpart + f[0], f1))

        if mod or mod2:
            modal(_(b'opening for edit: %s\n'), b'edit -c %s' % use, files=mod + mod2, encoder=client.encodename)

        if mod or add or mod2:
            ui.note(_(b'retrieving file contents...\n'))
            opener = scmutil.vfs.vfs(client.rootpart)

            for name, mode in mod + add + mod2:
                ui.debug(_(b'writing: %s\n') % name)
                if b'l' in mode:
                    opener.symlink(ctx[name].data(), name)
                else:
                    fp = opener(name, mode=b"w")
                    fp.write(ctx[name].data())
                    fp.close()
                util.setflags(client.localpath(name), b'l' in mode, b'x' in mode)

        if add:
            modal(_(b'opening for add: %s\n'), b'add -f -c %s' % use, files=add, encoder=lambda n:n)

        if rem:
            modal(_(b'opening for delete: %s\n'), b'delete -c %s' % use, files=rem, encoder=client.encodename)

        # submit the changelist to p4 if --submit was given
        if is_submit:
            if ntg:
                client.runs(b'resolve -f -c %s -ay ...' % use, abort=False)
            client.submit(use)
        else:
            ui.note(_(b'pending changelist %s\n') % use)

    except Exception:
        if ui.debugflag:
            ui.note(_(b'not reverting changelist %s\n') % use)
        else:
            revert(ui, repo, use, **pycompat.strkwargs(opts))
        raise


# --------------------------------------------------------------------------

def subrevcommon(mode, ui, repo, changes, opts):
    'Collect list of changelist numbers from commandline'

    if repo.path.startswith(b'p4://'):
        dest = repo.path
    else:
        dest = _push_path(repo, ui)
    client = p4client(ui, repo, dest)

    if changes:
        try:
            changes = [int(c) for c in changes]
        except ValueError:
            if ui.traceback:ui.traceback()
            raise error.Abort(_(b'changelist must be a number'))
    elif opts[b'all']:
        changes = [e[0] for e in client.getpendinglist() if not e[1]]
        if not changes:
            raise error.Abort(_(b'no pending changelists to %s') % mode)
    else:
        raise error.Abort(_(b'no changelists specified'))

    return client, changes


@command(b"p4submit",
         [ (b'a', b'all', None, _(b'submit all changelists listed by p4pending')) ],
         b'hg p4submit [-a] changelist...')
def submit(ui, repo, *changes, **opts):
    'submit one or more changelists to the p4 depot.'

    opts = pycompat.byteskwargs(opts)
    client, changes = subrevcommon('submit', ui, repo, changes, opts)

    for c in changes:
        ui.status(_(b'submitting: %d\n') % c)
        cl = client.describe(c)
        client.submit(c)


@command(b"p4revert",
         [ (b'a', b'all', None, _(b'revert all changelists listed by p4pending')) ],
         b'hg p4revert [-a] changelist...')
def revert(ui, repo, *changes, **opts):
    'revert one or more pending changelists and all opened files.'

    opts = pycompat.byteskwargs(opts)
    client, changes = subrevcommon('revert', ui, repo, changes, opts)

    for c in changes:
        ui.status(_(b'reverting: %d\n') % c)
        try:
            cl = client.describe(c)
        except Exception as e:
            if ui.traceback:ui.traceback()
            ui.warn('%s\n' % e)
            cl = None

        if cl is not None:
            files = [f[0] for f in cl.files]
            if files:
                ui.note(_(b'reverting: %s\n') % b' '.join(files))
                client.runs(b'revert', client=cl.client, files=files, abort=False)

            if cl.jobs:
                ui.note(_(b'unfixing: %s\n') % b' '.join(cl.jobs))
                client.runs(b'fix -d -c %d' % c, client=cl.client, files=cl.jobs, abort=False)

            ui.note(_(b'deleting: %d\n') % c)
            client.runs(b'change -d %d' %c , client=cl.client, abort=False)


@command(b"p4pending",
         [ (b's', b'summary', None, _(b'print p4 changelist summary')) ],
            b'hg p4pending [-s] [p4://server/client]')
def pending(ui, repo, dest=None, **opts):
    'report changelists already pushed and pending for submit in p4'

    opts = pycompat.byteskwargs(opts)
    dest = _push_path(repo, ui, dest)
    client = p4client(ui, repo, dest)

    dolong = opts.get(b'summary')
    hexfunc = ui.verbose and hex or short
    pl = client.getpendinglist()
    if pl:
        w = max(len(str(e[0])) for e in pl)
        for e in pl:
            if dolong:
                if ui.verbose:
                    cl = client.describe(e[0], local=True)
                ui.write(_(b'changelist:  %d\n') % e[0])
                if ui.verbose:
                    ui.write(_(b'client:      %s\n') % e[4])
                ui.write(_(b'status:      %s\n') % ([b'pending',b'submitted'][e[1]]))
                for n in e[2]:
                    ui.write(_(b'revision:    %s\n') % hexfunc(n))
                if ui.verbose:
                    ui.write(_(b'files:       %s\n') % b' '.join(f[4] for f in cl.files))
                    ui.write(_(b'description:\n'))
                    ui.write(e[3])
                    ui.write(b'\n')
                else:
                    ui.write(_(b'summary:     %s\n') % e[3].splitlines()[0])
                ui.write(b'\n')
            else:
                output = []
                output.append(b'%*d' % (-w, e[0]))
                output.append([b'p',b's'][e[1]])
                output+=[hexfunc(n) for n in e[2]]
                ui.write(b"%s\n" % b' '.join(output))


@command(b"p4identify",
         [ (b'b', b'base', None, _(b'show base revision for new incoming changes')),
           (b'c', b'changelist', 0, _(b'identify the specified p4 changelist')),
           (b'i', b'id',   None, _(b'show global revision id')),
           (b'n', b'num',  None, _(b'show local revision number')),
           (b'p', b'p4',   None, _(b'show p4 revision number')),
           (b'r', b'rev',  b'',   _(b'identify the specified revision')),
         ],
         b'hg p4identify [-binp] [-r REV | -c CHANGELIST]')
def identify(ui, repo, *args, **opts):
    '''show p4 and hg revisions for the most recent p4 changelist

    With no revision, show a summary of the most recent revision
    in the repository that was converted from p4.
    Otherwise, find the p4 changelist for the revision given.
    '''

    opts = pycompat.byteskwargs(opts)
    rev = opts.get(b'rev')
    if rev:
        ctx = repo[rev]
        extra = ctx.extra()
        if b'p4' not in extra:
            raise error.Abort(_(b'no p4 changelist revision found'))
        changelist = int(extra[b'p4'])
    else:
        client = p4client(ui, repo, b'p4:///')
        cl = opts.get(b'changelist')
        if cl:
            rev = None
        else:
            rev = b'.'
        p4rev, changelist = client.find(rev=rev, base=opts.get(b'base'), p4rev=cl)
        ctx = repo[p4rev]

    num = opts.get(b'num')
    doid = opts.get(b'id')
    dop4 = opts.get(b'p4')
    default = not (num or doid or dop4)
    hexfunc = ui.verbose and hex or short
    output = []

    if default or dop4:
        output.append(int_to_bytes(changelist))
    if num:
        output.append(int_to_bytes(ctx.rev()))
    if default or doid:
        output.append(hexfunc(ctx.node()))

    ui.write(b"%s\n" % b' '.join(output))


templatekeyword = registrar.templatekeyword()

if tuple(util.version().split(b".",2)) < (b"4",b"6"):
    # Mercurial 4.5.3 and older
    @templatekeyword(b'p4')
    def showp4cl(repo, ctx, templ, **args):
        """String. p4 changelist number."""
        return ctx.extra().get(b"p4")

    @templatekeyword(b'p4jobs')
    def showp4jobs(repo, ctx, templ, **args):
        """String. A list of p4 jobs."""
        return ctx.extra().get(b"p4jobs")

else:
    @templatekeyword(b'p4', requires={b'ctx'})
    def showp4cl(context, mapping):
        """String. p4 changelist number."""
        ctx = context.resource(mapping, b'ctx')
        return ctx.extra().get(b"p4")

    @templatekeyword(b'p4jobs', requires={b'ctx'})
    def showp4jobs(context, mapping):
        """String. A list of p4 jobs."""
        ctx = context.resource(mapping, b'ctx')
        return ctx.extra().get(b"p4jobs")

