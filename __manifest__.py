{
    'name': 'Firebase Attachments Storage',
    'version': '17.0.1.0.0',
    'summary': 'Store Odoo attachments in Google Firebase Storage.',
    'author': 'Gemini',
    'license': 'LGPL-3',
    'category': 'Technical/Storage',
    'depends': ['base'],
    'data': [
        'views/ir_attachment_view.xml',
    ],
    'external_dependencies': {
        'python': [
            'firebase-admin',
            'requests',
        ],
    },
    'installable': True,
    'application': False,
}
