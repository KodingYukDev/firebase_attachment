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
            service_account_info = json.loads(service_account_key_str)
        except (json.JSONDecodeError, TypeError) as e:
            _logger.error("Firebase Initialization FAILED: Service account key is not valid JSON. Error: %s", e)
            return None

        try:
            cred = credentials.Certificate(service_account_info)
            _firebase_app = firebase_admin.initialize_app(cred, {
                'storageBucket': bucket_name if '.appspot.com' in bucket_name else f"{bucket_name}.appspot.com"
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
            # This will be true if the import failed.
            return False
        # Use a system parameter to easily enable/disable the feature
        use_firebase = self.env['ir.config_parameter'].sudo().get_param('firebase_storage.use_firebase', 'false')
        is_enabled = tools.str2bool(use_firebase)
        if not is_enabled:
            _logger.info("Firebase Upload SKIPPED: 'firebase_storage.use_firebase' parameter is not 'true'.")
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
        # Use attachment ID for uniqueness
        return f'{db_name}/{attachment.id or self._origin.id or "new"}/{attachment.name}'

    def _compute_datas(self):
        """
        Override to download data from Firebase if the attachment is stored there.
        """
        firebase_attachments = self.filtered(lambda a: a.type == 'url' and a.firebase_url and not a.db_datas)
        
        for attachment in firebase_attachments:
            _logger.info("Firebase Download: Fetching attachment '%s' from URL.", attachment.name)
            try:
                # Use a simple GET request on the public URL
                response = requests.get(attachment.url, timeout=20)
                response.raise_for_status()
                attachment.datas = base64.b64encode(response.content)
            except requests.exceptions.RequestException as e:
                _logger.error("Firebase Download FAILED for attachment %s from URL %s: %s", attachment.name, attachment.url, e)
                attachment.datas = False
        
        super(IrAttachment, self - firebase_attachments)._compute_datas()


    def _inverse_datas(self):
        """
        Override to upload the file to Firebase Storage instead of the default filestore.
        """
        # Filter attachments that have data and for which Firebase should be used
        attachments_to_upload = self.filtered(lambda a: a.datas and self._should_use_firebase())
        
        if not attachments_to_upload:
            # This will include attachments without data, or those where _should_use_firebase is False
            return super()._inverse_datas()

        _logger.info("Firebase Upload: Found %d attachment(s) to upload.", len(attachments_to_upload))
        bucket = self._get_firebase_bucket()
        if not bucket:
            # Fallback to default Odoo behavior if Firebase is not configured
            _logger.warning("Firebase Upload FAILED: Bucket not available. Falling back to default Odoo storage.")
            # Note: This will cause ALL attachments in this batch to fall back to Odoo's storage.
            # If only a subset should fallback, the logic here would need to be more granular.
            # For now, it's a full fallback on initialization failure.
            return super(IrAttachment, self)._inverse_datas() 
            
        for attachment in attachments_to_upload:
            file_content = base64.b64decode(attachment.datas)
            blob_name = self._get_firebase_blob_name(attachment)
            blob = bucket.blob(blob_name)
            
            _logger.info("Firebase Upload: Attempting to upload '%s' as blob '%s'.", attachment.name, blob_name)
            try:
                # Upload the file content to Firebase
                blob.upload_from_string(
                    file_content,
                    content_type=attachment.mimetype or 'application/octet-stream'
                )

                # Make the blob publicly readable to get a public URL
                blob.make_public()
                
                # Use a direct SQL query to update the record and avoid triggering the ORM's inverse methods again
                # This is a critical step to prevent infinite recursion
                # This direct SQL update is to avoid triggering _inverse_datas again and again for the same attachment.
                self.env.cr.execute("""
                    UPDATE ir_attachment
                    SET
                        firebase_url = %s,
                        "type" = 'url',
                        url = %s,
                        store_fname = NULL,
                        db_datas = NULL,
                        file_size = %s
                    WHERE id = %s
                """, (blob.public_url, blob.public_url, len(file_content), attachment.id))
                
                # Invalidate the cache for this record to ensure Odoo reads the new values
                # This makes sure subsequent reads for this attachment get the updated values from the database.
                self.env.cache.invalidate([('ir.attachment', attachment.id)])
                _logger.info("Firebase Upload SUCCESS for blob '%s'. Public URL: %s", blob_name, blob.public_url)

            except Exception as e:
                _logger.error("Firebase Upload FAILED for blob '%s': %s", blob_name, e, exc_info=True)
                # If a specific attachment fails, we log it, but allow other attachments in the batch to proceed.
                # The current attachment will fallback to default Odoo storage in the super() call below.
                # IMPORTANT: This means if an individual upload fails, it will still be saved by Odoo locally.
                pass 

        # Handle other attachments (not for Firebase or without data) using the standard mechanism
        # This will also store attachments that failed individual Firebase upload attempts.
        super(IrAttachment, self - attachments_to_upload)._inverse_datas()

    def unlink(self):
        """
        Override to delete the file from Firebase Storage upon record deletion.
        """
        attachments_in_firebase = self.filtered(lambda a: a.firebase_url)
        
        if attachments_in_firebase:
            _logger.info("Firebase Delete: Found %d attachment(s) to delete from Firebase.", len(attachments_in_firebase))
            bucket = self._get_firebase_bucket()
            if bucket:
                for attachment in attachments_in_firebase:
                    # Reconstruct the blob name to delete it
                    # We use the same _get_firebase_blob_name logic to ensure consistency.
                    blob_name = self._get_firebase_blob_name(attachment)
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
