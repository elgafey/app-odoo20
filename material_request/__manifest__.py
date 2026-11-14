# -*- coding: utf-8 -*-
{
    'name': 'Material Request',
    'version': '20.0.1.0.0',
    'category': 'Supply Chain/Inventory',
    'summary': 'Branch transfer requests (through a transit location) and '
               'purchase requests (RFQ) between branches',
    'description': """
Material Request
================
Two kinds of requests between branches (internal stock locations):

* *Branch Transfer Request*: a branch asks another branch for goods.
  Approval creates an internal transfer (source branch -> transit).  When the
  source branch validates it, a second transfer (transit -> requesting branch)
  is created automatically and becomes ready for the requesting branch.
* *Purchase Request*: approval creates a draft request for quotation
  (purchase.order).  No stock transfer is involved.

Configuration
-------------
* ``branch_name`` on internal stock locations: how the branch is displayed.
* ``mr_allowed_location_ids`` on users: which branches' real transfers
  (sending or receiving) the user may see and process.
""",
    'author': 'Custom Development',
    'license': 'LGPL-3',
    'depends': ['stock', 'purchase', 'mail'],
    'data': [
        'security/material_request_security.xml',
        'security/ir.access.csv',
        'data/material_request_data.xml',
        'views/material_request_views.xml',
        'views/stock_location_views.xml',
    ],
    'installable': True,
    'application': True,
}
