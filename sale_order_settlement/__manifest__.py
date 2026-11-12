# -*- coding: utf-8 -*-
{
    'name': 'Sale Order Settlement',
    'version': '20.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Settle a sales order end to end: delivery, invoice, payment, return and refund',
    'author': 'Custom Development',
    'depends': [
        'sale_management',   # نموذج sale.order وقائمة Sales
        'sale_stock',        # الشحنات: sale.order.picking_ids + action_view_delivery()
        'account',           # الفواتير والدفعات
    ],
    'data': [
        'security/ir.access.csv',
        'views/sale_order_views.xml',
        'views/payment_wizard_views.xml',
        'views/return_wizard_views.xml',
        'views/refund_wizard_views.xml',
        'views/add_product_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'sale_order_settlement/static/src/css/sale_order_settlement.css',
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
}
