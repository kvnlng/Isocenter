import json
import base64
from typing import Dict, Any, Optional
from .entities import Instance
from .crypto import CryptoEngine, KeyManager
from .logger import get_logger


class ReversibilityService:
    """
    Handles the embedding and recovery of encrypted original data in DICOM files.

    Compliant with DICOM Part 15, E.1.2 "Re-identifier" logic via the
    Encrypted Attributes Sequence (0400,0500). Uses `CryptoEngine` for encryption.
    """

    # DICOM Standard Tags for Encrypted Attributes
    TAG_ENCRYPTED_ATTRS_SEQ = "0400,0500"
    TAG_ENCRYPTED_CONTENT = "0400,0510"
    TAG_TRANSFER_SYNTAX_UID = "0400,0520"

    # Transfer Syntax for the Encrypted Payload (Dataset)
    # We use Implicit VR Little Endian (Default) as a signal that the decrypted bytes
    # form a dataset-like structure (even though we wrap JSON, this is metadata).
    PAYLOAD_TRANSFER_SYNTAX = "1.2.840.10008.1.2"

    def __init__(self, key_manager: KeyManager):
        self.key_manager = key_manager
        self.engine = CryptoEngine(key_manager.get_key())
        self.logger = get_logger()

    def generate_identity_token(self, original_attributes: Dict[str, Any]) -> bytes:
        """
        Serializes and encrypts the attributes into a reusable token.

        Args:
            original_attributes (Dict[str, Any]): Dictionary of tag-value pairs to preserve.

        Returns:
            bytes: The encrypted JSON payload.
        """
        if not original_attributes:
            return b""

        json_str = json.dumps(original_attributes)
        data_bytes = json_str.encode('utf-8')
        return self.engine.encrypt(data_bytes)

    def embed_identity_token(self, instance: Instance, token: bytes):
        """
        Embeds a pre-calculated encrypted token into the instance.

        Wraps the token in an Encrypted Attributes Sequence item with the
        appropriate Transfer Syntax UID.

        Args:
            instance (Instance): The target instance.
            token (bytes): The encrypted payload.
        """
        if not token:
            return

        try:
            # Create Sequence Item
            from .entities import DicomItem

            item = DicomItem()
            item.set_attr(self.TAG_ENCRYPTED_CONTENT, token)
            item.set_attr(self.TAG_TRANSFER_SYNTAX_UID, self.PAYLOAD_TRANSFER_SYNTAX)

            # Embed into attributes dict via Sequence helper
            # TODO: Check if sequence already exists and has items.
            # Currently, this appends a new item. If called twice (e.g. on already anonymized data),
            # it will append a second (likely incorrect) token. Recovery uses the first item, so it's safe,
            # but we should probably clear existing items or warn.
            instance.add_sequence_item(self.TAG_ENCRYPTED_ATTRS_SEQ, item)

            # self.logger.debug(f"Embedded token into {instance.sop_instance_uid}.")

        except Exception as e:
            self.logger.error(f"Failed to embed token: {e}")
            raise

    def embed_original_data(self, instance: Instance, original_attributes: Dict[str, Any]):
        """
        Serializes, encrypts, and embeds the provided attributes into the instance.

        This is a higher-level wrapper for `generate_identity_token` + `embed_identity_token`.

        Args:
            instance (Instance): The instance to modify.
            original_attributes (Dict[str, Any]): attributes to encrypt and store.
        """
        if not original_attributes:
            return

        try:
            token = self.generate_identity_token(original_attributes)
            self.embed_identity_token(instance, token)
            self.logger.debug(
                f"Embedded {
                    len(token)} bytes of encrypted data into {
                    instance.sop_instance_uid}.")

        except Exception as e:
            self.logger.error(f"Failed to embed original data: {e}")
            raise

    def recover_original_data(self, instance: Instance) -> Optional[Dict[str, Any]]:
        """
        Extracts and decrypts the original attributes from the instance.

        Locates the Encrypted Attributes Sequence, decrypts the first item's
        Encrypted Content, and deserializes the JSON.

        Args:
            instance (Instance): The anonymized instance.

        Returns:
            Optional[Dict[str, Any]]: The recovered dictionary of original attributes, or None if failed/missing.
        """
        try:
            # 1. Check for Sequence
            if self.TAG_ENCRYPTED_ATTRS_SEQ not in instance.sequences:
                return None

            seq = instance.sequences[self.TAG_ENCRYPTED_ATTRS_SEQ]
            if not seq.items:
                return None

            # 2. Read First Item
            item = seq.items[0]
            encrypted_bytes = item.attributes.get(self.TAG_ENCRYPTED_CONTENT)

            if not encrypted_bytes:
                self.logger.warning("EncryptedContent (0400,0510) not found in sequence item.")
                return None

            # 3. Decrypt
            decrypted_bytes = self.engine.decrypt(encrypted_bytes)

            # 4. Deserialize
            json_str = decrypted_bytes.decode('utf-8')
            return json.loads(json_str)

        except Exception as e:
            self.logger.error(f"Failed to recover data from {instance.sop_instance_uid}: {e}")
            return None
