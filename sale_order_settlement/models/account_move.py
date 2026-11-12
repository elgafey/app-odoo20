# -*- coding: utf-8 -*-
"""علامة على إشعار الدائن الذي تنشئه نافذة الإرجاع.

لماذا علامة على الإشعار الدائن أصلًا؟

لأنّ إشعار الدائن الذي تبنيه نافذتنا ليس مُميَّزًا أصلًا: هو سطر من
أسطر أمر البيع، موصول بـ ``sale_line_ids``، مبنيّ بنداء النواة نفسه
``_prepare_invoice_line`` — لو خلا عنوانه لكان قابلًا للتفريق من بين
ألف فاتورة يدويّة صنعها المحاسب بنفس الطريقة. والعلامة تقول «هذه
وثيقة إرجاع صنعها الطرف لا أمر ثانٍ قرّره محاسب»، وهي ما تستدعيه:

* دفتر الملاحظات في ``action_return`` (الوقت الذي أُنشئت فيه الوثيقة).
* أي سؤال لاحق في التقارير: هل هذا الإشعار الدائن مرتجع بضاعة أم
  تخفيض سعر؟

العلامة معلومة عن الوثيقة لا رافعة على حساب: الكميّات تُحسب من
الوثائق نفسها في ``wizard/return_wizard.py`` سواء وُجدت العلامة أم لا.
"""

from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    sst_return_credit = fields.Boolean(
        string='Sales return credit note', copy=False,
        help="Set by the Return wizard of sale_order_settlement on the "
             "credit note it creates for returned goods, marking the "
             "document as a sales return rather than a manual credit "
             "note.")
