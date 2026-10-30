"""Terminate only exact project sandbox-wrapper processes, never bare MCP servers."""
import os
import signal
import subprocess
from src.broker.sandbox_inspection import NODE, ROOT

def cleanup_stale_sandbox_wrappers():
    listing=subprocess.run(['/bin/ps','-axo','pid=,command='],capture_output=True,text=True,check=True).stdout
    target=str(NODE)+' '+str(ROOT/'scripts/sandbox-mcp-audit.mjs')
    count=0
    for line in listing.splitlines():
        parts=line.strip().split(None,1)
        if len(parts)!=2 or parts[1]!=target:continue
        pid=int(parts[0])
        if pid==os.getpid():continue
        # Recheck identity immediately before termination, with no process environment read.
        current=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
        if current.returncode==0 and current.stdout.strip()==target:
            try:os.kill(pid,signal.SIGTERM);count+=1
            except ProcessLookupError:pass
    return count
