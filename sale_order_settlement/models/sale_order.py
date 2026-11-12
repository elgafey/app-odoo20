# -*- coding: utf-8 -*-
"""تسوية أمر البيع (sale_order_settlement).

المرحلة 1 — زر واحد على شاشة أمر البيع ينفّذ بالترتيب الصحيح:
    تأكيد أمر البيع (إن كان مسودة) ← تأكيد الشحنات ← إنشاء الفاتورة ← ترحيلها

المرحلة 2 — زران يفتحان نافذة الدفعة:
    تسجيل دفعة (دفعة حقيقية موفّقة على الفاتورة)
    استبدال دفعة (إلغاء القديم + إنشاء الجديد + التوفيق في معاملة واحدة)

المرحلة 3 — زر الإرجاع (إرجاع كامل أو جزئي) بإشعار دائن تلقائي.
المرحلة 6 — زر «إضافة منتج»: سطر ← تسليم ← فاتورة مرحّلة.
المرحلة 7 — صندوق الرصيد تحت الإجمالي + زر «ردّ المال للزبون».

قواعد ثابتة في كل مكان:
    * لا تعديل واحد على نواة Odoo — نداءاتها العامة فقط.
    * كل ``sudo()`` محكوم بـ ``check_access('write')`` على العقد، ومقيّد
      بسجلات هذا الأمر وحدها.
    * كل عملية = معاملة Odoo الواحدة؛ فشل خطأ يُسقطها كلها.
    * ما قد يتغيّر بين فتح النافذة والضغط يُعاد اشتقاقه وقت التنفيذ
      من الوثائق نفسها لا من حقول العميل.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import format_amount

from .tools import join_names


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # ------------------------------------------------------------------
    #  نوع الفاتورة: نقدًا (Cash) أم آجل (Credit)
    # ------------------------------------------------------------------
    sst_invoice_type = fields.Selection(
        [('cash', 'Cash'),
         ('credit', 'Credit')],
        string='Invoice Type',
        default='cash',
        required=True,
        help="Cash — the customer pays when the invoice is issued.\n"
             "Credit — the invoice is paid later.",
    )

    # ------------------------------------------------------------------
    #  المرحلة 1: تأكيد التسليم + الفاتورة
    # ------------------------------------------------------------------
    def action_settlement_confirm_delivery_invoice(self):
        """زر واحد ينفّذ أربع خطوات:

        1) تأكيد أمر البيع إن كان مسودةً أو مرسلًا — حتى تُنشأ شحنات التسليم.

        2) تأكيد الشحنات عبر ``stock.picking.button_validate()`` القياسية.
           لو أعادت النواة نافذة تفاعلية (كمية ناقصة، طلب بصفر، backorder)
           نُعيدها للمستخدم كما هي **ونتوقف هنا** — فالكميات قرار المستخدم.
           أمّا إن كان الـ dict مجرّد **إجراء طباعة تلقائية** (الشحنات وصلت
           ``done`` فعلًا) فنخزّنه ونكمل الفوترة، ونُعيده في آخر النداء.

        3) إنشاء الفاتورة عبر نافذة النواة ``sale.advance.payment.inv``
           بطريقة ``delivered`` — نفس طريق زر «Create Invoice» الأصلي.
           فشل الإنشاء يُعزل في ``savepoint`` فرعي: التسليم المؤكَّد لا
           يُمحى، والسبب يظهر في الإشعار.

        4) ترحيل الفاتورة (``action_post``) — لأن "تأكيد الفاتورة" يعني
           Posted، وبدون ترحيل لا يمكن تسجيل دفعة عليها. والترحيل وحده
           يحتاج ``sudo``: مندوب المبيعات ليس له write على ``account.move``
           — sudo مقيّد بفواتير أمر البيع هذا حصريًا.

        تعيد دائمًا إشعارًا من نوع ``display_notification`` — أو نافذة Odoo
        إن احتاج الأمر تفاعلًا.
        """
        notes = []
        problem = False
        print_action = None

        # ---- 0) لا يعمل على أمر مقفل -------------------------------------
        if self.locked:
            raise UserError(_('Order %s is locked.') % self.display_name)

        # ---- 1) تأكيد أمر البيع إن كان مسودة --------------------------
        draft = self.filtered(lambda order: order.state in ('draft', 'sent'))
        if draft:
            draft.action_confirm()
            # تأكيد الأمر يُنشئ الشحنات ويغيّر invoice_status؛ نُبطل الكاش
            # كي لا نقرأ قيمًا قديمة من الشاشة
            self.invalidate_recordset(['picking_ids', 'invoice_status',
                                       'invoice_ids'])
            notes.append('%s: %s' % (_('Order confirmed'),
                                     join_names(draft)))

        # ---- 2) تأكيد التسليم -----------------------------------------
        pickings = self.picking_ids.filtered(
            lambda picking: picking.state not in ('done', 'cancel'))
        if pickings:
            res = pickings.button_validate()
            if isinstance(res, dict):
                if all(picking.state == 'done' for picking in pickings):
                    # طباعة تلقائية لا نافذة تفاعلية — التسليم تمّ، نكمل
                    # الفوترة ونُرجع إجراء الطباعة في آخر النداء.
                    print_action = res
                else:
                    # نافذة تفاعلية حقيقية — قرارها للمستخدم ونكتفّ هنا.
                    return res
            # التسليم يغيّر qty_delivered فتتحدّث invoice_status
            self.invalidate_recordset(['invoice_status', 'invoice_ids'])
            notes.append('%s: %s' % (_('Delivery confirmed'),
                                     join_names(pickings)))

        # ---- 3) إنشاء الفاتورة ----------------------------------------
        to_invoice = self.filtered(
            lambda order: order.invoice_status == 'to invoice')
        if to_invoice:
            # نلتقط القائمة قبل الإنشاء لنعرف ما أُنشئ حديثًا
            existing_ids = set(to_invoice.invoice_ids.ids)

            wizard = self.env['sale.advance.payment.inv'].with_context(
                active_ids=to_invoice.ids,
                active_model='sale.order',
            ).create({'advance_payment_method': 'delivered'})
            try:
                # savepoint فرعي: إن رمت النواة (مثل «لا شيء للفوترة»)
                # تراجع خطوة الفوترة وحدها ولا يُمحى ما سبقها من تأكيد
                # وتسليم — والسبب يظهر للمستخدم في الإشعار.
                with self.env.cr.savepoint():
                    wizard.create_invoices()
            except (UserError, ValidationError) as error:
                problem = True
                notes.append('%s: %s' % (_('Invoice NOT created'), error))
            else:
                to_invoice.invalidate_recordset(['invoice_ids'])
                new_ids = set(to_invoice.invoice_ids.ids) - existing_ids
                new_invoices = self.env['account.move'].browse(sorted(new_ids))
                if new_invoices:
                    notes.append('%s: %s' % (_('Invoices created'),
                                             join_names(new_invoices)))

                    # ---- 4) ترحيل الفاتورة -----------------------------
                    # sudo مقيّد بفواتير هذا الأمر حصريًا. ولو فشل الترحيل
                    # لا نُسقط العملية كلها: التسليم تمّت والفاتورة تبقى
                    # مسودة يمكن ترحيلها يدويًا، ونُبلّغ بالسبب.
                    try:
                        post_result = new_invoices.sudo().action_post()
                    except (UserError, ValidationError) as error:
                        problem = True
                        notes.append('%s: %s' % (_('Invoice NOT posted'),
                                                 error))
                    else:
                        if isinstance(post_result, dict):
                            # نافذة Odoo تفاعلية (مبلغ غير معتاد …)
                            return post_result
                        notes.append('%s: %s' % (_('Invoices posted'),
                                                 join_names(new_invoices)))
                else:
                    notes.append(_('Nothing was invoiced.'))

        # ---- لا شيء للقيام به ----------------------------------------
        if not notes:
            notes.append(_('The delivery and the invoice are already done.'))

        if print_action:
            # أثر الطباعة التلقائية يُنفَّذ على المتصفح بعد اكتمال كل شيء.
            return print_action

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Delivery and Invoice'),
                'message': '\n'.join(notes),
                'type': 'warning' if problem else 'success',
                'sticky': False,
                # نمط النواة: يُغلق أي حوار مفتوح ثم يُعيد ``model.load()``
                # على الفورم المخفي خلفه — تحديث في المكان بلا إعادة تحميل.
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    # ------------------------------------------------------------------
    #  المرحلة 2: الدفعة
    # ------------------------------------------------------------------
    sst_payment_count = fields.Integer(
        string='Payments', compute='_compute_sst_payment_count')

    @api.depends('invoice_ids')
    def _compute_sst_payment_count(self):
        """عدد الدفعات الفعّالة على فواتير هذا الأمر (تُستبعد الملغاة).

        لماذا غير مخزَّن؟ لأن ``invoice_ids`` حقل محسوب غير مخزَّن، وOdoo
        يرفض أن يعتمد حقل مخزَّن على حقل غير مخزَّن.

        ``sudo`` هنا لأن العدّاد يُقرأ على شاشة أمر البيع، ولا يجوز أن
        تتعطّل الشاشة لو غاب تصريح قراءة على ``account.payment``؛ والقراءة
        مقيّدة بفواتير هذا الأمر وحده. وهذا هو المذهب في الوحدة كلها:
        **لا يقرأ المندوب الدفعات بحسابه أبدًا** — لذلك لا يوجد أي تصريح
        على ``account.payment`` في ``security/ir.access.csv``.
        """
        for order in self:
            payments = order.sudo().invoice_ids.matched_payment_ids
            order.sst_payment_count = len(
                payments.filtered(lambda payment: payment.state != 'canceled'))

    def action_open_settlement_payment_wizard(self):
        """يفتح نافذة «تسجيل الدفعة» أو «استبدال الدفعة».

        الوضع يأتي من ``sst_mode`` في سياق الزر، ولكل وضع تحقّق مسبق يمنع
        فتح شاشة فارغة: لا فاتورة مستحقة ← لا تسجيل، ولا دفعة فعّالة ← لا
        استبدال.
        """
        self.ensure_one()

        # لا تُفتح نافذة الدفعة على أمر مقفل
        if self.locked:
            raise UserError(_('Order %s is locked.') % self.display_name)

        mode = self.env.context.get('sst_mode') or 'register'
        if mode not in ('register', 'replace'):
            mode = 'register'

        if self.state != 'sale':
            raise UserError(_(
                'Confirm the sales order before recording a payment.'))

        invoices = self.sudo().invoice_ids
        if mode == 'replace':
            invoices = invoices.filtered(
                lambda move: move.matched_payment_ids.filtered(
                    lambda payment: payment.state != 'canceled'))
            empty_message = _(
                'There is no active payment on the invoices of %s.')
        else:
            invoices = invoices.filtered(
                lambda move: move.state == 'posted'
                and not move.currency_id.is_zero(move.amount_residual))
            empty_message = _(
                'There is no posted invoice with an amount due on %s.')

        if not invoices:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Nothing to do'),
                    'message': empty_message
                               % (self.display_name or self.name),
                    'type': 'warning',
                    'sticky': False,
                },
            }

        return {
            'type': 'ir.actions.act_window',
            'name': _('Payment'),
            'res_model': 'sale.settlement.payment.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_order_id': self.id,
                'default_mode': mode,
                'default_invoice_id': invoices[0].id,
            },
        }

    # ------------------------------------------------------------------
    #  المرحلة 3: الإرجاع
    # ------------------------------------------------------------------
    sst_return_count = fields.Integer(
        string='Returns', compute='_compute_sst_return_count')

    @api.depends('picking_ids')
    def _compute_sst_return_count(self):
        """عدد شحنات الإرجاع المرتبطة بهذا الأمر.

        الإرجاع يرتبى بالأمر بلا أن نكتب شيئًا في ``sale_id``:
        ``stock.picking.sale_id`` محسوب مخزَّن يعتمد على
        ``move_ids.sale_line_id.order_id``، و ``_create_return()`` يبني
        حركاته بـ ``move.copy_data()`` فتتسرّب معها ``sale_line_id`` إلى
        حركة الإرجاع.
        """
        for order in self:
            order.sst_return_count = len(
                order.sudo().picking_ids.filtered(lambda p: p.return_id))

    def action_open_settlement_returns(self):
        """الزر الذكي «Returns» — قائمة شحنات الإرجاع الخاصة بالأمر."""
        self.ensure_one()
        returns = self.sudo().picking_ids.filtered(lambda p: p.return_id)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Returns'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', returns.ids)],
            'context': {'create': False},
        }

    def action_open_settlement_add_product_wizard(self):
        """يفتح نافذة «إضافة منتج» (المرحلة 6).

        التحقّق هنا مبكر ومقصود: نافذة تُفتح ثم تُرفض عند الضغط أسوأ من
        رسالة واحدة عند الفتح.

        * الأمر يجب أن يكون مؤكدًا وغير مقفل: ``sale_stock``
          ``_action_launch_stock_rule`` يتخطّى الأوامر المقفولة صامتًا،
          فكان السطر سيُكتب ولا تُنشأ شحنة أبدًا.
        * بوابة الكتابة ``check_access('write')`` تعيش في التنفيذ لا هنا،
          حتى لا تمنع القراءة بصلاحية كتابة.
        """
        self.ensure_one()

        if self.state != 'sale':
            raise UserError(_(
                'Confirm the sales order before adding a product.'))
        if self.locked:
            raise UserError(_(
                'Order %s is locked. Unlock it before adding a product.',
                self.display_name or self.name))

        return {
            'type': 'ir.actions.act_window',
            'name': _('Add Product'),
            'res_model': 'sale.settlement.add.product.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_order_id': self.id,
            },
        }

    def action_open_settlement_return_wizard(self):
        """يفتح نافذة الإرجاع بخيارَيها (كامل / جزئي).

        التحقّق هنا مبكر ومقصود: نافذة فارغة أسوأ من رسالة Odoo القياسية.
        ولهذا لا نضع هنا إلا ما يستحيل تغيّره بين الضغطتين؛ وكل ما قد
        يتغيّر أثناء الجلوس في النافذة يُعاد فحصه في ``action_return``
        قبل لمس أي سجل.
        """
        self.ensure_one()

        # لا تُفتح نافذة الإرجاع على أمر مقفل
        if self.locked:
            raise UserError(_('Order %s is locked.') % self.display_name)

        if self.state != 'sale':
            raise UserError(_(
                'Returns are only possible on a confirmed sales order.'))

        wizard_model = self.env['sale.settlement.return.wizard']
        pickings = wizard_model._sst_returnable_pickings(self)
        if not pickings:
            raise UserError(_(
                'There is nothing left to return on %s.',
                self.display_name or self.name))

        # الشحنة الافتراضية = الأحدث إنشاءً (id الأكبر) ما دام فيها ما
        # يُرجَع. تبديلها في النافذة يعيد بناء الجدول كاملًا عبر
        # ``_onchange_picking_id``.
        target = pickings.sorted(lambda p: p.id, reverse=True)[0]

        return {
            'type': 'ir.actions.act_window',
            'name': _('Return'),
            'res_model': 'sale.settlement.return.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_order_id': self.id,
                'default_picking_id': target.id,
                'default_line_ids': wizard_model._sst_line_commands(target),
            },
        }

    # ------------------------------------------------------------------
    #  المرحلة 7: الرصيد المستحق على شاشة أمر البيع
    # ------------------------------------------------------------------
    sst_amount_due = fields.Monetary(
        string='Amount due',
        currency_field='currency_id',
        compute='_compute_sst_amount_due',
        help="Net of what this order is still worth to us: every posted "
             "invoice minus every posted credit note.\n"
             "Positive — the customer still owes us. Negative — the money "
             "is on our side and has to go back.")

    sst_due_state = fields.Selection(
        [('none', 'Nothing posted'),
         ('settled', 'Fully settled'),
         ('collect', 'Collect from the customer'),
         ('refund', 'Give back to the customer')],
        string='Balance', compute='_compute_sst_amount_due',
        help="Drives the box drawn under the total of this order.")

    sst_due_text = fields.Char(
        string='Balance message', compute='_compute_sst_amount_due',
        help="The sentence shown in that box: the direction plus the "
             "amount, spelled out.")

    @api.depends('invoice_ids', 'invoice_ids.state',
                 'invoice_ids.amount_residual')
    def _compute_sst_amount_due(self):
        """الرصيد الصافي بيننا وبين الزبون على هذا الأمر.

        لماذا ``amount_residual`` لا ``amount_total``؟ لأن الطلب أن نعرض
        **ما بقي** لا ما صار: فاتورة دُفعت كلّها تساوي صفرًا، وإشعار
        دائن أُوفِّق بفاتورة غير مدفوعة يساوي صفرًا هو أيضًا.

        ولماذا نضع الإشارة بأيدينا؟ لأن ``amount_residual`` موجب في
        الوثيقتين معًا في Odoo 20، والعلامة السالبة الوحيدة في
        ``amount_residual_signed`` وهي مقيّدة بعملة الشركة لا بعملة الأمر.
        فنقرأ الموجب بعملة الأمر ونقلب علامة الإشعار الدائن نحن.

        ``sudo`` هنا لا يفتح بابًا: المندوب يقرأ فواتيره بقاعدة
        ``account_invoice_rule_see_personal`` وإلّا لسقطت الشاشة بـ
        ``AccessError`` على فاتورة كُتبت باسم مندوب آخر.
        """
        for order in self:
            due = 0.0
            posted = False
            for move in order.sudo().invoice_ids:
                if move.state != 'posted':
                    continue
                if move.move_type not in ('out_invoice', 'out_refund'):
                    continue
                posted = True
                due += (move.amount_residual
                        if move.move_type == 'out_invoice'
                        else -move.amount_residual)

            currency = order.currency_id
            if not posted or not currency:
                order.sst_amount_due = 0.0
                order.sst_due_state = 'none'
                order.sst_due_text = False
                continue

            order.sst_amount_due = due
            if currency.is_zero(due):
                order.sst_due_state = 'settled'
                order.sst_due_text = _(
                    'Everything is settled — nothing to collect and '
                    'nothing to give back.')
            elif due > 0.0:
                order.sst_due_state = 'collect'
                order.sst_due_text = _(
                    'Collect %(amount)s from the customer.',
                    amount=format_amount(self.env, due, currency))
            else:
                order.sst_due_state = 'refund'
                order.sst_due_text = _(
                    'Give back %(amount)s to the customer.',
                    amount=format_amount(self.env, -due, currency))

    # ------------------------------------------------------------------
    #  المرحلة 7: ردّ المال للزبون
    # ------------------------------------------------------------------
    def action_open_settlement_refund_wizard(self):
        """يفتح نافذة «ردّ المال» حين يكون الرصيد في صالح الزبون."""
        self.ensure_one()

        if self.state != 'sale':
            raise UserError(_(
                'Confirm the sales order before giving money back.'))
        if self.sst_due_state != 'refund':
            raise UserError(_(
                'There is nothing to give back to the customer on %s.',
                self.display_name or self.name))

        return {
            'type': 'ir.actions.act_window',
            'name': _('Refund Customer'),
            'res_model': 'sale.settlement.refund.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_order_id': self.id},
        }

    def _sst_refundable_journals(self):
        """دفاتر شركة الأمر القادرة على **إرسال** مال.

        نفس شرط النواة في ``_get_available_journal_ids``: نوعها ``bank``
        أو ``cash`` أو ``credit`` **و** لها سطر طريقة دفع صادر — وإلّا فلن
        تجد نافذة التسجيل أي طريقة دفع وتُسقط الدفعة.

        لا حاجة لـ ``sudo()`` هنا: مندوب المبيعات يقرأ ``account.journal``
        و ``account.payment.method.line`` بقواعد أساسية.
        """
        self.ensure_one()
        company = self.company_id or self.env.company
        journals = self.env['account.journal'].search([
            *self.env['account.journal']._check_company_domain(company),
            ('type', 'in', ('bank', 'cash', 'credit')),
        ], order='type, name')
        return journals.filtered(
            lambda j: j._get_available_payment_method_lines('outbound'))

    def _sst_default_refund_journal(self):
        """الافتراضي = **الوسيلة التي دُفع بها الأمر فعلًا**.

        إن دُفع أكثر من مرّة بوسيلتين مختلفتين نأخذ الأحدث. وإن لم يُدفع
        شيء بعد نرجع إلى أول وسيلة متاحة (الترتيب ``type, name`` فيضع
        البنك أولًا).

        الـ ``sudo()`` هنا ضروري: دفعات الفاتورة تقع على ``account.payment``
        وليس للمندوب عليها صلاحية — وفي هذه الوحدة عمدًا لا يوجد لها أي
        تصريح (راجع ``security/ir.access.csv``).
        """
        self.ensure_one()
        available = self._sst_refundable_journals()
        if not available:
            return self.env['account.journal']
        by_id = {journal.id: journal for journal in available}

        invoices = self.sudo().invoice_ids.filtered(
            lambda m: m.move_type in ('out_invoice', 'out_refund'))
        paid = invoices.matched_payment_ids.filtered(
            lambda p: p.payment_type == 'inbound')
        for payment in paid.sorted(
                key=lambda p: (p.date or fields.Date.today(), p.id),
                reverse=True):
            if payment.journal_id.id in by_id:
                # نُرجع السجل من مجموعة ``available`` لا من ``sudo``:
                # تركيبة الحقل المخزَّن لا تحبّ مزيج بيئتين.
                return by_id[payment.journal_id.id]
        return available[:1]
