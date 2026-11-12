# -*- coding: utf-8 -*-
"""نافذة «الإرجاع» — المرحلة الثالثة من sale_order_settlement.

نافذة واحدة بخيارين:

    ``full``     إرجاع كامل   — كل الكميات المتبقية في الشحنة المختارة
    ``partial``  إرجاع جزئي   — المستخدم يكتب الكميات المطلوب إرجاعها

وشريط تحذير أعلى النافذة يكتب أثر فرق المدفوعات قبل الضغط: هل يأخذ من
العميل أم يردّ عليه. والإشعار الدائن يخرج تلقائيًا عن كل إرجاع لم يُفوَّر
أثره بعد، أمّا ردّ النقد فزرّ مستقل («Refund Customer») لا يخرج شيء من
الصندوق تلقائيًا.

**ما تسلّمنا إياه النواة (مُتحقَّق منه في Odoo 20):**

* ``stock.picking._create_return()`` يبني شحنة الإرجاع كاملة: حقل
  ``return_id`` يشير إلى الشحنة الأصلية، و``origin_returned_move_id`` على
  كل حركة تشير إلى حركة الأصل، والمواقع معكوسة، ورسالة في سجلّ المتابعة
  تربط الشحنتين. **الحركات تصل بكمية صفر** — فضبط الكميات واجبتنا.
* ``stock.move._prepare_return_data()`` (sale_stock) يعيد
  ``remaining_delivered_qty`` = ما سُلِّم ناقص ما سُبق إرجاعه؛ وهو مصدر
  الحقيقة الوحيد للاحترام (يمنع الإرجاع المزدوج).
* ``sale.order.line.create()`` على أمر مؤكد يُنشئ شحن البضاعة الجديدة
  تلقائيًا — وهي حقيقة تستفيد منها نافذة «Add Product» لا هذه النافذة.

**الصلاحيات:** نداءات المخزون كلها خلف ``sudo()`` مقيّد بشحنة هذا الأمر
وحده. زر «Validate» في النواة يحمل ``groups="stock.group_stock_user"`` —
أي أن مندوب المبيعات العادي لا يملك تصحيح الشحنات، ومن دون sudo كان
الإرجاع يرمي ``AccessError`` عند كل مستخدم لا يملك مجموعة المخزون.
و``sudo()`` لا يغيّر المستخدم الحالي: ``create_uid``/``write_uid``
تبقى على المستخدم الحقيقي فلا يختفي الأثر.
"""

from odoo import _, api, fields, models, Command
from odoo.exceptions import UserError, ValidationError
from odoo.tools import clean_context, format_amount
from odoo.tools.float_utils import float_compare

from odoo.addons.sale_order_settlement.models.tools import join_names


class SaleSettlementReturnWizard(models.TransientModel):
    _name = 'sale.settlement.return.wizard'
    _description = 'Return products from a Sales Order'

    # ------------------------------------------------------------------
    #  الحقول
    # ------------------------------------------------------------------
    order_id = fields.Many2one(
        'sale.order', string='Sales Order', required=True,
        readonly=True, ondelete='cascade')

    mode = fields.Selection(
        [('full', 'Full return'),
         ('partial', 'Partial return')],
        string='Return type', required=True, default='full',
        help="Full return — every quantity that is still returnable.\n"
             "Partial return — only the quantities you enter.")

    # ملاحظة: ليس له ``required=True`` على مستوى الحقل رغم أنه مخزَّن.
    # حقل محسوب غير ملزوم قد يُركَّب بقيمة فارغة فيفشل INSERT بـ
    # NOT NULL مباشرة. فالإلزام يعيش في الشاشة، والتحقّق يعيش في
    # ``action_return``.
    picking_id = fields.Many2one(
        'stock.picking', string='Delivery to return',
        domain="[('id', 'in', suitable_picking_ids)]")
    suitable_picking_ids = fields.Many2many(
        'stock.picking', compute='_compute_suitable_picking_ids')

    reason_id = fields.Many2one(
        'return.reason', string='Reason',
        help="Shown on the return transfer (Sale Stock adds this field "
             "to stock.picking).")

    # ------------------------------------------------------------------
    #  المرحلة 7 — قيمة البضاعة المرتجعة (للعرض فقط)
    # ------------------------------------------------------------------
    currency_id = fields.Many2one(
        related='order_id.currency_id', string='Currency')

    settlement_amount = fields.Monetary(
        string='Value of the returned goods', currency_field='currency_id',
        compute='_compute_settlement_amount',
        help="Tax-included value of the quantities you are about to "
             "return. Read only — it tells you what this return is "
             "worth, and the banner on top of this window tells you "
             "what it does to the money.")

    payment_warning = fields.Char(
        string='Payment difference',
        compute='_compute_payment_warning',
        help="What this button will do to the money. It is written on "
             "the banner at the top of this window and changes with "
             "every edit, before you press anything.")

    line_ids = fields.One2many(
        'sale.settlement.return.wizard.line', 'wizard_id',
        string='Lines')

    # ------------------------------------------------------------------
    #  قيمة البضاعة المرتجعة (للعرض فقط)
    # ------------------------------------------------------------------
    @api.depends('line_ids', 'line_ids.qty_to_return')
    def _compute_settlement_amount(self):
        for wizard in self:
            wizard.settlement_amount = wizard._sst_returned_value()

    def _sst_returned_value(self):
        """قيمة الكميات المرتجعة **شاملة الضريبة** لعرضها في النافذة.

        لا نخمّن: نمشي إلى سطر أمر البيع الأصلي عبر
        ``stock.move.sale_line_id`` فيأخذ السعر والخصم والضرائب من حيث
        أخذتها الفاتورة فعلًا، ثم نمرّرها إلى ``account.tax.compute_all``
        — وهو النداء الذي تحسب به النواة نفسها ``price_total``.

        * لا يدخل في هذا الحساب إلا ما كانت ``qty_to_return`` فيه موجبة.
        * حقل غير مخزَّن، فيُعاد حسابه مع كل تعديل للكميات فلا يبهت.
        """
        self.ensure_one()
        currency = self.order_id.currency_id
        total = 0.0
        for line in self.line_ids:
            if line.qty_to_return <= 0:
                continue
            sale_line = line.move_id.sale_line_id
            if not sale_line:
                continue
            price_unit = sale_line.price_unit * (
                1.0 - (sale_line.discount or 0.0) / 100.0)
            taxes = sale_line.tax_ids.compute_all(
                price_unit,
                currency=currency,
                quantity=line.qty_to_return,
                product=sale_line.product_id,
                partner=sale_line.order_id.partner_invoice_id,
            )
            total += taxes['total_included']
        return currency.round(total) if currency else total

    # ------------------------------------------------------------------
    #  شريط تحذير فرق المدفوعات
    # ------------------------------------------------------------------
    @api.depends('settlement_amount', 'order_id',
                 'order_id.sst_amount_due',
                 'line_ids', 'line_ids.qty_to_return')
    def _compute_payment_warning(self):
        for wizard in self:
            wizard.payment_warning = wizard._sst_payment_warning()

    def _sst_payment_warning(self):
        """النصّ الذي يشرح للمستخدم ماذا سيحدث للّفلوس قبل أن يضغط.

        الحارس حسابيّ لا مُخمَّن — يكرّر بالضبط ما سيفعله ``_sst_settle``
        لاحقًا، من نفس الأرقام ومن دون تنفيذ شيء.

        صار من الأسئلة الوحيدة: هل يصير الزبون مدينًا لنا أم نصير نحن
        مدينين له؟ والإجابة حسابية::

            الرصيد بعد العملية = الرصيد الحالي − قيمة ما سيُنسب

        فالإشعار الدائن يضيف جانبًا سالبًا بمقدار ما ننسبه تمامًا، ثم
        يُوفَّق فتنتقل نفس القيمة إلى جانب الفواتير والفرق صفر. ومن ثم:

        * لا وثيقة أصلًا ← لا شيء يُحاسَب، وفقط البضاعة تعود.
        * الرصيد بعد العملية موجبًا ← ما زال على الزبون أن يدفع.
        * الرصيد يساوي صفرًا ← كل شيء يستوي بعد الإرجاع.
        * الرصيد يصير سالبًا ← صار علينا أن نردّ، وهذا ما يقوله
          الشريط الأحمر تحت الإجمالي بعد التنفيذ.
        """
        self.ensure_one()
        currency = self.currency_id
        precision = self.env['decimal.precision'].precision_get(
            'Product Unit')
        amount = self.settlement_amount or 0.0
        if not currency or currency.is_zero(amount):
            return _('Nothing to settle: what you selected is worth 0.')

        # القيمة التي ستكتبها الدفاتر فعلًا — لا الظاهرة في السطر.
        credit = self._sst_credit_note_value(precision)
        if currency.is_zero(credit):
            posted = self.order_id.sudo().invoice_ids.filtered(
                lambda move: move.state == 'posted')
            if not posted:
                return _(
                    'Only the goods go back: %(order)s has no invoice '
                    'yet, so no return invoice can be created.',
                    order=self.order_id.display_name)
            return _(
                'Only the goods go back: what you selected is not '
                'invoiced on %(order)s, or has already been credited.',
                order=self.order_id.display_name)

        after = self.order_id.sst_amount_due - credit
        if currency.compare_amounts(after, 0.0) > 0:
            return _(
                'A credit note of %(credit)s will be posted. Afterwards '
                'the customer still owes %(left)s.',
                credit=format_amount(self.env, credit, currency),
                left=format_amount(self.env, after, currency))
        if currency.is_zero(after):
            return _(
                'A credit note of %(credit)s will be posted and offset. '
                'Nothing left to collect, nothing to give back.',
                credit=format_amount(self.env, credit, currency))
        return _(
            'A credit note of %(credit)s will be posted. Afterwards '
            '%(back)s is owed back to the customer — give it back later '
            'with the Refund Customer button on the order.',
            credit=format_amount(self.env, credit, currency),
            back=format_amount(self.env, -after, currency))

    # ------------------------------------------------------------------
    #  الشحنات التي يُسمح إرجاعها
    # ------------------------------------------------------------------
    @api.depends('order_id')
    def _compute_suitable_picking_ids(self):
        for wizard in self:
            wizard.suitable_picking_ids = \
                wizard._sst_returnable_pickings(wizard.order_id)

    @api.model
    def _sst_returnable_pickings(self, order):
        """شحنات هذا الأمر التي ما زال فيها كمية قابلة للإرجاع.

        ثلاث شروط، كلها مستمدة من النواة لا من اجتهادنا:

        * ``picking_type_code == 'outgoing'`` — الإرجاع يردّ بضاعة خرجت
          للعميل فقط (لا إيصال مورّد ولا تحويل داخلي).
        * ``state == 'done'`` — شحنات لم تُصَحَّح بعد لا شيء يُرجع منه.
        * وجود حركة منتهية ``remaining_delivered_qty > 0`` — وإلا كانت
          الشحنة مُرجَعَة بالفعل ونعرض شاشة فارغة بلا سبب.

        نمرّ على كل هذا بـ ``sudo`` لأننا نبني منطقًا للعرض لا نقرأ
        بضاعة بصلاحية أوسع؛ وكل ما يعود هو قائمة معرّفات تدخل دومينًا.
        """
        result = self.env['stock.picking']
        if not order:
            return result
        for picking in order.sudo().picking_ids:
            if picking.picking_type_code != 'outgoing' or picking.state != 'done':
                continue
            for move in picking.move_ids:
                if move.state != 'done':
                    continue
                if move._prepare_return_data()['remaining_delivered_qty'] > 0:
                    result |= picking
                    break
        return result

    # ------------------------------------------------------------------
    #  بناء سطور الكمية
    # ------------------------------------------------------------------
    @api.model
    def _sst_line_commands(self, picking):
        """أوامر إنشاء ``(0, 0, vals)`` لسطور الكمية القابلة للإرجاع.

        * ``qty_delivered``  — ``move.quantity``، أي ما سُلِّم فعلًا.
        * ``qty_remaining``  — ما بقي قابلًا للإرجاع بعد طرح ما سبق
          إرجاعه (``_prepare_return_data``).
        * ``qty_to_return``  — ``remaining``، وهي القيمة الصحيحة الوحيدة
          في الوضع «كامل». أمّا الوضع الجزئي فيصفّرها ``_onchange_mode``
          عند التبديل لأن طلب صاحب المشروع أن يكتب المستخدم بيده.

        لقطة داخلة في السطر لا حقلًا محسوبًا: فالسطر مؤقت يُقرأ أثناء
        الكتابة فقط، ولو جعلناه محسوبًا لتغيّر تحت يد المستخدم بعد
        إغلاق وإعادة فتح النافذة.
        """
        commands = []
        for move in picking.move_ids:
            if move.state != 'done':
                continue
            remaining = move._prepare_return_data()['remaining_delivered_qty']
            if remaining <= 0:
                continue
            commands.append((0, 0, {
                'move_id': move.id,
                'qty_delivered': move.quantity,
                'qty_remaining': remaining,
                'qty_to_return': remaining,
            }))
        return commands

    @api.onchange('picking_id')
    def _onchange_picking_id(self):
        """تبديل الشحنة = إعادة بناء الجدول بالكامل.

        بدون هذا كان المستخدم يستطيع أن يختار شحنة ثانية وتبقى سطور
        الشحنة الأولى تحت يده — أي يرجع بضاعة من شحنة لا يملكها. ولهذا
        أيضًا يتحقّق ``action_return`` أن كل سطر منتمٍ للشحنة المختارة،
        فالحماية مزدوجة: تطابق في العرض وتحقّق في التنفيذ.
        """
        for wizard in self:
            wizard.line_ids = [(5, 0, 0)] + \
                wizard._sst_line_commands(wizard.picking_id)

    @api.onchange('mode')
    def _onchange_mode(self):
        """الوضع الجزئي يبدأ من الصفر — المستخدم يكتب بيده.

        طلب صاحب المشروع حرفيًّا: «ولو العميل لما يختار إرجاع جزئي
        خلي الاعداد qty to return 0 وهو يكتب بايده منع للاخطاء».

        * ``full``    ← ``remaining`` (لا يُقرأ أصلًا، لكنه صواب).
        * ``partial`` ← ``0.0``، والمستخدم يكتب ما يريد.
        """
        for wizard in self:
            if wizard.mode == 'partial':
                for line in wizard.line_ids:
                    line.qty_to_return = 0.0
            else:
                for line in wizard.line_ids:
                    line.qty_to_return = line.qty_remaining

    # ------------------------------------------------------------------
    #  التنفيذ
    # ------------------------------------------------------------------
    def _sst_wanted_quantities(self, picking):
        """{move_id: quantity} — ما سيُرجَع فعلاً ومن أين.

        وضع «كامل» لا يقرأ ``qty_to_return`` من الشاشة أصلًا: يقرأ
        القيمة الحية من النواة، فلا ينسخ رقمًا قديمًا لو عدّل المستخدم
        الكميات ثم رجع إلى الوضع الكامل.
        """
        self.ensure_one()
        wanted = {}
        dp = self.env['decimal.precision'].precision_get('Product Unit')

        if self.mode == 'full':
            for move in picking.move_ids:
                if move.state != 'done':
                    continue
                remaining = move._prepare_return_data()[
                    'remaining_delivered_qty']
                if float_compare(remaining, 0.0, precision_digits=dp) > 0:
                    wanted[move.id] = remaining
            if not wanted:
                raise UserError(_(
                    "Nothing is left to return on transfer %s.",
                    picking.display_name))
            return wanted

        for line in self.line_ids:
            if float_compare(line.qty_to_return, 0.0,
                             precision_digits=dp) <= 0:
                continue
            move = line.move_id.sudo()
            if move.picking_id.id != picking.id:
                raise UserError(_(
                    "The line of %s does not belong to transfer %s.",
                    line.product_id.display_name, picking.display_name))
            remaining = move._prepare_return_data()[
                'remaining_delivered_qty']
            if float_compare(line.qty_to_return, remaining,
                             precision_digits=dp) > 0:
                raise UserError(_(
                    "You cannot return more than %s %s of %s on transfer %s "
                    "(the rest was already returned).",
                    remaining, line.uom_id.display_name,
                    line.product_id.display_name, picking.display_name))
            wanted[move.id] = line.qty_to_return

        if not wanted:
            raise UserError(_(
                "Enter at least one quantity to return."))
        return wanted

    def action_return(self):
        """ينفّذ الإرجاع كله في معاملة Odoo الواحدة.

        الترتيب ليس عشوائيًا: شحنة الإرجاع أولًا، ثم إشعار الدائن
        وتوفيقه. ولو تعثّر أي خطأ يرمي Odoo استثناءً فتُلغى المعاملة
        كلّها — لا يبقى إرجاع بلا أثر ولا أثر بلا إرجاع.

        يعيد دائمًا إشعارًا من نوع ``display_notification``، أو نافذة
        Odoo إن احتاج الأمر تفاعلًا.
        """
        self.ensure_one()

        # ---- 0) تنظيف السياق قبل أي عملية مخزون ----------------------
        # هذه أهم سطر في الملف. شاشة الأمر تفتح النافذة بسياق فيه
        # ``default_picking_id`` و ``default_order_id`` و
        # ``default_line_ids`` — وهذه مفاتيح إنشائية لحقول تحمل الأسماء
        # نفسها. ``sale.order.line.create`` و ``stock.picking.copy``
        # يناديان ``default_get`` لكل حقل غائب عن قيم الكتابة، فأي حقل
        # غير ممرَّر صراحةً يأخذ قيمته الافتراضية **من السياق**.
        #
        # و``stock.rule._get_stock_move_values()`` لا يضع ``picking_id``
        # في قيم الحركة، فتقرأه ``stock.move.create`` من
        # ``default_get('picking_id')`` ← ``default_picking_id`` ← أي
        # شحنة الأمر **ولو كانت قد انتهت بالفعل**. والعَرَض: تُحقن حركة
        # داخل شحنة التسليم المنتهية فتعود إلى حالة ``assigned``، ولا
        # يخرج شحن جديد، ويرتدّ ``button_validate`` بنافذة
        # «Create Backorder?» — أي نصف عملية بدل واحدة.
        #
        # ``clean_context`` أدوات النواة نفسها، وتُستعمل في
        # ``stock.move._action_confirm`` بالضبط لهذه الحالة. ننظّف
        # السياق هنا مرة واحدة فتصان كل ما بعدها. ويحذف مفاتيح
        # ``default_*`` فقط — فيبقى ``lang`` و ``tz`` و ``uid`` و
        # ``allowed_company_ids`` كما هي.
        self = self.with_context(clean_context(self.env.context))

        order = self.order_id
        notes = []

        # ---- 1) التحقّق قبل أن نلمس أي بيانات ------------------------
        # قراءة ``order.state`` بحساب المستخدم العادي هي بحد ذاتها
        # التحقّق من الصلاحية: قاعدة sales_team.group_sale_salesman تمنع
        # رؤية أمر لا يخصه، فيرمي Odoo AccessError هنا قبل أي sudo.
        if order.state != 'sale':
            raise UserError(_(
                'Returns are only possible on a confirmed sales order.'))

        if not self.picking_id:
            raise UserError(_('Choose a delivery to return.'))

        picking = self.picking_id.sudo()
        if picking.id not in order.sudo().picking_ids.ids:
            raise UserError(_(
                'Transfer %s does not belong to this sales order.',
                picking.display_name))
        if picking.state != 'done':
            raise UserError(_(
                'Transfer %s has not been validated yet.',
                picking.display_name))
        if picking.picking_type_id.code != 'outgoing':
            raise UserError(_(
                'Only delivery transfers can be returned.'))

        wanted = self._sst_wanted_quantities(picking)

        # إعادة تأكيد انتماء سطور الشاشة للشحنة المختارة — في الوضعين
        # معًا: الحماية مزدوجة (عرض + تنفيذ).
        for wline in self.line_ids:
            if wline.move_id.picking_id.id != picking.id:
                raise UserError(_(
                    'The line of %s does not belong to transfer %s.',
                    wline.product_id.display_name, picking.display_name))

        # ---- 2) شحنة الإرجاع ------------------------------------------
        # كل ما بعدها خلف sudo: راجع ترويسة الملف.
        return_picking = picking._create_return()
        return_picking.return_reason_id = self.reason_id.id

        orphaned = set(wanted)
        extra_moves = return_picking.move_ids.browse()
        for move in return_picking.move_ids:
            origin_id = move.origin_returned_move_id.id
            if origin_id in wanted:
                move.product_uom_qty = wanted[origin_id]
                orphaned.discard(origin_id)
            else:
                # سطر لم نطلبه (إرجاع جزئي). نحذفه لا نصفّيه: لو تركنا
                # حركة ``product_uom_qty == 0`` لأعادت ``action_confirm``
                # نافذة «Zero Demand Warning» كنافذة تفاعلية توقف نصف
                # العملية. والحذف ممكن هنا لأن الشحنة كلها خلف ``sudo``.
                extra_moves |= move
        if orphaned:
            # دفاع: لم تُنشئ النواة حركة مقابلة لحركة منتهية. تعذّر
            # الإرجاع كله، والمعاملة تُلغى.
            raise UserError(_(
                'The return transfer could not be built for %d move(s).',
                len(orphaned)))
        if extra_moves:
            extra_moves.unlink()

        # ``button_validate`` مباشرة على الشحنة وهي **مسودة**. لا ننادي
        # ``action_assign`` قبلها عمدًا: في النواة يملأ ``button_validate``
        # ``move.quantity`` من ``product_uom_qty`` فقط للشحنات المسودة،
        # فإن سبقنا بـ ``action_assign`` لصارت ``assigned`` وسقطت تلك
        # الخطوة، وبقيت الكمية صفرًا فرفضت ``_sanity_check`` الإرجاع
        # كله. والإرجاع إدخال لا حجز، فلا يحتاج احتياطًا ولا مخزونًا.
        res = return_picking.with_context(
            skip_backorder=True).button_validate()

        if return_picking.state != 'done':
            # حارس دفاعي: لم تصل الشحنة إلى ``done`` رغم أنّ كل شيء كان
            # مضبوطًا. لا نُكمل عملية الإرجاع ونسكّ المعاملة كلها.
            raise UserError(_(
                'Transfer %s could not be validated.',
                return_picking.display_name))

        # ---- 3) التسوية المالية + أثر مرئي في أمر البيع ---------------
        notes.append('%s: %s' % (_('Return transferred'),
                                 return_picking.display_name))
        notes.extend(self._sst_settle(wanted))

        # سطر واحد في سجلّ المتابعة يقول من سجّل ماذا ومتى.
        # ``message_post`` ينادي ``check_access('write')`` على الأمر
        # بحساب المستخدم نفسه فليس هنا تجاوز لصلاحية أحد. ويُكتب قبل
        # فحص ``res`` حتى يُسجَّل الأثر حتى في مسار الطباعة التلقائية.
        order.message_post(
            body=_('%(mode)s recorded by %(user)s.\n%(notes)s',
                   mode=dict(self._fields['mode'].selection).get(self.mode),
                   user=self.env.user.name,
                   notes='\n'.join(notes)),
            subtype_xmlid='mail.mt_note',
        )

        if isinstance(res, dict):
            # إجراء طباعة تلقائية أعادته النواة والشحنة وصلت done فعلًا.
            return res

        # ---- 4) الإشعار -----------------------------------------------
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Return'),
                'message': '\n'.join(notes),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    # ------------------------------------------------------------------
    #  وثيقة الإرجاع (تلقائية)
    # ------------------------------------------------------------------
    def _sst_settle(self, wanted):
        """يبني الإشعار الدائن، يُرحّله، ويُوفّقه بفواتير الأمر.

        كل إرجاع لبضاعة عليها وثيقة يُخرج إشعارًا دائنًا، لأن الإشعار
        الدائن حقيقةٌ واقعة لا قرار يتخذه المستخدم. أمّا ردّ النقد
        نفسه فزرّ منفصل على أمر البيع (``sale.settlement.refund.wizard``)
        فلا يخرج شيء من الصندوق تلقائيًا.

        لماذا نبني الإشعار الدائن بأيدينا ولا نستعمل
        ``order._create_invoices(final=True)``؟

        لأن النواة لا ترى «الإرجاع» إلّا تحت سياسة فوترة **حسب
        التسليم**. بسياسة **حسب الطلب**::

            qty_to_invoice = product_uom_qty - qty_invoiced

        أي أن إعادة البضاعة لا تغيّر فيه شيئًا مهما أُرجِع. وقد عولج
        ذلك في موضعه الصحيح ``models/sale_order_line.py``: المرتجع يُخرج
        من ``qty_to_invoice`` بدل أن يُخفى من ``qty_invoiced``.

        لذلك نبني أسطر الإشعار الدائن نحن من الكميات المرتجعة،
        مستعينين بنداء النواة نفسه ``sale.order.line._prepare_invoice_line``
        مع تمرير ``quantity`` سالبة. ثم نسلك **مسلك النواة حرفيًا**:
        ننشئ فاتورة، ونقلب نوعها عبر ``action_switch_move_type()`` فتُقص
        الكميات إلى موجبة وتنقلب إلى ``out_refund`` بضرائبها الصحيحة.

        ولو تعثّر أي خطأ يرمي Odoo استثناءً فتُلغى المعاملة **كلها**:
        لا يبقى إرجاع بلا وثيقة ولا وثيقة بلا إرجاع.
        """
        self.ensure_one()

        # بوابة الوثيقة: من ينشئ سجلًّا على العقد يجب أن يملك الكتابة
        # عليه. النواة نفسها تشترط ذلك في ``_create_invoices`` فلو
        # سلّمنا ``sudo`` دون تحقّق صار بإمكان أي مندوب أن يمرّر
        # ``order_id`` لا يملكه.
        self.order_id.check_access('write')

        order = self.order_id.sudo()

        # ``qty_invoiced`` و``qty_to_invoice`` حقول محسوبة **تُخزَّن**
        # قد تغيّرت منذ فتح النافذة وبقيت في كاش ORM. نُفرّغ الكاش كلّه
        # قبل القراءة بدل الاعتماد على تتبّع الاعتمادات عبر sudo.
        self.env.invalidate_all()

        precision = self.env['decimal.precision'].precision_get(
            'Product Unit')
        if wanted is None:
            raise UserError(
                _("Internal error: missing validated return quantities."))
        credit_lines = self._sst_credit_note_lines(precision, wanted)

        if not credit_lines:
            # **ليس خطأً** — إرجاع بضاعة لم تُفوَّتر بعد أمْرٌ مشروع
            # والشحنة وحدها تكفي. نكتب سببًا بدل أن نكسر العملية.
            posted = order.invoice_ids.filtered(
                lambda move: move.state == 'posted')
            if not posted:
                if order.invoice_ids:
                    return [_(
                        'No return invoice: %(order)s has a draft invoice '
                        'only — nothing is posted to credit yet.',
                        order=order.display_name)]
                return [_('No return invoice: %s has no invoice yet.',
                          order.display_name)]
            return [_(
                'No return invoice: what you selected is not invoiced on '
                '%(order)s, or has already been credited.',
                order=order.display_name)]

        refund = self._sst_post_credit_note(order, credit_lines)
        notes = ['%s: %s' % (_('Credit note'), join_names(refund))]

        # التوفيق فقط — لا نقد يخرج من هنا.
        notes.extend(self._sst_reconcile(refund, order))
        return notes

    def _sst_credit_note_lines(self, precision, wanted):
        """سطور الإشعار الدائن: سطر لكل بضاعة مرجَّعة بكميتها المرجَّعة.

        سقف صارم واحد: لا نُقرض أبدًا أكثر مما فُوتر فعلاً — وإلّا ردّدنا
        لزبون لم يدفع شيئًا أو ردّدنا مرتين على الإرجاع نفسه. نأخذ الأصغر
        بين المرتجَع والمفوتر صافيًا، ولا نبني سطرًا للصفر.
        """
        return [
            Command.create(
                sale_line._prepare_invoice_line(quantity=-creditable))
            for sale_line, creditable in
            self._sst_creditable_quantities(precision, wanted)
        ]

    def _sst_creditable_quantities(self, precision, wanted=None):
        """[(سطر أمر البيع، الكمية القابلة للاقراض الفعلية)].

        القلب: نأخذ الأصغر بين المطلوب إرجاعه والمفوتر صافيًا على سطر
        الأمر، ولا نُرجع شيئًا إن كان الصفر. والكمية تُحوَّل أولًا إلى
        وحدة سطر أمر البيع لأن الإشعار الدائن وثيقة مبيعات لا مخزون.

        ``wanted=None`` مسار الشريط (onchange) فقط: يقرأ من سطور الشاشة.
        ``wanted`` مسار التنفيذ: يقرأ من قاموس الكميات المُصادَق عليها
        وقت التنفيذ — لا من شيء وصل من العميل.
        """
        self.ensure_one()
        order = self.order_id.sudo()
        result = []
        if wanted is None:
            for wline in self.line_ids:
                if float_compare(wline.qty_to_return, 0.0,
                                 precision_digits=precision) <= 0:
                    continue
                move = wline.move_id.sudo()
                sale_line = move.sale_line_id
                if not sale_line or sale_line.order_id != order:
                    continue
                wanted_qty = wline.uom_id._compute_quantity(
                    wline.qty_to_return, sale_line.product_uom_id,
                    round=False)
                creditable = min(wanted_qty, self._sst_billed_qty(sale_line))
                if float_compare(creditable, 0.0,
                                 precision_digits=precision) > 0:
                    result.append((sale_line, creditable))
        else:
            for move_id, return_qty in wanted.items():
                move = self.env['stock.move'].sudo().browse(move_id)
                sale_line = move.sale_line_id
                if not sale_line or sale_line.order_id != order:
                    continue
                wanted_qty = move.uom_id._compute_quantity(
                    return_qty, sale_line.product_uom_id, round=False)
                creditable = min(wanted_qty, self._sst_billed_qty(sale_line))
                if float_compare(creditable, 0.0,
                                 precision_digits=precision) > 0:
                    result.append((sale_line, creditable))
        return result

    def _sst_credit_note_value(self, precision, wanted=None):
        """قيمة الإشعار الدائن التي **ستُنشأ فعلًا** ضريبةً محسوبة.

        ليست ``settlement_amount``: تلك قيمة كل ما اختره المستخدم،
        وهذه قيمة ما هو قابل للافتراض. الفرق عملي لا نظري — سطر لم
        يُفوتر بعد لا يدخل الإشعار الدائن أصلًا، ولو نبّهنا الشريط إلى
        مبلغ لن يخرج من الصندوق أبدًا لكانت النصيحة أكذوبة.
        """
        self.ensure_one()
        currency = self.order_id.sudo().currency_id
        total = 0.0
        for sale_line, creditable in self._sst_creditable_quantities(
                precision, wanted):
            price_unit = sale_line.price_unit * (
                1.0 - (sale_line.discount or 0.0) / 100.0)
            taxes = sale_line.tax_ids.compute_all(
                price_unit,
                currency=currency,
                quantity=creditable,
                product=sale_line.product_id,
                partner=sale_line.order_id.partner_invoice_id,
            )
            total += taxes['total_included']
        return currency.round(total) if currency else total

    @api.model
    def _sst_billed_qty(self, sale_line):
        """الكمية المفوترة **صافيًا** — ما فُوتر ولم يُرجَّع بعد.

        تُحسب من الوثائق المُرحَّلة وحدها: مسودّة لم يترتب عليها مال،
        فإقراض العميل ضدها إعطاء مال لم يُجمع.
        """
        billed = 0.0
        for invoice_line in sale_line.invoice_lines:
            move = invoice_line.move_id
            if move.state != 'posted':
                continue
            qty = invoice_line.product_uom_id._compute_quantity(
                invoice_line.quantity, sale_line.product_uom_id, round=False)
            if move.move_type == 'out_invoice':
                billed += qty
            elif move.move_type == 'out_refund':
                billed -= qty
        return max(billed, 0.0)

    def _sst_post_credit_note(self, order, line_commands):
        """ينشئ الإشعار الدائن ويُرحّله — بنفس مسار النواة خطوة بخطوة.

        ``_prepare_invoice`` يعطي الترويسة (الشريك والضرائب والفترة
        واليومية…) و ``_create_account_invoices`` هو الذي ينشئ فعلًا
        خلف ``sudo`` — وهذا sudo مقيَّد مسبقًا بفحص ``check_access``
        في ``_sst_settle``.
        """
        self.ensure_one()
        invoice_vals = order._prepare_invoice()
        invoice_vals['invoice_line_ids'] = line_commands
        # علامة تُميّز وثيقة الإرجاع التي نصنعها بأنفسنا عن أية
        # فاتورة ائتمان كتبها محاسب بنفس الطريقة.
        invoice_vals['sst_return_credit'] = True

        refund = order._create_account_invoices([invoice_vals])
        if not refund:
            raise UserError(_(
                'The credit note could not be created on %s.',
                order.display_name))

        # فاتورة بكميات سالبة ← قلب النوع: تُقصّ الكميات إلى موجبة
        # ويُقلَب ``extra_tax_data`` للضرائب المضمومة في السعر. ثم
        # الرابط إلى الفاتورة الأصلية كما تفعل النواة في ``_create_invoices``.
        # النواة نفسها تحرس القلب بـ ``amount_total < 0``: سطر صفر
        # الإجمالي (بضاعة مجانية / خصم 100%) يبقى بكمياته السالبة
        # وهو مرحَّل — وهذا سلوك النواة نفسها في ``final=True``.
        if refund.amount_total < 0:
            refund.action_switch_move_type()
        order.invoice_ids._set_reversed_entry(refund)
        refund.action_post()
        return refund

    def _sst_reconcile(self, refund, order):
        """يُوفّق الإشعار الدائن بفواتير هذا الأمر — بلا نقد.

        المنطق حسابيّ لا مُخمَّن — نضع أطراف **هذه** العملية في
        استدعاء ``reconcile()`` واحد فتتكفّل النواة بالمزايدة:

        * الفاتورة **غير مدفوعة** ← المتبقّي عليها يمتصّ الإشعار الدائن
          كله، فيستوي الرصيد ولا يبقى شيء لردّه.
        * الفاتورة **مدفوعة كلها أو بعضها** ← لا متبقٍّ يمتصّه، فيبقى
          على الإشعار الدائن متبقٍّ **بالضبط** بمقدار ما جُمِع فعلًا،
          وهو المبلغ الذي يظهر بعدها أحمرًا تحت إجمالي أمر البيع
          (``sst_due_state == 'refund'``) ومن زرّ «Refund Customer»
          وحده يخرج.

        مثال: فاتورة 460 دُفعت كلّها ← إرجاع بـ 300 ← الإشعار الدائن
        لا يجد متبقًّا يمتصّه فيبقى عليه 300 للزبون. أمّا فاتورة 460
        دُفعت 200 فمتبقّيها 260 تبتلع من الإشعار 260 فيبقى 40 فرطًا
        عليه ← 40 للزبون.
        """
        notes = []
        posted = order.invoice_ids.filtered(
            lambda m: m.state == 'posted')

        receivable = refund.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable')
        if receivable:
            # حساب واحد بالضبط: ``reconcile()`` لا يمزج حسابات مختلفة،
            # وحساب الإشعار الدائن هو المرجع.
            account = receivable[:1].account_id
            candidates = posted.line_ids.filtered(
                lambda l: l.account_id == account and not l.reconciled)
            if len(candidates) > 1:
                candidates.reconcile()

        refund.invalidate_recordset(
            ['amount_residual', 'payment_state', 'matched_payment_ids'])
        if refund.currency_id.is_zero(refund.amount_residual):
            notes.append(_('Offset against the invoices of this order.'))
        else:
            notes.append(_(
                'Still open: %(amount)s is owed back to the customer — '
                'give it back with the Refund Customer button.',
                amount=format_amount(self.env, refund.amount_residual,
                                     refund.currency_id)))
        return notes


class SaleSettlementReturnWizardLine(models.TransientModel):
    _name = 'sale.settlement.return.wizard.line'
    _description = 'Return line'

    wizard_id = fields.Many2one(
        'sale.settlement.return.wizard', string='Return', required=True,
        ondelete='cascade', index=True)
    move_id = fields.Many2one(
        'stock.move', string='Delivery move', required=True,
        ondelete='cascade')

    # حقول مرتبطة بـ ``move_id``: تظهر في الجدول بلا تخزين، وتتوقّف عن
    # العمل لو غاب ``move_id`` — ولهذا يدخل هو أيضًا في الشاشة
    # (column_invisible) حتى يقرأها المتصفّح قبل الحقول التابعة له.
    product_id = fields.Many2one(
        related='move_id.product_id', string='Product', readonly=True)
    uom_id = fields.Many2one(
        related='move_id.uom_id', string='Unit', readonly=True)

    qty_delivered = fields.Float(string='Delivered', readonly=True)
    qty_remaining = fields.Float(string='Available to return', readonly=True)
    qty_to_return = fields.Float(string='Qty to return', default=0.0)

    # ------------------------------------------------------------------
    #  الإنشاء
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        """أكمل ``qty_remaining`` حين لا تصل قيمته من العميل.

        الحقل قراءة فقط في الشاشة، ونموذج Odoo يحذف الحقول القراءة من
        مُرسَل الحفظ ما لم تحمل ``force_save="1"`` — وحيث إنّ
        ``_check_qty_to_return`` يُنفَّذ أثناء ``create`` نفسها، فالغياب
        يعني دخول ``0.0`` إلى القيد ورسالة كاذبة: يعرض الجدول «Available
        to return 3.00» ويقول التنفيذ «لا يمكنك إرجاع أكثر من 0.0».

        الشاشة ترسلها بفضل ``force_save``، وهذا هنا دفاع لبقية
        المُرسِلات: أي عميل يكتب السطر مباشرة (XML-RPC أو استيراد) يُكمل
        النقص من المصدر نفسه الذي تُحتسب منه القيمة أصلًا — الحركة —
        فلا يتغيّر منطق القيد ولا يُغطّى عليه.
        """
        for vals in vals_list:
            if 'qty_remaining' not in vals and vals.get('move_id'):
                move = self.env['stock.move'].sudo().browse(vals['move_id'])
                if move.exists():
                    vals['qty_remaining'] = \
                        move._prepare_return_data()['remaining_delivered_qty']
        return super().create(vals_list)

    # ------------------------------------------------------------------
    #  قيود
    # ------------------------------------------------------------------
    @api.constrains('qty_to_return', 'qty_remaining')
    def _check_qty_to_return(self):
        """لا يُرجَع أكثر مما سُلِّم وما سبق إرجاعه."""
        precision = self.env['decimal.precision'].precision_get(
            'Product Unit')
        for line in self:
            if line.qty_to_return < 0:
                raise ValidationError(_(
                    'The quantity to return cannot be negative.'))
            if float_compare(line.qty_to_return, line.qty_remaining,
                             precision_digits=precision) > 0:
                raise ValidationError(_(
                    'You cannot return more than %(available)s %(uom)s of '
                    '%(product)s — %(available)s is what is still '
                    'available.',
                    available=line.qty_remaining,
                    uom=line.uom_id.display_name,
                    product=line.product_id.display_name))
