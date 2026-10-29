"""Explicit on-demand offline calculation. No configuration discovery or DB access."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime
from .calculation_documents import configuration_from_document,store_from_document
from .calculation_serialization import canonical_bytes,evidence_digest
from .calculation import recommend_batch


def atomic_publish(path,payload,*,before_replace=None):
    path=Path(path)
    descriptor,temporary=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        if before_replace:before_replace()
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def calculate(input_path,profile_path,output_path):
    if Path(output_path).resolve() in (Path(input_path).resolve(),Path(profile_path).resolve()):
        raise ValueError('Output must differ from input and profile')
    document=json.loads(Path(input_path).read_text())
    configuration=configuration_from_document(json.loads(Path(profile_path).read_text()))
    rows=tuple(store_from_document(r) for r in document['rows'])
    as_of=datetime.fromisoformat(document['as_of'])
    exceptions=tuple(document['source_exceptions'])
    fingerprint=evidence_digest(rows,as_of=as_of,source_exceptions=exceptions)
    result=recommend_batch(rows,configuration,as_of=as_of,input_digest=fingerprint,source_exceptions=exceptions)
    atomic_publish(output_path,canonical_bytes(result))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True);parser.add_argument('--profile',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if Path(args.output).resolve() in (Path(args.input).resolve(),Path(args.profile).resolve()):
        parser.error('Output must differ from input and profile')
    calculate(args.input,args.profile,args.output)

if __name__=='__main__':main()
