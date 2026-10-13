"""Freeze approved local sources and a bounded schema-0033 recovery variant.

Local artifact preparation only. No Git publication, deployment or DB access.
The recovery object contract is installed by the disposable migration rehearsal.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(source, output):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = subprocess.check_output(['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=source).decode().split('\0')
    files = sorted({name for name in files if name and (source/name).is_file()})
    for name in files:
        if name.startswith('.env') and name != '.env.example':
            raise RuntimeError('Environment files must not enter release artifacts.')
    candidate=output/'web-cron';recovery=output/'recovery'
    if candidate.exists() or recovery.exists():
        raise RuntimeError('Use a new artifact output directory.')
    for directory in (candidate,recovery):
        directory.mkdir(mode=0o700)
        for name in files:
            target=directory/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source/name,target)
    # Recovery retains the new assignment/inventory semantics. It cannot execute
    # scheduling, account or grant mutations, even if the cron gate is set true.
    guard=source/'scripts/release/prebeta_recovery_guard.py'
    shutil.copy2(guard,recovery/'app/prebeta_recovery_guard.py')
    main=recovery/'app/main.py'
    main.write_text(main.read_text()+'\nfrom app.prebeta_recovery_guard import install_recovery_guards\ninstall_recovery_guards(app)\n')
    board=recovery/'app/services/v2_scheduling_board_service.py'
    value=board.read_text();needle='        rebuild_schedule_warnings(db, schedule_period_id=period.id)'
    if value.count(needle)!=1:raise RuntimeError('Review changed board cache write before preparing recovery.')
    board.write_text(value.replace(needle,'        # Recovery reads the retained warning cache without changing it.'))
    for name in ('v2/base.html','legacy_base.html'):
        template=recovery/'app/templates'/name
        value=template.read_text();value=value.replace('<body', '<body', 1)
        body_end=value.index('>',value.index('<body'))+1
        value=value[:body_end]+'\n<p role="alert">{{ recovery_message }}</p>\n'+value[body_end:]
        template.write_text(value)
    cron=recovery/'scripts/run_schedule_automation.py'
    cron.write_text('''"""Bounded recovery never performs automation work."""\ndef main():\n    print('RECOVERY_DISABLED: schema-0033 scheduling automation unavailable.')\nif __name__ == '__main__': main()\n''')
    service=recovery/'app/services/v2_scheduling_policy_service.py'
    service.write_text(service.read_text()+'''\n\ndef run_schedule_automation(*args, **kwargs):\n    raise RuntimeError('RECOVERY_DISABLED: scheduling automation unavailable.')\n''')
    schema=recovery/'app/schema_contract.py'
    value=schema.read_text();start=value.index('def upgrade_database(');end=value.index('\n\ndef ',start+5)
    value=value[:start]+'''def upgrade_database(*args, **kwargs):\n    raise UnsupportedSchemaError('Recovery cannot migrate the database.')\n'''+value[end:]
    start=value.index('def stamp_matching_database(');end=value.find('\n\ndef ',start+5)
    if end<0:raise RuntimeError('Review schema command structure before preparing recovery.')
    value=value[:start]+'''def stamp_matching_database(*args, **kwargs):\n    raise UnsupportedSchemaError('Recovery cannot stamp the database.')\n'''+value[end:]
    schema.write_text(value)
    manifest={'baseline':subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),
        'schema':'20261013_0033','source_files':{name:digest(source/name) for name in files},
        'candidate_directory':str(candidate),'recovery_directory':str(recovery)}
    (output/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def seal(output):
    manifest=json.loads((output/'source-manifest.json').read_text());manifest['artifacts']={}
    for name in ('web-cron','recovery'):
        directory=output/name
        if name=='recovery' and not (directory/'app/recovery_schema_0033.json').is_file():
            raise RuntimeError('Rehearse and install the recovery schema contract before sealing.')
        archive=output/(name+'.tar.gz')
        with tarfile.open(archive,'w:gz') as tar:
            for path in sorted(directory.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts:
                    tar.add(path,arcname=str(path.relative_to(directory)),recursive=False)
        manifest['artifacts'][name]={'sha256':digest(archive),'bytes':archive.stat().st_size,
            'files':{str(p.relative_to(directory)):digest(p) for p in sorted(directory.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}}
    (output/'artifact-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({k:{field:v[field] for field in ('sha256','bytes')} for k,v in manifest['artifacts'].items()},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--seal',action='store_true')
    args=parser.parse_args()
    if args.seal:seal(args.output.resolve())
    else:prepare(Path.cwd(),args.output.resolve())
