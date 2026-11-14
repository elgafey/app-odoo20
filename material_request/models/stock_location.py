# -*- coding: utf-8 -*-
from odoo import api, fields, models


class StockLocation(models.Model):
    _inherit = 'stock.location'

    branch_name = fields.Char(
        string='Branch Name',
        index='trigram',
        help="Name under which this internal location is displayed when it is "
             "used as a branch in Material Requests.  Leave empty to keep the "
             "standard location name.")

    _branch_name_unique = models.UniqueIndex(
        "(branch_name) WHERE usage = 'internal' AND branch_name IS NOT NULL AND branch_name != ''",
        "This branch name is already used by another branch (internal location).",
    )

    # The branch is displayed everywhere by its own name: searching for a
    # branch should also match branch_name.
    _rec_names_search = ('complete_name', 'branch_name', 'barcode')

    @api.depends('name', 'location_id.complete_name', 'usage', 'branch_name')
    @api.depends_context('formatted_display_name', 'show_empty')
    def _compute_display_name(self):
        super()._compute_display_name()
        for location in self:
            if location.usage == 'internal' and location.branch_name:
                location.display_name = location.branch_name
