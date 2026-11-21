import sqlite3, json, hashlib
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS release_versions (
        run_id TEXT NOT NULL, version INTEGER NOT NULL, release_key TEXT NOT NULL,
        plan TEXT NOT NULL, source_sha256 TEXT NOT NULL, rule_version TEXT NOT NULL,
        published_at TEXT NOT NULL, PRIMARY KEY(run_id, version))""")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def _release_history(con,run_id):
    return [{'version':r['version'],'release_key':r['release_key'],'published_at':r['published_at'],
             'plan':json.loads(r['plan'])} for r in con.execute(
        'SELECT version,release_key,plan,published_at FROM release_versions WHERE run_id=? ORDER BY version',(run_id,))]

def _load_result(row):
    result=json.loads(row['result'])
    if 'release' not in result and result.get('input_snapshot'):
        # 旧规则批次按同一源快照补齐日结放款视图，展示与发布不丢证据
        from app.engine import evaluate
        fresh=evaluate({'dataset':result.get('source','dirty')},snapshot=result['input_snapshot'])
        fresh['manifest']=result['manifest'];fresh['input_snapshot']=result['input_snapshot']
        result=fresh
    return result

def get(path, run_id):
    with connect(path) as con:
        row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        history=_release_history(con,run_id)
    result=_load_result(row)
    result.update(revision=row['revision'],status=row['status'])
    result['release_history']=history
    result['published_release']=history[-1] if history else None
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def release_key(result):
    plan=result.get('release')
    if not plan: raise BusinessError('RELEASE_NOT_READY','批次尚未生成放款计划')
    material=json.dumps({'plan':plan,'sha256':result['manifest']['sha256'],
                         'rule_version':result['manifest']['rule_version']},
                        ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()

def publish(path,run_id,revision):
    """原子发布放款计划。同内容重发复用旧版次，不重复占用信用；中途失败旧版次保留。"""
    with connect(path) as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        if row['revision']!=revision: raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
        result=_load_result(row)
        key=release_key(result)
        latest=con.execute('SELECT * FROM release_versions WHERE run_id=? ORDER BY version DESC LIMIT 1',(run_id,)).fetchone()
        if latest and latest['release_key']==key:
            con.execute('UPDATE runs SET status=? WHERE id=?',('已发布',run_id))
            return {'run_id':run_id,'release_version':latest['version'],'release_key':key,
                    'published_at':latest['published_at'],'duplicate':True,'revision':revision}
        version=(latest['version'] if latest else 0)+1
        published_at=datetime.now(timezone.utc).isoformat()
        con.execute('INSERT INTO release_versions(run_id,version,release_key,plan,source_sha256,rule_version,published_at) VALUES(?,?,?,?,?,?,?)',
                    (run_id,version,key,json.dumps(result['release'],ensure_ascii=False),result['manifest']['sha256'],result['manifest']['rule_version'],published_at))
        con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已发布',run_id,revision))
        return {'run_id':run_id,'release_version':version,'release_key':key,
                'published_at':published_at,'duplicate':False,'revision':revision+1}
