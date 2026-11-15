import {it,expect} from 'vitest';
import {Miniflare} from 'miniflare';
import {build} from 'esbuild';
import {readFile} from 'node:fs/promises';
import {generateDevice,sign,canonical,hex,random,sha256,encoder} from '../src/crypto';

it('allows only fresh owner-signed fixed maintenance operations and verified ciphertext state',async()=>{
 const owner=generateDevice('knovv','owner'),other=generateDevice('knovv','other');
 const built=await build({stdin:{contents:`import {api} from './server/api';export default {fetch:(r,e)=>api(r,e,{email:e.OWNER_EMAIL})}`,resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'browser'});
 const mf=new Miniflare({modules:true,script:built.outputFiles[0].text,compatibilityDate:'2026-08-01',d1Databases:{DB:'control-test'},r2Buckets:['BUCKET'],bindings:{OWNER_PUBLIC_KEY:owner.signing_public,OWNER_EMAIL:'owner@example.test',MAX_CIPHER_BYTES:'1048576'}});
 try{
  const db=await mf.getD1Database('DB'),bucket=await mf.getR2Bucket('BUCKET');
  await db.exec((await readFile('server/schema.sql','utf8')).replace(/\n/g,' '));
  await db.prepare('INSERT INTO knovv_identity VALUES (1,?)').bind(owner.signing_public).run();
  async function send(operation:string,params:unknown[],updates:Record<string,unknown>={},key=owner.signing_key){
   const value=sign({schema:'knovv-control.v1',origin:'https://knovv.test',operation,params,issued:Math.floor(Date.now()/1000),nonce:hex(random()),...updates},key);
   return mf.dispatchFetch('https://knovv.test/api/control',{method:'POST',headers:{origin:'https://knovv.test','content-type':'application/json'},body:canonical(value)});
  }
  expect((await send('tables',[])).status).toBe(200);
  expect((await send('tables',[],{},other.signing_key)).status).toBe(403);
  expect((await send('tables',[],{issued:Math.floor(Date.now()/1000)-601})).status).toBe(403);
  expect((await send('tables',[],{origin:'https://other.test'})).status).toBe(403);
  expect((await send('constructor',[])).status).toBe(400);
  expect((await send('DROP TABLE objects',[])).status).toBe(400);
  const bytes=encoder.encode('synthetic ciphertext'),oid=sha256(bytes);
  expect((await send('object-reserve',[oid,bytes.length,bytes.length,2097152])).status).toBe(400);
  expect((await send('object-reserve',[oid,bytes.length,bytes.length,1048576])).status).toBe(200);
  expect((await send('object-verified',[oid])).status).toBe(409);
  await bucket.put(oid,bytes);
  expect((await send('object-verified',[oid])).status).toBe(200);
  expect((await send('object-reserve',[oid,bytes.length,bytes.length,1048576])).status).toBe(200);
  expect((await db.prepare('SELECT bytes FROM usage').first<any>()).bytes).toBe(bytes.length);
 }finally{await mf.dispose();}
},30000);
