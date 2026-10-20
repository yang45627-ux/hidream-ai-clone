import unittest,os,tempfile
from pathlib import Path
import support,server
from tools.migrate_sqlite import migrate

@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'需要独立PostgreSQL测试数据库')
class Migration(unittest.TestCase):
    def test_source_backup_full_copy_and_refuse_nonempty_target(self):
        old=server.DB;dsn=os.environ.pop('DATABASE_URL',None)
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'source.db';server.DB=source
            with server.connect() as c:
                project=server.mutate(c,'/api/projects',{'name':'迁移验证','idea':'状态保持','budget':'10'})['id']
                server.mutate(c,'/api/approvals',{'project':project,'kind':'采购','subject':'样品','amount':'1','reason':'验证'})
            support.start_database(self)
            try:
                result=migrate(source);self.assertTrue(Path(result['backup_file']).is_file())
                with server.connect() as c:
                    state=server.snapshot(c);self.assertEqual(state['projects'][0]['id'],project);self.assertEqual(len(state['approvals']),1)
                    server.log(c,project,'迁移后日志序列验证')
                with self.assertRaisesRegex(ValueError,'非空'):migrate(source)
            finally:support.stop_database(self);server.DB=old
        if dsn is not None:os.environ['DATABASE_URL']=dsn
