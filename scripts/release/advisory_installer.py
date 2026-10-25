"""Deployment-only, bounded private artifact download. No business DB or Square imports."""
import os,json,hashlib,tempfile,tarfile,sqlite3,urllib.request,urllib.parse
from pathlib import Path

def main():
    expected=json.loads(os.environ['FORECAST_ADVISORY_ASSET_SPEC'])
    assert os.environ.get('RENDER_GIT_COMMIT',expected['revision'])==expected['revision']
    assert os.environ['FORECAST_PRODUCT_PROJECTION_SHA256']==expected['projection_sha256']
    url=os.environ['FORECAST_PRODUCT_ARTIFACT_URL'];parsed=urllib.parse.urlsplit(url)
    assert parsed.scheme=='https' and parsed.hostname.endswith('.r2.cloudflarestorage.com') and parsed.username is None
    destination=Path(os.environ['FORECAST_PRODUCT_PROJECTION_PATH'])
    assert destination.resolve()==(Path.cwd()/'.forecast-stage4'/'product.sqlite3').resolve()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**kw):raise ValueError('Artifact redirects are prohibited')
    opener=urllib.request.build_opener(NoRedirect())
    destination.parent.mkdir(mode=0o700,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.prepare-',dir=destination.parent) as tmp:
        archive=Path(tmp)/'artifact.tar.gz';sha=hashlib.sha256();size=0
        with opener.open(urllib.request.Request(url,headers={'Accept-Encoding':'identity'}),timeout=60) as response, archive.open('xb') as output:
            assert response.status==200
            for chunk in iter(lambda:response.read(1024*1024),b''):
                size+=len(chunk);assert size<=expected['archive_bytes'];sha.update(chunk);output.write(chunk)
        assert size==expected['archive_bytes'] and sha.hexdigest()==expected['archive_sha256']
        target=Path(tmp)/'product.sqlite3'
        with tarfile.open(archive,'r:gz') as tar:
            members=tar.getmembers()
            assert len(members)==2 and {m.name for m in members}=={'product.sqlite','release-manifest.json'}
            assert all(m.isfile() and not m.issym() and not m.islnk() for m in members)
            assert tar.getmember('product.sqlite').size==expected['projection_bytes']
            assert tar.getmember('release-manifest.json').size<65536
            manifest=json.load(tar.extractfile('release-manifest.json'))
            assert manifest['web_candidate']==expected['revision'] and manifest['projection_sha256']==expected['projection_sha256']
            h=hashlib.sha256()
            with tar.extractfile('product.sqlite') as source,target.open('xb') as output:
                for chunk in iter(lambda:source.read(1024*1024),b''):h.update(chunk);output.write(chunk)
                output.flush();os.fsync(output.fileno())
        assert h.hexdigest()==expected['projection_sha256']
        with sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA application_id').fetchone()[0]==1163282000
            assert db.execute('PRAGMA user_version').fetchone()[0]==2
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert db.execute('SELECT count(*) FROM products').fetchone()[0]==21152
            assert db.execute('SELECT count(*) FROM details').fetchone()[0]==5288
        target.chmod(0o400)
        if destination.exists():
            assert hashlib.sha256(destination.read_bytes()).hexdigest()==expected['projection_sha256']
        os.replace(target,destination)
    print(json.dumps({'event':'ERUPTED_ADVISORY_PROJECTION_VERIFIED','revision':expected['revision'],'sha256':expected['projection_sha256'],'store_sku_rows':5288,'network_source':'private deployment artifact only','Square_requests':0,'business_writes':0}))

if __name__=='__main__':
    try:main()
    except Exception as exc:raise RuntimeError('Advisory artifact preparation failed: '+type(exc).__name__) from None
