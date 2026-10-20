"""Stop app services first. Migrate a SQLite source into an empty PostgreSQL database."""
import argparse,os,sys,sqlite3,json,datetime,uuid
from pathlib import Path
from urllib.parse import quote
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

TABLES=['projects','accounts','project_owners','tasks','records','events','attempts','grants','messages','campaigns','orders','experiments','entities','entity_versions','flows','plans','approvals','task_specs','files','followups','connectors','external_actions','external_imports','expenses','cash_entries','stores','checkouts','shop_messages','payment_events','order_checks','tool_grants','jobs','tool_uses','tool_charges','sources','assets','asset_fingerprints','video_requests','publications','article_routes','visits','checkout_attribution','purchase_attribution','contacts','shipments','service_actions','channel_metrics','consent_tokens','input_snapshots','fact_conflicts']

def migrate(source):
    if not os.environ.get('DATABASE_URL'):raise ValueError('必须通过服务端DATABASE_URL指定目标PostgreSQL；不接受聊天中的连接凭据')
    source=Path(source).resolve()
    if not source.is_file():raise ValueError('源数据库不存在')
    src=sqlite3.connect('file:'+quote(str(source))+'?mode=ro',uri=True);src.row_factory=sqlite3.Row
    backup=source.with_name(source.name+'.backup-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')+'-'+uuid.uuid4().hex[:8])
    with sqlite3.connect(backup) as b:src.backup(b)
    present={r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts={}
    try:
        with server.connect() as dst:
            dst.execute('BEGIN IMMEDIATE')
            if any(dst.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in TABLES):raise ValueError('目标数据库非空，拒绝覆盖已有数据')
            for table in TABLES:
                if table not in present:continue
                records=src.execute('SELECT * FROM '+table).fetchall()
                for row in records:
                    cols=list(row.keys())
                    dst.execute('INSERT INTO '+table+'('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',tuple(row))
                counts[table]=len(records)
            dst.execute("SELECT setval(pg_get_serial_sequence('events','id'),COALESCE(MAX(id),1),COUNT(*)>0) FROM events")
            # Login sessions intentionally expire during migration; passwords remain hashed.
    finally:src.close()
    return {'tables':counts,'backup_file':str(backup),'sessions':'迁移后请重新登录'}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True);args=parser.parse_args()
    try:print(json.dumps(migrate(args.source),ensure_ascii=False))
    except ValueError as e:raise SystemExit(str(e))
