# -*- coding: utf-8 -*-
import base64
import logging
import requests
import os
import re
import json

from odoo import api, fields, models, _, tools
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, storage
except ImportError:
    # Use WARNING level, so it's visible in standard logs
    _logger.warning("Firebase Admin SDK not installed (or not found in Odoo's python environment). Please run 'pip install firebase-admin requests'. Firebase integration will be disabled.")
    firebase_admin = None

_firebase_app = None

# Parameter keys reused from 'absensi_siswa' module
FIREBASE_CREDENTIALS_PARAM = 'firebase.service.account.key'
FIREBASE_BUCKET_NAME_PARAM = 'firebase.storage.bucket.name'

def get_firebase_app(env):
    """
    Initializes and returns the Firebase app instance using existing system parameters.
    This function ensures that Firebase is initialized only once.
    """
    global _firebase_app

    # Check if firebase is globally disabled before trying to initialize
    use_firebase_param = env['ir.config_parameter'].sudo().get_param('firebase_storage.use_firebase', 'false')
    if not tools.str2bool(use_firebase_param):
        # Don't log anything here, _should_use_firebase will do it.
        return None

    if firebase_admin and not isinstance(_firebase_app, firebase_admin.App):
        _logger.info("Firebase App: Not initialized. Attempting to initialize now.")
        IrConfigParameter = env['ir.config_parameter'].sudo()
        service_account_key_str = IrConfigParameter.get_param(FIREBASE_CREDENTIALS_PARAM)
        bucket_name = IrConfigParameter.get_param(FIREBASE_BUCKET_NAME_PARAM)

        _logger.info("Firebase Check: Bucket Name from params: '%s'", bucket_name)
        _logger.info("Firebase Check: Service account key is present: %s", bool(service_account_key_str))

        if not (service_account_key_str and bucket_name):
            _logger.warning("Firebase integration is DISABLED. Missing '%s' or '%s' in system parameters.",
                         FIREBASE_CREDENTIALS_PARAM, FIREBASE_BUCKET_NAME_PARAM)
            return None
        
        try:
            # Strip whitespace and control characters from the key string before loading
            service_account_info = json.loads(service_account_key_str.strip())
        except (json.JSONDecodeError, TypeError) as e:
            _logger.error("Firebase Initialization FAILED: Service account key is not valid JSON. Error: %s", e)
            return None

        try:
            cred = credentials.Certificate(service_account_info)
            # Ensure the bucket name doesn't contain gs:// or other prefixes
            clean_bucket_name = re.sub(r'^gs://', '', bucket_name).strip()
            _firebase_app = firebase_admin.initialize_app(cred, {
                'storageBucket': clean_bucket_name if '.appspot.com' in clean_bucket_name else f"{clean_bucket_name}.appspot.com"
            })
            _logger.info("Firebase Admin SDK initialized successfully via shared configuration.")
        except Exception as e:
            _logger.error("Firebase Initialization FAILED: Could not initialize app. Check credentials and bucket name. Error: %s", e)
            _firebase_app = None # Reset on failure
    
    return _firebase_app


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    # Add a field to store the Firebase URL for reference and easier access
    firebase_url = fields.Char('Firebase URL', readonly=True, copy=False)

    def _should_use_firebase(self):
        """ Check if Firebase storage should be used for this operation """
        if not firebase_admin:
            # This will be true if the import failed. The warning is already logged at import time.
            _logger.info("Firebase Upload SKIPPED: 'firebase-admin' library not installed.")
            return False

        # Use a system parameter to easily enable/disable the feature
        use_firebase_param = self.env['ir.config_parameter'].sudo().get_param('firebase_storage.use_firebase', 'false')
        is_enabled = tools.str2bool(use_firebase_param)

        if not is_enabled:
            _logger.info("Firebase Upload SKIPPED: System parameter 'firebase_storage.use_firebase' is not set to 'true'. Current value: '%s'", use_firebase_param)
            
        return is_enabled

    def _get_firebase_bucket(self):
        """ Get the firebase storage bucket instance """
        app = get_firebase_app(self.env)
        if not app:
            return None
        return storage.bucket(app=app)

    @api.model
    def _get_firebase_blob_name(self, attachment):
        """ Generate a unique blob name for the attachment """
        # A good practice is to include the database name to avoid conflicts in a multi-tenant environment
        db_name = self.env.cr.dbname
        # Use attachment ID for uniqueness. Use self.id for existing or self._origin.id for newly created records in memory
        att_id = attachment.id or (attachment._origin and attachment._origin.id)
        if not att_id:
            # For new records not yet in DB, we can't form a stable URL. Fallback is better.
            # This can happen in on-change contexts. The actual write will have an ID.
            return None
        return f'{db_name}/{att_id}/{attachment.name}'

    def _compute_datas(self):
        """
        Override to download data from Firebase if the attachment is stored there.
        """
        # Separate attachments that should be fetched from Firebase
        firebase_attachments = self.filtered(lambda a: a.type == 'url' and a.firebase_url and not a.db_datas)
        other_attachments = self - firebase_attachments

        for attachment in firebase_attachments:
            _logger.info("Firebase Download: Fetching '%s' from URL: %s", attachment.name, attachment.url)
            try:
                # Use a simple GET request on the public URL
                response = requests.get(attachment.url, timeout=30)
                response.raise_for_status()  # Raises HTTPError for bad responses (4xx or 5xx)
                
                # Write the downloaded content back to the record.
                # This will store it in db_datas for the current session.
                # We do this on a singleton record to avoid unintended side effects on other records.
                super(IrAttachment, attachment).write({
                    'datas': base64.b64encode(response.content)
                })
                
            except requests.exceptions.RequestException as e:
                _logger.error("Firebase Download FAILED for attachment '%s' (ID: %d) from URL %s: %s", 
                              attachment.name, attachment.id, attachment.url, e)
                # If download fails, we explicitly set datas to False
                super(IrAttachment, attachment).write({'datas': False})

        # Call super() for all other attachments that are not stored in Firebase
        if other_attachments:
            super(IrAttachment, other_attachments)._compute_datas()


    def _inverse_datas(self):
        """
        Override to upload the file to Firebase Storage instead of the default filestore.
        If the upload fails, it will raise an error to the user instead of falling back.
        """
        # If the feature is not enabled, use the default Odoo storage mechanism completely.
        if not self._should_use_firebase():
            _logger.debug("Firebase is disabled, using default Odoo storage.")
            return super()._inverse_datas()

        # If the feature IS enabled, we proceed with Firebase logic.
        _logger.info("Firebase storage is ENABLED. Processing uploads.")
        
        bucket = self._get_firebase_bucket()
        if not bucket:
            # If we can't get the bucket, the configuration is wrong. This is a hard stop.
            _logger.error("Firebase Upload HALTED: Bucket not available. Check system parameters and Firebase initialization logs.")
            raise UserError(_(
                "Could not connect to Firebase Storage. Please contact your IT administrator. (Reason: Bucket not available)"
            ))

        # Separate attachments with and without data.
        attachments_with_data = self.filtered('datas')
        attachments_without_data = self - attachments_with_data

        for attachment in attachments_with_data:
            file_content = base64.b64decode(attachment.datas)
            if not file_content:
                continue

            blob_name = self._get_firebase_blob_name(attachment)
            if not blob_name:
                _logger.error("Firebase Upload HALTED for '%s': Could not generate a stable blob name. The record might not be saved yet.", attachment.name)
                raise UserError(_(
                    "Could not determine a file name for the attachment '%s' before saving. Please save the record first and then attach the file.",
                    attachment.name
                ))

            blob = bucket.blob(blob_name)
            
            _logger.info("Firebase Upload: Attempting to upload '%s' as blob '%s'.", attachment.name, blob_name)
            try:
                blob.upload_from_string(
                    file_content,
                    content_type=attachment.mimetype or 'application/octet-stream'
                )

                try:
                    blob.make_public()
                except Exception as e:
                    _logger.error(
                        "Firebase Security FAILED: Could not make blob '%s' public. Error: %s",
                        blob_name, e
                    )
                    # Re-raise a specific exception to be caught by the outer block.
                    raise Exception(f"Could not set public permissions on the file. Check bucket 'Public access prevention' settings. Original error: {e}") from e

                # If upload is successful, update the record via SQL to avoid recursion.
                self.env.cr.execute("""
                    UPDATE ir_attachment
                    SET firebase_url = %s, "type" = 'url', url = %s, store_fname = NULL, db_datas = NULL, file_size = %s
                    WHERE id = %s
                """, (blob.public_url, blob.public_url, len(file_content), attachment.id))
                
                # Invalidate cache for the updated record.
                self.env.cache.invalidate([('ir.attachment', [attachment.id])])
                _logger.info("Firebase Upload SUCCESS for blob '%s'.", blob_name)

            except Exception as e:
                # If any part of the upload process fails, log the detailed error and show a user-friendly dialog.
                _logger.error("Firebase Upload FAILED for blob '%s'. Error: %s", blob_name, e, exc_info=True)
                raise UserError(_(
                    "The file '%s' could not be uploaded to Firebase Storage. Please contact your IT administrator.\n\nReason: %s",
                    attachment.name, e
                ))

        # After the loop, handle attachments that had no data to begin with.
        # We do NOT process any failed attachments here, as they would have already raised an error.
        if attachments_without_data:
            _logger.debug("Calling super()._inverse_datas for %d attachments that had no data.", len(attachments_without_data))
            super(IrAttachment, attachments_without_data)._inverse_datas()


    def unlink(self):
        """
        Override to delete the file from Firebase Storage upon record deletion.
        """
        attachments_in_firebase = self.filtered('firebase_url')
        
        if attachments_in_firebase:
            _logger.info("Firebase Delete: Found %d attachment(s) to delete from Firebase.", len(attachments_in_firebase))
            bucket = self._get_firebase_bucket()
            if bucket:
                for attachment in attachments_in_firebase:
                    # Reconstruct the blob name to delete it
                    blob_name = self._get_firebase_blob_name(attachment)
                    if not blob_name:
                        _logger.warning("Firebase Delete SKIPPED: Could not determine blob name for attachment ID %d.", attachment.id)
                        continue
                    try:
                        blob = bucket.blob(blob_name)
                        if blob.exists():
                            blob.delete()
                            _logger.info("Firebase Delete SUCCESS for blob '%s'.", blob_name)
                        else:
                            _logger.warning("Firebase Delete SKIPPED: Blob '%s' not found in Firebase (might have been deleted manually or never uploaded).", blob_name)
                    except Exception as e:
                        # Log the error but don't prevent the Odoo record from being deleted
                        _logger.error("Firebase Delete FAILED for blob '%s': %s", blob_name, e)
            else:
                _logger.warning("Firebase Delete SKIPPED: Firebase bucket not available (initialization failed).")

        return super().unlink()
