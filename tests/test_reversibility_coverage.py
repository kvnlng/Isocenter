"""
Tests for reversibility coverage.
"""
from unittest.mock import MagicMock, patch
from cryptography.fernet import Fernet
import pytest

from isocenter.reversibility import ReversibilityService
from isocenter.entities import Instance

@pytest.fixture
def mock_key_manager():
    km = MagicMock()
    km.get_key.return_value = Fernet.generate_key()
    return km

@pytest.fixture
def rev_service(mock_key_manager):
    return ReversibilityService(mock_key_manager)

@pytest.fixture
def mock_instance():
    inst = MagicMock(spec=Instance)
    inst.sequences = {}
    inst.sop_instance_uid = "1.2.3"
    return inst

def test_generate_token_empty(rev_service):
    assert rev_service.generate_identity_token(None) == b""
    assert rev_service.generate_identity_token({}) == b""

def test_embed_token_empty(rev_service, mock_instance):
    # Should do nothing and return None
    assert rev_service.embed_identity_token(mock_instance, None) is None
    assert rev_service.embed_identity_token(mock_instance, b"") is None
    mock_instance.add_sequence_item.assert_not_called()

def test_embed_token_exception(rev_service, mock_instance):
    token = b"valid_token"
    # Mock add_sequence_item to raise exception
    mock_instance.add_sequence_item.side_effect = Exception("Embed fail")

    with pytest.raises(Exception, match="Embed fail"):
        rev_service.embed_identity_token(mock_instance, token)

def test_embed_original_data_empty(rev_service, mock_instance):
    rev_service.embed_original_data(mock_instance, None)
    rev_service.embed_original_data(mock_instance, {})
    mock_instance.add_sequence_item.assert_not_called()

def test_embed_original_data_exception(rev_service, mock_instance):
    # Mock generate_identity_token to fail or subsequent embed to fail
    with patch.object(rev_service, 'generate_identity_token', side_effect=Exception("Gen fail")):
        with pytest.raises(Exception, match="Gen fail"):
            rev_service.embed_original_data(mock_instance, {"PatientID": "123"})

def test_recover_no_sequence(rev_service, mock_instance):
    mock_instance.sequences = {}
    assert rev_service.recover_original_data(mock_instance) is None

def test_recover_empty_sequence(rev_service, mock_instance):
    seq_mock = MagicMock()
    seq_mock.items = []
    mock_instance.sequences = {ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ: seq_mock}
    assert rev_service.recover_original_data(mock_instance) is None

def test_recover_missing_content_item(rev_service, mock_instance):
    # Sequence exists, has item, but item has no encrypted bytes
    item_mock = MagicMock()
    item_mock.attributes = {}
    seq_mock = MagicMock()
    seq_mock.items = [item_mock]
    mock_instance.sequences = {ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ: seq_mock}

    assert rev_service.recover_original_data(mock_instance) is None

def test_recover_decryption_failure(rev_service, mock_instance):
    # Setup valid structure but mock decryption fail
    item_mock = MagicMock()
    item_mock.attributes = {ReversibilityService.TAG_ENCRYPTED_CONTENT: b"ciphertext"}
    seq_mock = MagicMock()
    seq_mock.items = [item_mock]
    mock_instance.sequences = {ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ: seq_mock}

    # Mock decrypt method
    with patch.object(rev_service.engine, 'decrypt', side_effect=Exception("Decrypt fail")):
        # helper logs error but returns None
        assert rev_service.recover_original_data(mock_instance) is None
