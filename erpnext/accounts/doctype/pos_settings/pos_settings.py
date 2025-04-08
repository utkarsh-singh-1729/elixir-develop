# Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from collections import Counter

import frappe
from frappe import _
from frappe.model.document import Document


class POSSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.pos_field.pos_field import POSField
		from erpnext.accounts.doctype.pos_search_fields.pos_search_fields import POSSearchFields

		invoice_fields: DF.Table[POSField]
		pos_search_fields: DF.Table[POSSearchFields]
	# end: auto-generated types

	def validate(self):
		self.validate_invoice_fields()

	def validate_invoice_fields(self):
		invoice_fields = [field.fieldname for field in self.invoice_fields]
		duplicate_invoice_fields = {key for key, value in Counter(invoice_fields).items() if value > 1}

		if len(duplicate_invoice_fields):
			for field in duplicate_invoice_fields:
				frappe.throw(
					title=_("Duplicate POS Fields"), msg=_("'{0}' has been already added.").format(field)
				)
