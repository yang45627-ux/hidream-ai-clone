"""Keep tests off production databases; optionally exercise a real PostgreSQL server."""
import os,uuid
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode,quote

def start_database(owner):
    owner.original_dsn=os.environ.pop('DATABASE_URL',None);owner.pg_schema=None
    test_url=os.environ.get('TEST_DATABASE_URL')
    if test_url:
        import psycopg
        from psycopg import sql
        schema='test_'+uuid.uuid4().hex;owner.pg_schema=schema
        with psycopg.connect(test_url,autocommit=True) as c:c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        u=urlsplit(test_url);params=parse_qsl(u.query);params.append(('options','-c search_path='+schema))
        os.environ['DATABASE_URL']=urlunsplit((u.scheme,u.netloc,u.path,urlencode(params,quote_via=quote),u.fragment))

def stop_database(owner):
    os.environ.pop('DATABASE_URL',None)
    if owner.pg_schema:
        import psycopg
        from psycopg import sql
        with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(owner.pg_schema)))
    if owner.original_dsn is not None:os.environ['DATABASE_URL']=owner.original_dsn
