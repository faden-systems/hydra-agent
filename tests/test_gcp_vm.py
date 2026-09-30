"""Offline GCP command-contract tests: gcloud is always a shell mock."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'setup/gcp-vm.sh'
PUBLIC_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestFixture hermes@imac'


class GCPVMTests(unittest.TestCase):
    def invoke(self, args=(), key: str | None = PUBLIC_KEY, source_only=False, cloud_status=0):
        self.assertTrue(SCRIPT.is_file(), 'GCP creation script not implemented')
        with tempfile.TemporaryDirectory(prefix='gcp test ') as tmp:
            home = Path(tmp)
            (home / '.ssh').mkdir()
            if key is not None:
                (home / '.ssh/id_ed25519.pub').write_text(key + '\n')
            # Exported function takes precedence over any real SDK on PATH.
            body = '''gcloud() { printf '%s\\0' "$@" >> "$HOME/calls"; return "$CLOUD_STATUS"; }
export -f gcloud
'''
            if source_only:
                body += 'source "$1"'
            else:
                body += 'bash "$@"'
            result = subprocess.run(
                ['bash', '-c', body, 'test', str(SCRIPT), *args],
                env={**os.environ, 'HOME': tmp, 'CLOUD_STATUS': str(cloud_status),
                     'CLOUDSDK_CORE_PROJECT': 'unapproved-default-project'},
                capture_output=True, text=True)
            calls = home / 'calls'
            argv = calls.read_bytes().decode().rstrip('\0').split('\0') if calls.exists() else []
            return result, argv

    def test_exact_create_command_with_explicit_project(self):
        result, argv = self.invoke(['example-project-123'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(argv, [
            'compute', 'instances', 'create', 'hydra-manager',
            '--project', 'example-project-123',
            '--zone', 'us-west1-b', '--machine-type', 'e2-standard-4',
            '--image-family', 'ubuntu-2404-lts-amd64',
            '--image-project', 'ubuntu-os-cloud', '--boot-disk-size', '100GB',
            '--boot-disk-type', 'pd-balanced',
            '--metadata', 'ssh-keys=hermes:' + PUBLIC_KEY,
            '--tags', 'hydra-manager'])

    def test_project_is_required_even_with_environment_default(self):
        result, argv = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Usage:', result.stderr)
        self.assertEqual(argv, [])

    def test_invalid_project_or_extra_arguments_fail_before_cloud(self):
        for args in [('',), ('--quiet',), ('bad project',), ('abc123', 'extra')]:
            with self.subTest(args=args):
                result, argv = self.invoke(args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(argv, [])

    def test_missing_public_key_fails_before_cloud(self):
        result, argv = self.invoke(['example-project-123'], key=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('public key', result.stderr)
        self.assertEqual(argv, [])

    def test_bad_key_or_metadata_injection_fails_before_cloud(self):
        for key in ['', 'PRIVATE KEY fixture', PUBLIC_KEY + '\n' + PUBLIC_KEY,
                    PUBLIC_KEY + ',startup-script=unwanted', PUBLIC_KEY + '\r']:
            with self.subTest(key=key):
                result, argv = self.invoke(['example-project-123'], key=key)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(argv, [])

    def test_cloud_failure_is_propagated_without_retry(self):
        result, argv = self.invoke(['example-project-123'], cloud_status=23)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(argv.count('create'), 1)

    def test_source_does_not_create_vm(self):
        result, argv = self.invoke(source_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(argv, [])


if __name__ == '__main__':
    unittest.main()
