import ast,importlib.util,json,unittest
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('cloth_result',ROOT/'src/addons/send2ue/core/cloth_receipt.py')
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)

class ClothReceiptTests(unittest.TestCase):
    def setUp(self):
        self.record=dict(render_asset_path='/Game/Hair/Back',sim_asset_path='/Game/Hair/Back_GuideSim',manifest_sha256='abc',simulation_enabled=True)
        self.receipt={**self.record,'verified':True,'cloth_asset_path':'/Game/Hair/CA_Back','binding_asset_path':'/Game/Hair/Binding'}
    def output(self,receipt):return 'unrelated log\n'+M.PREFIX+json.dumps(receipt)+'\n'
    def test_verified_matching_build(self):
        self.assertEqual(M.validate_import_response(self.output(self.receipt),self.record),self.receipt)
    def test_rejected_paint_transfer_is_an_operation_error(self):
        with self.assertRaisesRegex(RuntimeError,'rail orientation changed'):
            M.validate_import_response(self.output(dict(verified=False,detail='rail orientation changed')),self.record)
    def test_missing_or_ambiguous_receipt(self):
        for output in ('RPC succeeded',self.output(self.receipt)*2):
            with self.subTest(output=output),self.assertRaisesRegex(RuntimeError,'one verified'):
                M.validate_import_response(output,self.record)
    def test_foreign_output_wrong_hash_or_sim_mode(self):
        for key,value in [('render_asset_path','/Game/Hair/Other'),('manifest_sha256','old'),('simulation_enabled',False),('sim_asset_path','/Game/Hair/OtherSim')]:
            with self.subTest(key=key),self.assertRaises(RuntimeError):
                M.validate_import_response(self.output({**self.receipt,key:value}),self.record)
    def test_render_only_success_does_not_require_a_cloth_asset(self):
        record={**self.record,'simulation_enabled':False}
        result=dict(render_asset_path=record['render_asset_path'],manifest_sha256='abc',simulation_enabled=False,verified=True)
        self.assertEqual(M.validate_import_response(self.output(result),record),result)
    def test_extension_propagates_build_failure(self):
        tree=ast.parse((ROOT/'src/addons/send2ue/resources/extensions/hair_guide_cloth.py').read_text('utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='HairGuideClothExtension')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='post_import')
        logs=[]
        env={'hair_guide_cloth':SimpleNamespace(import_record=lambda *a:self.record,diagnostic=lambda *a:logs.append(a)),
             'cloth_receipt':M,'PIPELINE_FILE':'pipe.py','RECEIPT_PREFIX':M.PREFIX,
             '_run_cloth_commands':lambda commands:self.output(dict(verified=False,detail='paint rejected'))}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<extension>','exec'),env)
        with self.assertRaisesRegex(RuntimeError,'import failed.*paint rejected'):
            env['post_import'](SimpleNamespace(enabled=True),{'asset_path':self.record['render_asset_path']},None)
        self.assertEqual(len(logs),1)
    def test_disabled_extension_does_not_dispatch(self):
        tree=ast.parse((ROOT/'src/addons/send2ue/resources/extensions/hair_guide_cloth.py').read_text('utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='HairGuideClothExtension')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='post_import')
        env={};exec(compile(ast.Module(body=[method],type_ignores=[]),'<extension>','exec'),env)
        self.assertIsNone(env['post_import'](SimpleNamespace(enabled=False),{},None))

class OperatorResultTests(unittest.TestCase):
    def method(self,name,env):
        tree=ast.parse((ROOT/'src/addons/send2ue/operators.py').read_text('utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Send2Ue')
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<operator>','exec'),env)
        return env[name]
    def test_disconnected_invoke_and_execute_are_not_finished(self):
        for name,args in [('invoke',(None,None)),('execute',(None,))]:
            with self.subTest(name=name):
                failed=[]
                sender=SimpleNamespace(_begin_coordinated=lambda ctx:None,_fail_coordinated=failed.append)
                env={'utilities':SimpleNamespace(is_unreal_connected=lambda:False)}
                self.assertEqual(self.method(name,env)(sender,*args),{'CANCELLED'})
                self.assertEqual(len(failed),1)
    def test_export_validation_error_cleans_up_and_is_not_finished(self):
        import queue
        failed=[];reports=[]
        def reject(props):raise ValueError('invalid export')
        wm=SimpleNamespace(send2ue=SimpleNamespace(progress=0))
        context=SimpleNamespace(window_manager=wm)
        sender=SimpleNamespace(_begin_coordinated=lambda ctx:None,pre_operation=lambda:None,
                               execution_queue=queue.Queue(),escape_operation=lambda *a,**k:failed.append(k),
                               report=lambda *a:reports.append(a))
        env={'utilities':SimpleNamespace(is_unreal_connected=lambda:True),
             'bpy':SimpleNamespace(context=SimpleNamespace(scene=SimpleNamespace(send2ue=None),workspace=SimpleNamespace(status_text_set_internal=lambda text:None))),
             'os':SimpleNamespace(environ={}), 'export':SimpleNamespace(send2ue=reject)}
        self.assertEqual(self.method('invoke',env)(sender,context,None),{'CANCELLED'})
        self.assertEqual(len(failed),1);self.assertEqual(reports[0][0],{'ERROR'})

if __name__=='__main__':unittest.main()
