"""Synthetic tests. No supplied document, account, provider, or desktop is used."""
import ast
import contextlib
import io
import itertools
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ATTEMPTS = []

def blocked(*args, **kwargs):
    ATTEMPTS.append('network_or_process')
    raise AssertionError('Offline tests forbid network and child processes')

class OfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = contextlib.ExitStack()
        for name in ['socket.getaddrinfo', 'socket.gethostbyname',
                     'socket.create_connection', 'socket.socket.connect',
                     'socket.socket.connect_ex', 'subprocess.Popen', 'os.system']:
            cls.stack.enter_context(patch(name, side_effect=blocked))
        cls.stack.enter_context(patch.dict(os.environ, {'ILVF_PIPELINE_MODE':'manual_agent'}))

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()
        if ATTEMPTS:
            raise AssertionError(f'Unexpected external attempts: {len(ATTEMPTS)}')

    def test_all_sources_compile_and_local_modules_resolve(self):
        modules = {'.'.join(p.relative_to(ROOT).with_suffix('').parts)
                   for p in ROOT.rglob('*.py')}
        for p in ROOT.rglob('*.py'):
            tree = ast.parse(p.read_bytes(), filename=str(p))
            compile(tree, str(p), 'exec')
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    if node.module.split('.')[0] in ('agents', 'integrations', 'scripts'):
                        self.assertTrue(node.module in modules or node.module+'.__init__' in modules,
                                        f'{p.name}:{node.lineno} missing {node.module}')

    def test_clean_text_keeps_structure(self):
        from integrations.text_cleaning import clean_extracted_text
        self.assertEqual(clean_extracted_text('  A\x00   B\nC\t D  '), 'A B\nC D\n')
        self.assertEqual(clean_extracted_text(None), '')

    def test_atomic_unicode_roundtrip_and_snapshot(self):
        from integrations.atomic_io import atomic_write_json, read_json
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'nested'/'fictional.json';snap=Path(t)/'snapshots'
            atomic_write_json(p, {'text':'مثال خیالی'})
            atomic_write_json(p, {'text':'updated'}, snapshot_dir=snap)
            self.assertEqual(read_json(p), {'text':'updated'})
            self.assertEqual(len(list(snap.glob('*.json'))), 1)
            self.assertFalse(list(p.parent.glob('*.tmp.*')))

    def test_cache_keys_order_independent_and_payload_sensitive(self):
        from integrations.llm_cache import compute_key, LLMCache
        self.assertEqual(compute_key({'a':1,'b':2}), compute_key({'b':2,'a':1}))
        self.assertNotEqual(compute_key({'a':1}), compute_key({'a':2}))
        with tempfile.TemporaryDirectory() as t:
            cache=LLMCache(t);key=compute_key({'fictional':True})
            self.assertIsNone(cache.get(key));cache.set(key,{'ok':True})
            self.assertEqual(cache.get(key),{'ok':True})

    def test_modes_default_manual_unknown_rejected_and_paid_gated(self):
        from integrations.pipeline_mode import resolve_mode, fail_closed_if_stage_needs_llm, PipelineModeError
        self.assertEqual(resolve_mode(), 'manual_agent')
        with self.assertRaises(PipelineModeError): resolve_mode('typo')
        for available,approved in [(False,False),(False,True),(True,False)]:
            with self.assertRaises(PipelineModeError):
                fail_closed_if_stage_needs_llm('content_generation','llm_paid',
                    llm_client_available=available,approved=approved)
        manual=fail_closed_if_stage_needs_llm('content_generation','manual_agent')
        self.assertTrue(manual['require_authored']);self.assertFalse(manual['allow_paid'])

    def test_manual_and_dry_llm_never_call_adapter(self):
        from integrations.llm_client import LLMClient
        for provider,mode in [('manual','manual'),('openai','dry_run')]:
            client=LLMClient({'provider':provider,'mode':mode,'cache_enabled':False},pipeline_mode='llm_dry_run')
            with patch.object(client,'_adapter_complete',side_effect=blocked):
                self.assertIsNone(client.generate_content(package_id='fictional',package_evidence={'pages':[]}))
                self.assertFalse(client.paid_allowed())

    def test_paid_llm_missing_key_refuses_without_network(self):
        from integrations.llm_client import LLMClient, LLMClientError
        with patch.dict(os.environ,{},clear=True):
            client=LLMClient({'provider':'openai','mode':'live','model':'fictional',
                             'max_cost_per_run':1,'cache_enabled':False},pipeline_mode='llm_paid',approved=True)
            with patch.object(client,'_adapter_complete',side_effect=blocked):
                with self.assertRaises(LLMClientError): client.generate_content(package_id='fictional')

    def test_environment_detection_does_not_probe_dns_by_default(self):
        from integrations.env_detector import detect_environment
        result=detect_environment(ROOT)
        self.assertFalse(result.has_proxy_403_signature)

    def test_synthetic_segmentation_exhaustive_small_sequences(self):
        from integrations.package_segmenter import PageView, walk_pages
        for n in range(1,7):
            for signals in itertools.product(['continue','start','context_only'],repeat=n):
                pages=[PageView(f'page_{i:03}',i,'text',[],'concept_explanation',sig,
                                'high',f'Fictional {i}','',False)
                       for i,sig in enumerate(signals,1)]
                result=walk_pages(pages,max_pages_per_package=2)
                actual=[p.page_number for pkg in result.packages for p in pkg.pages]
                expected=[i for i,sig in enumerate(signals,1) if sig!='context_only']
                self.assertEqual(actual,expected)
                self.assertTrue(all(len(pkg.pages)<=2 for pkg in result.packages))
                self.assertTrue(all([p.page_number for p in pkg.pages] ==
                                    list(range(pkg.pages[0].page_number,pkg.pages[-1].page_number+1))
                                    for pkg in result.packages))

    def test_original_authored_registries_empty_and_new_input_not_file_one(self):
        from agents.persian_content_agent import _CURATED_CONTENT
        from agents.visual_classification_reviewer import _VISUAL_REVIEWS,_FILE1_SOURCE_NAMES,_load_run_reviews
        self.assertFalse(_CURATED_CONTENT);self.assertFalse(_VISUAL_REVIEWS);self.assertFalse(_FILE1_SOURCE_NAMES)
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()):
            reviews,hints,low=_load_run_reviews(Path(t),{'source_file':'1.pdf'})
            self.assertEqual(reviews,{});self.assertEqual(low,[])

    def test_cli_help_imports_without_network_or_runtime_inputs(self):
        from scripts.run_pipeline import main
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as exc: main(['--help'])
        self.assertEqual(exc.exception.code,0)
        self.assertIn('convert',out.getvalue())

    def test_small_fictional_pdf_roundtrip(self):
        import fitz
        from integrations.pdf_extractor import extract_pdf, sha256_of_file
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'fictional.pdf'
            with fitz.open() as doc:
                doc.new_page().insert_text((72,72),'Fictional lesson: three colored cubes.')
                doc.save(p)
            with fitz.open(p) as doc:
                self.assertEqual(len(doc),1)
                self.assertIn('Fictional lesson',doc[0].get_text())
            progress=[]
            pages=extract_pdf(p,Path(t)/'pages',Path(t)/'text',dpi=72,
                              on_page=lambda i,n:progress.append((i,n)))
            self.assertEqual(progress,[(1,1)])
            self.assertEqual(pages[0]['extraction_status'],'ok')
            self.assertEqual(pages[0]['text_sha256'],sha256_of_file(pages[0]['text_path']))
            self.assertEqual(pages[0]['image_sha256'],sha256_of_file(pages[0]['image_path']))
            self.assertIn('Fictional lesson',Path(pages[0]['text_path']).read_text())

    def test_artifact_schemas_are_valid(self):
        from jsonschema.validators import validator_for
        for path in (ROOT/'schemas').glob('*.schema.json'):
            schema=json.loads(path.read_text(encoding='utf-8'))
            validator_for(schema).check_schema(schema)

if __name__ == '__main__': unittest.main()
