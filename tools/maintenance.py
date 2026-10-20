"""Administrator backup/restore/retention commands. Never overwrite a populated target."""
import argparse,datetime,os,sqlite3,subprocess,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as s

def pg_env():
    import psycopg.conninfo
    values=psycopg.conninfo.conninfo_to_dict(os.environ['DATABASE_URL'])
    env=os.environ.copy()
    mapping={'host':'PGHOST','port':'PGPORT','dbname':'PGDATABASE','user':'PGUSER','password':'PGPASSWORD','sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','options':'PGOPTIONS'}
    for key,value in values.items():
        if key not in mapping:raise ValueError('备份连接含未支持参数，请使用服务商备份工具或受支持的libpq配置')
        env[mapping[key]]=value
    return env

def backup(target):
    target=Path(target).resolve()
    if target.exists():raise ValueError('备份目标已存在，拒绝覆盖')
    target.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    try:
        if os.environ.get('DATABASE_URL'):
            subprocess.run(['pg_dump','--format=custom','--no-owner','--no-acl','--file',str(target)],env=pg_env(),check=True,capture_output=True)
            subprocess.run(['pg_restore','--list',str(target)],check=True,capture_output=True)
        else:
            if not s.DB.is_file():raise ValueError('源数据库不存在')
            with sqlite3.connect('file:'+str(s.DB.resolve())+'?mode=ro',uri=True) as src,sqlite3.connect(target) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('备份完整性检查失败')
        return {'status':'已备份并验证文件结构','bytes':target.stat().st_size,'restore_drill':'仍须恢复到独立目标核对业务数据'}
    except Exception:
        target.unlink(missing_ok=True);raise

def restore_sqlite(source,target):
    source=Path(source).resolve();target=Path(target).resolve()
    if target.exists():raise ValueError('恢复目标已存在，拒绝覆盖')
    if not source.is_file():raise ValueError('备份不存在')
    target.parent.mkdir(parents=True,exist_ok=True);fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    try:
        with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as src,sqlite3.connect(target) as dst:
            if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('源备份完整性检查失败')
            src.backup(dst)
            if dst.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('恢复后关联数据检查失败')
            counts={name:dst.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ['projects','tasks','approvals','orders']}
        return {'status':'已恢复到新目标','counts':counts}
    except Exception:target.unlink(missing_ok=True);raise

def restore_postgres(source):
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL']) as c:
        count=c.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]
        if count:raise ValueError('目标PostgreSQL非空，拒绝覆盖')
    environment=pg_env()
    subprocess.run(['pg_restore','--exit-on-error','--single-transaction','--no-owner','--no-acl','--dbname',environment.get('PGDATABASE','postgres'),str(Path(source).resolve())],env=environment,check=True,capture_output=True)
    return {'status':'恢复完成，请独立核对数据后切换服务'}

def purge(days=90):
    if not isinstance(days,int) or not 30<=days<=730:raise ValueError('匿名访问保留期限须为30–730天')
    before=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=days)).isoformat()
    with s.connect() as c:
        result=c.execute('DELETE FROM visits WHERE created<?',(before,)).rowcount
        c.execute('DELETE FROM sessions WHERE expires<?',(__import__('time').time(),))
    return {'anonymous_visits_deleted':result,'business_records':'订单、审批、来源与经营档案不在此命令中自动删除'}

if __name__=='__main__':
    import json
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    a=sub.add_parser('backup');a.add_argument('--output',required=True)
    a=sub.add_parser('restore-sqlite');a.add_argument('--source',required=True);a.add_argument('--target',required=True)
    a=sub.add_parser('restore-postgres');a.add_argument('--source',required=True)
    a=sub.add_parser('purge-anonymous');a.add_argument('--days',type=int,default=90)
    args=p.parse_args()
    try:
        result=backup(args.output) if args.action=='backup' else restore_sqlite(args.source,args.target) if args.action=='restore-sqlite' else restore_postgres(args.source) if args.action=='restore-postgres' else purge(args.days)
        print(json.dumps(result,ensure_ascii=False))
    except (ValueError,sqlite3.DatabaseError,FileNotFoundError,subprocess.CalledProcessError):raise SystemExit('操作未完成；未覆盖已有目标。请核查数据库、权限、客户端工具及安全连接配置；不输出连接凭据。')
