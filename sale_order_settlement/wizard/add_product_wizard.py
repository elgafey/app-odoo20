# -*- coding: utf-8 -*-
"""نافذة «إضافة منتج» — المرحلة السادسة.

نصف «الاستبدال» المستقل عن النصف الآخر: يُنفَّذ بعد زرّ «Return» بضغطة
منفصلة، أو وحده على أمر لا مرتجع فيه.

    1. **Return**       — ما يُرجَع من البضاعة (بإمكانه الجزئي) + إشعار دائن.
    2. **Add Product**  — منتج واحد + كمية واحدة ← سطر في أمر البيع،
       ثم التسليم، ثم فاتورة مبيعات مُرحَّلة فورًا.

**ما تسلّمنا إياه النواة (مُتحقَّق منه في Odoo 20):**

* ``sale.order.line.create()`` على أمر مؤكد يمرّ إلى
  ``sale_stock._action_launch_stock_rule()`` فيُنشأ الشحن ويُؤكَّد في
  النداء نفسه — فلا نبني شحنة بأيدينا. **لكن** تخطّي الأمر ``locked``
  يكون صامتًا، فنرفض الأمر المقفل برسالة واضحة قبل الكتابة.
* ``sale.order.line._prepare_invoice_line(**optional_values)`` هو نفسه
  الذي يبني به ``_create_invoices`` أسطر فواتيره كلّها؛ وتمرير
  ``quantity=`` يحقن الكمية مباشرة في ``res`` — أي أنّنا نفاتور
  **ما اختاره المستخدم بالضبط** لا ما تحسبه السياسة لحظتها.
* ``sale.order._prepare_invoice()`` + ``_create_account_invoices()`` +
  ``action_post()`` — نفس ثلاث خطوات ``_create_invoices`` حرفيًا، لكن
  مقيّدةً بسطر واحد: لو مرّرنا ``_create_invoices()`` لفاتورت كل ما هو
  «قابل للفوترة» في الأمر.

والشريط الأصفر أعلى النافذة يكتب رقم المبلغ الذي عليه أن يحصّله من
العميل **قبل** أن يضغط — ومتى سبق أن خرج مال إلى الزبون على هذا الأمر
يظهر صافي الخطوتين معًا.
"""

from odoo import _, api, fields, models, Command
from odoo.exceptions import UserError
from odoo.tools import clean_context, format_amount
from odoo.tools.float_utils import float_compare

from odoo.addons.sale_order_settlement.models.tools import join_names


class SaleSettlementAddProductWizard(models.TransientModel):
    _name = 'sale.settlement.add.product.wizard'
    _description = 'Add a product to a Sales Order'

    # ------------------------------------------------------------------
    #  الحقول
    # ------------------------------------------------------------------
    order_id = fields.Many2one(
        'sale.order', string='Sales Order', required=True,
        readonly=True, ondelete='cascade')
    currency_id = fields.Many2one(
        related='order_id.currency_id', string='Currency')

    product_id = fields.Many2one(
        'product.product', string='Product', required=True,
        # ``combo`` ممنوعة لأنها ليست منتجًا يُباع وحده: النواة ترفض
        # سطر أمر البيع من نوع combo بلا زوجها.
        domain="[('sale_ok', '=', True), ('type', '!=', 'combo')]",
        help="The product to sell. Its price and taxes come from this "
             "order's pricelist and fiscal position, exactly like a line "
             "you type by hand.")

    quantity = fields.Float(
        string='Quantity', required=True, default=1.0,
        help="How many units the customer takes.")

    amount_total = fields.Monetary(
        string='To be invoiced', currency_field='currency_id',
        compute='_compute_amounts',
        help="Tax-included value of the invoice this button will post.")

    payment_warning = fields.Char(
        string='Payment difference',
        compute='_compute_payment_warning',
        help="Whether you have to take money from the customer or give "
             "it back. Shown on the banner before you press anything.")

    payment_net_warning = fields.Char(
        string='Net across both steps',
        compute='_compute_payment_warning',
        help="Only shown when money has already gone back to this "
             "customer on this order: the net between the return and "
             "this invoice.")

    # ------------------------------------------------------------------
    #  المعاينة (قبل التنفيذ)
    # ------------------------------------------------------------------
    def _sst_preview_line(self):
        """سطر أمر البيع **افتراضيّ** بالمنتج والكمية المكتوبَين.

        ``new()`` تبني سجلًّا بـ ``NewId`` وتُشغّل حقوله المحسوبة عند
        القراءة — فمن دون أن نكتب في قاعدة البيانات نقرأ نفس
        ``price_unit`` و ``tax_ids`` و ``price_total`` التي سيأخذها
        السطر الحقيقي من pricelist الأمر وموقعه الضريبي.
        """
        self.ensure_one()
        if not self.order_id or not self.product_id:
            return self.env['sale.order.line']
        return self.order_id.sudo().order_line.new({
            'order_id': self.order_id.id,
            'product_id': self.product_id.id,
            'product_uom_qty': self.quantity or 0.0,
        })

    @api.depends('order_id', 'product_id', 'quantity')
    def _compute_amounts(self):
        for wizard in self:
            line = wizard._sst_preview_line()
            wizard.amount_total = line.price_total if line else 0.0

    # ------------------------------------------------------------------
    #  شريط تحذير فرق المدفوعات
    # ------------------------------------------------------------------
    @api.depends('order_id', 'product_id', 'quantity', 'amount_total')
    def _compute_payment_warning(self):
        for wizard in self:
            wizard.payment_warning = wizard._sst_payment_warning()
            wizard.payment_net_warning = wizard._sst_payment_net_warning()

    def _sst_payment_warning(self):
        """الخطوة الأولى: هذا المبلغ سيُحصَّل الآن.

        هذه النافذة لا تفعل شيئًا غير أن تبيع، فالرسالة صريحة دائمًا —
        «خُذ من العميل».
        """
        self.ensure_one()
        currency = self.currency_id
        if not self.product_id or not currency:
            return ''
        return _(
            '%(amount)s will be invoiced now — take it from the '
            'customer.',
            amount=format_amount(self.env, self.amount_total, currency))

    def _sst_payment_net_warning(self):
        """الخطوة الثانية: الصافي مع مرتجع سبق أن رُدّ فيه مال.

        لا يظهر إلا إن خرج فعلًا مال **صادر** على فواتير هذا الأمر؛
        فالإشعار الدائن الذي لم يُسدَّد نقدًا لا يُغيّر شيئًا في يد
        الموظف. والصافي يُقرأ في الاتجاهين: لو كان المرتجع أكبر من هذه
        الفاتورة فالمال الباقي **يذهب للعميل**، والعكس يعني أن عليه
        أن يحصّل.
        """
        self.ensure_one()
        currency = self.currency_id
        if not currency:
            return ''
        returned = self._sst_cash_already_returned()
        if currency.is_zero(returned):
            return ''
        header = _(
            'Already returned to this customer on this order: '
            '%(returned)s.',
            returned=format_amount(self.env, returned, currency))
        net = returned - (self.amount_total or 0.0)
        if currency.is_zero(net):
            tail = _('Net across both steps: nothing is left to move.')
        elif net > 0:
            tail = _('Net across both steps: give back %(net)s.',
                     net=format_amount(self.env, net, currency))
        else:
            tail = _('Net across both steps: take %(net)s from the '
                     'customer.',
                     net=format_amount(self.env, -net, currency))
        return '%s %s' % (header, tail)

    def _sst_cash_already_returned(self):
        """مجموع ما خرج فعلًا من مال هذا الأمر إلى العميل.

        ``sudo()`` ضروريّ: ``account.payment`` ليس للمندوب عليه قراءة —
        والوحدة عمدًا لا تمنحه إيّاها — والقراءة مقيّدة بدفعات فواتير
        هذا الأمر وحدها.
        """
        self.ensure_one()
        payments = self.order_id.sudo().invoice_ids.matched_payment_ids \
            .filtered(
                lambda payment: payment.payment_type == 'outbound'
                and payment.state != 'canceled')
        return sum(payments.mapped('amount'))

    # ------------------------------------------------------------------
    #  التنفيذ
    # ------------------------------------------------------------------
    def action_add_and_invoice(self):
        """يضيف السطر ← يسلّم ← يُصدِّر الفاتورة، في معاملة واحدة.

        الترتيب حاسم:

        1) **السطر** أولًا — فلا يمكن تسليم بضاعة لم تُبَع.
        2) **التسليم** ثانيًا — نتحقّق من أن المخزون يكفي **قبل** أن
           ننادي ``button_validate``؛ وإلّا لعادت النافذة التفاعلية
           «Create Backorder?» ونصفُ العملية مكتملة والنصف الآخر ناقص.
        3) **الفاتورة** أخيرًا — بما سُلِّم فعلًا وبعدد الوحدات الذي
           كتبه المستخدم.

        ولو تعثّر أي خطأ يرمي Odoo استثناءً فتُلغى المعاملة كلها:
        لا يبقى سطر بلا فاتورة ولا فاتورة بلا سطر.
        """
        self.ensure_one()

        # بوابة الكتابة على الأمر — بلاها كان بإمكان أي يدٍ تمرير
        # ``order_id`` أن يفاتور أمرًا لا يملكه. ووضعناها هنا لا في زرّ
        # الفتح حتى لا تتأثّر القراءة بصلاحية الكتابة.
        self.order_id.check_access('write')

        self = self.with_context(clean_context(self.env.context))
        order = self.order_id

        if order.state != 'sale':
            raise UserError(_(
                'Confirm the sales order before adding a product.'))

        # تخطّي الأوامر المقفولة في النواة صامت، فالرفض هنا صريح.
        if order.locked:
            raise UserError(_(
                'Order %s is locked. Unlock it before adding a product.',
                order.display_name))

        precision = self.env['decimal.precision'].precision_get(
            'Product Unit')
        if float_compare(self.quantity, 0.0,
                         precision_digits=precision) <= 0:
            raise UserError(_('Enter a quantity greater than zero.'))

        notes = []
        print_action = None

        # ---- 1) سطر أمر البيع -----------------------------------------
        # ``sudo`` ليس ترفًا: إنشاء سطر على أمر مؤكد يستدعي ``sale_stock``
        # التي تكتب في ``stock.picking``، ومندوب المبيعات ليس له عليها
        # كتابة في النواة.
        line = order.sudo().order_line.create({
            'order_id': order.id,
            'product_id': self.product_id.id,
            'product_uom_qty': self.quantity,
        })
        order.invalidate_recordset(['picking_ids', 'invoice_status',
                                    'invoice_ids'])
        notes.append(_('%(qty)s x %(product)s added',
                       qty=self.quantity,
                       product=line.product_id.display_name))

        # ---- 2) التسليم -----------------------------------------------
        pickings = line.move_ids.picking_id.filtered(
            lambda picking: picking.state not in ('done', 'cancel'))
        if pickings:
            pickings.sudo().action_assign()
            short = []
            for move in pickings.sudo().move_ids:
                if move.state in ('done', 'cancel'):
                    continue
                if float_compare(move.quantity, move.product_uom_qty,
                                 precision_digits=precision) < 0:
                    short.append(_(
                        '%(product)s: %(asked)s asked, %(reserved)s '
                        'reserved',
                        product=move.product_id.display_name,
                        asked=move.product_uom_qty,
                        reserved=move.quantity))
            if short:
                # رفض قبل التصحيح: المعاملة كلها تُلغى ولا يبقى سطر
                # بضاعة مُباعة لم تُسلَّم.
                raise UserError(_(
                    'Not enough stock to deliver %s:\n%s',
                    order.display_name,
                    '\n'.join('* ' + item for item in short)))
            res = pickings.sudo().button_validate()
            pickings.invalidate_recordset(['state'])
            if isinstance(res, dict):
                if all(picking.state == 'done' for picking in pickings):
                    # طباعة تلقائية — التسليم تمّ، نكمل الفوترة
                    # ونُرجع إجراء الطباعة في آخر النداء.
                    print_action = res
                else:
                    # نافذة تفاعلية حقيقية — نتوقف قبل الفوترة.
                    return res
            notes.append('%s: %s' % (_('Delivered'), join_names(pickings)))

        # ---- 3) فاتورة المبيعات ---------------------------------------
        # بوابة المال نفسها التي في ``_sst_settle``.
        invoice_vals = order._prepare_invoice()
        invoice_vals['invoice_line_ids'] = [
            Command.create(line._prepare_invoice_line(
                quantity=self.quantity))]
        invoice = order._create_account_invoices([invoice_vals])
        if not invoice:
            raise UserError(_(
                'The invoice could not be created on %s.',
                order.display_name))
        invoice.sudo().action_post()
        notes.append('%s: %s' % (_('Invoice posted'),
                                 join_names(invoice)))

        # ---- 4) أثر مرئي في أمر البيع ---------------------------------
        order.message_post(
            body=_('%(product)s added by %(user)s.\n%(notes)s',
                   product=line.product_id.display_name,
                   user=self.env.user.name,
                   notes='\n'.join(notes)),
            subtype_xmlid='mail.mt_note',
        )

        if print_action:
            return print_action

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Add Product'),
                'message': '\n'.join(notes),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
