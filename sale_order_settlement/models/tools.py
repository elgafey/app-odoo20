# -*- coding: utf-8 -*-
"""دوال مساعدة صغيرة مشتركة بين النموذج والنوافذ."""


def join_names(records):
    """اسم مقروء لكل سجل في قائمة.

    سبب وجودها: ``account.move.name`` يبقى ``False`` ما دامت الفاتورة مسودة،
    وكانت ``', '.join(records.mapped('name'))`` ترمي::

        TypeError: sequence item 0: expected str instance, bool found

    لذلك نمرّ عبر السلسلة: ``name`` ← ``display_name`` ← الرقم الوصفي.
    """
    return ', '.join(
        str(record.name or record.display_name or record.id)
        for record in records
    )
