"""Declared source meshes retain hidden shared armatures through native export."""
import importlib.util,json,sys,types,unittest
from pathlib import Path

MODULE=Path(__file__).resolve().parents[1]/'src/addons/send2ue/core/export_selection.py'
class Obj(dict):
    __hash__=object.__hash__
    def __init__(self,name,kind,parent=None):
        super().__init__();self.name=name;self.type=kind;self.parent=parent
        self.users_collection=[];self.modifiers=[];self.hide_viewport=True;self.hide_render=True;self.hide_select=True;self.hidden=True
    def __bool__(self):return True
    def __eq__(self,o):return self is o
    def hide_get(self):return self.hidden
    def hide_set(self,v):self.hidden=v
class Members(list):
    def link(self,o):self.append(o);o.users_collection.append(collection)
    def unlink(self,o):self.remove(o);o.users_collection.remove(collection)
class RigDependencies(unittest.TestCase):
    def test_hidden_rig_activation_and_exact_cleanup(self):
        for initially_linked in (True,False):
            with self.subTest(initially_linked=initially_linked):
                global collection
                rig=Obj('Rig','ARMATURE');other=Obj('OtherRig','ARMATURE')
                unit=Obj('Unit','EMPTY',rig);mesh=Obj('Low','MESH',unit)
                mesh.modifiers=[types.SimpleNamespace(type='ARMATURE',object=rig)]
                unit['send2ue_export_source_names']=json.dumps(['Low'])
                collection=types.SimpleNamespace(objects=Members(),hide_viewport=True,hide_render=True)
                layer=types.SimpleNamespace(collection=collection,children=[],exclude=True,hide_viewport=True)
                for o in (unit,mesh,other):collection.objects.link(o)
                if initially_linked:collection.objects.link(rig)
                bpy=types.SimpleNamespace(data=types.SimpleNamespace(collections={'Export':collection},objects={o.name:o for o in (rig,other,unit,mesh)}),context=types.SimpleNamespace(window_manager={'send2ue_export_unit_names':json.dumps(['Unit'])},view_layer=types.SimpleNamespace(update=lambda:None,layer_collection=layer)))
                previous=sys.modules.get('bpy');sys.modules['bpy']=bpy
                try:
                    spec=importlib.util.spec_from_file_location('rig_rules',MODULE);rules=importlib.util.module_from_spec(spec);spec.loader.exec_module(rules)
                    rules.prepare()
                    self.assertIn(rig,collection.objects)
                    self.assertFalse(layer.exclude or layer.hide_viewport or collection.hide_viewport or collection.hide_render)
                    self.assertFalse(rig.hide_viewport or rig.hide_render or rig.hide_get() or rig.hide_select)
                    self.assertTrue(other.hide_viewport and other.hide_render and other.hide_get())
                    rules.cleanup()
                    self.assertTrue(rig.hide_viewport and rig.hide_render and rig.hide_get() and rig.hide_select)
                    self.assertEqual(rig in collection.objects,initially_linked)
                    self.assertTrue(layer.exclude and layer.hide_viewport and collection.hide_viewport and collection.hide_render)
                    self.assertIsNone(rules._state)
                finally:
                    if previous is None:sys.modules.pop('bpy',None)
                    else:sys.modules['bpy']=previous
if __name__=='__main__':unittest.main()
