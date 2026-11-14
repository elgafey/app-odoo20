# -*- coding: utf-8 -*-
"""material_request - Phase 1 harness (Odoo 20).

Covers: module skeleton (sequence, transit location, groups), the branch
naming on stock.location (display name override + uniqueness), the res.users
branch assignment guard, product_ids, and basic request behaviour.

Everything runs inside one `odoo shell` transaction and is rolled back.
Output format (parsed by run_tests.sh):  PASS <label>  /  FAIL <label>
"""
from odoo.exceptions import AccessError, UserError, ValidationError
from psycopg2 import errors as pg_errors

RESULTS = []


def ck(label, cond, extra=""):
    RESULTS.append(("PASS" if cond else "FAIL", label))
    print("%s %s%s" % ("PASS" if cond else "FAIL", label,
                       ("  <<< " + str(extra)) if (extra and not cond) else ""))


def expect(exc, fn, label):
    """`fn` must raise `exc` (inside a savepoint so nothing leaks)."""
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
user_a = Users.create({'name': 'P1 Branch A User', 'login': 'p1_user_a', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
user_b = Users.create({'name': 'P1 Branch B User', 'login': 'p1_user_b', 'password': 'demo',
                       'group_ids': [(6, 0, [G_BASE.id, G_USER.id])]})
mgr = Users.create({'name': 'P1 Manager', 'login': 'p1_mgr', 'password': 'demo',
                    'group_ids': [(6, 0, [G_BASE.id, G_USER.id, G_MGR.id])]})
ck("fixtures: 3 users built",
   user_a and user_b and mgr
   and not user_a.has_group('material_request.group_material_request_manager')
   and mgr.has_group('material_request.group_material_request_manager'))

Loc = env['stock.location']
wh_stock = env.ref('stock.stock_location_stock')
loc_a = Loc.create({'name': 'P1 Branch A', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P1 Branch A'})
loc_b = Loc.create({'name': 'P1 Branch B', 'location_id': wh_stock.id,
                    'usage': 'internal', 'branch_name': 'P1 Branch B'})
A, B, M = env(user=user_a.id), env(user=user_b.id), env(user=mgr.id)

# --------------------------------------------------------------------------
# 1. module skeleton
# --------------------------------------------------------------------------
ck("module installed at 20.0.1.0.0",
   env.ref('base.module_material_request').latest_version == '20.0.1.0.0',
   env.ref('base.module_material_request').latest_version)
ck("sequence record exists",
   bool(env.ref('material_request.seq_material_request', False)))
seq = env.ref('material_request.seq_material_request')
ck("sequence prefix and padding",
   seq.prefix == 'MR/%(year)s/' and seq.padding == 4, seq.prefix)

transit = env.ref('material_request.stock_location_material_transit')
ck("transit location: usage transit, no company",
   transit.usage == 'transit' and not transit.company_id and transit.active,
   (transit.usage, bool(transit.company_id)))

ck("manager implies the user group",
   env.ref('material_request.group_material_request_user') in G_MGR.implied_ids,
   G_MGR.implied_ids.mapped('full_name'))
ck("user group implies internal user",
   env.ref('base.group_user') in G_USER.implied_ids,
   G_USER.implied_ids.mapped('full_name'))

# --------------------------------------------------------------------------
# 2. branch naming on stock.location
# --------------------------------------------------------------------------
ck("branch_name displayed instead of the location name",
   loc_a.display_name == 'P1 Branch A', loc_a.display_name)
ck("non-branch locations keep their complete name",
   wh_stock.display_name == 'WH/Stock', wh_stock.display_name)
expect(pg_errors.UniqueViolation,
       lambda: Loc.create({'name': 'P1 Branch A Twin', 'location_id': wh_stock.id,
                           'usage': 'internal', 'branch_name': 'P1 Branch A'}),
       "duplicate branch_name in the same company is rejected")
twin = Loc.create({'name': 'P1 Branch A Twin', 'location_id': wh_stock.id,
                   'usage': 'internal', 'branch_name': 'P1 Branch A Twin',
                   'active': False})
ck("archived locations do not block a branch_name",
   bool(twin.id), twin.id)

# --------------------------------------------------------------------------
# 3. res.users: branch assignment guard
# --------------------------------------------------------------------------
expect(AccessError,
       lambda: A['res.users'].browse(user_a.id).write(
           {'mr_allowed_location_ids': [(4, loc_a.id)]}),
       "a user cannot assign branches to himself")

mgr.write({'mr_allowed_location_ids': [(6, 0, [loc_a.id, loc_b.id])]})
ck("manager assigns his own branches", loc_a in mgr.mr_allowed_location_ids)
M['res.users'].browse(user_a.id).write({'mr_allowed_location_ids': [(4, loc_a.id)]})
M['res.users'].browse(user_b.id).write({'mr_allowed_location_ids': [(4, loc_b.id)]})
ck("manager assigns branches to other users",
   loc_a in user_a.mr_allowed_location_ids and loc_b in user_b.mr_allowed_location_ids)
expect(AccessError,
       lambda: B['res.users'].browse(user_a.id).write(
           {'mr_allowed_location_ids': [(4, loc_b.id)]}),
       "a non-manager cannot assign branches to another user")

# --------------------------------------------------------------------------
# 4. basic request behaviour
# --------------------------------------------------------------------------
prod = env['product.product'].create({'name': 'P1 Widget', 'is_storable': True})
prod2 = env['product.product'].create({'name': 'P1 Gadget', 'is_storable': True})

req = A['material.request'].create({
    'request_type': 'transfer',
    'location_dest_id': loc_a.id,
    'location_src_id': loc_b.id,
    'line_ids': [(0, 0, {'product_id': prod.id, 'quantity': 3})],
})
ck("employee creates a transfer request", bool(req.id))
ck("reference looks like MR/YYYY/NNNN",
   req.name.startswith('MR/20') and len(req.name.rsplit('/', 1)[-1]) == 4, req.name)
ck("requester defaults to the current user", req.requester_id == user_a)
ck("company defaults to the user company", req.company_id == env.company)
ck("state starts at draft", req.state == 'draft')
ck("line keeps quantity and related UoM",
   req.line_ids.quantity == 3 and req.line_ids.uom_id == prod.uom_id)
ck("product_ids compute", req.product_ids == prod, req.product_ids)
ck("qty_requested follows the lines from the start", req.qty_requested == 3.0, req.qty_requested)
ck("picking_count starts at 0", req.picking_count == 0)
ck("shipped/received stay at zero before approval",
   req.qty_shipped == 0.0 and req.qty_received == 0.0,
   (req.qty_shipped, req.qty_received))

req2 = B['material.request'].create({
    'request_type': 'purchase',
    'location_dest_id': loc_b.id,
    'line_ids': [(0, 0, {'product_id': prod2.id, 'quantity': 1})],
})
ck("purchase request needs no source branch", req2.request_type == 'purchase')

expect(Exception,
       lambda: A['material.request.line'].create({'request_id': req.id, 'quantity': 1}),
       "a request line requires a product")

print("--- PHASE1 DONE: %s pass / %s fail ---" % (
    sum(1 for s, _ in RESULTS if s == 'PASS'),
    sum(1 for s, _ in RESULTS if s == 'FAIL')))
