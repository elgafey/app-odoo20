# -*- coding: utf-8 -*-
"""قاعدة الفوترة الصحيحة بعد الإرجاع.

المبدأ:

* ``qty_invoiced`` **تصبح أمينة** — النواة تطرح سطر الإشعار الدائن كما هو
  (``_prepare_qty_invoiced``). وهذا هو ما يُعرض في عمود Invoiced وعلى كل
  تقرير يقرأه.
* ومنع إعادة بيع البضاعة المرتجعة ينتقل إلى ``qty_to_invoice``: بسياسة
  «حسب الطلب» نطرح منه الكمية المُرجَّعة، فيبقى مقدار التسويات المالية
  كما كان تمامًا قبل هذا التغيير — لم يتغيّر زرّ الفواتير ولا
  ``invoice_status`` — لكن العدّاد صار صادقًا.

لماذا كانت النواة نفسها في حاجة إلينا؟ بسياسة «حسب الطلب»::

    qty_to_invoice = product_uom_qty - qty_invoiced

ومرّة تحلل فيها ``qty_invoiced`` المرتجع صارت ``3 - 0 = 3`` بعد إرجاع
كامل، فأعاد الزرّ **بيع المرتجعة**. والنواة لا ترى الإرجاع أصلًا لأن
``product_uom_qty`` هو المُطلَّب لا المُسلَّم.

ولو اطرحنا المرتجع من ``qty_to_invoice`` دون تفكيك نحصل على المطلوب
حرفيًّا::

    qty_to_invoice   = product_uom_qty - (qty_invoiced - المرتجع)
                     = product_uom_qty - qty_invoiced + المرتجع
    نريد             = product_uom_qty - qty_invoiced   (المفوتر فقط)
    إذن              = qty_to_invoice - المرتجع

أي أنّ ما نطرحه هنا هو **العكس تمامًا** ممّا تطرحه النواة في
``qty_invoiced``. ولهذا نمرّ على نفس شرط النواة حرفيًّا: نفس نوع
الوثيقة، نفس معالجة ``cancel`` و``invoicing_legacy``، ونفس دعوة
``_affects_qty_invoiced`` — فلو استثناه أحد الاستهلاكات لاستثناه
الطرفان معًا وبقيت المعادلة متزنة.

أمّا سياسة «حسب التسليم» فلا تحتاج تدخّلًا: ``qty_delivered`` تنقص هي
أيضًا بالإرجاع، فيختفي أثر المرتجع من المعادلة وحدها::

    (D - R) - (I - R) = D - I

تحذير تركيب: هذا الحساب يطرح المرتجع **مرة واحدة**. لا تُثبَّت هذه
الوحدة على قاعدة فيها وحدة أخرى تعيد تعريف ``_compute_qty_to_invoice``
بنفس المذهب (مثل sale_payment_flow) — وإلّا ضُرب المرتجع في اثنين.
"""

from odoo import api, models
from odoo.tools import float_compare


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    @api.depends(
        'qty_invoiced', 'qty_delivered', 'product_uom_qty', 'state',
        'order_id.invoice_overages',
        # نضيفها صراحةً لأنّ حسابنا يقرأها هو، ولو تغيّرت هذه
        # دون أن يتحرّك qty_invoiced لبقي qty_to_invoice قديمًا.
        'invoice_lines', 'invoice_lines.quantity',
        'invoice_lines.move_id.state', 'invoice_lines.move_id.move_type',
    )
    def _compute_qty_to_invoice(self):
        """نترك النواة تحسب ثم نطرح منها ما أُرجِع فعلاً.

        لا نلمس سياسة «حسب التسليم» ولا خطوط الدفعة المقدّمة ولا
        الأسطر غير المُرحَّلة — نطرح وحسب، ونقف على الصفر حتى لا
        يخرج رقم سالب في العمود.
        """
        super()._compute_qty_to_invoice()
        precision = None
        for line in self:
            refunded = line._sst_refunded_qty()
            if not refunded:
                continue
            if precision is None:
                precision = self.env['decimal.precision'].precision_get(
                    'Product Unit')
            if float_compare(refunded, 0.0,
                             precision_digits=precision) == 0:
                continue
            if line.invoice_policy != 'order':
                # سياسة «حسب التسليم» تنتهي من نفسها: المرتجع ينقص
                # qty_delivered وqty_invoiced معًا فيلغى أثره.
                continue
            line.qty_to_invoice = max(line.qty_to_invoice - refunded, 0.0)

    def _sst_refunded_qty(self):
        """كمية الوحدات المرتجعة على هذا السطر — بما يماثل النواة.

        ليست عدّة إشعار دائن: هي مجموع ما تطرحه ``_prepare_qty_invoiced``
        من إشعارات الدائن، سطرًا بسطر ونفس الشروط، لأنّ المعادلة في
        الأعلى لا تصحّ إلّا لو كان الطرفان متطابقين.
        """
        self.ensure_one()
        if self.display_type or not self.invoice_lines:
            return 0.0
        refunded = 0.0
        for invoice_line in self._get_invoice_lines():
            move = invoice_line.move_id
            if move.move_type != 'out_refund':
                continue
            if move.state == 'cancel' and \
                    move.payment_state != 'invoicing_legacy':
                continue
            if not self._affects_qty_invoiced(invoice_line):
                continue
            refunded += invoice_line.product_uom_id._compute_quantity(
                invoice_line.quantity, self.product_uom_id, round=False)
        return refunded
