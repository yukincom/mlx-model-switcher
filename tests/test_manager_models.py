import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mlx_activation_lab import manager_models as models, server_control as control


TEST_GGUF_ALIAS = "gguf:Qwen3.8-Flash-Next-125B"


class ModelManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / 'models.conf'
        self.config.write_text('# retained comment\ngemma=owner/gemma-4-26B-QAT-4bit\nother=owner/Qwen3.8-27B-8bit\n')
        for owner, name, value in ((control, 'MODELS_CONFIG', self.config),
                                   (models, 'SETTINGS', self.root / 'settings.json'),
                                   (models, 'PENDING', self.root / 'starting.json'),
                                   (models, 'GGUF_ROOT', self.root / 'gguf')):
            mocked = patch.object(owner, name, value)
            mocked.start(); self.addCleanup(mocked.stop)
        for owner, name, value in ((models, '_cache_repos', []),
                                  (control, 'status', {'running': False}),
                                  (control, '_listener_pid', None),
                                  (models, 'ensure_lab_idle', None)):
            mocked = patch.object(owner, name, return_value=value)
            mocked.start(); self.addCleanup(mocked.stop)
        models.set_default('gemma')

    def gguf(self):
        root = models.GGUF_ROOT
        root.mkdir()
        profiles = [
            {"target": "125b", "alias": TEST_GGUF_ALIAS, "model": "Qwen3.8-Flash-Next-125B-IQ3_XXS",
             "name": "Qwen3.8-Flash-Next-125B", "manifest": "models-manifest.json"},
            {"target": "gemma-gguf", "alias": "gguf:gemma-test", "model": "Gemma-4-26B-A4B-QAT-UD-Q4_K_XL",
             "name": "Gemma-4-26B-QAT-UD-Q4_K_XL", "manifest": "gemma4-qat-manifest.json"},
        ]
        (root / 'server.py').write_text('def gguf_profiles():\n    return ' + repr(profiles) + '\n')
        files = []
        for folder, name in [('target', 'one.gguf'), ('target', 'two.gguf'), ('mtp', 'mtp.gguf')]:
            path = root / 'models' / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
            files.append({'directory': folder, 'file': name, 'bytes': 7})
        (root / 'models-manifest.json').write_text(json.dumps({'files': files}))
        return root

    def add_gemma(self, root):
        path = root / 'models/gemma/gemma.gguf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'gemma-fixture')
        (root / 'gemma4-qat-manifest.json').write_text(json.dumps({'files': [
            {'directory': 'gemma', 'file': 'gemma.gguf', 'bytes': path.stat().st_size}]}))
        return path

    def external_gguf_cache(self, root):
        cache = self.root / 'cache' / root.name / 'models'
        cache.parent.mkdir(parents=True)
        (root / 'models').rename(cache)
        with (root / 'server.py').open('a') as stream:
            stream.write('\nfrom pathlib import Path\nMODELS_ROOT = Path(' + repr(str(cache)) + ')\n')
        return cache

    def test_external_gguf_cache_lists_size_and_previews_actual_location(self):
        root = self.gguf()
        self.add_gemma(root)
        cache = self.external_gguf_cache(root)
        self.assertEqual(models.gguf_models_root(), cache)
        self.assertEqual(models.find_model(TEST_GGUF_ALIAS)['size_bytes'], 21)
        preview = models.deletion_preview(TEST_GGUF_ALIAS, True)
        self.assertEqual(preview['location'], str(cache))
        self.assertEqual(preview['file_count'], 3)
        self.assertEqual(preview['bytes'], 21)
        self.assertTrue(all(path.is_relative_to(cache) for path in models.gguf_files(TEST_GGUF_ALIAS)))
        self.assertFalse((root / 'models').exists())

    def test_external_gguf_cache_deletion_preserves_other_profile_and_unlisted_file(self):
        for selected, kept in [('gguf:gemma-test', TEST_GGUF_ALIAS), (TEST_GGUF_ALIAS, 'gguf:gemma-test')]:
            with self.subTest(selected=selected), patch.object(models, 'GGUF_ROOT', self.root / selected.replace(':', '-')):
                root = self.gguf()
                self.add_gemma(root)
                cache = self.external_gguf_cache(root)
                unrelated = cache / 'unlisted.gguf'
                unrelated.write_bytes(b'keep')
                expected = models.gguf_files(kept)
                preview = models.deletion_preview(selected, True)
                models.delete_model(selected, True, preview['fingerprint'])
                self.assertTrue(all(path.is_file() for path in expected))
                self.assertEqual(unrelated.read_bytes(), b'keep')
                self.assertEqual(models.gguf_files(selected), [])

    def test_external_gguf_cache_rejects_symlinked_root(self):
        root = self.gguf()
        cache = self.external_gguf_cache(root)
        real_cache = cache.with_name('real-models')
        cache.rename(real_cache)
        cache.symlink_to(real_cache, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'リンク先'):
            models.deletion_preview(TEST_GGUF_ALIAS, True)

    def test_external_gguf_cache_rejects_relative_root(self):
        with patch.object(models, 'gguf_runtime', return_value=SimpleNamespace(MODELS_ROOT='relative/models')):
            with self.assertRaisesRegex(ValueError, '保存先'):
                models.gguf_models_root()

    def test_gemma_uses_its_own_profile_name_size_and_start_target(self):
        root = self.gguf()
        path = self.add_gemma(root)
        item = models.find_model('gguf:gemma-test')
        self.assertEqual(item['size_bytes'], path.stat().st_size)
        self.assertEqual(item['name'], 'Gemma-4-26B-QAT-UD-Q4_K_XL')
        from unittest.mock import Mock
        runtime = models.gguf_runtime()
        runtime.start_gguf_if_idle = Mock(return_value=456)
        with patch.object(models, 'gguf_runtime', return_value=runtime):
            self.assertEqual(control.start('gguf:gemma-test', self.root / 'log'), 456)
        runtime.start_gguf_if_idle.assert_called_once_with('gemma-gguf', thinking=False)

    def test_each_gguf_deletion_preserves_the_other_model(self):
        for selected, kept in [('gguf:gemma-test', TEST_GGUF_ALIAS), (TEST_GGUF_ALIAS, 'gguf:gemma-test')]:
            with self.subTest(selected=selected), patch.object(models, 'GGUF_ROOT', self.root / ('fixture-' + selected.replace(':', '-'))):
                root = self.gguf()
                self.add_gemma(root)
                expected = list(models.gguf_files(kept))
                self.assertTrue(expected)
                preview = models.deletion_preview(selected, True)
                models.delete_model(selected, True, preview['fingerprint'])
                self.assertTrue(all(path.is_file() for path in expected))
                self.assertIn(kept, [item['alias'] for item in models.models()])
                self.assertNotIn(selected, [item['alias'] for item in models.models()])
                models.restore_gguf()
                self.assertIn(selected, [item['alias'] for item in models.models()])

    def test_gemma_pending_and_active_status_keep_correct_model_identity(self):
        root = self.gguf()
        self.add_gemma(root)
        runtime = models.gguf_runtime()
        runtime.RECORD = self.root / 'runtime-record.json'
        runtime.RECORD.write_text(json.dumps({'pid': 123, 'target': 'gemma-gguf'}))
        runtime.is_our_llama = lambda current: current['pid'] == 123
        with patch.object(models, 'gguf_runtime', return_value=runtime), patch.object(control, '_command', return_value='llama-server -m gemma.gguf --port 8080'):
            pending = models.pending_gguf()
            self.assertTrue(pending['loading'])
            self.assertEqual(pending['active_model'], 'Gemma-4-26B-A4B-QAT-UD-Q4_K_XL')
            with patch.object(control, '_listener_pid', return_value=123):
                active = control._status()
            self.assertTrue(active['trusted'])
            self.assertEqual(active['active_model'], pending['active_model'])

    def test_gguf_record_without_target_is_not_assumed_to_be_a_model(self):
        self.gguf()
        runtime = models.gguf_runtime()
        runtime.RECORD = self.root / 'runtime-record.json'
        runtime.RECORD.write_text(json.dumps({'pid': 123}))
        runtime.is_our_llama = lambda current: True
        with patch.object(models, 'gguf_runtime', return_value=runtime):
            self.assertEqual(models.managed_gguf({'pid': 123, 'command': 'llama-server'}), '')

    def test_short_names_keep_variant_and_hide_build_suffixes(self):
        self.assertEqual(models.short_name('owner/gemma-4-26B-A4B-it-QAT-MLX-4bit'), 'Gemma-4-26B')
        self.assertEqual(models.short_name('owner/Huihui-gemma-4-26B-A4B-it-qat-q4_0'), 'Gemma-4-26B-Huihui')
        self.assertEqual(models.short_name('owner/Qwen3.8-27B-8bit'), 'Qwen3.8-27B')


    def test_rename_rejects_empty_multiline_and_long_names(self):
        for name in ['', 'a\nb', 'x' * 61]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                models.rename_model('other', name)

    def test_default_persists_and_does_not_start_model(self):
        with patch.object(control, 'start') as start:
            models.set_default('other')
            self.assertEqual(models.default_alias(), 'other')
            self.assertEqual(json.loads(models.SETTINGS.read_text())['default_alias'], 'other')
            start.assert_not_called()

    def test_unregister_preserves_weights_and_other_config_lines(self):
        weights = self.root / 'weights.safetensors'; weights.write_bytes(b'keep')
        before = models.deletion_preview('other')
        models.delete_model('other', False, before['fingerprint'])
        self.assertEqual(weights.read_bytes(), b'keep')
        self.assertIn('# retained comment', self.config.read_text())
        self.assertEqual([item['alias'] for item in control.aliases()], ['gemma'])

    def test_default_can_be_unregistered_while_another_model_runs(self):
        current = {'running': True, 'pid': 123, 'active_model': 'owner/Qwen3.8-27B-8bit'}
        with patch.object(control, 'status', return_value=current), patch.object(control, '_listener_pid', return_value=123), patch.object(control, 'stop') as stop:
            preview = models.deletion_preview('gemma')
            models.delete_model('gemma', False, preview['fingerprint'])
            self.assertEqual(models.default_alias(), 'other')
            self.assertNotIn('default_alias', models.settings())
            stop.assert_not_called()

    def test_active_model_and_its_alias_cannot_be_removed(self):
        self.config.write_text(self.config.read_text() + 'duplicate=owner/Qwen3.8-27B-8bit\n')
        for body in (False, True):
            for alias in ('other', 'duplicate'):
                with self.subTest(body=body, alias=alias), patch.object(control, 'status', return_value={'running': True, 'active_model': 'owner/Qwen3.8-27B-8bit'}):
                    with self.assertRaisesRegex(ValueError, '起動中'):
                        models.deletion_preview(alias, body)

    def test_changed_preview_is_rejected_before_deleting(self):
        root = self.gguf()
        preview = models.deletion_preview(TEST_GGUF_ALIAS, True)
        (root / 'models/target/one.gguf').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '確認後'):
            models.delete_model(TEST_GGUF_ALIAS, True, preview['fingerprint'])
        self.assertEqual(len(models.gguf_files(TEST_GGUF_ALIAS)), 3)

    def test_gguf_deletion_only_removes_manifest_weights_and_mtp(self):
        root = self.gguf()
        keep = root / 'models/target/unrelated.gguf'; keep.write_bytes(b'keep')
        preview = models.deletion_preview(TEST_GGUF_ALIAS, True)
        self.assertEqual(preview['bytes'], 21)
        self.assertEqual(preview['file_count'], 3)
        models.delete_model(TEST_GGUF_ALIAS, True, preview['fingerprint'])
        self.assertTrue(keep.is_file())
        self.assertTrue((root / 'server.py').is_file())
        self.assertEqual(models.gguf_files(TEST_GGUF_ALIAS), [])
        self.assertIn(TEST_GGUF_ALIAS, models.settings()['hidden_gguf_aliases'])

    def test_symlink_and_parent_traversal_are_rejected(self):
        root = self.gguf()
        outside = self.root / 'outside.gguf'; outside.write_bytes(b'outside')
        path = root / 'models/target/one.gguf'; path.unlink(); path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'リンク'):
            models.deletion_preview(TEST_GGUF_ALIAS, True)
        self.assertEqual(outside.read_bytes(), b'outside')
        path.unlink(); path.write_bytes(b'fixture')
        manifest = root / 'models-manifest.json'
        manifest.write_text(json.dumps({'files': [{'directory': '..', 'file': 'outside.gguf'}]}))
        with self.assertRaisesRegex(ValueError, '保存先'):
            models.deletion_preview(TEST_GGUF_ALIAS, True)

    def test_hf_deletion_reuses_existing_delete_function(self):
        blob = self.root / 'model.safetensors'; blob.write_bytes(b'fixture')
        revision = SimpleNamespace(commit_hash='a'*40, files=[SimpleNamespace(blob_path=blob)])
        repo = SimpleNamespace(repo_id='owner/Qwen3.8-27B-8bit', repo_path=self.root, size_on_disk=7, revisions=[revision])
        with patch.object(models, '_cache_repos', return_value=[repo]), patch.object(models, 'delete_cached_model') as delete:
            preview = models.deletion_preview('other', True)
            models.delete_model('other', True, preview['fingerprint'])
            delete.assert_called_once_with(repo.repo_id)

    def test_unknown_port_state_blocks_body_deletion(self):
        self.gguf()
        with patch.object(control, '_listener_pid', side_effect=RuntimeError('確認できない')):
            with self.assertRaisesRegex(RuntimeError, '確認できない'):
                models.deletion_preview(TEST_GGUF_ALIAS, True)

    def test_loading_record_blocks_another_start_without_listener(self):
        models.PENDING.write_text(json.dumps({'pid':123, 'started':'same start', 'alias':'other'}))
        with patch.object(models, 'process_started', return_value='same start'), patch.object(control, '_command', return_value='mlx_lm.server --port 8080'):
            pending = models.pending_mlx()
            self.assertTrue(pending['loading'])
            with patch.object(control, 'status', return_value=pending), patch.object(control.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ValueError, '稼働中'):
                    control.start('gemma', self.root / 'log')
                spawn.assert_not_called()
        with patch.object(models, 'process_started', return_value='reused pid'):
            self.assertIsNone(models.pending_mlx())

    def test_bulk_unregister_validates_entire_selection_before_mutation(self):
        before = self.config.read_text()
        with self.assertRaisesRegex(ValueError, '未登録'):
            models.deletion_preview_many(['other', 'missing'])
        self.assertEqual(self.config.read_text(), before)

    def test_bulk_unregister_success_and_stale_second_item_preflight(self):
        self.gguf()
        aliases = ['other', TEST_GGUF_ALIAS]
        preview = models.deletion_preview_many(aliases)
        models.rename_model(TEST_GGUF_ALIAS, 'renamed')
        with self.assertRaisesRegex(ValueError, '確認後'):
            models.delete_models(aliases, False, preview['fingerprint'])
        self.assertTrue(any(item['alias'] == 'other' for item in models.models()))
        preview = models.deletion_preview_many(aliases)
        models.delete_models(aliases, False, preview['fingerprint'])
        self.assertEqual([item['alias'] for item in models.models()], ['gemma'])
        self.assertEqual(len(models.gguf_files(TEST_GGUF_ALIAS)), 3)

    def test_bulk_body_deduplicates_shared_hf_weights(self):
        self.config.write_text(self.config.read_text() + 'duplicate=owner/Qwen3.8-27B-8bit\n')
        self.gguf()
        blob = self.root / 'weights'; blob.write_bytes(b'fixture')
        revision = SimpleNamespace(commit_hash='a'*40, files=[SimpleNamespace(blob_path=blob)])
        repo = SimpleNamespace(repo_id='owner/Qwen3.8-27B-8bit', repo_path=self.root, size_on_disk=7, revisions=[revision])
        aliases = ['other', 'duplicate', TEST_GGUF_ALIAS]
        with patch.object(models, '_cache_repos', return_value=[repo]), patch.object(models, 'delete_cached_model') as delete:
            preview = models.deletion_preview_many(aliases, True)
            self.assertEqual(len(preview['items']), 2)
            self.assertEqual(preview['bytes'], 28)
            models.delete_models(aliases, True, preview['fingerprint'])
            delete.assert_called_once_with(repo.repo_id)
        self.assertEqual([item['alias'] for item in models.models()], ['gemma'])
        self.assertEqual(models.gguf_files(TEST_GGUF_ALIAS), [])

    def test_bulk_delete_reports_partial_failure(self):
        self.gguf()
        aliases = ['other', TEST_GGUF_ALIAS]
        preview = models.deletion_preview_many(aliases)
        original = models._delete_item
        def fail_second(item, body):
            if item['alias'] == TEST_GGUF_ALIAS:
                raise OSError('fixture failure')
            return original(item, body)
        with patch.object(models, '_delete_item', side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, '完了: Qwen3.8-27B。未完了: Qwen3.8-Flash-Next-125B'):
                models.delete_models(aliases, False, preview['fingerprint'])
        self.assertTrue(any(item['alias'] == TEST_GGUF_ALIAS for item in models.models()))

    def cache_fixture(self, repo_id='owner/Qwen3.8-27B-8bit'):
        folder = self.root / ('models--' + repo_id.replace('/', '--'))
        blob = folder / 'blobs/weights'
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b'fixture')
        snapshot = folder / 'snapshots' / ('a' * 40)
        snapshot.mkdir(parents=True)
        (snapshot / 'weights.safetensors').symlink_to(blob)
        revision = SimpleNamespace(commit_hash='a'*40, snapshot_path=snapshot, files=[SimpleNamespace(blob_path=blob)])
        return SimpleNamespace(repo_id=repo_id, repo_path=folder, size_on_disk=7, revisions=[revision])

    def test_other_gguf_can_be_deleted_while_gguf_is_running_or_loading(self):
        for loading in (False, True):
            for body in (False, True):
                with self.subTest(loading=loading, body=body), patch.object(models, 'GGUF_ROOT', self.root / f'gguf-{loading}-{body}'):
                    root = self.gguf()
                    active_file = self.add_gemma(root)
                    models.restore_gguf()
                    models.set_default(TEST_GGUF_ALIAS)
                    current = {'running': True, 'loading': loading, 'pid': 123, 'format': 'GGUF', 'active_model': 'Gemma-4-26B-A4B-QAT-UD-Q4_K_XL'}
                    with patch.object(control, 'status', return_value=current), patch.object(control, '_listener_pid', return_value=None if loading else 123), patch.object(control, 'stop') as stop:
                        preview = models.deletion_preview(TEST_GGUF_ALIAS, body)
                        models.delete_model(TEST_GGUF_ALIAS, body, preview['fingerprint'])
                        stop.assert_not_called()
                    self.assertTrue(active_file.is_file())
                    self.assertNotIn(TEST_GGUF_ALIAS, [item['alias'] for item in models.models()])
                    self.assertNotEqual(models.default_alias(), TEST_GGUF_ALIAS)
                    self.assertEqual(bool(models.gguf_files(TEST_GGUF_ALIAS)), not body)

    def test_other_mlx_body_can_be_deleted_even_when_it_is_the_default(self):
        repo = self.cache_fixture()
        models.set_default('other')
        self.config.write_text(self.config.read_text() + 'duplicate=owner/Qwen3.8-27B-8bit\n')
        for loading in (False, True):
            current = {'running': True, 'loading': loading, 'active_model': 'owner/gemma-4-26B-QAT-4bit'}
            with patch.object(models, '_cache_repos', return_value=[repo]), patch.object(control, 'status', return_value=current), patch.object(models, 'delete_cached_model') as delete, patch.object(control, 'stop') as stop:
                preview = models.deletion_preview('other', True)
                if loading:
                    models.delete_model('other', True, preview['fingerprint'])
                    delete.assert_called_once_with(repo.repo_id)
                stop.assert_not_called()
        self.assertEqual([item['alias'] for item in models.models()], ['gemma'])
        self.assertEqual(models.default_alias(), 'gemma')

    def test_snapshot_paths_and_symlinked_aliases_match_the_active_cache(self):
        repo = self.cache_fixture()
        snapshot = repo.revisions[0].snapshot_path
        linked = self.root / 'linked-model'
        linked.symlink_to(snapshot, target_is_directory=True)
        self.config.write_text(self.config.read_text() + f'linked={linked}\n')
        for active in (str(snapshot), str(linked), repo.repo_id):
            for alias in ('other', 'linked'):
                for body in (False, True):
                    with self.subTest(active=active, alias=alias, body=body), patch.object(models, '_cache_repos', return_value=[repo]), patch.object(control, 'status', return_value={'running': True, 'active_model': active}):
                        with self.assertRaisesRegex(ValueError, '起動中'):
                            models.deletion_preview(alias, body)
        self.assertTrue((snapshot / 'weights.safetensors').is_file())

    def test_local_model_symlinks_protect_shared_cached_weights(self):
        repo = self.cache_fixture()
        active = self.root / 'local-running-model'
        active.mkdir()
        (active / 'model.safetensors').symlink_to(next(iter(repo.revisions[0].files)).blob_path)
        with patch.object(models, '_cache_repos', return_value=[repo]), patch.object(control, 'status', return_value={'running': True, 'active_model': str(active)}):
            models.deletion_preview('other', False)
            with self.assertRaisesRegex(ValueError, '共有'):
                models.deletion_preview('other', True)

    def test_shared_gguf_projector_or_draft_blocks_body_only(self):
        root = self.gguf()
        self.add_gemma(root)
        manifest = root / 'gemma4-qat-manifest.json'
        value = json.loads(manifest.read_text())
        value['files'].append({'directory': 'mtp', 'file': 'mtp.gguf', 'bytes': 7})
        manifest.write_text(json.dumps(value))
        current = {'running': True, 'format': 'GGUF', 'active_model': 'Gemma-4-26B-A4B-QAT-UD-Q4_K_XL'}
        with patch.object(control, 'status', return_value=current):
            models.deletion_preview(TEST_GGUF_ALIAS, False)
            with self.assertRaisesRegex(ValueError, '共有'):
                models.deletion_preview(TEST_GGUF_ALIAS, True)

    def test_external_gguf_main_and_projector_paths_are_protected(self):
        root = self.gguf()
        main = root / 'models/target/one.gguf'
        for body in (False, True):
            with patch.object(control, 'status', return_value={'running': True, 'format': 'GGUF', 'active_model': str(main)}):
                with self.assertRaisesRegex(ValueError, '起動中'):
                    models.deletion_preview(TEST_GGUF_ALIAS, body)
        external = self.root / 'external.gguf'
        external.write_bytes(b'fixture')
        current = {'running': True, 'format': 'GGUF', 'active_model': str(external),
                   'command': f'llama-server -m {external} --mmproj {root / "models/mtp/mtp.gguf"} --port 8080'}
        with patch.object(control, 'status', return_value=current):
            with self.assertRaisesRegex(ValueError, '共有'):
                models.deletion_preview(TEST_GGUF_ALIAS, True)

    def test_managed_gguf_profiles_with_the_same_main_file_are_both_protected(self):
        root = self.gguf()
        (root / 'gemma4-qat-manifest.json').write_text((root / 'models-manifest.json').read_text())
        for body in (False, True):
            current = {'running': True, 'active_model': 'Gemma-4-26B-A4B-QAT-UD-Q4_K_XL'}
            with patch.object(control, 'status', return_value=current):
                with self.assertRaisesRegex(ValueError, '起動中'):
                    models.deletion_preview(TEST_GGUF_ALIAS, body)

    def test_cached_main_does_not_hide_draft_model_references(self):
        main = self.cache_fixture('owner/gemma-4-26B-QAT-4bit')
        draft = self.cache_fixture()
        for reference in (draft.repo_id, str(draft.revisions[0].snapshot_path)):
            for flag in ('--draft-model', '--model-draft', '--adapter-path'):
                current = {'running': True, 'trusted': True, 'active_model': main.repo_id,
                           'command': f'python -m mlx_lm.server --model {main.repo_id} {flag} {reference} --port 8080'}
                with self.subTest(reference=reference, flag=flag), patch.object(models, '_cache_repos', return_value=[main, draft]), patch.object(control, 'status', return_value=current):
                    models.deletion_preview('other', False)
                    with self.assertRaisesRegex(ValueError, '共有'):
                        models.deletion_preview('other', True)

    def test_external_relative_model_paths_are_not_assumed_to_use_launcher_cwd(self):
        repo = self.cache_fixture()
        active = self.root / 'relative-model'
        active.mkdir()
        (active / 'model.safetensors').symlink_to(repo.revisions[0].files[0].blob_path)
        for reference in ('./relative-model', './missing-model', 'relative-model'):
            current = {'running': True, 'trusted': True, 'active_model': reference,
                       'command': f'python -m mlx_lm.server --model {reference} --port 8080'}
            with patch.object(control, 'SERVER_ROOT', self.root), patch.object(models, '_cache_repos', return_value=[repo]), patch.object(control, 'status', return_value=current):
                for body in (False, True):
                    with self.assertRaisesRegex(ValueError, '確認できない'):
                        models.deletion_preview('other', body)
        current = {'running': True, 'trusted': True, 'active_model': 'owner/gemma-4-26B-QAT-4bit',
                   'command': 'python -m mlx_lm.server --model owner/gemma-4-26B-QAT-4bit --draft-model ./missing-draft --port 8080'}
        with patch.object(control, 'status', return_value=current), patch.object(models, '_cache_repos', return_value=[repo]):
            with self.assertRaisesRegex(ValueError, '確認できない'):
                models.deletion_preview('other', True)

    def test_unknown_model_or_listener_change_blocks_both_operations(self):
        for body in (False, True):
            for current in ({'running': True, 'active_model': ''}, {'running': True, 'active_model': 'unidentified'}):
                with patch.object(control, 'status', return_value=current):
                    with self.assertRaisesRegex(ValueError, '確認できない'):
                        models.deletion_preview('other', body)
            with patch.object(control, 'status', return_value={'running': False}), patch.object(control, '_listener_pid', return_value=456):
                with self.assertRaisesRegex(ValueError, '変わった'):
                    models.deletion_preview('other', body)

    def test_model_becoming_active_after_confirmation_is_not_deleted(self):
        root = self.gguf()
        before = list(models.gguf_files(TEST_GGUF_ALIAS))
        preview = models.deletion_preview(TEST_GGUF_ALIAS, True)
        with patch.object(control, 'status', return_value={'running': True, 'active_model': 'Qwen3.8-Flash-Next-125B-IQ3_XXS'}):
            with self.assertRaisesRegex(ValueError, '起動中'):
                models.delete_model(TEST_GGUF_ALIAS, True, preview['fingerprint'])
        self.assertTrue(all(path.is_file() for path in before))

    def test_deleting_the_last_default_leaves_an_empty_selection(self):
        self.config.write_text('gemma=owner/gemma-4-26B-QAT-4bit\n')
        preview = models.deletion_preview('gemma')
        models.delete_model('gemma', False, preview['fingerprint'])
        self.assertEqual(models.models(), [])
        self.assertEqual(models.default_alias(), '')
        self.assertNotIn('default_alias', models.settings())


class ProcessGuardTests(unittest.TestCase):
    def test_long_and_short_model_flags_do_not_capture_other_options_or_python_module(self):
        self.assertEqual(control._active_model('python -m mlx_lm.server --model owner/gemma --host 127.0.0.1 --port 8080'), 'owner/gemma')
        self.assertEqual(control._active_model('/path/llama-server -m /path/モデル 名.gguf -md /mtp.gguf --port 8080'), '/path/モデル 名.gguf')
        self.assertEqual(control._active_model('python -m unrelated --port 8080'), '')

    def test_strict_listener_check_rejects_failures_and_ambiguous_pids(self):
        for output in [SimpleNamespace(returncode=1, stdout='', stderr='permission denied'),
                       SimpleNamespace(returncode=0, stdout='123\n456\n', stderr=''),
                       SimpleNamespace(returncode=2, stdout='', stderr='')]:
            with self.subTest(output=output), patch.object(control.subprocess, 'run', return_value=output):
                with self.assertRaises(RuntimeError): control._listener_pid(strict=True)
        with patch.object(control.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='')):
            self.assertIsNone(control._listener_pid(strict=True))

    def test_lab_and_detached_console_experiments_block_deletion(self):
        for command in ['python -m mlx_activation_lab.webapp', 'python -m mlx_activation_lab run --model owner/model',
                        '/env/bin/mlx-activation-lab run --model owner/model', 'python -m mlx_activation_lab app']:
            with self.subTest(command=command), patch.object(models.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=command)):
                with self.assertRaisesRegex(ValueError, '実験ラボ'): models.ensure_lab_idle()
        with patch.object(models.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='MLXServerManager --port 8766')):
            models.ensure_lab_idle()


if __name__ == '__main__':
    unittest.main()
