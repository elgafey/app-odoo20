# -*- coding: utf-8 -*-
from odoo import _, fields, models


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    material_request_id = fields.Many2one(
        'material.request', string='Material Request',
        copy=False, index='btree_not_null',
        help="Material request this draft RFQ was created from.")

    def write(self, vals):
        res = super().write(vals)
        if vals.get('state'):
            for order in self:
                request = order.material_request_id
                if not request or request.request_type != 'purchase':
                    continue
                if vals['state'] in ('purchase', 'done') and request.state == 'approved':
                    request.sudo().state = 'done'
                    request.sudo().message_post(body=_(
                        'Purchase order %s confirmed: the request is done.',
                        order.name))
        return res
