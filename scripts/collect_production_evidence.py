"""Finite source-only worker; activation is an external reviewed daily schedule."""
import argparse,json,time,sys
from uuid import uuid4
from app.services.runtime_identity_service import capture_identity


def main():
    p=argparse.ArgumentParser();p.add_argument('--reason',choices=('DAILY','RECOVERY','MANUAL'),required=True);a=p.parse_args()
    identity=capture_identity();print(json.dumps(dict(event='ERUPTED_RUNTIME_IDENTITY',role='evidence-collector',**identity)),flush=True)
    if identity['status']!='VERIFIED':raise RuntimeError('Unverified source artifact; no collection allowed')
    from app.services.daily_evidence_runtime import configured,source_post
    from app.services.daily_square_collector import run_once
    engine,target=configured();key=None if a.reason=='DAILY' else a.reason.lower()+':'+str(uuid4())
    try:
        for attempt in range(3):
            print(json.dumps(dict(event='EVIDENCE_COLLECTION_STARTED',reason=a.reason,attempt=attempt+1)),flush=True)
            try:result=run_once(engine,source_post,reason=a.reason,command_key=key,target=target)
            except Exception as exc:
                print(json.dumps(dict(event='EVIDENCE_COLLECTION_FAILED',error_type=type(exc).__name__,attempt=attempt+1)),flush=True)
                if attempt==2:raise SystemExit(1) from None
                time.sleep(2**attempt);continue
            print(json.dumps(dict(event='EVIDENCE_COLLECTION_COMPLETED',**result)),flush=True)
            if result['status']=='PARTIAL' and attempt<2:time.sleep(2**attempt);continue
            if result['status']=='PARTIAL':raise SystemExit(2)
            return
    finally:engine.dispose()

if __name__=='__main__':main()
