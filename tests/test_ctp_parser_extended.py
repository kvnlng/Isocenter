
import sys
import os
import io
import pytest
from unittest.mock import patch, mock_open
from isocenter.utils.ctp_parser import CTPParser
import isocenter.utils.ctp_parser as ctp_parser_module

# Sample CTP content
SAMPLE_SCRIPT = """
// Comment
{ Manufacturer.containsIgnoreCase("GE MEDICAL") * ManufacturerModelName.containsIgnoreCase("LightSpeed") }
(0, 0, 100, 100)
(10, 10, 50, 50)

{ Manufacturer.containsIgnoreCase("SIEMENS") }
(5, 5, 20, 20)
"""

def test_parse_valid_script():
    rules = CTPParser.parse_script(SAMPLE_SCRIPT)
    assert len(rules) == 2

    assert rules[0]['manufacturer'] == "GE MEDICAL"
    assert rules[0]['model_name'] == "LightSpeed"
    assert len(rules[0]['redaction_zones']) == 2
    assert rules[0]['redaction_zones'][0] == [0, 100, 0, 100] # y, y+h, x, x+w

    assert rules[1]['manufacturer'] == "SIEMENS"
    assert len(rules[1]['redaction_zones']) == 1

def test_parse_no_zones():
    script = "{ Manufacturer.containsIgnoreCase('Test') }"
    rules = CTPParser.parse_script(script)
    assert len(rules) == 0

def test_parse_partial_criteria():
    script = """
    { Manufacturer.containsIgnoreCase("Philips") }
    (0,0,10,10)
    """
    rules = CTPParser.parse_script(script)
    assert len(rules) == 1
    assert rules[0]['manufacturer'] == "Philips"
    assert rules[0]['model_name'] == "Unknown"

def test_main_execution_success(tmp_path):
    input_file = tmp_path / "input.script"
    output_file = tmp_path / "output.yaml"

    input_file.write_text(SAMPLE_SCRIPT)

    with patch.object(sys, 'argv', ["prog", str(input_file), str(output_file)]):
        # We need to import the module and execute the main block?
        # Since it's inside `if __name__ == "__main__":`, we can't easily import it to run it.
        # We can simulate it by running with subprocess, OR since we are inside a test running CTPParser methods directly is better.
        # But to cover lines 107-133 we need to use subprocess or execute the file.

        # Let's use subprocess for the main block coverage
        pass

def test_main_subprocess_execution(tmp_path):
    import subprocess
    input_file = tmp_path / "test.script"
    output_file = tmp_path / "test.yaml"
    input_file.write_text(SAMPLE_SCRIPT)

    # Get the python executable
    python_exe = sys.executable
    script_path = os.path.abspath(ctp_parser_module.__file__)

    result = subprocess.run([python_exe, script_path, str(input_file), str(output_file)], capture_output=True, text=True)
    assert result.returncode == 0
    assert "Successfully converted" in result.stdout
    assert output_file.exists()

def test_main_subprocess_missing_args():
    import subprocess
    python_exe = sys.executable
    script_path = os.path.abspath(ctp_parser_module.__file__)

    result = subprocess.run([python_exe, script_path], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Usage:" in result.stdout

def test_main_subprocess_file_not_found():
    import subprocess
    python_exe = sys.executable
    script_path = os.path.abspath(ctp_parser_module.__file__)

    result = subprocess.run([python_exe, script_path, "nonexistent", "out.yaml"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Error: Input file" in result.stdout
