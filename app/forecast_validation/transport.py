"""One finite request/retry/deadline budget, including unsuccessful HTTP attempts."""
import time
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError,URLError
from .contracts import digest

class BoundedRead:
    def __init__(self,post,max_calls=400,budget=900,*,max_retries=2,max_retry_attempts=20,sleep=time.sleep,clock=time.monotonic):
        if not 1<=max_calls<=400 or not 0<budget<=900 or not 0<=max_retries<=2 or not 0<=max_retry_attempts<=20:raise ValueError('Invalid bounded source limits')
        self.post=post;self.max_calls=max_calls;self.clock=clock;self.started=clock();self.deadline=self.started+budget;self.calls=0;self.max_retries=max_retries;self.retry_remaining=max_retry_attempts;self.sleep=sleep;self.records=[]
    def __call__(self,path,payload):
        for attempt in range(self.max_retries+1):
            remaining=self.deadline-self.clock()
            if self.calls>=self.max_calls or remaining<=0:raise TimeoutError('Source read budget exhausted')
            self.calls+=1;started=self.clock();record={'path':path,'payload_sha256':digest(payload),'attempt':attempt+1}
            try:
                result=self.post(path,payload,timeout_seconds=min(15,remaining))
                field='items' if 'catalog' in path else 'counts' if '/counts/' in path else 'changes'
                record.update(status='COMPLETE',records=len(result.get(field,[])) if isinstance(result,dict) else 0,has_cursor=bool(result.get('cursor')) if isinstance(result,dict) else False)
                return result
            except Exception as exc:
                cause=exc
                while cause.__cause__ is not None:cause=cause.__cause__
                status=getattr(cause,'code',None);retryable=status in (429,502,503,504) or isinstance(cause,(URLError,TimeoutError,ConnectionError)) and not isinstance(cause,HTTPError)
                record.update(status='FAILED',error_type=type(cause).__name__,http_status=status)
                if not retryable or attempt==self.max_retries or self.retry_remaining<=0:raise
                delay=2**attempt
                if isinstance(cause,HTTPError) and cause.headers and cause.headers.get('Retry-After'):
                    raw=cause.headers['Retry-After']
                    try:delay=max(delay,float(raw))
                    except ValueError:
                        try:delay=max(delay,(parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds())
                        except Exception:raise exc
                if delay>5 or self.clock()+delay>=self.deadline or self.calls>=self.max_calls:raise
                self.retry_remaining-=1;self.sleep(delay)
            finally:
                record['seconds']=self.clock()-started;self.records.append(record)
    def metrics(self):
        return {'requests':self.calls,'maximum_requests':self.max_calls,'elapsed_seconds':self.clock()-self.started,'retries':sum(r['attempt']>1 for r in self.records),'pages':sum(r['status']=='COMPLETE' for r in self.records),'records':sum(r.get('records',0) for r in self.records),'attempts':self.records}
