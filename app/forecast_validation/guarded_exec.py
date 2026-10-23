"""Linux worker bootstrap: low priority and fail-stop if its supervisor dies."""
import ctypes
import argparse
import os
import resource
import signal
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent',type=int,required=True)
    parser.add_argument('--address-bytes',type=int,required=True)
    parser.add_argument('--cpu-seconds',type=int,required=True)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if sys.platform != 'linux' or not command or min(args.parent,args.address_bytes,args.cpu_seconds)<=0:
        raise SystemExit('Explicit Linux supervisor, limits and command required')
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_PDEATHSIG, checked before exec and after binding to close the race.
    if libc.prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0 or os.getppid() != args.parent:
        raise SystemExit('Worker lost its supervisor')
    os.nice(15)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_AS,(args.address_bytes,args.address_bytes))
    resource.setrlimit(resource.RLIMIT_CPU,(args.cpu_seconds,args.cpu_seconds+1))
    os.execvpe(command[0], command, os.environ)


if __name__ == '__main__':
    main()
