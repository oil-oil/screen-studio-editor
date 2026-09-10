import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
class CredentialTests(unittest.TestCase):
    def test_actual_readers_consume_each_profile_without_legacy_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.json';config.write_text('{}')
            with patch.dict(os.environ,{'SCREEN_STUDIO_EDITOR_CONFIG':str(config)}):
                import gemini_edit_candidates as candidates
                import session_edit_planner as session
                import model_bakeoff as models
                import bailian_transcribe as asr
            args=SimpleNamespace(api_key='',api_key_file=None,dry_run=False,gemini_api_key='',bailian_api_key='')
            with patch.dict(os.environ,{'ZENMUX_API_KEY':'TEST_Z','DASHSCOPE_API_KEY':'TEST_D','GEMINI_API_KEY':'TEST_G'}),patch.object(Path,'read_text',side_effect=AssertionError('不应读取旧密钥')):
                self.assertEqual(candidates.api_key_from_args(args),'TEST_Z')
                self.assertEqual(session.api_key_from_args(args),'TEST_Z')
                self.assertEqual(candidates.bailian_api_key_from_args(args),'TEST_D')
                self.assertEqual(models.bailian_api_key(args),'TEST_D')
                self.assertEqual(asr._load_dashscope_api_key(),'TEST_D')
                self.assertEqual(candidates.gemini_api_key_from_args(args),'TEST_G')
                for fn,field in [(candidates.api_key_from_args,'api_key'),(candidates.gemini_api_key_from_args,'gemini_api_key'),(candidates.bailian_api_key_from_args,'bailian_api_key'),(session.api_key_from_args,'api_key'),(models.bailian_api_key,'bailian_api_key')]:
                    rejected=SimpleNamespace(**vars(args));setattr(rejected,field,'TEST_DO_NOT_ECHO')
                    with self.assertRaises(ValueError) as error:fn(rejected)
                    self.assertNotIn('TEST_DO_NOT_ECHO',str(error.exception))
