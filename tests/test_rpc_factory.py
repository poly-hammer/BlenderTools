"""RPC source-generation regressions; these tests need no Blender or Unreal."""
import os
import sys
import unittest

DEPENDENCIES = os.path.join(os.path.dirname(__file__), '..', 'src', 'addons',
                            'send2ue', 'dependencies')
sys.path.insert(0, os.path.abspath(DEPENDENCIES))
from rpc.factory import RPCFactory


def directory_exists(asset_path):
    """
    Checks to see if a directory exist in unreal.

    :param str asset_path: The path to the unreal asset.
    """
    return asset_path.startswith('/Game/')


def same_string_in_body():
    """Preserve this string."""
    return 'Preserve this string.'


def triple_quoted_body_string():
    """Remove only the function docstring."""
    message = """An executable body value.
    It contains if and must remain a string.
    """
    return message


def only_docstring():
    """A valid function with no operation."""


class TestRPCFactory(unittest.TestCase):
    def generated_function(self, function):
        source = '\n'.join(RPCFactory(rpc_client=object())._get_code(function))
        scope = {}
        exec(compile(source, '<remote-call>', 'exec'), scope)
        return source, scope[function.__name__]

    def test_indented_docstring_does_not_leak_into_remote_code(self):
        source, remote = self.generated_function(directory_exists)
        self.assertNotIn('Checks to see if a directory exist in unreal.', source)
        self.assertTrue(remote('/Game/Test'))
        self.assertFalse(remote('/Other/Test'))

    def test_matching_body_string_is_preserved(self):
        _, remote = self.generated_function(same_string_in_body)
        self.assertEqual(remote(), 'Preserve this string.')

    def test_triple_quoted_runtime_string_is_preserved(self):
        _, remote = self.generated_function(triple_quoted_body_string)
        self.assertEqual(remote(), triple_quoted_body_string())

    def test_docstring_only_function_remains_valid(self):
        _, remote = self.generated_function(only_docstring)
        self.assertIsNone(remote())


if __name__ == '__main__':
    unittest.main(argv=[__file__])
