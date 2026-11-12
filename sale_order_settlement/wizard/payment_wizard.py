# -*- coding: utf-8 -*-
"""نافذة «تسجيل الدفعة» / «استبدال الدفعة» — المرحلة الثانية.

تُفتح من زرين في هيدر أمر البيع، وتُنفّذ العملية **كلها داخل معاملة
Odoo الواحدة** (كل نداء RPC = معاملة واحدة تُلغي كُلها عند أي خطأ)، أي
أن «الاستبدال الذرّي» ليس وعودًا: إلغاء القديم وإنشاء الجديد والتوفيق
تتم قبل أن يُرسَل ردٌّ واحد إلى المتصفح.

مبادئ أمنية ثابتة هنا:

* لا يقرأ المندوب أي دفعة بحسابه: كل لمسة على ``matched_payment_ids``
  خلف ``sudo``، مقيّدة بفاتورة اختُبرت قبلها. لذلك لا يوجد في
  ``security/ir.access.csv`` أي تصريح على ``account.payment``.
* ما قد يُزوَّر في نداء RPC لا يُوثَق: حالة الفاتورة تُفحص وقت
  التنفيذ لا في الحقل المحسوب وحده، والدفعات المُراد إلغاؤها تُشتق
  من الفاتورة نفسها لا من حقول العميل.
* بعد إنشاء الدفعة يُتحقَّق مما كُتب فعلاً: إن خرج المبلغ غير المطلوب
  رُمي استثناء فتُلغى المعاملة كلها — لا نصف أثر.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount

from odoo.addons.sale_order_settlement.models.tools import join_names


class SaleSettlementPaymentWizard(models.TransientModel):
    _name = 'sale.settlement.payment.wizard'
    _description = 'Register or Replace a Payment on a Sales Order'

    # ------------------------------------------------------------------
    #  الحقول
    # ------------------------------------------------------------------
    order_id = fields.Many2one(
        'sale.order', string='Sales Order', required=True,
        ondelete='cascade')
    mode = fields.Selection(
        [('register', 'Register Payment'), ('replace', 'Replace Payment')],
        string='Mode', required=True, default='register', readonly=True)

    # الفواتير التي يُسمح اختيارها (دومين للشاشة + تحقّق في action_apply)
    invoice_ids = fields.Many2many(
        'account.move', string='Eligible invoices',
        compute='_compute_invoice_ids')
    invoice_id = fields.Many2one(
        'account.move', string='Invoice', required=True,
        domain="[('id', 'in', invoice_ids)]")

    currency_id = fields.Many2one(related='invoice_id.currency_id')
    amount = fields.Monetary(
        string='Amount', currency_field='currency_id',
        compute='_compute_amount', store=True, readonly=False)
    payment_date = fields.Date(
        string='Payment Date', required=True,
        default=fields.Date.context_today)
    journal_id = fields.Many2one(
        'account.journal', string='Journal',
        domain="[('id', 'in', available_journal_ids)]")
    available_journal_ids = fields.Many2many(
        'account.journal', compute='_compute_available_journal_ids')
    memo = fields.Char(string='Memo')

    # نص «ما سيُلغى» في وضع الاستبدال — نص محسوب خلف sudo بدل عرض سجلات
    # account.payment للمندوب (الذي لا يملك عليها قراءة عمدًا).
    payment_summary = fields.Char(
        string='Payments that will be cancelled',
        compute='_compute_payment_summary')

    # ------------------------------------------------------------------
    #  الحقول المحسوبة
    # ------------------------------------------------------------------
    @api.depends('order_id', 'mode')
    def _compute_invoice_ids(self):
        """الفواتير المسموح بها: المرحّلة ذات المتبقي (تسجيل)
        أو ذات الدفعات الفعّالة (استبدال)."""
        for wizard in self:
            invoices = wizard.order_id.sudo().invoice_ids
            if wizard.mode == 'replace':
                invoices = invoices.filtered(
                    lambda move: move.matched_payment_ids.filtered(
                        lambda payment: payment.state != 'canceled'))
            else:
                invoices = invoices.filtered(
                    lambda move: move.state == 'posted'
                    and not move.currency_id.is_zero(move.amount_residual))
            wizard.invoice_ids = [(6, 0, invoices.ids)]

    def _sst_active_payments(self, invoice):
        """الدفعات الفعّالة على فاتورة — الملغاة تُستبعد لأن
        ``matched_payment_ids`` لا يُنظَّف عند الإلغاء في Odoo 20."""
        return invoice.sudo().matched_payment_ids.filtered(
            lambda payment: payment.state != 'canceled')

    @api.depends('mode', 'invoice_id')
    def _compute_payment_summary(self):
        for wizard in self:
            if wizard.mode == 'replace' and wizard.invoice_id:
                payments = wizard._sst_active_payments(wizard.invoice_id)
                wizard.payment_summary = ', '.join(
                    '%s — %s %s' % (
                        payment.name or payment.move_id.name,
                        payment.amount,
                        payment.currency_id.symbol or '')
                    for payment in payments) or False
            else:
                wizard.payment_summary = False

    @api.depends('mode', 'invoice_id')
    def _compute_amount(self):
        """المبلغ الافتراضي:
        • تسجيل  = المتبقي على الفاتورة (ادفع المستحقّ)
        • استبدال = مجموع ما سيُلغى (الجديد يبقى مكان القديم تمامًا)"""
        for wizard in self:
            if wizard.mode == 'replace' and wizard.invoice_id:
                wizard.amount = sum(
                    wizard._sst_active_payments(wizard.invoice_id)
                    .mapped('amount'))
            else:
                wizard.amount = wizard.invoice_id.amount_residual

    @api.depends('invoice_id')
    def _compute_available_journal_ids(self):
        """دفاتر قابلة للاستلام (bank/cash/credit) وفيها خط دفع وارد —
        نفس شرط ``account.payment.register`` في النواة."""
        for wizard in self:
            company = (wizard.invoice_id.company_id
                       or wizard.order_id.company_id
                       or self.env.company)
            journals = self.env['account.journal'].search([
                ('type', 'in', ('bank', 'cash', 'credit')),
                ('company_id', '=', company.id),
            ]).filtered('inbound_payment_method_line_ids')
            wizard.available_journal_ids = [(6, 0, journals.ids)]

    # ------------------------------------------------------------------
    #  التنفيذ
    # ------------------------------------------------------------------
    def action_apply(self):
        """نقطة الدخول الوحيدة للزرّين — يتفرّع بحسب ``mode``."""
        self.ensure_one()

        # 1) على المستخدم أن يملك الكتابة على أمر البيع هذا.
        #    (بدون هذا السطر كان sudo يفتح الباب أمام أي مندوب لأي أمر.)
        self.order_id.check_access('write')

        if self.order_id.state != 'sale':
            raise UserError(_(
                'The sales order must be confirmed before recording a '
                'payment.'))

        # 2) الفاتورة يجب أن تخصّ هذا الأمر بالضبط — يمنع أي نداء RPC
        #    مُلفَّقًا يمرّر invoice_id لا علاقة له بالأمر.
        if self.invoice_id not in self.order_id.sudo().invoice_ids:
            raise UserError(_(
                'This invoice does not belong to sales order %s.')
                % (self.order_id.display_name,))

        # 3) الفاتورة مرحّلة: نافذة النواة تقبل الدفع على مسودة صراحةً
        #    (تفرض فقط payment_difference_handling='open')، فلن نعتمد على
        #    فلتر الحقل المحسوب — الحارس هنا وقت التنفيذ.
        if self.invoice_id.state != 'posted':
            raise UserError(_(
                'Invoice %s is not posted yet.')
                % (self.invoice_id.display_name,))

        # 4) مبلغ موجب
        if self.amount is None or self.amount <= 0:
            raise UserError(_('The payment amount must be greater than '
                              'zero.'))

        if self.mode == 'replace':
            return self._sst_replace()
        return self._sst_register()

    # ---- الوضع الأول: تسجيل دفعة جديدة --------------------------------
    def _sst_register(self):
        invoice = self.invoice_id
        if invoice.currency_id.is_zero(invoice.amount_residual):
            raise UserError(_(
                'Invoice %s is already fully paid.')
                % (invoice.display_name,))

        payments = self._sst_create_payment(invoice)
        return self._sst_done(
            _('Payment registered'),
            _('Payment registered on %s: %s')
            % (invoice.display_name, join_names(payments)))

    # ---- الوضع الثاني: استبدال ذرّي -------------------------------------
    def _sst_replace(self):
        invoice = self.invoice_id
        # إعادة اشتقاق الدفعات من الفاتورة نفسها وقت التنفيذ، لا من أي
        # قيمة وصلت من العميل.
        to_cancel = self._sst_active_payments(invoice)
        if not to_cancel:
            raise UserError(_(
                'There is no active payment to replace on invoice %s.')
                % (invoice.display_name,))

        # 1) إلغاء القديم: action_cancel() على الدفعة تُلغي قيدها وتفكّ
        #    التوفيق عن الفاتورة فيعود المبلغ كاملاً مستحقًّا.
        to_cancel.sudo().action_cancel()

        # 2) إنشاء الجديد + التوفيق — على نفس المعاملة
        payments = self._sst_create_payment(invoice)

        # ملاحظة: رسالة الإشعار تُهرَب وتُعرض كنص عادي في النواة، لذا
        # نفصل بشرطة — وليس بسطر جديد يبتلعه CSS.
        return self._sst_done(
            _('Payment replaced'),
            '%s: %s — %s: %s' % (
                _('Payments cancelled'), join_names(to_cancel),
                _('New payment'), join_names(payments)))

    # ---- إنشاء الدفعة عبر نافذة النواة ----------------------------------
    def _sst_create_payment(self, invoice):
        """ينشئ دفعة ويوافقها على الفاتورة عبر ``account.payment.register``.

        لماذا sudo؟
            المندوب ليس له أي صلاحية على ``account.payment`` ولا على
            ``account.payment.register`` — ووحدتنا عمدًا لا تمنحه شيئًا.
            sudo هنا مقيّد بفاتورة واحدة اجتازت ``check_access('write')``
            على أمر البيع الذي يملكه المستخدم.

        لماذا الدالة العامة ``action_create_payments``؟
            لأنها ما يستدعيه زرّ الشاشة، وتُعيد ``True`` بدل التوجيه إن
            مرّرنا ``dont_redirect_to_payments``. وبعدها نقرأ
            ``matched_payment_ids`` لنعرف ما أُنشئ — API عام كامل.

        وفي الأخير **التحقق من فعل الأثر**: ما كُتب في الدفاتر يجب أن
        يساوي ما طلبناه حرفيًّا، وإلّا رمي استثناء فتُلغى المعاملة كلها.
        """
        before = invoice.sudo().matched_payment_ids

        register = self.env['account.payment.register'].sudo().with_context(
            active_model='account.move',
            active_ids=invoice.ids,
            dont_redirect_to_payments=True,
        ).create(self._sst_register_vals())

        register.action_create_payments()

        invoice.invalidate_recordset(['matched_payment_ids', 'amount_residual',
                                      'payment_state'])
        paid = (invoice.sudo().matched_payment_ids - before).filtered(
            lambda payment: payment.state != 'canceled')
        got = sum(paid.mapped('amount'))
        if not paid or invoice.currency_id.compare_amounts(
                got, self.amount) != 0:
            raise UserError(_(
                'Expected to register %(want)s on %(move)s but %(got)s was '
                'recorded. Nothing has been saved.',
                want=format_amount(self.env, self.amount,
                                   invoice.currency_id),
                move=invoice.display_name,
                got=format_amount(self.env, got, invoice.currency_id)))
        return paid

    def _sst_register_vals(self):
        """القيم التي تمرّرها نافذة النواة — الباقي تحسبه النواة بنفسها
        (journal و payment_method_line و payment_type و partner)."""
        vals = {
            'payment_date': self.payment_date,
            'amount': self.amount,
            # اترك الفاتورة مفتوحة لو دفعنا أقلّ منها — لا شطب صامت
            'payment_difference_handling': 'open',
        }
        if self.journal_id:
            vals['journal_id'] = self.journal_id.id
        if self.memo:
            vals['communication'] = self.memo
        return vals

    # ---- الإشعار --------------------------------------------------------
    def _sst_done(self, title, message):
        """إشعار نجاح ← إغلاق نافذة الحوار ← تحديث شاشة أمر البيع خلفها.

        ``act_window_close`` هو نمط النواة نفسه: النافذة تُغلق، والإشعار
        يظهر، وشاشة أمر البيع تُعيد قراءة بياناتها **في مكانها** — بلا
        وميض وبلا فقدان موضع التمرير — فيتحدّث ``sst_payment_count``
        ويظهر زر «استبدال الدفعة» فورًا.
        """
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
