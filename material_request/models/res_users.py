# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import AccessError


class ResUsers(models.Model):
    _inherit = 'res.users'

    mr_allowed_location_ids = fields.Many2many(
        'stock.location',
        'mr_allowed_location_res_users_rel', 'user_id', 'location_id',
        string='Allowed Material Request Branches',
        domain=[('usage', '=', 'internal')],
        help="Branches (internal locations) whose real transfers - sending or "
             "receiving - this user may see and process.  Used by the Material "
             "Request module to restrict stock transfers visibility.")

    def write(self, vals):
        """Branch assignment is managed by the Material Request manager.

        Two special cases are handled here so that neither the standard
        ``res.users`` rights nor the user's own self-write access can be
        abused:

        * a manager may write this field (and only this field) on any user
          through a sudo path, without having any other ``res.users`` right;
        * a plain user can never grant branches to himself, even though the
          standard rules would allow writing his own record.
        """
        if not self.env.su and 'mr_allowed_location_ids' in vals:
            if set(vals) == {'mr_allowed_location_ids'}:
                if self.env.user.has_group('material_request.group_material_request_manager'):
                    res = super(ResUsers, self.sudo()).write(vals)
                    # access domains embedding user.mr_allowed_location_ids are cached
                    self.env.transaction.invalidate_ormcache()
                    return res
                if self.env.uid in self._ids:
                    raise AccessError(_(
                        'You cannot change your own allowed Material Request '
                        'branches. Ask a Material Request manager.'))
            else:
                if self.env.uid in self._ids and not self.env.user.has_group(
                        'material_request.group_material_request_manager'):
                    raise AccessError(_(
                        'You cannot change your own allowed Material Request '
                        'branches. Ask a Material Request manager.'))
        return super().write(vals)
