"""
Tests for reversibility coverage.
"""
import logging
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

# The three tests below watch `add_sequence`, which is the call
# `embed_identity_token` makes since #399; it appended through
# `add_sequence_item` before. Only the exception one goes red on that
# change -- the two `assert_not_called()` tests stay green while
# asserting the absence of a call the code can no longer make, which is
# a test that passes and pins nothing. All three moved together, and
# what the empty-token path *does* is unchanged: it still returns
# before touching the instance at all.

def test_embed_token_empty(rev_service, mock_instance):
    # Should do nothing and return None
    assert rev_service.embed_identity_token(mock_instance, None) is None
    assert rev_service.embed_identity_token(mock_instance, b"") is None
    mock_instance.add_sequence.assert_not_called()

def test_embed_token_exception(rev_service, mock_instance):
    token = b"valid_token"
    # Mock add_sequence to raise exception
    mock_instance.add_sequence.side_effect = Exception("Embed fail")

    with pytest.raises(Exception, match="Embed fail"):
        rev_service.embed_identity_token(mock_instance, token)


def test_a_failed_embed_names_a_message_less_exception(rev_service,
                                                       mock_instance, caplog):
    """The ERROR before the re-raise names the exception's type (#487).

    A bare `Exception()` has an empty `str()`, so `f"...: {e}"` logged
    `Failed to embed token: ` -- a line saying a step failed without
    saying how (#435's class). The re-raise carries the exception
    onward, which is why the mutation probe classed deleting this line
    as equivalent; the line is still what a reader of the log gets, and
    it now reads `Failed to embed token: Exception`.
    """
    mock_instance.add_sequence.side_effect = Exception()

    with caplog.at_level(logging.ERROR, logger="isocenter"):
        with pytest.raises(Exception):
            rev_service.embed_identity_token(mock_instance, b"valid_token")
    errors = [r.getMessage() for r in caplog.records
              if r.name == "isocenter" and r.levelno == logging.ERROR]
    assert errors == ["Failed to embed token: Exception"], caplog.text

def test_recover_no_sequence(rev_service, mock_instance):
    mock_instance.sequences = {}
    assert rev_service.recover_original_data(mock_instance) is None

def test_recover_empty_sequence(rev_service, mock_instance):
    seq_mock = MagicMock()
    seq_mock.items = []
    mock_instance.sequences = {ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ: seq_mock}
    assert rev_service.recover_original_data(mock_instance) is None

def test_recover_missing_content_item(rev_service, mock_instance, caplog):
    """An item with no Encrypted Content says so, not just "no token" (#439).

    This read returns None for every failure (the lock's re-lock check
    needs that tolerance; recovery raises through `recover_or_raise`
    since #539), so this WARNING is the only word that says the item is
    there and malformed rather than absent. Deleting it left the suite green until the record was
    asserted, not just the None.
    """
    # Sequence exists, has item, but item has no encrypted bytes
    item_mock = MagicMock()
    item_mock.attributes = {}
    seq_mock = MagicMock()
    seq_mock.items = [item_mock]
    mock_instance.sequences = {ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ: seq_mock}

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        assert rev_service.recover_original_data(mock_instance) is None
    warnings = [r for r in caplog.records
                if r.name == "isocenter" and r.levelno == logging.WARNING]
    assert len(warnings) == 1, caplog.text
    assert "(0400,0520)" in warnings[0].getMessage()

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


def test_a_recovery_that_cannot_decrypt_names_the_instance(caplog):
    """A token under the wrong key is an ERROR naming the instance (#439).

    `recover_original_data` returns None for an absent token, a malformed
    item and a failed decryption alike, and the one thing that tells the
    last apart -- and says *which* instance could not be recovered -- is
    the ERROR its `except` logs. Deleting it left the suite green: the
    test above mocks `decrypt` and asserts only the None.

    A real token and a real wrong key, no mock: Fernet's `InvalidToken`
    has an empty message, so until #487 the record's tail after the UID
    was blank -- `Failed to recover data from SOP_WRONG_KEY_439: ` --
    and a reader could not tell a wrong key from any other failure. The
    reason is spelled by `logger.describe_exception` now (#435's
    pattern), so the tail names the type, and this pins it.
    """
    def service():
        km = MagicMock()
        km.get_key.return_value = Fernet.generate_key()
        return ReversibilityService(km)

    locker, stranger = service(), service()
    inst = Instance("SOP_WRONG_KEY_439", "1.2.840.10008.5.1.4.1.1.2", 1)
    locker.embed_identity_token(
        inst, locker.generate_identity_token({"0010,0010": "Original^Name"}))
    assert locker.recover_original_data(inst) == {"0010,0010": "Original^Name"}

    with caplog.at_level(logging.WARNING, logger="isocenter"):
        assert stranger.recover_original_data(inst) is None
    errors = [r for r in caplog.records
              if r.name == "isocenter" and r.levelno == logging.ERROR]
    assert len(errors) == 1, caplog.text
    message = errors[0].getMessage()
    assert "SOP_WRONG_KEY_439" in message
    assert message.endswith("SOP_WRONG_KEY_439: InvalidToken"), message
