# Copyright (c) 2013, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _, qb, scrub
from frappe.query_builder.functions import IfNull
from frappe.utils import getdate, nowdate

from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
	get_accounting_dimensions,
	get_dimension_with_children,
)
from erpnext.accounts.report.financial_statements import get_cost_centers_with_children


class PartyLedgerSummaryReport:
	def __init__(self, filters=None):
		self.filters = frappe._dict(filters or {})
		self.filters.from_date = getdate(self.filters.from_date or nowdate())
		self.filters.to_date = getdate(self.filters.to_date or nowdate())

		if not self.filters.get("company"):
			self.filters["company"] = frappe.db.get_single_value("Global Defaults", "default_company")

	def run(self, args):
		if self.filters.from_date > self.filters.to_date:
			frappe.throw(_("From Date must be before To Date"))

		self.filters.party_type = args.get("party_type")
		self.party_naming_by = frappe.db.get_single_value(args.get("naming_by")[0], args.get("naming_by")[1])

		self.get_gl_entries()
		self.get_additional_columns()
		self.get_return_invoices()
		self.get_party_adjustment_amounts()

		columns = self.get_columns()
		data = self.get_data()
		return columns, data

	def get_additional_columns(self):
		"""
		Additional Columns for 'User Permission' based access control
		"""

		if self.filters.party_type == "Customer":
			self.territories = frappe._dict({})
			self.customer_group = frappe._dict({})

			customer = qb.DocType("Customer")
			result = (
				frappe.qb.from_(customer)
				.select(
					customer.name, customer.territory, customer.customer_group, customer.default_sales_partner
				)
				.where(customer.disabled == 0)
				.run(as_dict=True)
			)

			for x in result:
				self.territories[x.name] = x.territory
				self.customer_group[x.name] = x.customer_group
		else:
			self.supplier_group = frappe._dict({})
			supplier = qb.DocType("Supplier")
			result = (
				frappe.qb.from_(supplier)
				.select(supplier.name, supplier.supplier_group)
				.where(supplier.disabled == 0)
				.run(as_dict=True)
			)

			for x in result:
				self.supplier_group[x.name] = x.supplier_group

	def get_columns(self):
		columns = [
			{
				"label": _(self.filters.party_type),
				"fieldtype": "Link",
				"fieldname": "party",
				"options": self.filters.party_type,
				"width": 200,
			}
		]

		if self.party_naming_by == "Naming Series":
			columns.append(
				{
					"label": _(self.filters.party_type + "Name"),
					"fieldtype": "Data",
					"fieldname": "party_name",
					"width": 110,
				}
			)

		credit_or_debit_note = "Credit Note" if self.filters.party_type == "Customer" else "Debit Note"

		columns += [
			{
				"label": _("Opening Balance"),
				"fieldname": "opening_balance",
				"fieldtype": "Currency",
				"options": "currency",
				"width": 120,
			},
			{
				"label": _("Invoiced Amount"),
				"fieldname": "invoiced_amount",
				"fieldtype": "Currency",
				"options": "currency",
				"width": 120,
			},
			{
				"label": _("Paid Amount"),
				"fieldname": "paid_amount",
				"fieldtype": "Currency",
				"options": "currency",
				"width": 120,
			},
			{
				"label": _(credit_or_debit_note),
				"fieldname": "return_amount",
				"fieldtype": "Currency",
				"options": "currency",
				"width": 120,
			},
		]

		for account in self.party_adjustment_accounts:
			columns.append(
				{
					"label": account,
					"fieldname": "adj_" + scrub(account),
					"fieldtype": "Currency",
					"options": "currency",
					"width": 120,
					"is_adjustment": 1,
				}
			)

		columns += [
			{
				"label": _("Closing Balance"),
				"fieldname": "closing_balance",
				"fieldtype": "Currency",
				"options": "currency",
				"width": 120,
			},
			{
				"label": _("Currency"),
				"fieldname": "currency",
				"fieldtype": "Link",
				"options": "Currency",
				"width": 50,
			},
		]

		# Hidden columns for handling 'User Permissions'
		if self.filters.party_type == "Customer":
			columns += [
				{
					"label": _("Territory"),
					"fieldname": "territory",
					"fieldtype": "Link",
					"options": "Territory",
					"hidden": 1,
				},
				{
					"label": _("Customer Group"),
					"fieldname": "customer_group",
					"fieldtype": "Link",
					"options": "Customer Group",
					"hidden": 1,
				},
			]
		else:
			columns += [
				{
					"label": _("Supplier Group"),
					"fieldname": "supplier_group",
					"fieldtype": "Link",
					"options": "Supplier Group",
					"hidden": 1,
				}
			]

		return columns

	def get_data(self):
		company_currency = frappe.get_cached_value("Company", self.filters.get("company"), "default_currency")
		invoice_dr_or_cr = "debit" if self.filters.party_type == "Customer" else "credit"
		reverse_dr_or_cr = "credit" if self.filters.party_type == "Customer" else "debit"

		self.party_data = frappe._dict({})
		for gle in self.gl_entries:
			self.party_data.setdefault(
				gle.party,
				frappe._dict(
					{
						"party": gle.party,
						"party_name": gle.party_name,
						"opening_balance": 0,
						"invoiced_amount": 0,
						"paid_amount": 0,
						"return_amount": 0,
						"closing_balance": 0,
						"currency": company_currency,
					}
				),
			)

			if self.filters.party_type == "Customer":
				self.party_data[gle.party].update({"territory": self.territories.get(gle.party)})
				self.party_data[gle.party].update({"customer_group": self.customer_group.get(gle.party)})
			else:
				self.party_data[gle.party].update({"supplier_group": self.supplier_group.get(gle.party)})

			amount = gle.get(invoice_dr_or_cr) - gle.get(reverse_dr_or_cr)
			self.party_data[gle.party].closing_balance += amount

			if gle.posting_date < self.filters.from_date or gle.is_opening == "Yes":
				self.party_data[gle.party].opening_balance += amount
			else:
				if amount > 0:
					self.party_data[gle.party].invoiced_amount += amount
				elif gle.voucher_no in self.return_invoices:
					self.party_data[gle.party].return_amount -= amount
				else:
					self.party_data[gle.party].paid_amount -= amount

		out = []
		for party, row in self.party_data.items():
			if (
				row.opening_balance
				or row.invoiced_amount
				or row.paid_amount
				or row.return_amount
				or row.closing_amount
			):
				total_party_adjustment = sum(
					amount for amount in self.party_adjustment_details.get(party, {}).values()
				)
				row.paid_amount -= total_party_adjustment

				adjustments = self.party_adjustment_details.get(party, {})
				for account in self.party_adjustment_accounts:
					row["adj_" + scrub(account)] = adjustments.get(account, 0)

				out.append(row)

		return out

	def get_gl_entries(self):
		gle = qb.DocType("GL Entry")
		query = (
			qb.from_(gle)
			.select(
				gle.posting_date,
				gle.party,
				gle.voucher_type,
				gle.voucher_no,
				gle.against_voucher_type,
				gle.against_voucher,
				gle.debit,
				gle.credit,
				gle.is_opening,
			)
			.where(
				(gle.docstatus < 2)
				& (gle.is_cancelled == 0)
				& (gle.party_type == self.filters.party_type)
				& (IfNull(gle.party, "") != "")
				& (gle.posting_date <= self.filters.to_date)
			)
			.orderby(gle.posting_date)
		)

		if self.filters.party_type == "Customer":
			customer = qb.DocType("Customer")
			query = (
				query.select(customer.customer_name.as_("party_name"))
				.left_join(customer)
				.on(customer.name == gle.party)
			)
		elif self.filters.party_type == "Supplier":
			supplier = qb.DocType("Supplier")
			query = (
				query.select(supplier.supplier_name.as_("party_name"))
				.left_join(supplier)
				.on(supplier.name == gle.party)
			)

		query = self.prepare_conditions(query)
		self.gl_entries = query.run(as_dict=True)

	def prepare_conditions(self, query):
		gle = qb.DocType("GL Entry")
		if self.filters.company:
			query = query.where(gle.company == self.filters.company)

		if self.filters.finance_book:
			query = query.where(IfNull(gle.finance_book, "") == self.filters.finance_book)

		if self.filters.party:
			query = query.where(gle.party == self.filters.party)

		if self.filters.party_type == "Customer":
			customer = qb.DocType("Customer")
			if self.filters.customer_group:
				query = query.where(
					(gle.party).isin(
						qb.from_(customer)
						.select(customer.name)
						.where(customer.customer_group == self.filters.customer_group)
					)
				)

			if self.filters.territory:
				query = query.where(
					(gle.party).isin(
						qb.from_(customer)
						.select(customer.name)
						.where(customer.territory == self.filters.territory)
					)
				)

			if self.filters.payment_terms_template:
				query = query.where(
					(gle.party).isin(
						qb.from_(customer)
						.select(customer.name)
						.where(customer.payment_terms == self.filters.payment_terms_template)
					)
				)

			if self.filters.sales_partner:
				query = query.where(
					(gle.party).isin(
						qb.from_(customer)
						.select(customer.name)
						.where(customer.default_sales_partner == self.filters.sales_partner)
					)
				)

			if self.filters.sales_person:
				sales_team = qb.DocType("Sales Team")
				query = query.where(
					(gle.party).isin(
						qb.from_(sales_team)
						.select(sales_team.parent)
						.where(sales_team.sales_person == self.filters.sales_person)
					)
				)

		if self.filters.party_type == "Supplier":
			if self.filters.supplier_group:
				supplier = qb.DocType("Supplier")
				query = query.where(
					(gle.party).isin(
						qb.from_(supplier)
						.select(supplier.name)
						.where(supplier.supplier_group == self.filters.supplier_group)
					)
				)

		if self.filters.cost_center:
			self.filters.cost_center = get_cost_centers_with_children(self.filters.cost_center)
			query = query.where((gle.cost_center).isin(self.filters.cost_center))

		if self.filters.project:
			query = query.where((gle.project).isin(self.filters.project))

		accounting_dimensions = get_accounting_dimensions(as_list=False)

		if accounting_dimensions:
			for dimension in accounting_dimensions:
				if self.filters.get(dimension.fieldname):
					if frappe.get_cached_value("DocType", dimension.document_type, "is_tree"):
						self.filters[dimension.fieldname] = get_dimension_with_children(
							dimension.document_type, self.filters.get(dimension.fieldname)
						)
						query = query.where(
							(gle[dimension.fieldname]).isin(self.filters.get(dimension.fieldname))
						)
					else:
						query = query.where(
							(gle[dimension.fieldname]).isin(self.filters.get(dimension.fieldname))
						)

		return query

	def get_return_invoices(self):
		doctype = "Sales Invoice" if self.filters.party_type == "Customer" else "Purchase Invoice"
		self.return_invoices = [
			d.name
			for d in frappe.get_all(
				doctype,
				filters={
					"is_return": 1,
					"docstatus": 1,
					"posting_date": ["between", [self.filters.from_date, self.filters.to_date]],
				},
			)
		]

	def get_party_adjustment_amounts(self):
		account_type = "Expense Account" if self.filters.party_type == "Customer" else "Income Account"
		self.income_or_expense_accounts = frappe.db.get_all(
			"Account", filters={"account_type": account_type, "company": self.filters.company}, pluck="name"
		)
		invoice_dr_or_cr = "debit" if self.filters.party_type == "Customer" else "credit"
		reverse_dr_or_cr = "credit" if self.filters.party_type == "Customer" else "debit"
		round_off_account = frappe.get_cached_value("Company", self.filters.company, "round_off_account")

		if not self.income_or_expense_accounts:
			# prevent empty 'in' condition
			self.income_or_expense_accounts.append("")
		else:
			# escape '%' in account name
			# ignoring frappe.db.escape as it replaces single quotes with double quotes
			self.income_or_expense_accounts = [x.replace("%", "%%") for x in self.income_or_expense_accounts]

		gl = qb.DocType("GL Entry")
		accounts_query = self.get_base_accounts_query()
		accounts_query_voucher_no = accounts_query.select(gl.voucher_no)
		accounts_query_voucher_type = accounts_query.select(gl.voucher_type)

		subquery = self.get_base_subquery()
		subquery_voucher_no = subquery.select(gl.voucher_no)
		subquery_voucher_type = subquery.select(gl.voucher_type)

		gl_entries = (
			qb.from_(gl)
			.select(
				gl.posting_date, gl.account, gl.party, gl.voucher_type, gl.voucher_no, gl.debit, gl.credit
			)
			.where(
				(gl.docstatus < 2)
				& (gl.is_cancelled == 0)
				& (gl.voucher_no.isin(accounts_query_voucher_no))
				& (gl.voucher_type.isin(accounts_query_voucher_type))
				& (gl.voucher_no.isin(subquery_voucher_no))
				& (gl.voucher_type.isin(subquery_voucher_type))
			)
		).run(as_dict=True)

		self.party_adjustment_details = {}
		self.party_adjustment_accounts = set()
		adjustment_voucher_entries = {}
		for gle in gl_entries:
			adjustment_voucher_entries.setdefault((gle.voucher_type, gle.voucher_no), [])
			adjustment_voucher_entries[(gle.voucher_type, gle.voucher_no)].append(gle)

		for voucher_gl_entries in adjustment_voucher_entries.values():
			parties = {}
			accounts = {}
			has_irrelevant_entry = False

			for gle in voucher_gl_entries:
				if gle.account == round_off_account:
					continue
				elif gle.party:
					parties.setdefault(gle.party, 0)
					parties[gle.party] += gle.get(reverse_dr_or_cr) - gle.get(invoice_dr_or_cr)
				elif frappe.get_cached_value("Account", gle.account, "account_type") == account_type:
					accounts.setdefault(gle.account, 0)
					accounts[gle.account] += gle.get(invoice_dr_or_cr) - gle.get(reverse_dr_or_cr)
				else:
					has_irrelevant_entry = True

			if parties and accounts:
				if len(parties) == 1:
					party = next(iter(parties.keys()))
					for account, amount in accounts.items():
						self.party_adjustment_accounts.add(account)
						self.party_adjustment_details.setdefault(party, {})
						self.party_adjustment_details[party].setdefault(account, 0)
						self.party_adjustment_details[party][account] += amount
				elif len(accounts) == 1 and not has_irrelevant_entry:
					account = next(iter(accounts.keys()))
					self.party_adjustment_accounts.add(account)
					for party, amount in parties.items():
						self.party_adjustment_details.setdefault(party, {})
						self.party_adjustment_details[party].setdefault(account, 0)
						self.party_adjustment_details[party][account] += amount

	def get_base_accounts_query(self):
		gl = qb.DocType("GL Entry")
		query = qb.from_(gl).where(
			(gl.account.isin(self.income_or_expense_accounts))
			& (gl.posting_date.gte(self.filters.from_date))
			& (gl.posting_date.lte(self.filters.to_date))
		)
		return query

	def get_base_subquery(self):
		gl = qb.DocType("GL Entry")
		query = qb.from_(gl).where(
			(gl.docstatus < 2)
			& (gl.party_type == self.filters.party_type)
			& (IfNull(gl.party, "") != "")
			& (gl.posting_date.between(self.filters.from_date, self.filters.to_date))
		)
		query = self.prepare_conditions(query)
		return query


def execute(filters=None):
	args = {
		"party_type": "Customer",
		"naming_by": ["Selling Settings", "cust_master_name"],
	}
	return PartyLedgerSummaryReport(filters).run(args)
