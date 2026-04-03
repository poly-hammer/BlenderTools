import os
import sys
import unittest


TESTS_FOLDER = os.path.dirname(__file__)
DEPENDENCIES_FOLDER = os.path.normpath(
    os.path.join(TESTS_FOLDER, os.path.pardir, 'src', 'addons', 'send2ue', 'dependencies')
)
if DEPENDENCIES_FOLDER not in sys.path:
    sys.path.append(DEPENDENCIES_FOLDER)

from rpc.factory import RPCFactory


def directory_exists(asset_path):
    """
    Checks to see if a directory exist in unreal.

    :param str asset_path: The path to the unreal asset.
    :return bool: Whether or not the asset exists.
    """
    return asset_path.startswith('/Game/')


class TestRPCFactory(unittest.TestCase):
    def setUp(self):
        self.factory = RPCFactory(rpc_client=object())

    def test_get_code_removes_docstring_and_compiles(self):
        code_lines = self.factory._get_code(directory_exists)
        generated_code = '\n'.join(code_lines)

        self.assertNotIn('Checks to see if a directory exist in unreal.', generated_code)
        self.assertNotIn(':param str asset_path: The path to the unreal asset.', generated_code)
        self.assertNotIn(':return bool: Whether or not the asset exists.', generated_code)

        for line in generated_code.splitlines():
            stripped_line = line.strip()
            if stripped_line:
                self.assertFalse(
                    all(character in {'"', "'"} for character in stripped_line),
                    f'Quote-only line leaked into generated payload: {line}'
                )

        compile(generated_code, '<rpc_generated_code>', 'exec')

    def test_remove_function_docstring_supports_windows_line_endings(self):
        code = (
            "def sample(asset_path):\r\n"
            "    \"\"\"\r\n"
            "    Checks to see if a directory exist in unreal.\r\n"
            "\r\n"
            "    :param str asset_path: The path to the unreal asset.\r\n"
            "    :return bool: Whether or not the asset exists.\r\n"
            "    \"\"\"\r\n"
            "    return asset_path.startswith('/Game/')\r\n"
        )

        stripped_code = RPCFactory._remove_function_docstring(code, 'sample')
        stripped_code = stripped_code.replace('\r\n', '\n').replace('\r', '\n')

        self.assertNotIn('Checks to see if a directory exist in unreal.', stripped_code)
        self.assertNotIn(':param str asset_path: The path to the unreal asset.', stripped_code)
        self.assertNotIn(':return bool: Whether or not the asset exists.', stripped_code)

        compile(stripped_code, '<rpc_docstring_removed>', 'exec')

