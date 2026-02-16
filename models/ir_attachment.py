# -*- coding: utf-8 -*-
import base64
import logging
import requests
import os
import re
import json
import uuid  # Digunakan untuk generate nama file unik

from odoo import api, fields, models, _, tools
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, storage
except ImportError:
    _logger.warning("Firebase Admin SDK tidak ditemukan. Jalankan 'pip install firebase-admin'.")
    firebase_admin = None

_firebase_app = None

# Parameter sistem
FIREBASE_CREDENTIALS_PARAM = 'firebase.service.account.key'
FIREBASE_BUCKET_NAME_PARAM = 'firebase.storage.bucket.name'
USE_FIREBASE_PARAM = 'firebase_storage.use_firebase'

def get_firebase_app(env):
    global _firebase_app
    
    icp = env['ir.config_parameter'].sudo()
    if not tools.str2bool(icp.get_param(USE_FIREBASE_PARAM, 'false')):
        return None

    if firebase_admin and not isinstance(_firebase_app, firebase_admin.App):
        key_str = icp.get_param(FIREBASE_CREDENTIALS_PARAM)
        bucket_name = icp.get_param(FIREBASE_BUCKET_NAME_PARAM)

        if not (key_str and bucket_name):
            return None
        
        try:
            service_account_info = json.loads(key_str.strip())
            cred = credentials.Certificate(service_account_info)
            clean_bucket = re.sub(r'^gs://', '', bucket_name).strip()
            
            _firebase_app = firebase_admin.initialize_app(cred, {
                'storageBucket': clean_bucket if '.appspot.com' in clean_bucket else f"{clean_bucket}.appspot.com"
            })
            _logger.info("Firebase Berhasil Diinisialisasi.")
        except Exception as e:
            _logger.error("Gagal Inisialisasi Firebase: %s", e)
            _firebase_app = None
    
    return _firebase_app

class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    firebase_url = fields.Char('Firebase Blob Path', readonly=True, copy=False)

    def _should_use_firebase(self):
        if not firebase_admin:
            return False
        val = self.env['ir.config_parameter'].sudo().get_param(USE_FIREBASE_PARAM, 'false')
        return tools.str2bool(val)

    def _get_firebase_bucket(self):
        app = get_firebase_app(self.env)
        return storage.bucket(app=app) if app else None

    def _get_firebase_blob_name(self, attachment):
        """ Menghasilkan path unik dengan UUID agar tidak perlu ID Odoo saat upload """
        db_name = self.env.cr.dbname
        # Mengambil ekstensi file asli
        extension = os.path.splitext(attachment.name)[1] if attachment.name else ''
        # Generate UUID random
        random_name = f"{uuid.uuid4()}{extension}"
        return f'{db_name}/uploads/{random_name}'

    def _inverse_datas(self):
        """ 
        Mencegat penyimpanan data agar langsung ke Firebase 
        dan mengosongkan kolom database Postgres.
        """
        if not self._should_use_firebase():
            return super()._inverse_datas()

        bucket = self._get_firebase_bucket()
        if not bucket:
            raise UserError(_("Firebase tidak terkonfigurasi dengan benar."))

        for attachment in self:
            if not attachment.datas:
                continue

            # Ambil data mentah
            file_content = base64.b64decode(attachment.datas)
            blob_name = self._get_firebase_blob_name(attachment)
            blob = bucket.blob(blob_name)

            try:
                # Upload ke Firebase Storage
                blob.upload_from_string(
                    file_content,
                    content_type=attachment.mimetype or 'application/octet-stream'
                )
                
                # Membuat file dapat diakses publik (Opsional, tergantung setting bucket)
                try:
                    blob.make_public()
                    public_url = blob.public_url
                except:
                    public_url = f"https://storage.googleapis.com/{bucket.name}/{blob_name}"

                # Update via SQL untuk bypass penyimpanan file di Postgres
                self.env.cr.execute("""
                    UPDATE ir_attachment
                    SET firebase_url = %s, 
                        url = %s, 
                        type = 'url', 
                        db_datas = NULL, 
                        store_fname = NULL,
                        file_size = %s
                    WHERE id = %s
                """, (blob_name, public_url, len(file_content), attachment.id))

                # Hapus dari cache agar Odoo membaca data URL yang baru
                attachment.invalidate_recordset(['datas', 'db_datas', 'store_fname', 'type', 'url'])
                _logger.info("Berhasil upload ke Firebase: %s", blob_name)

            except Exception as e:
                _logger.error("Upload Gagal: %s", e)
                raise UserError(_("Gagal mengunggah file ke Firebase: %s") % e)

    def _compute_datas(self):
        """ Mengambil data dari Firebase saat file dibuka di Odoo """
        firebase_rec = self.filtered(lambda a: a.type == 'url' and a.firebase_url)
        other_rec = self - firebase_rec

        for attachment in firebase_rec:
            try:
                # Ambil dari URL publik
                response = requests.get(attachment.url, timeout=20)
                if response.status_code == 200:
                    attachment.datas = base64.b64encode(response.content)
                else:
                    attachment.datas = False
            except Exception:
                attachment.datas = False

        if other_rec:
            super(IrAttachment, other_rec)._compute_datas()

    def unlink(self):
        """ Hapus file di Firebase saat record di Odoo dihapus """
        bucket = self._get_firebase_bucket()
        if bucket:
            for attachment in self.filtered('firebase_url'):
                try:
                    blob = bucket.blob(attachment.firebase_url)
                    if blob.exists():
                        blob.delete()
                except Exception as e:
                    _logger.error("Gagal hapus blob di Firebase: %s", e)
        return super().unlink()