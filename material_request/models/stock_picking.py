# -*- coding: utf-8 -*-
from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    material_request_id = fields.Many2one(
        'material.request', string='Material Request',
        copy=False, index='btree_not_null',
        help="Material request this transfer belongs to (shipping leg from the "
             "source branch to transit, or receiving leg to the requesting "
             "branch).")

    def _create_backorder_picking(self):
        """``copy()`` drops ``copy=False`` fields: carry the material request
        link over to the backorder explicitly so partial shipments stay
        connected to their request."""
        backorder = super()._create_backorder_picking()
        if backorder.material_request_id != self.material_request_id:
            backorder.material_request_id = self.material_request_id
        return backorder

    def _action_done(self):
        res = super()._action_done()
        # Validation may complete a material request step: goods leaving the
        # source branch trigger the receiving leg, goods arriving complete
        # the request.  The request itself decides what happens.
        for picking in self:
            request = picking.material_request_id
            if request:
                request.sudo()._sync_after_picking_done(picking)
        return res

    def action_cancel(self):
        res = super().action_cancel()
        for picking in self:
            request = picking.material_request_id
            if request:
                request.sudo()._check_fully_received()
        return res
