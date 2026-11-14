# -*- coding: utf-8 -*-
"""material_request - Phase 4 harness (Odoo 20).

Covers Purchase Requests: manager-only approval, vendor required at
approval, RFQ creation (partner, origin, lines with quantity and UoM),
purchase order confirmation completing the request, and the fact that
purchase requests never create transfers.

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
# fixtures
# --------------------------------------------------------------------------
G_USER = env.ref('material_request.group_material_request_user')
G_MGR = env.ref('material_request.group_material_request_manager')
G_BASE = env.ref('base.group_user')

Users = env['res.users']
user_a = Users.create({'name': 'P4 Branch A User', 'login': 'p4_user_a', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
mgr = Users.create({'name': 'P4 Manager', 'login': 'p4_mgr', 'password': 'demo',
                    'group_ids': [(6, 0, [G_BASE.id, G_USER.id, G_MGR.id])]})

Loc = env['stock.location']
wh_stock = env.ref('stock.stock_location_stock')
loc_a = Loc.create({'name': 'P4 Branch A', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P4 Branch A'})
M = env(user=mgr.id)
M['res.users'].browse(user_a.id).write({'mr_allowed_location_ids': [(4, loc_a.id)]})
A = env(user=user_a.id)

prod = env['product.product'].create({'name': 'P4 Widget', 'is_storable': True})
vendor = env['res.partner'].create({'name': 'P4 Vendor'})

# --------------------------------------------------------------------------
# 1. gates
# --------------------------------------------------------------------------
req = A['material.request'].create({
    'request_type': 'purchase',
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 5})],
})
req.action_submit()
ck("purchase request submitted", req.state == 'submitted')

expect(AccessError, lambda: req.action_approve(),
       "a regular branch user cannot approve a purchase request")
expect(UserError, lambda: M['material.request'].browse(req.id).action_approve(),
       "a vendor is required to approve a purchase request")

bad_prod = A['material.request'].create({
    'request_type': 'purchase',
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': env['product.product'].create(
        {'name': 'P4 Not Buyable', 'is_storable': True, 'purchase_ok': False}).id,
        'quantity': 1})],
})
bad_prod.action_submit()
expect(UserError, lambda: M['material.request'].browse(bad_prod.id).action_approve(),
       "a non-purchasable product cannot be approved")

# --------------------------------------------------------------------------
# 2. approve -> RFQ
# --------------------------------------------------------------------------
req.partner_id = vendor.id
M['material.request'].browse(req.id).action_approve()
ck("manager approves the purchase request", req.state == 'approved')
po = req.purchase_order_id
ck("draft RFQ created", bool(po) and po.state == 'draft', po.state if po else None)
ck("RFQ partner, company and origin",
   po.partner_id == vendor and po.company_id == req.company_id
   and po.origin == req.name,
   (po.partner_id.name, po.origin))
ck("RFQ lines match the request lines",
   len(po.order_line) == 1
   and po.order_line.product_id == prod
   and po.order_line.product_qty == 5.0
   and po.order_line.uom_id == prod.uom_id,
   po.order_line.mapped(lambda l: (l.product_id.name, l.product_qty)))
ck("no transfers for a purchase request",
   req.picking_count == 0 and not req.picking_out_id and not req.picking_in_id)
expect(UserError, lambda: req.unlink(),
       "an approved request cannot be deleted")

# --------------------------------------------------------------------------
# 3. confirm the RFQ -> request done
# --------------------------------------------------------------------------
expect(AccessError, lambda: A['purchase.order'].browse(po.id).button_confirm(),
       "the branch user cannot confirm the RFQ (read-only access)")
po.sudo().button_confirm()
ck("RFQ confirmed", po.state == 'purchase')
ck("request done once the purchase order is confirmed", req.state == 'done', req.state)

# --------------------------------------------------------------------------
# 4. cancel path
# --------------------------------------------------------------------------
req_c = A['material.request'].create({
    'request_type': 'purchase',
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 2})],
})
req_c.action_submit()
req_c.partner_id = vendor.id
M['material.request'].browse(req_c.id).action_approve()
req_c.purchase_order_id.sudo().button_confirm()
expect(UserError, lambda: req_c.action_cancel(),
       "a done request cannot be cancelled")

req_r = A['material.request'].create({
    'request_type': 'purchase',
    'location_dest_id': loc_a.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 2})],
})
req_r.action_submit()
M['material.request'].browse(req_r.id).action_reset_draft()
ck("manager resets a submitted request to draft", req_r.state == 'draft')

print("--- PHASE4 DONE: %s pass / %s fail ---" % (
    sum(1 for s, _ in RESULTS if s == 'PASS'),
    sum(1 for s, _ in RESULTS if s == 'FAIL')))
