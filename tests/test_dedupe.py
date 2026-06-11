'''Fixture-based checks for the global identity dedupe.

These build synthetic install layouts in a temp dir (emulating /opt/<ide>,
symlink/wrapper launchers and Toolbox installLocations) and assert the result
of ProgramList._dedupe_pairs, the algorithm that backs get_menu_items.  They
avoid touching PATH or the real Toolbox state by constructing packages and
setting their resolved attributes directly.

Run with:  python -m tests.test_dedupe   (or  python tests/test_dedupe.py)
'''
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from NautilusCode.types import (  # noqa: E402
    Native, Toolbox, Flatpak, Program, ProgramList,
)


def make_native (cmd_path, alias=None):
    pkg = Native(alias=alias)          # no PATH lookup; we set the path below
    pkg.cmd_path = cmd_path
    pkg._identity = None
    return pkg


def make_toolbox (launch_command, install_location=''):
    pkg = Toolbox()                    # tool_ids empty -> nothing matched
    pkg.launch_command = launch_command
    pkg.install_location = install_location
    pkg._identity = None
    return pkg


def make_flatpak (app_id, alias=None):
    pkg = Flatpak(app_id, alias=alias)
    return pkg


def dedupe (*pairs):
    '''Return the surviving (program_id, type_name) tuples in order.'''
    survivors = ProgramList._dedupe_pairs(list(pairs))
    return [(prog.id, pkg.type_name_raw) for prog, pkg in survivors]


def write_exec (path, content='#!/bin/sh\n'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    os.chmod(path, 0o755)
    return path


def test_cross_program_toolbox_eap_collapses (root):
    '''The reported bug: Toolbox adopted /opt/pycharm-eap (stable program) and
       Native('pycharm-eap') resolving to bin/pycharm.sh of the same install
       must collapse to ONE entry, Native preferred.'''
    install = os.path.join(root, 'opt', 'pycharm-eap')
    real_bin = write_exec(os.path.join(install, 'bin', 'pycharm.sh'))
    tb_launch = write_exec(os.path.join(install, 'bin', 'pycharm'))
    # /usr/bin/pycharm-eap symlink -> bin/pycharm.sh
    usr_link = os.path.join(root, 'usr', 'bin', 'pycharm-eap')
    os.makedirs(os.path.dirname(usr_link), exist_ok=True)
    os.symlink(real_bin, usr_link)

    p_stable = Program('pycharm', 'PyCharm')
    p_eap = Program('pycharm-eap', 'PyCharm (EAP)')
    tb = make_toolbox(tb_launch, install_location=install)
    nat = make_native(usr_link)

    result = dedupe((p_stable, tb), (p_eap, nat))
    assert result == [('pycharm-eap', 'Native')], result


def test_distinct_installs_preserved (root):
    '''Community at /opt/pycharm-community + EAP at /opt/pycharm-eap stay as
       two entries (different install dirs).'''
    eap = os.path.join(root, 'opt', 'pycharm-eap')
    comm = os.path.join(root, 'opt', 'pycharm-community')
    eap_bin = write_exec(os.path.join(eap, 'bin', 'pycharm.sh'))
    comm_bin = write_exec(os.path.join(comm, 'bin', 'pycharm.sh'))

    p_eap = Program('pycharm-eap', 'PyCharm (EAP)')
    p_comm = Program('pycharm', 'PyCharm')
    result = dedupe((p_eap, make_native(eap_bin)),
                    (p_comm, make_native(comm_bin)))
    assert sorted(result) == [('pycharm', 'Native'),
                              ('pycharm-eap', 'Native')], result


def test_pure_toolbox_with_path_launcher (root):
    '''A Toolbox install whose PATH launcher is a wrapper script: Native
       resolves through the wrapper into the install, so a single Native entry
       survives.'''
    install = os.path.join(root, 'opt', 'webstorm')
    tb_launch = write_exec(os.path.join(install, 'bin', 'webstorm'))
    wrapper = write_exec(os.path.join(root, 'scripts', 'webstorm'),
                         '#!/bin/bash\n"%s" "$@"\n' % tb_launch)

    prog = Program('webstorm', 'WebStorm')
    tb = make_toolbox(tb_launch, install_location=install)
    nat = make_native(wrapper)
    # Same program here, but global dedupe still applies.
    result = dedupe((prog, nat), (prog, tb))
    assert result == [('webstorm', 'Native')], result


def test_flatpak_native_alias (root):
    '''Flatpak + native of the same IDE: with an alias declared they merge
       (native wins); without an alias they remain two entries.'''
    install = os.path.join(root, 'opt', 'rider')
    nat_bin = write_exec(os.path.join(install, 'bin', 'rider.sh'))

    prog = Program('rider', 'Rider')

    # With alias on both -> single entry, Native preferred.
    nat = make_native(nat_bin, alias='rider')
    fp = make_flatpak('com.jetbrains.Rider', alias='rider')
    fp.flatpak_path = '/usr/bin/flatpak'        # pretend flatpak is present
    result = dedupe((prog, nat), (prog, fp))
    assert result == [('rider', 'Native')], result

    # Without alias -> both kept.
    nat2 = make_native(nat_bin)
    fp2 = make_flatpak('com.jetbrains.Rider')
    fp2.flatpak_path = '/usr/bin/flatpak'
    result2 = dedupe((prog, nat2), (prog, fp2))
    assert sorted(result2) == [('rider', 'Flatpak'),
                               ('rider', 'Native')], result2


def main ():
    tests = [
        test_cross_program_toolbox_eap_collapses,
        test_distinct_installs_preserved,
        test_pure_toolbox_with_path_launcher,
        test_flatpak_native_alias,
    ]
    failed = 0
    for test in tests:
        with tempfile.TemporaryDirectory() as root:
            try:
                test(root)
                print(f'PASS {test.__name__}')
            except AssertionError as exc:
                failed += 1
                print(f'FAIL {test.__name__}: {exc}')
    if failed:
        print(f'\n{failed} test(s) failed')
        sys.exit(1)
    print('\nall fixture tests passed')


if __name__ == '__main__':
    main()
