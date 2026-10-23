"""Explicit FV-only build tool. Never runs on the default business build path."""
import argparse,hashlib,io,os,platform,tarfile,tempfile,urllib.request
from pathlib import Path
URL='https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz'
ARCHIVE_SHA='cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10'
BINARY_SHA='eb7dd1b518f0a307c99cd97782623c5321da049154b04acd2d98d21aa7bc9b2c'


def install(target, *, source=None):
    target=Path(target)
    if source is not None:data=Path(source).read_bytes()
    else:
        if platform.system()!='Linux' or platform.machine() not in ('x86_64','amd64'):
            raise RuntimeError('Pinned forecast tool supports Linux amd64 only')
        with urllib.request.urlopen(URL,timeout=30) as r:archive=r.read(32*1024**2+1)
        if len(archive)>32*1024**2 or hashlib.sha256(archive).hexdigest()!=ARCHIVE_SHA:
            raise RuntimeError('Official age release archive differs')
        with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tar:
            member=tar.getmember('age/age')
            if not member.isfile() or member.size>20*1024**2:raise RuntimeError('Unexpected age archive member')
            with tar.extractfile(member) as f:data=f.read()
    if hashlib.sha256(data).hexdigest()!=BINARY_SHA:raise RuntimeError('Official age binary differs')
    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if target.exists():
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest()!=BINARY_SHA:
            raise RuntimeError('Existing tool must not be replaced')
        return
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o700)
    with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source');p.add_argument('--target',default='tools/forecast-age/age');a=p.parse_args();install(a.target,source=a.source)
if __name__=='__main__':main()
