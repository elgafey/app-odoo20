# -*- coding: utf-8 -*-
"""material_request - Phase 2 harness (Odoo 20).

Covers the complete Branch Transfer Request flow end to end:
draft -> submitted -> approved (leg 1 to transit, reserved) -> source branch
validates -> receiving leg auto-created and assigned -> requesting branch
validates -> done, with correct stock movements and quantities; plus the
cancel path.

Everything runs inside one `odoo shell` transaction and is rolled back.
Output format (parsed by run_tests.sh):  PASS <label>  /  FAIL <label>
"""
from odoo.exceptions import AccessError, UserError

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
# fixtures: two branches, stock in branch B, three users
# --------------------------------------------------------------------------
G_USER = env.ref('material_request.group_material_request_user')
G_MGR = env.ref('material_request.group_material_request_manager')
G_BASE = env.ref('base.group_user')

Users = env['res.users']
user_a = Users.create({'name': 'P2 Branch A User', 'login': 'p2_user_a', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
user_b = Users.create({'name': 'P2 Branch B User', 'login': 'p2_user_b', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
mgr = Users.create({'name': 'P2 Manager', 'login': 'p2_mgr', 'password': 'demo',
                    'group_ids': [(6, 0, [G_BASE.id, G_USER.id, G_MGR.id])]})

Loc = env['stock.location']
wh_stock = env.ref('stock.stock_location_stock')
loc_a = Loc.create({'name': 'P2 Branch A', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P2 Branch A'})
loc_b = Loc.create({'name': 'P2 Branch B', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P2 Branch B'})
mgr.write({'mr_allowed_location_ids': [(6, 0, [loc_a.id, loc_b.id])]})
M = env(user=mgr.id)
M['res.users'].browse(user_a.id).write({'mr_allowed_location_ids': [(4, loc_a.id)]})
M['res.users'].browse(user_b.id).write({'mr_allowed_location_ids': [(4, loc_b.id)]})
A, B = env(user=user_a.id), env(user=user_b.id)

prod = env['product.product'].create({'name': 'P2 Widget', 'is_storable': True})
env['stock.quant']._update_available_quantity(prod, loc_b, 100.0)
transit = env.ref('material_request.stock_location_material_transit')


def quant_qty(product, location):
    quants = env['stock.quant'].sudo().search(
        [('product_id', '=', product.id), ('location_id', '=', location.id)])
    return sum(quants.mapped('quantity'))


def avail(product, location):
    return env['stock.quant']._get_available_quantity(product, location)


# --------------------------------------------------------------------------
# 1. validation gates
# --------------------------------------------------------------------------
no_src = A['material.request'].create({
    'request_type': 'transfer',
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 10})],
})
expect(UserError, lambda: no_src.action_submit(),
       "transfer without a source branch cannot be submitted")
no_src.location_src_id = loc_b
no_src.action_submit()
ck("submit works once the source branch is set", no_src.state == 'submitted')
expect(UserError, lambda: no_src.action_submit(),
       "double submit is rejected")
expect(UserError, lambda: no_src.unlink(),
       "a submitted request cannot be deleted")

no_qty = A['material.request'].create({
    'request_type': 'transfer',
    'location_src_id': loc_b.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 0.0})],
})
expect(UserError, lambda: no_qty.action_submit(),
       "line with quantity 0 cannot be submitted")

non_storable = A['material.request'].create({
    'request_type': 'transfer',
    'location_src_id': loc_b.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': env['product.product'].create(
        {'name': 'P2 Service', 'is_storable': False}).id, 'quantity': 1})],
})
expect(UserError, lambda: non_storable.action_submit(),
       "a non-storable product cannot be transferred")

same = A['material.request'].create({
    'request_type': 'transfer',
    'location_src_id': loc_a.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 1})],
})
expect(UserError, lambda: same.action_submit(),
       "source branch must differ from the requesting branch")

# --------------------------------------------------------------------------
# 2. the happy path
# --------------------------------------------------------------------------
req = A['material.request'].create({
    'request_type': 'transfer',
    'location_src_id': loc_b.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 10})],
})
ck("request created with a reference", bool(req.id), req.name)
req.action_submit()
ck("submitted by the requesting branch", req.state == 'submitted')

expect(AccessError, lambda: A['material.request'].browse(req.id).action_approve(),
       "the requesting branch cannot approve its own transfer request")

req2 = B['material.request'].browse(req.id)
req2.action_approve()
ck("source branch approves the request", req.state == 'approved')
leg1 = req.picking_out_id
ck("leg 1 created: source branch -> transit",
   leg1 and leg1.location_id == loc_b and leg1.location_dest_id == transit,
   leg1.location_id.display_name if leg1 else None)
ck("leg 1 is confirmed and reserved", leg1.state == 'assigned', leg1.state)
ck("leg 1 carries the request reference",
   leg1.origin == req.name and leg1.material_request_id == req,
   (leg1.origin, leg1.material_request_id.name if leg1.material_request_id else None))
ck("leg 1 demand matches the line",
   B['stock.picking'].browse(leg1.id).move_ids.product_uom_qty == 10.0
   and B['stock.picking'].browse(leg1.id).move_ids.product_id == prod,
   B['stock.picking'].browse(leg1.id).move_ids.mapped('product_uom_qty'))
ck("stock in branch B reserved", avail(prod, loc_b) == 90.0, avail(prod, loc_b))

expect(AccessError, lambda: A['stock.picking'].browse(leg1.id).write({'origin': 'x'}),
       "the requesting branch cannot write the source branch's leg 1")
B['stock.picking'].browse(leg1.id).button_validate()
ck("source branch validates leg 1", leg1.state == 'done', leg1.state)
leg2 = req.picking_in_id
ck("receiving leg auto-created: transit -> requesting branch",
   leg2 and leg2.location_id == transit and leg2.location_dest_id == loc_a,
   leg2.location_id.display_name if leg2 else None)
ck("request is now in transit", req.state == 'in_transit', req.state)
ck("quantities: shipped 10, received 0",
   req.qty_shipped == 10.0 and req.qty_received == 0.0,
   (req.qty_shipped, req.qty_received))
ck("leg 2 is assigned (transit has the goods)", leg2.state == 'assigned', leg2.state)
ck("transit holds 10", quant_qty(prod, transit) == 10.0, quant_qty(prod, transit))

expect(AccessError, lambda: B['stock.picking'].browse(leg2.id).write({'origin': 'x'}),
       "the source branch cannot write the receiving leg")
A['stock.picking'].browse(leg2.id).button_validate()
ck("requesting branch validates leg 2", leg2.state == 'done')
ck("request done", req.state == 'done', req.state)
ck("quantities: shipped 10, received 10",
   req.qty_shipped == 10.0 and req.qty_received == 10.0,
   (req.qty_shipped, req.qty_received))
ck("branch B lost 10", quant_qty(prod, loc_b) == 90.0, quant_qty(prod, loc_b))
ck("branch A gained 10", quant_qty(prod, loc_a) == 10.0, quant_qty(prod, loc_a))
ck("transit emptied", quant_qty(prod, transit) == 0.0, quant_qty(prod, transit))
ck("two transfers linked",
   M['material.request'].browse(req.id).picking_count == 2,
   M['material.request'].browse(req.id).picking_ids.mapped('name'))
expect(UserError, lambda: req.unlink(),
       "a done request cannot be deleted")

# --------------------------------------------------------------------------
# 3. cancel path
# --------------------------------------------------------------------------
req_c = A['material.request'].create({
    'request_type': 'transfer',
    'location_src_id': loc_b.id,
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 5})],
})
req_c.action_submit()
req_c.action_approve()
expect(AccessError, lambda: req_c.action_cancel(),
       "the requesting branch cannot cancel an approved request")
B['material.request'].browse(req_c.id).action_cancel()
ck("source branch cancels the approved request", req_c.state == 'cancelled')
ck("its leg 1 was cancelled too",
   req_c.picking_out_id.state == 'cancel', req_c.picking_out_id.state)
expect(UserError, lambda: req_c.unlink(),
       "a cancelled request with transfers cannot be deleted")

req_d = A['material.request'].create({
    'request_type': 'purchase',
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 2})],
})
req_d.action_submit()
M['material.request'].browse(req_d.id).action_cancel()
ck("requester-submitted request cancelled, no pickings involved",
   req_d.state == 'cancelled' and req_d.picking_count == 0)

print("--- PHASE2 DONE: %s pass / %s fail ---" % (
    sum(1 for s, _ in RESULTS if s == 'PASS'),
    sum(1 for s, _ in RESULTS if s == 'FAIL')))
