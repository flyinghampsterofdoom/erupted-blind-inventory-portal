/** Public cached read projection; no Square credentials or privileged endpoints. */
let responseEtag=null,responsePayload=null;
export function safeSquareImage(value) {
 try {
  const url=new URL(value),host=url.hostname;
  if(url.protocol!=='https:'||url.username||url.password||(url.port&&url.port!=='443')||[...url.searchParams.keys()].some(key=>/token|credential|signature|x-amz/i.test(key)))return null;
  if(!(/^(?:items-images-production|square-production)\.s3(?:\.[a-z0-9-]+)?\.amazonaws\.com$/.test(host)||host==='images.squareup.com'||host==='square-web-production-f.squarecdn.com'))return null;
  return url.href;
 }catch{return null}
}
export function safeProductImage(value){
 if(typeof value!=='string')return null;
 if(/^\/flavor-explorer\/media\/photos\/[a-f0-9]{64}\.webp$/.test(value)||/^\/flavor-explorer\/media\/originals\/[1-9][0-9]{0,18}$/.test(value))return value;
 return safeSquareImage(value);
}
export function productImageCaption(product){
 const image=safeProductImage(product.imageUrl);if(!image)return 'PRODUCT IMAGE COMING SOON';
 return product.imageSource==='staff-photo'||image.startsWith('/flavor-explorer/media/photos/')||image.startsWith('/flavor-explorer/media/originals/')?'PRODUCT PHOTO':'PRODUCT IMAGE FROM SQUARE';
}
export async function loadCatalog() {
 if(typeof EMBEDDED_CATALOG!=='undefined')return EMBEDDED_CATALOG;
 const headers={Accept:'application/json'};
 if(responseEtag)headers['If-None-Match']=responseEtag;
 const response=await fetch('./api/catalog',{credentials:'same-origin',cache:'no-cache',headers});
 if(response.status===304&&responsePayload)return responsePayload;
 if(!response.ok)throw new Error('Availability needs a quick check. Please ask our team.');
 const incoming=response.headers.get('ETag');
 if(incoming&&incoming===responseEtag&&responsePayload)return responsePayload;
 const payload=await response.json();
 if(!Array.isArray(payload.products)||!Array.isArray(payload.stores)||payload.stores.length!==4||payload.meta?.source!=='square')throw new Error('Please ask our team for help finding your flavor.');
 responseEtag=response.headers.get('ETag');responsePayload=payload;
 return payload;
}
