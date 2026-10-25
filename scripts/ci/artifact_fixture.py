"""Synthetic projection/archive fixture. Never reads business or hosted data."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from scripts.release.artifact_preflight import repackage, INSTALLER

REVISION='ea2c7abe877a88db9f6d84a470e9c9cc00ee7dad'

def make(root, revision=REVISION):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    projection=root/'synthetic.sqlite'
    with sqlite3.connect(projection) as db:
        db.execute('PRAGMA application_id=1163282000');db.execute('PRAGMA user_version=2')
        db.execute('CREATE TABLE products (id INTEGER PRIMARY KEY)');db.execute('CREATE TABLE details (id INTEGER PRIMARY KEY)')
        db.executemany('INSERT INTO products VALUES (?)',[(n,) for n in range(21152)])
        db.executemany('INSERT INTO details VALUES (?)',[(n,) for n in range(5288)])
    digest=hashlib.sha256(projection.read_bytes()).hexdigest()
    spec=repackage(projection,{'projection_sha256':digest,'schema':'forecast-product-release-v2','synthetic':True},digest,revision,root/'artifact.tar.gz')
    now=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    env={'RENDER_GIT_COMMIT':revision,'FORECAST_ADVISORY_ASSET_SPEC':json.dumps(spec),
        'FORECAST_ADVISORY_INSTALLER_B64':base64.b64encode(INSTALLER.read_bytes()).decode(),
        'FORECAST_PRODUCT_ARTIFACT_URL':'https://synthetic.r2.cloudflarestorage.com/fixture?X-Amz-Date='+now+'&X-Amz-Expires=3600&X-Amz-Signature=SYNTHETIC',
        'FORECAST_PRODUCT_PROJECTION_PATH':'/opt/render/project/src/.forecast-stage4/product.sqlite3',
        'FORECAST_PRODUCT_PROJECTION_SHA256':digest,'FORECAST_VALIDATION_WORKER_ENABLED':'false','FORECAST_VALIDATION_SCHEDULE_ENABLED':'false'}
    return spec,env
