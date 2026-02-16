# -*- coding: utf-8 -*-
import base64
import logging
import requests
import os
import re

from odoo import api, fields, models, _, tools
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, storage
except ImportError:
    _logger.debug("Firebase Admin SDK not installed. Please run 'pip install firebase-admin requests'.")
    firebase_admin = None

_firebase_app = None

import json

# ... (imports)

_logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, storage
except ImportError:
    _logger.debug("Firebase Admin SDK not installed. Please run 'pip install firebase-admin requests'.")
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
        IrConfigParameter = env['ir.config_parameter'].sudo()
        service_account_key_str = IrConfigParameter.get_param(FIREBASE_CREDENTIALS_PARAM)
        bucket_name = IrConfigParameter.get_param(FIREBASE_BUCKET_NAME_PARAM)

        if not (service_account_key_str and bucket_name):
            _logger.info("Firebase integration is disabled. Missing '%s' or '%s' in system parameters.",
                         FIREBASE_CREDENTIALS_PARAM, FIREBASE_BUCKET_NAME_PARAM)
            return None
        
        try:
            service_account_info = json.loads(service_account_key_str)
        except (json.JSONDecodeError, TypeError):
            _logger.error("Firebase service account key format is invalid (must be a valid JSON string).")
            return None

        try:
            cred = credentials.Certificate(service_account_info)
            _firebase_app = firebase_admin.initialize_app(cred, {
                'storageBucket': bucket_name if '.appspot.com' in bucket_name else f"{bucket_name}.appspot.com"
            })
            _logger.info("Firebase Admin SDK initialized successfully via shared configuration.")
        except Exception as e:
            _logger.error("Failed to initialize Firebase Admin SDK: %s", e)
            _firebase_app = None # Reset on failure
    
    return _firebase_app


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    # Add a field to store the Firebase URL for reference and easier access
    firebase_url = fields.Char('Firebase URL', readonly=True, copy=False)

    def _should_use_firebase(self):
        """ Check if Firebase storage should be used for this operation """
        if not firebase_admin:
            return False
        # Use a system parameter to easily enable/disable the feature
        use_firebase = self.env['ir.config_parameter'].sudo().get_param('firebase_storage.use_firebase', 'false')
        return tools.str2bool(use_firebase)

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
            try:
                # Use a simple GET request on the public URL
                response = requests.get(attachment.url, timeout=20)
                response.raise_for_status()
                attachment.datas = base64.b64encode(response.content)
            except requests.exceptions.RequestException as e:
                _logger.error("Failed to fetch attachment from Firebase URL %s: %s", attachment.url, e)
                attachment.datas = False
        
        super(IrAttachment, self - firebase_attachments)._compute_datas()


    def _inverse_datas(self):
        """
        Override to upload the file to Firebase Storage instead of the default filestore.
        """
        # Filter attachments that have data and for which Firebase should be used
        attachments_to_upload = self.filtered(lambda a: a.datas and self._should_use_firebase())
        
        if not attachments_to_upload:
            return super()._inverse_datas()

        bucket = self._get_firebase_bucket()
        if not bucket:
            # Fallback to default Odoo behavior if Firebase is not configured
            _logger.warning("Firebase not initialized or configured. Falling back to default attachment storage.")
            return super()._inverse_datas()
            
        for attachment in attachments_to_upload:
            file_content = base64.b64decode(attachment.datas)
            blob_name = self._get_firebase_blob_name(attachment)
            blob = bucket.blob(blob_name)
            
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
                self.env.cache.invalidate([('ir.attachment', attachment.id)])

            except Exception as e:
                _logger.error("Failed to upload attachment '%s' to Firebase: %s", attachment.name, e, exc_info=True)
                raise UserError(_("Failed to upload file to Firebase: %s") % e)

        # Handle other attachments (not for Firebase or without data) using the standard mechanism
        super(IrAttachment, self - attachments_to_upload)._inverse_datas()

    def unlink(self):
        """
        Override to delete the file from Firebase Storage upon record deletion.
        """
        attachments_in_firebase = self.filtered(lambda a: a.firebase_url and self._should_use_firebase())
        
        if attachments_in_firebase:
            bucket = self._get_firebase_bucket()
            if bucket:
                for attachment in attachments_in_firebase:
                    # Reconstruct the blob name to delete it
                    blob_name = self._get_firebase_blob_name(attachment)
                    try:
                        blob = bucket.blob(blob_name)
                        if blob.exists():
                            blob.delete()
                            _logger.info("Deleted attachment '%s' (%s) from Firebase.", attachment.name, blob_name)
                    except Exception as e:
                        # Log the error but don't prevent the Odoo record from being deleted
                        _logger.error("Failed to delete attachment '%s' from Firebase: %s", blob_name, e)
            else:
                _logger.warning("Firebase not configured. Could not delete attachments from Firebase.")

        return super().unlink()
