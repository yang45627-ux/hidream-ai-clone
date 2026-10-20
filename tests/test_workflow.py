import support
import unittest, tempfile
from pathlib import Path
import server

class Workflow(unittest.TestCase):
    def setUp(self):
        support.start_database(self)
        self.temp=tempfile.TemporaryDirectory(); self.old=server.DB; server.DB=Path(self.temp.name)/'test.db'; self.c=server.connect()
        self.p=server.mutate(self.c,'/api/projects',{'name':'测试项目','idea':'验证需求','budget':'1000'})['id']; self.c.commit()
    def tearDown(self):
        self.c.close(); server.DB=self.old; self.temp.cleanup(); support.stop_database(self)
    def test_dependencies_review_rework_persistence(self):
        tasks=server.snapshot(self.c)['tasks']; first,second=tasks[:2]
        def change(t,s): return server.mutate(self.c,'/api/tasks',{'project':self.p,'id':t['id'],'status':s,'note':'测试交接证据'})
        with self.assertRaises(ValueError): change(second,'进行中')
        with self.assertRaises(ValueError): change(first,'已完成')
        change(first,'待验收'); change(first,'已完成'); change(second,'进行中'); change(first,'需返工'); self.c.commit()
        self.c.close(); self.c=server.connect()
        rows=server.snapshot(self.c)['tasks']
        self.assertEqual(rows[0]['status'],'需返工'); self.assertEqual(rows[1]['status'],'等待依赖')
        self.assertGreaterEqual(len(server.snapshot(self.c)['events']),5)
    def test_cost_missing_and_exact(self):
        base={'project':self.p,'kind':'成本测算','content':'预测，不是实际'}
        result=server.mutate(self.c,'/api/records',base)
        self.assertIsNone(result['result']); self.assertIn('purchase',result['missing'])
        result=server.mutate(self.c,'/api/records',base|dict(revenue='100.10',discount='0.10',refund='0',pass_through='0',purchase='20',shipping='10',fees='3',tax='2',other='0',marketing='15'))
        self.assertEqual(result['result'],dict(net=10000,pre_marketing=6500,post_marketing=5000))
        for bad in ['NaN','Infinity','-1','0.001']:
            with self.assertRaises(ValueError): server.money(bad)
    def test_evidence_and_project_boundary(self):
        with self.assertRaises(ValueError): server.mutate(self.c,'/api/records',{'project':self.p,'kind':'机会证据','content':'未经核实的结论'})
        task=server.snapshot(self.c)['tasks'][0]
        other=server.mutate(self.c,'/api/projects',{'name':'另一项目','idea':'隔离检查','budget':'0'})['id']
        with self.assertRaises(ValueError): server.mutate(self.c,'/api/tasks',{'project':other,'id':task['id'],'status':'进行中','note':'不匹配项目'})

if __name__=='__main__': unittest.main()
