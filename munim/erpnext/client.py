from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

import httpx

from munim.erpnext.exceptions import (
    ERPNextAuthError,
    ERPNextError,
    ERPNextMutationConflict,
    ERPNextMutationNotFound,
    ERPNextMutationUncertain,
    ERPNextPDFDownloadError,
    ERPNextPermissionError,
    ERPNextTransientError,
    ERPNextValidationError,
)
from munim.erpnext.models import ERPDocument

READ_ALLOWED_DOCTYPES = {
    "Attendance",
    "Sales Invoice",
    "Sales Order",
    "Purchase Order",
    "Employee Advance",
    "Journal Entry",
    "Payroll Entry",
    "Salary Slip",
    "Customer",
    "Supplier",
    "Employee",
    "Item",
    "Item Price",
    "Company",
    "Leave Application",
    "Payment Entry",
    "GL Entry",
    "Account",
    "Bin",
    "Stock Ledger Entry",
    "Sales Invoice Item",
    "Quotation",
    "Delivery Note",
    "Purchase Invoice",
    "Print Format",
    "Pricing Rule",
    "Address",
    "GST HSN Code",
    "Sales Taxes and Charges Template",
    "Purchase Taxes and Charges Template",
    "Item Tax Template",
}

WRITE_ALLOWED_DOCTYPES = {
    "Customer",  # v2: add-customer preview, only after the Approve button
    "Address",  # a GST-registered customer's billing state (place of supply)
    "Supplier",  # v2: add-supplier preview, only after the Approve button
    "Auto Repeat",  # recurring bill, after the Approve button
    "Pricing Rule",  # a customer's permanent discount, after the Approve button
    "Attendance",
    "Sales Invoice",
    "Employee Advance",
    "Journal Entry",
    "Payroll Entry",
    "Salary Slip",
    "Quotation",
    "Sales Order",
    "Delivery Note",
    "Purchase Invoice",
    "Payment Entry",
}

ALLOWED_DOCTYPES = READ_ALLOWED_DOCTYPES
MASTER_FIELD_UPDATES: dict[str, set[str]] = {"Item": {"gst_hsn_code"}, "Pricing Rule": {"discount_percentage", "disable"}}
ALLOWED_REPORTS = {"Accounts Receivable", "General Ledger"}
MAPPER_METHODS = {  # ERPNext's own "make next document" mappers: they keep links and pending qty
    "erpnext.selling.doctype.quotation.quotation.make_sales_order",
    "erpnext.selling.doctype.quotation.quotation.make_sales_invoice",
    "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
    "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
    "erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice",
    "erpnext.accounts.doctype.sales_invoice.sales_invoice.make_delivery_note",
}
ALLOWED_METHODS = MAPPER_METHODS | {
    "erpnext.accounts.doctype.sales_invoice.sales_invoice.make_sales_return",
    "hrms.payroll.doctype.payroll_entry.payroll_entry.get_payroll_entries_for_jv",
}


class ERPNextClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=20)
        self.headers = {"Authorization": f"token {api_key}:{api_secret}"}

    async def get_logged_user(self) -> str:
        data = await self._request(
            "GET",
            "/api/method/frappe.auth.get_logged_user",
        )
        user = data.get("message")
        if not isinstance(user, str) or not user:
            raise ERPNextError("ERPNext returned an invalid authenticated user")
        return user

    async def get_document(self, doctype: str, name: str) -> ERPDocument:
        self._check_doctype(doctype)
        data = await self._request("GET", f"/api/resource/{quote(doctype)}/{quote(name)}")
        return ERPDocument(doctype=doctype, name=data["data"].get("name"), docstatus=data["data"].get("docstatus"), data=data["data"])

    async def list_documents(
        self,
        doctype: str,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        *,
        limit_page_length: int = 100,
        limit_start: int = 0,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        self._check_doctype(doctype)

        if not 1 <= limit_page_length <= 500:
            raise ERPNextValidationError(
                "limit_page_length must be between 1 and 500"
            )
        if limit_start < 0:
            raise ERPNextValidationError(
                "limit_start cannot be negative"
            )

        params: dict[str, Any] = {
            "limit_page_length": limit_page_length,
            "limit_start": limit_start,
        }

        if filters:
            params["filters"] = json.dumps(
                filters,
                separators=(",", ":"),
            )
        if fields:
            params["fields"] = json.dumps(
                fields,
                separators=(",", ":"),
            )
        if order_by:
            params["order_by"] = order_by

        data = await self._request(
            "GET",
            f"/api/resource/{quote(doctype)}",
            params=params,
        )
        return list(data["data"])

    async def list_all_documents(
        self,
        doctype: str,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        *,
        page_size: int = 100,
        max_records: int = 2000,
    ) -> list[dict[str, Any]]:
        if not 1 <= page_size <= 500:
            raise ERPNextValidationError(
                "page_size must be between 1 and 500"
            )
        if max_records < 1:
            raise ERPNextValidationError(
                "max_records must be at least 1"
            )

        records: list[dict[str, Any]] = []

        while len(records) < max_records:
            request_size = min(
                page_size,
                max_records - len(records),
            )

            page = await self.list_documents(
                doctype,
                filters=filters,
                fields=fields,
                limit_page_length=request_size,
                limit_start=len(records),
                order_by="name asc",
            )

            records.extend(page)

            if len(page) < request_size:
                break

        return records

    async def list_child_rows(self, child_doctype: str, parent_doctype: str, fields: list[str],
                              filters: list[Any] | None = None) -> list[dict[str, Any]]:
        """Child-table rows (e.g. Item Tax of every Item) in one call via frappe.client.get_list."""
        self._check_doctype(parent_doctype)
        params: dict[str, Any] = {"doctype": child_doctype, "parent": parent_doctype, "fields": json.dumps(fields),
                                  "limit_page_length": 0}
        if filters:
            params["filters"] = json.dumps(filters)
        data = await self._request("GET", "/api/method/frappe.client.get_list", params=params)
        return list(data.get("message") or [])

    async def create_draft_document(self, doctype: str, payload: dict[str, Any]) -> ERPDocument:
        self._check_write_doctype(doctype)
        safe_payload = dict(payload)
        safe_payload["docstatus"] = 0
        data = await self._mutation_request("POST", f"/api/resource/{quote(doctype)}", json=safe_payload)
        doc = data["data"]
        return ERPDocument(doctype=doctype, name=doc.get("name"), docstatus=doc.get("docstatus"), data=doc)

    async def attach_file(self, doctype: str, name: str, filename: str, content: bytes, mime: str) -> None:
        """Attach the original bill (photo/PDF) to the document it became, as a private file."""
        self._check_write_doctype(doctype)
        await self._mutation_request("POST", "/api/method/upload_file", files={"file": (filename, content, mime)},
                                     data={"doctype": doctype, "docname": name, "is_private": "1"})

    async def update_master_fields(self, doctype: str, name: str, payload: dict[str, Any]) -> ERPDocument:
        """Narrow master edits only (e.g. an item's HSN code after the Approve button)."""
        allowed = MASTER_FIELD_UPDATES.get(doctype, set())
        if not payload or set(payload) - allowed:
            raise ERPNextPermissionError(f"Master update not allowlisted: {doctype} {sorted(set(payload) - allowed)}")
        data = await self._mutation_request("PUT", f"/api/resource/{quote(doctype)}/{quote(name)}", json=payload)
        updated = data["data"]
        return ERPDocument(doctype=doctype, name=updated.get("name"), docstatus=updated.get("docstatus"), data=updated)

    async def update_draft(self, doctype: str, name: str, payload: dict[str, Any]) -> ERPDocument:
        self._check_write_doctype(doctype)
        doc = await self.get_document(doctype, name)
        if doc.docstatus != 0:
            raise ERPNextValidationError("Only draft documents may be updated")
        data = await self._mutation_request("PUT", f"/api/resource/{quote(doctype)}/{quote(name)}", json=payload)
        updated = data["data"]
        return ERPDocument(doctype=doctype, name=updated.get("name"), docstatus=updated.get("docstatus"), data=updated)

    async def submit_document(self, doctype: str, name: str) -> ERPDocument:
        self._check_write_doctype(doctype)
        return await self.call_method("frappe.client.submit", {"doc": {"doctype": doctype, "name": name}})

    async def cancel_document(self, doctype: str, name: str) -> ERPDocument:
        self._check_write_doctype(doctype)
        return await self.call_method("frappe.client.cancel", {"doctype": doctype, "name": name})

    async def run_report(self, report_name: str, filters: dict[str, Any]) -> dict[str, Any]:
        if report_name not in ALLOWED_REPORTS:
            raise ERPNextPermissionError(f"Report not allowlisted: {report_name}")
        return await self._request(
            "GET",
            "/api/method/frappe.desk.query_report.run",
            params={"report_name": report_name, "filters": json.dumps(filters, separators=(",", ":"))},
        )

    async def call_method(self, method: str, payload: dict[str, Any]) -> ERPDocument:
        if method not in ALLOWED_METHODS and method not in {"frappe.client.submit", "frappe.client.cancel"}:
            raise ERPNextPermissionError(f"Method not allowlisted: {method}")
        data = await self._mutation_request("POST", f"/api/method/{method}", json=payload)
        result = data.get("message") or data.get("data") or {}
        return ERPDocument(doctype=result.get("doctype", ""), name=result.get("name"), docstatus=result.get("docstatus"), data=result)

    async def download_pdf(self, doctype: str, name: str, print_format: str | None = None) -> bytes:
        self._check_doctype(doctype)
        params = {
            "doctype": doctype,
            "name": name,
            "no_letterhead": "0",
        }
        if print_format:  # otherwise ERPNext uses the doctype's default: the business's own design once set
            params["format"] = print_format
        response = await self.http.request(
            "GET",
            f"{self.base_url}/api/method/frappe.utils.print_format.download_pdf",
            params=params,
            headers=self.headers,
        )
        if response.status_code != 200:
            self._raise_for_status(response)
        content_type = response.headers.get("content-type", "")
        if "application/pdf" not in content_type.lower() and not response.content.startswith(b"%PDF"):
            raise ERPNextPDFDownloadError(
                "ERPNext did not return a PDF",
                upstream_status=response.status_code,
                upstream_content_type=content_type,
            )
        return response.content

    async def fetch_receivables(self, filters: dict[str, Any]) -> dict[str, Any]:
        return await self.run_report("Accounts Receivable", filters)

    async def fetch_business_summary_inputs(self, filters: dict[str, Any]) -> dict[str, Any]:
        return {"receivables": await self.fetch_receivables(filters)}

    async def list_employees(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self.list_documents(
            "Employee",
            fields=["name", "employee_name", "status", "company"],
            limit_page_length=limit,
        )

    async def list_item_prices(
        self,
        item_code: str,
        *,
        customer: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {"item_code": item_code, "selling": 1}
        if customer:
            filters["customer"] = customer
        return await self.list_documents(
            "Item Price",
            filters=filters,
            fields=["name", "item_code", "price_list_rate", "currency", "uom", "customer"],
            limit_page_length=limit,
        )

    async def list_previous_sales_invoices(
        self,
        customer: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return await self.list_documents(
            "Sales Invoice",
            filters={"customer": customer, "docstatus": 1},
            fields=["name", "customer", "posting_date", "grand_total", "currency"],
            limit_page_length=limit,
            order_by="posting_date desc",
        )

    async def list_open_sales_invoices(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return await self.list_documents(
            "Sales Invoice",
            filters={"docstatus": 1, "outstanding_amount": [">", 0]},
            fields=[
                "name",
                "customer",
                "posting_date",
                "due_date",
                "grand_total",
                "outstanding_amount",
                "status",
                "docstatus",
                "currency",
            ],
            limit_page_length=limit,
            order_by="posting_date desc",
        )

    async def list_draft_sales_invoices(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return await self.list_documents(
            "Sales Invoice",
            filters={"docstatus": 0},
            fields=[
                "name",
                "customer",
                "posting_date",
                "grand_total",
                "outstanding_amount",
                "status",
                "docstatus",
                "currency",
            ],
            limit_page_length=limit,
            order_by="modified desc",
        )

    async def list_customer_sales_invoices(self, customer: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return await self.list_documents(
            "Sales Invoice",
            filters={"customer": customer},
            fields=[
                "name",
                "customer",
                "posting_date",
                "grand_total",
                "outstanding_amount",
                "status",
                "docstatus",
                "currency",
            ],
            limit_page_length=limit,
            order_by="posting_date desc",
        )

    async def get_customer_last_invoices(self, customer: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.get_customer_recent_documents(customer, "Sales Invoice", limit)

    async def get_customer_last_quotations(self, customer: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.get_customer_recent_documents(customer, "Quotation", limit)

    async def get_customer_last_delivery_notes(self, customer: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.get_customer_recent_documents(customer, "Delivery Note", limit)

    async def get_customer_recent_documents(
        self,
        customer: str,
        doctype: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit)))
        if doctype == "Sales Invoice":
            return await self.list_documents(
                "Sales Invoice",
                filters={"customer": customer},
                fields=[
                    "name",
                    "customer",
                    "posting_date",
                    "grand_total",
                    "outstanding_amount",
                    "status",
                    "docstatus",
                    "currency",
                    "modified",
                ],
                limit_page_length=limit,
                order_by="posting_date desc",
            )
        if doctype == "Quotation":
            return await self.list_documents(
                "Quotation",
                filters={"party_name": customer},
                fields=[
                    "name",
                    "party_name",
                    "transaction_date",
                    "grand_total",
                    "status",
                    "docstatus",
                    "currency",
                    "modified",
                ],
                limit_page_length=limit,
                order_by="transaction_date desc",
            )
        if doctype == "Sales Order":
            return await self.list_documents(
                "Sales Order",
                filters={"customer": customer},
                fields=["name", "customer", "transaction_date", "grand_total", "status", "docstatus", "currency", "modified"],
                limit_page_length=limit,
                order_by="transaction_date desc",
            )
        if doctype == "Delivery Note":
            return await self.list_documents(
                "Delivery Note",
                filters={"customer": customer},
                fields=[
                    "name",
                    "customer",
                    "posting_date",
                    "grand_total",
                    "status",
                    "docstatus",
                    "currency",
                    "modified",
                ],
                limit_page_length=limit,
                order_by="posting_date desc",
            )
        raise ERPNextPermissionError(f"Document history doctype not allowlisted: {doctype}")

    async def fetch_customer_outstanding(self, customer: str, *, limit: int = 20) -> dict[str, Any]:
        invoices = await self.list_documents(
            "Sales Invoice",
            filters={"customer": customer, "docstatus": 1, "outstanding_amount": [">", 0]},
            fields=[
                "name",
                "customer",
                "posting_date",
                "grand_total",
                "outstanding_amount",
                "status",
                "docstatus",
                "currency",
            ],
            limit_page_length=limit,
            order_by="posting_date desc",
        )
        return {"customer": customer, "invoices": invoices}

    async def get_customer_outstanding(self, customer_name: str, *, limit: int = 20) -> dict[str, Any]:
        data = await self.fetch_customer_outstanding(customer_name, limit=limit)
        invoices = list(data.get("invoices") or [])
        last_invoice = invoices[0] if invoices else None
        last_payment = await self.get_customer_last_payment(customer_name)
        return {
            "customer": customer_name,
            "invoices": invoices,
            "last_invoice": last_invoice,
            "last_payment": last_payment,
        }

    async def fetch_general_ledger(
        self,
        customer: str,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {"party_type": "Customer", "party": customer}
        if from_date:
            filters["from_date"] = from_date
        if to_date:
            filters["to_date"] = to_date
        return await self.run_report("General Ledger", filters)

    async def get_item_details(self, item_code: str) -> dict[str, Any]:
        item = await self.get_document("Item", item_code)
        prices = await self.list_item_prices(item_code, limit=20)
        return {"item": item.data, "prices": prices}

    async def get_sales_summary(
        self,
        *,
        from_date: str,
        to_date: str,
        include_drafts: bool = False,
    ) -> dict[str, Any]:
        submitted = await self.list_sales_invoices_for_period(from_date=from_date, to_date=to_date)
        drafts: list[dict[str, Any]] = []
        if include_drafts:
            drafts = await self.list_documents(
                "Sales Invoice",
                filters={"docstatus": 0, "posting_date": ["between", [from_date, to_date]]},
                fields=[
                    "name",
                    "customer",
                    "posting_date",
                    "grand_total",
                    "outstanding_amount",
                    "status",
                    "docstatus",
                    "currency",
                ],
                limit_page_length=100,
                order_by="posting_date desc",
            )
        return {"submitted": submitted, "drafts": drafts}

    async def get_collection_summary(self, *, from_date: str, to_date: str) -> dict[str, Any]:
        payments = await self.list_documents(
            "Payment Entry",
            filters={
                "docstatus": 1,
                "payment_type": "Receive",
                "posting_date": ["between", [from_date, to_date]],
            },
            fields=[
                "name",
                "party",
                "party_name",
                "posting_date",
                "paid_amount",
                "received_amount",
                "mode_of_payment",
                "status",
                "docstatus",
            ],
            limit_page_length=200,
            order_by="posting_date desc",
        )
        return {"payments": payments}

    async def get_item_rate(self, item_name: str, customer_name: str | None = None) -> dict[str, Any]:
        details = await self.get_item_details(item_name)
        last_sales: list[dict[str, Any]] = []
        try:
            filters: dict[str, Any] = {"item_code": item_name, "docstatus": 1}
            if customer_name:
                filters["customer"] = customer_name
            last_sales = await self.list_documents(
                "Sales Invoice Item",
                filters=filters,
                fields=["parent", "item_code", "item_name", "qty", "rate", "amount", "creation"],
                limit_page_length=1,
                order_by="creation desc",
            )
        except ERPNextError:
            last_sales = []
        return {"item": details.get("item") or {}, "prices": details.get("prices") or [], "last_sales": last_sales}

    async def get_item_stock(self, item_name: str, warehouse: str | None = None) -> dict[str, Any]:
        filters: dict[str, Any] = {"item_code": item_name}
        if warehouse:
            filters["warehouse"] = warehouse
        bins = await self.list_documents(
            "Bin",
            filters=filters,
            fields=["name", "item_code", "warehouse", "actual_qty", "projected_qty", "stock_uom"],
            limit_page_length=100,
            order_by="warehouse asc",
        )
        return {"item": item_name, "bins": bins}

    async def get_overdue_customers(self, *, limit: int = 5) -> list[dict[str, Any]]:
        invoices = await self.list_documents(
            "Sales Invoice",
            filters={"docstatus": 1, "outstanding_amount": [">", 0]},
            fields=[
                "name",
                "customer",
                "posting_date",
                "due_date",
                "grand_total",
                "outstanding_amount",
                "status",
                "docstatus",
                "currency",
            ],
            limit_page_length=200,
            order_by="due_date asc",
        )
        return invoices[: max(1, min(20, int(limit)))]

    async def get_receivable_summary(self, *, as_of_date: str | None = None) -> dict[str, Any]:
        invoices = await self.list_documents(
            "Sales Invoice",
            filters={"docstatus": 1, "outstanding_amount": [">", 0]},
            fields=[
                "name",
                "customer",
                "posting_date",
                "due_date",
                "grand_total",
                "outstanding_amount",
                "status",
                "docstatus",
                "currency",
            ],
            limit_page_length=200,
            order_by="due_date asc",
        )
        return {"as_of_date": as_of_date, "invoices": invoices}

    async def get_customer_last_payment(self, customer_name: str) -> dict[str, Any] | None:
        payments = await self.list_documents(
            "Payment Entry",
            filters={"party_type": "Customer", "party": customer_name, "payment_type": "Receive", "docstatus": 1},
            fields=[
                "name",
                "party",
                "party_name",
                "posting_date",
                "paid_amount",
                "received_amount",
                "mode_of_payment",
                "status",
                "docstatus",
            ],
            limit_page_length=1,
            order_by="posting_date desc",
        )
        return payments[0] if payments else None

    async def list_sales_invoices_for_period(
        self,
        *,
        from_date: str,
        to_date: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self.list_documents(
            "Sales Invoice",
            filters={"docstatus": 1, "posting_date": ["between", [from_date, to_date]]},
            fields=[
                "name",
                "customer",
                "posting_date",
                "grand_total",
                "outstanding_amount",
                "status",
                "docstatus",
                "currency",
            ],
            limit_page_length=limit,
            order_by="posting_date desc",
        )

    async def create_sales_invoice_draft(
        self,
        *,
        customer: str,
        items: list[dict[str, Any]],
        company: str,
    ) -> ERPDocument:
        return await self.create_draft_document(
            "Sales Invoice",
            {"customer": customer, "company": company, "items": items},
        )

    async def create_quotation_draft(
        self,
        *,
        customer: str,
        items: list[dict[str, Any]],
        company: str,
    ) -> ERPDocument:
        return await self.create_draft_document(
            "Quotation",
            {
                "quotation_to": "Customer",
                "party_name": customer,
                "company": company,
                "items": items,
            },
        )

    async def create_delivery_note_draft(
        self,
        *,
        customer: str,
        items: list[dict[str, Any]],
        company: str,
    ) -> ERPDocument:
        return await self.create_draft_document(
            "Delivery Note",
            {"customer": customer, "company": company, "items": items},
        )

    async def create_payment_entry_draft(
        self,
        *,
        customer: str,
        company: str,
        amount: float,
        mode_of_payment: str,
        paid_to: str,
    ) -> ERPDocument:
        return await self.create_draft_document(
            "Payment Entry",
            {
                "payment_type": "Receive",
                "party_type": "Customer",
                "party": customer,
                "company": company,
                "paid_amount": amount,
                "received_amount": amount,
                "mode_of_payment": mode_of_payment,
                "paid_to": paid_to,
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.http.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise ERPNextTransientError(f"ERPNext transient status {response.status_code}")
                if response.status_code != 200:
                    self._raise_for_status(response)
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError, ERPNextTransientError) as exc:
                last_error = exc
                await asyncio.sleep(0.2 * (attempt + 1))
        raise ERPNextTransientError("ERPNext request failed after retries") from last_error

    async def _mutation_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.http.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ERPNextMutationUncertain(f"ERPNext {method} mutation result is uncertain") from exc
        if response.status_code in {409}:
            raise ERPNextMutationConflict("ERPNext mutation conflict")
        if response.status_code == 404:
            raise ERPNextMutationNotFound("ERPNext mutation target not found")
        if response.status_code in {429, 500, 502, 503, 504}:
            raise ERPNextMutationUncertain(f"ERPNext {method} mutation returned uncertain status {response.status_code}")
        if response.status_code != 200:
            self._raise_for_status(response)
        return response.json()

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise ERPNextAuthError("ERPNext authentication or permission failed")
        if response.status_code == 409:
            raise ERPNextMutationConflict("ERPNext conflict")
        if response.status_code == 404:
            raise ERPNextMutationNotFound("ERPNext document not found")
        if response.status_code == 417 or response.status_code == 422:
            raise ERPNextValidationError(_frappe_message(response) or "ERPNext validation failed")
        raise ERPNextError(f"ERPNext status {response.status_code}")

    def _check_doctype(self, doctype: str) -> None:
        if doctype not in READ_ALLOWED_DOCTYPES:
            raise ERPNextPermissionError(f"Doctype not allowlisted: {doctype}")

    def _check_write_doctype(self, doctype: str) -> None:
        if doctype not in WRITE_ALLOWED_DOCTYPES:
            raise ERPNextPermissionError(f"Write doctype not allowlisted: {doctype}")


def _frappe_message(response: httpx.Response) -> str:
    """The human message ERPNext attached to a validation error (no HTML, short)."""
    import re

    try:
        data = response.json()
    except ValueError:
        return ""
    texts: list[str] = []
    raw = data.get("_server_messages")
    if raw:
        try:
            for item in json.loads(raw):
                message = json.loads(item).get("message") if isinstance(item, str) else None
                if message:
                    texts.append(str(message))
        except (ValueError, AttributeError):
            pass
    if not texts and data.get("exception"):
        texts.append(str(data["exception"]).split(":", 1)[-1])
    text = re.sub(r"<[^>]+>", "", " ".join(texts))
    return re.sub(r"\s+", " ", text).strip()[:300]
