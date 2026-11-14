# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare

REQUEST_TYPE_SELECTION = [
    ('transfer', 'Branch Transfer Request'),
    ('purchase', 'Purchase Request'),
]

STATE_SELECTION = [
    ('draft', 'Draft'),
    ('submitted', 'Submitted'),
    ('approved', 'Approved'),
    ('in_transit', 'In Transit'),
    ('done', 'Done'),
    ('cancelled', 'Cancelled'),
]


class MaterialRequest(models.Model):
    _name = 'material.request'
    _description = 'Material Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _rec_name = 'name'

    name = fields.Char(
        string='Reference', default='New', copy=False, readonly=True, index=True)
    request_type = fields.Selection(
        REQUEST_TYPE_SELECTION, string='Request Type',
        default='transfer', required=True, index=True, tracking=True)
    state = fields.Selection(
        STATE_SELECTION, string='Status',
        default='draft', required=True, index=True, tracking=True, copy=False)
    requester_id = fields.Many2one(
        'res.users', string='Requested By',
        default=lambda self: self.env.user, copy=False, tracking=True, index=True)
    date = fields.Date(
        string='Request Date', default=fields.Date.context_today,
        required=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company, index=True)
    location_src_id = fields.Many2one(
        'stock.location', string='Requested From (Branch)',
        domain=[('usage', '=', 'internal')], check_company=True, tracking=True,
        help="Branch (internal location) the goods are requested from.  Only "
             "used by Branch Transfer Requests.")
    location_dest_id = fields.Many2one(
        'stock.location', string='Requested For (Branch)',
        domain=[('usage', '=', 'internal')], check_company=True, tracking=True,
        help="Branch (internal location) asking for the goods: the receiving "
             "branch of a transfer, or the branch the purchase is delivered to.")
    partner_id = fields.Many2one(
        'res.partner', string='Vendor', tracking=True,
        help="Vendor to buy from.  Required when a Purchase Request is "
             "approved because a purchase order needs a partner.")
    line_ids = fields.One2many(
        'material.request.line', 'request_id', string='Products', copy=True)
    product_ids = fields.Many2many(
        'product.product', string='Requested Products', compute='_compute_product_ids',
        help="Products of the request lines, for search and display.")
    notes = fields.Text(string='Notes')

    picking_out_id = fields.Many2one(
        'stock.picking', string='Shipping Transfer', copy=False, readonly=True,
        help="First leg: from the source branch to the transit location.")
    picking_in_id = fields.Many2one(
        'stock.picking', string='Receiving Transfer', copy=False, readonly=True,
        help="Latest leg created: from the transit location to the requesting "
             "branch.  Partial shipments create one receiving transfer per leg.")
    picking_ids = fields.One2many(
        'stock.picking', 'material_request_id', string='Transfers')
    picking_count = fields.Integer(
        string='Transfers Count', compute='_compute_picking_count')
    purchase_order_id = fields.Many2one(
        'purchase.order', string='Purchase Order', copy=False, readonly=True,
        help="Draft RFQ created when a Purchase Request is approved.")

    qty_requested = fields.Float(
        string='Requested Quantity', compute='_compute_quantities',
        digits='Product Unit')
    qty_shipped = fields.Float(
        string='Shipped Quantity', compute='_compute_quantities',
        digits='Product Unit')
    qty_received = fields.Float(
        string='Received Quantity', compute='_compute_quantities',
        digits='Product Unit')

    _unique_reference = models.Constraint(
        'UNIQUE(name)',
        'The material request reference must be unique!',
    )

    # ------------------------------------------------------------------
    # Creates
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('material.request') or 'New'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    def _all_pickings(self):
        """Every transfer linked to the request, seen through sudo: branch
        users only see the legs their access rules allow, but the request
        logic and counters must reflect reality."""
        self.ensure_one()
        return self.sudo().picking_ids

    def _compute_picking_count(self):
        for request in self:
            request.picking_count = len(request._all_pickings())

    @api.depends('line_ids.product_id')
    def _compute_product_ids(self):
        for request in self:
            request.product_ids = request.line_ids.mapped('product_id')

    @api.depends(
        'request_type', 'line_ids.quantity',
        'picking_ids.state', 'picking_ids.location_id', 'picking_ids.location_dest_id',
        'picking_ids.move_ids.state', 'picking_ids.move_ids.quantity',
    )
    def _compute_quantities(self):
        """Quantities are expressed in the products' UoM, which is what the
        request lines and the transfers' moves both use."""
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        for request in self:
            if request.request_type != 'transfer':
                request.qty_requested = 0.0
                request.qty_shipped = 0.0
                request.qty_received = 0.0
                continue
            request.qty_requested = sum(request.line_ids.mapped('quantity'))
            transit = request._get_transit_location()
            qty_shipped = qty_received = 0.0
            for picking in request._all_pickings():
                if picking.state != 'done':
                    continue
                for move in picking.move_ids:
                    if move.state != 'done':
                        continue
                    if move.location_id == transit and move.location_dest_id == request.location_dest_id:
                        qty_received += move.quantity
                    elif move.location_dest_id == transit and move.location_id == request.location_src_id:
                        qty_shipped += move.quantity
            request.qty_shipped = qty_shipped
            request.qty_received = qty_received

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_transit_location(self):
        return self.env.ref('material_request.stock_location_material_transit')

    def _get_internal_picking_type(self, location):
        """Internal operation type of the warehouse the branch belongs to,
        falling back to the company's internal types.  Note that on a
        one-step warehouse Odoo archives the internal type ("Internal
        Transfers") - it is still a valid type to create transfers with, so
        archived types are considered too (active ones first)."""
        self.ensure_one()
        PickingType = self.env['stock.picking.type'].sudo()

        def pick_active(types):
            return (types.filtered('active')[:1] or types[:1])

        warehouse = location.warehouse_id
        if warehouse:
            if warehouse.int_type_id:
                return warehouse.int_type_id
            types = PickingType.with_context(active_test=False).search([
                ('code', '=', 'internal'), ('warehouse_id', '=', warehouse.id),
            ])
            if types:
                return pick_active(types)
        types = PickingType.with_context(active_test=False).search([
            ('code', '=', 'internal'), ('company_id', '=', self.company_id.id),
        ])
        if not types:
            raise UserError(_(
                'No internal transfer operation type found for company %s. '
                'Create a warehouse or an internal operation type first.',
                self.company_id.name))
        return pick_active(types)

    def _check_manager(self):
        if not self.env.su and not self.env.user.has_group(
                'material_request.group_material_request_manager'):
            raise AccessError(_('Only Material Request managers may do this.'))

    def _check_approve_rights(self):
        """Approving a transfer request is the source branch side's decision;
        approving a purchase request needs the manager (vendor, RFQ)."""
        if self.env.su or self.env.user.has_group('material_request.group_material_request_manager'):
            return
        for request in self:
            if request.request_type == 'purchase':
                raise AccessError(_(
                    'Only Material Request managers may approve a Purchase Request.'))
            if request.location_src_id not in self.env.user.mr_allowed_location_ids:
                raise AccessError(_(
                    'Only the source branch (%s) may approve this transfer request.',
                    request.location_src_id.display_name or request.location_src_id.complete_name))

    def _check_cancel_rights(self):
        if self.env.su or self.env.user.has_group('material_request.group_material_request_manager'):
            return
        user = self.env.user
        for request in self:
            if request.state in ('draft', 'submitted') and request.requester_id == user:
                continue
            if (request.request_type == 'transfer'
                    and request.location_src_id in user.mr_allowed_location_ids):
                continue
            raise AccessError(_('You are not allowed to cancel this request.'))

    def _validate_request_data(self, approval=False):
        for request in self:
            if request.request_type == 'transfer':
                if not request.location_src_id or not request.location_dest_id:
                    raise UserError(_(
                        'A Branch Transfer Request needs both branches: '
                        '"Requested From" and "Requested For".'))
                if request.location_src_id == request.location_dest_id:
                    raise UserError(_(
                        'The source branch and the requesting branch must be different.'))
                if not request.line_ids:
                    raise UserError(_('Add at least one product to the request.'))
                for line in request.line_ids:
                    if not line.product_id.is_storable:
                        raise UserError(_(
                            'Product %s cannot be transferred: it is not storable.',
                            line.product_id.display_name))
                    if not line.quantity:
                        raise UserError(_(
                            'Set a quantity for product %s.', line.product_id.display_name))
            else:
                if not request.location_dest_id:
                    raise UserError(_(
                        'A Purchase Request needs the requesting branch ("Requested For").'))
                if approval and not request.partner_id:
                    raise UserError(_(
                        'A vendor is required to approve a Purchase Request: '
                        'the purchase order needs a partner.'))
                if not request.line_ids:
                    raise UserError(_('Add at least one product to the request.'))
                for line in request.line_ids:
                    if not line.quantity:
                        raise UserError(_(
                            'Set a quantity for product %s.', line.product_id.display_name))
                    if approval and not line.product_id.purchase_ok:
                        raise UserError(_(
                            'Product %s cannot be purchased.', line.product_id.display_name))

    # ------------------------------------------------------------------
    # Classification of the transfer legs
    # ------------------------------------------------------------------
    def _out_picking_ids(self):
        """Legs carrying goods from the source branch to the transit location
        (the original shipping transfer and its backorders)."""
        self.ensure_one()
        transit = self._get_transit_location()
        return self._all_pickings().filtered(
            lambda p: p.location_id == self.location_src_id
            and p.location_dest_id == transit
            and p.state != 'cancel')

    def _in_picking_ids(self):
        """Legs carrying goods from the transit location to the requesting
        branch (the receiving transfers and their backorders)."""
        self.ensure_one()
        transit = self._get_transit_location()
        return self._all_pickings().filtered(
            lambda p: p.location_id == transit
            and p.location_dest_id == self.location_dest_id
            and p.state != 'cancel')

    # ------------------------------------------------------------------
    # Workflow buttons
    # ------------------------------------------------------------------
    def action_submit(self):
        for request in self:
            if request.state != 'draft':
                raise UserError(_('Only a draft request can be submitted.'))
            request._validate_request_data()
            request.state = 'submitted'
            request.message_post(body=_('Request submitted for approval.'))
        return True

    def action_approve(self):
        self._check_approve_rights()
        for request in self:
            if request.state != 'submitted':
                raise UserError(_('Only a submitted request can be approved.'))
            request._validate_request_data(approval=True)
            if request.request_type == 'transfer':
                if request._out_picking_ids():
                    raise UserError(_('A shipping transfer already exists for this request.'))
                picking_out = request._create_out_picking()
                request.picking_out_id = picking_out
                request.state = 'approved'
                request.message_post(body=_(
                    'Request approved. Shipping transfer %s (from %s to the transit '
                    'location) is waiting for the source branch to validate it.',
                    picking_out.name, request.location_src_id.display_name))
            else:
                if request.purchase_order_id and request.purchase_order_id.state not in ('cancel',):
                    raise UserError(_(
                        'A purchase order (RFQ) already exists for this request: %s.',
                        request.purchase_order_id.name))
                purchase_order = request._create_purchase_order()
                request.purchase_order_id = purchase_order
                request.state = 'approved'
                request.message_post(body=_(
                    'Request approved. Draft RFQ %s has been created for %s.',
                    purchase_order.name, request.partner_id.display_name))
        return True

    def _cancel_open_pickings(self):
        """Cancel every transfer still waiting for goods (draft, confirmed or
        waiting).  Done transfers are left alone: their quantities stay in the
        branches.  Cancelling goes through sudo: the branch user cancelling
        the request may not have write access to the other side's picking."""
        self.ensure_one()
        open_pickings = self._all_pickings().filtered(
            lambda p: p.state not in ('done', 'cancel'))
        open_pickings.sudo().action_cancel()

    def action_cancel(self):
        for request in self:
            if request.state in ('done', 'cancelled'):
                raise UserError(_('This request is already finished.'))
            request._check_cancel_rights()
            request._cancel_open_pickings()
            request.state = 'cancelled'
            request.message_post(body=_(
                'Request cancelled. Quantities already shipped/received stay in '
                'the branches (shipped: %(shipped)s, received: %(received)s).',
                shipped=request.qty_shipped, received=request.qty_received))
        return True

    def action_reset_draft(self):
        self._check_manager()
        for request in self:
            if request.state == 'submitted':
                request.state = 'draft'
                request.message_post(body=_('Request reset to draft.'))
            elif request.state == 'cancelled':
                if request._all_pickings():
                    raise UserError(_(
                        'This cancelled request already has transfers (shipped: '
                        '%(shipped)s, received: %(received)s). Create a new request '
                        'instead of reusing it.',
                        shipped=request.qty_shipped, received=request.qty_received))
                request.state = 'draft'
                request.message_post(body=_('Cancelled request reopened as draft.'))
            else:
                raise UserError(_(
                    'Only a submitted or a cancelled request can be reset to draft.'))
        return True

    # ------------------------------------------------------------------
    # Transfer flow
    # ------------------------------------------------------------------
    def _prepare_move_vals(self, line, location_src, location_dest, quantity=None):
        self.ensure_one()
        return {
            'product_id': line.product_id.id,
            'product_uom_qty': quantity if quantity is not None else line.quantity,
            'uom_id': line.uom_id.id,
            'location_id': location_src.id,
            'location_dest_id': location_dest.id,
            'company_id': self.company_id.id,
            'origin': self.name,
        }

    def _create_out_picking(self):
        """First leg: source branch -> transit.  Confirmed and reserved right
        away so the source branch only has to validate it."""
        self.ensure_one()
        transit = self._get_transit_location()
        picking_type = self._get_internal_picking_type(self.location_src_id)
        picking = self.env['stock.picking'].sudo().create({
            'picking_type_id': picking_type.id,
            'location_id': self.location_src_id.id,
            'location_dest_id': transit.id,
            'origin': self.name,
            'material_request_id': self.id,
            'move_ids': [
                (0, 0, self._prepare_move_vals(line, self.location_src_id, transit))
                for line in self.line_ids
            ],
        })
        picking.sudo().action_confirm()
        return picking

    def _sync_after_picking_done(self, picking):
        """Called from stock.picking._action_done() for every validated
        transfer linked to this request.  Either goods just left the source
        branch (create the receiving leg), or they just arrived."""
        self.ensure_one()
        transit = self._get_transit_location()
        is_out = (picking.location_id == self.location_src_id
                  and picking.location_dest_id == transit)
        is_in = (picking.location_id == transit
                 and picking.location_dest_id == self.location_dest_id)
        if not is_out and not is_in:
            return
        if is_out:
            new_picking = self._create_missing_in_picking()
            if new_picking:
                self.message_post(body=_(
                    'Goods left %s: receiving transfer %s (transit -> %s) is now '
                    'ready for the requesting branch.',
                    self.location_src_id.display_name,
                    new_picking.name, self.location_dest_id.display_name))
        self._check_fully_received()

    def _shipped_qty_by_product(self):
        """Quantities that actually left the source branch: done quantities of
        the validated shipping legs (demand would overcount partials)."""
        quantities = {}
        for picking in self._out_picking_ids().filtered(lambda p: p.state == 'done'):
            for move in picking.move_ids:
                if move.state != 'done':
                    continue
                quantities[move.product_id.id] = (
                    quantities.get(move.product_id.id, 0.0) + move.quantity)
        return quantities

    def _committed_qty_by_product(self):
        """Quantities already committed to a receiving transfer (the demand of
        every live receiving leg, done ones included)."""
        quantities = {}
        for picking in self._in_picking_ids():
            for move in picking.move_ids:
                if move.state == 'cancel':
                    continue
                quantities[move.product_id.id] = (
                    quantities.get(move.product_id.id, 0.0) + move.product_uom_qty)
        return quantities

    def _create_missing_in_picking(self):
        """Create the receiving leg (transit -> requesting branch) for the
        quantities that left the source branch but are not committed to a
        receiving transfer yet.  Handles partial shipments and backorders."""
        self.ensure_one()
        shipped = self._shipped_qty_by_product()
        committed = self._committed_qty_by_product()
        lines_to_create = []
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        for line in self.line_ids:
            remaining = shipped.get(line.product_id.id, 0.0) - committed.get(line.product_id.id, 0.0)
            if float_compare(remaining, 0.0, precision_digits=precision) > 0:
                lines_to_create.append((line, remaining))
        if not lines_to_create:
            return self.env['stock.picking']
        transit = self._get_transit_location()
        picking_type = self._get_internal_picking_type(self.location_dest_id)
        picking = self.env['stock.picking'].sudo().create({
            'picking_type_id': picking_type.id,
            'location_id': transit.id,
            'location_dest_id': self.location_dest_id.id,
            'origin': self.name,
            'material_request_id': self.id,
            'move_ids': [
                (0, 0, self._prepare_move_vals(line, transit, self.location_dest_id, quantity=remaining))
                for line, remaining in lines_to_create
            ],
        })
        picking.sudo().action_confirm()
        self.picking_in_id = picking
        return picking

    def _check_fully_received(self):
        """The request is done once no transfer is open anymore: everything
        that could be shipped was shipped and received.  Partial shipments
        with no backorders left therefore end as a (partial) done request."""
        self.ensure_one()
        if self.state not in ('approved', 'in_transit'):
            return
        if not self._in_picking_ids():
            return
        open_pickings = self._all_pickings().filtered(
            lambda p: p.state not in ('done', 'cancel'))
        if open_pickings:
            self.state = 'in_transit'
            return
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        fully = float_compare(
            self.qty_received, self.qty_requested, precision_digits=precision) >= 0
        if fully:
            body = _('All transfers are validated: the full quantity is received. '
                     'The request is done.')
        else:
            body = _('All transfers are validated: the request is done '
                     '(received %(received)s out of %(requested)s).',
                     received=self.qty_received, requested=self.qty_requested)
        self.state = 'done'
        self.message_post(body=body)

    # ------------------------------------------------------------------
    # Purchase flow
    # ------------------------------------------------------------------
    def _create_purchase_order(self):
        self.ensure_one()
        purchase_order = self.env['purchase.order'].sudo().create({
            'partner_id': self.partner_id.id,
            'company_id': self.company_id.id,
            'origin': self.name,
            'material_request_id': self.id,
            'order_line': [
                (0, 0, {
                    'product_id': line.product_id.id,
                    'product_qty': line.quantity,
                    'uom_id': line.uom_id.id,
                })
                for line in self.line_ids
            ],
        })
        return purchase_order

    # ------------------------------------------------------------------
    # Smart buttons
    # ------------------------------------------------------------------
    def action_view_pickings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Transfers'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('material_request_id', '=', self.id)],
        }

    def action_view_purchase_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Purchase Order'),
            'res_model': 'purchase.order',
            'view_mode': 'form',
            'res_id': self.purchase_order_id.id,
        }

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    def unlink(self):
        for request in self:
            if request.state not in ('draft', 'cancelled'):
                raise UserError(_('Only a draft or a cancelled request can be deleted.'))
        return super().unlink()
