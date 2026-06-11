import json
import os
import re
from gettext import gettext as _

from gi.repository import Nautilus
from gi.repository import GLib


user_data_dir = GLib.get_user_data_dir()


# Roots that are shared by many unrelated programs.  An install token must
# never be derived from these, otherwise two distinct products that happen to
# live under e.g. /usr would be wrongly merged.
_SYSTEM_ROOTS = frozenset({
    '/', '/usr', '/usr/local', '/opt', '/bin', '/sbin',
    '/usr/bin', '/usr/sbin', '/usr/local/bin',
    os.path.normpath(os.path.expanduser('~')),
    os.path.normpath(os.path.join(os.path.expanduser('~'), '.local')),
})

# Matches a quoted token in a shell wrapper, e.g. "/opt/app/bin/app" "$@".
_QUOTED = re.compile(r'"([^"]+)"|\'([^\']+)\'')


def _wrapper_target (path):
    '''If `path` is a small shell wrapper that execs a quoted absolute
       executable (the form Toolbox and many distro launchers use), return that
       executable.  Returns None when `path` is not such a wrapper or cannot be
       read/parsed.'''
    try:
        if os.path.getsize(path) > 65536:
            return None
        with open(path, encoding='utf-8', errors='ignore') as f:
            head = f.read(8192)
    except OSError:
        return None
    if not head.startswith('#!'):
        return None
    for match in _QUOTED.finditer(head):
        candidate = match.group(1) or match.group(2)
        if candidate and os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
    return None


def resolve_launcher (path):
    '''Resolve a launcher path to the real executable it ultimately starts.

       Follows symlinks and, when the target is a small shell wrapper that
       execs another quoted executable, follows that one level too.  Distro
       agnostic: it never looks at hard-coded prefixes.  Degrades to the
       resolved real path (or the input) on any error so callers can treat the
       result as "unique" rather than crash.'''
    try:
        real = os.path.realpath(path)
    except OSError:
        return path
    target = _wrapper_target(real)
    if target:
        try:
            return os.path.realpath(target)
        except OSError:
            return target
    return real


def install_token (real_path):
    '''Return a stable token for the install directory that owns `real_path`,
       or None when it cannot be determined from an authoritative layout.

       Uses the conventional `<install>/bin/<exe>` layout shared by the
       JetBrains IDEs (and most self-contained app bundles).  System roots such
       as /usr or /usr/local are deliberately excluded so two unrelated
       programs installed there are never merged.'''
    try:
        real = os.path.realpath(real_path)
    except OSError:
        return None
    parent = os.path.dirname(real)
    if os.path.basename(parent) != 'bin':
        return None
    root = os.path.dirname(parent)
    if not root or os.path.normpath(root) in _SYSTEM_ROOTS:
        return None
    return ('install', os.path.normpath(root))


class NamedList (dict):
    def __iter__ (self):
        return self.values().__iter__()

    @property
    def names (self):
        return self.keys()


class Priority:
    '''Named priority levels for package deduplication.  Higher value wins on
     an identity collision, so the base default (FALLBACK) is the lowest and
     any package type added without an explicit priority safely loses.  Values
     are spaced so new levels can be slotted in between without renumbering.'''
    FALLBACK = 0
    NORMAL = 10
    PREFERRED = 20


class Package:
    run_command: tuple[str]
    is_installed: bool
    type_name = _('Unknown')
    priority = Priority.FALLBACK

    @property
    def identity (self):
        '''The set of values that identify the actual program being launched.
           Each package describes only itself; two installed packages are
           considered the same install when their identity sets intersect.  On
           a collision the one with the higher priority wins and the other is
           dropped by _dedupe.  Return an empty set to opt out of dedup.'''
        return frozenset()

    @property
    def type_name_raw (self):
        return self.__class__.__name__

    def __str__ (self):
        return f"{self.type_name}:\n  installed = {self.is_installed}"


class Native (Package):
    type_name = _('Native')
    priority = Priority.PREFERRED
    cmd_path = ''

    def __init__ (self, *commands, alias=None):
      self.commands = commands
      self.alias = alias
      self._identity = None
      for cmd in commands:
        if cmd_path := GLib.find_program_in_path(cmd):
          self.cmd_path = cmd_path
          break

    @property
    def run_command (self) -> tuple[str]:
        '''The command that should be executed in order to
           run a program using this type of package'''
        return (self.cmd_path,)

    @property
    def is_installed (self):
        return bool(self.cmd_path)

    @property
    def identity (self):
        '''The real executable this command resolves to, plus the install dir
           that owns it.  `resolve_launcher` follows symlinks and shell wrappers
           so e.g. `/usr/bin/pycharm-eap` -> `.../bin/pycharm.sh` lands on the
           true binary, and the install token lets `.sh` vs non-`.sh` siblings
           of one install collide.  An optional `alias` token enables opt-in
           dedup against another package (e.g. a Flatpak of the same IDE).'''
        if not self.cmd_path:
            return frozenset()
        if self._identity is None:
            real = resolve_launcher(self.cmd_path)
            ids = {real}
            token = install_token(real)
            if token:
                ids.add(token)
            if self.alias:
                ids.add(('alias', self.alias))
            self._identity = frozenset(ids)
        return self._identity

    def __str__ (self):
        lines = super().__str__().splitlines()
        if self.cmd_path:
          lines.insert(-1, f"  command = {os.path.basename(self.cmd_path)}")
        elif self.commands:
          lines.insert(-1, f"  command(s) = " + ', '.join(self.commands))
        return  '\n'.join(lines)


class Toolbox (Package):
    type_name = _('Toolbox')
    toolbox_dir = os.path.join(user_data_dir, 'JetBrains', 'Toolbox')
    state_file = os.path.join(toolbox_dir, 'state.json')
    settings_file = os.path.join(toolbox_dir, '.settings.json')
    _tools = None
    _scripts_dir = None

    @classmethod
    def _load_tools (cls):
        if cls._tools is None:
            try:
                with open(cls.state_file, encoding='utf-8') as f:
                    cls._tools = json.load(f).get('tools', []) or []
            except (OSError, ValueError):
                cls._tools = []
        return cls._tools

    @classmethod
    def scripts_dir (cls):
        '''Directory where Toolbox writes the shell-script launchers it adds
           to PATH. The location is user-configurable in Toolbox settings and
           defaults to the "scripts" folder next to state.json.'''
        if cls._scripts_dir is None:
            cls._scripts_dir = os.path.normpath(cls._read_scripts_location())
        return cls._scripts_dir

    @classmethod
    def _read_scripts_location (cls):
        '''Read the configured shell-scripts location from Toolbox settings,
           falling back to the default "scripts" folder next to state.json
           when the settings file is missing, unreadable, or unset.'''
        default = os.path.join(cls.toolbox_dir, 'scripts')
        try:
            with open(cls.settings_file, encoding='utf-8') as f:
                settings = json.load(f)
        except (OSError, ValueError):
            return default
        return (settings.get('shell_scripts') or {}).get('location') or default

    def __init__ (self, *tool_ids):
      self.tool_ids = tool_ids
      self.launch_command = ''
      self.install_location = ''
      self._identity = None
      for tool in self._load_tools():
        if tool.get('toolId') in tool_ids:
          cmd = tool.get('launchCommand', '')
          if cmd and os.path.exists(cmd):
            self.launch_command = cmd
            self.install_location = tool.get('installLocation', '')
            break

    @property
    def run_command (self) -> tuple[str]:
        '''The command that should be executed in order to
           run a program using this type of package'''
        return (self.launch_command,)

    @property
    def is_installed (self) -> bool:
        return bool(self.launch_command)

    @property
    def identity (self):
        '''The launcher executable plus every launcher Toolbox dropped on PATH
           that points at this install.  By declaring the PATH launchers it
           owns, a Native command that resolves to one of them collides with
           this entry without Native needing to know Toolbox exists.'''
        if not self.launch_command:
            return frozenset()
        if self._identity is None:
            ids = {os.path.realpath(self.launch_command)}
            ids.update(self._owned_scripts())
            # The install dir is authoritative for this tool, so siblings such
            # as bin/pycharm vs bin/pycharm.sh of the same install collide via
            # the shared install token even though they are different files.
            token = None
            if self.install_location:
                token = ('install', os.path.normpath(
                    os.path.realpath(self.install_location)))
            if not token:
                token = install_token(self.launch_command)
            if token:
                ids.add(token)
            self._identity = frozenset(ids)
        return self._identity

    def _owned_scripts (self):
        '''Real paths of launchers in the Toolbox scripts dir that start this
           install.  Toolbox writes either a symlink into the install dir or a
           wrapper shell script that execs the launchCommand, so both forms are
           recognised here.'''
        owned = set()
        install = os.path.realpath(self.install_location) if self.install_location else ''
        try:
            entries = list(os.scandir(self.scripts_dir()))
        except OSError:
            return owned
        for entry in entries:
            real = os.path.realpath(entry.path)
            # Symlink launcher resolving into this install dir.
            if install and (real == install or real.startswith(install + os.sep)):
                owned.add(real)
                continue
            # Wrapper script launcher that execs this install's launchCommand.
            try:
                with open(entry.path, encoding='utf-8', errors='ignore') as f:
                    if self.launch_command in f.read():
                        owned.add(real)
            except OSError:
                pass
        return owned

    def __str__ (self):
        lines = super().__str__().splitlines()
        if self.launch_command:
          lines.insert(-1, f"  command = {self.launch_command}")
        elif self.tool_ids:
          lines.insert(-1, f"  tool_id(s) = " + ', '.join(self.tool_ids))
        return  '\n'.join(lines)


class Flatpak (Package):
    type_name = _('Flatpak')
    priority = Priority.FALLBACK
    flatpak_path = GLib.find_program_in_path('flatpak') or ''
    flatpak_bin_dirs = None

    def __init__ (self, app_id: str, alias=None):
      self.app_id = app_id
      self.alias = alias
      self._calculate_bin_dirs()

    def _calculate_bin_dirs(self):
        if self.flatpak_bin_dirs is not None:
            return

        if self.__class__.flatpak_bin_dirs is not None:
            self.flatpak_bin_dirs = self.__class__.flatpak_bin_dirs
            return

        self.flatpak_bin_dirs = self.__class__.flatpak_bin_dirs = []

        postfix = '/flatpak/exports/bin'
        for bin_dir in [
          user_data_dir + postfix,
          *os.environ.get('PATH', '').split(':'),
          '/var/lib' + postfix
          ]:
          if (postfix in bin_dir
          and bin_dir not in self.flatpak_bin_dirs):
            self.flatpak_bin_dirs.append(bin_dir)

    @property
    def run_command (self) -> tuple[str]:
        '''The command that should be executed in order to
           run a program using this type of package'''
        return (self.flatpak_path, 'run', self.app_id)

    @property
    def is_installed (self) -> bool:
        if not self.flatpak_path:
            return False

        for bin_dir in self.flatpak_bin_dirs:
            if os.path.exists (os.path.join (bin_dir, self.app_id)):
                return True

        return False

    @property
    def identity (self):
        # Flatpak apps are sandboxed; their app-id token can never collide with
        # a filesystem path used by Native or Toolbox packages.  An optional
        # `alias` token opts this Flatpak into dedup against another package
        # (e.g. a native install of the same IDE) when a program declares it.
        ids = {('flatpak', self.app_id)}
        if self.alias:
            ids.add(('alias', self.alias))
        return frozenset(ids)

    def __str__ (self):
        lines = super().__str__().splitlines()
        lines.insert(-1, f"  app_id = {self.app_id}")
        return  '\n'.join(lines)


def _dedupe_by_identity (items, package_of):
    '''Identity-intersection dedup shared by per-program and global dedup.

       `items` is any iterable and `package_of(item)` yields its Package.  Items
       are processed highest-priority first; an item is dropped when its
       package identity set intersects one already kept, so the same physical
       install is represented once by its highest-priority package.  An empty
       identity set never collides.  Original order is preserved.'''
    kept = []
    kept_identities = []
    for item in sorted(items, key=lambda it: -package_of(it).priority):
        ident = package_of(item).identity
        if ident and any(ident & seen for seen in kept_identities):
            continue
        kept.append(item)
        if ident:
            kept_identities.append(ident)
    return [it for it in items if it in kept]


class Program:
    def __init__ (self, id:str, name:str, *packages, arguments:list[str]=None, supports_files=False):
        self.id = id
        self.name = name
        self.arguments = arguments or []
        self.supports_files = supports_files
        self.packages = NamedList()
        for pkg in packages:
            self.add(pkg)

    @property
    def installed_packages (self):
        pkgs = []
        for pkg in self.packages:
            if pkg.is_installed:
                pkgs.append(pkg)
        return self._dedupe(pkgs)

    @staticmethod
    def _dedupe (pkgs):
        '''Per-program convenience dedup.  The menu uses the global
           `ProgramList._dedupe_pairs` instead, which also catches the same
           install surfaced under two different programs; this remains for
           callers that inspect a single program's installed packages.'''
        return _dedupe_by_identity(pkgs, lambda p: p)

    def add (self, pkg):
        self.packages[pkg.type_name_raw] = pkg
        return self

    def __iadd__ (self, pkg):
        return self.add(pkg)

    def __getitem__ (self, type_name_raw):
        return self.packages[type_name_raw]

    def __str__ (self):
        _str  =  'Program:'
        _str += f'\n  Id: {self.id}'
        _str += f'\n  Name: {self.name}'
        if self.arguments:
          _str += f'\n  Arguments: ' + ' '.join(repr(x) for x in self.arguments)
        for pkg in self.packages:
          for line in str(pkg).splitlines():
            _str += '\n  ' + line
        return _str


class ProgramList (NamedList):

    @staticmethod
    def _activate_item (item, command: list[str]):
        pid, *io = GLib.spawn_async(command)
        GLib.spawn_close_pid(pid)

    @staticmethod
    def _dedupe_pairs (pairs):
        '''Global dedup over (program, package) pairs.  Runs the shared
           identity-intersection algorithm across every installed package of
           every program, so a single install surfaced under two different
           programs (e.g. an EAP adopted by Toolbox under the stable program and
           detected natively under the EAP program) collapses to one entry.'''
        return _dedupe_by_identity(pairs, lambda pp: pp[1])

    def get_menu_items (self, path, *, id_prefix='', is_file=False):
        pairs = []
        for program in self:
            if is_file and not program.supports_files:
                continue
            for pkg in program.packages:
                if pkg.is_installed:
                    pairs.append((program, pkg))

        pairs = self._dedupe_pairs(pairs)

        # Count surviving packages per program so the (Type) suffix is only
        # added when a program still has more than one distinct package after
        # global dedup.
        survivors = {}
        for program, pkg in pairs:
            survivors[program.id] = survivors.get(program.id, 0) + 1

        items = []
        for program, pkg in pairs:
            name = id_prefix + program.id
            command = [*pkg.run_command, *program.arguments, path]
            label = _('Open in %s') % program.name
            if survivors[program.id] > 1:
                label += f' ({pkg.type_name})'

            item = Nautilus.MenuItem.new(name, label)
            item.connect('activate', self._activate_item, command)
            items.append(item)

        return items

    def __iadd__ (self, value, /):
        self[value.id] = value
        return self
