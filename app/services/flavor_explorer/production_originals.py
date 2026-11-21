"""Read existing staff bottle photos by exact active Square variation identity."""
from io import BytesIO
import hashlib,json
from sqlalchemy import text

PREFIX="/flavor-explorer/media/originals/"
MAX_BYTES=12*1024*1024
QUERY="""
SELECT l.square_variation_id, l.format, a.id, a.storage_key, a.content_type,
       a.size_bytes, a.content_hash, a.width, a.height
FROM touchscreen_flavor_sku_links l
JOIN touchscreen_flavors f ON f.id=l.touchscreen_flavor_id
JOIN touchscreen_flavor_media m ON m.touchscreen_flavor_id=f.id AND m.role='PRIMARY'
JOIN digital_signage_media_assets a ON a.id=m.media_asset_id
WHERE l.is_active AND f.is_active AND f.deleted_at IS NULL
  AND a.media_type='IMAGE' AND a.archived_at IS NULL
"""

class ExistingOriginals:
    def __init__(self,engine,object_storage=None):
        self.engine=engine;self.object_storage=object_storage

    def rows(self):
        with self.engine.connect() as c:return [dict(row) for row in c.execute(text(QUERY)).mappings()]

    def version(self):
        # Replacement can update media_asset_id in place; max IDs/timestamps miss it.
        rows=self.rows()
        return hashlib.sha256(json.dumps(sorted(
            (r["square_variation_id"],r["format"],r["id"],r["content_hash"],r["size_bytes"]) for r in rows),
            separators=(",",":"),default=str).encode()).hexdigest()

    def attach(self,products):
        by={}
        for row in self.rows():
            by.setdefault((row["square_variation_id"],row["format"]),set()).add(row["id"])
        for product in products:
            product.pop("originalImageUrl",None)
            kind={"freebase":"FREEBASE","salt":"SALT"}.get(product.get("type"))
            ids=product.get("squareVariationIds") or [v["id"] for v in product.get("variations",[])]
            candidates=[by.get((vid,kind),set()) for vid in ids]
            if candidates and all(len(values)==1 for values in candidates):
                assets=set.union(*candidates)
                if len(assets)==1:product["originalImageUrl"]=PREFIX+str(assets.pop())
        return products

    def asset(self,asset_id):
        if type(asset_id) is not int or asset_id<=0:raise LookupError("Product photo is unavailable.")
        rows=[r for r in self.rows() if r["id"]==asset_id]
        if not rows:raise LookupError("Product photo is unavailable.")
        row=rows[0]
        if row["content_type"] not in {"image/png","image/jpeg","image/webp"} or not 0<row["size_bytes"]<=MAX_BYTES:
            raise LookupError("Product photo is unavailable.")
        if self.object_storage is None:
            from app.services.digital_signage_storage import configured_signage_storage
            objects=configured_signage_storage()
        else:objects=self.object_storage
        data=objects.get(row["storage_key"])
        if len(data)!=row["size_bytes"] or hashlib.sha256(data).hexdigest()!=row["content_hash"]:
            raise ValueError("Retained product photo failed integrity verification.")
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"JPEG","PNG","WEBP"} or getattr(image,"n_frames",1)!=1 or not (0<image.width<=16384 and 0<image.height<=16384):
                raise ValueError("Retained product photo is unsupported.")
            if {"PNG":"image/png","JPEG":"image/jpeg","WEBP":"image/webp"}[image.format]!=row["content_type"] or (image.width,image.height)!=(row["width"],row["height"]):
                raise ValueError("Retained product photo metadata differs.")
            image.verify()
        return {"data":data,"mime":row["content_type"]}
