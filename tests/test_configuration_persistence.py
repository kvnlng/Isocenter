
import pytest
import os
import yaml
import stat
from isocenter.configuration import IsocenterConfiguration
from isocenter.session import DicomSession

class TestConfigurationPersistence:

    @pytest.fixture
    def config_file(self, tmp_path):
        p = tmp_path / "test_config_persist.yaml"
        data = {
            "version": "2.0",
            "machines": [
                {"serial_number": "INIT001", "redaction_zones": []}
            ]
        }
        p.write_text(yaml.dump(data))
        return str(p)

    def test_save_on_add_rule(self, config_file, tmp_path):
        db_path = str(tmp_path / "isocenter_test.db")
        session = DicomSession(persistence_file=db_path)
        session.load_config(config_file)

        # Add a new rule
        session.configuration.add_rule("NEW002", "NewMan", "NewModel", [[0,10,0,10]])

        # Verify persistence
        with open(config_file, 'r') as f:
            data = yaml.safe_load(f)

        machines = data.get("machines", [])
        assert len(machines) == 2
        new_rule = next((m for m in machines if m["serial_number"] == "NEW002"), None)
        assert new_rule is not None
        assert new_rule["manufacturer"] == "NewMan"
        assert new_rule["redaction_zones"] == [[0,10,0,10]]

        session.close()

    def test_save_on_update_rule(self, config_file, tmp_path):
        db_path = str(tmp_path / "isocenter_test_update.db")
        session = DicomSession(persistence_file=db_path)
        session.load_config(config_file)

        # Update existing rule
        session.configuration.update_rule("INIT001", {"redaction_zones": [[50,60,50,60]]})

        # Verify persistence
        with open(config_file, 'r') as f:
            data = yaml.safe_load(f)

        machines = data.get("machines", [])
        rule = next((m for m in machines if m["serial_number"] == "INIT001"), None)
        assert rule["redaction_zones"] == [[50,60,50,60]]

        session.close()

    def test_save_on_delete_rule(self, config_file, tmp_path):
        db_path = str(tmp_path / "isocenter_test_del.db")
        session = DicomSession(persistence_file=db_path)
        session.load_config(config_file)

        # Delete rule
        session.configuration.delete_rule("INIT001")

        # Verify persistence
        with open(config_file, 'r') as f:
            data = yaml.safe_load(f)

        machines = data.get("machines", [])
        assert len(machines) == 0

        session.close()

    def test_save_on_phi_tag(self, config_file, tmp_path):
        db_path = str(tmp_path / "isocenter_test_phi.db")
        session = DicomSession(persistence_file=db_path)
        session.load_config(config_file)

        # Add PHI tag
        session.configuration.set_phi_tag("0010,0010", "REPLACE", "John Doe")

        # Verify persistence
        with open(config_file, 'r') as f:
            data = yaml.safe_load(f)

        phi = data.get("phi_tags", {})
        assert "0010,0010" in phi
        assert phi["0010,0010"]["action"] == "REPLACE"
        assert phi["0010,0010"]["replacement"] == "John Doe"

        session.close()

    def test_save_permission_error_handling(self, config_file, tmp_path, capsys):
        """
        Ensures that if the file is not writable, the application doesn't crash
        and prints a warning (as per our implementation).
        """
        db_path = str(tmp_path / "isocenter_test_perm.db")
        session = DicomSession(persistence_file=db_path)
        session.load_config(config_file)

        # Make config file read-only
        os.chmod(config_file, stat.S_IREAD)

        try:
            # Attempt modification
            session.configuration.add_rule("ERR001", "ErrMan", "ErrModel")

            # Capture output
            captured = capsys.readouterr()

            # Notes:
            # 1. We expect it NOT to crash (no exception raised out of add_rule)
            # 2. We expect a warning print.
            # Depending on how the test runner captures stdout vs internal buffering,
            # we might see the print.

            assert "WARNING: Failed to auto-save configuration" in captured.out

        finally:
            # Restore permissions for cleanup
            os.chmod(config_file, stat.S_IWUSR | stat.S_IREAD)

        session.close()
