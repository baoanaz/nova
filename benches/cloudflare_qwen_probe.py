import concurrent.futures
import json
import os
from pathlib import Path
import time
import numpy as np
import httpx

MODEL=os.environ['CLOUDFLARE_EMBED_MODEL']
BASE='https://api.cloudflare.com/client/v4/accounts/'+os.environ['CLOUDFLARE_ACCOUNT_ID']+'/ai'
client=httpx.Client(headers={'Authorization':'Bearer '+os.environ['CLOUDFLARE_API_TOKEN']},timeout=60,trust_env=False)
usage=[]
def embed(payload, compatible=False):
    start=time.perf_counter()
    r=client.post(BASE+('/v1/embeddings' if compatible else '/run/'+MODEL),json=payload)
    elapsed=time.perf_counter()-start
    if r.status_code!=200:
        raise RuntimeError('Embedding HTTP '+str(r.status_code))
    d=r.json()
    if d.get('success') is False: raise RuntimeError('Embedding unsuccessful')
    result=d.get('result',d)
    u=result.get('usage',d.get('usage',{}));usage.append(u)
    data=result['data']
    if isinstance(data[0],dict):data=[x['embedding'] for x in sorted(data,key=lambda x:x['index'])]
    vectors=np.array(data,dtype=np.float32)
    assert vectors.ndim==2 and vectors.shape[1]==1024 and np.isfinite(vectors).all()
    return vectors,elapsed,u

# Paired synthetic snippets: each query has a specific target and a same-domain distractor.
cases=[
('password_verify','def authenticate(password, saved_hash):\n    return argon2.verify(saved_hash, password)', '登录时在哪里核对用户密码是否正确？','Where is the supplied password checked against its stored hash?'),
('password_hash','def save_password(password):\n    return argon2.hash(password)', '新密码入库前在哪里做不可逆处理？','Where is a new password hashed before persistence?'),
('token_revoke','def revoke_api_token(token_id):\n    db.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (token_id,))','使已经发出的 API 凭证失效。','Invalidate an existing API credential.'),
('token_issue','def issue_api_token(user_id):\n    token = secrets.token_urlsafe(32)\n    db.insert_token(user_id, sha256(token))\n    return token','给用户生成新的访问密钥。','Create a fresh access key for a user.'),
('cache_expire','def cache_get(key):\n    value, expires_at = cache[key]\n    if expires_at < time.time():\n        del cache[key]\n        return None\n    return value','缓存超过有效期后不再返回旧值。','Reject a cached result after its TTL expires.'),
('cache_lru','def cache_put(key, value):\n    cache[key] = value\n    cache.move_to_end(key)\n    if len(cache) > capacity:\n        cache.popitem(last=False)','容量不足时淘汰最久没用的缓存。','Evict the least recently used entry when cache capacity is exceeded.'),
('retry_429','async def request_with_retry(client):\n    response = await client.post(url)\n    if response.status_code == 429:\n        await asyncio.sleep(float(response.headers.get("Retry-After", 1)))\n        return await client.post(url)\n    return response','上游限流后按提示等待再试。','Wait and retry when the upstream rate limit is exceeded.'),
('reject_401','def check_response(response):\n    if response.status_code == 401:\n        raise InvalidCredentials("Replace the API key; do not retry")','凭据无效时立即报错而非重试。','Fail immediately rather than retrying an invalid credential.'),
('markdown_filter','def allowed(path):\n    return path.suffix.lower() in {".md", ".markdown"}','只允许 Markdown 文档进入索引。','Allow only Markdown documents into the index.'),
('binary_filter','def is_text(content):\n    return b"\\x00" not in content and content.decode("utf-8", errors="ignore").isprintable()','如何防止二进制内容被当作文本处理？','How are binary blobs excluded from text processing?'),
('git_branch','def branch_name(root):\n    return subprocess.check_output(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"]).decode().strip()','获得当前代码检出的分支名称。','Get the name of the currently checked-out Git branch.'),
('git_remote','def remote_url(root):\n    return subprocess.check_output(["git", "-C", root, "remote", "get-url", "origin"]).decode().strip()','读取仓库 origin 的远程地址。','Read the URL of the repository origin remote.'),
('delete_vectors','def remove_file_vectors(path):\n    table.delete("file_path = ?", [path])','源文件删掉后如何清除搜索中的残留向量？','Remove stale search vectors after a source file is deleted.'),
('upsert_vectors','def update_vectors(chunks):\n    table.merge_insert("chunk_id").when_matched_update_all().when_not_matched_insert_all().execute(chunks)','新增和修改的代码块如何写入向量库？','Insert new chunk vectors and replace changed ones.'),
('lock_process','def acquire_project_lock(path):\n    fd = open(path, "a+")\n    fcntl.flock(fd, fcntl.LOCK_EX)\n    return fd','多个进程怎样避免同时修改同一索引？','Prevent concurrent processes from writing the same index.'),
('lock_async','async def update_local_state(value):\n    async with asyncio.Lock():\n        state.value = value','协程之间如何串行修改内存状态？','Serialize coroutine updates to in-memory state.'),
]
report={'model':MODEL,'dataset':'16 synthetic snippets, paired distractors, 32 bilingual queries; not a real-repository quality benchmark','scenarios':[]}
docs=[x[1] for x in cases]
dv,elapsed,u=embed({'documents':docs})
queries=[x[i] for x in cases for i in (2,3)]
qv,_,_=embed({'queries':queries,'instruction':'Given a code search query, retrieve the relevant code snippet that implements the requested behavior.'})
dv=dv/np.linalg.norm(dv,axis=1,keepdims=True);qv=qv/np.linalg.norm(qv,axis=1,keepdims=True)
ranks=[];misses=[]
for i,scores in enumerate(qv@dv.T):
    order=np.argsort(-scores).tolist();expected=i//2;rank=order.index(expected)+1;ranks.append(rank)
    if rank!=1:misses.append({'query':queries[i],'expected':cases[expected][0],'top1':cases[order[0]][0],'rank':rank})
report['retrieval']={'queries':len(ranks),'candidates':len(cases),'top1':sum(r==1 for r in ranks)/len(ranks),'recall_at_3':sum(r<=3 for r in ranks)/len(ranks),'mrr':sum(1/r for r in ranks)/len(ranks),'misses':misses}
print('RETRIEVAL',json.dumps(report['retrieval'],ensure_ascii=False),flush=True)
v,latency,u=embed({'model':MODEL,'input':['Code search compatibility probe.']},compatible=True)
report['openai_compatible']={'shape':list(v.shape),'latency_s':latency,'usage':u}
for concurrency in (1,4):
    def request(i):
        texts=[f'# Independent workload {concurrency}-{i}-{j}\n'+docs[(i+j)%len(docs)]+'\n'+('The index stores code chunks with file paths, content hashes and vector embeddings. Updates replace changed entries and preserve unchanged vectors.\n'*12) for j in range(8)]
        _,latency,u=embed({'documents':texts})
        return latency,u
    started=time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        results=list(pool.map(request,range(8)))
    elapsed=time.perf_counter()-started
    tokens=sum(u.get('prompt_tokens',u.get('total_tokens',0)) for _,u in results)
    latencies=sorted(x[0] for x in results)
    scenario={'concurrency':concurrency,'requests':8,'texts_per_request':8,'prompt_tokens':tokens,'wall_s':elapsed,'observed_tokens_per_minute':tokens/elapsed*60,'observed_requests_per_minute':8/elapsed*60,'latency_p50_s':float(np.median(latencies)),'latency_max_s':max(latencies)}
    report['scenarios'].append(scenario)
    print('THROUGHPUT',json.dumps(scenario),flush=True)
report['total_usage']={'prompt_tokens':sum(u.get('prompt_tokens',0) for u in usage),'neurons':sum(u.get('neurons',0) for u in usage),'requests':len(usage)}
Path('/tmp/nova-cf-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print('TOTAL',json.dumps(report['total_usage']),flush=True)
client.close()
