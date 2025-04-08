# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt

from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos, get_serial_nos_from_sle_list
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import get_stock_balance_for
from erpnext.stock.report.stock_ledger.stock_ledger import (
	check_inventory_dimension_filters_applied,
	get_item_details,
	get_item_group_condition,
	get_opening_balance,
	get_opening_balance_from_batch,
	get_stock_ledger_entries,
)
from erpnext.stock.utils import (
	is_reposting_item_valuation_in_progress,
	update_included_uom_in_report,
)


def execute(filters=None):
	is_reposting_item_valuation_in_progress()
	include_uom = filters.get("include_uom")
	columns = get_columns(filters)
	items = get_items(filters)
	sl_entries = get_stock_ledger_entries(filters, items)
	item_details = get_item_details(items, sl_entries, include_uom)

	opening_row, actual_qty, stock_value = get_opening_balance_data(filters, columns, sl_entries)

	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))
	data, conversion_factors = process_stock_ledger_entries(
		filters, sl_entries, item_details, opening_row, actual_qty, stock_value, precision
	)

	update_included_uom_in_report(columns, data, include_uom, conversion_factors)
	return columns, data


def get_opening_balance_data(filters, columns, sl_entries):
	if filters.get("batch_no"):
		opening_row = get_opening_balance_from_batch(filters, columns, sl_entries)
	else:
		opening_row = get_opening_balance(filters, columns, sl_entries)

	actual_qty = opening_row.get("qty_after_transaction") if opening_row else 0
	stock_value = opening_row.get("stock_value") if opening_row else 0
	return opening_row, actual_qty, stock_value


def process_stock_ledger_entries(
	filters, sl_entries, item_details, opening_row, actual_qty, stock_value, precision
):
	data = []
	conversion_factors = []

	if opening_row:
		data.append(opening_row)
		conversion_factors.append(0)

	batch_balance_dict = frappe._dict({})

	if actual_qty and filters.get("batch_no"):
		batch_balance_dict[filters.batch_no] = [actual_qty, stock_value]

	available_serial_nos = get_serial_nos_from_sle_list(
		[sle.serial_and_batch_bundle for sle in sl_entries if sle.serial_and_batch_bundle]
	)

	for sle in sl_entries:
		update_stock_ledger_entry(
			sle, item_details, filters, actual_qty, stock_value, batch_balance_dict, precision
		)
		update_available_serial_nos(available_serial_nos, sle)
		data.append(sle)

		if filters.get("include_uom"):
			conversion_factors.append(item_details[sle.item_code].conversion_factor)

	return data, conversion_factors


def update_stock_ledger_entry(
	sle, item_details, filters, actual_qty, stock_value, batch_balance_dict, precision
):
	item_detail = item_details[sle.item_code]
	sle.update(item_detail)

	if filters.get("batch_no") or check_inventory_dimension_filters_applied(filters):
		actual_qty += flt(sle.actual_qty, precision)
		stock_value += sle.stock_value_difference

		if sle.batch_no:
			batch_balance_dict.setdefault(sle.batch_no, [0, 0])
			batch_balance_dict[sle.batch_no][0] += sle.actual_qty

		if sle.voucher_type == "Stock Reconciliation" and not sle.actual_qty:
			actual_qty = sle.qty_after_transaction
			stock_value = sle.stock_value

		sle.update({"qty_after_transaction": actual_qty, "stock_value": stock_value})

	sle.update({"in_qty": max(sle.actual_qty, 0), "out_qty": min(sle.actual_qty, 0)})

	if sle.actual_qty:
		sle["in_out_rate"] = flt(sle.stock_value_difference / sle.actual_qty, precision)
	elif sle.voucher_type == "Stock Reconciliation":
		sle["in_out_rate"] = sle.valuation_rate


def update_available_serial_nos(available_serial_nos, sle):
	serial_nos = (
		get_serial_nos(sle.serial_no)
		if sle.serial_no
		else available_serial_nos.get(sle.serial_and_batch_bundle)
	)
	key = (sle.item_code, sle.warehouse)
	if key not in available_serial_nos:
		stock_balance = get_stock_balance_for(
			sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time
		)
		serials = get_serial_nos(stock_balance["serial_nos"]) if stock_balance["serial_nos"] else []
		available_serial_nos.setdefault(key, serials)
		sle.balance_serial_no = "\n".join(serials)
		return

	existing_serial_no = available_serial_nos[key]
	for sn in serial_nos:
		if sn in existing_serial_no:
			existing_serial_no.remove(sn)
		else:
			existing_serial_no.append(sn)

	sle.balance_serial_no = "\n".join(existing_serial_no)


def get_columns(filters):
	columns = [
		{"label": _("Date"), "fieldname": "date", "fieldtype": "Datetime", "width": 150},
		{
			"label": _("Item"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 100,
		},
		{"label": _("Item Name"), "fieldname": "item_name", "width": 100},
		{
			"label": _("Stock UOM"),
			"fieldname": "stock_uom",
			"fieldtype": "Link",
			"options": "UOM",
			"width": 90,
		},
	]

	for dimension in get_inventory_dimensions():
		columns.append(
			{
				"label": _(dimension.doctype),
				"fieldname": dimension.fieldname,
				"fieldtype": "Link",
				"options": dimension.doctype,
				"width": 110,
			}
		)

	columns.extend(
		[
			{
				"label": _("In Qty"),
				"fieldname": "in_qty",
				"fieldtype": "Float",
				"width": 80,
				"convertible": "qty",
			},
			{
				"label": _("Out Qty"),
				"fieldname": "out_qty",
				"fieldtype": "Float",
				"width": 80,
				"convertible": "qty",
			},
			{
				"label": _("Balance Qty"),
				"fieldname": "qty_after_transaction",
				"fieldtype": "Float",
				"width": 100,
				"convertible": "qty",
			},
			{
				"label": _("Warehouse"),
				"fieldname": "warehouse",
				"fieldtype": "Link",
				"options": "Warehouse",
				"width": 150,
			},
			{
				"label": _("Item Group"),
				"fieldname": "item_group",
				"fieldtype": "Link",
				"options": "Item Group",
				"width": 100,
			},
			{
				"label": _("Brand"),
				"fieldname": "brand",
				"fieldtype": "Link",
				"options": "Brand",
				"width": 100,
			},
			{"label": _("Description"), "fieldname": "description", "width": 200},
			{
				"label": _("Incoming Rate"),
				"fieldname": "incoming_rate",
				"fieldtype": "Currency",
				"width": 110,
				"options": "Company:company:default_currency",
				"convertible": "rate",
			},
			{
				"label": _("Avg Rate (Balance Stock)"),
				"fieldname": "valuation_rate",
				"fieldtype": filters.valuation_field_type,
				"width": 180,
				"options": "Company:company:default_currency"
				if filters.valuation_field_type == "Currency"
				else None,
				"convertible": "rate",
			},
			{
				"label": _("Valuation Rate"),
				"fieldname": "in_out_rate",
				"fieldtype": filters.valuation_field_type,
				"width": 140,
				"options": "Company:company:default_currency"
				if filters.valuation_field_type == "Currency"
				else None,
				"convertible": "rate",
			},
			{
				"label": _("Balance Value"),
				"fieldname": "stock_value",
				"fieldtype": "Currency",
				"width": 110,
				"options": "Company:company:default_currency",
			},
			{
				"label": _("Value Change"),
				"fieldname": "stock_value_difference",
				"fieldtype": "Currency",
				"width": 110,
				"options": "Company:company:default_currency",
			},
			{"label": _("Voucher Type"), "fieldname": "voucher_type", "width": 110},
			{
				"label": _("Voucher #"),
				"fieldname": "voucher_no",
				"fieldtype": "Dynamic Link",
				"options": "voucher_type",
				"width": 100,
			},
			{
				"label": _("Batch"),
				"fieldname": "batch_no",
				"fieldtype": "Link",
				"options": "Batch",
				"width": 100,
			},
			{
				"label": _("Serial No"),
				"fieldname": "serial_no",
				"fieldtype": "Link",
				"options": "Serial No",
				"width": 100,
			},
			{
				"label": _("Serial and Batch Bundle"),
				"fieldname": "serial_and_batch_bundle",
				"fieldtype": "Link",
				"options": "Serial and Batch Bundle",
				"width": 100,
			},
			{"label": _("Balance Serial No"), "fieldname": "balance_serial_no", "width": 100},
			{
				"label": _("Project"),
				"fieldname": "project",
				"fieldtype": "Link",
				"options": "Project",
				"width": 100,
			},
			{
				"label": _("Company"),
				"fieldname": "company",
				"fieldtype": "Link",
				"options": "Company",
				"width": 110,
			},
		]
	)

	return columns


def get_items(filters):
	item = frappe.qb.DocType("Item")
	query = frappe.qb.from_(item).select(item.name).where(item.has_serial_no == 1)
	conditions = []

	if item_code := filters.get("item_code"):
		conditions.append(item.name == item_code)
	else:
		if brand := filters.get("brand"):
			conditions.append(item.brand == brand)
		if item_group := filters.get("item_group"):
			if condition := get_item_group_condition(item_group, item):
				conditions.append(condition)

	if conditions:
		for condition in conditions:
			query = query.where(condition)

	return query.run(pluck=True)
