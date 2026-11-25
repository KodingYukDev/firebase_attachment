# Firebase Attachments Storage (Odoo 17)

Store Odoo \ir.attachment\ binary files in **Google Firebase Storage** instead of the local filestore or database.

## Features
- Seamless offloading of Odoo attachments to Firebase Storage
- Lightweight integration extending \ase\ (\ir.attachment\)
- Configurable via Odoo system settings and views

## Requirements
- Odoo 17.0
- Python packages:
  - \irebase-admin\
  - \equests\

## Installation
1. Install Python dependencies:
   \\\ash
   pip install firebase-admin requests
   \\\
2. Copy this module into your Odoo \ddons_path\.
3. Update the Apps list and install **Firebase Attachments Storage**.
4. Configure your Firebase credentials and bucket name in Odoo settings (do not commit credentials to version control).

## License
Licensed under the **LGPL-3.0** license. See [LICENSE](LICENSE) for details.