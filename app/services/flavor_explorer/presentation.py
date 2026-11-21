"""Pure staff Explorer presentation and cache boundaries; no local data authority."""
import gzip,hashlib,json,threading,time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

IMAGE_SOURCES="https://items-images-production.s3.amazonaws.com https://items-images-production.s3.us-west-2.amazonaws.com https://items-images-production.s3.us-west-1.amazonaws.com https://square-production.s3.amazonaws.com https://images.squareup.com https://square-web-production-f.squarecdn.com"

CSP=f"default-src 'self'; img-src 'self' data: {IMAGE_SOURCES}; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

def portal_document_path(value):
    decoded=unquote(str(value).split("?",1)[0].split("#",1)[0])
    return Path(decoded).name.casefold() in {"admin.html","staff.html","compatibility.html","compatibility-request.html","owner-preview.html"}

class ProjectionCache:
    def __init__(self,loader,versions,clock=time.time):
        self.loader=loader;self.versions=versions;self.clock=clock;self.lock=threading.Lock()
        self.version=None;self.body=None;self.compressed=None;self.etag=None;self.expires=0
    def get(self):
        with self.lock:
            version=self.versions();now=self.clock()
            if self.body is not None and version==self.version and now<self.expires:
                return self.body,self.compressed,self.etag
            payload=deepcopy(self.loader());payload.setdefault("meta",{}).pop("projectedAt",None)
            body=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode()
            deadlines=[]
            for product in payload.get("products",[]):
                for variation in product.get("variations",[]):
                    for record in variation.get("availability",{}).values():
                        value=record.get("validUntil")
                        if not isinstance(value,str):continue
                        try:deadline=datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()
                        except (TypeError,ValueError):continue
                        if deadline>now:deadlines.append(deadline)
            self.version=version;self.body=body;self.compressed=gzip.compress(body)
            self.etag='W/"'+hashlib.sha256(body).hexdigest()+'"'
            self.expires=min(deadlines) if deadlines else float("inf")
            return body,self.compressed,self.etag
