"""Optional PostgreSQL adapter preserving the small SQLite DB-API surface."""
import os

class Record(dict):
    def __getitem__(self,key):return list(self.values())[key] if isinstance(key,int) else super().__getitem__(key)

class Cursor:
    def __init__(self,cursor):self.cursor=cursor
    def fetchone(self):
        row=self.cursor.fetchone();return Record(row) if row is not None else None
    def fetchall(self):return [Record(r) for r in self.cursor.fetchall()]
    def __iter__(self):return iter(self.fetchall())
    @property
    def rowcount(self):return self.cursor.rowcount

class Postgres:
    def __init__(self,url):
        try:import psycopg
        except ImportError:raise RuntimeError('配置了DATABASE_URL，请先安装requirements.txt中的PostgreSQL驱动') from None
        from psycopg.rows import dict_row
        self.connection=psycopg.connect(url,row_factory=dict_row,connect_timeout=10)
    @property
    def in_transaction(self):return self.connection.info.transaction_status!=0
    def execute(self,sql,params=()):
        if sql.strip().upper()=='BEGIN IMMEDIATE':
            # A single transactional advisory lock preserves the original budget/queue critical sections.
            return Cursor(self.connection.execute('SELECT pg_advisory_xact_lock(719430182)'))
        sql=sql.replace('%','%%').replace('?', '%s').replace('ORDER BY rowid','ORDER BY id')
        return Cursor(self.connection.execute(sql,params))
    def executescript(self,script):
        script=script.replace('events(id INTEGER PRIMARY KEY','events(id BIGSERIAL PRIMARY KEY').replace('data BLOB','data BYTEA')
        for statement in script.split(';'):
            if statement.strip():self.connection.execute(statement)
        self.connection.commit()
    def commit(self):self.connection.commit()
    def rollback(self):self.connection.rollback()
    def close(self):self.connection.close()
    def __enter__(self):return self
    def __exit__(self,kind,value,traceback):
        try:self.connection.rollback() if kind else self.connection.commit()
        finally:self.close()

def postgres_url():return os.environ.get('DATABASE_URL','')
