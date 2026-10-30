import os
import plistlib
import subprocess
import sys
from .core import ROOT

LABEL = 'local.recruiter-finder'

def plist(root=ROOT):
    return {'Label': LABEL, 'ProgramArguments': [str(root / '.venv/bin/python'), '-m', 'recruiter.cli', 'tick'],
            'WorkingDirectory': str(root), 'StartInterval': 300, 'RunAtLoad': True,
            'StandardOutPath': str(root / 'state/launchd.out.log'),
            'StandardErrorPath': str(root / 'state/launchd.err.log'),
            'ProcessType': 'Background', 'ThrottleInterval': 60}

def path():
    from pathlib import Path
    return Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')

def install():
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(plist()))
    os.chmod(target, 0o600)
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(target)], check=True)

def stop():
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], check=True)
