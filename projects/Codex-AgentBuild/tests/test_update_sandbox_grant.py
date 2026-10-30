import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('sandbox_update',Path('scripts/update-sandbox-grant.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class UpdateSandboxGrantTests(unittest.TestCase):
    def run_script(self, inputs, tty=True):
        values={'environment':'sandbox','credentials':{'client_id':'fake-id','client_secret':'fake-secret','refresh_token':'fake-old'}}
        backend=Mock()
        state=[json.dumps(values)]
        backend.get_password.side_effect=lambda service,account:state[0]
        backend.set_password.side_effect=lambda service,account,value:state.__setitem__(0,value)
        out=io.StringIO()
        with patch.object(m,'_backend',return_value=backend), patch.object(m.sys,'argv',['script']), patch.object(m.sys.stdin,'isatty',return_value=tty), patch.object(m.sys.stderr,'isatty',return_value=tty), patch.object(m.getpass,'getpass',side_effect=inputs), patch('sys.stdout',out):
            result=m.main()
        return result,backend,json.loads(state[0]),out.getvalue()

    def test_updates_only_sandbox_refresh(self):
        result,backend,bundle,out=self.run_script(['fake-new','fake-new'])
        self.assertEqual(result,0)
        self.assertEqual(bundle['credentials'],{'client_id':'fake-id','client_secret':'fake-secret','refresh_token':'fake-new'})
        self.assertEqual(backend.set_password.call_args.args[:2],('com.codex-agentbuild.tastytrade.sandbox','oauth-client-v1'))
        for value in bundle['credentials'].values():self.assertNotIn(value,out)

    def test_invalid_input_never_writes(self):
        for inputs in (['fake-new','different'],[' fake-new',' fake-new'],['',''],['fake\nnew','fake\nnew']):
            with patch('sys.stderr',new_callable=io.StringIO):
                result,backend,_,_=self.run_script(inputs)
            self.assertEqual(result,1);backend.set_password.assert_not_called()

    def test_no_noninteractive_input(self):
        with patch('sys.stderr',new_callable=io.StringIO):
            result,backend,_,_=self.run_script([],tty=False)
        self.assertEqual(result,1);backend.set_password.assert_not_called()
