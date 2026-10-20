"""Portable application checkpoint, streamed JSONL + files, restored atomically into empty DB."""
import base64,hashlib,json,os,sys,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as s
from tools.migrate_sqlite import TABLES

def encode(row):return {k:{'$bytes':base64.b64encode(bytes(v)).decode()} if isinstance(v,(bytes,bytearray,memoryview)) else v for k,v in row.items()}
def decode(row):
    if not isinstance(row,dict):raise ValueError('备份行无效')
    return {k:base64.b64decode(v['$bytes'],validate=True) if isinstance(v,dict) and set(v)=={'$bytes'} else v for k,v in row.items()}

def backup(target):
    path=Path(target).resolve()
    if path.exists():raise ValueError('备份目标已存在，拒绝覆盖')
    path.parent.mkdir(parents=True,exist_ok=True);fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    manifest={'application':'venture-workspace','format':1,'created':s.now(),'tables':{}}
    try:
        with s.connect() as c,zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            if os.environ.get('DATABASE_URL'):c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            else:c.execute('BEGIN')
            for table in TABLES:
                digest=hashlib.sha256();count=0;cursor=c.execute('SELECT * FROM '+table)
                with archive.open(table+'.jsonl','w') as out:
                    while True:
                        rows=cursor.fetchmany(1 if table=='files' else 100)
                        if not rows:break
                        for row in rows:
                            raw=(json.dumps(encode(dict(row)),ensure_ascii=False,separators=(',',':'))+'\n').encode();digest.update(raw);out.write(raw);count+=1
                manifest['tables'][table]={'count':count,'sha256':digest.hexdigest()}
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
        with zipfile.ZipFile(path) as archive:
            if archive.testzip():raise ValueError('备份ZIP完整性检查失败')
        return {'status':'应用检查点已生成','tables':{k:v['count'] for k,v in manifest['tables'].items()},'sessions':'登录会话不包含在应用检查点，恢复后重新登录'}
    except Exception:path.unlink(missing_ok=True);raise

def restore(source):
    path=Path(source).resolve()
    with zipfile.ZipFile(path) as archive:
        if sum(i.file_size for i in archive.infolist())>2*1024*1024*1024:raise ValueError('应用检查点超过2GiB，请使用数据库原生恢复工具')
        manifest=json.loads(archive.read('manifest.json'))
        if manifest.get('application')!='venture-workspace' or manifest.get('format')!=1:raise ValueError('检查点格式无效')
        if set(manifest['tables'])!=set(TABLES):raise ValueError('检查点结构与当前应用版本不同，请先匹配版本后恢复')
        counts={}
        with s.connect() as c:
            s.begin_write(c)
            if any(c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in TABLES+['model_connections']):raise ValueError('恢复目标非空，拒绝覆盖')
            for table in TABLES:
                cursor=c.execute('SELECT * FROM '+table+' LIMIT 0');description=getattr(cursor,'description',None) or cursor.cursor.description;columns={col[0] for col in description}
                digest=hashlib.sha256();count=0
                with archive.open(table+'.jsonl') as stream:
                    for raw in stream:
                        if len(raw)>50*1024*1024:raise ValueError('检查点单行超出限制')
                        digest.update(raw);row=decode(json.loads(raw))
                        if set(row)!=columns:raise ValueError('检查点字段与目标不一致')
                        if any(isinstance(v,(dict,list)) for v in row.values()):raise ValueError('检查点值类型无效')
                        keys=list(row);c.execute('INSERT INTO '+table+'('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',tuple(row.values()));count+=1
                expected=manifest['tables'][table]
                if count!=expected['count'] or digest.hexdigest()!=expected['sha256']:raise ValueError('检查点数据或校验和不一致，已回滚')
                counts[table]=count
            if os.environ.get('DATABASE_URL'):c.execute("SELECT setval(pg_get_serial_sequence('events','id'),COALESCE(MAX(id),1),COUNT(*)>0) FROM events")
            else:
                if c.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('恢复后的数据关联检查失败')
    return {'status':'已原子恢复到空目标','counts':counts,'next':'核对业务后再启动服务；运行中请求重启后只核查，不重复发送'}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['backup','restore']);parser.add_argument('--file',required=True);args=parser.parse_args()
    try:print(json.dumps(backup(args.file) if args.action=='backup' else restore(args.file),ensure_ascii=False))
    except Exception as e:raise SystemExit(str(e) if isinstance(e,ValueError) else '检查点操作失败，请核查文件、权限和安全数据库连接；未输出连接凭据')
