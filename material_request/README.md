# material_request — طلبات المواد (Material Request)

A self-contained Odoo 20 module implementing a **Material Request** system with
two request types between **branches** (an internal `stock.location`):

1. **Branch Transfer Request** — a branch requests goods from another branch.
   Approval creates a **real stock transfer** (source branch → transit). When
   the source branch validates it, a second transfer (transit → requesting
   branch) is created **automatically** and becomes ready for the requesting
   branch to receive. Validating it completes the request.
2. **Purchase Request** — no stock transfers. Approval creates a **draft
   RFQ** (`purchase.order`) with the requested products; confirming the RFQ
   completes the request.

No core code is modified — everything is `_inherit`, new models, views,
security and data.

| Item        | Value                                   |
|-------------|-----------------------------------------|
| Name        | `material_request`                      |
| Version     | `20.0.1.0.0`                            |
| Depends     | `stock`, `purchase`, `mail`             |
| License     | LGPL-3                                  |
| Models      | `material.request`, `material.request.line` (+ `stock.location`, `res.users`, `stock.picking`, `purchase.order` inherits) |

---

## 1. Install

Requirements: Odoo 20 (Python 3.12, PostgreSQL 16), `stock` and `purchase`
installed on the target database.

```bash
# 1) put the module folder at /home/admin/custom_addons/material_request
#    and make sure /home/admin/custom_addons is in addons_path
odoo -d <yourdb> -i material_request --stop-after-init \
    --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/home/admin/custom_addons
```

On install the module creates (both `noupdate=1`):

* sequence `material.request` → references `MR/<year>/0001`;
* a dedicated transit location **“Internal Transfer Transit”**
  (`stock_location_material_transit`, `usage = transit`, shared between
  companies) used by every branch transfer.

Then assign users: *Settings → Users → “Material Request”* group (**User** /
**Administrator**), and open *Material Requests → Configuration*.

## 2. Configuration

* **Branches** (`Configuration → Branches`): every internal location is a
  branch. Set its **Branch Name** (`branch_name`): the branch is *displayed
  everywhere by this name* (it overrides `display_name` for internal
  locations that carry one). Creating/deleting locations still requires the
  standard Inventory administrator; the Material Request manager may rename
  them.
* **Branch Access** (`Configuration → Branch Access`): for each user select
  `mr_allowed_location_ids` — the branches whose **real transfers**
  (sending **or** receiving) the user may see and process. A Material Request
  manager can edit this without any other `res.users` rights (the module only
  lifts the write for this exact field); users can never assign branches to
  themselves.

## 3. Request flow

`material.request` states: `Draft → Submitted → Approved → (In Transit) →
Done`, or `Cancelled`.

* **Submit** — the requester sends the draft (validates: branches set, at
  least one product with a quantity; transfer products must be storable,
  purchase products must be purchasable).
* **Approve**
  * *transfer*: the source branch side (a user whose allowed branches include
    `location_src_id`) or a manager approves → creates **leg 1**
    (`internal` picking: source branch → Internal Transfer Transit, confirmed
    and reserved, `origin = MR/...`).
  * *purchase*: a manager approves (a **vendor is required at approval**
    because `purchase.order` needs a partner) → creates the **draft RFQ**
    with the request lines and links it back.
* **In Transit** — when the source branch validates leg 1 (goods leave the
  source branch), the module automatically creates **leg 2**
  (transit → requesting branch, confirmed/reserved) which is immediately
  visible to the requesting branch via the allowed-branches rule.
* **Done** — when the requesting branch validates leg 2 and no transfer is
  open anymore, the request is *Done*.
* **Cancel** — cancels open transfers and the request (already shipped /
  received quantities stay in the branches). **Reset to Draft** — from
  *Submitted* or from *Cancelled* (only if no transfer was ever created).

### Partial quantities / backorders

Leg 1 validated with less than requested → Odoo creates the standard
backorder (kept linked to the request); the request goes *In Transit* and a
receiving transfer is created **only for the shipped quantity**. When a
backorder of leg 1 is validated later, another receiving transfer is created
for the new quantity. The request ends *Done* only when **no transfer is
open** anymore (a partially-filled request with nothing pending also ends
*Done* — the chatter notes what was received). Cancelling the remaining
transfers ends the request as *Cancelled*.

## 4. Visibility rules (security)

Implemented with Odoo 20's single `ir.access` model (`security/ir.access.csv`,
group rows are permissions, no-group rows are global restrictions):

| Access                                                | Who                                              | Domain                                                                                              |
|-------------------------------------------------------|--------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| `material.request` / lines (`crud`)                   | User                                             | own requests **or** `location_src_id`/`location_dest_id` ∈ `user.mr_allowed_location_ids`           |
| `material.request` / lines (`crud`)                   | Administrator                                    | everything                                                                                          |
| `stock.picking` (`ru`)                                | User                                             | `location_id` or `location_dest_id` ∈ allowed branches — covers both transit legs                     |
| `stock.picking` (`r`)                                 | Administrator                                    | everything                                                                                          |
| `stock.move` (`ru`) / `stock.move.line` (`crud`)      | User                                             | `('picking_id', 'access', 'write')` — i.e. as far as the picking itself is writable by the user      |
| `purchase.order` / lines (`r`)                        | User & Administrator                             | linked to a material request                                                                        |
| `stock.location` (`u`)                                | Administrator                                    | to maintain branch names                                                                            |
| `stock.lot` (`cru`)                                   | User                                             | so tracked products can be received                                                                 |

Note: a user who is **also** a standard `stock.group_stock_user` keeps the
full Inventory visibility granted by the stock module — the rules above only
*grant* access to Material Request users, they never restrict other groups'
users, and administrators are unaffected.

Consequently a branch user only *sees* the transfers of their own side, but
the request's progress counters (`qty_shipped`, `qty_received`,
`picking_count`) are computed **through sudo** so every user sees the real
progress even when the other branch's transfer itself is invisible.

## 5. Assumptions & notes

* A “branch” is an **internal** `stock.location`; branches may live outside
  warehouses — the legs then use the company's first internal operation type
  (`stock.picking.type`, code `internal`), otherwise the one of the branch's
  warehouse.
* The transit location is **shared** (`company_id = False`) like Odoo's own
  inter-company transit; branch transfers are assumed to be within one
  company (`material.request.company_id`).
* Request quantities are in the **product's UoM** (the line's unit is a
  stored compute from the product); transfers and RFQ lines reuse it.
* Purchase requests create **no pickings** (pure `purchase` flow).
* Line quantities may exceed stock; leg 1 simply stays *Waiting* until stock
  is available — approval does not block on availability.

## 6. Test suite

```bash
cd /home/admin/custom_addons/material_request/test_suite
ODOO_DB=<scratch db> ./run_tests.sh          # installs/upgrades + runs both flows + access tests
```

Five phase harnesses, each in its own rolled-back `odoo shell` transaction:
1. skeleton, transit location, branch naming, user branch guard
2. transfer request end to end (submit → approve → leg 1 → leg 2 → done)
3. partial shipments, backorders, no-backorder partial done, mid-flight cancel
4. purchase request (manager-only approval, vendor gate, RFQ, confirm)
5. security rules (scope, leg visibility, move/line access, workflow rights)

Note for future harness work: in Odoo 20 a *partial* `button_validate()`
returns the backorder wizard instead of validating — the suite replays the
wizard's contexts (`skip_backorder=True`, plus `picking_ids_not_to_backorder`
when no backorder is wanted).
