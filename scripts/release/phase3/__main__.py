"""Offline manifest preparation/verification. No deployment command exists."""
import argparse
import json
from pathlib import Path
from scripts.release.phase3.manifest import prepare, verify_source, write_once, archive


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    make=sub.add_parser('prepare');make.add_argument('--root',type=Path,required=True)
    make.add_argument('--revision',required=True);make.add_argument('--from-schema',required=True)
    make.add_argument('--baseline-revision',required=True);make.add_argument('--baseline-supports',required=True,nargs='+')
    make.add_argument('--compatibility-review',required=True);make.add_argument('--maintenance',action='store_true')
    make.add_argument('--output',type=Path,required=True)
    check=sub.add_parser('verify');check.add_argument('--root',type=Path,required=True)
    check.add_argument('--manifest',type=Path,required=True);check.add_argument('--artifact',type=Path,required=True)
    a=p.parse_args()
    if a.command=='prepare':
        m=prepare(a.root,a.revision,from_schema=a.from_schema,baseline_revision=a.baseline_revision,
            baseline_supports=a.baseline_supports,compatibility_review=a.compatibility_review,maintenance=a.maintenance)
        a.output.mkdir(parents=True,exist_ok=False)
        with (a.output/'source.tar').open('xb') as stream:stream.write(archive(a.root,a.revision))
        write_once(a.output/'manifest.json',m)
        print(json.dumps({'manifest_id':m['manifest_id'],'revision':m['revision'],'artifact':m['artifact']}))
    else:
        m=json.loads(a.manifest.read_text());verify_source(a.root,m,a.artifact)
        print(json.dumps({'status':'PASS','manifest_id':m['manifest_id']}))


if __name__=='__main__':main()
