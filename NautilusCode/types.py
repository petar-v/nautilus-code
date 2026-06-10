import json
import os
from gettext import gettext as _

from gi.repository import Nautilus
from gi.repository import GLib


user_data_dir = GLib.get_user_data_dir()


class NamedList (dict):
    def __iter__ (self):
        return self.values().__iter__()

    @property
    def names (self):
        return self.keys()


class Package:
    run_command: tuple[str]
    is_installed: bool
    type_name = _('Unknown')

    @property
    def type_name_raw (self):
        return self.__class__.__name__

    def __str__ (self):
        return f"{self.type_name}:\n  installed = {self.is_installed}"


class Native (Package):
    type_name = _('Native')
    cmd_path = ''

    def __init__ (self, *commands):
      self.commands = commands
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

    def __str__ (self):
        lines = super().__str__().splitlines()
        if self.cmd_path:
          lines.insert(-1, f"  command = {os.path.basename(self.cmd_path)}")
        elif self.commands:
          lines.insert(-1, f"  command(s) = " + ', '.join(self.commands))
        return  '\n'.join(lines)


class Toolbox (Package):
    type_name = _('Toolbox')
    state_file = os.path.join(user_data_dir, 'JetBrains', 'Toolbox', 'state.json')
    _tools = None

    @classmethod
    def _load_tools (cls):
        if cls._tools is None:
            try:
                with open(cls.state_file, encoding='utf-8') as f:
                    cls._tools = json.load(f).get('tools', []) or []
            except (OSError, ValueError):
                cls._tools = []
        return cls._tools

    def __init__ (self, *tool_ids):
      self.tool_ids = tool_ids
      self.launch_command = ''
      for tool in self._load_tools():
        if tool.get('toolId') in tool_ids:
          cmd = tool.get('launchCommand', '')
          if cmd and os.path.exists(cmd):
            self.launch_command = cmd
            break

    @property
    def run_command (self) -> tuple[str]:
        '''The command that should be executed in order to
           run a program using this type of package'''
        return (self.launch_command,)

    @property
    def is_installed (self) -> bool:
        return bool(self.launch_command)

    def __str__ (self):
        lines = super().__str__().splitlines()
        if self.launch_command:
          lines.insert(-1, f"  command = {self.launch_command}")
        elif self.tool_ids:
          lines.insert(-1, f"  tool_id(s) = " + ', '.join(self.tool_ids))
        return  '\n'.join(lines)


class Flatpak (Package):
    type_name = _('Flatpak')
    flatpak_path = GLib.find_program_in_path('flatpak') or ''
    flatpak_bin_dirs = None

    def __init__ (self, app_id: str):
      self.app_id = app_id
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

    def __str__ (self):
        lines = super().__str__().splitlines()
        lines.insert(-1, f"  app_id = {self.app_id}")
        return  '\n'.join(lines)


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
        return pkgs

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

    def get_menu_items (self, path, *, id_prefix='', is_file=False):
        items = []

        for program in self:
            if is_file and not program.supports_files:
                continue

            installed_pkgs = program.installed_packages
            include_type_name = True if len(installed_pkgs) > 1 else False
            for pkg in installed_pkgs:

                name = id_prefix + program.id
                command = [*pkg.run_command, *program.arguments, path]
                label = _('Open in %s') % program.name
                if include_type_name:
                    label += f' ({pkg.type_name})'

                item = Nautilus.MenuItem.new(name, label)
                item.connect('activate', self._activate_item, command)
                items.append(item)

        return items

    def __iadd__ (self, value, /):
        self[value.id] = value
        return self
