import json,unittest
from unittest.mock import patch
import server as s,engine,runtime,model_connection as m
import test_workflow

class Connection(unittest.TestCase):
    setUp=test_workflow.Workflow.setUp
    tearDown=test_workflow.Workflow.tearDown
    def save(self,owner='root',**values):
        with patch('net.public_url',side_effect=lambda u:u):
            result=m.mutate(self.c,'/api/model/save',{'base':'https://provider.example/v1','api_key':'TEST-private-token','model':'TEST-model','quote':'0',**values},owner)
        self.c.commit();return result
    def test_credentials_are_private_and_used_by_owned_projects(self):
        result=self.save();self.assertNotIn('TEST-private-token',json.dumps(result));self.assertEqual(engine.model_config(self.p,self.c),('https://provider.example/v1','TEST-private-token','TEST-model'))
        self.assertNotIn('TEST-private-token',json.dumps(s.snapshot(self.c)))
        self.assertNotIn('api_key',m.public(self.c,'root'));self.assertFalse(m.public(self.c,'other')['key_present'])
    def test_account_cannot_borrow_saved_connection(self):
        self.save();self.c.execute('INSERT INTO project_owners VALUES(?,?)',(self.p,'other'));self.c.commit()
        with patch.dict('os.environ',{'AI_API_KEY':'ROOT-secret','OPENAI_API_KEY':'ROOT-secret','AI_MODEL':'root'}):self.assertFalse(engine.model_status(self.p,self.c)['configured'])
        self.save('other',api_key='TEST-other',model='other-model');self.assertEqual(engine.model_config(self.p,self.c)[1],'TEST-other')
    def test_no_key_reuse_at_changed_destination(self):
        self.save()
        with self.assertRaisesRegex(ValueError,'新地址'):self.save(base='https://elsewhere.example/v1',api_key='')
        self.c.rollback()
        for address in ['http://provider.example/v1','https://user:pass@provider.example/v1','https://provider.example/console/keys','https://provider.example/v1/chat/completions']:
            with self.assertRaises(ValueError):self.save(base=address)
            self.c.rollback()
    def test_model_discovery_is_distinct_from_paid_conversation(self):
        self.save()
        with patch('net.json_request',return_value={'data':[{'id':'TEST-b'},{'id':'TEST-a'}]}) as request:
            result=m.mutate(self.c,'/api/model/list',{},'root')
            self.assertEqual(result['models'],['TEST-a','TEST-b']);self.assertTrue(request.call_args.args[0].endswith('/v1/models'))
        self.assertEqual(m.public(self.c,'root')['verified'],'')
    def test_dialogue_test_requires_grant_and_verifies_actual_json(self):
        self.save()
        with self.assertRaisesRegex(ValueError,'额度'):m.mutate(self.c,'/api/model/test',{'project':self.p},'root')
        self.c.rollback();s.mutate(self.c,'/api/grants',{'project':self.p,'calls':1});self.c.commit()
        response={'choices':[{'message':{'content':json.dumps({'summary':'TEST','deliverables':['TEST'],'evidence_ids':[],'unknowns':[],'next_steps':[]})}}]}
        with patch('net.json_request',return_value=response):result=m.mutate(self.c,'/api/model/test',{'project':self.p},'root')
        self.c.commit();self.assertIn('通过',result['status']);self.assertTrue(m.public(self.c,'root')['verified']);self.assertEqual(self.c.execute('SELECT remaining FROM grants WHERE project=?',(self.p,)).fetchone()[0],0)
