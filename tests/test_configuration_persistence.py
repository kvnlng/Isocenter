
import pytest
import os
import yaml
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
        # The write is this test's subject, and since #715 it is opt-in.
        session.configuration.auto_save = True

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
        session.configuration.auto_save = True

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
        session.configuration.auto_save = True

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
        session.configuration.auto_save = True

        # Add PHI tag
        session.configuration.set_phi_tag("0010,0010", "REPLACE", "John Doe")

        # Verify persistence
        with open(config_file, 'r') as f:
            data = yaml.safe_load(f)

        phi = data.get("phi_tags", {})
        assert "0010,0010" in phi
        assert phi["0010,0010"]["action"] == "REPLACE"
        # Stored as the rule's `value`, which REPLACE writes (#538); the
        # `replacement` key it was saved under until 0.9.8 was never read.
        assert phi["0010,0010"]["value"] == "John Doe"
        assert "replacement" not in phi["0010,0010"]

        session.close()

    def test_a_failed_auto_save_raises_and_undoes_the_change(self, config_file, tmp_path, capsys):
        """
        A write that fails raises its `OSError`, and the change is undone
        (#715). This test pinned the opposite until 1.0 -- no exception, a
        printed `WARNING: Failed to auto-save configuration`, and the rule
        kept in memory -- which is the swallow #715 removes.

        The path sits under a regular file rather than on a read-only one,
        so the write fails whatever the permissions, and as root.
        """
        db_path = str(tmp_path / "isocenter_test_perm.db")
        session = DicomSession(persistence_file=db_path)
        session.load_config(config_file)
        plain = tmp_path / "plain_file"
        plain.write_text("")
        session.configuration.config_path = str(plain / "c.yaml")
        session.configuration.auto_save = True
        capsys.readouterr()

        with pytest.raises(NotADirectoryError):
            session.configuration.add_rule("ERR001", "ErrMan", "ErrModel")

        assert "WARNING: Failed to auto-save configuration" not in capsys.readouterr().out
        assert session.configuration.get_rule("ERR001") is None

        session.close()
