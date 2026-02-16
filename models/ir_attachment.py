# -*- coding: utf-8 -*-
import base64
import logging
import requests
import os
import re
import json
import uuid

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
    """
    Inisialisasi Firebase App dengan pengecekan format bucket agar tidak 404.
    """
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
            
            # Membersihkan nama bucket dari prefix gs://
            clean_bucket = re.sub(r'^gs://', '', bucket_name).strip()
            
            # Logika cerdas: hanya tambah .appspot.com jika tidak ada titik sama sekali (Project ID saja)
            if '.' not in clean_bucket:
                storage_bucket = f"{clean_bucket}.appspot.com"
            else:
                storage_bucket = clean_bucket
            
            # Cek jika app sudah ada
            try:
                _firebase_app = firebase_admin.get_app()
                _logger.info("Firebase App sudah ada, menggunakan app yang ada dengan bucket: %s", storage_bucket)
            except ValueError:
                # App belum ada, inisialisasi baru
                _firebase_app = firebase_admin.initialize_app(cred, {
                    'storageBucket': storage_bucket
                })
                _logger.info("Firebase Berhasil Diinisialisasi dengan bucket: %s", storage_bucket)
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
        extension = os.path.splitext(attachment.name)[1] if attachment.name else ''
        random_name = f"{uuid.uuid4()}{extension}"
        return f'{db_name}/uploads/{random_name}'

    @api.model_create_multi
    def create(self, vals_list):
        """ Memastikan file langsung diupload saat record dibuat (misal dari chat) """
        attachments = super(IrAttachment, self).create(vals_list)
        if self._should_use_firebase():
            for attachment in attachments:
                if attachment.datas:
                    attachment._inverse_datas()
        return attachments

    def _inverse_datas(self):
        """ Logika untuk bypass Postgres dan simpan ke Firebase Storage """
        if not self._should_use_firebase():
            return super()._inverse_datas()

        bucket = self._get_firebase_bucket()
        if not bucket:
            _logger.error("Firebase Bucket tidak tersedia. Pastikan konfigurasi benar.")
            return super()._inverse_datas()

        for attachment in self:
            if not attachment.datas:
                continue

            # Decode data mentah Odoo
            file_content = base64.b64decode(attachment.datas)
            blob_name = self._get_firebase_blob_name(attachment)
            blob = bucket.blob(blob_name)

            try:
                # Proses upload ke Firebase
                blob.upload_from_string(
                    file_content,
                    content_type=attachment.mimetype or 'application/octet-stream'
                )
                
                # Mengatur visibilitas file
                try:
                    blob.make_public()
                    public_url = blob.public_url
                except Exception:
                    # Fallback URL jika make_public gagal (biasanya karena policy bucket)
                    public_url = f"https://storage.googleapis.com/{bucket.name}/{blob_name}"

                # Update via SQL untuk menghapus jejak file di database Postgres
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

                # Hapus cache agar Odoo membaca data dari URL
                attachment.invalidate_recordset(['datas', 'db_datas', 'store_fname', 'type', 'url'])
                _logger.info("Firebase Upload SUCCESS: %s (ID: %s)", blob_name, attachment.id)

            except Exception as e:
                _logger.error("Firebase Upload FAILED for %s: %s", attachment.name, e)
                raise UserError(_("Gagal mengunggah ke Firebase: %s") % e)

    def _compute_datas(self):
        """ Mengambil kembali data dari Firebase saat file dipanggil di UI Odoo """
        firebase_rec = self.filtered(lambda a: a.type == 'url' and a.firebase_url)
        other_rec = self - firebase_rec

        for attachment in firebase_rec:
            try:
                # Mengunduh data via URL publik
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
        """ Menghapus file fisik di Firebase saat record Odoo dihapus """
        bucket = self._get_firebase_bucket()
        if bucket:
            for attachment in self.filtered('firebase_url'):
                try:
                    blob = bucket.blob(attachment.firebase_url)
                    if blob.exists():
                        blob.delete()
                        _logger.info("Firebase Delete SUCCESS: %s", attachment.firebase_url)
                except Exception as e:
                    _logger.error("Firebase Delete FAILED: %s", e)
        return super().unlink()