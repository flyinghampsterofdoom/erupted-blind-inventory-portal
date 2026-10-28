import {build} from 'esbuild';
import {writeFile,readdir,unlink} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const out='../../app/purchasing_v4/worksheet_static';
for(const file of await readdir(out))if(/^app-.*\.(js|css|map)$/.test(file))await unlink(`${out}/${file}`);
const result=await build({entryPoints:['src/app.ts'],bundle:true,format:'esm',target:['chrome120','firefox120','safari17'],outdir:out,entryNames:'app-[hash]',metafile:true,minify:true,sourcemap:true,logOverride:{'css-syntax-error':'error'}});
const outputs=Object.keys(result.metafile.outputs);await writeFile(`${out}/assets.json`,JSON.stringify({js:outputs.find(s=>s.endsWith('.js')).split('/').pop(),css:outputs.find(s=>s.endsWith('.css')).split('/').pop()},null,2));
