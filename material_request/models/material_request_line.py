# -*- coding: utf-8 -*-
from odoo import api, fields, models


class MaterialRequestLine(models.Model):
    _name = 'material.request.line'
    _description = 'Material Request Line'
    _order = 'request_id, id'

    request_id = fields.Many2one(
        'material.request', string='Request',
        required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one(
        'product.product', string='Product', required=True,
        index='btree_not_null')
    quantity = fields.Float(
        string='Quantity', digits='Product Unit', default=1.0, required=True)
    uom_id = fields.Many2one(
        'uom.uom', string='Unit',
        compute='_compute_uom_id', store=True, readonly=True,
        help="The product's Unit of Measure.")
    company_id = fields.Many2one(
        'res.company', related='request_id.company_id', store=True, readonly=True)

    @api.depends('product_id')
    def _compute_uom_id(self):
        for line in self:
            line.uom_id = line.product_id.uom_id
