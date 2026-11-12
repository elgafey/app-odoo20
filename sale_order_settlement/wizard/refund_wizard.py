# -*- coding: utf-8 -*-
"""نافذة «ردّ المال للزبون» — المرحلة السابعة.

الإرجاع يُخرج وثيقة (إشعار دائن) تلقائيًا في كل حال — راجع
``sale.settlement.return.wizard._sst_settle``. أمّا **ردّ المال** فحدثٌ
لاحق على أمر البيع نفسه: زرّ يظهر فقط حين يكون الرصيد في صالح الزبون،
ونافذة واحدة تقول كم وبأي دفتر.

قاعدة المشروع محفوظة: لا تعديل واحد على نواة Odoo — نستدعي
``account.payment.register`` نفسه الذي يستدعيه زرّ «Register Payment»
في النواة. وكل لمسة على الدفعات خلف ``sudo`` — الوحدة لا تمنح المندوب
أي صلاحية على ``account.payment``.

**مُتحقَّق منه في Odoo 20:**

* ``account.payment.register`` تُنشأ بـ ``active_model``/``active_ids``
  على وثيقة واحدة، و ``can_edit_wizard`` تصبح ``True`` حين
  ``len(batches) == 1``، عندها تُبنى الدفعة من ``self.amount`` بالضبط.
  ونتحقّق بعد التنفيذ من المبلغ المدفوع فعلًا؛ فإن اختلف رمي Odoo
  استثناءً فتُلغى المعاملة كلّها ولا يبقى أثر ناقص.
* ``payment_difference_handling='open'`` تترك الفارق مفتوحًا على
  الإشعار الدائن بدل أن تكتبه في حساب فروقات.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount


class SaleSettlementRefundWizard(models.TransientModel):
    _name = 'sale.settlement.refund.wizard'
    _description = 'Give money back to the customer'

    # ------------------------------------------------------------------
    #  الحقول
    # ------------------------------------------------------------------
    order_id = fields.Many2one(
        'sale.order', string='Sales Order', required=True,
        readonly=True, ondelete='cascade')

    currency_id = fields.Many2one(
        related='order_id.currency_id', string='Currency', readonly=True)

    amount_owed = fields.Monetary(
        string='Owed back to the customer',
        compute='_compute_amount_owed',
        help="What this order owes the customer right now — the number "
             "the red box under the total shows.")

    amount = fields.Monetary(
        string='Amount to give back', required=True, default=0.0,
        help="Leave it at the full amount unless you are giving the money "
             "back in instalments. You cannot give back more than the "
             "order owes.")

    refund_journal_id = fields.Many2one(
        'account.journal', string='Give back through',
        compute='_compute_refund_journal_id',
        store=True, readonly=False,
        domain="[('id', 'in', available_refund_journal_ids)]",
        help="The bank or cash journal the money leaves through. It is "
             "pre-filled with the one the customer paid this order with — "
             "change it when the money has to come from somewhere else.")

    available_refund_journal_ids = fields.Many2many(
        'account.journal',
        compute='_compute_available_refund_journal_ids',
        help="Journals of this order's company that can send money.")

    payment_warning = fields.Char(
        string='What will happen', compute='_compute_payment_warning')

    # ------------------------------------------------------------------
    #  القيم الافتراضية
    # ------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        """نبدأ من المبلغ المستحقّ كاملًا — هو الحال الشائعة.

        لماذا ``default_get`` لا حقل محسوب ``readonly=False``؟ لأن هذا
        الحقل يجب أن يقبل تعديل المستخدم ولا يُعاد حسابه تحت يدِه،
        والحقل المحسوب المخزَّن يعود إلى حسابه متى تغيّرت اعتماداتِه.
        وهنا لا اعتماد أصلًا: الرقم يُقرأ مرّة عند فتح النافذة.
        """
        res = super().default_get(fields_list)
        if 'amount' not in fields_list or res.get('amount'):
            return res
        order_id = res.get('order_id')
        if not order_id:
            return res
        order = self.env['sale.order'].browse(order_id)
        if order.sst_due_state == 'refund':
            res['amount'] = abs(order.sst_amount_due)
        return res

    # ------------------------------------------------------------------
    #  الحسابات
    # ------------------------------------------------------------------
    @api.depends('order_id', 'order_id.sst_amount_due')
    def _compute_amount_owed(self):
        for wizard in self:
            order = wizard.order_id
            wizard.amount_owed = (
                abs(order.sst_amount_due)
                if order.sst_due_state == 'refund' else 0.0)

    @api.depends('order_id')
    def _compute_available_refund_journal_ids(self):
        for wizard in self:
            wizard.available_refund_journal_ids = (
                wizard.order_id._sst_refundable_journals()
                if wizard.order_id else wizard.env['account.journal'])

    @api.depends('order_id')
    def _compute_refund_journal_id(self):
        for wizard in self:
            wizard.refund_journal_id = (
                wizard.order_id._sst_default_refund_journal()
                if wizard.order_id else False)

    @api.depends('order_id', 'amount', 'refund_journal_id')
    def _compute_payment_warning(self):
        for wizard in self:
            wizard.payment_warning = wizard._sst_payment_warning()

    def _sst_payment_warning(self):
        """السطر الذي يشرح ماذا سيحدث قبل أن يضغط المستخدم.

        نفس مذهب شريط التحذير في نافذة الإرجاع: نكتب بالضبط ما سيفعله
        ``action_refund`` من نفس الأرقام ومن دون تنفيذ شيء.
        """
        self.ensure_one()
        order = self.order_id
        currency = order.currency_id
        if not currency or currency.is_zero(self.amount or 0.0):
            return _('Enter the amount to give back and the journal it '
                     'leaves through.')
        journal = self.refund_journal_id
        if not journal:
            return _('%(amount)s will go back to %(partner)s — choose the '
                     'journal it leaves through.',
                     amount=format_amount(self.env, self.amount, currency),
                     partner=order.partner_id.display_name)
        return _('%(amount)s goes back to %(partner)s through %(journal)s.',
                 amount=format_amount(self.env, self.amount, currency),
                 partner=order.partner_id.display_name,
                 journal=journal.display_name)

    # ------------------------------------------------------------------
    #  التنفيذ
    # ------------------------------------------------------------------
    def action_refund(self):
        """يردّ المبلغ على الزبون ويوثّقه في دفتر الأستاذ.

        الترتيب ليس عشوائيًا: تحقّق كامل ثم تنفيذ، ولو تعثّر أي خطأ
        يرمي Odoo استثناءً فتُلغى المعاملة كلّها — لا يبقى دفعة بلا
        أثر ولا أثر بلا دفعة.

        ونرجع من أصغر أثر: نمرّ على إشعارات هذا الأمر الدائنة مرّة
        مرّة ونصرف من كل واحدة ما يتبقّى، فلو كان المستخدم يردّ
        على مراحل صار كل مرحلة على وثيقة بذاتها.
        """
        self.ensure_one()

        order = self.order_id
        currency = order.currency_id

        # بوابة الكتابة: من يردّ المال يجب أن يملك الكتابة على العقد.
        order.check_access('write')

        if order.state != 'sale':
            raise UserError(_(
                'Confirm the sales order before giving money back.'))
        if order.sst_due_state != 'refund':
            raise UserError(_(
                'There is nothing to give back to the customer on %s.',
                order.display_name or order.name))

        owed = abs(order.sst_amount_due)
        amount = currency.round(self.amount or 0.0)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_('Enter an amount greater than zero.'))
        if currency.compare_amounts(amount, owed) > 0:
            raise UserError(_(
                'You cannot give back more than %(owed)s — that is all '
                '%(order)s owes the customer.',
                owed=format_amount(self.env, owed, currency),
                order=order.display_name or order.name))

        journal = self.refund_journal_id
        if not journal or not journal._get_available_payment_method_lines(
                'outbound'):
            raise UserError(_(
                "Choose the journal to refund the customer through."))

        # كاش نظيف قبل القراءة: ``sst_amount_due`` حقل محسوب غير مخزَّن
        # وقد تغيّر منذ فتح النافذة.
        self.env.invalidate_all()
        if order.sst_due_state != 'refund':
            raise UserError(_(
                'The balance of %s changed. Close this window and check '
                'it again.', order.display_name or order.name))

        targets = order.sudo().invoice_ids.filtered(
            lambda move: move.move_type == 'out_refund'
            and move.state == 'posted'
            and not currency.is_zero(move.amount_residual)
        ).sorted(lambda move: move.id)

        notes = []
        remaining = amount
        for refund in targets:
            if currency.compare_amounts(remaining, 0.0) <= 0:
                break
            residual = abs(refund.amount_residual)
            take = (residual
                    if currency.compare_amounts(residual, remaining) <= 0
                    else remaining)
            notes.append(self._sst_pay_one(refund, journal, take))
            remaining -= take

        if currency.compare_amounts(remaining, 0.0) > 0:
            # لم تكفِ إشعارات هذا الأمر. مستحيل في الحساب العاديّ لأن
            # ``owed = Σ إشعارات دائنة − Σ فواتير``، لكن إن وقع رُمي
            # استثناءً فتُلغى كل الدفعات التي سبقت.
            raise UserError(_(
                'Only %(left)s could be given back out of %(amount)s.',
                left=format_amount(self.env, remaining, currency),
                amount=format_amount(self.env, amount, currency)))

        order.message_post(
            body=_('%(amount)s given back to %(partner)s by %(user)s '
                   'through %(journal)s.',
                   amount=format_amount(self.env, amount, currency),
                   partner=order.partner_id.display_name,
                   user=self.env.user.name,
                   journal=journal.display_name),
            subtype_xmlid='mail.mt_note',
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Refund Customer'),
                'message': '\n'.join(notes),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def _sst_pay_one(self, refund, journal, take):
        """دفعة صادرة على إشعار دائن واحد بمقدار ``take``.

        لماذا نمرّ وثيقة وثيقة بدل أن نمرّها كلها في نافذة واحدة؟ لأن
        ``account.payment.register`` حين تُستدعى على أكثر من وثيقة
        **ولا** تُحرَّر (``can_edit_wizard`` كاذبة) تبني الدفعة من مبلغ
        الدفعة نفسها لا من ``self.amount`` فلو مرّرنا مبلغًا أقلّ من
        المتبقّي لصرفت أكثر ممّا أردنا. والوثيقة الواحدة دفعة واحدة
        فالمبلغ هو بعينه.

        ونتحقّق بعد التنفيذ: إن خرج مبلغ غير الذي طلبنا رمي Odoo
        استثناءً فتُلغى المعاملة كلّها ولا يبقى نصف أثر.
        """
        self.ensure_one()
        currency = refund.currency_id
        before = refund.sudo().matched_payment_ids

        register = self.env['account.payment.register'].sudo().with_context(
            active_model='account.move',
            active_ids=refund.ids,
            dont_redirect_to_payments=True,
        ).create({
            'payment_date': fields.Date.context_today(self),
            'amount': take,
            'journal_id': journal.id,
            # الفارق يبقى مفتوحًا على الإشعار الدائن — لا نشطب صمتًا
            # ولا نرميه في حساب فروقات.
            'payment_difference_handling': 'open',
            'communication': self.order_id.display_name,
        })
        register.action_create_payments()

        refund.invalidate_recordset(
            ['matched_payment_ids', 'amount_residual', 'payment_state'])
        paid = (refund.sudo().matched_payment_ids - before).filtered(
            lambda payment: payment.state != 'canceled')
        got = sum(paid.mapped('amount'))
        if currency.compare_amounts(got, take) != 0:
            raise UserError(_(
                'Expected to give back %(want)s on %(move)s but %(got)s '
                'was recorded. Nothing has been saved.',
                want=format_amount(self.env, take, currency),
                move=refund.display_name,
                got=format_amount(self.env, got, currency)))

        return _('%(amount)s given back through %(move)s.',
                 amount=format_amount(self.env, take, currency),
                 move=refund.display_name)
