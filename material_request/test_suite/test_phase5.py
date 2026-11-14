# -*- coding: utf-8 -*-
"""material_request - Phase 5 harness (Odoo 20).

Covers the access rules of the ir.access.csv:
 - material.request scope: own requests + requests between allowed branches;
 - transfer leg visibility per branch (leg 1 = source branch, leg 2 =
   requesting branch) and move/move-line rights through the
   ('picking_id', 'access', 'write') domain;
 - purchase order/lines are read-only for branch users;
 - manager read-only on transfers;
 - approve/cancel/reset rights split;
 - branch users get no stock module access (no implicit stock groups).

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
# fixtures: three branches, five users
# --------------------------------------------------------------------------
G_USER = env.ref('material_request.group_material_request_user')
G_MGR = env.ref('material_request.group_material_request_manager')
G_BASE = env.ref('base.group_user')

Users = env['res.users']
user_a = Users.create({'name': 'P5 Branch A User', 'login': 'p5_user_a', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
user_b = Users.create({'name': 'P5 Branch B User', 'login': 'p5_user_b', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
user_out = Users.create({'name': 'P5 No Branch User', 'login': 'p5_user_out', 'password': 'demo',
                         'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
mgr = Users.create({'name': 'P5 Manager', 'login': 'p5_mgr', 'password': 'demo',
                    'group_ids': [(6, 0, [G_BASE.id, G_USER.id, G_MGR.id])]})

Loc = env['stock.location']
wh_stock = env.ref('stock.stock_location_stock')
loc_a = Loc.create({'name': 'P5 Branch A', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P5 Branch A'})
loc_b = Loc.create({'name': 'P5 Branch B', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P5 Branch B'})
loc_c = Loc.create({'name': 'P5 Branch C', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P5 Branch C'})
M = env(user=mgr.id)
M['res.users'].browse(user_a.id).write({'mr_allowed_location_ids': [(4, loc_a.id)]})
M['res.users'].browse(user_b.id).write({'mr_allowed_location_ids': [(4, loc_b.id)]})
A, B, O = env(user=user_a.id), env(user=user_b.id), env(user=user_out.id)

prod = env['product.product'].create({'name': 'P5 Widget', 'is_storable': True})
env['stock.quant']._update_available_quantity(prod, loc_b, 100.0)
vendor = env['res.partner'].create({'name': 'P5 Vendor'})

Request = 'material.request'


def transfer_request(user_env, dest, src):
    req = user_env[Request].create({
        'request_type': 'transfer',
        'location_src_id': src.id,
        'location_dest_id': dest.id,
        'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 10})],
    })
    req.action_submit()
    return req


# requests: A asks B, B asks C, outsider asks A
req_ba = transfer_request(A, loc_a, loc_b)   # B ships to A
req_cb = transfer_request(B, loc_b, loc_c)   # C ships to B
req_oa = transfer_request(O, loc_a, loc_c)   # C ships to A, asked by outsider

# --------------------------------------------------------------------------
# 1. record scope of material.request
# --------------------------------------------------------------------------
ck("branch A user sees only requests touching branch A",
   set(A[Request].search([]).ids) == {req_ba.id, req_oa.id},
   sorted(A[Request].search([]).ids))
ck("branch B user sees only requests touching branch B",
   set(B[Request].search([]).ids) == {req_ba.id, req_cb.id},
   sorted(B[Request].search([]).ids))
ck("user without branches sees only his own request",
   set(O[Request].search([]).ids) == {req_oa.id}, sorted(O[Request].search([]).ids))
ck("manager sees everything",
   {req_ba.id, req_cb.id, req_oa.id} <= set(M[Request].search([]).ids))
expect(AccessError, lambda: A[Request].browse(req_cb.id).read(['name']),
       "branch A user cannot read a request he is not part of")
expect(AccessError, lambda: O[Request].browse(req_ba.id).read(['name']),
       "outsider cannot read another user's request")

# approve req_ba and reserve leg 1; keep both legs OPEN for the move tests
B[Request].browse(req_ba.id).action_approve()
leg1 = req_ba.picking_out_id
ck("leg 1 assigned", leg1.state == 'assigned', leg1.state)

# --------------------------------------------------------------------------
# 2. moves and move lines follow picking access (legs still open)
# --------------------------------------------------------------------------
leg1_move = leg1.move_ids[0]
ck("source branch writes its leg's move",
   B['stock.move'].browse(leg1_move.id).write({'quantity': 10.0}) is not False)
expect(AccessError, lambda: A['stock.move'].browse(leg1_move.id).write({'quantity': 5.0}),
       "requesting branch cannot write the other leg's move")
expect(AccessError, lambda: O['stock.move'].browse(leg1_move.id).write({'quantity': 5.0}),
       "outsider cannot write any move")
expect(AccessError, lambda: A['stock.move.line'].search(
    [('picking_id', '=', leg1.id)]).unlink(),
       "requesting branch cannot unlink the other leg's move lines")
expect(AccessError, lambda: O['stock.move.line'].search(
    [('picking_id', '=', leg1.id)]).unlink(),
       "outsider cannot unlink move lines")

# --------------------------------------------------------------------------
# 3. transfer leg visibility per branch
# --------------------------------------------------------------------------
ck("source branch user reads leg 1", bool(B['stock.picking'].browse(leg1.id).name))
expect(AccessError, lambda: A['stock.picking'].browse(leg1.id).read(['name']),
       "requesting branch user cannot read the source branch's leg 1")
expect(AccessError, lambda: O['stock.picking'].browse(leg1.id).read(['name']),
       "outsider reads no transfer at all")
ck("manager reads every leg",
   bool(M['stock.picking'].browse(leg1.id).name))
expect(AccessError, lambda: M['stock.picking'].browse(leg1.id).write({'origin': 'X'}),
       "manager has read-only access to transfers")

# validate leg 1 -> receiving leg exists for the requesting branch
B['stock.picking'].browse(leg1.id).button_validate()
leg2 = req_ba.picking_in_id
ck("leg 2 exists and is assigned", leg2.state == 'assigned', leg2.state)
ck("requesting branch user reads leg 2", bool(A['stock.picking'].browse(leg2.id).name))
expect(AccessError, lambda: B['stock.picking'].browse(leg2.id).read(['name']),
       "source branch user cannot read the receiving leg")
leg2_move = leg2.move_ids[0]
expect(AccessError, lambda: B['stock.move'].browse(leg2_move.id).write({'quantity': 5.0}),
       "source branch cannot write the receiving leg's move")
ck("requesting branch writes its leg's move",
   A['stock.move'].browse(leg2_move.id).write({'quantity': 10.0}) is not False)

# --------------------------------------------------------------------------
# 4. purchase order access
# --------------------------------------------------------------------------
req_po = O[Request].create({
    'request_type': 'purchase',
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 3})],
})
req_po.action_submit()
M[Request].browse(req_po.id).write({'partner_id': vendor.id})
M[Request].browse(req_po.id).action_approve()
po = req_po.purchase_order_id
ck("purchase order readable by a branch user",
   bool(A['purchase.order'].browse(po.id).name))
ck("purchase order lines follow the order",
   bool(A['purchase.order.line'].search([('order_id', '=', po.id)]).ids))
ck("any material request user can read linked orders",
   bool(B['purchase.order'].browse(po.id).name))
expect(AccessError, lambda: A['purchase.order'].browse(po.id).write(
    {'partner_id': vendor.id}),
    "branch users have read-only access to purchase orders")

# --------------------------------------------------------------------------
# 5. workflow rights split
# --------------------------------------------------------------------------
expect(AccessError, lambda: req_cb.action_approve(),
       "the destination branch (B) cannot approve a request sourced from C")
user_c = Users.create({'name': 'P5 Branch C User', 'login': 'p5_user_c', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
M['res.users'].browse(user_c.id).write({'mr_allowed_location_ids': [(4, loc_c.id)]})
C = env(user=user_c.id)
C[Request].browse(req_cb.id).action_approve()
ck("the source branch (C) approves the request sourced from it",
   req_cb.state == 'approved')
expect(AccessError, lambda: O[Request].browse(req_oa.id).action_approve(),
       "the requesting branch cannot approve")
expect(AccessError, lambda: A[Request].browse(req_oa.id).action_reset_draft(),
       "only managers reset requests to draft")
M[Request].browse(req_oa.id).action_reset_draft()
ck("manager resets to draft", req_oa.state == 'draft')
expect(AccessError, lambda: req_cb.action_cancel(),
       "the destination branch cannot cancel")
C[Request].browse(req_cb.id).action_cancel()
ck("the source branch cancels", req_cb.state == 'cancelled')

# --------------------------------------------------------------------------
# 6. branch users stay out of the stock module
# --------------------------------------------------------------------------
ck("branch user is not a stock user",
   not user_a.has_group('stock.group_stock_user'))
expect(AccessError, lambda: A['product.product'].create({'name': 'P5 X'}),
       "branch users cannot create products")
ck("branch users can read locations (branch naming)",
   bool(A['stock.location'].browse(loc_a.id).display_name))
ck("branch users can read products",
   bool(A['product.product'].browse(prod.id).display_name))

print("--- PHASE5 DONE: %s pass / %s fail ---" % (
    sum(1 for s, _ in RESULTS if s == 'PASS'),
    sum(1 for s, _ in RESULTS if s == 'FAIL')))
