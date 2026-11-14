# -*- coding: utf-8 -*-
"""material_request - Phase 3 harness (Odoo 20).

Covers partial shipments and backorders:
 - source branch ships 6 of 10 -> receiving leg for 6 only, request stays
   in transit until the backorder chain completes;
 - backorder validated -> second receiving leg for the remaining 4;
 - everything received -> request done with 10/10;
 - separate scenario: shipping leg validated WITHOUT backorder -> the
   request ends done with only 6 received (partial done).
 - also a two-line partial where one product is shipped fully and the
   other partially.

Everything runs inside one `odoo shell` transaction and is rolled back.
Output format (parsed by run_tests.sh):  PASS <label>  /  FAIL <label>
"""
RESULTS = []


def ck(label, cond, extra=""):
    RESULTS.append(("PASS" if cond else "FAIL", label))
    print("%s %s%s" % ("PASS" if cond else "FAIL", label,
                       ("  <<< " + str(extra)) if (extra and not cond) else ""))


def expect(exc, fn, label):
    try:
        with env.cr.savepoint():
            fn()
    except exc:
        ck(label, True)
        return
    except Exception as e:                                  # noqa: BLE001
        ck(label, False, "raised %s instead of %s: %s"
           % (type(e).__name__, exc.__name__, str(e)[:160]))
        return
    ck(label, False, "no exception raised")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
G_USER = env.ref('material_request.group_material_request_user')
G_MGR = env.ref('material_request.group_material_request_manager')
G_BASE = env.ref('base.group_user')

Users = env['res.users']
user_a = Users.create({'name': 'P3 Branch A User', 'login': 'p3_user_a', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
user_b = Users.create({'name': 'P3 Branch B User', 'login': 'p3_user_b', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})

Loc = env['stock.location']
wh_stock = env.ref('stock.stock_location_stock')
loc_a = Loc.create({'name': 'P3 Branch A', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P3 Branch A'})
loc_b = Loc.create({'name': 'P3 Branch B', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P3 Branch B'})
M = env(user=Users.create({'name': 'P3 Manager', 'login': 'p3_mgr', 'password': 'demo',
                           'group_ids': [(6, 0, [G_BASE.id, G_USER.id, G_MGR.id])]}).id)
M['res.users'].browse(user_a.id).write({'mr_allowed_location_ids': [(4, loc_a.id)]})
M['res.users'].browse(user_b.id).write({'mr_allowed_location_ids': [(4, loc_b.id)]})
A, B = env(user=user_a.id), env(user=user_b.id)

prod = env['product.product'].create({'name': 'P3 Widget', 'is_storable': True})
prod2 = env['product.product'].create({'name': 'P3 Gadget', 'is_storable': True})
env['stock.quant']._update_available_quantity(prod, loc_b, 100.0)
env['stock.quant']._update_available_quantity(prod2, loc_b, 100.0)
transit = env.ref('material_request.stock_location_material_transit')

Request = 'material.request'


def ship_leg1(request, qty, backorder=True):
    """As branch B: validate leg 1 partially (qty) or fully (None).

    In Odoo 20 a partial validation makes ``button_validate()`` return the
    backorder wizard instead of validating, so the wizard's own contexts are
    replayed here: ``skip_backorder=True`` validates, plus
    ``picking_ids_not_to_backorder`` when no backorder is wanted."""
    leg1 = B['stock.picking'].browse(request.picking_out_id.id)
    move = leg1.move_ids.filtered(lambda m: m.product_id == prod)
    if qty is not None:
        move.quantity = qty
    ctx = {'skip_backorder': True}
    if not backorder:
        ctx['picking_ids_not_to_backorder'] = [leg1.id]
    leg1.with_context(**ctx).button_validate()
    return M['stock.picking'].browse(leg1.id)


def recv(request, picking, qty=None):
    """As branch A: validate a receiving leg partially or fully."""
    picking = A['stock.picking'].browse(picking.id)
    for move in picking.move_ids:
        if qty is not None and move.product_id == prod:
            move.quantity = qty
    picking.button_validate()
    return M['stock.picking'].browse(picking.id)


# --------------------------------------------------------------------------
# 1. full backorder chain: 10 shipped as 6 + 4, received 6 then 4
# --------------------------------------------------------------------------
req = A[Request].create({
    'request_type': 'transfer',
    'location_src_id': loc_b.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 10})],
})
req.action_submit()
B[Request].browse(req.id).action_approve()
ship_leg1(req, 6.0, backorder=True)
leg1 = M['stock.picking'].browse(req.picking_out_id.id)
ck("leg 1 done (6 shipped)", leg1.state == 'done')
leg1_bo = req._all_pickings().filtered(
    lambda p: p.location_id == loc_b and p.state not in ('done', 'cancel'))
ck("shipping backorder created for 4",
   leg1_bo and M['stock.picking'].browse(leg1_bo.id).move_ids.product_uom_qty == 4.0,
   leg1_bo.mapped('name'))
leg2 = M['stock.picking'].browse(req.picking_in_id.id)
ck("receiving leg for 6 created (not 10)",
   leg2 and leg2.move_ids.product_uom_qty == 6.0,
   leg2.move_ids.mapped('product_uom_qty'))
ck("request in transit after partial ship", req.state == 'in_transit')
ck("shipped 6, received 0",
   req.qty_shipped == 6.0 and req.qty_received == 0.0,
   (req.qty_shipped, req.qty_received))

recv(req, leg2)
ck("first receiving leg done", leg2.state == 'done')
ck("still in transit: backorder open", req.state == 'in_transit', req.state)
ck("received 6", req.qty_received == 6.0, req.qty_received)
ck("transit emptied of the 6", env['stock.quant']._get_available_quantity(prod, transit) == 0.0)

leg1_bo = B['stock.picking'].browse(leg1_bo.id)
leg1_bo.button_validate()
ck("shipping backorder done", leg1_bo.state == 'done')
leg2_bo = M['stock.picking'].browse(req.picking_in_id.id)
ck("second receiving leg for 4 created",
   leg2_bo != leg2 and leg2_bo.move_ids.product_uom_qty == 4.0,
   leg2_bo.mapped('name') if leg2_bo else None)
ck("still in transit: second receiving leg open", req.state == 'in_transit')
recv(req, leg2_bo)
ck("request done after the full backorder chain", req.state == 'done', req.state)
ck("10 shipped / 10 received",
   req.qty_shipped == 10.0 and req.qty_received == 10.0,
   (req.qty_shipped, req.qty_received))
ck("branch A holds 10",
   env['stock.quant']._get_available_quantity(prod, loc_a) == 10.0)
ck("four transfers in total",
   M['material.request'].browse(req.id).picking_count == 4,
   M['material.request'].browse(req.id).picking_ids.mapped('name'))

# --------------------------------------------------------------------------
# 2. no backorder: partial done request
# --------------------------------------------------------------------------
req_p = A[Request].create({
    'request_type': 'transfer',
    'location_src_id': loc_b.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod2.id, 'quantity': 8})],
})
req_p.action_submit()
B[Request].browse(req_p.id).action_approve()
B['stock.picking'].browse(req_p.picking_out_id.id).move_ids.quantity = 3.0
ship_leg1(req_p, 3.0, backorder=False)
leg1_p = M['stock.picking'].browse(req_p.picking_out_id.id)
ck("leg 1 shipped 3 with no backorder",
   leg1_p.state == 'done'
   and not req_p._all_pickings().filtered(
       lambda p: p.location_id == loc_b and p.state not in ('done', 'cancel')))
leg2_p = M['stock.picking'].browse(req_p.picking_in_id.id)
ck("receiving leg for 3", leg2_p.move_ids.product_uom_qty == 3.0,
   leg2_p.move_ids.mapped('product_uom_qty'))
A['stock.picking'].browse(leg2_p.id).button_validate()
ck("partial request ends done", req_p.state == 'done', req_p.state)
ck("3 shipped / 3 received of 8",
   req_p.qty_shipped == 3.0 and req_p.qty_received == 3.0,
   (req_p.qty_shipped, req_p.qty_received))

# --------------------------------------------------------------------------
# 3. two-line request: one product fully, the other partially
# --------------------------------------------------------------------------
req_2 = A[Request].create({
    'request_type': 'transfer',
    'location_src_id': loc_b.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 4}),
                 (0, 0, {'product_id': prod2.id, 'quantity': 6})],
})
req_2.action_submit()
B[Request].browse(req_2.id).action_approve()
leg1_2 = B['stock.picking'].browse(req_2.picking_out_id.id)
leg1_2.move_ids.filtered(lambda m: m.product_id == prod).quantity = 4.0
leg1_2.move_ids.filtered(lambda m: m.product_id == prod2).quantity = 2.0
leg1_2.with_context(skip_backorder=True, picking_ids_not_to_backorder=[leg1_2.id]).button_validate()
leg2_2 = M['stock.picking'].browse(req_2.picking_in_id.id)
ck("receiving leg has 4 widgets and 2 gadgets",
   sorted((leg2_2.move_ids.mapped(lambda m: (m.product_id.name, m.product_uom_qty)))) ==
   [('P3 Gadget', 2.0), ('P3 Widget', 4.0)],
   leg2_2.move_ids.mapped(lambda m: (m.product_id.name, m.product_uom_qty)))
A['stock.picking'].browse(leg2_2.id).button_validate()
ck("two-line request done partially (4/4 widgets, 2/6 gadgets)",
   req_2.state == 'done'
   and req_2.qty_shipped == 6.0 and req_2.qty_received == 6.0,
   (req_2.state, req_2.qty_shipped, req_2.qty_received))

# --------------------------------------------------------------------------
# 4. cancelling mid-flight keeps done legs and drops the open ones
# --------------------------------------------------------------------------
req_x = A[Request].create({
    'request_type': 'transfer',
    'location_src_id': loc_b.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 10})],
})
req_x.action_submit()
B[Request].browse(req_x.id).action_approve()
ship_leg1(req_x, 6.0, backorder=True)
ck("mid-flight setup: 6 shipped, backorder waiting", req_x.state == 'in_transit')
B[Request].browse(req_x.id).action_cancel()
ck("cancelled mid-flight", req_x.state == 'cancelled')
ck("done legs kept, open legs cancelled",
   M['stock.picking'].browse(req_x.picking_out_id.id).state == 'done'
   and not req_x._all_pickings().filtered(
       lambda p: p.state not in ('done', 'cancel')),
   req_x._all_pickings().mapped(lambda p: (p.name, p.state)))
ck("quantities of the done legs survive",
   req_x.qty_shipped == 6.0 and req_x.qty_received == 0.0,
   (req_x.qty_shipped, req_x.qty_received))

print("--- PHASE3 DONE: %s pass / %s fail ---" % (
    sum(1 for s, _ in RESULTS if s == 'PASS'),
    sum(1 for s, _ in RESULTS if s == 'FAIL')))
